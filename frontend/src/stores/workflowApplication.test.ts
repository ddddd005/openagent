import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, newGraph, type GraphDocument, type GraphSession } from "../domain/workflowGraph";

const json = (value: unknown) => new Response(JSON.stringify(value));
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(release => { resolve = release; });
  return { promise, resolve };
}
function memory() {
  let raw: string | null = null;
  return { getItem: () => raw, setItem: (_key: string, value: string) => { raw = value; } };
}
function fixture() {
  const graph = useWorkflowGraphStore();
  graph.setPersistenceGuard(() => true);
  const id = graph.createWorkflow();
  const current = graphClone(graph.document!); current.revision = 2; current.name = "current definition";
  const old = newGraph("historical definition");
  old.nodes = [{ node_binding_id: crypto.randomUUID(), component_id: "plugin.output", component_version: "1",
    title: "Historical output", position: { x: 25, y: 30 }, config: { text: "old configuration" } }];
  const view: GraphSession = { schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
    workflow_definition_id: current.workflow_definition_id, definition_revision: 2, revision: 3, data_revision: 2,
    head_revision: 2, status: "succeeded", can_submit: true, nodes: [], chains: [], outputs: [],
    data: { revision: 2, values: {} }, selected_chain_run_id: null };
  graph.entries[id] = { document: current, saved_document: graphClone(current), saved_revision: 2,
    session_id: view.workflow_session_id, pending: null };
  graph.views[view.workflow_session_id] = view;
  const candidateId = crypto.randomUUID(), chainId = crypto.randomUUID();
  const directory = { schema_version: 1 as const, kind: "workflow.graph-candidates" as const,
    workflow_session_id: view.workflow_session_id, workflow_definition_id: current.workflow_definition_id,
    definition_revision: 2, session_revision: 3, head_revision: 2, candidates: [{ candidate_id: candidateId,
      chain_run_id: chainId, source_session_id: crypto.randomUUID(), source_workflow_definition_id: old.workflow_definition_id,
      source_definition_revision: old.revision, data_revision: 1, selected: false, can_select: true, diagnostic: null }] };
  graph.candidates[view.workflow_session_id] = directory;
  const selected = { ...view, workflow_definition_id: old.workflow_definition_id, definition_revision: old.revision,
    revision: 4, data_revision: 3, head_revision: 3, selected_chain_run_id: chainId };
  return { graph, id, current, old, view, directory, candidateId, chainId, selected };
}
beforeEach(() => { setActivePinia(createPinia()); vi.useFakeTimers(); });
afterEach(() => {
  useWorkbenchPersistenceStore().$dispose();
  useWorkflowGraphStore().$dispose();
  useWorkspaceStore().$dispose();
  vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals();
});

