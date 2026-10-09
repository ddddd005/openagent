import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone, type GraphSession } from "../domain/workflowGraph";
import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";

function fixture(saved = true) {
  const pinia = createPinia(); setActivePinia(pinia);
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
  const id = graph.createWorkflow(), workspace = useWorkspaceStore(), original = graphClone(graph.document!);
  const view: GraphSession = { schema_version: 2, execution_model: "graph",
    workflow_definition_id: id, workflow_session_id: crypto.randomUUID(), definition_revision: 1,
    revision: 1, head_revision: 1, data_revision: 0, status: "idle", can_submit: true,
    nodes: [], outputs: [], chains: [], data: { revision: 0, values: {} } };
  if (saved) {
    graph.entries[id].saved_revision = 1; graph.entries[id].saved_document = graphClone(original);
    graph.entries[id].session_id = view.workflow_session_id; graph.views[view.workflow_session_id] = view;
    workspace.markWorkflowSaved(id);
  }
  return { pinia, graph, workspace, id, original, view };
}
const json = (value: unknown) => new Response(JSON.stringify(value));
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => {
  useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals();
});
describe("same-identity workflow revisions", () => {
  it("edits and publishes the same identity without copying or adopting a session revision", async () => {
    const f = fixture(), calls: string[] = [];
    stubGraphApplicationFetch(vi.fn(async (path, init) => {
      calls.push(path);
      if (path === "/api/graph/definitions") return json(JSON.parse(String(init.body)).document);
      if (path.endsWith("/rebind")) return json({ ...f.view, definition_revision: 2, revision: 2 });
      return json(f.view);
    }));
    expect(f.graph.renameWorkflow("Edited")).toBe(f.id);
    expect(f.workspace.workflows).toHaveLength(1); expect(f.graph.document?.revision).toBe(1);
    expect(f.graph.entries[f.id].saved_document).toEqual(f.original);
    expect(await f.graph.saveWorkflow()).toBe(true);
    expect(calls).toEqual(["/api/graph/definitions"]);
    expect(f.graph.entries[f.id].saved_revision).toBe(2); expect(f.graph.session?.definition_revision).toBe(1);
    await f.graph.submitPrimary(); expect(calls.some(path => path.endsWith("/runs"))).toBe(false);
    expect(await f.graph.adoptRevision()).toBe(true);
    expect(f.graph.session?.definition_revision).toBe(2);
    expect(f.graph.entries[f.id].session_document).toEqual(f.graph.entries[f.id].saved_document);
  });
  it.each(["prepared", "running", "pausing", "paused"])("refuses editing and adopting in %s even with can_submit true", async status => {
    const f = fixture();
    f.graph.views[f.view.workflow_session_id] = { ...f.view, status };
    expect(f.graph.renameWorkflow("blocked")).toBeNull(); expect(f.graph.document).toEqual(f.original);
    const fetcher = vi.fn(async () => json({ ...f.view, status })); stubGraphApplicationFetch(fetcher);
    expect(await f.graph.adoptRevision()).toBe(false);
    expect(fetcher.mock.calls.length).toBe(1);
  });
  it("retains the draft and original baseline after a publish CAS conflict", async () => {
    const f = fixture(); f.graph.renameWorkflow("Local draft");
    const edited = graphClone(f.graph.document);
    stubGraphApplicationFetch(vi.fn(async () => new Response(JSON.stringify({
      error: { reason_code: "stale_definition_revision" },
    }), { status: 409 })));
    expect(await f.graph.saveWorkflow()).toBe(false);
    expect(f.graph.document).toEqual(edited); expect(f.graph.entries[f.id].saved_document).toEqual(f.original);
    expect(f.graph.entries[f.id].saved_revision).toBe(1); expect(f.graph.pending).toBeNull();
  });
  it("does not downgrade a later observed publication when its own original save receipt arrives", async () => {
    const f = fixture(); f.graph.renameWorkflow("Local r2");
    stubGraphApplicationFetch(vi.fn(async (_path, init) => {
      f.graph.entries[f.id].latest_published_revision = 3;
      return json(JSON.parse(String(init.body)).document);
    }));
    expect(await f.graph.saveWorkflow()).toBe(true);
    expect(f.graph.entries[f.id].saved_revision).toBe(2);
    expect(f.graph.entries[f.id].latest_published_revision).toBe(3);
  });
  it("keeps the draft baseline after discovering a newer publication and rejects malformed restore metadata", async () => {
    const f = fixture(); f.graph.renameWorkflow("Local draft");
    stubGraphApplicationFetch(vi.fn(async () => json({ ...f.original, revision: 3, name: "Other window" })));
    expect(await f.graph.refreshPublishedRevision()).toBe(true);
    expect(f.graph.document?.name).toBe("Local draft"); expect(f.graph.entries[f.id].saved_revision).toBe(1);
    const snapshot = f.graph.storeSnapshot();
    expect(f.graph.restoreSnapshot(snapshot)).toBe(true);
    snapshot.entries[f.id].latest_published_revision = -1;
    expect(f.graph.restoreSnapshot(snapshot)).toBe(false);
  });
  it("discards modifications back to the published baseline and really removes an unpublished workflow", () => {
    const f = fixture(); f.graph.renameWorkflow("discard");
    f.graph.discardDraft(); expect(f.graph.document).toEqual(f.original);
    expect(f.workspace.activeWorkflow.state).toBe("saved"); expect(f.graph.session).toEqual(f.view);
    const unsaved = f.graph.createWorkflow(); f.graph.renameWorkflow("Unpublished");
    f.graph.discardDraft();
    expect(f.graph.entries[unsaved]).toBeUndefined();
    expect(f.workspace.activeWorkflowId).toBe(f.id);
    expect(f.workspace.workflows.some(row => row.id === unsaved)).toBe(false);
  });
  it("requires explicit publishing before session creation and run start", async () => {
    const f = fixture(false), fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect(await f.graph.createSession()).toBe(false); await f.graph.submitPrimary();
    expect(fetcher).not.toHaveBeenCalled();
    f.graph.discardDraft(); expect(f.workspace.workflows).toHaveLength(0);
    expect(f.workspace.activeWorkflowId).toBe(""); expect(f.graph.document).toBeNull();
  });
  it("undoes back to a clean baseline without manufacturing a new identity", () => {
    const f = fixture(); f.graph.renameWorkflow("First"); f.graph.renameWorkflow("Second");
    expect(f.workspace.activeWorkflowId).toBe(f.id);
    f.graph.undo(); f.graph.undo();
    expect(f.graph.document).toEqual(f.original); expect(f.workspace.activeWorkflow.state).toBe("saved");
  });
  it("does not redo or discard a persisted draft through a paused session", () => {
    const f = fixture(); f.graph.renameWorkflow("Draft"); f.graph.undo();
    f.graph.views[f.view.workflow_session_id] = { ...f.view, status: "running" };
    f.graph.undo(true);
    expect(f.graph.document).toEqual(f.original); expect(f.graph.canRedo).toBe(false);
    f.graph.views[f.view.workflow_session_id] = f.view; f.graph.undo(true);
    const edited = graphClone(f.graph.document);
    f.graph.views[f.view.workflow_session_id] = { ...f.view, status: "paused" };
    f.graph.discardDraft(); f.graph.undo();
    expect(f.graph.document).toEqual(edited);
  });
});
