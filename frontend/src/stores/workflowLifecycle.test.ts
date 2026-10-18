import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { graphClone, isGraphSession, newGraph, type GraphDocument, type GraphSession } from "../domain/workflowGraph";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { workbenchDraftAddress } from "../adapters/localDraftsApi";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
let pinia: Pinia;
let evidence: ReturnType<typeof memoryRequestEvidence>;
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia);
  evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
});
afterEach(() => { disposePinia(pinia); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function fixture() {
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
  const id = graph.createWorkflow(), document = graphClone(graph.document!);
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  document.revision = 1;
  graph.entries[id] = { document, saved_document: graphClone(document), saved_revision: 1,
    session_id: null, pending: null };
  workspace.markWorkflowSaved(id);
  return { graph, workspace, id, document };
}
function session(document: GraphDocument, status: GraphSession["status"] = "running"): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
    workflow_definition_id: document.workflow_definition_id, definition_revision: 1,
    revision: 2, data_revision: 0, head_revision: 1, status, can_submit: false,
    nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} }, available_actions: ["pause"] };
}
function result(id: string, action: "archive" | "delete", sid?: string) {
  return { schema_version: 1, kind: sid ? "workflow.session-management" : "workflow.definition-management",
    workflow_definition_id: id, ...(sid ? { workflow_session_id: sid } : {}),
    revision: sid ? 2 : 1, archived: action === "archive", deleted: action === "delete" };
}
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }); }
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

