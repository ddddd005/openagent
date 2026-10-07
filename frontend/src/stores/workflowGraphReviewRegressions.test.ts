import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, newGraph, type GraphDocument, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
import { graphWorkbenchStorage } from "../testUtils/graphWorkbenchStorage";

const textType: GraphNodeType = { component_id: "workflow.text", component_version: "1", display_name: "text",
  category: "content", config_schema: { type: "object" }, default_config: {}, inputs: [], outputs: [],
  is_output: false, executable: true };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(release => { resolve = release; });
  return { promise, resolve };
}
const response = (value: unknown) => new Response(JSON.stringify(value));
function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .sort(([a], [b]) => a.localeCompare(b)).map(([key, row]) => [key, canonical(row)]));
  return value;
}
function memory() {
  const workspace = useWorkspaceStore();
  return graphWorkbenchStorage({ graph: useWorkflowGraphStore().storeSnapshot(), catalog: workspace.workflows,
    activeWorkflowId: workspace.activeWorkflowId, selectedWorkflowId: workspace.selectedWorkflowId });
}
function view(document: GraphDocument, status = "idle"): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
    workflow_definition_id: document.workflow_definition_id, definition_revision: document.revision,
    revision: 1, data_revision: 0, head_revision: 1, status, can_submit: status === "idle",
    nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} } };
}
function setup(status = "idle") {
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true); graph.catalog = [textType];
  const id = graph.createWorkflow();
  const current = view(graph.document!, status);
  graph.entries[id].saved_revision = current.definition_revision;
  graph.entries[id].saved_document = graphClone(graph.document!);
  graph.entries[id].session_id = current.workflow_session_id;
  graph.views[current.workflow_session_id] = current;
  return { graph, id, current, workspace: useWorkspaceStore() };
}
beforeEach(() => { setActivePinia(createPinia()); vi.useFakeTimers(); });
afterEach(() => {
  useWorkbenchPersistenceStore().$dispose(); useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose();
  vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals();
});
describe("reviewed graph receipt and operation races", () => {
  it("accepts a canonical lost-save receipt read with its original body and rejects changed content", async () => {
    const { graph, id } = setup();
    graph.entries[id].session_id = null;
    graph.entries[id].document.nodes.push({ node_binding_id: crypto.randomUUID(), component_id: "external.test",
      component_version: "1", title: "plugin", position: { x: 1, y: 2 },
      config: { z: "retained", nested: { z: 3, a: [false, 1, "1"] } } });
    stubGraphApplicationFetch( vi.fn().mockRejectedValue(new Error("lost receipt")));
    expect(await graph.saveWorkflow()).toBe(false);
    const request = graphClone(graph.pending)!;
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true);
    const calls: unknown[] = [];
    let invalid = true;
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(init.body), body = envelope.parameters; calls.push(body);
        const receipt = graphClone(body.document);
        if (invalid) receipt.nodes[0].config.z = "foreign content";
        return graphReceiptReadResponse(envelope.operation, body, canonical(receipt) as Record<string, unknown>);
      }
      return response([]);
    }));
    await graph.reconcile(id);
    expect(graph.pending).toEqual(request);
    invalid = false; await graph.reconcile(id);
    expect(calls).toEqual([request.body, request.body]);
    expect(graph.pending).toBeNull(); expect(graph.entries[id].saved_revision).toBe(2);
    expect(graph.entries[id].saved_document?.nodes[0].config).toEqual((request.body.document as GraphDocument).nodes[0].config);
  });

  it("blocks start during selection and restores the complete persisted workbench", async () => {
    const { graph, id, current } = setup();
    const storage = memory(); const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    const next = view(graph.document!); const choice = deferred<Response>();
    const fetcher = vi.fn(path => path.endsWith(next.workflow_session_id) ? choice.promise
      : Promise.resolve(response(current)));
    stubGraphApplicationFetch( fetcher);
    const selecting = graph.selectSession(next.workflow_session_id);
    expect(graph.locked).toBe(true);
    await graph.submitPrimary();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(graph.entries[id].session_id).toBe(current.workflow_session_id);
    expect(graph.pending).toBeNull();
    choice.resolve(response(next)); await selecting;
    expect(graph.entries[id].session_id).toBe(next.workflow_session_id);
    expect(persistence.save()).toBe(true);
    persistence.$dispose(); graph.$dispose(); setActivePinia(createPinia());
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    expect(restored.status).toBe("saved");
    expect(useWorkflowGraphStore().entries[id].session_id).toBe(next.workflow_session_id);
    expect(useWorkflowGraphStore().entries[id].pending).toBeNull();
  });

  it("lets only the latest selection win and retains its lock when an older read ends", async () => {
    const { graph, current } = setup();
    const b = view(graph.document!), c = view(graph.document!);
    const first = deferred<Response>(), latest = deferred<Response>();
    stubGraphApplicationFetch( vi.fn(path => path.endsWith(b.workflow_session_id) ? first.promise : latest.promise));
    const selectingB = graph.selectSession(b.workflow_session_id);
    const selectingC = graph.selectSession(c.workflow_session_id);
    first.resolve(response(b)); await selectingB;
    expect(graph.session?.workflow_session_id).toBe(current.workflow_session_id);
    expect(graph.locked).toBe(true);
    latest.resolve(response(c)); await selectingC;
    expect(graph.session?.workflow_session_id).toBe(c.workflow_session_id); expect(graph.locked).toBe(false);
  });

  it("ignores an older selection that finishes after the latest one", async () => {
    const { graph } = setup(); const b = view(graph.document!), c = view(graph.document!);
    const first = deferred<Response>(), latest = deferred<Response>();
    stubGraphApplicationFetch( vi.fn(path => path.endsWith(b.workflow_session_id) ? first.promise : latest.promise));
    const selectingB = graph.selectSession(b.workflow_session_id), selectingC = graph.selectSession(c.workflow_session_id);
    latest.resolve(response(c)); await selectingC;
    first.resolve(response(b)); await selectingB;
    expect(graph.session?.workflow_session_id).toBe(c.workflow_session_id);
  });

  it("ignores a selection from before leaving and returning to the same workflow", async () => {
    const { graph, id, current, workspace } = setup();
    const next = view(graph.document!), choice = deferred<Response>();
    stubGraphApplicationFetch( vi.fn(path => {
      if (path.endsWith(next.workflow_session_id)) return choice.promise;
      if (path.endsWith("/sessions")) return Promise.resolve(response([current]));
      return Promise.resolve(response(current));
    }));
    const selecting = graph.selectSession(next.workflow_session_id);
    const other = graph.createWorkflow(); await graph.activate(other);
    workspace.openWorkflow(id); await graph.activate(id);
    choice.resolve(response(next)); await selecting;
    expect(graph.entries[id].session_id).toBe(current.workflow_session_id); expect(graph.locked).toBe(false);
  });

  it("ignores a selection when save starts and finishes before its response arrives", async () => {
    const { graph, id, current } = setup(); const next = view(graph.document!), choice = deferred<Response>();
    graph.entries[id].document.name = "changed definition";
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      if (path.endsWith(next.workflow_session_id)) return choice.promise;
      if (path === "/api/graph/definitions") return response(JSON.parse(init.body).document);
      if (path.endsWith("/rebind")) return response({ ...current, definition_revision: 2, revision: 2 });
      return response(current);
    }));
    const selecting = graph.selectSession(next.workflow_session_id);
    expect(await graph.saveWorkflow()).toBe(true);
    expect(graph.busy).toBeNull(); expect(graph.pending).toBeNull();
    choice.resolve(response(next)); await selecting;
    expect(graph.entries[id].session_id).toBe(current.workflow_session_id);
    expect(graph.session?.definition_revision).toBe(2); expect(graph.locked).toBe(false);
  });

  it("retains the displayed workflow poll after a hidden command receipt", async () => {
    const { graph, id: a, current, workspace } = setup("running");
    const command = deferred<Response>();
    const b = graph.createWorkflow(); const bView = view(graph.document!, "running");
    graph.entries[b].session_id = bView.workflow_session_id; graph.views[bView.workflow_session_id] = bView;
    const paths: string[] = [];
    stubGraphApplicationFetch( vi.fn(async path => {
      paths.push(path);
      if (path.endsWith("/control")) return command.promise;
      if (path.includes(`/definitions/${b}/`)) return response([bView]);
      if (path.endsWith(bView.workflow_session_id)) return response(bView);
      if (path.endsWith("/sessions")) return response([current]);
      return response(current);
    }));
    workspace.openWorkflow(a); const pausing = graph.submitPrimary();
    workspace.openWorkflow(b); await graph.activate(b);
    paths.length = 0;
    command.resolve(response({ ...current, revision: 2 })); await pausing;
    expect(graph.entries[a].pending).toBeNull();
    expect(graph.views[current.workflow_session_id].revision).toBe(2);
    await vi.advanceTimersByTimeAsync(350);
    expect(paths).toEqual([`/api/graph/definitions/${b}/sessions`, `/api/graph/sessions/${bView.workflow_session_id}`]);
  });

  it("retains the displayed workflow poll after a hidden in-flight poll finishes", async () => {
    const { graph, id: a, current, workspace } = setup("running");
    const b = graph.createWorkflow(), bView = view(graph.document!, "running"), oldPoll = deferred<Response>();
    graph.entries[b].session_id = bView.workflow_session_id; graph.views[bView.workflow_session_id] = bView;
    let polling = false; const paths: string[] = [];
    stubGraphApplicationFetch( vi.fn(async path => {
      paths.push(path);
      if (path.includes(`/definitions/${a}/`) && polling) return oldPoll.promise;
      if (path.includes(`/definitions/${b}/`)) return response([bView]);
      if (path.endsWith(bView.workflow_session_id)) return response(bView);
      if (path.endsWith("/sessions")) return response([current]);
      return response(current);
    }));
    workspace.openWorkflow(a); await graph.activate(a); polling = true;
    await vi.advanceTimersByTimeAsync(350);
    workspace.openWorkflow(b); await graph.activate(b); paths.length = 0;
    oldPoll.resolve(response([current]));
    await vi.advanceTimersByTimeAsync(0);
    paths.length = 0;
    await vi.advanceTimersByTimeAsync(350);
    expect(paths).toEqual([`/api/graph/definitions/${b}/sessions`, `/api/graph/sessions/${bView.workflow_session_id}`]);
  });

  it("keeps session rows current when the detail read settles after the directory read", async () => {
    const { graph, id, current } = setup("running");
    const other = view(graph.document!);
    graph.sessions[id] = [current, other];
    const completed = { ...current, status: "succeeded", revision: 2, can_submit: true };
    stubGraphApplicationFetch(vi.fn(async path => response(path.endsWith("/sessions")
      ? [current, other] : completed)));
    await graph.refresh(id);
    expect(graph.session).toEqual(completed);
    expect(graph.sessions[id]).toEqual([completed, other]);
    expect(graph.sessions[id][0]).toBe(graph.session);
    stubGraphApplicationFetch(vi.fn(async path => response(path.endsWith("/sessions")
      ? [current, other] : current)));
    await graph.refresh(id);
    expect(graph.sessions[id][0]).toEqual(completed);
    expect(graph.sessions[id][0]).toBe(graph.session);
  });

  it("reads final run permissions once after the active poll reaches a settled status", async () => {
    const { graph, id, current } = setup("running");
    let terminating = false, detailReads = 0;
    const paths: string[] = [];
    const early = { ...current, status: "failed", revision: 2, available_actions: ["close"] };
    const settled = { ...early, revision: 3, available_actions: ["retry_failed_node", "close"] };
    stubGraphApplicationFetch(vi.fn(async path => {
      paths.push(path);
      if (path.endsWith("/sessions")) return response([current]);
      return response(terminating ? ++detailReads === 1 ? early : settled : current);
    }));
    await graph.activate(id);
    paths.length = 0; terminating = true;
    await vi.advanceTimersByTimeAsync(350);
    expect(paths).toEqual([
      `/api/graph/definitions/${id}/sessions`, `/api/graph/sessions/${current.workflow_session_id}`,
      `/api/graph/definitions/${id}/sessions`, `/api/graph/sessions/${current.workflow_session_id}`,
    ]);
    expect(graph.session).toEqual(settled);
    expect(graph.sessions[id][0]).toBe(graph.session);
    expect(graph.session?.available_actions).toContain("retry_failed_node");
    await vi.advanceTimersByTimeAsync(1050);
    expect(detailReads).toBe(2);
    expect(paths).toHaveLength(4);
  });

  it("explains a stale prepared pause without resending the rejected control", async () => {
    const { graph } = setup("prepared");
    const fetcher = vi.fn(async (_path: string, _init: { body: string }) => new Response(JSON.stringify({ error: {
      reason_code: "stale_revision", diagnostic: { reason_code: "stale_revision", message: "Session changed" },
    } }), { status: 409 }));
    stubGraphApplicationFetch(fetcher);
    await graph.submitPrimary();
    const message = "会话状态已变化，暂停未提交；请刷新运行后重试 [stale_revision]";
    expect(graph.primaryAction).toBe("pause");
    expect(graph.pending).toBeNull();
    expect(graph.active?.diagnostics).toEqual([{ reason_code: "stale_revision", message }]);
    expect(useWorkbenchNoticesStore().error?.reason).toBe(message);
    await vi.advanceTimersByTimeAsync(1050);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetcher.mock.calls[0][1].body).action).toBe("pause");
  });
});

