import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, isGraphDocument, type GraphDocument, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import { debtCleanupDerivationResult, installDebtCleanupConfiguration } from "../testUtils/debtCleanupConfiguration";
import type { GraphConfigurationResult } from "../domain/workflowConfiguration";
import { WorkbenchApiError } from "../adapters/workbenchApi";
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
async function fixture() {
  const graph = useWorkflowGraphStore(); graph.catalog = [source,sink];
  const id = await graph.createWorkflow();
  await graph.reconcile(id);
  const text = await graph.addNode("workflow.text@1", { x: 0, y: 0 });
  const output = await graph.addNode("workflow.output@1", { x: 300, y: 0 });
  await graph.connect({ source_node_id: text!, source_port_id: "output", target_node_id: output!, target_port_id: "input" });
  return { graph, id, text: text!, output: output! };
}
function session(document: GraphDocument, sid = crypto.randomUUID()): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: sid,
    workflow_definition_id: document.workflow_definition_id, definition_revision: document.revision,
    revision: 1, data_revision: 0, head_revision: 1, status: "idle", can_submit: true,
    nodes: document.nodes.map(n => ({ node_binding_id: n.node_binding_id, label: n.title, status: "idle",
      run_id: null, revision: null, outputs: {}, diagnostic: null })), chains: [], outputs: [], data: { revision: 0, values: {} } };
}
beforeEach(() => { setActivePinia(createPinia()); installDebtCleanupEvidence(); });
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe("generic workbench execution and document transactions", () => {
  it("resets selection when creating blank drafts and keeps empty/populated switches on the same document contract", async () => {
    const { graph, id, text, output } = await fixture(), workspace = useWorkspaceStore();
    const populated = graphClone(graph.document!);
    graph.executionPackageLock = [{ package_id: "plugin", version: "1.0.0" }];
    graph.selectedNodeIds = [text, output]; graph.selectedEdgeId = populated.edges[0].edge_id;
    const blankId = await graph.createWorkflow(), blank = graphClone(graph.document!);
    await graph.reconcile(blankId);
    expect(blank.schema_version).toBe(populated.schema_version);
    expect(Object.keys(blank).sort()).toEqual(Object.keys(populated).sort());
    expect(blank.package_lock).toEqual([{ package_id: "plugin", version: "1.0.0" }]);
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
    expect(graph.history[blankId]).toEqual({ past: [], future: [] });
    expect(graph.entries[blankId]).toMatchObject({ saved_revision: 0, session_id: null });
    expect(graph.entries[id].document).toEqual(populated);
    const added = (await graph.addNode("workflow.text@1", { x: 20, y: 30 }))!;
    expect(graph.selectedNodeIds).toEqual([added]); expect(isGraphDocument(graph.document)).toBe(true);
    await graph.removeSelection();
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
  it("creates only blank independent ordinary drafts without exposing a provided-document preset entry", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const id = await graph.createWorkflow(), before = graphClone(graph.entries[id]);
    graph.selectedNodeIds = [crypto.randomUUID()]; graph.selectedEdgeId = crypto.randomUUID();
    const nextId = await graph.createWorkflow();
    expect(workspace.activeWorkflowId).toBe(nextId);
    expect(workspace.activeWorkflow).toMatchObject({ state: "draft", nodeCount: 0, description: "" });
    expect(graph.entries[nextId]).toMatchObject({ saved_revision: 0, session_id: null,
      document: { nodes: [], edges: [], object_bindings: [] } });
    expect(graph.history[nextId]).toEqual({ past: [], future: [] });
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
    expect(graph.entries[id]).toEqual(before);
    expect("createWorkflowDraft" in graph).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("opens frozen v1 definitions without adding package locks or restoring an editor snapshot", async () => {
    const { graph, id } = await fixture(), workspace = useWorkspaceStore();
    const old: GraphDocument = { schema_version: 1, workflow_definition_id: id, revision: 7,
      name: "Legacy frozen", nodes: graphClone(graph.document!.nodes), edges: graphClone(graph.document!.edges) };
    const snapshot = graph.storeSnapshot();
    snapshot.entries[id] = { document: graphClone(old), saved_document: graphClone(old),
      saved_revision: 7, session_id: null };
    workspace.markWorkflowSaved(id, old.name);
    graph.executionPackageLock = [{ package_id: "plugin", version: "2.0.0" }];
    graph.entries[id] = graphClone(snapshot.entries[id]);
    stubGraphApplicationFetch(vi.fn(async () => new Response(JSON.stringify([]))));
    await graph.activate(id);
    expect(graph.entries[id].document).toEqual(old); expect(graph.entries[id].saved_document).toEqual(old);
    expect(graph.document!.schema_version).toBe(1); expect(graph.document!.package_lock).toBeUndefined();
    expect(graph.storeSnapshot().entries[id]).toEqual(snapshot.entries[id]);
  });
  it("keeps an explicitly empty execution lock usable when the catalog arrives after blank creation", async () => {
    const graph = useWorkflowGraphStore(), evidence = installDebtCleanupEvidence();
    const configuration = installDebtCleanupConfiguration(graph, evidence);
    const id = await graph.createWorkflow();
    await configuration.settled(id);
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
    const text = (await graph.addNode("workflow.text@1", { x: 0, y: 0 }))!;
    await configuration.settled(id);
    const output = (await graph.addNode("workflow.output@1", { x: 300, y: 0 }))!;
    await configuration.settled(id);
    await graph.connect({ source_node_id: text, source_port_id: "output", target_node_id: output, target_port_id: "input" });
    await configuration.settled(id);
    saved = graphClone(graph.document!);
    await graph.submitPrimary();
    expect(saved).toMatchObject({ schema_version: 3, package_lock: [], control_edges: [] });
    expect(saved!.nodes).toHaveLength(2); expect(saved!.edges).toHaveLength(1);
    expect(isGraphDocument(saved)).toBe(true); expect(graph.session!.status).toBe("succeeded");
  });
  it("uses a registered independent plugin without a frontend kind branch", async () => {
    const { graph } = await fixture();
    graph.catalog.push({ ...source, component_id: "independent.upper", display_name: "大写" });
    const id = (await graph.addNode("independent.upper@1", { x: 10, y: 20 }))!;
    expect(graph.document?.nodes.find(n => n.node_binding_id === id)?.component_id).toBe("independent.upper");
    graph.selectedNodeIds = [id]; await graph.removeSelection(); await graph.undo();
    expect(graph.document?.nodes.some(n => n.node_binding_id === id)).toBe(true);
  });
  it("moves different node types as one transaction and preserves edges during mode edits", async () => {
    const { graph, text, output } = await fixture();
    await graph.moveNodes([{ id: text, position: { x: 22, y: 33 } }, { id: output, position: { x: 444, y: 555 } }]);
    await graph.undo();
    expect(graph.document?.nodes.map(n => n.position)).toEqual([{ x:0,y:0 },{ x:300,y:0 }]);
    await graph.undo(true);
    expect(graph.document?.nodes[1].position).toEqual({ x:444,y:555 });
    const edges = graphClone(graph.document?.edges);
    await graph.patchNode(output, { config: { mode: "prompt" } }); expect(graph.document?.edges).toEqual(edges);
  });
  it("keeps repeated wiring a no-op and duplicates the selected nodes and their edge as one undoable edit", async () => {
    const { graph, text, output } = await fixture();
    const original = graphClone(graph.document!), undoCount = graph.history[useWorkspaceStore().activeWorkflowId].past.length;
    expect(await graph.connect({ source_node_id: text, source_port_id: "output",
      target_node_id: output, target_port_id: "input" })).toMatchObject({ outcome: "no-change" });
    expect(graph.history[useWorkspaceStore().activeWorkflowId].past).toHaveLength(undoCount);
    graph.selectedNodeIds = [text, output]; await graph.duplicateSelection();
    const copies = graph.document!.nodes.slice(2);
    expect(graph.document!.edges[1]).toMatchObject({ source_node_id: copies[0].node_binding_id,
      target_node_id: copies[1].node_binding_id, order: 0 });
    expect(new Set(graph.document!.edges.map(edge => edge.edge_id)).size).toBe(2);
    await graph.undo(); expect(graph.document).toEqual(original);
    await graph.undo(true); expect(graph.document!.nodes).toHaveLength(4);
    graph.selectedNodeIds = [copies[0].node_binding_id]; await graph.removeSelection();
    expect(graph.document!.edges).toEqual(original.edges);
  });
  it.each(["single-source", "schema", "missing-port", "combined-cycle"])(
    "rejects %s connections without changing the graph, undo history or persisted state", async issue => {
      const { graph, id, text, output } = await fixture();
      const other = (await graph.addNode("workflow.text@1", { x: 0, y: 200 }))!;
      graph.catalog = graphClone([source, sink]);
      if (issue === "schema") graph.catalog[1]!.inputs[0]!.data_schema_version = 2;
      if (issue === "combined-cycle") {
        graph.catalog[1]!.inputs[0]!.multiple = true;
        graph.document!.control_edges = [{ edge_id: crypto.randomUUID(), source_node_id: output, target_node_id: other }];
      }
      await graph.patchNode(text, { title: "Undoable title" }); await graph.undo();
      const document = graphClone(graph.document!), history = graphClone(graph.history[id]);
      const persist = vi.fn(() => true), fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
      expect(await graph.connect({ source_node_id: other, source_port_id: issue === "missing-port" ? "missing" : "output",
        target_node_id: output, target_port_id: "input" })).toMatchObject({ outcome: "no-change" });
      expect(graph.document).toEqual(document); expect(graph.history[id]).toEqual(history);
      expect(useWorkspaceStore().activeWorkflowId).toBe(id);
      expect(persist).not.toHaveBeenCalled(); expect(fetcher).not.toHaveBeenCalled();
      expect(useWorkbenchNoticesStore().error).toMatchObject({ kind: "rejected" });
    });
  it("saves exact definitions and starts with empty external inputs and no legacy endpoints", async () => {
    const graph = useWorkflowGraphStore(), configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    graph.catalog = [source, sink];
    const workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    const text = (await graph.addNode("workflow.text@1", { x: 0, y: 0 }))!;
    await configuration.settled(workflowId);
    const output = (await graph.addNode("workflow.output@1", { x: 300, y: 0 }))!;
    await configuration.settled(workflowId);
    await graph.connect({ source_node_id: text, source_port_id: "output", target_node_id: output, target_port_id: "input" });
    await configuration.settled(workflowId);
    const document = graphClone(graph.document!); let view: GraphSession;
    const calls: { path:string; body:Record<string,unknown> }[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, options) => {
      const body = JSON.parse(options.body); calls.push({ path, body });
      if (path === "/api/graph/sessions") { view = session(document!); return new Response(JSON.stringify(view)); }
      view = { ...view!, status: "succeeded", revision: 2 }; return new Response(JSON.stringify(view));
    }));
    await graph.submitPrimary();
    expect(calls.map(call => call.path)).toEqual(["/api/graph/sessions", `/api/graph/sessions/${graph.session?.workflow_session_id}/runs`]);
    expect(calls[1].body.inputs).toEqual({}); expect(graph.session?.status).toBe("succeeded");
    expect(configuration.update.mock.calls.at(-1)?.[0].document.nodes).toEqual(document.nodes);
    expect(configuration.update.mock.calls.at(-1)?.[0].document.edges).toEqual(document.edges);
    expect(configuration.application.document(workflowId)).toEqual(document);
    expect(useWorkspaceStore().activeWorkflow.state).toBe("saved");
  });
  it("derives fixed node edits without changing the old session and runs only the fresh target", async () => {
    const { graph, id, text } = await fixture(), workspace = useWorkspaceStore();
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    await configuration.application.forWorkflow(id).initialize();
    await graph.renameWorkflow("Formal source");
    await configuration.settled(id);
    const source = graphClone(graph.document!), view = session(source), targetId = crypto.randomUUID();
    graph.entries[id].session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    configuration.update.mockImplementation(async request => debtCleanupDerivationResult(request, targetId));
    const confirm = vi.fn(() => true);
    vi.stubGlobal("window", { confirm });
    const edited = await graph.patchNode(text, { config: { text: "second edit" } });
    expect(edited).toMatchObject({ outcome: "confirmation-required" });
    expect(graph.activeDerivationAsk?.request.document.nodes.find(node =>
      node.node_binding_id === text)?.config.text).toBe("second edit");
    expect(confirm).not.toHaveBeenCalled();
    expect(await graph.confirmDerivation()).toBe(true);
    await vi.waitFor(() => expect(workspace.activeWorkflowId).toBe(targetId));
    await graph.reconcile(targetId);
    const target = graphClone(graph.document!), created = session(target);
    expect(graph.entries[id].document).toEqual(source);
    expect(graph.views[view.workflow_session_id]).toEqual(view);
    expect(graph.entries[targetId].session_id).toBeNull();
    expect(target.workflow_definition_id).not.toBe(source.workflow_definition_id);
    const calls: string[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      calls.push(path);
      if (path === "/api/graph/sessions") {
        expect(JSON.parse(init.body).workflow_definition_id).toBe(targetId);
        return new Response(JSON.stringify(created));
      }
      if (path.endsWith("/runs")) return new Response(JSON.stringify({ ...created, revision: 2, status: "succeeded" }));
      if (path.endsWith(view.workflow_session_id)) return new Response(JSON.stringify(view));
      if (path.endsWith(created.workflow_session_id)) return new Response(JSON.stringify(created));
      return new Response(JSON.stringify(view));
    }));
    expect(await graph.createSession()).toBe(true);
    expect(calls.at(-1)).toBe("/api/graph/sessions");
    expect(graph.session!.workflow_session_id).toBe(created.workflow_session_id);
    expect(graph.session!.definition_revision).toBe(target.revision);
    calls.length = 0;
    await graph.submitPrimary();
    expect(calls).toEqual([`/api/graph/sessions/${created.workflow_session_id}`,
      `/api/graph/sessions/${created.workflow_session_id}/runs`]);
    expect(calls.some(path => path.endsWith("/rebind"))).toBe(false);
    expect(graph.views[view.workflow_session_id]).toEqual(view);
  });
  it("keeps current rejection notices and blocks configuration updates before evidence is stored", async () => {
    const graph = useWorkflowGraphStore(), evidence = installDebtCleanupEvidence();
    const configuration = installDebtCleanupConfiguration(graph, evidence), workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    const original = graphClone(graph.document!);
    vi.mocked(evidence.ports.write).mockRejectedValueOnce(
      new WorkbenchApiError("rejected", "Original evidence unavailable", 503, "evidence_storage_failed"));
    const renamed = await graph.renameWorkflow("unsent modification");
    expect(renamed).toMatchObject({ outcome: "failed" });
    await vi.waitFor(() => expect(useWorkbenchNoticesStore().error?.reason).toContain("Original evidence unavailable"));
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    expect(configuration.update).toHaveBeenCalledOnce();
    expect(graph.document).toEqual(original);
    expect(configuration.application.forWorkflow(workflowId).pending.value).toBeNull();
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ error: {
      code: "graph_no_outputs", diagnostic: { reason_code: "graph_no_outputs", message: "No connected output" },
    } }), { status: 400 }));
    stubGraphApplicationFetch(fetcher);
    expect(await graph.createSession()).toBe(false);
    expect(fetcher).toHaveBeenCalledOnce();
    expect(useWorkbenchNoticesStore().error?.reason).toContain("graph_no_outputs");
    expect(useWorkbenchNoticesStore().error?.kind).toBe("rejected");
    expect(graph.document).toEqual(original);
  });
  it("accepts the closed control receipt and allows a later fresh run", async () => {
    const { graph,id } = await fixture(); const view = session(graph.document!);
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

describe("mutually exclusive selection, delete targets and existence pruning (Q04-3/Q04-4)", () => {
  async function appFixture() {
    const graph = useWorkflowGraphStore(); graph.catalog = [source, sink];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const id = await graph.createWorkflow();
    await configuration.settled(id);
    const text = (await graph.addNode("workflow.text@1", { x: 0, y: 0 }))!;
    await configuration.settled(id);
    const output = (await graph.addNode("workflow.output@1", { x: 300, y: 0 }))!;
    await configuration.settled(id);
    await graph.connect({ source_node_id: text, source_port_id: "output",
      target_node_id: output, target_port_id: "input" });
    await configuration.settled(id);
    return { graph, configuration, id, text, output };
  }
  it("maintains node multiselect and single-edge exclusivity through the store actions", async () => {
    const { graph, text, output } = await fixture();
    const edgeId = graph.document!.edges[0]!.edge_id;
    graph.selectEdge(edgeId);
    expect(graph.selectedEdgeId).toBe(edgeId);
    expect(graph.selectedNodeIds).toEqual([]);
    graph.selectNodes([text, crypto.randomUUID(), output, text]);
    expect(graph.selectedNodeIds).toEqual([text, output]);
    expect(graph.selectedEdgeId).toBeNull();
    graph.selectEdge(null);
    expect(graph.selectedEdgeId).toBeNull();
    expect(graph.selectedNodeIds).toEqual([]);
    // An unknown edge identity never leaves a node selection behind.
    graph.selectNodes([text]);
    graph.selectEdge(crypto.randomUUID());
    expect(graph.selectedNodeIds).toEqual([]);
    expect(graph.selectedEdgeId).toBeNull();
    graph.clearSelection();
    expect(graph.selectedNodeIds).toEqual([]);
    expect(graph.selectedEdgeId).toBeNull();
  });
  it("clears an old edge selection when a node is added or replaced", async () => {
    const { graph } = await fixture();
    graph.selectEdge(graph.document!.edges[0]!.edge_id);
    const added = (await graph.addNode("workflow.output@1", { x: 600, y: 0 }))!;
    expect(graph.selectedNodeIds).toEqual([added]);
    expect(graph.selectedEdgeId).toBeNull();
    const { graph: second, text } = await fixture();
    second.selectEdge(second.document!.edges[0]!.edge_id);
    expect(await second.replaceNode(text, "workflow.output@1")).toMatchObject({ outcome: "accepted" });
    expect(second.selectedNodeIds).toEqual([text]);
    expect(second.selectedEdgeId).toBeNull();
  });
  it("deletes selected nodes with their related edges but never an unrelated stale edge", async () => {
    const { graph, text, output } = await fixture();
    const text2 = (await graph.addNode("workflow.text@1", { x: 0, y: 200 }))!;
    const output2 = (await graph.addNode("workflow.output@1", { x: 300, y: 200 }))!;
    await graph.connect({ source_node_id: text2, source_port_id: "output",
      target_node_id: output2, target_port_id: "input" });
    const unrelated = graph.document!.edges.find(edge => edge.source_node_id === text2)!.edge_id;
    // Legacy direct writers could leave both selectors populated; node mode wins and the
    // unrelated old edge identity must not enter the deletion range.
    graph.selectedNodeIds = [text];
    graph.selectedEdgeId = unrelated;
    expect(await graph.removeSelection()).toMatchObject({ outcome: "accepted" });
    expect(graph.document!.nodes.map(node => node.node_binding_id)).toEqual([output, text2, output2]);
    expect(graph.document!.edges.map(edge => edge.edge_id)).toEqual([unrelated]);
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
  });
  it("deletes exactly one selected edge without touching nodes", async () => {
    const { graph, text, output } = await fixture();
    const edgeId = graph.document!.edges[0]!.edge_id;
    graph.selectEdge(edgeId);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "accepted" });
    expect(graph.document!.edges).toEqual([]);
    expect(graph.document!.nodes.map(node => node.node_binding_id)).toEqual([text, output]);
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
  });
  it("submits nothing when every selected identity is already stale", async () => {
    const { graph, configuration, id } = await appFixture();
    const calls = configuration.update.mock.calls.length, before = graphClone(graph.document!);
    graph.selectedNodeIds = [crypto.randomUUID()];
    graph.selectedEdgeId = crypto.randomUUID();
    expect(await graph.removeSelection()).toEqual({ outcome: "no-change", workflowId: id });
    expect(configuration.update.mock.calls.length).toBe(calls);
    expect(graph.document).toEqual(before);
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
  });
  it("keeps the still-valid selection after a definite delete failure", async () => {
    const { graph, configuration, id, output } = await appFixture();
    graph.selectNodes([output]);
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError(
      "rejected", "工作流正在修改，请等待当前请求结束后按原基线重试", 409, "workflow_modification_busy"));
    expect(await graph.removeSelection()).toMatchObject({ outcome: "failed" });
    expect(graph.selectedNodeIds).toEqual([output]);
    expect(graph.selectedEdgeId).toBeNull();
    expect(graph.document!.nodes.some(node => node.node_binding_id === output)).toBe(true);
    expect(useWorkspaceStore().activeWorkflowId).toBe(id);
  });
  it("prunes deleted identities after the formal document accepts the deletion", async () => {
    const { graph, text, output } = await appFixture();
    graph.selectNodes([output]);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "accepted" });
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
    expect(graph.document!.nodes.map(node => node.node_binding_id)).toEqual([text]);
    expect(graph.document!.edges).toEqual([]);
  });
  it("prunes after undo/redo without restoring the historical selection", async () => {
    const { graph, text } = await fixture();
    graph.selectNodes([text]);
    const added = (await graph.addNode("workflow.text@1", { x: 0, y: 200 }))!;
    expect(graph.selectedNodeIds).toEqual([added]);
    expect(await graph.undo()).toMatchObject({ outcome: "accepted" });
    expect(graph.document!.nodes.some(node => node.node_binding_id === added)).toBe(false);
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
    expect(await graph.undo(true)).toMatchObject({ outcome: "accepted" });
    expect(graph.document!.nodes.some(node => node.node_binding_id === added)).toBe(true);
    // Redo restores the document, never the selection that existed before the undo.
    expect(graph.selectedNodeIds).toEqual([]); expect(graph.selectedEdgeId).toBeNull();
  });
  it("clears on workflow switch while a late accepted result cannot clear the new selection", async () => {
    const graph = useWorkflowGraphStore(); graph.catalog = [source, sink];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const first = await graph.createWorkflow();
    await configuration.settled(first);
    const textA = (await graph.addNode("workflow.text@1", { x: 0, y: 0 }))!;
    await configuration.settled(first);
    const defaultUpdate = configuration.update.getMockImplementation()!;
    const gate = deferred<GraphConfigurationResult>();
    configuration.update.mockImplementationOnce(() => gate.promise);
    const saving = graph.patchNode(textA, { title: "late edit" });
    await vi.waitFor(() => expect(configuration.update).toHaveBeenCalledTimes(3));
    const request = configuration.update.mock.calls.at(-1)![0];
    const second = await graph.createWorkflow();
    await configuration.settled(second);
    const textB = (await graph.addNode("workflow.text@1", { x: 0, y: 0 }))!;
    await configuration.settled(second);
    expect(useWorkspaceStore().activeWorkflowId).toBe(second);
    expect(graph.selectedNodeIds).toEqual([textB]);
    gate.resolve(await defaultUpdate(request));
    expect(await saving).toMatchObject({ outcome: "accepted" });
    expect(useWorkspaceStore().activeWorkflowId).toBe(second);
    expect(graph.selectedNodeIds).toEqual([textB]); expect(graph.selectedEdgeId).toBeNull();
  });
  it("does not replace the new workflow selection when a late node add is accepted", async () => {
    const graph = useWorkflowGraphStore(); graph.catalog = [source, sink];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const first = await graph.createWorkflow();
    await configuration.settled(first);
    const defaultUpdate = configuration.update.getMockImplementation()!;
    const gate = deferred<GraphConfigurationResult>();
    configuration.update.mockImplementationOnce(() => gate.promise);
    const savingAdd = graph.addNode("workflow.text@1", { x: 0, y: 100 });
    await vi.waitFor(() => expect(configuration.update).toHaveBeenCalledTimes(2));
    const request = configuration.update.mock.calls.at(-1)![0];
    const second = await graph.createWorkflow();
    await configuration.settled(second);
    const textB = (await graph.addNode("workflow.text@1", { x: 0, y: 0 }))!;
    await configuration.settled(second);
    graph.selectNodes([textB]);
    gate.resolve(await defaultUpdate(request));
    await expect(savingAdd).resolves.toEqual(expect.any(String));
    expect(useWorkspaceStore().activeWorkflowId).toBe(second);
    expect(graph.selectedNodeIds).toEqual([textB]);
    expect(graph.selectedEdgeId).toBeNull();
  });
});

