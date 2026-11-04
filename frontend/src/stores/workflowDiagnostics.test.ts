import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, type GraphDocument, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
import { installDebtCleanupConfiguration } from "../testUtils/debtCleanupConfiguration";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { workflowPreviewFixture } from "../testUtils/workflowPreviewFixture";
import { WorkbenchApiError } from "../adapters/workbenchApi";

const textType: GraphNodeType = { component_id: "workflow.text", component_version: "1",
  display_name: "text", category: "content", config_schema: { type: "object" },
  default_config: {}, inputs: [], outputs: [], is_output: false, executable: true };

beforeEach(() => setActivePinia(createPinia()));
afterEach(() => {
  useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

async function configured() {
  const graph = useWorkflowGraphStore(); graph.catalog = [graphClone(textType)];
  const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
  const id = await graph.createWorkflow();
  await configuration.settled(id);
  const node = (await graph.addNode("workflow.text@1", { x: 0, y: 0 }))!;
  await configuration.settled(id);
  return { graph, configuration, id, node };
}

function sessionView(document: GraphDocument): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
    workflow_definition_id: document.workflow_definition_id, definition_revision: document.revision,
    revision: 1, data_revision: 0, head_revision: 1, status: "idle", can_submit: true,
    available_actions: ["close"], nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} } };
}

