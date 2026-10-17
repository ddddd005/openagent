import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { WORKBENCH_RECOVERY_STORAGE_KEY, WORKBENCH_STORAGE_KEY } from "../adapters/workbenchPersistence";
import { workbenchDraftAddress } from "../adapters/localDraftsApi";
import { graphClone, newGraph } from "../domain/workflowGraph";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); vi.useFakeTimers(); });
afterEach(() => { disposePinia(pinia); vi.clearAllTimers(); vi.useRealTimers(); });

describe("legacy browser workbench isolation", () => {
  it.each(["{broken", JSON.stringify({ schemaVersion: 99, pending: { body: { text: "original" } } })])(
    "does not read, import, archive, or overwrite old browser bytes: %s", async raw => {
      const data = new Map([[WORKBENCH_STORAGE_KEY, raw], [WORKBENCH_RECOVERY_STORAGE_KEY, raw]]);
      const storage = { getItem: vi.fn((key: string) => data.get(key) ?? null),
        setItem: vi.fn(), removeItem: vi.fn() };
      const backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
      await persistence.initialize(storage, backend.ports);
      expect(persistence.status).toBe("ready");
      expect(storage.getItem).not.toHaveBeenCalled();
      expect(storage.setItem).not.toHaveBeenCalled(); expect(storage.removeItem).not.toHaveBeenCalled();
      expect(data.size).toBe(2); expect(data.get(WORKBENCH_STORAGE_KEY)).toBe(raw);
      expect("importLegacy" in persistence).toBe(false); expect("recoverWorkspace" in persistence).toBe(false);
      expect(backend.records.size).toBe(0);
      expect(backend.ports.save).not.toHaveBeenCalled();
    });
  it("uses backend selection preferences without restoring an old browser workspace", async () => {
    const old = newGraph("Old browser product"), storage = { getItem: vi.fn(() => JSON.stringify({
      schemaVersion: 7, kind: "graph-workbench", activeWorkflowId: old.workflow_definition_id,
      graph: { entries: { [old.workflow_definition_id]: { document: old } } },
    })), setItem: vi.fn(), removeItem: vi.fn() };
    const backend = memoryLocalDraftStorage(), first = useWorkbenchPersistenceStore();
    const current = newGraph("Current backend definition"), currentId = current.workflow_definition_id;
    const record = await backend.ports.save({ address: workbenchDraftAddress, format_version: 1,
      expected_sequence: 0, idempotency_key: crypto.randomUUID(),
      value: { schemaVersion: 7, kind: "graph-workbench", activeWorkflowId: currentId, selectedWorkflowId: currentId,
        graph: { schema_version: 1, entries: {} } } });
    await first.initialize(storage, backend.ports);
    expect(first.preferredActiveWorkflowId).toBe(currentId);
    expect(useWorkflowGraphStore().document).toBeNull();
    expect(await first.save()).toBe(false);
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const restored = useWorkbenchPersistenceStore();
    await restored.initialize(storage, backend.ports);
    expect(restored.preferredActiveWorkflowId).toBe(currentId);
    expect(useWorkspaceStore().workflows).toEqual([]);
    expect(useWorkflowGraphStore().storeSnapshot()).toEqual({ schema_version: 1, entries: {} });
    expect(await backend.ports.read(workbenchDraftAddress)).toEqual(graphClone(record));
    expect(backend.ports.save).toHaveBeenCalledOnce();
    expect(storage.getItem).not.toHaveBeenCalled(); expect(storage.removeItem).not.toHaveBeenCalled();
  });
});
