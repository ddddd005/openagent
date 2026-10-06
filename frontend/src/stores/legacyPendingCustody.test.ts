import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { useWorkbenchRuntimeStore, validRuntimeSnapshot, type PendingMutation, type WorkbenchRuntimeSnapshot } from "./workbenchRuntime";
import { useModelConfigurationStore } from "./modelConfiguration";
import { useExposuresStore } from "./exposures";
import { useGlobalContentStore } from "./globalContent";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import type { ExactPromptSelection } from "../adapters/workbenchSessions";
import { WORKBENCH_STORAGE_KEY, type SavedWorkbench } from "../adapters/workbenchPersistence";
import { encodeUnifiedWorkbench } from "../adapters/unifiedWorkbenchDocument";
import { legacyWorkbenchRaw } from "../testUtils/legacyWorkbenchStorage";
import { isConfigurationMutation, type ConfigurationMutation, type ExactModelSelection } from "../domain/modelConfiguration";
import { isExposureMutation, type ExposureMutation } from "../domain/exposureConfiguration";
import type { GlobalContent } from "../domain/workbenchResources";

const id = (value: number) => `00000000-0000-4000-8000-${String(value).padStart(12, "0")}`;
const SID = id(8101), KEY = id(8102), RUN = id(8103), TARGET = id(8104), CONFIG = id(8105);
const CONTENT_KEY = "workflow-workbench:content-drafts:v1";
const TEXT = "  original\r\n{\"quoted\":\"value\"}\n\\literal  ";
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
const selection: ExactPromptSelection = { schema_version: 1, kind: "workflow_prompt_selection", nodes: {
  A: { config_id: CONFIG, revision: 2 },
  B: { config_id: id(8115), revision: 4 },
} };
const modelSelection: ExactModelSelection = { schema_version: 1, kind: "workflow_model_selection", config_id: id(8106), revision: 3 };
const promptConfig = { schema_version: 1, kind: "config", config_id: CONFIG, revision: 2, name: "Original", inputs: [] };

function runtimeCommand(action: string): PendingMutation {
  const base = { requestId: KEY, action, replayable: true };
  const body = { idempotency_key: KEY };
  if (action === "create-session") return { ...base, replayable: false,
    path: "/api/sessions", body: { workflow_id: MAIN_WORKFLOW_ID } };
  if (action === "start") return { ...base, path: `/api/sessions/${SID}/inputs`,
    body: { ...body, text: TEXT, prompt_selection: clone(selection), model_selection: clone(modelSelection) } };
  if (action === "save-config") return { ...base, path: "/api/prompt-configs/config",
    body: { ...body, record: clone(promptConfig), expected_revision: 1 } };
  if (action.startsWith("copy-workflow:")) return { ...base, path: `/api/sessions/${SID}/copy`,
    body: { ...body, expected_source_revision: 7, expected_data_revision: 4, target_workflow_id: TARGET } };
  if (action === "write-variables") return { ...base, path: `/api/sessions/${SID}/variables/write`, body: {
    ...body, expected_session_revision: 7, expected_ref_revision: 4, expected_head_commit_id: id(8107),
    prompt_config: { items: [], groups: [], config: clone(promptConfig) },
    name: "original", value: TEXT, expected_variable_revision: 3,
  } };
  if (action === "fork") return { ...base, path: `/api/sessions/${SID}/branches`,
    body: { ...body, visible_message_id: id(8108), candidate_id: id(8109), expected_source_revision: 7 } };
  if (action === "candidate-select" || action === "reroll") return { ...base,
    path: `/api/sessions/${SID}/${action === "reroll" ? "chains" : "candidates"}/${RUN}/${action === "reroll" ? "reroll" : "select"}`,
    body: { ...body, expected_session_revision: 7, expected_ref_revision: 4, expected_head_commit_id: id(8107) } };
  const endpoint = ({ retry_archive: "retry-archive", retry_publish: "retry-publish" } as Record<string, string>)[action] ?? action;
  return { ...base, path: `/api/sessions/${SID}/runs/${RUN}/${endpoint}`, body: {
    ...body, expected_session_revision: 7,
    ...(["resume", "interrupt", "extend_budget"].includes(action) ? { expected_run_revision: 3 } : {}),
    ...(action === "extend_budget" ? { additional_model_requests: 2, additional_model_attempts: 5 } : {}),
  } };
}

function runtimeSnapshot(version: 1 | 2, command: PendingMutation | null, select = false): WorkbenchRuntimeSnapshot {
  const sessionId = command?.action === "create-session" ? null : SID;
  return { schemaVersion: version, kind: "workbench-runtime",
    selectedSessions: [{ workflowId: MAIN_WORKFLOW_ID, sessionId }],
    sessions: [{ workflowId: MAIN_WORKFLOW_ID, sessionId, pending: clone(command),
      requiresRefresh: false, promptSelection: clone(selection),
      ...(version === 2 ? { modelSelection: clone(modelSelection) } : {}) }],
    selectionPending: select ? { path: "/api/active-session", requestId: KEY,
      action: "select-session", replayable: true,
      body: { workflow_session_id: SID, expected_selection_revision: 4, idempotency_key: KEY } } : null };
}

