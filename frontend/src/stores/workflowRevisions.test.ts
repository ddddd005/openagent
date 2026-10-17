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
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true);
  const id = graph.createWorkflow(), workspace = useWorkspaceStore(), original = graphClone(graph.document!);
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
    f.graph.renameWorkflow("Formal original");
    await configuration.settled(f.id);
    const original = graphClone(f.graph.document!);
    const originalSession = { ...f.view, definition_revision: original.revision };
    f.graph.entries[f.id].session_id = originalSession.workflow_session_id;
    f.graph.views[originalSession.workflow_session_id] = originalSession;
    f.graph.catalog = [fixedType];
    configuration.update.mockImplementation(async request => debtCleanupDerivationResult(request, targetId));
    vi.stubGlobal("window", { confirm: vi.fn(() => true) });
    f.graph.addNode("plugin.revision@1", { x: 10, y: 20 });
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
    expect(f.graph.renameWorkflow("blocked")).toBeNull(); expect(f.graph.document).toEqual(f.original);
    expect("adoptRevision" in f.graph).toBe(false);
    expect("adoptRevision" in f.graph.management.commands).toBe(false);
  });
  it("keeps the formal baseline and original proposed evidence after a real configuration CAS rejection", async () => {
    const f = await fixture(false), evidence = installDebtCleanupEvidence();
    const configuration = installDebtCleanupConfiguration(f.graph, evidence);
    await configuration.application.forWorkflow(f.id).initialize();
    f.graph.renameWorkflow("Formal original");
    await configuration.settled(f.id);
    const original = graphClone(f.graph.document!);
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError(
      "rejected", "Configuration CAS conflict [stale_edit_sequence]", 409, "stale_edit_sequence"));
    f.graph.catalog = [fixedType];
    const nodeId = f.graph.addNode("plugin.revision@1", { x: 10, y: 20 });
    await vi.waitFor(() => expect(useWorkbenchNoticesStore().error?.reason).toContain("stale_edit_sequence"));
    await vi.waitFor(() => expect(f.graph.locked).toBe(false));
    expect(configuration.update).toHaveBeenCalledTimes(2);
    expect(f.graph.document).toEqual(original);
    expect(f.graph.entries[f.id].saved_document).toEqual(original);
    expect(configuration.application.forWorkflow(f.id).pending.value).toBeNull();
    const request = configuration.update.mock.calls[1]![0];
    expect(request.document.nodes[0]?.node_binding_id).toBe(nodeId);
    expect([...evidence.records.values()].find(record => record.idempotency_key === request.idempotency_key))
      .toMatchObject({ request, resolution: "verified" });
  });
  it("does not downgrade a later observed publication when its own original save receipt arrives", async () => {
    const f = await fixture(false), configuration = installDebtCleanupConfiguration(f.graph, installDebtCleanupEvidence());
    await configuration.application.forWorkflow(f.id).initialize();
    f.graph.renameWorkflow("Formal original");
    await configuration.settled(f.id);
    const accept = configuration.update.getMockImplementation()!;
    configuration.update.mockImplementationOnce(async request => {
      f.graph.entries[f.id].latest_published_revision = 3;
      return await accept(request);
    });
    f.graph.catalog = [fixedType];
    f.graph.addNode("plugin.revision@1", { x: 10, y: 20 });
    await configuration.settled(f.id);
    expect(f.graph.entries[f.id].saved_revision).toBe(2);
    expect(f.graph.entries[f.id].latest_published_revision).toBe(3);
  });
  it("keeps a local baseline after a newer observation and rejects retired snapshot restoration", async () => {
    const f = await fixture(); f.graph.renameWorkflow("Local draft");
    stubGraphApplicationFetch(vi.fn(async () => json({ ...f.original, revision: 3, name: "Other window" })));
    expect(await f.graph.refreshPublishedRevision()).toBe(true);
    expect(f.graph.document?.name).toBe("Local draft"); expect(f.graph.entries[f.id].saved_revision).toBe(1);
    const snapshot = f.graph.storeSnapshot();
    expect(f.graph.restoreSnapshot(snapshot)).toBe(false);
    snapshot.entries[f.id].latest_published_revision = -1;
    expect(f.graph.restoreSnapshot(snapshot)).toBe(false);
  });
  it("discards only in-memory edits and removes an unsubmitted local workflow", async () => {
    const f = await fixture(); f.graph.renameWorkflow("discard");
    f.graph.discardDraft(); expect(f.graph.document).toEqual(f.original);
    expect(f.workspace.activeWorkflow.state).toBe("saved"); expect(f.graph.session).toEqual(f.view);
    const unsaved = f.graph.createWorkflow();
    await f.graph.reconcile(unsaved);
    f.graph.renameWorkflow("Unpublished");
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
    const f = await fixture(); f.graph.renameWorkflow("First"); f.graph.renameWorkflow("Second");
    expect(f.workspace.activeWorkflowId).toBe(f.id);
    f.graph.undo(); f.graph.undo();
    expect(f.graph.document).toEqual(f.original); expect(f.workspace.activeWorkflow.state).toBe("saved");
  });
  it("does not redo or discard a persisted draft through a paused session", async () => {
    const f = await fixture(); f.graph.renameWorkflow("Draft"); f.graph.undo();
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