describe("round-3 preview, structured diagnostics and original run errors", () => {
  it("reads a version-2 read-only save preview with the accepted baseline and writes nothing", async () => {
    const { graph, configuration, id, node } = await configured();
    const requests: { path: string; body: Record<string, unknown> }[] = [];
    const occupantSession = crypto.randomUUID(), chain = crypto.randomUUID();
    vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
      requests.push({ path, body: JSON.parse(String(init?.body)) });
      return new Response(JSON.stringify(workflowPreviewFixture({
        baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: true,
          occupying_sessions: [{ workflow_session_id: occupantSession, active_chain_run_id: chain }],
          has_effective_session: true },
        changes: { categories: ["fixed"], would_outcome: "confirmation_required",
          would_require_confirmation: true, would_derive_after_confirmation: true, diagnostics: [] },
      })));
    }));
    const updateCalls = configuration.update.mock.calls.length;
    expect(await graph.previewConfiguration(id)).toBe(true);
    expect(requests).toHaveLength(1);
    expect(requests[0]!.path).toBe("/api/graph/queries");
    expect(requests[0]!.body).toMatchObject({ operation: "definition.preview",
      parameters: { workflow_definition_id: id } });
    expect((requests[0]!.body.parameters as { expected_edit_sequence: number }).expected_edit_sequence)
      .toBe(configuration.application.accepted(id)!.edit_sequence);
    expect(configuration.update.mock.calls.length).toBe(updateCalls);
    const state = graph.activePreview!;
    expect(state).toMatchObject({ busy: false, stale: false, error: "" });
    expect(state.preview).toMatchObject({ contract_version: 2, read_only: true, reserves_save_right: false,
      changes: { would_outcome: "confirmation_required", would_require_confirmation: true,
        would_derive_after_confirmation: true } });
    expect(state.preview!.baseline.occupying_sessions).toEqual([
      { workflow_session_id: occupantSession, active_chain_run_id: chain }]);
    // The preview describes the document it read; a later accepted edit marks it stale.
    expect(await graph.patchNode(node, { title: "changed after preview" }))
      .toMatchObject({ outcome: "accepted" });
    expect(graph.activePreview!.stale).toBe(true);
  });

  it("marks a preview stale when the edited document changes while the read is in flight", async () => {
    const { graph, id, node } = await configured();
    const gate: { resolve?: (value: Response) => void } = {};
    const fetcher = vi.fn(() => new Promise<Response>(resolve => { gate.resolve = resolve; }));
    vi.stubGlobal("fetch", fetcher);
    const previewing = graph.previewConfiguration(id);
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
    expect(graph.activePreview?.busy).toBe(true);
    expect(await graph.patchNode(node, { title: "changed during preview" }))
      .toMatchObject({ outcome: "accepted" });
    gate.resolve!(new Response(JSON.stringify(workflowPreviewFixture())));
    expect(await previewing).toBe(true);
    expect(graph.activePreview!.preview).not.toBeNull();
    expect(graph.activePreview!.stale).toBe(true);
  });

  it("keeps a preview failure with its backend diagnostics visible", async () => {
    const { graph, id } = await configured();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: { reason_code: "graph_missing_edge_node", message: "目标节点不存在",
        diagnostics: [{ reason_code: "graph_missing_edge_node", code: "graph_missing_edge_node",
          phase: "save", path: "/edges/0/target_node_id", message: "目标节点不存在",
          expected: "existing-node", actual: null }] } }), { status: 400 })));
    expect(await graph.previewConfiguration(id)).toBe(false);
    const state = graph.activePreview!;
    expect(state.preview).toBeNull();
    expect(state.error).toContain("数据边端点节点不存在");
    expect(state.failure?.code).toBe("graph_missing_edge_node");
    expect(state.failure?.diagnostics[0]).toMatchObject({ phase: "save",
      path: "/edges/0/target_node_id", expected: "existing-node", actual: null });
  });

  it("retains a G21 run-lock save rejection with occupying session and chain identities", async () => {
    const { graph, configuration, id, node } = await configured();
    const occupant = crypto.randomUUID(), chain = crypto.randomUUID();
    const error = new WorkbenchApiError("rejected",
      "工作流会话正在运行或被占用，配置修改被阻止 [HTTP 409 / configuration_run_locked]",
      409, "configuration_run_locked");
    error.diagnostics = [{ reason_code: "configuration_run_locked", code: "configuration_run_locked",
      message: "工作流会话正在运行", phase: "save", path: "/baseline/run_lock",
      occupying_sessions: [{ workflow_session_id: occupant, active_chain_run_id: chain }] }];
    configuration.update.mockRejectedValueOnce(error);
    expect(await graph.patchNode(node, { title: "blocked by run lock" }))
      .toMatchObject({ outcome: "failed" });
    const failure = graph.activeEditFailure!;
    expect(failure).toMatchObject({ workflowId: id, action: "edit", code: "configuration_run_locked" });
    expect(failure.diagnostics[0]).toMatchObject({ phase: "save", path: "/baseline/run_lock" });
    expect(failure.diagnostics[0]!.occupying_sessions).toEqual([
      { workflow_session_id: occupant, active_chain_run_id: chain }]);
    expect(useWorkbenchNoticesStore().error?.reason).toContain("configuration_run_locked");
    // A later accepted edit is new reality; the stale structured failure is retired.
    expect(await graph.patchNode(node, { title: "fixed" })).toMatchObject({ outcome: "accepted" });
    expect(graph.activeEditFailure).toBeNull();
  });

  it("keeps a failed edit and its original target identity when the view switches", async () => {
    const { graph, configuration, id, node } = await configured();
    const workspace = useWorkspaceStore();
    const error = new WorkbenchApiError("rejected",
      "工作流会话正在运行或被占用，配置修改被阻止 [HTTP 409 / configuration_run_locked]",
      409, "configuration_run_locked");
    error.diagnostics = [{ reason_code: "configuration_run_locked", code: "configuration_run_locked",
      message: "工作流会话正在运行", phase: "save", path: "/baseline/run_lock", node_id: node }];
    configuration.update.mockRejectedValueOnce(error);
    expect(await graph.patchNode(node, { title: "blocked" })).toMatchObject({ outcome: "failed" });
    const failure = graph.activeEditFailure!;
    expect(failure.targetIds).toContain(id);
    expect(failure.diagnostics[0]!.node_id).toBe(node);
    const other = await graph.createWorkflow();
    await configuration.settled(other);
    expect(workspace.activeWorkflowId).toBe(other);
    expect(graph.activeEditFailure).toBeNull();
    // Switching back shows the same error text and the original target identity,
    // without moving the failure onto an unrelated object.
    workspace.openWorkflow(id);
    expect(graph.activeEditFailure).toMatchObject({ workflowId: id, code: "configuration_run_locked" });
    expect(graph.activeEditFailure!.targetIds).toEqual([id]);
    expect(graph.activeEditFailure!.diagnostics[0]!.node_id).toBe(node);
  });

  it("retains the original prepare failure instead of showing a fake success or empty reset", async () => {
    installDebtCleanupEvidence();
    const graph = useWorkflowGraphStore(); graph.catalog = [graphClone(textType)];
    const id = await graph.createWorkflow();
    await graph.reconcile(id);
    const current = sessionView(graph.document!);
    graph.entries[id]!.saved_revision = current.definition_revision;
    graph.entries[id]!.saved_document = graphClone(graph.document!);
    graph.entries[id]!.session_id = current.workflow_session_id;
    graph.views[current.workflow_session_id] = current;
    const fetcher = vi.fn(async (path: string) => {
      if (path.endsWith(`/sessions/${current.workflow_session_id}`))
        return new Response(JSON.stringify(current));
      if (path.endsWith("/runs")) return new Response(JSON.stringify({
        error: { reason_code: "package_missing_dependency", message: "package dependency missing",
          diagnostics: [{ code: "package_missing_dependency", reason_code: "package_missing_dependency",
            phase: "prepare", path: "/package_lock/plugin", message: "缺少执行包依赖 plugin@2",
            expected: "plugin@2", actual: null }] } }), { status: 500 });
      throw new Error(`unexpected fixture path: ${path}`);
    });
    stubGraphApplicationFetch(fetcher);
    await graph.submitPrimary();
    const failure = graph.activeRunFailure!;
    expect(failure).toMatchObject({ workflowId: id, operation: "start", phase: "prepare",
      code: "package_missing_dependency", status: 500 });
    expect(failure.reason).toContain("执行包依赖缺失");
    expect(failure.diagnostics[0]).toMatchObject({ phase: "prepare",
      path: "/package_lock/plugin", expected: "plugin@2", actual: null });
    // The observed session is the original one, never a fabricated successful run.
    expect(graph.session?.status).toBe("idle");
    expect(graph.views[current.workflow_session_id]?.status).toBe("idle");
    expect(graph.views[current.workflow_session_id]?.chains).toEqual([]);
    graph.dismissRunFailure();
    expect(graph.activeRunFailure).toBeNull();
  });
});
