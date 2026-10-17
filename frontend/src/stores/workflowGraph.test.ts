import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, isGraphDocument, type GraphDocument, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import { debtCleanupDerivationResult, installDebtCleanupConfiguration } from "../testUtils/debtCleanupConfiguration";
import { readLegacyWorkbenchEvidence } from "../adapters/workbenchPersistence";
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
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true); graph.catalog = [source,sink];
  const id = graph.createWorkflow();
  await graph.reconcile(id);
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
async function copyEvidenceFixture() {
  const state = await fixture(), workspace = useWorkspaceStore(), view = session(state.graph.document!);
  const document = graphClone(state.graph.document!);
  document.workflow_definition_id = crypto.randomUUID();
  const target = { id: document.workflow_definition_id, sourceId: state.id, title: "Frozen copy",
    description: "", nodeCount: document.nodes.length, state: "draft" as const, copyPending: true };
  const original = { action: "copy", path: `/api/graph/sessions/${view.workflow_session_id}/copy`,
    body: { document, expected_session_revision: view.revision, expected_data_revision: view.data_revision,
      expected_definition_revision: view.definition_revision, expected_head_revision: view.head_revision,
      idempotency_key: crypto.randomUUID() } };
  const record = { document, saved_revision: 0, session_id: null, pending: graphClone(original) };
  const evidence = readLegacyWorkbenchEvidence({ schemaVersion: 7, kind: "graph-workbench",
    graph: { schema_version: 1, entries: { [target.id]: record } } });
  expect(evidence.unreadablePending).toBe(0);
  expect(evidence.pending[0]?.command).toEqual(original);
  state.graph.entries[target.id] = { ...record, pending: graphClone(evidence.pending[0]!.command) };
  workspace.registerGraphWorkflow(target);
  return { ...state, workspace, view, target, original };
}
beforeEach(() => { setActivePinia(createPinia()); installDebtCleanupEvidence(); });
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe("generic workbench execution and document transactions", () => {
  it("resets selection when creating blank drafts and keeps empty/populated switches on the same document contract", async () => {
    const { graph, id, text, output } = await fixture(), workspace = useWorkspaceStore();
    const populated = graphClone(graph.document!);
    graph.executionPackageLock = [{ package_id: "plugin", version: "1.0.0" }];
    graph.selectedNodeIds = [text, output]; graph.selectedEdgeId = populated.edges[0].edge_id;
    const blankId = graph.createWorkflow(), blank = graphClone(graph.document!);
    await graph.reconcile(blankId);
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
  it("creates only blank independent ordinary drafts without exposing a provided-document preset entry", () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const id = graph.createWorkflow(), before = graphClone(graph.entries[id]);
    graph.selectedNodeIds = [crypto.randomUUID()]; graph.selectedEdgeId = crypto.randomUUID();
    const nextId = graph.createWorkflow();
    expect(workspace.activeWorkflowId).toBe(nextId);
    expect(workspace.activeWorkflow).toMatchObject({ state: "draft", nodeCount: 0, description: "" });
    expect(graph.entries[nextId]).toMatchObject({ saved_revision: 0, session_id: null, pending: null,
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
      saved_revision: 7, session_id: null, pending: null };
    workspace.markWorkflowSaved(id, old.name);
    graph.executionPackageLock = [{ package_id: "plugin", version: "2.0.0" }];
    expect(graph.restoreSnapshot(snapshot)).toBe(false);
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
    const id = graph.createWorkflow();
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
    const text = graph.addNode("workflow.text@1", { x: 0, y: 0 })!;
    await configuration.settled(id);
    const output = graph.addNode("workflow.output@1", { x: 300, y: 0 })!;
    await configuration.settled(id);
    graph.connect({ source_node_id: text, source_port_id: "output", target_node_id: output, target_port_id: "input" });
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
    const id = graph.addNode("independent.upper@1", { x: 10, y: 20 })!;
    expect(graph.document?.nodes.find(n => n.node_binding_id === id)?.component_id).toBe("independent.upper");
    graph.selectedNodeIds = [id]; graph.removeSelection(); graph.undo();
    expect(graph.document?.nodes.some(n => n.node_binding_id === id)).toBe(true);
  });
  it("moves different node types as one transaction and preserves edges during mode edits", async () => {
    const { graph, text, output } = await fixture();
    graph.moveNodes([{ id: text, position: { x: 22, y: 33 } }, { id: output, position: { x: 444, y: 555 } }]);
    graph.undo();
    expect(graph.document?.nodes.map(n => n.position)).toEqual([{ x:0,y:0 },{ x:300,y:0 }]);
    graph.undo(true);
    expect(graph.document?.nodes[1].position).toEqual({ x:444,y:555 });
    const edges = graphClone(graph.document?.edges);
    graph.patchNode(output, { config: { mode: "prompt" } }); expect(graph.document?.edges).toEqual(edges);
  });
  it("keeps repeated wiring a no-op and duplicates the selected nodes and their edge as one undoable edit", async () => {
    const { graph, text, output } = await fixture();
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
  it.each(["single-source", "schema", "missing-port", "combined-cycle"])(
    "rejects %s connections without changing the graph, undo history or persisted state", async issue => {
      const { graph, id, text, output } = await fixture();
      const other = graph.addNode("workflow.text@1", { x: 0, y: 200 })!;
      graph.catalog = graphClone([source, sink]);
      if (issue === "schema") graph.catalog[1]!.inputs[0]!.data_schema_version = 2;
      if (issue === "combined-cycle") {
        graph.catalog[1]!.inputs[0]!.multiple = true;
        graph.document!.control_edges = [{ edge_id: crypto.randomUUID(), source_node_id: output, target_node_id: other }];
      }
      graph.patchNode(text, { title: "Undoable title" }); graph.undo();
      const document = graphClone(graph.document!), history = graphClone(graph.history[id]);
      const persist = vi.fn(() => true), fetcher = vi.fn();
      graph.setPersistenceGuard(persist); vi.stubGlobal("fetch", fetcher);
      expect(graph.connect({ source_node_id: other, source_port_id: issue === "missing-port" ? "missing" : "output",
        target_node_id: output, target_port_id: "input" })).toBeNull();
      expect(graph.document).toEqual(document); expect(graph.history[id]).toEqual(history);
      expect(useWorkspaceStore().activeWorkflowId).toBe(id);
      expect(persist).not.toHaveBeenCalled(); expect(fetcher).not.toHaveBeenCalled();
      expect(useWorkbenchNoticesStore().error).toMatchObject({ kind: "rejected" });
    });
  it("saves exact definitions and starts with empty external inputs and no legacy endpoints", async () => {
    const graph = useWorkflowGraphStore(), configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    graph.catalog = [source, sink];
    const workflowId = graph.createWorkflow();
    await configuration.settled(workflowId);
    const text = graph.addNode("workflow.text@1", { x: 0, y: 0 })!;
    await configuration.settled(workflowId);
    const output = graph.addNode("workflow.output@1", { x: 300, y: 0 })!;
    await configuration.settled(workflowId);
    graph.connect({ source_node_id: text, source_port_id: "output", target_node_id: output, target_port_id: "input" });
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
  it("reads an old rebind outbox with the same owner body and key without accepting it", async () => {
    const { graph, id } = await fixture();
    const document = graphClone(graph.entries[id].document);
    const view = session(document);
    document.revision = 2;
    graph.entries[id].document = document;
    graph.entries[id].saved_document = graphClone(document);
    graph.entries[id].saved_revision = 2;
    graph.entries[id].session_id = view.workflow_session_id;
    graph.views[view.workflow_session_id] = view;
    const original = { path: `/api/graph/sessions/${view.workflow_session_id}/rebind`,
      body: { definition_revision: 2, expected_revision: view.revision,
        expected_data_revision: view.data_revision, expected_head_revision: view.head_revision,
        mappings: [], idempotency_key: crypto.randomUUID() }, action: "rebind" };
    graph.entries[id].pending = graphClone(original);
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(false);
    expect(graph.entries[id].pending).toEqual(original);
    const accepted = { ...view, definition_revision: 2, revision: 2 };
    const paths: string[] = [], receiptReads: unknown[] = [];
    stubGraphApplicationFetch(vi.fn(async (path, init) => {
      paths.push(path);
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(init.body);
        expect(envelope.operation).toBe("session.rebind");
        const { session_id, ...body } = envelope.parameters;
        expect(session_id).toBe(view.workflow_session_id);
        receiptReads.push(body);
        return graphReceiptReadResponse(envelope.operation, envelope.parameters, accepted);
      }
      return new Response(JSON.stringify(path.endsWith("/sessions") ? [accepted] : accepted));
    }));

    await graph.reconcile(id);

    expect(receiptReads).toEqual([original.body]);
    expect(paths[0]).toBe("/api/graph/receipts/read");
    expect(paths).not.toContain(original.path);
    expect(graph.entries[id].pending).toEqual(original);
    expect(graph.session?.workflow_session_id).toBe(view.workflow_session_id);
    expect(graph.session?.definition_revision).toBe(view.definition_revision);
  });
  it("keeps an unresolved old rebind outbox and never submits it", async () => {
    const { graph, id } = await fixture();
    const document = graphClone(graph.entries[id].document);
    const view = session(document);
    document.revision = 2;
    graph.entries[id].document = document;
    graph.entries[id].saved_document = graphClone(document);
    graph.entries[id].saved_revision = 2;
    graph.entries[id].session_id = view.workflow_session_id;
    graph.views[view.workflow_session_id] = view;
    const original = { path: `/api/graph/sessions/${view.workflow_session_id}/rebind`,
      body: { definition_revision: 2, expected_revision: view.revision,
        expected_data_revision: view.data_revision, expected_head_revision: view.head_revision,
        mappings: [], idempotency_key: crypto.randomUUID() }, action: "rebind" };
    graph.entries[id].pending = graphClone(original);
    const paths: string[] = [], requests: unknown[] = [];
    stubGraphApplicationFetch(vi.fn(async (path, init) => {
      paths.push(path);
      if (path !== "/api/graph/receipts/read") throw new Error(`Unexpected request: ${path}`);
      const envelope = JSON.parse(init.body);
      requests.push(envelope);
      return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-receipt-read",
        outcome: "unresolved", reason_code: "not_found" }));
    }));

    await graph.reconcile(id);

    expect(paths).toEqual(["/api/graph/receipts/read"]);
    expect(requests).toEqual([{ operation: "session.rebind", parameters: {
      ...original.body, session_id: view.workflow_session_id,
    } }]);
    expect(graph.entries[id].pending).toEqual(original);
    expect(graph.session?.workflow_session_id).toBe(view.workflow_session_id);
  });
  it("reloads frozen copy evidence and reads the same UUID and body without accepting it", async () => {
    const { graph, id, workspace, view, target, original } = await copyEvidenceFixture();
    expect(target.copyPending).toBe(true); expect(workspace.activeWorkflowId).toBe(id);
    const frozen = session(original.body.document);
    const before = graphClone(graph.entries[target.id]);
    const fetcher = vi.fn(async (path: string, init: RequestInit) => {
      if (path === "/api/graph/receipts/read") {
        const request = JSON.parse(String(init.body));
        return graphReceiptReadResponse(request.operation, request.parameters, frozen);
      }
      return new Response(JSON.stringify(path.endsWith("/sessions") ? [frozen] : frozen));
    });
    stubGraphApplicationFetch( fetcher); await graph.reconcile(target.id);
    expect(fetcher.mock.calls[0][0]).toBe("/api/graph/receipts/read");
    expect(JSON.parse(String(fetcher.mock.calls[0][1].body)).parameters).toEqual({
      ...original?.body, session_id: view.workflow_session_id,
    });
    expect(workspace.activeWorkflowId).toBe(id);
    expect(graph.entries[target.id]).toEqual(before);
    expect(graph.receiptChecks[target.id]).toContain("pending 证据保留");
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it("preserves a restored copy without original coordinates or rejection evidence without making a new request", async () => {
    const { graph, target } = await copyEvidenceFixture();
    graph.entries[target.id].pending = null;
    graph.entries[target.id].diagnostics = [{ reason_code: "stale_revision", message: "old diagnostic is not request evidence" }];
    const before = graphClone(graph.entries[target.id]), fetcher = vi.fn();
    stubGraphApplicationFetch(fetcher);
    await graph.reconcile(target.id); await graph.retryRejectedCopy(target.id);
    expect(fetcher).not.toHaveBeenCalled();
    expect(graph.entries[target.id]).toEqual(before); expect(target.copyPending).toBe(true);
    expect(graph.canRetryRejectedCopy(target.id)).toBe(false);
    expect(useWorkbenchNoticesStore().error).toMatchObject({ kind: "unknown" });
  });
  it.each([404, 410])("retains an unknown copy after an HTTP %s receipt read and blocks explicit retries", async status => {
    const { graph, target, original } = await copyEvidenceFixture(), paths: string[] = [];
    stubGraphApplicationFetch(vi.fn(async path => {
      paths.push(path);
      return new Response(JSON.stringify({ error: { reason_code: "not_found" } }), { status });
    }));
    await graph.reconcile(target.id); await graph.retryRejectedCopy(target.id);
    expect(paths).toEqual(["/api/graph/receipts/read"]);
    expect(graph.entries[target.id].pending).toEqual(original);
    expect(graph.entries[target.id].rejected_copy).toBeUndefined(); expect(target.copyPending).toBe(true);
  });
  it("derives fixed node edits without changing the old session and runs only the fresh target", async () => {
    const { graph, id, text } = await fixture(), workspace = useWorkspaceStore();
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    await configuration.application.forWorkflow(id).initialize();
    graph.renameWorkflow("Formal source");
    await configuration.settled(id);
    const source = graphClone(graph.document!), view = session(source), targetId = crypto.randomUUID();
    graph.entries[id].session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    configuration.update.mockImplementation(async request => debtCleanupDerivationResult(request, targetId));
    vi.stubGlobal("window", { confirm: vi.fn(() => true) });
    graph.patchNode(text, { config: { text: "second edit" } });
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
    const configuration = installDebtCleanupConfiguration(graph, evidence), workflowId = graph.createWorkflow();
    await configuration.settled(workflowId);
    const original = graphClone(graph.document!);
    vi.mocked(evidence.ports.write).mockRejectedValueOnce(
      new WorkbenchApiError("rejected", "Original evidence unavailable", 503, "evidence_storage_failed"));
    graph.renameWorkflow("unsent modification");
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
