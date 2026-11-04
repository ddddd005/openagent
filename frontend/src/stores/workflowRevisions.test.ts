import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { graphClone, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import { debtCleanupDerivationResult, installDebtCleanupConfiguration } from "../testUtils/debtCleanupConfiguration";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { useWorkbenchNoticesStore } from "./workbenchNotices";

const fixedType: GraphNodeType = { component_id: "plugin.revision", component_version: "1",
  display_name: "Fixed node", category: "Test", config_schema: {}, default_config: {},
  inputs: [], outputs: [], is_output: false, executable: true };
async function fixture(saved = true) {
  const pinia = createPinia(); setActivePinia(pinia);
  const graph = useWorkflowGraphStore();
  const id = await graph.createWorkflow(), workspace = useWorkspaceStore(), original = graphClone(graph.document!);
  await graph.reconcile(id);
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
beforeEach(() => { setActivePinia(createPinia()); installDebtCleanupEvidence(); });
afterEach(() => {
  useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
describe("same-identity workflow revisions", () => {
  it("derives a fixed edit without rebinding the old session and creates a fresh target session", async () => {
    const f = await fixture(false), calls: string[] = [], targetId = crypto.randomUUID();
    const configuration = installDebtCleanupConfiguration(f.graph, installDebtCleanupEvidence());
    await configuration.application.forWorkflow(f.id).initialize();
    await f.graph.renameWorkflow("Formal original");
    await configuration.settled(f.id);
    const original = graphClone(f.graph.document!);
    const originalSession = { ...f.view, definition_revision: original.revision };
    f.graph.entries[f.id].session_id = originalSession.workflow_session_id;
    f.graph.views[originalSession.workflow_session_id] = originalSession;
    f.graph.catalog = [fixedType];
    configuration.update.mockImplementation(async request => debtCleanupDerivationResult(request, targetId));
    const confirm = vi.fn(() => true);
    vi.stubGlobal("window", { confirm });
    // The fixed edit asks for a derivation instead of deriving on a global confirm.
    expect(await f.graph.addNode("plugin.revision@1", { x: 10, y: 20 })).toBeNull();
    expect(f.graph.activeDerivationAsk).not.toBeNull();
    expect(confirm).not.toHaveBeenCalled();
    expect(await f.graph.confirmDerivation()).toBe(true);
    await vi.waitFor(() => expect(f.workspace.activeWorkflowId).toBe(targetId));
    await f.graph.reconcile(targetId);
    const created: GraphSession = { ...f.view, workflow_definition_id: targetId,
      workflow_session_id: crypto.randomUUID(), definition_revision: f.graph.document!.revision };
    stubGraphApplicationFetch(vi.fn(async (path, init) => {
      calls.push(path);
      if (path === "/api/graph/sessions") return json(created);
      return json(originalSession);
    }));
    expect(f.workspace.workflows).toHaveLength(2);
    expect(f.graph.entries[f.id].saved_document).toEqual(original);
    expect(f.graph.views[originalSession.workflow_session_id]).toEqual(originalSession);
    expect(f.graph.entries[targetId].session_id).toBeNull();
    expect(await f.graph.createSession()).toBe(true);
    expect(f.graph.session?.workflow_session_id).toBe(created.workflow_session_id);
    expect(f.graph.session?.workflow_definition_id).toBe(targetId);
    expect(calls).toEqual(["/api/graph/sessions"]);
    expect(calls.some(path => path.endsWith("/rebind"))).toBe(false);
  });
  it.each(["prepared", "running", "pausing", "paused"])("refuses editing in %s even with can_submit true", async status => {
    const f = await fixture();
    f.graph.views[f.view.workflow_session_id] = { ...f.view, status };
    expect(await f.graph.renameWorkflow("blocked")).toMatchObject({ outcome: "failed" });
    expect(f.graph.document).toEqual(f.original);
    expect("adoptRevision" in f.graph).toBe(false);
    expect("adoptRevision" in f.graph.management.commands).toBe(false);
  });
  it("keeps the formal baseline and original proposed evidence after a real configuration CAS rejection", async () => {
    const f = await fixture(false), evidence = installDebtCleanupEvidence();
    const configuration = installDebtCleanupConfiguration(f.graph, evidence);
    await configuration.application.forWorkflow(f.id).initialize();
    await f.graph.renameWorkflow("Formal original");
    await configuration.settled(f.id);
    const original = graphClone(f.graph.document!);
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError(
      "rejected", "Configuration CAS conflict [stale_edit_sequence]", 409, "stale_edit_sequence"));
    f.graph.catalog = [fixedType];
    // The rejected edit does not claim a new node: the awaited entry reports the failure.
    expect(await f.graph.addNode("plugin.revision@1", { x: 10, y: 20 })).toBeNull();
    await vi.waitFor(() => expect(useWorkbenchNoticesStore().error?.reason).toContain("stale_edit_sequence"));
    await vi.waitFor(() => expect(f.graph.locked).toBe(false));
    expect(configuration.update).toHaveBeenCalledTimes(2);
    expect(f.graph.document).toEqual(original);
    expect(f.graph.entries[f.id].saved_document).toEqual(original);
    expect(configuration.application.forWorkflow(f.id).pending.value).toBeNull();
    const request = configuration.update.mock.calls[1]![0];
    expect(request.document.nodes[0]?.node_binding_id).toMatch(/^[0-9a-f-]{36}$/);
    expect([...evidence.records.values()].find(record => record.idempotency_key === request.idempotency_key))
      .toMatchObject({ request, resolution: "verified" });
  });
  it("does not downgrade a later observed publication when its own original save receipt arrives", async () => {
    const f = await fixture(false), configuration = installDebtCleanupConfiguration(f.graph, installDebtCleanupEvidence());
    await configuration.application.forWorkflow(f.id).initialize();
    await f.graph.renameWorkflow("Formal original");
    await configuration.settled(f.id);
    const accept = configuration.update.getMockImplementation()!;
    configuration.update.mockImplementationOnce(async request => {
      f.graph.entries[f.id].latest_published_revision = 3;
      return await accept(request);
    });
    f.graph.catalog = [fixedType];
    await f.graph.addNode("plugin.revision@1", { x: 10, y: 20 });
    await configuration.settled(f.id);
    expect(f.graph.entries[f.id].saved_revision).toBe(2);
    expect(f.graph.entries[f.id].latest_published_revision).toBe(3);
  });
  it("keeps a local baseline after a newer observation and rejects retired snapshot restoration", async () => {
    const f = await fixture(); await f.graph.renameWorkflow("Local draft");
    stubGraphApplicationFetch(vi.fn(async () => json([{
      workflow_definition_id: f.id, display_name: "Other window", fixed_definition_revision: 3,
      edit_sequence: 1, archived: false, node_count: 1, session_count: 1, has_current_configuration: true,
    }])));
    expect(await f.graph.refreshPublishedRevision()).toBe(true);
    expect(f.graph.document?.name).toBe("Local draft"); expect(f.graph.entries[f.id].saved_revision).toBe(1);
    const snapshot = f.graph.storeSnapshot();
    snapshot.entries[f.id].latest_published_revision = -1;
  });
  it("reads the latest fixed revision from definition summaries without downloading a document", async () => {
    const f = await fixture(), paths: string[] = [];
    let latest = 3;
    stubGraphApplicationFetch(vi.fn(async (path: string) => {
      paths.push(path);
      return json([{ workflow_definition_id: f.id, display_name: "Published", fixed_definition_revision: latest,
        edit_sequence: 1, archived: false, node_count: 1, session_count: 1, has_current_configuration: true }]);
    }));
    expect(await f.graph.refreshPublishedRevision()).toBe(true);
    expect(f.graph.entries[f.id].latest_published_revision).toBe(3);
    // A "latest" question always issues a fresh summary read; an older local summary is not reused.
    latest = 4;
    expect(await f.graph.refreshPublishedRevision()).toBe(true);
    expect(f.graph.entries[f.id].latest_published_revision).toBe(4);
    expect(paths).toEqual(["/api/graph/definitions", "/api/graph/definitions"]);
    expect(paths.some(path => path.includes("/revisions/"))).toBe(false);
  });
  it("keeps the previous fixed revision when the latest summary read fails", async () => {
    const f = await fixture();
    f.graph.entries[f.id].latest_published_revision = 2;
    const handler = vi.fn()
      .mockResolvedValueOnce(json([]))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        error: { reason_code: "internal_error", message: "summary unavailable" },
      }), { status: 500 }));
    stubGraphApplicationFetch(handler);
    // A missing summary row is a real failure, not "no newer revision".
    expect(await f.graph.refreshPublishedRevision()).toBe(false);
    expect(f.graph.entries[f.id].latest_published_revision).toBe(2);
    expect(await f.graph.refreshPublishedRevision()).toBe(false);
    expect(f.graph.entries[f.id].latest_published_revision).toBe(2);
    expect(handler).toHaveBeenCalledTimes(2);
    await vi.waitFor(() => expect(useWorkbenchNoticesStore().error?.requestId).toBe("读取最新已发布修订"));
    expect(useWorkbenchNoticesStore().error?.reason).toContain("internal_error");
  });
  it("reuses a successful frozen definition read by identity and never caches failures", async () => {
    const f = await fixture(), paths: string[] = [];
    let failure: Response | null = null;
    stubGraphApplicationFetch(vi.fn(async (path: string) => {
      paths.push(path);
      if (failure) return failure;
      const [, identity, revision] = /\/definitions\/([^/]+)\/revisions\/(\d+)$/.exec(path) ?? [];
      return json({ ...f.original, workflow_definition_id: identity, revision: Number(revision) });
    }));
    const otherIdentity = crypto.randomUUID();
    const first = await f.graph.readFrozenDefinition(f.id, 1);
    const second = await f.graph.readFrozenDefinition(f.id, 1);
    expect(paths.filter(path => path.includes(`/definitions/${f.id}/revisions/1`))).toHaveLength(1);
    // The same revision under a different formal identity is a separate cache entry.
    await f.graph.readFrozenDefinition(otherIdentity, 1);
    expect(paths.filter(path => path.includes(`/definitions/${otherIdentity}/revisions/1`))).toHaveLength(1);
    expect(second).toEqual(first);
    expect(second).not.toBe(first);
    failure = new Response(JSON.stringify({ error: { code: "internal_error", message: "unavailable" } }), { status: 500 });
    await expect(f.graph.readFrozenDefinition(f.id, 2)).rejects.toBeTruthy();
    failure = null;
    await f.graph.readFrozenDefinition(f.id, 2);
    expect(paths.filter(path => path.endsWith("/revisions/2"))).toHaveLength(2);
    await f.graph.readFrozenDefinition(f.id, 2);
    expect(paths.filter(path => path.endsWith("/revisions/2"))).toHaveLength(2);
  });
  it("bounds the frozen definition cache and evicts the oldest identity", async () => {
    const f = await fixture(), paths: string[] = [];
    stubGraphApplicationFetch(vi.fn(async (path: string) => {
      paths.push(path);
      const revision = Number(path.split("/revisions/").at(-1));
      return json({ ...f.original, workflow_definition_id: f.id, revision });
    }));
    for (let revision = 1; revision <= 9; revision++) await f.graph.readFrozenDefinition(f.id, revision);
    expect(paths).toHaveLength(9);
    // The cache holds the newest eight identities, so the first one is a real re-read.
    await f.graph.readFrozenDefinition(f.id, 1);
    expect(paths).toHaveLength(10);
    await f.graph.readFrozenDefinition(f.id, 9);
    expect(paths).toHaveLength(10);
  });
  it("discards only in-memory edits and removes an unsubmitted local workflow", async () => {
    const f = await fixture(); await f.graph.renameWorkflow("discard");
    f.graph.discardDraft(); expect(f.graph.document).toEqual(f.original);
    expect(f.workspace.activeWorkflow.state).toBe("saved"); expect(f.graph.session).toEqual(f.view);
    const unsaved = await f.graph.createWorkflow();
    await f.graph.reconcile(unsaved);
    await f.graph.renameWorkflow("Unpublished");
    f.graph.discardDraft();
    expect(f.graph.entries[unsaved]).toBeUndefined();
    expect(f.workspace.activeWorkflowId).toBe(f.id);
    expect(f.workspace.workflows.some(row => row.id === unsaved)).toBe(false);
  });
  it("does not create or run sessions from an unsubmitted in-memory document", async () => {
    const f = await fixture(false), fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect(await f.graph.createSession()).toBe(false); await f.graph.submitPrimary();
    expect(fetcher).not.toHaveBeenCalled();
    f.graph.discardDraft(); expect(f.workspace.workflows).toHaveLength(0);
    expect(f.workspace.activeWorkflowId).toBe(""); expect(f.graph.document).toBeNull();
  });
  it("undoes back to a clean baseline without manufacturing a new identity", async () => {
    const f = await fixture();
    await f.graph.renameWorkflow("First"); await f.graph.renameWorkflow("Second");
    expect(f.workspace.activeWorkflowId).toBe(f.id);
    await f.graph.undo(); await f.graph.undo();
    expect(f.graph.document).toEqual(f.original); expect(f.workspace.activeWorkflow.state).toBe("saved");
  });
  it("does not redo or discard a persisted draft through a paused session", async () => {
    const f = await fixture(); await f.graph.renameWorkflow("Draft"); await f.graph.undo();
    f.graph.views[f.view.workflow_session_id] = { ...f.view, status: "running" };
    await f.graph.undo(true);
    expect(f.graph.document).toEqual(f.original); expect(f.graph.canRedo).toBe(false);
    f.graph.views[f.view.workflow_session_id] = f.view; await f.graph.undo(true);
    const edited = graphClone(f.graph.document);
    f.graph.views[f.view.workflow_session_id] = { ...f.view, status: "paused" };
    f.graph.discardDraft(); await f.graph.undo();
    expect(f.graph.document).toEqual(edited);
  });
});
