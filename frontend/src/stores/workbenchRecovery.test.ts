import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { WORKBENCH_RECOVERY_STORAGE_KEY, WORKBENCH_STORAGE_KEY, type SavedWorkbench } from "../adapters/workbenchPersistence";
import { workbenchDraftAddress } from "../adapters/localDraftsApi";
import { graphClone, newGraph } from "../domain/workflowGraph";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { WorkbenchApiError } from "../adapters/workbenchApi";
let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); vi.useFakeTimers(); });
afterEach(() => { disposePinia(pinia); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });
function saved(): SavedWorkbench {
  const document = newGraph("Imported browser graph"), id = document.workflow_definition_id;
  return { schemaVersion: 7, kind: "graph-workbench", revision: 1, savedAt: "2026-10-09T01:00:00Z",
    activeWorkflowId: id, selectedWorkflowId: id,
    catalog: [{ id, title: document.name, description: "", nodeCount: 0, state: "draft" }],
    graph: { schema_version: 1, entries: { [id]: { document, saved_revision: 0, session_id: null, pending: null } } } };
}
function storage(raw = "{broken") {
  const records = new Map([[WORKBENCH_STORAGE_KEY, raw]]);
  return { records, getItem: (key: string) => records.get(key) ?? null,
    setItem: vi.fn((key: string, value: string) => { records.set(key, value); }) };
}
describe("explicit browser-to-backend draft import", () => {
  it("preserves damaged bytes and provides no fake browser-isolated recovery action", async () => {
    const backend = memoryLocalDraftStorage(), target = storage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(target, backend.ports);
    expect(persistence.rawRecord()).toBe("{broken");
    expect(persistence.canRecover).toBe(false); expect(await persistence.recoverWorkspace()).toBe(false);
    expect(target.setItem).not.toHaveBeenCalled(); expect(target.getItem(WORKBENCH_STORAGE_KEY)).toBe("{broken");
    expect(useWorkflowGraphStore().active?.pending).toBeNull();
  });
  it("does not auto-adopt or auto-replay a valid browser record and imports only on explicit action", async () => {
    const value = saved(), key = value.activeWorkflowId;
    value.graph.entries[key].pending = { action: "save", path: "/api/graph/definitions",
      body: { document: graphClone(value.graph.entries[key].document), expected_revision: 0, idempotency_key: crypto.randomUUID() } };
    const raw = JSON.stringify(value), target = storage(raw), backend = memoryLocalDraftStorage(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const persistence = useWorkbenchPersistenceStore(); await persistence.initialize(target, backend.ports);
    expect(useWorkspaceStore().activeWorkflowId).not.toBe(key); expect(persistence.canImportLegacy).toBe(true);
    expect(await persistence.importLegacy()).toBe(true);
    expect(useWorkspaceStore().activeWorkflowId).toBe(key);
    expect(useWorkflowGraphStore().active?.pending).toEqual(value.graph.entries[key].pending);
    expect(target.getItem(WORKBENCH_STORAGE_KEY)).toBe(raw); expect(target.setItem).not.toHaveBeenCalled();
    expect(fetcher).not.toHaveBeenCalled(); expect(persistence.canImportLegacy).toBe(false);
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const reopened = useWorkbenchPersistenceStore(); await reopened.initialize(target, backend.ports);
    expect(reopened.canImportLegacy).toBe(false); expect(reopened.isolated).toBe(true);
    expect(reopened.rawRecord()).toBe(raw); expect(reopened.hasRawRecord).toBe(true);
    expect(useWorkflowGraphStore().active?.pending).toEqual(value.graph.entries[key].pending);
    expect(target.setItem).not.toHaveBeenCalled(); expect(fetcher).not.toHaveBeenCalled();
  });
  it("retains unsupported future record bytes without adopting frozen coordinates", async () => {
    const original = newGraph("Future archive"), raw = JSON.stringify({ schemaVersion: 99, document: original,
      pending: { body: { idempotency_key: crypto.randomUUID(), input: "old operation" } } });
    const target = storage(raw), backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(target, backend.ports);
    expect(persistence.canImportLegacy).toBe(false); expect(await persistence.importLegacy()).toBe(false);
    expect(useWorkflowGraphStore().document!.workflow_definition_id).not.toBe(original.workflow_definition_id);
    expect(target.getItem(WORKBENCH_STORAGE_KEY)).toBe(raw);
  });
  it("uses the backend on reopening even when an old isolated browser namespace exists", async () => {
    const old = saved(), target = storage("{broken"), backend = memoryLocalDraftStorage();
    target.records.set(WORKBENCH_RECOVERY_STORAGE_KEY, JSON.stringify(old));
    const first = useWorkbenchPersistenceStore(); await first.initialize(target, backend.ports);
    useWorkflowGraphStore().document!.name = "Backend is authoritative";
    expect(await first.save()).toBe(true);
    const snapshot = graphClone(useWorkflowGraphStore().storeSnapshot());
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    await useWorkbenchPersistenceStore().initialize(target, backend.ports);
    expect(useWorkflowGraphStore().storeSnapshot()).toEqual(snapshot);
    expect(useWorkflowGraphStore().document!.name).toBe("Backend is authoritative");
    expect(target.records.get(WORKBENCH_RECOVERY_STORAGE_KEY)).toBe(JSON.stringify(old));
    expect(target.setItem).not.toHaveBeenCalled();
  });
  it("merges an imported independent graph without dropping newly edited local graphs or undo history", async () => {
    const value = saved(), target = storage(JSON.stringify(value)), backend = memoryLocalDraftStorage();
    const persistence = useWorkbenchPersistenceStore(); await persistence.initialize(target, backend.ports);
    const graph = useWorkflowGraphStore(), localId = useWorkspaceStore().activeWorkflowId;
    graph.document!.nodes.push({ node_binding_id: crypto.randomUUID(), component_id: "external.test", component_version: "1",
      title: "New local edit", position: { x: 1, y: 2 }, config: { opaque: "retained" } });
    graph.history[localId] = { past: [graphClone(graph.document!)], future: [] };
    const local = graphClone(graph.entries[localId]), undo = graphClone(graph.history[localId]);
    expect(await persistence.importLegacy()).toBe(true);
    expect(graph.entries[localId]).toEqual(local);
    expect(graph.history[localId]).toEqual(undo);
    expect(graph.entries[value.activeWorkflowId]).toEqual(value.graph.entries[value.activeWorkflowId]);
    expect(useWorkspaceStore().workflows).toHaveLength(2);
  });
  it("keeps a renamed empty graph rather than mistaking zero nodes for an untouched workspace", async () => {
    const value = saved(), target = storage(JSON.stringify(value)), backend = memoryLocalDraftStorage();
    const persistence = useWorkbenchPersistenceStore(); await persistence.initialize(target, backend.ports);
    const graph = useWorkflowGraphStore(), localId = useWorkspaceStore().activeWorkflowId;
    graph.document!.name = "An intentionally empty user graph";
    expect(await persistence.importLegacy()).toBe(true);
    expect(graph.entries[localId]?.document.name).toBe("An intentionally empty user graph");
    expect(useWorkspaceStore().workflows).toHaveLength(2);
  });
  it("reports marker failures and retries only that confirmation instead of reimporting graphs", async () => {
    const value = saved(), target = storage(JSON.stringify(value)), backend = memoryLocalDraftStorage();
    const persistence = useWorkbenchPersistenceStore(); await persistence.initialize(target, backend.ports);
    const originalSave = backend.ports.save;
    vi.mocked(backend.ports.save).mockImplementationOnce(originalSave).mockRejectedValueOnce(
      new WorkbenchApiError("rejected", "marker unavailable", 409, "draft_capacity_exceeded"));
    expect(await persistence.importLegacy()).toBe(false);
    expect(persistence.isolated).toBe(false); expect(persistence.canImportLegacy).toBe(true);
    const graph = graphClone(useWorkflowGraphStore().storeSnapshot()), count = useWorkspaceStore().workflows.length;
    expect(await persistence.importLegacy()).toBe(true);
    expect(useWorkflowGraphStore().storeSnapshot()).toEqual(graph);
    expect(useWorkspaceStore().workflows).toHaveLength(count);
    expect(persistence.canImportLegacy).toBe(false); expect(target.setItem).not.toHaveBeenCalled();
  });
  it("blocks only legacy adoption if the independent marker cannot be read safely", async () => {
    const value = saved(), target = storage(JSON.stringify(value)), backend = memoryLocalDraftStorage();
    await backend.ports.save({ address: { ...workbenchDraftAddress, kind: "intent", key: "workbench:legacy-import:v1" },
      format_version: 2, value: { future: true }, expected_sequence: 0, idempotency_key: crypto.randomUUID() });
    const persistence = useWorkbenchPersistenceStore(); await persistence.initialize(target, backend.ports);
    expect(persistence.status).toBe("saved"); expect(persistence.canImportLegacy).toBe(false);
    expect(persistence.legacyImportError).toContain("格式无效"); expect(persistence.rawRecord()).toBe(target.getItem(WORKBENCH_STORAGE_KEY));
    useWorkflowGraphStore().document!.name = "Current work remains writable";
    expect(await persistence.save()).toBe(true);
    expect(await persistence.importLegacy()).toBe(false);
  });
  it("refuses identity collisions instead of replacing an existing backend workflow", async () => {
    const value = saved(), target = storage(JSON.stringify(value)), backend = memoryLocalDraftStorage();
    value.graph.entries[value.activeWorkflowId].document.nodes.push({ node_binding_id: crypto.randomUUID(),
      component_id: "external.test", component_version: "1", title: "Existing", position: { x: 0, y: 0 }, config: {} });
    value.catalog[0].nodeCount = 1;
    await backend.ports.save({ address: workbenchDraftAddress, format_version: 1, value, expected_sequence: 0,
      idempotency_key: crypto.randomUUID() });
    const persistence = useWorkbenchPersistenceStore(); await persistence.initialize(target, backend.ports);
    const before = graphClone(useWorkflowGraphStore().storeSnapshot());
    expect(await persistence.importLegacy()).toBe(false);
    expect(useWorkflowGraphStore().storeSnapshot()).toEqual(before); expect(persistence.error).toContain("身份冲突");
  });
  it("does not import records whose pending paths fail runtime validation", async () => {
    const value = saved(), key = value.activeWorkflowId;
    value.graph.entries[key].pending = { action: "start", path: "/api/graph/sessions/foreign/runs",
      body: { idempotency_key: crypto.randomUUID() } };
    const target = storage(JSON.stringify(value)), backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(target, backend.ports);
    const before = graphClone(useWorkflowGraphStore().storeSnapshot());
    expect(await persistence.importLegacy()).toBe(false); expect(persistence.error).toContain("无法安全导入");
    expect(useWorkflowGraphStore().storeSnapshot()).toEqual(before);
    expect(target.setItem).not.toHaveBeenCalled();
  });
  it("keeps originals when an import cannot be durably saved", async () => {
    const target = storage(JSON.stringify(saved())), backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(target, backend.ports);
    vi.mocked(backend.ports.save).mockRejectedValueOnce(new Error("disk failed"));
    expect(await persistence.importLegacy()).toBe(false);
    expect(persistence.status).toBe("blocked"); expect(persistence.canImportLegacy).toBe(true);
    expect(persistence.rawRecord()).toBe(target.getItem(WORKBENCH_STORAGE_KEY));
    expect(target.setItem).not.toHaveBeenCalled();
  });
});
