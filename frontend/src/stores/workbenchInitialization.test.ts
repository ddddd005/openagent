import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone, newGraph } from "../domain/workflowGraph";
import { WORKBENCH_STORAGE_KEY } from "../adapters/workbenchPersistence";
import { workbenchDraftAddress } from "../adapters/localDraftsApi";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { graphWorkbenchStorage } from "../testUtils/graphWorkbenchStorage";

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
  it("waits for the backend without creating or storing an empty replacement", async () => {
    const storage = memory(), backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    let finish!: (value: null) => void;
    vi.mocked(backend.ports.read).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const loading = persistence.initialize(storage, backend.ports);
    expect(persistence.status).toBe("loading"); expect(useWorkspaceStore().workflows).toEqual([]);
    finish(null); await loading;
    expect(useWorkspaceStore().workflows).toEqual([]);
    expect(useWorkflowGraphStore().document).toBeNull();
    expect(persistence.status).toBe("ready");
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(backend.records.size).toBe(0);
    expect(backend.ports.save).not.toHaveBeenCalled();
    expect(backend.ports.receipt).not.toHaveBeenCalled();
  });
  it("does not adopt or rewrite old fixed browser material", async () => {
    const raw = JSON.stringify({ schemaVersion: 5, kind: "fixed-workbench" }), storage = memory(raw);
    const backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(storage, backend.ports);
    expect(useWorkspaceStore().workflows).toEqual([]); expect("importLegacy" in persistence).toBe(false);
    expect(storage.getItem(WORKBENCH_STORAGE_KEY)).toBe(raw); expect(storage.setItem).not.toHaveBeenCalled();
    expect(backend.records.size).toBe(0);
    expect(backend.ports.save).not.toHaveBeenCalled();
  });
  it("reopens only frozen pending evidence without restoring or rewriting editor metadata", async () => {
    const document = newGraph("Frozen original"), id = document.workflow_definition_id;
    const command = { action: "save", path: "/api/graph/definitions", body: {
      document: graphClone(document), expected_revision: 0, idempotency_key: crypto.randomUUID() } };
    const original = { schema_version: 1 as const, entries: { [id]: {
      document, saved_revision: 0, session_id: null, pending: command,
      external_inputs: { original: "untouched editor input" },
    } } };
    const backend = memoryLocalDraftStorage();
    const configuration = JSON.parse(graphWorkbenchStorage({ graph: original, catalog: [],
      activeWorkflowId: id, selectedWorkflowId: id }).getItem()!);
    configuration.unknown_metadata = { retained: "original bytes" };
    const record = await backend.ports.save({ address: workbenchDraftAddress, format_version: 1, value: configuration,
      expected_sequence: 0, idempotency_key: crypto.randomUUID() });
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(memory(), backend.ports);
    expect(persistence.status).toBe("ready");
    expect(persistence.legacyRequests).toEqual([{
      key: `${id}:${command.body.idempotency_key}`, workflowId: id, command,
      legacy_action: null, basis: { definitionId: id, savedRevision: 0, sessionId: null }, status: "pending",
    }]);
    expect(useWorkflowGraphStore().storeSnapshot().entries).toEqual({});
    expect(JSON.stringify(persistence.legacyRequests)).not.toContain("untouched editor input");
    expect(await persistence.save()).toBe(false);
    expect(await backend.ports.read(record.address)).toEqual(record);
    expect(backend.ports.save).toHaveBeenCalledOnce();
    expect(backend.ports.receipt).not.toHaveBeenCalled();
    expect(persistence.preferredActiveWorkflowId).toBe(id);
    expect(persistence.preferredSelectedWorkflowId).toBe(id);
  });
  it("retains corrupt browser bytes while refusing retired workbench saving", async () => {
    const storage = memory("{broken"), backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(storage, backend.ports);
    expect(persistence.status).toBe("ready"); expect(await persistence.save()).toBe(false);
    expect("rawRecord" in persistence).toBe(false); expect("importLegacy" in persistence).toBe(false);
    expect(storage.getItem(WORKBENCH_STORAGE_KEY)).toBe("{broken"); expect(storage.setItem).not.toHaveBeenCalled();
    expect(backend.ports.save).not.toHaveBeenCalled();
    expect(persistence.error).toContain("已退役");
  });
  it("never overwrites a newer primary record or creates a retired conflict copy", async () => {
    const backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    const initial = { schemaVersion: 7, kind: "graph-workbench",
      graph: { schema_version: 1, entries: {} }, externally_saved: "first page" };
    const current = await backend.ports.save({ address: workbenchDraftAddress, format_version: 1,
      value: initial, expected_sequence: 0, idempotency_key: crypto.randomUUID() });
    await persistence.initialize(memory(), backend.ports);
    const newer = await backend.ports.save({ address: workbenchDraftAddress, format_version: 1,
      value: { ...(current.value as object), externally_saved: "newer page" },
      expected_sequence: current.sequence, idempotency_key: crypto.randomUUID() });
    const snapshot = graphClone(useWorkflowGraphStore().storeSnapshot());
    vi.mocked(backend.ports.save).mockClear();
    expect(await persistence.save()).toBe(false); expect(persistence.status).toBe("blocked");
    expect(await backend.ports.read(workbenchDraftAddress)).toEqual(newer);
    expect(persistence.conflictCopyKey).toBeNull();
    expect(backend.records.size).toBe(1);
    expect(backend.ports.save).not.toHaveBeenCalled();
    expect(useWorkflowGraphStore().storeSnapshot()).toEqual(snapshot);
  });
  it("blocks writes after backend read failure without creating an empty replacement", async () => {
    const backend = memoryLocalDraftStorage();
    vi.mocked(backend.ports.read).mockRejectedValue(new Error("storage contract unreadable"));
    const persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(memory(), backend.ports);
    expect(persistence.status).toBe("ready");
    expect(persistence.error).toContain("storage contract unreadable");
    expect(await persistence.save()).toBe(false);
    expect(useWorkspaceStore().workflows).toEqual([]); expect(backend.ports.save).not.toHaveBeenCalled();
  });
});
