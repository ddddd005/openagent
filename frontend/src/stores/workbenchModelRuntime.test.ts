import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkbenchRuntimeStore, validRuntimeSnapshot } from "./workbenchRuntime";
import { useModelConfigurationStore } from "./modelConfiguration";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { dependencyTarget, type ExactModelSelection } from "../domain/modelConfiguration";

const SID = "00000000-0000-4000-8000-000000000001";
const OTHER = "00000000-0000-4000-8000-000000000002";
const CONFIG = "00000000-0000-4000-8000-000000000003";
const PROVIDER = "00000000-0000-4000-8000-000000000004";
const RUN = "00000000-0000-4000-8000-000000000005";
const CHAIN = "00000000-0000-4000-8000-000000000006";
const USER = "00000000-0000-4000-8000-000000000007";
const HEAD = "00000000-0000-4000-8000-000000000009";
const choice = (revision = 1): ExactModelSelection => ({
  schema_version: 1, kind: "workflow_model_selection", config_id: CONFIG, revision,
});
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function view(sid = SID, selection: ExactModelSelection | null = null) {
  return {
    workflow_session_id: sid, revision: 1, mode: "deepseek", can_submit: true,
    ref_revision: 1, head_commit_id: HEAD, available_actions: [], messages: [], chains: [],
    model_selection: selection,
    nodes: [...Object.values(AGENT_BINDINGS), "7be319b8-30bd-4674-b7bf-d1cf54a1a10a"].map((binding) => ({
      node_binding_id: binding, label: "Agent", status: "idle", run_id: null, revision: null,
    })),
  };
}
function configured() {
  const models = useModelConfigurationStore();
  const id = models.addNode(MAIN_WORKFLOW_ID, { x: 10, y: -20 })!;
  const draft = models.getDraft(MAIN_WORKFLOW_ID)!;
  draft.configId = CONFIG;
  draft.nodes[0].provider_ref = { provider_id: PROVIDER, revision: 1 };
  models.connect(MAIN_WORKFLOW_ID, id, dependencyTarget("A"), "model-out", "model-in");
  models.connect(MAIN_WORKFLOW_ID, id, dependencyTarget("B"), "model-out", "model-in");
  models.setPersistenceGuard(() => true);
  return models;
}
function transport(inputFailure = false, modelFailure = false) {
  const selections = new Map<string, ExactModelSelection>();
  const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
    const body = options?.body ? JSON.parse(options.body as string) : null;
    if (path === "/api/health") return json({ mode: "deepseek" });
    if (path === "/api/model-configurations/model") {
      if (modelFailure) throw new Error("unknown model save");
      return json(body.record, 201);
    }
    if (path.startsWith("/api/prompt-configs/")) {
      if (options?.method === "GET") return json({ error: { code: "not_found" } }, 404);
      return json({ record: body.record, head: {
        kind: body.record.kind, id: body.record.item_id ?? body.record.group_id ?? body.record.config_id,
        definition_revision: 1, catalog_revision: 1, selectable: true,
      } }, 201);
    }
    if (path.endsWith("/inputs")) {
      if (inputFailure) throw new Error("unknown input");
      if (body.model_selection) selections.set(SID, body.model_selection);
      return json({ workflow_session_id: SID, chain_run_id: CHAIN, visible_message_id: USER, input_id: RUN, status: "prepared" }, 202);
    }
    if (path === "/api/sessions") return options?.method === "POST"
      ? json(view(), 201) : json([{ workflow_session_id: SID, created_at: null, mode: "deepseek" }]);
    if (path === `/api/sessions/${SID}`) return json(view(SID, selections.get(SID) ?? null));
    return json(view(OTHER, choice(9)));
  });
  return fetcher;
}
beforeEach(() => { setActivePinia(createPinia()); vi.useFakeTimers(); });
afterEach(() => {
  useModelConfigurationStore().$dispose();
  vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals();
});

