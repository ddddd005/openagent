import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone } from "../domain/workflowGraph";
import { WORKBENCH_STORAGE_KEY } from "../adapters/workbenchPersistence";
import { workbenchDraftAddress } from "../adapters/localDraftsApi";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { graphWorkbenchStorage } from "../testUtils/graphWorkbenchStorage";
import { WorkbenchApiError } from "../adapters/workbenchApi";

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); vi.useFakeTimers(); });
afterEach(() => { disposePinia(pinia); vi.clearAllTimers(); vi.useRealTimers(); });
function memory(initial: string | null = null) {
  const data = new Map<string, string>();
  if (initial !== null) data.set(WORKBENCH_STORAGE_KEY, initial);
  return { getItem: (key: string) => data.get(key) ?? null,
    setItem: vi.fn((key: string, value: string) => { data.set(key, value); }) };
}
describe("backend-authoritative workspace initialization", () => {
  it("waits for the backend before creating and storing one empty current graph", async () => {
    const storage = memory(), backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    let finish!: (value: null) => void;
    vi.mocked(backend.ports.read).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const loading = persistence.initialize(storage, backend.ports);
    expect(persistence.status).toBe("loading"); expect(useWorkspaceStore().workflows).toEqual([]);
    finish(null); await loading;
    expect(useWorkspaceStore().workflows).toHaveLength(1);
    expect(useWorkflowGraphStore().active?.document.nodes).toEqual([]);
    expect(persistence.status).toBe("saved");
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(backend.records.get(backend.key(workbenchDraftAddress))?.value).toMatchObject({ kind: "graph-workbench" });
  });
  it("does not adopt or rewrite old fixed browser material", async () => {
    const raw = JSON.stringify({ schemaVersion: 5, kind: "fixed-workbench" }), storage = memory(raw);
    const backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(storage, backend.ports);
    expect(useWorkspaceStore().workflows).toHaveLength(1); expect(persistence.canImportLegacy).toBe(false);
    expect(storage.getItem(WORKBENCH_STORAGE_KEY)).toBe(raw); expect(storage.setItem).not.toHaveBeenCalled();
  });
  it("preserves a frozen pending command and unknown metadata through the backend and reopening", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), id = graph.createWorkflow();
    graph.active!.pending = { action: "save", path: "/api/graph/definitions", body: {
      document: graphClone(graph.document!), expected_revision: 0, idempotency_key: crypto.randomUUID() } };
    graph.active!.external_inputs = { original: "untouched" };
    const original = graphClone(graph.storeSnapshot());
    const backend = memoryLocalDraftStorage();
    const configuration = JSON.parse(graphWorkbenchStorage({ graph: original, catalog: workspace.workflows,
      activeWorkflowId: id, selectedWorkflowId: id }).getItem()!);
    await backend.ports.save({ address: workbenchDraftAddress, format_version: 1, value: configuration,
      expected_sequence: 0, idempotency_key: crypto.randomUUID() });
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(memory(), backend.ports);
    expect(persistence.status).toBe("saved"); expect(useWorkflowGraphStore().storeSnapshot()).toEqual(original);
    expect(await persistence.save()).toBe(true);
    expect(backend.records.get(backend.key(workbenchDraftAddress))?.value).toMatchObject({ graph: original });
  });
  it("retains corrupt browser bytes while using a separately saved backend workspace", async () => {
    const storage = memory("{broken"), backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(storage, backend.ports);
    expect(persistence.status).toBe("saved"); expect(await persistence.save()).toBe(true);
    expect(persistence.rawRecord()).toBe("{broken"); expect(persistence.canImportLegacy).toBe(false);
    expect(storage.getItem(WORKBENCH_STORAGE_KEY)).toBe("{broken"); expect(storage.setItem).not.toHaveBeenCalled();
  });
  it("keeps both sides of a stale-page CAS conflict and never overwrites the newer primary record", async () => {
    const backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(memory(), backend.ports);
    const current = backend.records.get(backend.key(workbenchDraftAddress))!;
    await backend.ports.save({ address: workbenchDraftAddress, format_version: 1,
      value: { ...(current.value as object), externally_saved: "newer page" },
      expected_sequence: current.sequence, idempotency_key: crypto.randomUUID() });
    useWorkflowGraphStore().active!.document.name = "Unsent stale edit";
    expect(await persistence.save()).toBe(false); expect(persistence.status).toBe("blocked");
    expect(backend.records.get(backend.key(workbenchDraftAddress))?.value).toMatchObject({ externally_saved: "newer page" });
    expect(persistence.conflictCopyKey).not.toBeNull();
    const conflict = [...backend.records.values()].find(record => record.address.key === persistence.conflictCopyKey);
    expect(conflict?.value).toMatchObject({ graph: { entries: {
      [useWorkspaceStore().activeWorkflowId]: { document: { name: "Unsent stale edit" } },
    } } });
  });
  it("debounces edits and does not report saved after an unknown durable write", async () => {
    const backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(memory(), backend.ports);
    const original = graphClone(backend.records.get(backend.key(workbenchDraftAddress)));
    vi.mocked(backend.ports.save).mockRejectedValueOnce(new WorkbenchApiError("unknown", "disk response lost"));
    useWorkflowGraphStore().active!.document.name = "Local edit";
    expect(persistence.status).toBe("pending");
    await vi.advanceTimersByTimeAsync(200);
    expect(persistence.status).toBe("blocked");
    expect(backend.records.get(backend.key(workbenchDraftAddress))).toEqual(original);
    expect(await persistence.save()).toBe(false);
    expect(backend.ports.receipt).toHaveBeenCalled();
  });
  it("blocks writes after backend read failure without creating an empty replacement", async () => {
    const backend = memoryLocalDraftStorage();
    vi.mocked(backend.ports.read).mockRejectedValue(new Error("storage contract unreadable"));
    const persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(memory(), backend.ports);
    expect(persistence.status).toBe("blocked"); expect(await persistence.save()).toBe(false);
    expect(useWorkspaceStore().workflows).toEqual([]); expect(backend.ports.save).not.toHaveBeenCalled();
  });
});
