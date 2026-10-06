import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { readWorkbench, WORKBENCH_STORAGE_KEY, writeWorkbench, type SavedWorkbench } from "../adapters/workbenchPersistence";
import { legacyWorkbenchRaw } from "../testUtils/legacyWorkbenchStorage";
import { EMPTY_WORKFLOW_ID, MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { graphClone, graphUuid, newGraph, upgradeGraph, type GraphCommand, type GraphEntry } from "../domain/workflowGraph";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkspaceStore } from "./workspace";
import { useWorkflowGraphStore } from "./workflowGraph";
import { usePreparationStore } from "./preparation";
import { useWorkbenchRuntimeStore } from "./workbenchRuntime";
import { useModelConfigurationStore } from "./modelConfiguration";
import { useExposuresStore } from "./exposures";

let pinia: Pinia;
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia); vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn());
});
afterEach(() => {
  disposePinia(pinia); vi.clearAllTimers(); vi.useRealTimers();
  vi.unstubAllGlobals(); vi.restoreAllMocks();
});
function reopen() {
  disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
}
function memory(initial: string | null = null, other: Record<string, string> = {}) {
  const values = new Map(Object.entries(other));
  if (initial !== null) values.set(WORKBENCH_STORAGE_KEY, initial);
  const getItem = vi.fn((key: string) => values.get(key) ?? null);
  const setItem = vi.fn((key: string, value: string) => { values.set(key, value); });
  return { getItem, setItem, value: (key = WORKBENCH_STORAGE_KEY) => values.get(key) ?? null };
}
function emptyRuntime() {
  return { schemaVersion: 2, kind: "workbench-runtime", selectedSessions: [], sessions: [], selectionPending: null };
}
function currentSave(entry?: GraphEntry): SavedWorkbench {
  const document = upgradeGraph(newGraph("Already saved empty graph"), []);
  document.revision = 7;
  const id = entry?.document.workflow_definition_id ?? document.workflow_definition_id;
  return { schemaVersion: 6, kind: "fixed-workbench", revision: 12, savedAt: "2026-10-01T00:00:00.000Z",
    activeWorkflowId: id, selectedWorkflowId: id,
    layouts: { [id]: {} }, preparations: [], registrations: [], runtime: emptyRuntime(),
    models: { schemaVersion: 1, drafts: [], pending: null },
    exposures: { schemaVersion: 1, publications: [], pending: null },
    catalog: [{ id, title: entry?.document.name ?? document.name, description: "saved before loading", nodeCount: 0, state: "saved" }],
    graph: { schema_version: 1, entries: { [id]: entry ?? {
      document, saved_document: graphClone(document), saved_revision: document.revision, session_id: null, pending: null,
    } } } };
}
function legacyVersion(version: 1 | 2 | 3 | 4 | 5): SavedWorkbench {
  const value = JSON.parse(legacyWorkbenchRaw()) as SavedWorkbench;
  value.schemaVersion = version;
  if (version < 5) delete value.catalog;
  if (version < 3) delete value.exposures;
  if (version < 2) delete value.models;
  return value;
}
describe("workbench initialization and archived authority boundaries", () => {
  it("creates exactly one empty ordinary graph only for an absent readable record", () => {
    const storage = memory(), preparation = usePreparationStore();
    const getDraft = vi.spyOn(preparation, "getDraft");
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    const workspace = useWorkspaceStore(), graph = useWorkflowGraphStore();
    expect(persistence.status).toBe("saved"); expect(persistence.error).toBeNull();
    expect(workspace.workflows).toHaveLength(1);
    const id = workspace.activeWorkflowId;
    expect(graphUuid(id)).toBe(true); expect(workspace.selectedWorkflowId).toBe(id);
    expect(workspace.workflows[0]).toMatchObject({ id, nodeCount: 0, state: "draft" });
    expect(Object.keys(graph.entries)).toEqual([id]);
    expect(graph.entries[id]!.document).toMatchObject({ workflow_definition_id: id, nodes: [], edges: [] });
    expect(workspace.nodes).toEqual([]); expect(workspace.edges).toEqual([]);
    expect(Object.keys(workspace.drafts)).toEqual([id]);
    expect(Object.keys(workspace.initialLayouts)).toEqual([id]);
    expect(Object.keys(workspace.histories)).toEqual([id]);
    expect(preparation.exportConfiguration()).toEqual([]); expect(getDraft).not.toHaveBeenCalled();
    expect(useModelConfigurationStore().exportConfiguration().drafts).toEqual([]);
    expect(useWorkbenchRuntimeStore().exportRuntimeSnapshot().sessions).toEqual([]);
    expect(useExposuresStore().registrations).toEqual([]);
    const saved = JSON.parse(storage.value()!);
    expect(saved.schemaVersion).toBe(6); expect(saved.catalog.map((row: { id: string }) => row.id)).toEqual([id]);
    expect(Object.keys(saved.documents)).toEqual([id]);
    expect(saved.documents[id].compatibility).toBeUndefined();
    expect(storage.value()).not.toContain(MAIN_WORKFLOW_ID);
    expect(storage.value()).not.toContain(EMPTY_WORKFLOW_ID);
    expect(storage.setItem).toHaveBeenCalledTimes(1);
    persistence.initialize(storage); vi.advanceTimersByTime(250);
    expect(workspace.activeWorkflowId).toBe(id); expect(storage.setItem).toHaveBeenCalledTimes(1);
    expect(fetch).not.toHaveBeenCalled();
    const originalRaw = storage.value(), originalEntry = graphClone(graph.entries[id]!);
    reopen();
    const restoredPreparation = usePreparationStore(), restoredGetDraft = vi.spyOn(restoredPreparation, "getDraft");
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    const restoredWorkspace = useWorkspaceStore(), restoredGraph = useWorkflowGraphStore();
    expect(restored.status).toBe("saved");
    expect(restoredWorkspace.workflows.map(row => row.id)).toEqual([id]);
    expect(restoredWorkspace.activeWorkflowId).toBe(id); expect(restoredWorkspace.selectedWorkflowId).toBe(id);
    expect(Object.keys(restoredWorkspace.drafts)).toEqual([id]);
    expect(Object.keys(restoredWorkspace.initialLayouts)).toEqual([id]);
    expect(Object.keys(restoredWorkspace.histories)).toEqual([id]);
    expect(Object.keys(restoredGraph.entries)).toEqual([id]); expect(restoredGraph.entries[id]).toEqual(originalEntry);
    expect(restoredPreparation.exportConfiguration()).toEqual([]); expect(restoredGetDraft).not.toHaveBeenCalled();
    expect(useModelConfigurationStore().exportConfiguration().drafts).toEqual([]);
    expect(restored.save()).toBe(true); expect(storage.value()).toBe(originalRaw);
    expect(storage.setItem).toHaveBeenCalledTimes(1); expect(fetch).not.toHaveBeenCalled();
  });
  it("leaves independent provider, prompt and other outboxes untouched on fresh initialization", () => {
    const outboxes = { "workflow.models.resource.pending.v1": "original provider request",
      "workflow.prompts.resource.pending.v1": "original prompt request", "workflow.graph.outbox.v1": "foreign or older request" };
    const storage = memory(null, outboxes);
    useWorkbenchPersistenceStore().initialize(storage);
    for (const [key, raw] of Object.entries(outboxes)) expect(storage.value(key)).toBe(raw);
    expect(storage.getItem.mock.calls.every(([key]) => key === WORKBENCH_STORAGE_KEY)).toBe(true);
    expect(storage.setItem.mock.calls.every(([key]) => key === WORKBENCH_STORAGE_KEY)).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
  });
  it.each([
    { name: "readable", raw: JSON.stringify({ format: "workflow-ui-local/v1", id: "local:prototype",
      title: "Old prototype", nodes: [], edges: [], groups: {}, layout: {} }) },
    { name: "malformed", raw: "{broken prototype record" },
  ])("does not read, import or overwrite a $name retired prototype record during initialization or reopen", ({ raw }) => {
    const oldKey = "workflow-workbench:local-draft:v1", storage = memory(null, { [oldKey]: raw });
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    const workspace = useWorkspaceStore(), graph = useWorkflowGraphStore(), id = workspace.activeWorkflowId;
    expect(persistence.status).toBe("saved"); expect(graphUuid(id)).toBe(true);
    expect(workspace.workflows.map(row => row.id)).toEqual([id]);
    expect(graph.entries[id]!.document).toMatchObject({ workflow_definition_id: id, nodes: [], edges: [] });
    expect(storage.value(oldKey)).toBe(raw);
    // The initial read and first-save CAS check both use the current key.
    expect(storage.getItem).toHaveBeenCalledTimes(2);
    expect(storage.getItem.mock.calls).toEqual([[WORKBENCH_STORAGE_KEY], [WORKBENCH_STORAGE_KEY]]);
    expect(storage.setItem).toHaveBeenCalledTimes(1);
    expect(storage.setItem.mock.calls[0]![0]).toBe(WORKBENCH_STORAGE_KEY);
    expect(storage.value()).not.toContain("local:prototype");
    const saved = storage.value(), entry = graphClone(graph.entries[id]!);
    reopen();
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    expect(restored.status).toBe("saved");
    expect(useWorkspaceStore().activeWorkflowId).toBe(id);
    expect(useWorkflowGraphStore().entries[id]).toEqual(entry);
    expect(restored.save()).toBe(true);
    expect(storage.value()).toBe(saved); expect(storage.value(oldKey)).toBe(raw);
    expect(storage.getItem.mock.calls.every(([key]) => key === WORKBENCH_STORAGE_KEY)).toBe(true);
    expect(storage.setItem).toHaveBeenCalledTimes(1);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("does not interpret storage-read failure as a fresh installation", () => {
    const storage = { getItem: vi.fn(() => { throw new Error("denied"); }), setItem: vi.fn() };
    const graph = useWorkflowGraphStore(), create = vi.spyOn(graph, "createWorkflow");
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    expect(persistence.status).toBe("blocked"); expect(persistence.error).toBeTruthy();
    expect(create).not.toHaveBeenCalled(); expect(graph.entries).toEqual({});
    expect(persistence.save()).toBe(false); vi.advanceTimersByTime(250);
    expect(storage.setItem).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
  });
  it.each(["", "{broken", "null", '{"schemaVersion":7,"kind":"unified-workbench"}', "x".repeat(4_000_001)])(
    "never resets or overwrites a present unreadable record %#", raw => {
      const storage = memory(raw), graph = useWorkflowGraphStore();
      const create = vi.spyOn(graph, "createWorkflow"), preparation = usePreparationStore();
      const getDraft = vi.spyOn(preparation, "getDraft");
      const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
      expect(persistence.status).toBe("blocked"); expect(create).not.toHaveBeenCalled();
      expect(graph.entries).toEqual({}); expect(getDraft).not.toHaveBeenCalled();
      expect(persistence.save()).toBe(false); vi.advanceTimersByTime(250);
      expect(storage.value()).toBe(raw); expect(storage.setItem).not.toHaveBeenCalled();
      expect(fetch).not.toHaveBeenCalled();
    });
  it("blocks a fresh storage-write failure without dispatching or claiming the graph was saved", () => {
    const storage = memory();
    storage.setItem.mockImplementation(() => { throw new Error("quota"); });
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    expect(persistence.status).toBe("blocked"); expect(persistence.error).toContain("quota");
    expect(storage.value()).toBeNull(); expect(persistence.save()).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("restores an already-saved schema6 empty graph with its empty lock instead of generating or upgrading it", () => {
    const storage = memory(), value = currentSave();
    const raw = writeWorkbench(storage, value, null); storage.setItem.mockClear();
    const document = (value.graph as { entries: Record<string, GraphEntry> }).entries[value.activeWorkflowId]!.document;
    const graph = useWorkflowGraphStore(), create = vi.spyOn(graph, "createWorkflow");
    const preparation = usePreparationStore(), getDraft = vi.spyOn(preparation, "getDraft");
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    const workspace = useWorkspaceStore();
    expect(persistence.status).toBe("saved"); expect(create).not.toHaveBeenCalled();
    expect(workspace.workflows).toEqual(value.catalog);
    expect(Object.keys(workspace.drafts)).toEqual([value.activeWorkflowId]);
    expect(Object.keys(workspace.initialLayouts)).toEqual([value.activeWorkflowId]);
    expect(Object.keys(workspace.histories)).toEqual([value.activeWorkflowId]);
    expect(graph.entries[value.activeWorkflowId]!.document).toEqual(document);
    expect(graph.entries[value.activeWorkflowId]!.document.package_lock).toEqual([]);
    expect(preparation.exportConfiguration()).toEqual([]); expect(getDraft).not.toHaveBeenCalled();
    expect(persistence.save()).toBe(true);
    expect(storage.value()).toBe(raw); expect(storage.setItem).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });
  it.each([1, 2, 3, 4, 5] as const)("restores schema%s legacy catalog without automatic conversion or write", version => {
    const raw = JSON.stringify(legacyVersion(version)), storage = memory(raw);
    expect(readWorkbench(storage).error).toBeNull();
    const graph = useWorkflowGraphStore(), create = vi.spyOn(graph, "createWorkflow");
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    const workspace = useWorkspaceStore();
    expect(persistence.status).toBe("saved"); expect(workspace.activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
    expect(workspace.workflows.map(row => row.id)).toEqual([MAIN_WORKFLOW_ID, EMPTY_WORKFLOW_ID]);
    expect(create).not.toHaveBeenCalled(); expect(graph.isGeneric(MAIN_WORKFLOW_ID)).toBe(false);
    expect(graph.isGeneric(EMPTY_WORKFLOW_ID)).toBe(false);
    expect(storage.value()).toBe(raw); expect(storage.setItem).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });
  it("preserves a schema6 empty compatibility row and its original document identity", () => {
    const value = legacyVersion(5), empty = value.catalog!.find(row => row.id === EMPTY_WORKFLOW_ID)!;
    value.schemaVersion = 6; value.catalog = [empty];
    value.activeWorkflowId = EMPTY_WORKFLOW_ID; value.selectedWorkflowId = EMPTY_WORKFLOW_ID;
    value.layouts = { [EMPTY_WORKFLOW_ID]: {} }; value.preparations = [];
    value.graph = { schema_version: 1, entries: {} };
    const storage = memory(), raw = writeWorkbench(storage, value, null); storage.setItem.mockClear();
    const original = JSON.parse(raw).documents[EMPTY_WORKFLOW_ID];
    expect(original.compatibility).toEqual({ preparations: [] });
    const graph = useWorkflowGraphStore(), ensure = vi.spyOn(graph, "ensureEmpty");
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    expect(persistence.status).toBe("saved"); expect(ensure).not.toHaveBeenCalled();
    expect(graph.isGeneric(EMPTY_WORKFLOW_ID)).toBe(false); expect(graph.entries).toEqual({});
    expect(useWorkspaceStore().activeWorkflowId).toBe(EMPTY_WORKFLOW_ID);
    expect(persistence.save()).toBe(true); expect(storage.value()).toBe(raw);
    expect(JSON.parse(storage.value()!).documents[EMPTY_WORKFLOW_ID]).toEqual(original);
    expect(storage.setItem).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
  });
  it("restores missing-package documents and exact generic pending requests without replay or replacement", () => {
    const document = upgradeGraph(newGraph("Unknown installed package"), [{ package_id: "external.missing", version: "7.0.0" }]);
    document.revision = 3;
    document.nodes = [{ node_binding_id: crypto.randomUUID(), component_id: "external.future",
      component_version: "7", title: "Saved future node", position: { x: 25, y: 60 },
      config: { original: { nested: [true, null, "preserved"] } } }];
    const sid = crypto.randomUUID(), key = crypto.randomUUID();
    const pending: GraphCommand = { action: "start", path: `/api/graph/sessions/${sid}/runs`,
      body: { expected_revision: 9, expected_data_revision: 2, expected_definition_revision: 3,
        external_inputs: { text: "original body", plugin: { values: [1, 2] } }, idempotency_key: key } };
    const entry: GraphEntry = { document, saved_document: graphClone(document), saved_revision: 3, session_id: sid,
      pending, external_inputs: { text: "local edited form" } };
    const value = currentSave(entry); value.catalog![0]!.nodeCount = 1;
    const storage = memory(), raw = writeWorkbench(storage, value, null); storage.setItem.mockClear();
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    const graph = useWorkflowGraphStore(), restored = graph.entries[value.activeWorkflowId]!;
    expect(persistence.status).toBe("saved"); expect(restored.document).toEqual(document);
    expect(restored.saved_document).toEqual(document); expect(restored.pending).toEqual(pending);
    expect(restored.external_inputs).toEqual(entry.external_inputs);
    expect(restored.session_id).toBe(sid); expect(graph.pending).toEqual(pending);
    expect(graph.locked).toBe(true); expect(graph.catalog).toEqual([]); expect(graph.views).toEqual({});
    expect(persistence.save()).toBe(true); expect(storage.value()).toBe(raw);
    expect(storage.setItem).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
  });
  it("retains legacy session/model/exposure pending requests through schema5 initialization and explicit schema6 save", () => {
    const value = legacyVersion(5), sid = crypto.randomUUID(), requestId = crypto.randomUUID();
    const runtimePending = { path: `/api/sessions/${sid}/inputs`, requestId, action: "start", replayable: true,
      body: { text: "unknown original input", idempotency_key: requestId } };
    value.runtime = { ...emptyRuntime(), selectedSessions: [{ workflowId: MAIN_WORKFLOW_ID, sessionId: sid }],
      sessions: [{ workflowId: MAIN_WORKFLOW_ID, sessionId: sid, pending: runtimePending,
        requiresRefresh: true, promptSelection: null, modelSelection: null }] };
    const modelPending: NonNullable<NonNullable<SavedWorkbench["models"]>["pending"]> = {
      path: "/api/model-configurations/provider",
      body: { expected_revision: 0, idempotency_key: crypto.randomUUID(),
        record: { schema_version: 1, kind: "chat_provider", provider_id: crypto.randomUUID(), revision: 1,
          name: "Old provider", protocol: "chat", base_url: "https://api.deepseek.com",
          credential_ref: "env:DEEPSEEK_API_KEY", enabled: true } },
    };
    value.models!.pending = modelPending;
    const exposurePending: NonNullable<NonNullable<SavedWorkbench["exposures"]>["pending"]> = {
      expected_revision: 0, idempotency_key: crypto.randomUUID(),
      record: { schema_version: 1, kind: "workflow_exposure_configuration", config_id: crypto.randomUUID(),
        workflow_id: MAIN_WORKFLOW_ID, revision: 1, registrations: [] },
    };
    value.exposures!.pending = exposurePending;
    const original = JSON.stringify(value), storage = memory(original);
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    expect(persistence.status).toBe("saved");
    expect(useWorkbenchRuntimeStore().exportRuntimeSnapshot().sessions[0]!.pending).toEqual(runtimePending);
    expect(useModelConfigurationStore().exportConfiguration().pending).toEqual(modelPending);
    expect(useExposuresStore().exportConfiguration().pending).toEqual(exposurePending);
    expect(storage.value()).toBe(original); expect(storage.setItem).not.toHaveBeenCalled();
    expect(persistence.save()).toBe(true);
    const unified = JSON.parse(storage.value()!);
    expect(unified.schemaVersion).toBe(6); expect(unified.modelPending).toEqual(modelPending);
    expect(unified.runtime.sessions[0].pending).toEqual(runtimePending);
    expect(unified.exposures.pending).toEqual(exposurePending);
    expect(unified.documents[MAIN_WORKFLOW_ID].compatibility).toBeDefined();
    const schema6 = storage.value(); reopen(); storage.setItem.mockClear();
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    expect(restored.status).toBe("saved"); expect(storage.value()).toBe(schema6);
    expect(useWorkbenchRuntimeStore().exportRuntimeSnapshot().sessions[0]!.pending).toEqual(runtimePending);
    expect(useModelConfigurationStore().exportConfiguration().pending).toEqual(modelPending);
    expect(useExposuresStore().exportConfiguration().pending).toEqual(exposurePending);
    expect(storage.setItem).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
  });
  it("blocks a damaged pending snapshot without overwriting the original nonnull archive", () => {
    const value = currentSave(), id = value.activeWorkflowId;
    const graph = value.graph as { entries: Record<string, GraphEntry> };
    graph.entries[id]!.pending = { action: "start", path: "/api/graph/sessions/foreign/runs",
      body: { text: "must not reinterpret", idempotency_key: crypto.randomUUID() } };
    const storage = memory(), raw = writeWorkbench(storage, value, null); storage.setItem.mockClear();
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    expect(persistence.status).toBe("blocked"); expect(persistence.save()).toBe(false);
    vi.advanceTimersByTime(250); expect(storage.value()).toBe(raw);
    expect(storage.setItem).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
  });
});
