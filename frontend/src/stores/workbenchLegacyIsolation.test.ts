import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { WORKBENCH_RECOVERY_STORAGE_KEY, WORKBENCH_STORAGE_KEY } from "../adapters/workbenchPersistence";
import { workbenchDraftAddress } from "../adapters/localDraftsApi";
import { newGraph } from "../domain/workflowGraph";
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
      expect(persistence.status).toBe("saved");
      expect(storage.getItem).not.toHaveBeenCalled();
      expect(storage.setItem).not.toHaveBeenCalled(); expect(storage.removeItem).not.toHaveBeenCalled();
      expect(data.size).toBe(2); expect(data.get(WORKBENCH_STORAGE_KEY)).toBe(raw);
      expect("importLegacy" in persistence).toBe(false); expect("recoverWorkspace" in persistence).toBe(false);
      expect([...backend.records.values()].map(record => record.address.key)).toEqual([workbenchDraftAddress.key]);
    });
  it("uses only the current backend workspace when a complete old browser record remains", async () => {
    const old = newGraph("Old browser product"), storage = { getItem: vi.fn(() => JSON.stringify({
      schemaVersion: 7, kind: "graph-workbench", activeWorkflowId: old.workflow_definition_id,
      graph: { entries: { [old.workflow_definition_id]: { document: old } } },
    })), setItem: vi.fn(), removeItem: vi.fn() };
    const backend = memoryLocalDraftStorage(), first = useWorkbenchPersistenceStore();
    await first.initialize(storage, backend.ports);
    useWorkflowGraphStore().document!.name = "Current backend edit";
    expect(await first.save()).toBe(true);
    const currentId = useWorkspaceStore().activeWorkflowId;
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    await useWorkbenchPersistenceStore().initialize(storage, backend.ports);
    expect(useWorkspaceStore().activeWorkflowId).toBe(currentId);
    expect(useWorkflowGraphStore().document!.name).toBe("Current backend edit");
    expect(storage.getItem).not.toHaveBeenCalled(); expect(storage.removeItem).not.toHaveBeenCalled();
  });
});
