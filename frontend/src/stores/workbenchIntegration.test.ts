import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkbenchRuntimeStore } from "./workbenchRuntime";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useModelConfigurationStore } from "./modelConfiguration";
import { useExposuresStore } from "./exposures";
import { usePreparationStore } from "./preparation";
import { useWorkspaceStore } from "./workspace";
import { createPreparationDraft } from "../fixtures/preparation";
import { createWorkspaceDrafts, MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { OUTPUT_BINDING } from "../domain/observation";
import { dependencyTarget } from "../domain/modelConfiguration";
import { clonePreparation } from "../domain/preparation";
import { readWorkbench, WORKBENCH_STORAGE_KEY } from "../adapters/workbenchPersistence";
import type { ExposureConfiguration } from "../domain/exposureConfiguration";
import { legacyWorkbenchStorage } from "../testUtils/legacyWorkbenchStorage";

const id = (number: number) => `00000000-0000-4000-8000-${number.toString().padStart(12, "0")}`;
const SID = id(1), OTHER = id(2), CONFIG = id(3), PROVIDER = id(4);
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function memory() {
  return legacyWorkbenchStorage();
}
function transport() {
  const requests = new Map<string, { body: Record<string, unknown>; receipt: unknown }>();
  let rounds = 0, loseThirdReceipt = true;
  let declaration: ExposureConfiguration | null = null;
  let modelSelection: unknown = null;
  function view(sid: string) {
    const round = sid === SID ? rounds : 0;
    const chain = round ? id(100 + round) : null;
    const nodes = [AGENT_BINDINGS.A, AGENT_BINDINGS.B, OUTPUT_BINDING].map((binding, index) => ({
      node_binding_id: binding, label: ["A", "B", "Output"][index],
      status: round ? "succeeded" : "idle", run_id: round ? id(200 + round * 3 + index) : null,
      revision: round ? 4 : null,
    }));
    const head = id(400 + round);
    return {
      workflow_session_id: sid, revision: round + 1, ref_revision: round + 1,
      head_commit_id: head, mode: "deepseek", can_submit: true,
      model_selection: sid === SID ? modelSelection : null, available_actions: [],
      chains: round ? [{ chain_run_id: chain, status: "succeeded" }] : [],
      messages: [],
      observation: {
        schema_version: 1, kind: "workflow_observation", workflow_session_id: sid,
        session_revision: round + 1, head_commit_id: head, chain_run_id: chain,
        chain_status: round ? "succeeded" : "idle", diagnostic: null,
        nodes: nodes.map(node => ({
          ...node, workflow_session_id: sid, chain_run_id: chain,
          source_workflow_session_id: round ? sid : null, diagnostic: null,
          result: round ? { availability: "available", text: `round ${round}`, output_id: id(500 + round),
            source_run_id: node.run_id } : { availability: "unavailable", reason_code: "no_result" },
        })),
      },
      nodes,
    };
  }
  const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
    const body = options?.body ? JSON.parse(options.body as string) : null;
    if (path === "/api/health") return json({ mode: "deepseek" });
    if (path === "/api/model-configurations/model") return json(body.record, 201);
    if (path === "/api/exposure-configurations") { declaration = body.record; return json(declaration, 201); }
    if (path.startsWith("/api/prompt-configs/")) {
      if (options?.method === "GET") return json({ error: { code: "not_found" } }, 404);
      return json({ record: body.record, head: { kind: body.record.kind,
        id: body.record.item_id ?? body.record.group_id ?? body.record.config_id,
        definition_revision: body.record.revision, catalog_revision: body.record.revision, selectable: true } }, 201);
    }
    if (path === "/api/sessions") return json([SID, OTHER].map(sid => ({
      workflow_session_id: sid, mode: "deepseek", created_at: null,
    })));
    if (path === `/api/sessions/${SID}` || path === `/api/sessions/${OTHER}`) return json(view(path.split("/").at(-1)!));
    if (path.endsWith("/inputs")) {
      const previous = requests.get(body.idempotency_key);
      if (previous) {
        expect(body).toEqual(previous.body);
        return json(previous.receipt, 202);
      }
      rounds++;
      modelSelection = body.model_selection ?? null;
      const receipt = { workflow_session_id: SID, chain_run_id: id(100 + rounds),
        visible_message_id: id(600 + rounds), input_id: id(700 + rounds), status: "prepared" };
      requests.set(body.idempotency_key, { body, receipt });
      if (rounds === 3 && loseThirdReceipt) { loseThirdReceipt = false; throw new Error("lost reply"); }
      return json(receipt, 202);
    }
    if (path.includes("/exposures/")) {
      const sid = path.split("/")[3], current = view(sid);
      const record = declaration!;
      return json({
        schema_version: 1, kind: "workflow_exposure_read", config_id: record.config_id,
        revision: record.revision, workflow_session_id: sid, session_revision: current.revision,
        head_commit_id: current.head_commit_id, availability: "available", reason_code: null,
        registrations: record.registrations, observations: record.registrations.map(row => ({
          registrationId: row.id, workflowId: row.workflowId, workflowSessionId: sid,
          nodeBindingId: row.nodeBindingId, runId: current.nodes[0].run_id, schemaVersion: 1,
          availability: sid === SID && rounds ? "available" : "unavailable",
          ...(sid === SID && rounds ? { value: { result_text: `round ${rounds}` } }
            : { value: {}, reason: "no_result" }),
        })),
      });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  return { fetcher, requests };
}
beforeEach(() => { setActivePinia(createPinia()); vi.useFakeTimers(); });
afterEach(() => {
  useWorkbenchRuntimeStore().$dispose();
  useWorkbenchPersistenceStore().$dispose();
  useModelConfigurationStore().$dispose();
  useExposuresStore().$dispose();
  vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals();
});
describe("workbench package integration matrix", () => {
  it("runs three explicit inputs, restores unknown identities and declarations, and isolates session observations", async () => {
    const storage = memory(), server = transport();
    vi.stubGlobal("fetch", server.fetcher);
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const models = useModelConfigurationStore(), exposures = useExposuresStore();
    models.addNode(MAIN_WORKFLOW_ID, { x: 20, y: -40 });
    const workflowId = useWorkspaceStore().activeWorkflowId;
    const draft = models.getDraft(workflowId)!;
    const model = draft.nodes[0]!.id;
    draft.configId = CONFIG;
    draft.nodes[0].provider_ref = { provider_id: PROVIDER, revision: 1 };
    models.connect(workflowId, model, dependencyTarget("A"), "model-out", "model-in");
    models.connect(workflowId, model, dependencyTarget("B"), "model-out", "model-in");
    exposures.register(workflowId, "A", "A.result", "result", ["result_text"]);
    expect(await exposures.publish(workflowId)).toBe(true);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(workflowId);
    await runtime.restoreSession(SID);
    for (let round = 1; round <= 3; round++) {
      runtime.setInputText(`input ${round}`);
      await runtime.submitPrimary();
      if (round < 3) {
        expect(runtime.unknown).toBeNull();
        expect(runtime.nodeObservation("A")?.result).toMatchObject({ text: `round ${round}` });
      }
    }
    const original = clonePreparation(runtime.unknown);
    expect(original?.body.model_selection).toEqual({
      schema_version: 1, kind: "workflow_model_selection", config_id: CONFIG, revision: 3,
    });
    models.getDraft(workflowId)!.nodes[0].parameters.model = "later unsent draft";
    usePreparationStore().setRootText(workflowId, "A", "later unsent input");
    expect(persistence.save()).toBe(true);
    expect(readWorkbench(storage).value?.schemaVersion).toBe(6);
    const beforeRestore = server.fetcher.mock.calls.length;
    runtime.$dispose(); persistence.$dispose(); models.$dispose(); exposures.$dispose();
    setActivePinia(createPinia());
    const restoredPersistence = useWorkbenchPersistenceStore();
    restoredPersistence.initialize(storage);
    const restored = useWorkbenchRuntimeStore(), restoredExposures = useExposuresStore();
    expect(server.fetcher.mock.calls).toHaveLength(beforeRestore);
    expect(restoredExposures.observations).toEqual({});
    expect(restoredExposures.referenceFor(workflowId)?.revision).toBe(1);
    await restored.activateWorkflow(workflowId);
    expect(restored.unknown).toEqual(original);
    await restored.replayUnknown();
    expect(restored.unknown).toBeNull();
    expect(server.requests.size).toBe(3);
    expect(restored.nodeObservation("A")?.result).toMatchObject({ text: "round 3" });
    const graph = JSON.stringify(useModelConfigurationStore().drafts);
    await restored.restoreSession(OTHER);
    await restoredExposures.observePublished(workflowId, restored.session!);
    expect(restored.nodeObservation("A")?.run_id).toBeNull();
    expect(Object.values(restoredExposures.observations).every(row => row.availability === "unavailable")).toBe(true);
    await restored.restoreSession(SID);
    await restoredExposures.observePublished(workflowId, restored.session!);
    expect(Object.values(restoredExposures.observations)[0].value).toEqual({ result_text: "round 3" });
    expect(JSON.stringify(useModelConfigurationStore().drafts)).toBe(graph);
    expect(server.fetcher.mock.calls.filter(([path]) => path === "/api/model-configurations/model")).toHaveLength(3);
    expect(JSON.parse(storage.getItem()!).runtime.sessions.every((row: Record<string, unknown>) =>
      !Object.hasOwn(row, "view") && !Object.hasOwn(row, "observation"))).toBe(true);
  });
  it("migrates v2 without dispatch and disposes pending polling and late reads", async () => {
    const storage = memory();
    const saved = { schemaVersion: 2, kind: "fixed-workbench", revision: 1, savedAt: "2026-10-01T00:00:00Z",
      activeWorkflowId: MAIN_WORKFLOW_ID, selectedWorkflowId: MAIN_WORKFLOW_ID,
      layouts: Object.fromEntries(Object.entries(createWorkspaceDrafts()).map(([workflowId, draft]) => [workflowId,
        Object.fromEntries(draft.nodes.map(node => [node.id, { ...node.position }]))])),
      preparations: [createPreparationDraft(MAIN_WORKFLOW_ID, "A"), createPreparationDraft(MAIN_WORKFLOW_ID, "B")],
      registrations: [], models: { schemaVersion: 1, drafts: [], pending: null },
      runtime: { schemaVersion: 1, kind: "workbench-runtime", selectedSessions: [], sessions: [], selectionPending: null },
    };
    storage.setItem(WORKBENCH_STORAGE_KEY, JSON.stringify(saved));
    expect(readWorkbench(storage).value?.schemaVersion).toBe(2);
    setActivePinia(createPinia());
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const restored = useWorkbenchPersistenceStore();
    restored.initialize(storage);
    expect(restored.status).toBe("saved");
    expect(restored.save()).toBe(true);
    expect(readWorkbench(storage).value?.schemaVersion).toBe(6);
    expect(fetcher).not.toHaveBeenCalled();
    let resolve!: (response: Response) => void;
    fetcher.mockImplementation(() => new Promise<Response>(done => { resolve = done; }));
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    const reading = runtime.restoreSession(SID);
    runtime.$dispose();
    resolve(json({ invalid: "late" }));
    await reading;
    expect(runtime.session).toBeNull();
    await vi.advanceTimersByTimeAsync(3600);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