function modelCommand(kind: "provider" | "model"): ConfigurationMutation {
  return { path: `/api/model-configurations/${kind}`, body: { expected_revision: 4, idempotency_key: KEY,
    record: kind === "provider" ? {
      schema_version: 1, kind: "chat_provider", provider_id: CONFIG, revision: 5,
      name: "Original Provider", protocol: "chat", base_url: "https://original.example/v1",
      credential_ref: "env:DEEPSEEK_API_KEY", enabled: true,
    } : { schema_version: 1, kind: "workflow_model_configuration", config_id: CONFIG, revision: 5,
      workflow_id: MAIN_WORKFLOW_ID, nodes: [{ id: id(8110), position: { x: 34, y: -56 },
        provider_ref: { provider_id: id(8111), revision: 2 },
        parameters: { model: "original-model", temperature: 0.45, max_tokens: 2048 } }],
      edges: [{ id: id(8112), source: id(8110), target_binding_id: AGENT_BINDINGS.A }] } } };
}

function exposureCommand(): ExposureMutation {
  return { expected_revision: 4, idempotency_key: KEY,
    record: { schema_version: 1, kind: "workflow_exposure_configuration", config_id: CONFIG,
      workflow_id: MAIN_WORKFLOW_ID, revision: 5, registrations: [{
        schemaVersion: 1, id: id(8113), workflowId: MAIN_WORKFLOW_ID, stage: "A",
        nodeBindingId: AGENT_BINDINGS.A, publicName: "original.state", kind: "state",
        type: "public_agent_state", fields: ["status", "revision"],
      }] } };
}

function content(): GlobalContent {
  return { schema_version: 1, kind: "global_prompt", resource_id: CONFIG, revision: 5,
    name: "Original Content", enabled: true, members: [{
      id: id(8114), name: "Original Member", text: TEXT, role: "system", placement: "before",
      depth: null, order: 12, enabled: true,
    }] };
}

function memory(initial: Record<string, string>) {
  const entries = new Map(Object.entries(initial));
  return { entries,
    getItem: vi.fn((key: string) => entries.get(key) ?? null),
    setItem: vi.fn((key: string, raw: string) => { entries.set(key, raw); }),
    removeItem: vi.fn((key: string) => { entries.delete(key); }) };
}

