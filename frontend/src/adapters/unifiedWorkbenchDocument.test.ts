import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { usePreparationStore } from "../stores/preparation";
import { useWorkspaceStore } from "../stores/workspace";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { readWorkbench } from "./workbenchPersistence";
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkbenchPersistenceStore().$dispose(); useWorkflowGraphStore().$dispose(); vi.unstubAllGlobals(); });
describe("single-document workbench storage", () => {
  it("stores all layout/config/ports in one document, restores unknown plugins and legacy projections", () => {
    let raw: string | null = null;
    const storage = { getItem: () => raw, setItem: (_key:string,value:string) => { raw = value; } };
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    const graph = useWorkflowGraphStore();
    const id = graph.createWorkflow();
    graph.entries[id].document.nodes.push({ node_binding_id:crypto.randomUUID(), component_id:"external.future",
      component_version:"7", title:"Future", position:{ x:20,y:30 }, config:{ data:{ preserved:true } } });
    useWorkspaceStore().activeWorkflow.nodeCount = 1;
    expect(persistence.save()).toBe(true);
    const stored = JSON.parse(raw!);
    expect(stored.schemaVersion).toBe(6);
    expect(stored.preparations).toBeUndefined(); expect(stored.layouts).toBeUndefined(); expect(stored.models).toBeUndefined();
    expect(stored.graph.entries[id].document).toBeUndefined();
    expect(stored.documents[id].document.nodes[0].component_id).toBe("external.future");
    const preparationId = usePreparationStore().getDraft(MAIN_WORKFLOW_ID,"A").nodes[0].id;
    expect(readWorkbench(storage).value?.preparations.some(p => p.nodes.some(n => n.id === preparationId))).toBe(true);
    persistence.$dispose(); setActivePinia(createPinia());
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    expect(restored.status).toBe("saved");
    expect(useWorkflowGraphStore().entries[id].document.nodes[0].config).toEqual({ data:{ preserved:true } });
    expect(useWorkspaceStore().activeWorkflowId).toBe(id);
    expect(useWorkspaceStore().nodes).toEqual([]);
  });
  it("preserves damaged unified snapshots instead of resetting or overwriting them", () => {
    let raw: string | null = null;
    const storage = { getItem: () => raw, setItem: (_key:string,value:string) => { raw = value; } };
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage); persistence.save();
    const damaged = JSON.parse(raw!); damaged.documents[MAIN_WORKFLOW_ID].document.nodes[0].node_binding_id = "bad";
    raw = JSON.stringify(damaged); const original = raw;
    persistence.$dispose(); setActivePinia(createPinia());
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    expect(restored.status).toBe("blocked"); expect(restored.save()).toBe(false); expect(raw).toBe(original);
  });
});