describe("duplicated graph context references", () => {
  it("remaps declared plugin bindings while retaining references to nodes outside the selection", () => {
    const { graph, id } = setup(); graph.entries[id].session_id = null;
    const agentType: GraphNodeType = { ...textType, component_id: "agents.execute", component_version: "2" };
    const contextType: GraphNodeType = { ...textType, component_id: "plugin.bound-context",
      config_node_references: [
        { config_field: "agent_node_id", multiple: false,
          target_types: [{ component_id: "agents.execute", component_version: "2" }] },
        { config_field: "related_node_ids", multiple: true,
          target_types: [{ component_id: "agents.execute", component_version: "2" }] },
      ] };
    graph.catalog = [agentType, contextType];
    const agent = graph.addNode("agents.execute@2", { x: 0, y: 0 })!;
    const external = graph.addNode("agents.execute@2", { x: 0, y: 100 })!;
    const context = graph.addNode("plugin.bound-context@1", { x: 100, y: 0 })!;
    graph.patchNode(context, { config: { agent_node_id: agent, related_node_ids: [agent, external],
      unrelated_text: agent } });
    const original = graphClone(graph.document!);
    graph.selectedNodeIds = [agent, context]; graph.duplicateSelection();
    const [copiedAgent, copiedContext] = graph.document!.nodes.slice(3);
    expect(copiedContext.config).toEqual({ agent_node_id: copiedAgent.node_binding_id,
      related_node_ids: [copiedAgent.node_binding_id, external], unrelated_text: agent });
    expect(graph.document!.nodes.slice(0, 3)).toEqual(original.nodes);
    graph.undo(); expect(graph.document).toEqual(original);
  });

  it("remaps declared plugin sources and edges as one undoable transaction", () => {
    const { graph, id } = setup(); graph.entries[id].session_id = null;
    const doc = newGraph("references");
    const agent = crypto.randomUUID(), external = crypto.randomUUID();
    const node = (component_id: string, source_node_id?: string) => ({
      node_binding_id: crypto.randomUUID(), component_id, component_version: "1", title: component_id,
      position: { x: 1, y: 2 }, config: source_node_id ? { source_node_id } : {},
    });
    graph.catalog.push({ ...textType, component_id: "plugin.context",
      config_node_references: [{ config_field: "source_node_id", multiple: false, target_types: [] }] });
    const context = node("plugin.context", agent), outside = node("plugin.context", external);
    const plugin = node("external.plugin", agent);
    doc.workflow_definition_id = id;
    doc.nodes = [{ ...node("plugin.agent"), component_version: "2", node_binding_id: agent }, context, outside, plugin];
    doc.edges = [{ edge_id: crypto.randomUUID(), source_node_id: agent, source_port_id: "context",
      target_node_id: context.node_binding_id, target_port_id: "context", order: 0 }];
    graph.entries[id].document = doc;
    const original = graphClone(doc);
    graph.selectedNodeIds = doc.nodes.map(row => row.node_binding_id);
    graph.duplicateSelection();
    const copies = graph.document!.nodes.slice(4);
    expect(copies[1].config.source_node_id).toBe(copies[0].node_binding_id);
    expect(copies[2].config.source_node_id).toBe(external);
    expect(copies[3].config.source_node_id).toBe(agent);
    expect(graph.document!.edges[1]).toMatchObject({
      source_node_id: copies[0].node_binding_id, target_node_id: copies[1].node_binding_id,
    });
    expect(doc).toEqual(original); expect(graph.document!.nodes.slice(0, 4)).toEqual(original.nodes);
    graph.undo(); expect(graph.document).toEqual(original);
    graph.undo(true); expect(graph.document!.nodes).toHaveLength(8);
    graph.selectedNodeIds = [agent]; graph.removeSelection();
    expect(graph.document!.nodes.find(n => n.node_binding_id === copies[1].node_binding_id)?.config.source_node_id)
      .toBe(copies[0].node_binding_id);
    expect(graph.document!.edges).toEqual([expect.objectContaining({
      source_node_id: copies[0].node_binding_id, target_node_id: copies[1].node_binding_id,
    })]);
  });
});