describe("object-scoped workflow lifecycle", () => {
  it("reads backend definitions including archives without losing an independent local draft", async () => {
    const { graph, workspace, id } = await fixture(), local = graph.createWorkflow();
    graph.document!.name = "Unpublished user edit";
    const archived = newGraph("Archived backend graph");
    stubGraphApplicationFetch(vi.fn(async path => {
      expect(path).toBe("/api/graph/definitions");
      return json([{ document: archived, archived: true }]);
    }));
    expect(await graph.loadDefinitions()).toBe(true);
    expect(workspace.workflows.find(row => row.id === archived.workflow_definition_id)?.archived).toBe(true);
    expect(graph.entries[archived.workflow_definition_id].saved_document).toEqual(archived);
    expect(graph.entries[local].document.name).toBe("Unpublished user edit");
    expect(graph.entries[id]).toBeDefined();
  });
  it("archives a running definition directly without publishing or flushing unrelated forms", async () => {
    const { graph, workspace, id, document } = await fixture(), view = session(document);
    graph.entries[id].session_id = view.workflow_session_id;
    graph.views[view.workflow_session_id] = view; graph.sessions[id] = [view];
    graph.document!.name = "Unpublished retained edit"; workspace.markWorkflowDraft(id);
    const paths: string[] = [];
    stubGraphApplicationFetch(vi.fn(async (path, options) => {
      paths.push(path);
      if (path.endsWith("/archive")) {
        expect(JSON.parse(options.body)).toMatchObject({ expected_revision: 1 });
        return json(result(id, "archive"));
      }
      return json(path.endsWith("/sessions") ? [] : {
        ...view, archived: true, readonly: true, can_submit: false, available_actions: [],
      });
    }));
    expect(await graph.manageWorkflow(id, "archive")).toBe(true);
    expect(paths).toEqual([`/api/graph/definitions/${id}/archive`, `/api/graph/definitions/${id}/sessions`,
      `/api/graph/sessions/${view.workflow_session_id}`]);
    expect(evidence.ports.write).toHaveBeenCalledOnce();
    expect(workspace.activeWorkflow.archived).toBe(true); expect(graph.locked).toBe(true);
    expect(graph.document!.name).toBe("Unpublished retained edit");
    expect(graph.views[view.workflow_session_id]).toMatchObject({ archived: true, readonly: true,
      can_submit: false, available_actions: [] });
    // A stale directory row must not clear the confirmed archive flag.
    stubGraphApplicationFetch(vi.fn(async path => {
      expect(path).toBe("/api/graph/definitions");
      return json([{ document: graphClone(document), archived: false }]);
    }));
    expect(await graph.loadDefinitions()).toBe(true);
    expect(workspace.workflows.find(row => row.id === id)?.archived).toBe(true);
  });
  it("keeps target content unchanged after an explicitly rejected archive", async () => {
    const { graph, workspace, id } = await fixture(), before = graphClone(graph.entries[id]);
    stubGraphApplicationFetch(vi.fn(async () => json({ detail: {
      reason_code: "archive_unavailable", message: "archive storage failed",
    } }, 409)));
    expect(await graph.manageWorkflow(id, "archive")).toBe(false);
    expect(workspace.activeWorkflow.archived).not.toBe(true);
    expect(graph.entries[id].pending).toBeNull();
    expect(graph.entries[id].document).toEqual(before.document);
    expect(useWorkbenchNoticesStore().error).not.toBeNull();
  });
  it("deletes only the selected definition and its local session/history projections", async () => {
    const { graph, workspace, id, document } = await fixture(), owned = session(document);
    graph.views[owned.workflow_session_id] = owned; graph.sessions[id] = [owned];
    const otherId = graph.createWorkflow(), other = graphClone(graph.entries[otherId]);
    const otherView = session(other.document);
    graph.views[otherView.workflow_session_id] = otherView; graph.sessions[otherId] = [otherView];
    stubGraphApplicationFetch(vi.fn(async (path, options) => {
      expect(path).toBe(`/api/graph/definitions/${id}/delete`);
      expect(JSON.parse(options.body)).toMatchObject({ identity: id, expected_revision: 1 });
      return json(result(id, "delete"));
    }));
    expect(await graph.manageWorkflow(id, "delete")).toBe(true);
    expect(graph.entries[id]).toBeUndefined(); expect(graph.history[id]).toBeUndefined();
    expect(graph.views[owned.workflow_session_id]).toBeUndefined(); expect(graph.sessions[id]).toBeUndefined();
    expect(graph.entries[otherId]).toEqual(other); expect(graph.views[otherView.workflow_session_id]).toEqual(otherView);
    expect(workspace.workflows.map(row => row.id)).toEqual([otherId]);
  });
  it("retains an unknown deletion in independent evidence and reconciles by receipt without replay", async () => {
    const { graph, workspace, id } = await fixture();
    const handler = vi.fn(async () => { throw new TypeError("lost deletion response"); });
    stubGraphApplicationFetch(handler);
    expect(await graph.manageWorkflow(id, "delete")).toBe(false);
    const request = graphClone(graph.pending!);
    expect(request.action).toBe("definition-delete"); expect(handler).toHaveBeenCalledOnce();
    const original = graphClone([...evidence.records.values()][0]);
    expect(original).toMatchObject({ operation: "definition.delete", owner_id: id, resolution: "pending" });
    const receipt = vi.fn(async (path, options) => {
      expect(path).toBe("/api/graph/receipts/read");
      const envelope = JSON.parse(options.body);
      expect(envelope).toEqual({ operation: "definition.delete",
        parameters: { ...request.body, identity: id } });
      return graphReceiptReadResponse(envelope.operation, envelope.parameters, result(id, "delete"));
    });
    vi.stubGlobal("fetch", receipt);
    await graph.reconcile(id);
    expect(receipt).toHaveBeenCalledOnce(); expect(graph.entries[id]).toBeUndefined();
    expect(workspace.workflows).toEqual([]);
    expect([...evidence.records.values()][0]).toEqual({ ...original, resolution: "verified" });
  });
  it("does not restore a workflow from a directory requested during deletion", async () => {
    const { graph, workspace, id } = await fixture();
    const stale = graphClone(graph.entries[id]!.document);
    const deletion = deferred<Response>(), directory = deferred<Response>(), requested = deferred<void>();
    stubGraphApplicationFetch(vi.fn(async path => {
      if (path === `/api/graph/definitions/${id}/delete`) return deletion.promise;
      if (path === "/api/graph/definitions") { requested.resolve(undefined); return directory.promise; }
      throw new Error(`Unexpected request: ${path}`);
    }));
    const deleting = graph.manageWorkflow(id, "delete");
    const loading = graph.loadDefinitions();
    await requested.promise;
    deletion.resolve(json(result(id, "delete")));
    expect(await deleting).toBe(true);
    directory.resolve(new Response(JSON.stringify([{ document: stale, archived: false }])));
    expect(await loading).toBe(false);
    expect(graph.entries[id]).toBeUndefined();
    expect(workspace.workflows).toEqual([]);
  });
  it("keeps a deleted definition and its verification owner when evidence closing fails", async () => {
    const { graph, workspace, id } = await fixture();
    stubGraphApplicationFetch(vi.fn(async path => {
      expect(path).toBe(`/api/graph/definitions/${id}/delete`);
      return json(result(id, "delete"));
    }));
    vi.spyOn(requestEvidencePorts, "resolve").mockRejectedValue(new Error("Evidence disk unavailable"));
    expect(await graph.manageWorkflow(id, "delete")).toBe(false);
    // The business receipt is confirmed, but the owner is not silently removed.
    expect(graph.entries[id]).toBeDefined();
    expect(workspace.workflows.find(row => row.id === id)?.deleted).toBe(true);
    expect(graph.managementPendingRequests()).toEqual([
      expect.objectContaining({ workflowId: id, pending: true }),
    ]);
    expect([...evidence.records.values()][0]).toMatchObject({
      operation: "definition.delete", owner_id: id, resolution: "pending",
    });

    // A directory row for the confirmed deletion must not clear `deleted`,
    // recreate the entry, or hide the owner while evidence is still pending.
    stubGraphApplicationFetch(vi.fn(async path => {
      expect(path).toBe("/api/graph/definitions");
      return json([{ document: graphClone(graph.entries[id]!.document), archived: false }]);
    }));
    expect(await graph.loadDefinitions()).toBe(true);
    expect(graph.entries[id]).toBeDefined();
    expect(workspace.workflows.find(row => row.id === id)?.deleted).toBe(true);
    expect(graph.managementPendingRequests()).toEqual([
      expect.objectContaining({ workflowId: id, pending: true }),
    ]);

    vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
    const original = graphClone([...evidence.records.values()][0]);
    const receipt = vi.fn(async (path, options) => {
      expect(path).toBe("/api/graph/receipts/read");
      const envelope = JSON.parse(options.body);
      expect(envelope).toEqual({ operation: "definition.delete",
        parameters: { ...graph.pending!.body, identity: id } });
      return graphReceiptReadResponse(envelope.operation, envelope.parameters, result(id, "delete"));
    });
    vi.stubGlobal("fetch", receipt);
    await graph.reconcile(id);
    expect(graph.entries[id]).toBeUndefined();
    expect(workspace.workflows).toEqual([]);
    expect([...evidence.records.values()][0]).toEqual({ ...original, resolution: "verified" });
  });
  it("archives a running session and keeps the resulting selected session read-only", async () => {
    const { graph, id, document } = await fixture(), view = session(document), sid = view.workflow_session_id;
    graph.entries[id].session_id = sid; graph.views[sid] = view; graph.sessions[id] = [view];
    const archived = { ...view, archived: true, readonly: true, can_submit: false, available_actions: [] };
    const calls: string[] = [];
    stubGraphApplicationFetch(vi.fn(async path => {
      calls.push(path);
      if (path.endsWith("/archive")) return json(result(id, "archive", sid));
      if (path.endsWith("/sessions")) return json([archived]);
      return json(calls.length < 3 ? view : archived);
    }));
    expect(await graph.manageSession(sid, "archive")).toBe(true);
    expect(graph.session).toMatchObject({ archived: true, readonly: true, can_submit: false });
    expect(graph.editorLocked).toBe(true);
    expect("writeData" in graph).toBe(false);
    const count = calls.length; await graph.submitPrimary(); expect(calls).toHaveLength(count);
    expect(isGraphSession({ ...archived, can_submit: true }, id)).toBe(false);
  });
  it("deletes only the target session, retaining its workflow and sibling session", async () => {
    const { graph, id, document } = await fixture(), target = session(document), sibling = session(document);
    graph.entries[id].session_id = target.workflow_session_id;
    graph.views[target.workflow_session_id] = target; graph.views[sibling.workflow_session_id] = sibling;
    graph.sessions[id] = [target, sibling];
    stubGraphApplicationFetch(vi.fn(async path => json(path.endsWith("/delete")
      ? result(id, "delete", target.workflow_session_id) : target)));
    expect(await graph.manageSession(target.workflow_session_id, "delete")).toBe(true);
    expect(graph.entries[id].session_id).toBeNull(); expect(graph.entries[id].saved_revision).toBe(1);
    expect(graph.views[target.workflow_session_id]).toBeUndefined();
    expect(graph.sessions[id]).toEqual([sibling]); expect(graph.views[sibling.workflow_session_id]).toEqual(sibling);
  });
  it("stores deletion evidence independently and leaves the legacy workbench record unwritten", async () => {
    const backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(undefined, backend.ports);
    const { graph, workspace, id } = await fixture();
    stubGraphApplicationFetch(vi.fn(async () => json(result(id, "delete"))));
    expect(await graph.manageWorkflow(id, "delete")).toBe(true);
    expect(persistence.status).toBe("ready");
    expect(backend.ports.read).toHaveBeenCalledWith(workbenchDraftAddress);
    expect(backend.ports.save).not.toHaveBeenCalled();
    expect(backend.records.size).toBe(0);
    expect(workspace.workflows).toEqual([]);
    expect([...evidence.records.values()][0]).toMatchObject({
      operation: "definition.delete", owner_id: id, resolution: "verified",
    });
  });
});