let pinia: Pinia;
function reload() {
  disposePinia(pinia);
  pinia = createPinia();
  setActivePinia(pinia);
}
beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("Network access forbidden in pending reconciliation"); }));
});
afterEach(() => {
  disposePinia(pinia);
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("legacy pending custody without command replay", () => {
  const actions = ["create-session", "start", "save-config", `copy-workflow:${TARGET}`, "write-variables",
    "fork", "candidate-select", "reroll", "resume", "interrupt", "extend_budget", "retry_archive", "retry_publish"];

  it.each([1, 2] as const)("retains every original schema-%s runtime command through restore and local reconciliation", async version => {
    for (const action of actions) {
      const command = runtimeCommand(action);
      const source = runtimeSnapshot(version, command);
      const original = clone(source);
      expect(validRuntimeSnapshot(source), action).toBe(true);
      const runtime = useWorkbenchRuntimeStore();
      const persist = vi.fn(() => { throw new Error("Pending must not be cleared or rewritten"); });
      runtime.setMutationPersistenceGuard(persist);
      expect(runtime.restoreRuntimeSnapshot(source), action).toBe(true);
      runtime.workflowId = MAIN_WORKFLOW_ID;
      source.sessions[0]!.pending!.body = { rewritten: true };
      await runtime.replayUnknown();
      await runtime.replayUnknown();
      const exported = runtime.exportRuntimeSnapshot();
      expect(exported.sessions[0]!.pending, action).toEqual(original.sessions[0]!.pending);
      expect(exported.sessions[0]!.promptSelection).toEqual(selection);
      expect(exported.sessions[0]!.modelSelection).toEqual(version === 2 ? modelSelection : null);
      expect(exported.sessions[0]!.pending!.requestId).toBe(KEY);
      expect(runtime.error?.kind).toBe("unknown");
      expect(runtime.error?.reason).toContain("unresolved");
      expect(persist).not.toHaveBeenCalled();
      expect(fetch).not.toHaveBeenCalled();
      exported.sessions[0]!.pending!.body = { exportedCopyChanged: true };
      expect(runtime.exportRuntimeSnapshot().sessions[0]!.pending).toEqual(command);
      reload();
      const recovered = useWorkbenchRuntimeStore();
      const guard = vi.fn(() => false);
      recovered.setMutationPersistenceGuard(guard);
      expect(recovered.restoreRuntimeSnapshot(runtimeSnapshot(version, command))).toBe(true);
      recovered.workflowId = MAIN_WORKFLOW_ID;
      await recovered.replayUnknown();
      expect(recovered.exportRuntimeSnapshot().sessions[0]!.pending).toEqual(command);
      expect(guard).not.toHaveBeenCalled();
      expect(fetch).not.toHaveBeenCalled();
      reload();
    }
  });

  it.each([1, 2] as const)("retains schema-%s active-session selection without observing or resending it", async version => {
    const source = runtimeSnapshot(version, null, true);
    const runtime = useWorkbenchRuntimeStore();
    const persist = vi.fn(() => false);
    runtime.setMutationPersistenceGuard(persist);
    expect(runtime.restoreRuntimeSnapshot(source)).toBe(true);
    runtime.workflowId = MAIN_WORKFLOW_ID;
    await runtime.replayUnknown();
    const exported = runtime.exportRuntimeSnapshot();
    expect(exported.selectionPending).toEqual(source.selectionPending);
    reload();
    const recovered = useWorkbenchRuntimeStore();
    expect(recovered.restoreRuntimeSnapshot(exported)).toBe(true);
    recovered.workflowId = MAIN_WORKFLOW_ID;
    await recovered.replayUnknown();
    expect(recovered.exportRuntimeSnapshot().selectionPending).toEqual(source.selectionPending);
    expect(persist).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("refuses an incompatible runtime restore without replacing the retained command", async () => {
    const runtime = useWorkbenchRuntimeStore();
    const original = runtimeSnapshot(2, runtimeCommand("start"));
    expect(runtime.restoreRuntimeSnapshot(original)).toBe(true);
    const corrupted = clone(original);
    corrupted.sessions[0]!.pending!.body.idempotency_key = id(8199);
    expect(runtime.restoreRuntimeSnapshot(corrupted)).toBe(false);
    runtime.workflowId = MAIN_WORKFLOW_ID;
    await runtime.replayUnknown();
    expect(runtime.exportRuntimeSnapshot().sessions[0]!.pending).toEqual(original.sessions[0]!.pending);
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each(["provider", "model"] as const)("preserves the full old %s publication without network or persistence callbacks", async kind => {
    const command = modelCommand(kind);
    expect(isConfigurationMutation(command)).toBe(true);
    const source = { schemaVersion: 1, drafts: [], pending: clone(command) };
    const models = useModelConfigurationStore();
    const persist = vi.fn(() => { throw new Error("No request may be cleared"); });
    models.setPersistenceGuard(persist);
    expect(models.restoreConfiguration(source)).toBe(true);
    source.pending.body.idempotency_key = id(8199);
    await models.reconcile();
    await models.reconcile();
    expect(models.exportConfiguration().pending).toEqual(command);
    expect(models.error?.kind).toBe("unknown");
    expect(models.locked).toBe(true);
    expect(persist).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
    const exported = models.exportConfiguration();
    reload();
    const recovered = useModelConfigurationStore();
    const guard = vi.fn(() => false);
    recovered.setPersistenceGuard(guard);
    expect(recovered.restoreConfiguration(exported)).toBe(true);
    await recovered.reconcile();
    expect(recovered.exportConfiguration().pending).toEqual(command);
    expect(guard).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("retains rejected model restore evidence and its old pending unchanged", async () => {
    const models = useModelConfigurationStore();
    const source = { schemaVersion: 1, drafts: [], pending: modelCommand("provider") };
    expect(models.restoreConfiguration(source)).toBe(true);
    const corrupted = clone(source) as unknown as { pending: Record<string, unknown> };
    corrupted.pending.path = "/api/model-configurations/unknown";
    expect(models.restoreConfiguration(corrupted)).toBe(false);
    await models.reconcile();
    expect(models.exportConfiguration().pending).toEqual(source.pending);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("retains the original exposure request including nested declarations across reload", async () => {
    const command = exposureCommand();
    expect(isExposureMutation(command)).toBe(true);
    const source = { schemaVersion: 1, publications: [], pending: clone(command) };
    const exposures = useExposuresStore();
    const guard = vi.fn(() => { throw new Error("No exposure request may be cleared"); });
    exposures.setPersistenceGuard(guard);
    expect(exposures.restoreConfiguration(source)).toBe(true);
    source.pending.record.registrations[0]!.publicName = "changed input";
    await exposures.reconcile();
    expect(exposures.exportConfiguration().pending).toEqual(command);
    expect(exposures.locked).toBe(true);
    expect(exposures.error?.kind).toBe("unknown");
    const exported = exposures.exportConfiguration();
    reload();
    const recovered = useExposuresStore();
    recovered.setPersistenceGuard(guard);
    expect(recovered.restoreConfiguration(exported)).toBe(true);
    await recovered.reconcile();
    expect(recovered.exportConfiguration().pending).toEqual(command);
    expect(guard).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([false, true])("does not rewrite content pending or raw storage when reconciliation reads are unavailable: %s", async unavailable => {
    const command = { record: content(), expected_revision: 4, idempotency_key: KEY };
    const raw = JSON.stringify({ schemaVersion: 1, drafts: { [CONFIG]: content() }, selectedId: CONFIG, pending: command }, null, 2);
    const unrelated = "original unrelated browser key";
    const storage = memory({ [CONTENT_KEY]: raw, [WORKBENCH_STORAGE_KEY]: unrelated });
    vi.stubGlobal("window", { localStorage: storage });
    const fetcher = vi.fn(async (_path: string, init?: RequestInit) => {
      if (init?.method === "POST" || init?.body) throw new Error("Legacy mutation forbidden");
      if (unavailable) throw new Error("Read unavailable");
      return new Response("[]");
    });
    vi.stubGlobal("fetch", fetcher);
    const store = useGlobalContentStore();
    await store.initialize();
    const reads = fetcher.mock.calls.length;
    expect(store.pending).toEqual(command);
    await store.save(true);
    await store.save();
    await store.remove();
    store.discard();
    store.create("role_card");
    expect(fetcher).toHaveBeenCalledTimes(reads);
    expect(store.pending).toEqual(command);
    expect(storage.getItem(CONTENT_KEY)).toBe(raw);
    expect(storage.getItem(WORKBENCH_STORAGE_KEY)).toBe(unrelated);
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(storage.removeItem).not.toHaveBeenCalled();
    reload();
    const recovered = useGlobalContentStore();
    await recovered.initialize();
    const reloadedReads = fetcher.mock.calls.length;
    await recovered.save(true);
    expect(recovered.pending).toEqual(command);
    expect(fetcher).toHaveBeenCalledTimes(reloadedReads);
    expect(storage.getItem(CONTENT_KEY)).toBe(raw);
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(storage.removeItem).not.toHaveBeenCalled();
  });
});

describe("saved workbench pending restoration", () => {
  it.each([1, 2, 3, 4, 5, 6] as const)("reads schema-%s without overwriting its raw or losing command coordinates", async version => {
    const value = JSON.parse(legacyWorkbenchRaw()) as SavedWorkbench;
    value.runtime = runtimeSnapshot(version === 1 ? 1 : 2, runtimeCommand("start"), true);
    value.models = { schemaVersion: 1, drafts: [], pending: modelCommand("model") };
    value.exposures = { schemaVersion: 1, publications: [], pending: exposureCommand() };
    value.schemaVersion = version;
    if (version < 5) delete value.catalog;
    if (version < 3) delete value.exposures;
    if (version < 2) delete value.models;
    const serialized = version === 6
      ? encodeUnifiedWorkbench({ ...value, graph: { schema_version: 1, entries: {} } }) : value;
    const raw = JSON.stringify(serialized, null, 2);
    const storage = memory({ [WORKBENCH_STORAGE_KEY]: raw, [CONTENT_KEY]: "separate legacy content" });
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    expect(persistence.status).toBe("saved");
    const runtime = useWorkbenchRuntimeStore();
    runtime.workflowId = MAIN_WORKFLOW_ID;
    await runtime.replayUnknown();
    if (version >= 2) await useModelConfigurationStore().reconcile();
    if (version >= 3) await useExposuresStore().reconcile();
    vi.advanceTimersByTime(300);
    expect(runtime.exportRuntimeSnapshot().sessions[0]!.pending).toEqual(runtimeCommand("start"));
    expect(runtime.exportRuntimeSnapshot().selectionPending).toEqual(runtimeSnapshot(2, null, true).selectionPending);
    if (version >= 2) expect(useModelConfigurationStore().exportConfiguration().pending).toEqual(modelCommand("model"));
    if (version >= 3) expect(useExposuresStore().exportConfiguration().pending).toEqual(exposureCommand());
    expect(storage.getItem(WORKBENCH_STORAGE_KEY)).toBe(raw);
    expect(storage.getItem(CONTENT_KEY)).toBe("separate legacy content");
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(storage.removeItem).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
    reload();
    useWorkbenchPersistenceStore().initialize(storage);
    expect(useWorkbenchRuntimeStore().exportRuntimeSnapshot().sessions[0]!.pending).toEqual(runtimeCommand("start"));
    expect(storage.getItem(WORKBENCH_STORAGE_KEY)).toBe(raw);
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });
});
