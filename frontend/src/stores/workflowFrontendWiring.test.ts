import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { graphClone } from "../domain/workflowGraph";
import { frontendCatalog, frontendExecutionLock, frontendProjectLock } from "../testUtils/frontendFixture";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";

function fixture() {
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
  graph.catalog = graphClone(frontendCatalog); graph.packageLock = graphClone(frontendProjectLock);
  graph.executionPackageLock = graphClone(frontendExecutionLock);
  const id = graph.createWorkflow();
  const source = graph.addNode("tools.current-input@1", { x: 0, y: 0 })!;
  return { graph, id, source, request: { sourceNodeId: source, sourcePortId: "output",
    objectKey: "frontend", role: "user" as const } };
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals(); });

describe("frontend display definition transactions", () => {
  it("keeps newly created workflows blank even with the frontend package available", () => {
    const graph = useWorkflowGraphStore(); graph.catalog = graphClone(frontendCatalog);
    graph.packageLock = graphClone(frontendProjectLock); graph.executionPackageLock = graphClone(frontendExecutionLock);
    graph.createWorkflow();
    expect(graph.document!.nodes).toEqual([]); expect(graph.document!.edges).toEqual([]);
    expect(graph.document!.schema_version).toBe(2); expect(graph.document!.object_bindings).toEqual([]);
    expect(graph.document!.package_lock).toEqual(frontendExecutionLock);
  });
  it("accepts the entire recommendation as one undo/redo step and preserves it through draft persistence", () => {
    const { graph, id, request } = fixture(), before = graphClone(graph.document);
    expect(graph.attachFrontendDisplay(request)).toBe(id);
    const after = graphClone(graph.document);
    expect(graph.selectedNodeIds).toHaveLength(3);
    graph.undo(); expect(graph.document).toEqual(before);
    graph.undo(true); expect(graph.document).toEqual(after);
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true);
    expect(graph.document).toEqual(after);
  });
  it("does not change the old saved definition when the recommendation creates an editable full copy", () => {
    const { graph, id, request } = fixture(); const workspace = useWorkspaceStore();
    workspace.markWorkflowSaved(id);
    const before = graphClone(graph.entries[id].document);
    const copiedId = graph.attachFrontendDisplay(request)!;
    expect(copiedId).not.toBe(id); expect(workspace.activeWorkflowId).toBe(copiedId);
    expect(graph.entries[id].document).toEqual(before);
    expect(graph.document!.nodes).toHaveLength(before.nodes.length + 3);
    graph.undo();
    expect(graph.document!.nodes).toEqual(before.nodes);
    expect(graph.document!.edges).toEqual(before.edges);
    expect(graph.entries[id].document).toEqual(before);
  });
  it("edits role and object key using ordinary config patches and undoes the complete config", () => {
    const { graph, request } = fixture(); graph.attachFrontendDisplay(request);
    const node = graph.document!.nodes.find(node => node.component_id === "frontend.state.append")!;
    const before = graphClone(graph.document);
    graph.patchNode(node.node_binding_id, { config: { ...node.config, role: "system", object_key: "other" } });
    expect(graph.document!.nodes.find(row => row.node_binding_id === node.node_binding_id)?.config)
      .toEqual({ role: "system", object_key: "other" });
    expect(graph.document!.object_bindings).toEqual(before!.object_bindings);
    graph.undo(); expect(graph.document).toEqual(before);
  });
  it("replaces, adds and removes source edges as complete undoable edits without renumbering old edges", () => {
    const { graph, source, request } = fixture(); graph.attachFrontendDisplay(request);
    const replacement = graph.addNode("tools.current-input@1", { x: 0, y: 100 })!;
    const append = graph.document!.nodes.find(node => node.component_id === "frontend.state.append")!.node_binding_id;
    const edge = graph.document!.edges.find(edge => edge.target_node_id === append && edge.target_port_id === "content")!;
    const edgeId = edge.edge_id; const before = graphClone(graph.document);
    expect(graph.patchFrontendSource(append, edgeId, { nodeId: replacement, portId: "output" })).toBeTruthy();
    expect(graph.document!.edges.find(row => row.edge_id === edgeId)).toMatchObject({ order: 0, source_node_id: replacement });
    graph.undo(); expect(graph.document).toEqual(before); graph.undo(true);
    graph.patchFrontendSource(append, null, { nodeId: source, portId: "output" });
    const added = graph.document!.edges.find(row => row.target_node_id === append && row.target_port_id === "content" && row.edge_id !== edgeId)!;
    expect(added.order).toBe(1);
    graph.patchFrontendSource(append, added.edge_id, null);
    expect(graph.document!.edges.some(row => row.edge_id === added.edge_id)).toBe(false);
    graph.undo(); expect(graph.document!.edges.some(row => row.edge_id === added.edge_id)).toBe(true);
  });
  it("copies the full saved graph for source edits and undo restores its source plus roots, controls and bindings", () => {
    const { graph, id, source, request } = fixture(); graph.attachFrontendDisplay(request);
    graph.attachFrontendDisplay({ ...request, role: "assistant" });
    const replacement = graph.addNode("tools.current-input@1", { x: 0, y: 100 })!;
    const append = graph.document!.nodes.filter(node => node.component_id === "frontend.state.append")[1].node_binding_id;
    const edge = graph.document!.edges.find(row => row.target_node_id === append && row.target_port_id === "content")!;
    const before = graphClone(graph.document!); useWorkspaceStore().markWorkflowSaved(id);
    const copiedId = graph.patchFrontendSource(append, edge.edge_id, { nodeId: replacement, portId: "output" })!;
    expect(copiedId).not.toBe(id);
    expect(graph.entries[id].document).toEqual(before);
    expect(graph.document!.object_bindings).toEqual(before.object_bindings);
    expect(graph.document!.control_edges).toEqual(before.control_edges);
    expect(graph.document!.execution_roots).toEqual(before.execution_roots);
    expect(graph.document!.edges.find(row => row.edge_id === edge.edge_id)?.source_node_id).toBe(replacement);
    graph.undo();
    expect(graph.document!.edges).toEqual(before.edges);
    expect(graph.document!.nodes).toEqual(before.nodes);
    expect(graph.document!.edges.find(row => row.edge_id === edge.edge_id)?.source_node_id).toBe(source);
    expect(graph.document!.object_bindings).toEqual(before.object_bindings);
    expect(graph.document!.execution_roots).toEqual(before.execution_roots);
    expect(graph.document!.control_edges).toEqual(before.control_edges);
    expect(graph.entries[id].document).toEqual(before);
  });
  it("rolls back a rejected recommendation and reports its explicit package error", () => {
    const { graph, request } = fixture(); graph.packageLock = []; graph.executionPackageLock = [];
    const before = graphClone(graph.document), history = graph.history[useWorkspaceStore().activeWorkflowId].past.length;
    expect(graph.attachFrontendDisplay(request)).toBeNull();
    expect(graph.document).toEqual(before);
    expect(graph.history[useWorkspaceStore().activeWorkflowId].past).toHaveLength(history);
    expect(JSON.stringify(useWorkbenchNoticesStore().$state)).toContain("packages.configure");
  });
  it("rolls back an incompatible source edit and blocks all display changes while a command is pending", () => {
    const { graph, request } = fixture(); graph.attachFrontendDisplay(request);
    const append = graph.document!.nodes.find(node => node.component_id === "frontend.state.append")!.node_binding_id;
    const edge = graph.document!.edges.find(edge => edge.target_node_id === append && edge.target_port_id === "content")!;
    const before = graphClone(graph.document);
    expect(graph.patchFrontendSource(append, edge.edge_id, { nodeId: append, portId: "view" })).toBeNull();
    expect(graph.document).toEqual(before);
    graph.busy = "start";
    expect(graph.attachFrontendDisplay(request)).toBeNull();
    expect(graph.patchFrontendSource(append, edge.edge_id, null)).toBeNull();
    expect(graph.document).toEqual(before);
  });
});