describe("workflow application coordination", () => {
  it("restores a parent checkpoint definition, persists its distinct workspace identity and starts that exact version", async () => {
    const { graph, id, old, selected, directory, candidateId } = fixture();
    const storage = memory();
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    const paths: string[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      paths.push(path);
      if (path.endsWith("/candidates/select")) return json(selected);
      if (path.endsWith(`/revisions/${old.revision}`)) return json(old);
      if (path.endsWith("/sessions")) return json([selected]);
      if (path.endsWith("/candidates")) return json({ ...directory, workflow_definition_id: old.workflow_definition_id,
        definition_revision: 1, session_revision: 4, head_revision: 3 });
      if (path.endsWith("/runs")) {
        expect(JSON.parse(init.body).expected_revision).toBe(4);
        return json({ ...selected, revision: 5, status: "succeeded" });
      }
      return json(selected);
    }));
    expect(await graph.changeCandidate(candidateId)).toBe(true);
    expect(graph.document).toEqual(old);
    expect(graph.entries[id].saved_document).toEqual(old);
    expect(useWorkspaceStore().activeWorkflow).toMatchObject({ id, title: old.name, nodeCount: old.nodes.length, state: "saved" });
    expect(id).not.toBe(old.workflow_definition_id);
    expect(persistence.save()).toBe(true);
    persistence.$dispose(); graph.$dispose(); setActivePinia(createPinia());
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    const reopened = useWorkflowGraphStore();
    expect(restored.status).toBe("saved");
    expect(reopened.entries[id].document).toEqual(old);
    expect(reopened.entries[id].saved_revision).toBe(1);
    reopened.views[selected.workflow_session_id] = selected;
    paths.length = 0;
    await reopened.submitPrimary();
    expect(paths).toEqual([`/api/graph/sessions/${selected.workflow_session_id}`,
      `/api/graph/sessions/${selected.workflow_session_id}/runs`]);
    expect(reopened.entries[id].pending).toBeNull();
    expect(reopened.session?.workflow_definition_id).toBe(old.workflow_definition_id);
  });

  it("retains the frozen candidate target and original body after reload and a lost receipt", async () => {
    const { graph, id, old, selected, candidateId } = fixture();
    const storage = memory(), persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    stubGraphApplicationFetch( vi.fn().mockRejectedValue(new Error("lost receipt")));
    expect(await graph.changeCandidate(candidateId)).toBe(false);
    const request = graphClone(graph.entries[id].pending!);
    expect(request.expected_definition_id).toBe(old.workflow_definition_id);
    expect(request.expected_definition_revision).toBe(old.revision);
    persistence.$dispose(); graph.$dispose(); setActivePinia(createPinia());
    const restored = useWorkbenchPersistenceStore(); restored.initialize(storage);
    const reopened = useWorkflowGraphStore();
    expect(reopened.entries[id].pending).toEqual(request);
    const sent: unknown[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      if (path === request.path) { sent.push(JSON.parse(init.body)); return json(selected); }
      if (path.includes("/revisions/")) return json(old);
      if (path.endsWith("/sessions")) return json([selected]);
      return json(selected);
    }));
    await reopened.reconcile(id);
    expect(sent).toEqual([request.body]);
    expect(reopened.entries[id].pending).toBeNull();
    expect(reopened.entries[id].document).toEqual(old);
  });

  it("keeps an accepted original request unknown when a later definition arrives during exact definition lookup", async () => {
    const { graph, old, current, selected, view, candidateId } = fixture();
    const lookup = deferred<Response>();
    const fetcher = vi.fn(async path => path.includes("/revisions/") ? lookup.promise : json(selected));
    stubGraphApplicationFetch( fetcher);
    const restoring = graph.changeCandidate(candidateId);
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([path]) => path.includes("/revisions/"))).toBe(true));
    const later = { ...view, revision: 8, head_revision: 6, data_revision: 5 };
    graph.views[view.workflow_session_id] = later;
    lookup.resolve(json(old));
    expect(await restoring).toBe(false);
    expect(graph.pending?.action).toBe("candidate-select");
    expect(graph.document).toEqual(current);
    expect(graph.session).toEqual(later);
    expect(useWorkbenchNoticesStore().error).toMatchObject({ kind: "unknown" });
  });

  it("accepts the original receipt after failed local saving while adopting a newer exact observed definition", async () => {
    const { graph, id, old, current, selected, view, candidateId } = fixture();
    let saves = 0;
    graph.setPersistenceGuard(() => ++saves !== 2);
    stubGraphApplicationFetch( vi.fn(async path => path.includes("/revisions/") ? json(old) : json(selected)));
    expect(await graph.changeCandidate(candidateId)).toBe(false);
    const original = graphClone(graph.entries[id].pending!);
    expect(graph.document).toEqual(old);
    const later = { ...view, revision: 8, data_revision: 5, head_revision: 6 };
    graph.views[view.workflow_session_id] = later;
    const requests: unknown[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      if (path === original.path) { requests.push(JSON.parse(init.body)); return json(selected); }
      if (path.includes("/revisions/")) {
        expect(path).toBe(`/api/graph/definitions/${current.workflow_definition_id}/revisions/2`);
        return json(current);
      }
      if (path.endsWith("/sessions")) return json([later]);
      return json(later);
    }));
    await graph.reconcile(id);
    expect(requests).toEqual([original.body]);
    expect(graph.pending).toBeNull();
    expect(graph.document).toEqual(current);
    expect(graph.entries[id].saved_document).toEqual(current);
    expect(graph.session).toEqual(later);
  });

  it("locks editing and concurrent callers throughout a direct application start, including its pre-run query", async () => {
    const { graph, id, view, current } = fixture();
    const query = deferred<Response>();
    const fetcher = vi.fn(async (path, init) => {
      if (!path.endsWith("/runs")) return query.promise;
      expect(JSON.parse(init.body).inputs).toEqual({ prompt: { text: "original input" } });
      return json({ ...view, revision: 4 });
    });
    stubGraphApplicationFetch( fetcher);
    const target = { workflowId: id };
    graph.entries[id].external_inputs = { prompt: { text: "original input" } };
    const running = graph.management.commands.start(target);
    target.workflowId = "frontend:main-test";
    graph.entries[id].external_inputs = { prompt: { text: "later input" } };
    expect(graph.locked).toBe(true);
    expect(graph.moveNodes([])).toBeNull();
    await expect(graph.management.commands.control({ workflowId: id }, "close")).rejects.toThrow("正在提交");
    expect(graph.document).toEqual(current);
    expect(fetcher).toHaveBeenCalledTimes(1);
    query.resolve(json(view)); await running;
    expect(graph.locked).toBe(false);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("finishes direct application saving and returns detached inspection data", async () => {
    const { graph, id, view, current } = fixture();
    stubGraphApplicationFetch( vi.fn(async () => json(view)));
    await graph.management.commands.save({ workflowId: id });
    expect(useWorkspaceStore().activeWorkflow.state).toBe("saved");
    expect(graph.history[id]).toEqual({ past: [], future: [] });
    const inspected = graph.management.queries.inspect({ workflowId: id })!;
    inspected.document.name = "another editor";
    expect(graph.document).toEqual(current);
  });

  it("does not let direct application callers bypass an unresolved complete-copy transaction", async () => {
    const { graph, id } = fixture();
    useWorkspaceStore().setCopyPending(id, true);
    const fetcher = vi.fn(); stubGraphApplicationFetch( fetcher);
    await expect(graph.management.commands.start({ workflowId: id })).rejects.toThrow("副本");
    expect(fetcher).not.toHaveBeenCalled();
    expect(graph.entries[id].pending).toBeNull();
  });

  it("accepts a detached edit through the scoped editor without granting another mutable document reference", () => {
    const { graph, id, current } = fixture();
    let edited!: GraphDocument;
    expect(graph.editing.edit({ workflowId: id }, document => {
      edited = document; document.name = "accepted edit"; return true;
    })).toBe(id);
    edited.name = "later mutation";
    expect(graph.document?.name).toBe("accepted edit");
    expect(graph.history[id].past).toEqual([current]);
    graph.undo(); expect(graph.document).toEqual(current);
  });

  it("coordinates a full first-edit copy against the original session even when another workflow is displayed", async () => {
    const { graph, id, current, old, view } = fixture();
    useWorkspaceStore().markWorkflowSaved(id);
    const displayed = graph.createWorkflow();
    stubGraphApplicationFetch( vi.fn().mockRejectedValue(new Error("lost copy receipt")));
    const target = graph.editing.edit({ workflowId: id }, document => {
      document.nodes = graphClone(old.nodes); return true;
    })!;
    await vi.waitFor(() => expect(graph.busy).toBeNull());
    const request = graph.entries[target].pending!;
    expect(request.path).toBe(`/api/graph/sessions/${view.workflow_session_id}/copy`);
    expect(request.body).toMatchObject({ expected_session_revision: view.revision,
      expected_data_revision: view.data_revision, expected_definition_revision: view.definition_revision,
      expected_head_revision: view.head_revision, document: { workflow_definition_id: target, nodes: old.nodes } });
    expect(graph.entries[id].document).toEqual(current);
    expect(graph.entries[id].saved_document).toEqual(current);
    expect(useWorkspaceStore().activeWorkflowId).toBe(displayed);
    expect(useWorkspaceStore().workflows.find(row => row.id === target)?.copyPending).toBe(true);
  });

  it("does not import or create the legacy runtime for graph errors", () => {
    const pinia = createPinia(); setActivePinia(pinia);
    const graph = useWorkflowGraphStore(); graph.createWorkflow();
    graph.setObjectBindings([{ invalid: true }]);
    expect(useWorkbenchNoticesStore().error?.kind).toBe("rejected");
    expect(pinia.state.value["workbench-runtime"]).toBeUndefined();
  });
});
