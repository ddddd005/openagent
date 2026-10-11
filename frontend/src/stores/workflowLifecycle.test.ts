import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { graphClone, isGraphSession, newGraph, type GraphDocument, type GraphSession } from "../domain/workflowGraph";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { useFormDraftsStore } from "./formDrafts";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { workbenchDraftAddress } from "../adapters/localDraftsApi";
let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { disposePinia(pinia); vi.unstubAllGlobals(); });
function fixture() {
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), persist = vi.fn(() => true);
  graph.setPersistenceGuard(persist);
  const id = graph.createWorkflow(), document = graphClone(graph.document!);
  document.revision = 1;
  graph.entries[id] = { document, saved_document: graphClone(document), saved_revision: 1,
    session_id: null, pending: null };
  workspace.markWorkflowSaved(id);
  return { graph, workspace, id, document, persist };
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

describe("object-scoped workflow lifecycle", () => {
  it("reads backend definitions including archives without losing an independent local draft", async () => {
    const { graph, workspace, id } = fixture(), local = graph.createWorkflow();
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
    const { graph, workspace, id, document, persist } = fixture(), view = session(document);
    graph.entries[id].session_id = view.workflow_session_id;
    graph.views[view.workflow_session_id] = view; graph.sessions[id] = [view];
    graph.document!.name = "Unpublished retained edit"; workspace.markWorkflowDraft(id);
    const forms = useFormDraftsStore(), flush = vi.spyOn(forms, "flushAll").mockResolvedValue(false);
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
    expect(flush).not.toHaveBeenCalled(); expect(persist).toHaveBeenCalled();
    expect(workspace.activeWorkflow.archived).toBe(true); expect(graph.locked).toBe(true);
    expect(graph.document!.name).toBe("Unpublished retained edit");
    expect(graph.views[view.workflow_session_id]).toMatchObject({ archived: true, readonly: true,
      can_submit: false, available_actions: [] });
  });
  it("keeps target content unchanged after an explicitly rejected archive", async () => {
    const { graph, workspace, id } = fixture(), before = graphClone(graph.storeSnapshot());
    stubGraphApplicationFetch(vi.fn(async () => json({ detail: {
      reason_code: "archive_unavailable", message: "archive storage failed",
    } }, 409)));
    expect(await graph.manageWorkflow(id, "archive")).toBe(false);
    expect(workspace.activeWorkflow.archived).not.toBe(true);
    expect(graph.entries[id].pending).toBeNull();
    expect(graph.entries[id].document).toEqual(before.entries[id].document);
    expect(useWorkbenchNoticesStore().error).not.toBeNull();
  });
  it("deletes only the selected definition and its local session/history projections", async () => {
    const { graph, workspace, id, document } = fixture(), owned = session(document);
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
  it("retains an unknown deletion, reopens the same original, and reconciles by receipt without replay", async () => {
    const { graph, workspace, id } = fixture();
    const handler = vi.fn(async () => { throw new TypeError("lost deletion response"); });
    stubGraphApplicationFetch(handler);
    expect(await graph.manageWorkflow(id, "delete")).toBe(false);
    const request = graphClone(graph.entries[id].pending!), snapshot = graph.storeSnapshot(),
      rows = graphClone(workspace.workflows);
    expect(request.action).toBe("definition-delete"); expect(handler).toHaveBeenCalledOnce();
    disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
    const reopenedWorkspace = useWorkspaceStore(), reopened = useWorkflowGraphStore();
    reopenedWorkspace.restoreCatalog(rows, id, id);
    expect(reopened.restoreSnapshot(snapshot)).toBe(true); reopened.setPersistenceGuard(() => true);
    const receipt = vi.fn(async (path, options) => {
      expect(path).toBe("/api/graph/receipts/read");
      const envelope = JSON.parse(options.body);
      expect(envelope).toEqual({ operation: "definition.delete",
        parameters: { ...request.body, identity: id } });
      return graphReceiptReadResponse(envelope.operation, envelope.parameters, result(id, "delete"));
    });
    vi.stubGlobal("fetch", receipt);
    await reopened.reconcile(id);
    expect(receipt).toHaveBeenCalledOnce(); expect(reopened.entries[id]).toBeUndefined();
    expect(reopenedWorkspace.workflows).toEqual([]);
  });
  it("archives a running session and keeps the resulting selected session read-only", async () => {
    const { graph, id, document } = fixture(), view = session(document), sid = view.workflow_session_id;
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
    expect(await graph.writeData({ changed: "not allowed" })).toBe(false);
    const count = calls.length; await graph.submitPrimary(); expect(calls).toHaveLength(count);
    expect(isGraphSession({ ...archived, can_submit: true }, id)).toBe(false);
  });
  it("deletes only the target session, retaining its workflow and sibling session", async () => {
    const { graph, id, document } = fixture(), target = session(document), sibling = session(document);
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
  it("persists management evidence separately from peer business form flushing, including a deleted-empty directory", async () => {
    const backend = memoryLocalDraftStorage(), persistence = useWorkbenchPersistenceStore();
    await persistence.initialize(undefined, backend.ports);
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), id = workspace.activeWorkflowId;
    graph.entries[id].saved_revision = 1; graph.entries[id].saved_document = graphClone(graph.document!);
    workspace.markWorkflowSaved(id);
    const flush = vi.spyOn(useFormDraftsStore(), "flushAll").mockResolvedValue(false);
    stubGraphApplicationFetch(vi.fn(async () => json(result(id, "delete"))));
    expect(await graph.manageWorkflow(id, "delete")).toBe(true);
    expect(flush).not.toHaveBeenCalled(); expect(persistence.status).toBe("saved");
    expect(backend.records.get(backend.key(workbenchDraftAddress))?.value).toMatchObject({
      catalog: [], activeWorkflowId: "", selectedWorkflowId: "", graph: { entries: {} },
    });
  });
});
