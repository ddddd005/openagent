import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone, isGraphDocument, type GraphDocument, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";

const writer: GraphNodeType = { component_id: "plugin.writer", component_version: "1", display_name: "writer",
  category: "plugin", config_schema: {}, default_config: { object_key: "shared/main" },
  inputs: [], outputs: [], executable: true, is_output: false, input_storage: "references",
  capabilities: ["objects:read", "objects:write"] };
const source: GraphNodeType = { ...writer, component_id: "plugin.source", display_name: "source",
  outputs: [{ port_id: "output", data_type: "TEXT", data_schema_version: 2, required: true, multiple: false }],
  capabilities: ["objects:read"] };
const sink: GraphNodeType = { ...writer, component_id: "plugin.output", display_name: "output", is_output: true,
  default_config: {}, capabilities: [], inputs: [{ port_id: "input", data_type: "TEXT",
    data_schema_version: 2, required: true, multiple: false }], outputs: source.outputs };
const lock = [{ package_id: "plugin", version: "1.0.0" }];
function fixture() {
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
  graph.catalog = [writer, source, sink]; graph.packageLock = lock;
  const id = graph.createWorkflow();
  const write = graph.addNode("plugin.writer@1", { x: 0, y: 0 })!;
  const read = graph.addNode("plugin.source@1", { x: 100, y: 0 })!;
  const output = graph.addNode("plugin.output@1", { x: 200, y: 0 })!;
  graph.connect({ source_node_id: read, source_port_id: "output", target_node_id: output, target_port_id: "input" });
  return { graph, id, write, read, output };
}
function binding(readers: string[], writers: string[]) {
  return { object_key: "shared/main", type_id: "plugin.object", schema_version: 1,
    scope: "shared" as const, owner_node_id: null, readers, writers };
}
function session(document: GraphDocument): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
    workflow_definition_id: document.workflow_definition_id, definition_revision: document.revision,
    revision: 1, data_revision: 0, head_revision: 1, status: "idle", can_submit: true,
    nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} }, objects: {} };
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals(); });

