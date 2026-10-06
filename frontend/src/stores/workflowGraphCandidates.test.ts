import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone, type GraphSession } from "../domain/workflowGraph";
function fixture() {
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
  const id = graph.createWorkflow(), sid = crypto.randomUUID(), candidate = crypto.randomUUID(), chain = crypto.randomUUID();
  const view: GraphSession = { schema_version: 2, execution_model: "graph", workflow_session_id: sid,
    workflow_definition_id: graph.document!.workflow_definition_id, definition_revision: 1,
    revision: 3, data_revision: 2, head_revision: 2, status: "succeeded", can_submit: true,
    nodes: [], chains: [], outputs: [], data: { revision: 2, values: {} }, selected_chain_run_id: null };
  graph.entries[id].saved_revision = 1; graph.entries[id].saved_document = graphClone(graph.document!);
  graph.entries[id].session_id = sid; graph.views[sid] = view;
  const directory = { schema_version: 1 as const, kind: "workflow.graph-candidates" as const,
    workflow_session_id: sid, workflow_definition_id: view.workflow_definition_id, definition_revision: 1,
    session_revision: 3, head_revision: 2, candidates: [{ candidate_id: candidate, chain_run_id: chain,
      source_session_id: sid, source_workflow_definition_id: view.workflow_definition_id,
      source_definition_revision: 1, data_revision: 1, selected: false, can_select: true, diagnostic: null }] };
  graph.candidates[sid] = directory;
  return { graph, id, sid, view, directory, candidate, chain };
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals(); });

describe("workflow candidate boundaries", () => {
  it("submits a complete session/data/Head CAS without chat or Agent fields", async () => {
    const { graph, view, directory, candidate, chain } = fixture();
    const changed = { ...view, revision: 4, head_revision: 3, data_revision: 3, selected_chain_run_id: chain,
      data: { revision: 3, values: {}, data: { note: { value: "candidate" } } } };
    const fetcher = vi.fn(async (path, options) => new Response(JSON.stringify(path.includes("/revisions/")
      ? graph.document : path.endsWith("/candidates")
      ? { ...directory, session_revision: 4, head_revision: 3 } : changed)));
    stubGraphApplicationFetch( fetcher);
    expect(await graph.changeCandidate(candidate)).toBe(true);
    const request = JSON.parse(fetcher.mock.calls[0][1].body);
    expect(fetcher.mock.calls[0][0]).toBe(`/api/graph/sessions/${view.workflow_session_id}/candidates/select`);
    expect(request).toEqual({ candidate_id: candidate, expected_revision: 3, expected_data_revision: 2,
      expected_head_revision: 2, idempotency_key: expect.any(String) });
    expect(graph.session?.selected_chain_run_id).toBe(chain);
    expect(graph.session?.data.data).toEqual({ note: { value: "candidate" } });
  });

  it("restores and reads the original unknown candidate request after reload", async () => {
    const { graph, id, view, directory, candidate, chain } = fixture();
    stubGraphApplicationFetch( vi.fn().mockRejectedValue(new Error("lost")));
    expect(await graph.changeCandidate(candidate)).toBe(false);
    const original = graphClone(graph.entries[id].pending!);
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true);
    const selected = { ...view, selected_chain_run_id: chain, revision: 4, head_revision: 3 };
    const fetcher = vi.fn(async (path, options) => {
      if (path === "/api/graph/receipts/read") {
        const request = JSON.parse(options.body);
        return graphReceiptReadResponse(request.operation, request.parameters, selected);
      }
      return new Response(JSON.stringify(path.includes("/revisions/")
        ? graph.document : path.endsWith("/candidates") ? directory : selected));
    });
    stubGraphApplicationFetch( fetcher); await graph.reconcile(id);
    expect(JSON.parse(fetcher.mock.calls[0][1].body).parameters).toEqual({ ...original.body, session_id: view.workflow_session_id });
    expect(fetcher.mock.calls[0][0]).toBe("/api/graph/receipts/read");
    expect(graph.entries[id].pending).toBeNull();
  });

  it("accepts a same-workflow fork only with the original candidate provenance", async () => {
    const { graph, view, directory, candidate, chain } = fixture();
    const forkId = crypto.randomUUID();
    const fork = { ...view, workflow_session_id: forkId, revision: 1, head_revision: 1, selected_chain_run_id: chain,
      source: { kind: "fork_candidate", workflow_session_id: view.workflow_session_id, candidate_commit_id: candidate } };
    stubGraphApplicationFetch( vi.fn(async (path) => new Response(JSON.stringify(path.includes("/revisions/")
      ? graph.document : path.endsWith("/candidates")
      ? { ...directory, workflow_session_id: forkId, session_revision: 1, head_revision: 1 } : fork))));
    expect(await graph.changeCandidate(candidate, true)).toBe(true);
    expect(graph.session?.workflow_session_id).toBe(forkId);
    expect(graph.views[view.workflow_session_id].workflow_session_id).toBe(view.workflow_session_id);
    expect(graph.session?.source?.candidate_commit_id).toBe(candidate);
  });

  it("keeps a fork pending when a valid-looking session belongs to another candidate", async () => {
    const { graph, view, candidate, chain } = fixture();
    stubGraphApplicationFetch( vi.fn(async () => new Response(JSON.stringify({ ...view, workflow_session_id: crypto.randomUUID(),
      selected_chain_run_id: chain, source: { kind: "fork_candidate", workflow_session_id: view.workflow_session_id,
        candidate_commit_id: crypto.randomUUID() } }))));
    expect(await graph.changeCandidate(candidate, true)).toBe(false);
    expect(graph.pending?.body.candidate_id).toBe(candidate);
    expect(graph.session?.workflow_session_id).toBe(view.workflow_session_id);
  });

  it("does not let an original replay receipt regress a later candidate observation", async () => {
    const { graph, view, candidate, chain } = fixture();
    stubGraphApplicationFetch( vi.fn().mockRejectedValue(new Error("lost")));
    await graph.changeCandidate(candidate);
    const laterChain = crypto.randomUUID();
    const later = { ...view, revision: 8, head_revision: 6, selected_chain_run_id: laterChain };
    graph.views[view.workflow_session_id] = later;
    stubGraphApplicationFetch( vi.fn(async (path, options) => {
      if (path === "/api/graph/receipts/read") {
        const request = JSON.parse(options.body);
        return graphReceiptReadResponse(request.operation, request.parameters, {
          ...view, revision: 4, selected_chain_run_id: chain,
        });
      }
      return new Response(JSON.stringify(path.includes("/revisions/") ? graph.document
        : path.endsWith("/sessions") ? [later] : later));
    }));
    await graph.reconcile();
    expect(graph.session?.revision).toBe(8);
    expect(graph.session?.selected_chain_run_id).toBe(laterChain);
  });
});