describe("exact model selection in workbench requests", () => {
  it("publishes and sends the exact revision on each explicit start and never puts provider metadata in runtime storage", async () => {
    const models = configured();
    const runtime = useWorkbenchRuntimeStore();
    const fetcher = transport();
    vi.stubGlobal("fetch", fetcher);
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("first");
    await runtime.submitPrimary();
    runtime.setInputText("second");
    models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0].parameters.model = "changed";
    await runtime.submitPrimary();
    const inputs = fetcher.mock.calls.filter(([path]) => path.endsWith("/inputs"));
    expect(inputs).toHaveLength(2);
    expect(JSON.parse(inputs[0][1]!.body as string).model_selection).toEqual(choice());
    expect(JSON.parse(inputs[1][1]!.body as string).model_selection).toEqual(choice(2));
    expect(runtime.currentModelSelection).toEqual(choice(2));
    const snapshot = runtime.exportRuntimeSnapshot();
    expect(validRuntimeSnapshot(snapshot)).toBe(true);
    expect(JSON.stringify(snapshot)).not.toMatch(/credential|base_url|provider_id/);
  });
  it("restores an unknown input with the same model revision, body and key even after editing the draft", async () => {
    const models = configured();
    const runtime = useWorkbenchRuntimeStore();
    const fetcher = transport(true);
    vi.stubGlobal("fetch", fetcher);
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("lost input");
    await runtime.submitPrimary();
    const original = structuredClone(runtime.exportRuntimeSnapshot());
    expect(runtime.unknown?.body.model_selection).toEqual(choice());
    models.getDraft(MAIN_WORKFLOW_ID)!.nodes[0].parameters.model = "later";
    setActivePinia(createPinia());
    const restored = useWorkbenchRuntimeStore();
    expect(restored.restoreRuntimeSnapshot(original)).toBe(true);
    const recovered = transport();
    vi.stubGlobal("fetch", recovered);
    await restored.activateWorkflow(MAIN_WORKFLOW_ID);
    const request = structuredClone(restored.exportRuntimeSnapshot().sessions[0].pending);
    await restored.replayUnknown();
    const input = recovered.mock.calls.find(([path]) => path.endsWith("/inputs"))!;
    expect(JSON.parse(input[1]!.body as string)).toEqual(request!.body);
    expect(recovered.mock.calls.some(([path]) => path === "/api/model-configurations/model")).toBe(false);
    expect(restored.unknown).toBeNull();
  });
  it("keeps uncertain model publication locked and cannot create a session or input", async () => {
    const models = configured();
    const runtime = useWorkbenchRuntimeStore();
    const fetcher = transport(false, true);
    vi.stubGlobal("fetch", fetcher);
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("blocked");
    await runtime.submitPrimary();
    expect(models.pending).not.toBeNull();
    expect(runtime.locked).toBe(true);
    await runtime.submitPrimary();
    expect(fetcher.mock.calls.filter(([path]) => path === "/api/model-configurations/model")).toHaveLength(1);
    expect(fetcher.mock.calls.some(([path]) => path === "/api/sessions" || path.endsWith("/inputs"))).toBe(false);
  });
  it("observes authoritative choices per session without applying them to the workflow graph", async () => {
    const models = configured();
    const draft = JSON.stringify(models.drafts);
    const fetcher = vi.fn(async (path: string, _options?: RequestInit) =>
      json(view(path.endsWith(SID) ? SID : OTHER, choice(path.endsWith(SID) ? 1 : 9))));
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    expect(runtime.currentModelSelection).toEqual(choice());
    await runtime.restoreSession(OTHER);
    expect(runtime.currentModelSelection).toEqual(choice(9));
    await runtime.restoreSession(SID);
    expect(runtime.currentModelSelection).toEqual(choice());
    expect(JSON.stringify(models.drafts)).toBe(draft);
    expect(fetcher.mock.calls.every(([, options]) => options?.method === "GET")).toBe(true);
  });
  it("resumes the same run without publishing edited model nodes", async () => {
    configured();
    const paused = { ...view(SID, choice()), can_submit: false, available_actions: ["resume"],
      chains: [{ chain_run_id: CHAIN, status: "paused" }],
      nodes: view().nodes.map((node) => node.node_binding_id === AGENT_BINDINGS.A
        ? { ...node, status: "paused", run_id: RUN, revision: 2 } : node),
    };
    const fetcher = vi.fn(async (path: string, _options?: RequestInit) => path === "/api/health" ? json({ mode: "deepseek" })
      : path.endsWith("/resume") ? json({ workflow_session_id: SID, chain_run_id: CHAIN, run_id: RUN, status: "running" })
        : json(paused));
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    await runtime.submitPrimary();
    const command = fetcher.mock.calls.find(([path]) => path.endsWith("/resume"))!;
    expect(JSON.parse(command[1]!.body as string)).toMatchObject({ expected_run_revision: 2 });
    expect(fetcher.mock.calls.some(([path]) => path.includes("configurations"))).toBe(false);
  });
  it("upgrades old v1 runtime records with an empty model choice and rejects corrupt v2 refs", async () => {
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    const old = runtime.exportRuntimeSnapshot();
    old.schemaVersion = 1;
    old.sessions.forEach((row) => { delete row.modelSelection; });
    expect(runtime.restoreRuntimeSnapshot(old)).toBe(true);
    const upgraded = runtime.exportRuntimeSnapshot();
    expect(upgraded.schemaVersion).toBe(2);
    expect(upgraded.sessions[0].modelSelection).toBeNull();
    upgraded.sessions[0].modelSelection = { ...choice(), revision: 0 };
    expect(runtime.restoreRuntimeSnapshot(upgraded)).toBe(false);
  });
});
