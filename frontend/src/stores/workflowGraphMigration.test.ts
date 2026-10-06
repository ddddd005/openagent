import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone, type GraphDocument, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { usePreparationStore } from "./preparation";
function setup() {
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
  const binding = crypto.randomUUID();
  const document: GraphDocument = { schema_version: 1, workflow_definition_id: crypto.randomUUID(), revision: 1,
    name: "旧图", nodes: [{ node_binding_id: binding, component_id: "legacy.preparation.text", component_version: "1",
      title: "文本", position: { x: 10, y: 20 }, config: { text: "retained" } }], edges: [] };
  const type: GraphNodeType = { component_id: "workflow.text", component_version: "1", display_name: "文本", category: "内容",
    config_schema: { type: "object" }, default_config: {}, inputs: [],
    outputs: [{ port_id: "output", data_type: "TEXT", required: true, multiple: false }], is_output: false, executable: true };
  graph.catalog = [type];
  return { graph, document, workspace: useWorkspaceStore() };
}
function receipt(document: GraphDocument) {
  const session: GraphSession = { schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
    workflow_definition_id: document.workflow_definition_id, definition_revision: 1, revision: 1, data_revision: 0, head_revision: 1,
    status: "idle", can_submit: true, nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} } };
  return { document, session, provenance: { source_kind: "legacy", source_session_id: null } };
}
function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .sort(([a], [b]) => a.localeCompare(b)).map(([key, row]) => [key, canonical(row)]));
  return value;
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkbenchPersistenceStore().$dispose(); useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals(); });
describe("original migration request recovery", () => {
  it("retains the original document and replays the exact migration identity after a lost receipt", async () => {
    const { graph, workspace, document } = setup(); const original = graphClone(document);
    stubGraphApplicationFetch( vi.fn().mockRejectedValue(new Error("lost response")));
    expect(await graph.migrateLegacy(MAIN_WORKFLOW_ID, document, null, null)).toBe(false);
    const target = workspace.workflows.find(row => row.sourceId === MAIN_WORKFLOW_ID)!;
    const request = graphClone(graph.entries[target.id].pending)!;
    expect(request.action).toBe("migrate"); expect(target.copyPending).toBe(true);
    expect(workspace.activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
    expect(document).toEqual(original);
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true);
    const calls: unknown[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      if (path === "/api/graph/migrations") {
        const body = JSON.parse(init.body); calls.push(body);
        return new Response(JSON.stringify(canonical(receipt(body.document))));
      }
      return new Response(JSON.stringify(path.endsWith("/sessions") ? [graph.session] : graph.session));
    }));
    await graph.reconcile(target.id);
    expect(calls).toEqual([request.body]);
    expect(graph.entries[target.id].pending).toBeNull(); expect(graph.entries[target.id].migration_request).toBeUndefined();
    expect(workspace.activeWorkflowId).toBe(target.id);
    expect(workspace.workflows.find(row => row.id === MAIN_WORKFLOW_ID)?.state).toBe("saved");
    expect(graph.entries[target.id].migration_provenance?.source_kind).toBe("legacy");
  });
  it("keeps mismatched migration receipts unresolved instead of binding another definition", async () => {
    const { graph, document, workspace } = setup();
    stubGraphApplicationFetch( vi.fn(async (_path, init) => {
      const response = receipt(JSON.parse(init.body).document); response.session.workflow_definition_id = crypto.randomUUID();
      return new Response(JSON.stringify(response));
    }));
    await graph.migrateLegacy(MAIN_WORKFLOW_ID, document, null, null);
    const target = workspace.workflows.find(row => row.sourceId === MAIN_WORKFLOW_ID)!;
    expect(graph.entries[target.id].pending?.action).toBe("migrate"); expect(graph.entries[target.id].session_id).toBeNull();
    expect(workspace.activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
  });
  it("targets the current retained native run with budget CAS and preserves unknown retry requests", async () => {
    const { graph, workspace, document } = setup();
    const initial = receipt(document).session;
    initial.status = "budget_exhausted"; initial.can_submit = false; initial.available_actions = ["extend_budget", "resume"];
    graph.entries[MAIN_WORKFLOW_ID] = { document, saved_revision: 1, saved_document: graphClone(document),
      session_id: initial.workflow_session_id, pending: null };
    graph.views[initial.workflow_session_id] = initial;
    const calls: Record<string,unknown>[] = [];
    stubGraphApplicationFetch( vi.fn(async (_path, init) => {
      calls.push(JSON.parse(init.body)); return new Response(JSON.stringify({ ...initial, revision: 2 }));
    }));
    expect(graph.primaryAction).toBe("resume");
    expect(await graph.submitAgentAction("extend_budget", 2, 4)).toBe(true);
    expect(calls[0]).toMatchObject({ action: "extend_budget", expected_revision: 1, add_model_requests: 2, add_model_attempts: 4 });
    graph.views[initial.workflow_session_id] = { ...initial, status: "archive_failed", available_actions: ["retry_archive", "close"] };
    stubGraphApplicationFetch( vi.fn().mockRejectedValue(new Error("lost")));
    await graph.submitAgentAction("retry_archive");
    const original = graphClone(graph.pending);
    expect(original?.path).toBe(`/api/graph/sessions/${initial.workflow_session_id}/control`);
    expect(original?.body.action).toBe("retry_archive"); expect(workspace.activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true); expect(graph.pending).toEqual(original);
  });
  it("does not let a late refresh regress a newer accepted Agent control receipt", async () => {
    const { graph, document } = setup(); const initial = receipt(document).session;
    initial.status = "budget_exhausted"; initial.can_submit = false; initial.available_actions = ["extend_budget", "resume"];
    graph.entries[MAIN_WORKFLOW_ID] = { document, saved_revision: 1, saved_document: graphClone(document),
      session_id: initial.workflow_session_id, pending: null };
    graph.views[initial.workflow_session_id] = initial;
    let release: (value: Response) => void;
    stubGraphApplicationFetch( vi.fn(async path => {
      if (path.endsWith("/control")) return new Response(JSON.stringify({ ...initial, revision: 2 }));
      if (path.endsWith("/sessions")) return new Promise<Response>(resolve => { release = resolve; });
      return new Response(JSON.stringify(initial));
    }));
    const refresh = graph.refresh();
    await vi.waitFor(() => expect(release!).toBeDefined());
    await graph.submitAgentAction("extend_budget", 1, 4);
    release!(new Response(JSON.stringify([initial]))); await refresh;
    expect(graph.session?.revision).toBe(2);
    expect(graph.sessions[MAIN_WORKFLOW_ID][0].revision).toBe(2);
  });

  it("retains a generic result-acceptance retry across an unknown response and workbench restore", async () => {
    const { graph, document } = setup();
    const initial = receipt(document).session;
    initial.status = "archive_failed"; initial.can_submit = false;
    initial.available_actions = ["retry_acceptance", "close"];
    graph.entries[MAIN_WORKFLOW_ID] = { document, saved_revision: 1, saved_document: graphClone(document),
      session_id: initial.workflow_session_id, pending: null };
    graph.views[initial.workflow_session_id] = initial;
    const fetcher = vi.fn().mockRejectedValue(new Error("lost acceptance receipt"));
    stubGraphApplicationFetch( fetcher);
    expect(await graph.submitAgentAction("retry_acceptance")).toBe(false);
    const original = graphClone(graph.pending)!;
    expect(original.path).toBe(`/api/graph/sessions/${initial.workflow_session_id}/control`);
    expect(original.body).toMatchObject({ action: "retry_acceptance", expected_revision: 1 });
    expect(original.body).not.toHaveProperty("add_model_requests");
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true);
    expect(graph.pending).toEqual(original);
    expect(await graph.submitAgentAction("retry_acceptance")).toBe(false);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("restores a pending generic migration without redirecting later legacy edits into its graph", async () => {
    let raw: string | null = null;
    const storage = { getItem: () => raw, setItem: (_key: string, value: string) => { raw = value; } };
    const { graph, document, workspace } = setup();
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    stubGraphApplicationFetch( vi.fn().mockRejectedValue(new Error("lost migration receipt")));
    await graph.migrateLegacy(MAIN_WORKFLOW_ID, document, null, null);
    const target = workspace.workflows.find(row => row.sourceId === MAIN_WORKFLOW_ID)!;
    persistence.$dispose(); graph.$dispose(); setActivePinia(createPinia());
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    const recoveredGraph = useWorkflowGraphStore(); const recoveredWorkspace = useWorkspaceStore();
    const prep = usePreparationStore();
    const prompt = prep.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find(node => node.kind === "prompt-item")!;
    const original = graphClone(prompt.config); const count = recoveredWorkspace.workflows.length;
    prep.updateNodeConfig(MAIN_WORKFLOW_ID, "A", prompt.id, { text: "blocked while migration is unknown" });
    expect(recoveredWorkspace.workflows).toHaveLength(count);
    expect(prep.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find(node => node.id === prompt.id)?.config).toEqual(original);
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      if (path === "/api/graph/migrations") return new Response(JSON.stringify(receipt(JSON.parse(init.body).document)));
      return new Response(JSON.stringify(path.endsWith("/sessions") ? [recoveredGraph.session] : recoveredGraph.session));
    }));
    await recoveredGraph.reconcile(target.id); recoveredWorkspace.openWorkflow(MAIN_WORKFLOW_ID);
    prep.updateNodeConfig(MAIN_WORKFLOW_ID, "A", prompt.id, { text: "independent legacy edit" });
    await vi.waitFor(() => expect(recoveredWorkspace.activeWorkflowId).not.toBe(MAIN_WORKFLOW_ID));
    expect(recoveredWorkspace.activeWorkflowId).not.toBe(target.id);
    expect(recoveredGraph.entries[target.id].document.nodes[0].config).toEqual({ text: "retained" });
    expect(prep.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find(node => node.id === prompt.id)?.config).toEqual(original);
  });
});