describe("declared-reference delete/replace blocking and early dangling-edge rejection (S3-03 first batch)", () => {
  const referenceTarget: GraphNodeType = { component_id: "test.reference-target", component_version: "1",
    display_name: "引用目标", category: "Test", default_config: {}, config_schema: { type: "object", properties: {} },
    inputs: [], outputs: [], executable: true, is_output: false };
  const referenceTargetV2: GraphNodeType = { ...graphClone(referenceTarget), component_version: "2" };
  const referenceConsumer: GraphNodeType = { component_id: "test.reference-consumer", component_version: "1",
    display_name: "引用者", category: "Test", default_config: { target_id: "", related_ids: [] },
    config_schema: { type: "object", properties: { target_id: { type: "string" }, related_ids: { type: "array" } } },
    inputs: [], outputs: [], executable: true, is_output: false,
    config_node_references: [
      { config_field: "target_id", multiple: false, allow_empty: true, target_types: [] },
      { config_field: "related_ids", multiple: true, target_types: [] },
    ] };
  const portSource: GraphNodeType = { component_id: "test.port-source", component_version: "1",
    display_name: "端口源", category: "Test", default_config: {}, config_schema: { type: "object", properties: {} },
    inputs: [], outputs: [{ port_id: "out", data_type: "TEXT", data_schema_version: 2, required: true, multiple: false }],
    executable: true, is_output: false };
  const portSink: GraphNodeType = { component_id: "test.port-sink", component_version: "1",
    display_name: "端口目标", category: "Test", default_config: {}, config_schema: { type: "object", properties: {} },
    inputs: [{ port_id: "in", data_type: "TEXT", data_schema_version: 2, required: true, multiple: false }],
    outputs: [], executable: true, is_output: false };
  const portSinkNext: GraphNodeType = { ...graphClone(portSink), component_id: "test.port-sink-next" };
  const portSinkMissing: GraphNodeType = { ...graphClone(portSink), component_id: "test.port-sink-missing",
    inputs: [{ port_id: "other", data_type: "TEXT", data_schema_version: 2, required: true, multiple: false }] };
  const multiSink: GraphNodeType = { ...graphClone(portSink), component_id: "test.multi-sink",
    inputs: [{ port_id: "in", data_type: "TEXT", data_schema_version: 2, required: true, multiple: true }] };
  const singleSinkNext: GraphNodeType = { ...graphClone(portSink), component_id: "test.single-sink-next" };
  const objectProvider: GraphNodeType = { component_id: "test.object-provider", component_version: "1",
    display_name: "对象提供者", category: "Test", default_config: { object_key: "" },
    config_schema: { type: "object", properties: { object_key: { type: "string" } } }, inputs: [], outputs: [],
    executable: true, is_output: false,
    provided_session_item_types: [{ type_id: "test.item", schema_version: 1 }],
    object_accesses: [{ config_field: "object_key", multiple: false, access: "read_write",
      type_id: "test.item", schema_version: 1 }] };
  const objectProviderNext: GraphNodeType = { ...graphClone(objectProvider), component_version: "2" };
  const objectProviderUnrelated: GraphNodeType = { ...graphClone(objectProvider), component_id: "test.object-other",
    provided_session_item_types: [{ type_id: "test.other-item", schema_version: 1 }],
    object_accesses: [{ config_field: "object_key", multiple: false, access: "read_write",
      type_id: "test.other-item", schema_version: 1 }] };
  const objectConsumer: GraphNodeType = { component_id: "test.object-consumer", component_version: "1",
    display_name: "对象使用方", category: "Test", default_config: { object_key: "" },
    config_schema: { type: "object", properties: { object_key: { type: "string" } } }, inputs: [], outputs: [],
    executable: true, is_output: false,
    object_accesses: [{ config_field: "object_key", multiple: false, access: "read",
      type_id: "test.item", schema_version: 1 }] };
  async function configured(types: GraphNodeType[]) {
    const graph = useWorkflowGraphStore(); graph.catalog = types.map(type => graphClone(type));
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const id = await graph.createWorkflow();
    await configuration.settled(id);
    const add = async (component: string, x: number) => {
      const nodeId = (await graph.addNode(component, { x, y: 0 }))!;
      await configuration.settled(id);
      return nodeId;
    };
    return { graph, configuration, id, add };
  }
  it("blocks deleting a node that a surviving node still declares a reference to", async () => {
    const { graph, configuration, id, add } =
      await configured([referenceTarget, referenceConsumer]);
    const target = await add("test.reference-target@1", 0);
    const consumer = await add("test.reference-consumer@1", 100);
    expect(await graph.patchNode(consumer, { config: { target_id: target, related_ids: [target] } }))
      .toMatchObject({ outcome: "accepted" });
    const calls = configuration.update.mock.calls.length;
    graph.selectNodes([target]);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "failed" });
    expect(configuration.update.mock.calls.length).toBe(calls);
    expect(graph.activeEditBlock).toMatchObject({ workflowId: id, action: "delete-selection", targetIds: [target] });
    expect(graph.activeEditBlock!.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "node-reference", nodeId: consumer, nodeTitle: "引用者",
        configField: "target_id", targetNodeId: target }),
      expect.objectContaining({ kind: "node-reference", nodeId: consumer, configField: "related_ids",
        path: expect.stringContaining("/related_ids/0") }),
    ]));
    expect(graph.selectedNodeIds).toEqual([target]);
    expect(graph.document!.nodes).toHaveLength(2);
  });
  it("clears the block after the reference is fixed and allows deleting a co-selected referrer", async () => {
    const { graph, add } = await configured([referenceTarget, referenceConsumer]);
    const target = await add("test.reference-target@1", 0);
    const consumer = await add("test.reference-consumer@1", 100);
    await graph.patchNode(consumer, { config: { target_id: target, related_ids: [target] } });
    graph.selectNodes([target]);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "failed" });
    expect(graph.activeEditBlock).not.toBeNull();
    // Fixing the declared value (a normal accepted edit) retires the stale block.
    expect(await graph.patchNode(consumer, { config: { target_id: "", related_ids: [] } }))
      .toMatchObject({ outcome: "accepted" });
    expect(graph.activeEditBlock).toBeNull();
    graph.selectNodes([target]);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "accepted" });
    expect(graph.document!.nodes.map(node => node.node_binding_id)).toEqual([consumer]);
    // Same batch: selecting the referrer too never needs a fix first.
    const secondTarget = await add("test.reference-target@1", 200);
    expect(await graph.patchNode(consumer, { config: { target_id: secondTarget, related_ids: [] } }))
      .toMatchObject({ outcome: "accepted" });
    graph.selectNodes([consumer, secondTarget]);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "accepted" });
    expect(graph.document!.nodes).toEqual([]);
    expect(graph.activeEditBlock).toBeNull();
  });
  it("blocks deleting a provider whose object key is still used by a surviving consumer", async () => {
    const { graph, add } = await configured([objectProvider, objectConsumer]);
    const provider = await add("test.object-provider@1", 0);
    const consumer = await add("test.object-consumer@1", 100);
    const binding = graph.document!.object_bindings!.find(row => row.registrar_node_id === provider)!;
    expect(binding).toBeDefined();
    expect(await graph.patchNode(consumer, { config: { object_key: binding.object_key } }))
      .toMatchObject({ outcome: "accepted" });
    graph.selectNodes([provider]);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "failed" });
    expect(graph.activeEditBlock!.items).toEqual([expect.objectContaining({ kind: "object-access",
      nodeId: consumer, configField: "object_key", objectKey: binding.object_key, targetNodeId: provider })]);
    graph.selectNodes([provider, consumer]);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "accepted" });
    expect(graph.document!.nodes).toEqual([]); expect(graph.document!.object_bindings).toEqual([]);
    expect(graph.activeEditBlock).toBeNull();
  });
  it("blocks an incompatible replacement with concrete edges but keeps a compatible one untouched", async () => {
    const { graph, configuration, id, add } =
      await configured([portSource, portSink, portSinkNext, portSinkMissing]);
    const source = await add("test.port-source@1", 0);
    const sink = await add("test.port-sink@1", 200);
    expect(await graph.connect({ source_node_id: source, source_port_id: "out",
      target_node_id: sink, target_port_id: "in" })).toMatchObject({ outcome: "accepted" });
    await configuration.settled(id);
    const edge = graphClone(graph.document!.edges[0]!);
    const calls = configuration.update.mock.calls.length;
    expect(await graph.replaceNode(sink, "test.port-sink-missing@1"))
      .toMatchObject({ outcome: "failed" });
    expect(configuration.update.mock.calls.length).toBe(calls);
    expect(graph.activeEditBlock).toMatchObject({ action: "replace-node", targetIds: [sink] });
    expect(graph.activeEditBlock!.items).toEqual([expect.objectContaining({ kind: "edge-port",
      edgeId: edge.edge_id, portId: "in", path: expect.stringContaining("/target_port_id") })]);
    expect(graph.document!.nodes.find(node => node.node_binding_id === sink)!.component_id).toBe("test.port-sink");
    graph.dismissEditBlock();
    expect(await graph.replaceNode(sink, "test.port-sink-next@1")).toMatchObject({ outcome: "accepted" });
    await configuration.settled(id);
    // Compatible edges keep both identity and order.
    expect(graph.document!.edges).toEqual([edge]);
    expect(graph.activeEditBlock).toBeNull();
  });
  it("blocks a replacement when two old edges enter an input that now accepts one", async () => {
    const { graph, add } = await configured([portSource, multiSink, singleSinkNext]);
    const first = await add("test.port-source@1", 0);
    const second = await add("test.port-source@1", 100);
    const sink = await add("test.multi-sink@1", 200);
    for (const source of [first, second])
      expect(await graph.connect({ source_node_id: source, source_port_id: "out",
        target_node_id: sink, target_port_id: "in" })).toMatchObject({ outcome: "accepted" });
    expect(await graph.replaceNode(sink, "test.single-sink-next@1")).toMatchObject({ outcome: "failed" });
    expect(graph.activeEditBlock!.items).toHaveLength(2);
    expect(graph.activeEditBlock!.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "edge-input", portId: "in", edgeId: expect.any(String) })]));
  });
  it("blocks a replacement that a surviving declared reference would no longer accept", async () => {
    const typedConsumer: GraphNodeType = { ...graphClone(referenceConsumer), config_node_references: [
      { config_field: "target_id", multiple: false, allow_empty: true,
        target_types: [{ component_id: "test.reference-target", component_version: "1" }] }] };
    const { graph, add } = await configured([referenceTarget, referenceTargetV2, typedConsumer]);
    const target = await add("test.reference-target@1", 0);
    const consumer = await add("test.reference-consumer@1", 100);
    await graph.patchNode(consumer, { config: { target_id: target, related_ids: [] } });
    expect(await graph.replaceNode(target, "test.reference-target@2"))
      .toMatchObject({ outcome: "failed" });
    expect(graph.activeEditBlock!.items).toEqual([expect.objectContaining({ kind: "node-reference",
      nodeId: consumer, configField: "target_id", targetNodeId: target,
      targetComponent: "test.reference-target@2" })]);
    expect(graph.document!.nodes.find(node => node.node_binding_id === target)!.component_version).toBe("1");
  });
  it("blocks an old item registration that is still used unless the new component provides it", async () => {
    const { graph, add } = await configured([objectProvider, objectProviderNext, objectProviderUnrelated, objectConsumer]);
    const provider = await add("test.object-provider@1", 0);
    const consumer = await add("test.object-consumer@1", 100);
    const binding = graph.document!.object_bindings!.find(row => row.registrar_node_id === provider)!;
    await graph.patchNode(consumer, { config: { object_key: binding.object_key } });
    expect(await graph.replaceNode(provider, "test.object-other@1")).toMatchObject({ outcome: "failed" });
    expect(graph.activeEditBlock!.items).toEqual([expect.objectContaining({ kind: "provided-binding",
      objectKey: binding.object_key, typeId: "test.item", schemaVersion: 1 })]);
    graph.dismissEditBlock();
    // The next version still provides the same item type/schema: registration is preserved.
    expect(await graph.replaceNode(provider, "test.object-provider@2")).toMatchObject({ outcome: "accepted" });
    expect(graph.document!.object_bindings!.find(row => row.registrar_node_id === provider)?.object_key)
      .toBe(binding.object_key);
    expect(graph.activeEditBlock).toBeNull();
  });
  it("rejects a save that would leave a dangling data edge with edge/node location and no submission", async () => {
    const { graph, configuration, id, add } = await configured([source, sink]);
    const text = await add("workflow.text@1", 0);
    const output = await add("workflow.output@1", 200);
    expect(await graph.connect({ source_node_id: text, source_port_id: "output",
      target_node_id: output, target_port_id: "input" })).toMatchObject({ outcome: "accepted" });
    await configuration.settled(id);
    const missing = crypto.randomUUID();
    const dangling = { edge_id: crypto.randomUUID(), source_node_id: missing, source_port_id: "output",
      target_node_id: output, target_port_id: "input", order: 0 };
    const document = graphClone(graph.document!);
    document.edges.push(dangling);
    graph.entries[id]!.document = document;
    const calls = configuration.update.mock.calls.length;
    expect(await graph.patchNode(text, { title: "changed" }))
      .toMatchObject({ outcome: "failed" });
    expect(configuration.update.mock.calls.length).toBe(calls);
    const error = useWorkbenchNoticesStore().error!;
    expect(error.reason).toContain(dangling.edge_id);
    expect(error.reason).toContain(missing);
    expect(error.reason).toContain("/"); // JSON Pointer locates the endpoint field.
    expect(graph.document).toEqual(document);
  });
});
