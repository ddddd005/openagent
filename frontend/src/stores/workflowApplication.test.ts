import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, newGraph, type GraphCommand, type GraphDocument, type GraphSession } from "../domain/workflowGraph";
import { graphWorkbenchStorage } from "../testUtils/graphWorkbenchStorage";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { workbenchDraftAddress } from "../adapters/localDraftsApi";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";

const json = (value: unknown) => new Response(JSON.stringify(value));
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(release => { resolve = release; });
  return { promise, resolve };
}
function memory() {
  const workspace = useWorkspaceStore();
  return graphWorkbenchStorage({ graph: useWorkflowGraphStore().storeSnapshot(), catalog: workspace.workflows,
    activeWorkflowId: workspace.activeWorkflowId, selectedWorkflowId: workspace.selectedWorkflowId });
}
async function fixture() {
  const graph = useWorkflowGraphStore();
  graph.setPersistenceGuard(() => true);
  const id = graph.createWorkflow();
  await graph.reconcile(id);
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
  const candidate = { candidate_id: crypto.randomUUID(), chain_run_id: crypto.randomUUID(),
    source_workflow_definition_id: old.workflow_definition_id, source_definition_revision: old.revision };
  const selected = { ...view, workflow_definition_id: old.workflow_definition_id, definition_revision: old.revision,
    revision: 4, data_revision: 3, head_revision: 3, selected_chain_run_id: candidate.chain_run_id };
  return { graph, id, current, old, view, candidate, selected };
}
function pendingCandidateSelect(view: GraphSession, candidate: Awaited<ReturnType<typeof fixture>>["candidate"]): GraphCommand {
  return { path: `/api/graph/sessions/${view.workflow_session_id}/candidates/select`,
    body: { candidate_id: candidate.candidate_id, expected_revision: view.revision, expected_data_revision: view.data_revision,
      expected_head_revision: view.head_revision, idempotency_key: crypto.randomUUID() },
    action: "candidate-select", source_session_id: view.workflow_session_id,
    expected_chain_run_id: candidate.chain_run_id, expected_definition_id: candidate.source_workflow_definition_id,
    expected_definition_revision: candidate.source_definition_revision };
}
beforeEach(() => { setActivePinia(createPinia()); installDebtCleanupEvidence(); vi.useFakeTimers(); });
afterEach(() => {
  useWorkbenchPersistenceStore().$dispose();
  useWorkflowGraphStore().$dispose();
  useWorkspaceStore().$dispose();
  vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("workflow application coordination", () => {
  it("reconciles a previously selected candidate without replacing the editor or starting a run", async () => {
    const { graph, id, current, old, selected, candidate, view } = await fixture();
    const original = pendingCandidateSelect(view, candidate);
    graph.entries[id].pending = graphClone(original);
    const paths: string[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      paths.push(path);
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(init.body);
        return graphReceiptReadResponse(envelope.operation, envelope.parameters, selected);
      }
      if (path.endsWith(`/revisions/${old.revision}`)) return json(old);
      if (path.endsWith("/sessions")) return json([selected]);
      return json(selected);
    }));
    await graph.reconcile(id);
    expect(graph.document).toEqual(current);
    expect(graph.entries[id].saved_document).toEqual(current);
    expect(graph.entries[id].session_document).toBeUndefined();
    expect(useWorkspaceStore().activeWorkflow).toMatchObject({ id, title: "新工作流", state: "draft" });
    expect(id).not.toBe(old.workflow_definition_id);
    expect(paths[0]).toBe("/api/graph/receipts/read");
    expect(paths.some(path => path.endsWith("/candidates/select") || path.endsWith("/runs"))).toBe(false);
    paths.length = 0;
    await graph.submitPrimary();
    expect(paths.some(path => path.endsWith("/runs"))).toBe(false);
    expect(graph.entries[id].pending).toEqual(original);
    expect(graph.session).toEqual(view);
    expect(graph.receiptChecks[id]).toContain("pending 证据保留");
  });

  it("retains the frozen candidate target and original body after reload and a lost receipt", async () => {
    const { graph, id, old, selected, candidate, view } = await fixture();
    const original = pendingCandidateSelect(view, candidate);
    graph.entries[id].pending = graphClone(original);
    const storage = memory(), backend = memoryLocalDraftStorage();
    const record = await backend.ports.save({ address: workbenchDraftAddress, format_version: 1,
      value: JSON.parse(storage.getItem()!), expected_sequence: 0, idempotency_key: crypto.randomUUID() });
    graph.$dispose(); setActivePinia(createPinia());
    const restored = useWorkbenchPersistenceStore(); await restored.initialize(storage, backend.ports);
    const reopened = useWorkflowGraphStore();
    expect(reopened.entries).toEqual({});
    expect(restored.legacyRequests[0]?.command).toEqual(original);
    const sent: unknown[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(init.body);
        expect(envelope.operation).toBe("candidate.select");
        const { session_id, ...body } = envelope.parameters;
        expect(session_id).toBe(original.source_session_id);
        sent.push(body);
        return graphReceiptReadResponse(envelope.operation, envelope.parameters, selected);
      }
      expect(path).not.toBe(original.path);
      if (path.includes("/revisions/")) return json(old);
      if (path.endsWith("/sessions")) return json([selected]);
      return json(selected);
    }));
    expect(await restored.verifyLegacyRequest(restored.legacyRequests[0]!.key)).toBe(true);
    expect(sent).toEqual([original.body]);
    expect(restored.legacyRequests[0]?.command).toEqual(original);
    expect(restored.legacyRequests[0]?.status).toBe("verified");
    expect(reopened.entries).toEqual({});
    expect(await backend.ports.read(record.address)).toEqual(record);
    expect(backend.ports.save).toHaveBeenCalledOnce();
  });

  it("keeps an accepted original request unknown when a later definition arrives during exact definition lookup", async () => {
    const { graph, id, old, current, selected, view, candidate } = await fixture();
    const original = pendingCandidateSelect(view, candidate);
    graph.entries[id].pending = graphClone(original);
    const lookup = deferred<Response>();
    const fetcher = vi.fn(async (path, init) => path === "/api/graph/receipts/read"
      ? graphReceiptReadResponse("candidate.select", JSON.parse(init.body).parameters, selected)
      : path.includes("/revisions/") ? lookup.promise : json(selected));
    stubGraphApplicationFetch( fetcher);
    const restoring = graph.reconcile(id);
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([path]) => path.includes("/revisions/"))).toBe(true));
    const later = { ...view, revision: 8, head_revision: 6, data_revision: 5 };
    graph.views[view.workflow_session_id] = later;
    lookup.resolve(json(old));
    await restoring;
    expect(graph.pending?.action).toBe("candidate-select");
    expect(graph.document).toEqual(current);
    expect(graph.session).toEqual(later);
    expect(useWorkbenchNoticesStore().error).toMatchObject({ kind: "unknown" });
  });

  it("verifies the original receipt without saving or replacing a newer definition and session", async () => {
    const { graph, id, old, current, selected, view, candidate } = await fixture();
    const original = pendingCandidateSelect(view, candidate);
    graph.entries[id].pending = graphClone(original);
    let saves = 0;
    graph.setPersistenceGuard(() => ++saves !== 1);
    stubGraphApplicationFetch( vi.fn(async (path, init) => path === "/api/graph/receipts/read"
      ? graphReceiptReadResponse("candidate.select", JSON.parse(init.body).parameters, selected)
      : path.includes("/revisions/") ? json(old) : json(selected)));
    await graph.reconcile(id);
    expect(graph.document).toEqual(current);
    expect(graph.entries[id].session_document).toBeUndefined();
    const later = { ...view, revision: 8, data_revision: 5, head_revision: 6 };
    graph.views[view.workflow_session_id] = later;
    const requests: unknown[] = [];
    stubGraphApplicationFetch( vi.fn(async (path, init) => {
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(init.body);
        expect(envelope.operation).toBe("candidate.select");
        const { session_id, ...body } = envelope.parameters;
        expect(session_id).toBe(original.source_session_id);
        requests.push(body);
        return graphReceiptReadResponse(envelope.operation, envelope.parameters, selected);
      }
      expect(path).not.toBe(original.path);
      if (path.includes("/revisions/")) {
        expect(path).toBe(`/api/graph/definitions/${current.workflow_definition_id}/revisions/2`);
        return json(current);
      }
      if (path.endsWith("/sessions")) return json([later]);
      return json(later);
    }));
    await graph.reconcile(id);
    expect(requests).toEqual([original.body]);
    expect(graph.pending).toEqual(original);
    expect(graph.document).toEqual(current);
    expect(graph.entries[id].saved_document).toEqual(current);
    expect(graph.session).toEqual(later);
    expect(saves).toBe(0);
  });

  it("locks editing and concurrent callers throughout a direct application start, including its pre-run query", async () => {
    const { graph, id, view, current } = await fixture();
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
    target.workflowId = crypto.randomUUID();
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

  it("rejects retired application saving and returns detached inspection data", async () => {
    const { graph, id, view, current } = await fixture();
    stubGraphApplicationFetch( vi.fn(async () => json(view)));
    await expect(graph.management.commands.save({ workflowId: id })).rejects.toMatchObject({ kind: "unavailable" });
    expect(graph.entries[id].saved_document).toEqual(current);
    expect(graph.history[id]).toEqual({ past: [], future: [] });
    const inspected = graph.management.queries.inspect({ workflowId: id })!;
    inspected.document.name = "another editor";
    expect(graph.document).toEqual(current);
  });

  it("does not let direct application callers bypass an unresolved complete-copy transaction", async () => {
    const { graph, id } = await fixture();
    useWorkspaceStore().setCopyPending(id, true);
    const fetcher = vi.fn(); stubGraphApplicationFetch( fetcher);
    await expect(graph.management.commands.start({ workflowId: id })).rejects.toThrow("副本");
    expect(fetcher).not.toHaveBeenCalled();
    expect(graph.entries[id].pending).toBeNull();
  });

  it("accepts a detached edit through the scoped editor without granting another mutable document reference", async () => {
    const { graph, id, current } = await fixture();
    let edited!: GraphDocument;
    expect(graph.editing.edit({ workflowId: id }, document => {
      edited = document; document.name = "accepted edit"; return true;
    })).toBe(id);
    edited.name = "later mutation";
    expect(graph.document?.name).toBe("accepted edit");
    expect(graph.history[id].past).toEqual([current]);
    graph.undo(); expect(graph.document).toEqual(current);
  });

  it("keeps the retired editor clone out of the application and leaves the displayed workflow untouched", async () => {
    const { graph, id, current } = await fixture();
    useWorkspaceStore().markWorkflowSaved(id);
    const displayed = graph.createWorkflow();
    const fetcher = vi.fn();
    stubGraphApplicationFetch(fetcher);
    expect("copy" in graph.editing).toBe(false);
    const before = graphClone(graph.storeSnapshot()), count = useWorkspaceStore().workflows.length;
    // Without a formal current the narrow copy channel refuses; it never clones the
    // editor memory document and never touches the displayed workflow.
    expect(await graph.copyWorkflow(id)).toBeNull();
    expect(useWorkbenchNoticesStore().error).toMatchObject({ kind: "rejected" });
    expect(useWorkbenchNoticesStore().error?.reason).toContain("正式 current");
    expect(graph.entries[id].document).toEqual(current);
    expect(graph.entries[id].saved_document).toEqual(current);
    expect(useWorkspaceStore().activeWorkflowId).toBe(displayed);
    expect(graph.storeSnapshot()).toEqual(before);
    expect(useWorkspaceStore().workflows).toHaveLength(count);
    expect(graph.entries[id].pending).toBeNull();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not import or create the legacy runtime for graph errors", () => {
    const pinia = createPinia(); setActivePinia(pinia);
    const graph = useWorkflowGraphStore(); graph.createWorkflow();
    graph.setObjectBindings([{ invalid: true }]);
    expect(useWorkbenchNoticesStore().error?.kind).toBe("rejected");
    expect(pinia.state.value["workbench-runtime"]).toBeUndefined();
  });
});
