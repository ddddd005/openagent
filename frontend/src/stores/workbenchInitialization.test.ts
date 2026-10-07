import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone } from "../domain/workflowGraph";
import { graphWorkbenchStorage } from "../testUtils/graphWorkbenchStorage";

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); vi.useFakeTimers(); });
afterEach(() => { disposePinia(pinia); vi.clearAllTimers(); vi.useRealTimers(); });
function memory(initial: string | null = null) {
  let raw = initial;
  return { getItem: () => raw, setItem: vi.fn((_key: string, value: string) => { raw = value; }) };
}
describe("current-only workspace initialization", () => {
  it("creates and saves one empty current graph without constructing old stores", () => {
    const persistence = useWorkbenchPersistenceStore(), storage = memory();
    persistence.initialize(storage);
    const workspace = useWorkspaceStore(), graph = useWorkflowGraphStore();
    expect(workspace.workflows).toHaveLength(1);
    expect(graph.active?.document.nodes).toEqual([]);
    expect(persistence.status).toBe("saved");
    expect(JSON.parse(storage.getItem()!).kind).toBe("graph-workbench");
    expect([...pinia._s.keys()]).toEqual(expect.arrayContaining(["workspace", "workflow-graph", "workbench-notices", "workbench-persistence"]));
    expect([...pinia._s.keys()].some(id => /preparation|runtime|model-configuration|exposures/.test(id))).toBe(false);
  });
  it("replaces explicitly old fixed test material with a current workspace", () => {
    const storage = memory(JSON.stringify({ schemaVersion: 5, kind: "fixed-workbench" }));
    useWorkbenchPersistenceStore().initialize(storage);
    expect(useWorkspaceStore().workflows).toHaveLength(1);
    expect(JSON.parse(storage.getItem()!).schemaVersion).toBe(7);
  });
  it("preserves current pending and metadata across initialization and subsequent saves", () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), id = graph.createWorkflow();
    graph.active!.pending = { action: "save", path: "/api/graph/definitions", body: {
      document: graphClone(graph.document!), expected_revision: 0, idempotency_key: crypto.randomUUID() } };
    graph.active!.external_inputs = { original: "untouched" };
    const original = graphClone(graph.storeSnapshot());
    const storage = graphWorkbenchStorage({ graph: original, catalog: workspace.workflows,
      activeWorkflowId: id, selectedWorkflowId: id });
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    expect(persistence.status).toBe("saved"); expect(graph.storeSnapshot()).toEqual(original);
    expect(persistence.save()).toBe(true);
    expect(JSON.parse(storage.getItem()!).graph).toEqual(original);
  });
  it("does not overwrite corrupted storage or bypass a failed local pending save", async () => {
    const storage = memory("{broken"), persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    expect(persistence.status).toBe("blocked"); expect(persistence.save()).toBe(false);
    expect(storage.getItem()).toBe("{broken"); expect(storage.setItem).not.toHaveBeenCalled();
  });
  it("blocks a stale-page CAS write without overwriting the newer persisted record", () => {
    const storage = memory(), persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const replacement = '{"external":"newer tab"}';
    storage.setItem("workflow-workbench:fixed-base:v1", replacement);
    useWorkflowGraphStore().active!.document.name = "Unsent stale edit";
    expect(persistence.save()).toBe(false); expect(persistence.status).toBe("blocked");
    expect(storage.getItem()).toBe(replacement);
  });
  it("debounces local edits and preserves the last stored graph when storage rejects a write", () => {
    const storage = memory(), persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const original = storage.getItem();
    useWorkflowGraphStore().active!.document.name = "Local edit";
    expect(persistence.status).toBe("pending");
    expect(storage.getItem()).toBe(original);
    storage.setItem.mockImplementationOnce(() => { throw new Error("quota"); });
    vi.advanceTimersByTime(200);
    expect(persistence.status).toBe("blocked"); expect(storage.getItem()).toBe(original);
  });
});
