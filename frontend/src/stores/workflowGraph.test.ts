import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, isGraphDocument, newGraph, type GraphDocument, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
const source: GraphNodeType = { component_id: "workflow.text", component_version: "1", display_name: "文本", category: "内容",
  config_schema: { type: "object", properties: { text: { type: "string" } } }, default_config: { text: "hello" },
  inputs: [], outputs: [{ port_id: "output", data_type: "TEXT", required: true, multiple: false }], is_output: false, executable: true };
const sink: GraphNodeType = { ...source, component_id: "workflow.output", display_name: "输出", is_output: true,
  default_config: { mode: "text" }, inputs: [{ port_id: "input", data_type: "TEXT", required: true, multiple: false }] };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(release => { resolve = release; });
  return { promise, resolve };
}
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
  it("resets selection when creating blank drafts and keeps empty/populated switches on the same document contract", async () => {
    const { graph, id, text, output } = fixture(), workspace = useWorkspaceStore();
    const populated = graphClone(graph.document!);
    graph.executionPackageLock = [{ package_id: "plugin", version: "1.0.0" }];
    graph.selectedNodeIds = [text, output]; graph.selectedEdgeId = populated.edges[0].edge_id;
    const blankId = graph.createWorkflow(), blank = graphClone(graph.document!);
    expect(blank.schema_version).toBe(populated.schema_version);
    expect(Object.keys(blank).sort()).toEqual(Object.keys(populated).sort());
    expect(blank.package_lock).toEqual([{ package_id: "plugin", version: "1.0.0" }]);
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
    expect(graph.history[blankId]).toEqual({ past: [], future: [] });
    expect(graph.entries[blankId]).toMatchObject({ saved_revision: 0, session_id: null, pending: null });
    expect(graph.entries[id].document).toEqual(populated);
    const added = graph.addNode("workflow.text@1", { x: 20, y: 30 })!;
    expect(graph.selectedNodeIds).toEqual([added]); expect(isGraphDocument(graph.document)).toBe(true);
    graph.removeSelection();
    expect(graph.document).toEqual(blank);
    graph.selectedNodeIds = [added]; graph.selectedEdgeId = populated.edges[0].edge_id;
    stubGraphApplicationFetch(vi.fn(async () => new Response(JSON.stringify([]))));
    workspace.openWorkflow(id); await graph.activate(id);
    expect(graph.document).toEqual(populated);
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
    graph.selectedNodeIds = [text]; graph.selectedEdgeId = populated.edges[0].edge_id;
    workspace.openWorkflow(blankId); await graph.activate(blankId);
    expect(graph.document).toEqual(blank);
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
    expect(graph.canUndo).toBe(true); expect(graph.entries[id].document).toEqual(populated);
  });
  it("uses the same passive draft registration for provided documents without sharing their state", () => {
    const { graph, id, text } = fixture(), workspace = useWorkspaceStore();
    const before = graphClone(graph.entries[id]), persist = vi.fn(() => true), fetcher = vi.fn();
    graph.setPersistenceGuard(persist); vi.stubGlobal("fetch", fetcher);
    graph.selectedNodeIds = [text]; graph.selectedEdgeId = graph.document!.edges[0].edge_id;
    const doc = newGraph("Plugin draft", [{ package_id: "workflow.tavern", version: "1.2.0" }]);
    doc.nodes = [graphClone(graph.document!.nodes[0])];
    const nextId = graph.createWorkflowDraft(doc, "plugin")!;
    expect(workspace.activeWorkflowId).toBe(nextId);
    expect(workspace.activeWorkflow).toMatchObject({ state: "draft", nodeCount: 1, description: "plugin" });
    expect(graph.entries[nextId]).toMatchObject({ document: doc, saved_revision: 0, session_id: null, pending: null });
    expect(graph.history[nextId]).toEqual({ past: [], future: [] });
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
    expect(graph.entries[id]).toEqual(before);
    doc.nodes[0].config.text = "external mutation"; doc.package_lock![0].version = "2.0.0";
    expect(graph.document!.nodes[0].config.text).toBe("hello");
    expect(graph.document!.package_lock![0].version).toBe("1.2.0");
    expect(persist).toHaveBeenCalledOnce(); expect(fetcher).not.toHaveBeenCalled();
  });
  it("restores and opens frozen v1 definitions without adding package locks or migrating them", async () => {
    const { graph, id } = fixture(), workspace = useWorkspaceStore();
    const old: GraphDocument = { schema_version: 1, workflow_definition_id: id, revision: 7,
      name: "Legacy frozen", nodes: graphClone(graph.document!.nodes), edges: graphClone(graph.document!.edges) };
    const snapshot = graph.storeSnapshot();
    snapshot.entries[id] = { document: graphClone(old), saved_document: graphClone(old),
      saved_revision: 7, session_id: null, pending: null };
    workspace.markWorkflowSaved(id, old.name);
    graph.executionPackageLock = [{ package_id: "plugin", version: "2.0.0" }];
    expect(graph.restoreSnapshot(snapshot)).toBe(true);
    stubGraphApplicationFetch(vi.fn(async () => new Response(JSON.stringify([]))));
    await graph.activate(id);
    expect(graph.entries[id].document).toEqual(old); expect(graph.entries[id].saved_document).toEqual(old);
    expect(graph.document!.schema_version).toBe(1); expect(graph.document!.package_lock).toBeUndefined();
    expect(graph.storeSnapshot().entries[id]).toEqual(snapshot.entries[id]);
  });
  it("keeps an explicitly empty execution lock usable when the catalog arrives after blank creation", async () => {
    const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true); graph.createWorkflow();
    expect(graph.document!.package_lock).toEqual([]);
    let saved: GraphDocument | undefined, view: GraphSession | undefined;
    const lock = [{ package_id: "plugin", version: "1.0.0" }];
    stubGraphApplicationFetch(vi.fn(async (path, options) => {
      const body = options.body ? JSON.parse(options.body) : {};
      if (path === "/api/graph/node-types/v2") return new Response(JSON.stringify({ schema_version: 2,
        node_types: [source, sink], package_lock: lock, execution_package_lock: lock, data_types: [] }));
      if (path === "/api/graph/definitions") { saved = body.document; return new Response(JSON.stringify(saved)); }
      if (path === "/api/graph/sessions") { view = session(saved!); return new Response(JSON.stringify(view)); }
      return new Response(JSON.stringify({ ...view!, revision: 2, status: "succeeded" }));
    }));
    await graph.loadCatalog();
    const text = graph.addNode("workflow.text@1", { x: 0, y: 0 })!;
    const output = graph.addNode("workflow.output@1", { x: 300, y: 0 })!;
    graph.connect({ source_node_id: text, source_port_id: "output", target_node_id: output, target_port_id: "input" });
    await graph.submitPrimary();
    expect(saved).toMatchObject({ schema_version: 2, package_lock: [], execution_roots: [], control_edges: [] });
    expect(saved!.nodes).toHaveLength(2); expect(saved!.edges).toHaveLength(1);
    expect(isGraphDocument(saved)).toBe(true); expect(graph.session!.status).toBe("succeeded");
  });
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
  it("keeps repeated wiring a no-op and duplicates the selected nodes and their edge as one undoable edit", () => {
    const { graph, text, output } = fixture();
    const original = graphClone(graph.document!), undoCount = graph.history[useWorkspaceStore().activeWorkflowId].past.length;
    expect(graph.connect({ source_node_id: text, source_port_id: "output",
      target_node_id: output, target_port_id: "input" })).toBeNull();
    expect(graph.history[useWorkspaceStore().activeWorkflowId].past).toHaveLength(undoCount);
    graph.selectedNodeIds = [text, output]; graph.duplicateSelection();
    const copies = graph.document!.nodes.slice(2);
    expect(graph.document!.edges[1]).toMatchObject({ source_node_id: copies[0].node_binding_id,
      target_node_id: copies[1].node_binding_id, order: 0 });
    expect(new Set(graph.document!.edges.map(edge => edge.edge_id)).size).toBe(2);
    graph.undo(); expect(graph.document).toEqual(original);
    graph.undo(true); expect(graph.document!.nodes).toHaveLength(4);
    graph.selectedNodeIds = [copies[0].node_binding_id]; graph.removeSelection();
    expect(graph.document!.edges).toEqual(original.edges);
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
  it("keeps a lost copy command across reload and reads its same UUID and body", async () => {
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
    const frozen = session(original!.body.document as GraphDocument);
    const fetcher = vi.fn(async (path, init) => {
      if (path === "/api/graph/receipts/read") {
        const request = JSON.parse(init.body);
        return graphReceiptReadResponse(request.operation, request.parameters, frozen);
      }
      return new Response(JSON.stringify(path.endsWith("/sessions") ? [frozen] : frozen));
    });
    stubGraphApplicationFetch( fetcher); await graph.reconcile(target.id);
    expect(fetcher.mock.calls[0][0]).toBe("/api/graph/receipts/read");
    expect(JSON.parse(fetcher.mock.calls[0][1].body).parameters).toEqual({
      ...original?.body, session_id: view.workflow_session_id,
    });
    expect(workspace.activeWorkflowId).toBe(target.id); expect(graph.entries[target.id].pending).toBeNull();
  });
  it("keeps an explicitly retried rejected copy distinct from receipt reconciliation", async () => {
    const { graph, id, text } = fixture(), workspace = useWorkspaceStore();
    const view = session(graph.document!);
    graph.entries[id].session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    workspace.activeWorkflow.state = "saved";
    let rejectedKey: string;
    stubGraphApplicationFetch(vi.fn(async (_path, init) => {
      rejectedKey = JSON.parse(init.body).idempotency_key;
      return new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status: 409 });
    }));
    graph.patchNode(text, { config: { text: "new copy" } });
    await vi.waitFor(() => expect(graph.busy).toBeNull());
    const target = workspace.workflows.find(row => row.sourceId === id)!;
    expect(graph.entries[target.id].pending).toBeNull(); expect(target.copyPending).toBe(true);
    expect(graph.entries[target.id].rejected_copy?.body.idempotency_key).toBe(rejectedKey!);
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true);
    expect(graph.canRetryRejectedCopy(target.id)).toBe(true);
    const current = { ...view, revision: 4, data_revision: 2, head_revision: 3, data: { revision: 2, values: {} } };
    const paths: string[] = [], commands: Record<string, unknown>[] = [];
    stubGraphApplicationFetch(vi.fn(async (path, init) => {
      paths.push(path);
      if (path === `/api/graph/sessions/${view.workflow_session_id}`) return new Response(JSON.stringify(current));
      const body = JSON.parse(init.body); commands.push(body);
      return new Response(JSON.stringify(session(body.document)));
    }));
    await graph.reconcile(target.id);
    expect(paths).toEqual([]);
    await graph.retryRejectedCopy(target.id);
    expect(paths).toEqual([`/api/graph/sessions/${view.workflow_session_id}`, `/api/graph/sessions/${view.workflow_session_id}/copy`]);
    expect(commands[0]).toMatchObject({ expected_session_revision: 4, expected_data_revision: 2, expected_head_revision: 3 });
    expect(commands[0].idempotency_key).not.toBe(rejectedKey!);
    expect(target.copyPending).toBe(false); expect(graph.entries[target.id].pending).toBeNull();
    expect(graph.entries[target.id].rejected_copy).toBeUndefined();
  });
  it("preserves a restored copy without original coordinates or rejection evidence without making a new request", async () => {
    const { graph, id, text } = fixture(), workspace = useWorkspaceStore();
    const view = session(graph.document!);
    graph.entries[id].session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    workspace.activeWorkflow.state = "saved";
    stubGraphApplicationFetch(vi.fn().mockRejectedValue(new Error("unknown copy outcome")));
    graph.patchNode(text, { title: "retained incomplete copy" });
    await vi.waitFor(() => expect(graph.busy).toBeNull());
    const target = workspace.workflows.find(row => row.sourceId === id)!;
    const snapshot = graph.storeSnapshot();
    snapshot.entries[target.id].pending = null;
    snapshot.entries[target.id].diagnostics = [{ reason_code: "stale_revision", message: "old diagnostic is not request evidence" }];
    expect(graph.restoreSnapshot(snapshot)).toBe(true);
    const before = graphClone(graph.entries[target.id]), fetcher = vi.fn();
    stubGraphApplicationFetch(fetcher);
    await graph.reconcile(target.id); await graph.retryRejectedCopy(target.id);
    expect(fetcher).not.toHaveBeenCalled();
    expect(graph.entries[target.id]).toEqual(before); expect(target.copyPending).toBe(true);
    expect(graph.canRetryRejectedCopy(target.id)).toBe(false);
    expect(useWorkbenchNoticesStore().error).toMatchObject({ kind: "unknown" });
  });
  it.each([404, 410])("retains an unknown copy after an HTTP %s receipt read and blocks explicit retries", async status => {
    const { graph, id, text } = fixture(), workspace = useWorkspaceStore(), view = session(graph.document!);
    graph.entries[id].session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    workspace.activeWorkflow.state = "saved";
    stubGraphApplicationFetch(vi.fn().mockRejectedValue(new Error("unknown copy outcome")));
    graph.patchNode(text, { title: "unknown copy" });
    await vi.waitFor(() => expect(graph.busy).toBeNull());
    const target = workspace.workflows.find(row => row.sourceId === id)!;
    const original = graphClone(graph.entries[target.id].pending!), paths: string[] = [];
    stubGraphApplicationFetch(vi.fn(async path => {
      paths.push(path);
      return new Response(JSON.stringify({ error: { reason_code: "not_found" } }), { status });
    }));
    await graph.reconcile(target.id); await graph.retryRejectedCopy(target.id);
    expect(paths).toEqual(["/api/graph/receipts/read"]);
    expect(graph.entries[target.id].pending).toEqual(original);
    expect(graph.entries[target.id].rejected_copy).toBeUndefined(); expect(target.copyPending).toBe(true);
  });
  it("blocks duplicate explicit copy retries throughout the fresh source read and the submission", async () => {
    const { graph, id, text } = fixture(), workspace = useWorkspaceStore(), view = session(graph.document!);
    graph.entries[id].session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    workspace.activeWorkflow.state = "saved";
    stubGraphApplicationFetch(vi.fn(async () =>
      new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status: 409 })));
    graph.patchNode(text, { title: "rejected copy" });
    await vi.waitFor(() => expect(graph.busy).toBeNull());
    const target = workspace.workflows.find(row => row.sourceId === id)!, query = deferred<Response>();
    const paths: string[] = [];
    stubGraphApplicationFetch(vi.fn(async (path, init) => {
      paths.push(path);
      if (path.endsWith("/copy")) return new Response(JSON.stringify(session(JSON.parse(init.body).document)));
      return query.promise;
    }));
    const retrying = graph.retryRejectedCopy(target.id);
    await graph.retryRejectedCopy(target.id); await graph.reconcile(target.id);
    expect(paths).toEqual([`/api/graph/sessions/${view.workflow_session_id}`]);
    expect(graph.locked).toBe(true);
    query.resolve(new Response(JSON.stringify(view))); await retrying;
    expect(paths).toEqual([`/api/graph/sessions/${view.workflow_session_id}`,
      `/api/graph/sessions/${view.workflow_session_id}/copy`]);
    expect(target.copyPending).toBe(false); expect(graph.entries[target.id].pending).toBeNull();
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
