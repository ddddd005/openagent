import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone, type GraphDocument, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
const source: GraphNodeType = { component_id: "workflow.text", component_version: "1", display_name: "文本", category: "内容",
  config_schema: { type: "object", properties: { text: { type: "string" } } }, default_config: { text: "hello" },
  inputs: [], outputs: [{ port_id: "output", data_type: "TEXT", required: true, multiple: false }], is_output: false, executable: true };
const sink: GraphNodeType = { ...source, component_id: "workflow.output", display_name: "输出", is_output: true,
  default_config: { mode: "text" }, inputs: [{ port_id: "input", data_type: "TEXT", required: true, multiple: false }] };
function fixture() {
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true); graph.catalog = [source,sink];
  const id = graph.createWorkflow();
  const text = graph.addNode("workflow.text@1", { x: 0, y: 0 })!;
  const output = graph.addNode("workflow.output@1", { x: 300, y: 0 })!;
  graph.connect({ source_node_id: text, source_port_id: "output", target_node_id: output, target_port_id: "input" });
  return { graph, id, text, output };
}
function session(document: GraphDocument, sid = crypto.randomUUID()): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: sid,
    workflow_definition_id: document.workflow_definition_id, definition_revision: document.revision,
    revision: 1, data_revision: 0, head_revision: 1, status: "idle", can_submit: true,
    nodes: document.nodes.map(n => ({ node_binding_id: n.node_binding_id, label: n.title, status: "idle",
      run_id: null, revision: null, outputs: {}, diagnostic: null })), chains: [], outputs: [], data: { revision: 0, values: {} } };
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals(); });
describe("generic workbench execution and document transactions", () => {
  it("uses a registered independent plugin without a frontend kind branch", () => {
    const { graph } = fixture();
    graph.catalog.push({ ...source, component_id: "independent.upper", display_name: "大写" });
    const id = graph.addNode("independent.upper@1", { x: 10, y: 20 })!;
    expect(graph.document?.nodes.find(n => n.node_binding_id === id)?.component_id).toBe("independent.upper");
    graph.selectedNodeIds = [id]; graph.removeSelection(); graph.undo();
    expect(graph.document?.nodes.some(n => n.node_binding_id === id)).toBe(true);
  });
  it("moves different node types as one transaction and preserves edges during mode edits", () => {
    const { graph, text, output } = fixture();
    graph.moveNodes([{ id: text, position: { x: 22, y: 33 } }, { id: output, position: { x: 444, y: 555 } }]);
    graph.undo();
    expect(graph.document?.nodes.map(n => n.position)).toEqual([{ x:0,y:0 },{ x:300,y:0 }]);
    graph.undo(true);
    expect(graph.document?.nodes[1].position).toEqual({ x:444,y:555 });
    const edges = graphClone(graph.document?.edges);
    graph.patchNode(output, { config: { mode: "prompt" } }); expect(graph.document?.edges).toEqual(edges);
  });
  it("saves exact definitions and starts with empty external inputs and no legacy endpoints", async () => {
    const { graph } = fixture();
    let document: GraphDocument; let view: GraphSession;
    const calls: { path:string; body:Record<string,unknown> }[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, options) => {
      const body = JSON.parse(options.body); calls.push({ path, body });
      if (path === "/api/graph/definitions") { document = body.document; return new Response(JSON.stringify(document)); }
      if (path === "/api/graph/sessions") { view = session(document!); return new Response(JSON.stringify(view)); }
      view = { ...view!, status: "succeeded", revision: 2 }; return new Response(JSON.stringify(view));
    }));
    await graph.submitPrimary();
    expect(calls.map(call => call.path)).toEqual(["/api/graph/definitions", "/api/graph/sessions", `/api/graph/sessions/${graph.session?.workflow_session_id}/runs`]);
    expect(calls[2].body.inputs).toEqual({}); expect(graph.session?.status).toBe("succeeded");
    expect(useWorkspaceStore().activeWorkflow.state).toBe("draft");
  });
  it("keeps a lost copy command across reload and replays its same UUID and body", async () => {
    const { graph, id, text } = fixture(); const workspace = useWorkspaceStore();
    const doc = graph.document!; const view = session(doc);
    graph.entries[id].session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    workspace.activeWorkflow.state = "saved";
    stubGraphApplicationFetch( vi.fn().mockRejectedValue(new Error("lost")));
    graph.patchNode(text, { config: { text: "changed" } });
    await vi.waitFor(() => expect(graph.busy).toBeNull());
    const target = workspace.workflows.find(w => w.sourceId === id)!;
    const original = graphClone(graph.entries[target.id].pending);
    expect(target.copyPending).toBe(true); expect(workspace.activeWorkflowId).toBe(id);
    const snapshot = graph.storeSnapshot();
    expect(graph.restoreSnapshot(snapshot)).toBe(true);
    const fetcher = vi.fn(async (_path, init) => new Response(JSON.stringify(session((JSON.parse(init.body)).document))));
    stubGraphApplicationFetch( fetcher); await graph.reconcile(target.id);
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual(original?.body);
    expect(workspace.activeWorkflowId).toBe(target.id); expect(graph.entries[target.id].pending).toBeNull();
  });
  it("rebinds a copied session after later edits before starting another run", async () => {
    const { graph, id, text } = fixture(); const view = session(graph.document!);
    graph.entries[id].saved_revision = 1; graph.entries[id].saved_document = graphClone(graph.document!);
    graph.entries[id].session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    graph.patchNode(text, { config: { text: "second edit" } });
    const calls: string[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      calls.push(path); const body = init.body ? JSON.parse(init.body) : {};
      if (path === "/api/graph/definitions") return new Response(JSON.stringify(body.document));
      if (path.endsWith("/rebind")) return new Response(JSON.stringify({ ...view, definition_revision:2, revision:2 }));
      if (path.endsWith("/runs")) return new Response(JSON.stringify({ ...view, definition_revision:2, revision:3, status:"succeeded" }));
      return new Response(JSON.stringify(view));
    }));
    await graph.submitPrimary();
    expect(calls).toEqual(["/api/graph/definitions", `/api/graph/sessions/${view.workflow_session_id}`, `/api/graph/sessions/${view.workflow_session_id}/rebind`, `/api/graph/sessions/${view.workflow_session_id}/runs`]);
  });
  it("preserves definitive graph diagnostics and blocks mutations when request persistence fails", async () => {
    const { graph } = fixture(); graph.setPersistenceGuard(() => false);
    const fetcher = vi.fn(); stubGraphApplicationFetch( fetcher); await graph.submitPrimary();
    expect(fetcher).not.toHaveBeenCalled(); expect(graph.pending).toBeNull();
    graph.setPersistenceGuard(() => true);
    stubGraphApplicationFetch( vi.fn(async () => new Response(JSON.stringify({ error: { code:"graph_no_outputs",
      diagnostic: { reason_code:"graph_no_outputs", message:"No output" } } }), { status:400 })));
    await graph.submitPrimary(); expect(graph.active?.diagnostics?.[0].reason_code).toBe("graph_no_outputs");
  });
  it("accepts the closed control receipt and allows a later fresh run", async () => {
    const { graph,id } = fixture(); const view = session(graph.document!);
    graph.entries[id].saved_revision = 1; graph.entries[id].saved_document = graphClone(graph.document!);
    graph.entries[id].session_id = view.workflow_session_id;
    graph.views[view.workflow_session_id] = { ...view,status:"failed",can_submit:false,available_actions:["close"] };
    const calls:string[] = [];
    stubGraphApplicationFetch(vi.fn(async (path,init) => {
      calls.push(path); const body = init.body ? JSON.parse(init.body) : {};
      if (body.action === "close") return new Response(JSON.stringify({ ...view,status:"closed",revision:2 }));
      if (path.endsWith("/runs")) return new Response(JSON.stringify({ ...view,status:"succeeded",revision:3 }));
      return new Response(JSON.stringify({ ...view,status:"closed",revision:2 }));
    }));
    await graph.closeRun(); expect(graph.pending).toBeNull(); expect(graph.session?.status).toBe("closed");
    await graph.submitPrimary(); expect(calls.at(-1)).toBe(`/api/graph/sessions/${view.workflow_session_id}/runs`);
  });
});