describe("minimum v2 tools/prompt workbench wiring", () => {
  it("upgrades on an object writer even though it has no content outputs and records the loaded package lock", () => {
    const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
    graph.catalog = [writer]; graph.packageLock = lock; graph.createWorkflow();
    expect(graph.document?.schema_version).toBe(1);
    graph.addNode("plugin.writer@1", { x: 0, y: 0 });
    expect(graph.document?.schema_version).toBe(2);
    expect(graph.document?.package_lock).toEqual(lock);
    expect(graph.document?.object_bindings).toEqual([]);
    expect(graph.catalog[0].outputs).toEqual([]);
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true);
  });
  it("edits roots and control predecessors as undoable document transactions with stable edge IDs", () => {
    const { graph, write, read } = fixture();
    graph.setExecutionRoot(write, true); graph.setControlDependencies(read, [write]);
    const controlId = graph.document!.control_edges![0].edge_id;
    expect(graph.document?.execution_roots).toEqual([write]);
    graph.setControlDependencies(read, [write]);
    expect(graph.document!.control_edges![0].edge_id).toBe(controlId);
    graph.undo();
    expect(graph.document?.control_edges).toEqual([]);
    graph.undo(true);
    expect(graph.document!.control_edges![0].edge_id).toBe(controlId);
    expect(graph.setControlDependencies(read, [crypto.randomUUID()])).toBeNull();
    expect(graph.setExecutionRoot(crypto.randomUUID(), true)).toBeNull();
    expect(isGraphDocument(graph.document)).toBe(true);
  });
  it("validates binding edits against real node UUIDs and keeps node object keys explicit", () => {
    const { graph, write, read } = fixture();
    expect(graph.setObjectBindings([binding([write, read], [write])])).toBeTruthy();
    expect(graph.document?.nodes[0].config.object_key).toBe("shared/main");
    const before = graphClone(graph.document);
    expect(graph.setObjectBindings([binding([crypto.randomUUID()], [write])])).toBeNull();
    expect(graph.document).toEqual(before);
    expect(graph.setObjectBindings([binding([write], [write]), binding([write], [write])])).toBeNull();
  });
  it("duplicates controls, roots and shared permissions while initializing a separate private binding", () => {
    const { graph, write, read } = fixture();
    const owned = { ...binding([write], [write]), object_key: "private/main",
      scope: "private" as const, owner_node_id: write, default_value: { count: 0 } };
    graph.patchNode(write, { config: { object_key: owned.object_key, object_keys: ["shared/main", owned.object_key] } });
    graph.setObjectBindings([binding([write, read], [write]), owned]);
    graph.setExecutionRoot(write, true); graph.setControlDependencies(read, [write]);
    graph.selectedNodeIds = [write, read]; graph.duplicateSelection();
    const [copiedWriter, copiedReader] = graph.selectedNodeIds;
    expect(graph.document?.execution_roots).toEqual([write, copiedWriter]);
    expect(graph.document?.control_edges?.find(edge => edge.target_node_id === copiedReader)?.source_node_id).toBe(copiedWriter);
    const shared = graph.document!.object_bindings!.find(item => item.scope === "shared")!;
    expect(shared.readers).toEqual([write, read, copiedWriter, copiedReader]);
    expect(shared.writers).toEqual([write, copiedWriter]);
    const privateCopy = graph.document!.object_bindings!.find(item => item.owner_node_id === copiedWriter)!;
    expect(privateCopy.object_key).not.toBe(owned.object_key);
    expect(privateCopy.default_value).toEqual({ count: 0 });
    const config = graph.document!.nodes.find(node => node.node_binding_id === copiedWriter)!.config;
    expect(config.object_key).toBe(privateCopy.object_key);
    expect(config.object_keys).toEqual(["shared/main", privateCopy.object_key]);
    expect(isGraphDocument(graph.document)).toBe(true);
    graph.undo();
    expect(graph.document?.object_bindings).toEqual([binding([write, read], [write]), owned]);
  });
  it("removes roots, control dependencies and deleted-node permissions without leaving an ownerless private binding", () => {
    const { graph, write, read } = fixture();
    graph.setObjectBindings([binding([write, read], [write]),
      { ...binding([write], [write]), object_key: "private/main", scope: "private", owner_node_id: write }]);
    graph.setExecutionRoot(write, true); graph.setControlDependencies(read, [write]);
    graph.selectedNodeIds = [write]; graph.removeSelection();
    expect(graph.document?.execution_roots).toEqual([]);
    expect(graph.document?.control_edges).toEqual([]);
    expect(graph.document?.object_bindings).toEqual([binding([read], [])]);
    expect(isGraphDocument(graph.document)).toBe(true);
    graph.undo(); expect(graph.document?.object_bindings?.length).toBe(2);
  });
  it("removes a selected control edge without changing the content graph", () => {
    const { graph, write, read } = fixture();
    graph.setControlDependencies(read, [write]);
    const data = graphClone(graph.document!.edges);
    graph.selectedNodeIds = []; graph.selectedEdgeId = graph.document!.control_edges![0].edge_id;
    graph.removeSelection();
    expect(graph.document?.control_edges).toEqual([]); expect(graph.document?.edges).toEqual(data);
  });
  it("loads exact v2 catalog metadata and sends the complete control/binding definition before a run", async () => {
    const { graph, write, read } = fixture();
    let saved: GraphDocument | undefined; let view: GraphSession | undefined;
    const calls: { path: string; body?: Record<string, unknown> }[] = [];
    const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
      const body = options?.body ? JSON.parse(String(options.body)) : undefined;
      calls.push({ path, body });
      if (path === "/api/graph/node-types/v2") return new Response(JSON.stringify({ schema_version: 2,
        node_types: [writer, source, sink], package_lock: lock,
        data_types: [{ type_id: "plugin.object", schema_version: 1, scope: "session", schema: {}, has_default: true, default_value: {} }] }));
      if (path === "/api/graph/definitions") { saved = body.document; return new Response(JSON.stringify(saved)); }
      if (path === "/api/graph/sessions") { view = session(saved!); return new Response(JSON.stringify(view)); }
      return new Response(JSON.stringify({ ...view!, revision: 2, status: "succeeded" }));
    });
    stubGraphApplicationFetch( fetcher);
    await graph.loadCatalog();
    expect(graph.dataTypes[0].type_id).toBe("plugin.object");
    graph.setExecutionRoot(write, true); graph.setControlDependencies(read, [write]);
    graph.setObjectBindings([binding([write, read], [write])]);
    await graph.submitPrimary();
    expect(saved?.schema_version).toBe(2);
    expect(saved?.execution_roots).toEqual([write]);
    expect(saved?.control_edges?.[0].source_node_id).toBe(write);
    expect(saved?.object_bindings?.[0].object_key).toBe("shared/main");
    expect(calls.some(call => call.path === "/api/graph/node-types")).toBe(false);
    expect(graph.pending).toBeNull(); expect(graph.session?.objects).toEqual({});
  });
  it("keeps controls and object declarations when editing a saved graph creates a copied draft", () => {
    const { graph, id, write, read } = fixture();
    graph.setObjectBindings([binding([write, read], [write])]);
    graph.setExecutionRoot(write, true); graph.setControlDependencies(read, [write]);
    const before = graphClone(graph.document!);
    useWorkspaceStore().activeWorkflow.state = "saved";
    graph.patchNode(write, { title: "copied edit" });
    expect(useWorkspaceStore().activeWorkflowId).not.toBe(id);
    expect(graph.document?.control_edges).toEqual(before.control_edges);
    expect(graph.document?.execution_roots).toEqual(before.execution_roots);
    expect(graph.document?.object_bindings).toEqual(before.object_bindings);
    expect(graph.entries[id].document).toEqual(before);
  });
});
