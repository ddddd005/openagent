import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone, type GraphCommand, type GraphSession } from "../domain/workflowGraph";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import { readLegacyWorkbenchEvidence } from "../adapters/workbenchPersistence";

async function fixture() {
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
  const id = graph.createWorkflow(), sid = crypto.randomUUID(), candidate = crypto.randomUUID(), chain = crypto.randomUUID();
  await graph.reconcile(id);
  const view: GraphSession = { schema_version: 2, execution_model: "graph", workflow_session_id: sid,
    workflow_definition_id: graph.document!.workflow_definition_id, definition_revision: 1,
    revision: 3, data_revision: 2, head_revision: 2, status: "succeeded", can_submit: true,
    nodes: [], chains: [], outputs: [], data: { revision: 2, values: {} }, selected_chain_run_id: null };
  graph.entries[id].saved_revision = 1; graph.entries[id].saved_document = graphClone(graph.document!);
  graph.entries[id].session_id = sid; graph.views[sid] = view;
  return { graph, id, sid, view, candidate, chain };
}
function pendingCandidateFork(sid: string, view: GraphSession, candidateId: string, chain: string): GraphCommand {
  return { path: `/api/graph/sessions/${sid}/candidates/fork`,
    body: { candidate_id: candidateId, expected_revision: view.revision, expected_data_revision: view.data_revision,
      expected_head_revision: view.head_revision, idempotency_key: crypto.randomUUID() },
    action: "candidate-fork", source_session_id: sid, expected_chain_run_id: chain,
    expected_definition_id: view.workflow_definition_id, expected_definition_revision: view.definition_revision };
}
beforeEach(() => { setActivePinia(createPinia()); installDebtCleanupEvidence(); });
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("workflow candidate receipt retirement", () => {
  it("reloads and verifies an original select receipt without clearing evidence or installing its result", async () => {
    const { graph, id, sid, view, candidate, chain } = await fixture();
    const selected = { ...view, revision: 4, data_revision: 3, head_revision: 3,
      selected_chain_run_id: chain, data: { revision: 3, values: {}, data: { note: { value: "candidate" } } } };
    const request: GraphCommand = {
      path: `/api/graph/sessions/${sid}/candidates/select`,
      body: { candidate_id: candidate, expected_revision: 3, expected_data_revision: 2,
        expected_head_revision: 2, idempotency_key: crypto.randomUUID() },
      action: "candidate-select", source_session_id: sid, expected_chain_run_id: chain,
      expected_definition_id: view.workflow_definition_id, expected_definition_revision: 1,
    };
    graph.entries[id].pending = graphClone(request);
    const retained = readLegacyWorkbenchEvidence({ schemaVersion: 7, kind: "graph-workbench",
      graph: graph.storeSnapshot() });
    expect(retained.unreadablePending).toBe(0);
    graph.entries[id].pending = graphClone(retained.pending[0]!.command);
    expect(graph.entries[id].pending).toEqual(request);
    const historicalDefinition = graphClone(graph.document!); historicalDefinition.revision = 1;
    const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(String(init?.body));
        return graphReceiptReadResponse(envelope.operation, envelope.parameters, selected);
      }
      if (path === `/api/graph/definitions/${view.workflow_definition_id}/revisions/1`)
        return new Response(JSON.stringify(historicalDefinition));
      if (path === `/api/graph/definitions/${view.workflow_definition_id}/sessions`)
        return new Response(JSON.stringify([selected]));
      return new Response(JSON.stringify(selected));
    });
    stubGraphApplicationFetch(fetcher);
    await graph.reconcile(id);
    expect(fetcher.mock.calls[0]![0]).toBe("/api/graph/receipts/read");
    expect(fetcher.mock.calls.some(([path]) => path === request.path)).toBe(false);
    expect(graph.entries[id].pending).toEqual(request);
    expect(graph.session).toEqual(view);
    expect(graph.receiptChecks[id]).toContain("pending 证据保留");
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      operation: "candidate.select", parameters: { ...request.body, session_id: sid },
    });
  });

  it("verifies an original same-workflow fork receipt against its frozen candidate provenance", async () => {
    const { graph, id, sid, view, candidate, chain } = await fixture();
    const forkId = crypto.randomUUID();
    const fork = { ...view, workflow_session_id: forkId, revision: 1, head_revision: 1,
      selected_chain_run_id: chain, source: { kind: "fork_candidate", workflow_session_id: sid,
        candidate_commit_id: candidate } };
    const original = pendingCandidateFork(sid, view, candidate, chain);
    graph.entries[id].pending = graphClone(original);
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(false);
    expect(graph.entries[id].pending).toEqual(original);
    const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(String(init?.body));
        return graphReceiptReadResponse(envelope.operation, envelope.parameters, fork);
      }
      if (path === `/api/graph/definitions/${view.workflow_definition_id}/revisions/1`)
        return new Response(JSON.stringify(graph.document));
      return new Response(JSON.stringify(fork));
    });
    stubGraphApplicationFetch(fetcher);
    await graph.reconcile(id);
    expect(fetcher.mock.calls[0]![0]).toBe("/api/graph/receipts/read");
    expect(fetcher.mock.calls.some(([path]) => path.endsWith("/candidates/fork"))).toBe(false);
    expect(graph.session?.workflow_session_id).toBe(sid);
    expect(graph.entries[id].pending).toEqual(original);
    expect(graph.views[forkId]).toBeUndefined();
    expect(graph.receiptChecks[id]).toContain(forkId);
    expect(graph.views[sid]).toEqual(view);
  });

  it("keeps a fork pending when its otherwise valid receipt names another candidate", async () => {
    const { graph, id, sid, view, candidate, chain } = await fixture();
    const forkId = crypto.randomUUID();
    const original = pendingCandidateFork(sid, view, candidate, chain);
    graph.entries[id].pending = graphClone(original);
    const fork = { ...view, workflow_session_id: forkId, revision: 1, head_revision: 1,
      selected_chain_run_id: chain, source: { kind: "fork_candidate", workflow_session_id: sid,
        candidate_commit_id: crypto.randomUUID() } };
    const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(String(init?.body));
        return graphReceiptReadResponse(envelope.operation, envelope.parameters, fork);
      }
      return new Response(JSON.stringify(fork));
    });
    stubGraphApplicationFetch(fetcher);
    await graph.reconcile(id);
    expect(fetcher.mock.calls).toHaveLength(1);
    expect(fetcher.mock.calls[0]![0]).toBe("/api/graph/receipts/read");
    expect(graph.pending).toEqual(original);
    expect(graph.session?.workflow_session_id).toBe(sid);
  });

  it("keeps later fork-session observations unchanged after a frozen receipt read", async () => {
    const { graph, id, sid, view, candidate, chain } = await fixture();
    const forkId = crypto.randomUUID(), laterChain = crypto.randomUUID();
    const original = pendingCandidateFork(sid, view, candidate, chain);
    graph.entries[id].pending = graphClone(original);
    const fork = { ...view, workflow_session_id: forkId, revision: 1, head_revision: 1,
      selected_chain_run_id: chain, source: { kind: "fork_candidate", workflow_session_id: sid,
        candidate_commit_id: candidate } };
    const later = { ...fork, revision: 8, data_revision: 5, head_revision: 6,
      selected_chain_run_id: laterChain };
    graph.views[forkId] = later;
    const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/graph/receipts/read") {
        const envelope = JSON.parse(String(init?.body));
        return graphReceiptReadResponse(envelope.operation, envelope.parameters, fork);
      }
      if (path === `/api/graph/definitions/${view.workflow_definition_id}/revisions/1`)
        return new Response(JSON.stringify(graph.document));
      return new Response(JSON.stringify(later));
    });
    stubGraphApplicationFetch(fetcher);
    await graph.reconcile(id);
    expect(fetcher.mock.calls[0]![0]).toBe("/api/graph/receipts/read");
    expect(graph.pending).toEqual(original);
    expect(graph.session).toEqual(view);
    expect(graph.views[forkId]).toEqual(later);
  });
});
