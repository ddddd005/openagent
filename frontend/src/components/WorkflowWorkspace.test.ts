import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import WorkflowWorkspace from "./WorkflowWorkspace.vue";
import { createWorkflowConfigurationCopy } from "../application/workflowConfigurationCopy";
import { workflowConfigurationCopyOperation, workflowConfigurationCopyOwnerKind,
  type WorkflowConfigurationCopyRequest } from "../domain/workflowConfigurationCopy";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { installDebtCleanupConfiguration } from "../testUtils/debtCleanupConfiguration";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { workflowPreviewFixture } from "../testUtils/workflowPreviewFixture";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import type { GraphNodeType, GraphSession } from "../domain/workflowGraph";
import { graphClone } from "../domain/workflowGraph";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkbenchNoticesStore } from "../stores/workbenchNotices";

const editNodeType: GraphNodeType = { component_id: "sample.node", component_version: "1",
  display_name: "Sample", category: "Test", default_config: { text: "before" },
  config_schema: { type: "object", properties: { text: { type: "string" } } },
  inputs: [], outputs: [], executable: true, is_output: false };

vi.mock("./GraphRunControls.vue", async () => {
  const { h } = await import("vue");
  return { default: { render: () => h("span", { "data-region": "run-controls" }) } };
});
vi.mock("./GraphWorkbench.vue", async () => {
  const { h } = await import("vue");
  return { default: { render: () => h("section", { "data-region": "canvas" }) } };
});
vi.mock("./GraphHistory.vue", async () => {
  const { h } = await import("vue");
  return { default: { render: () => h("section", { "data-region": "history" }) } };
});

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { disposePinia(pinia); vi.restoreAllMocks(); });

async function render() {
  return await renderToString(createSSRApp(WorkflowWorkspace).use(pinia));
}

function stubCopyRows(graph: ReturnType<typeof useWorkflowGraphStore>,
  rows: { workflowId: string; title: string; error: string; busy: boolean; pending: boolean }[] = []) {
  return vi.spyOn(graph, "copyPendingRequests").mockReturnValue(rows.map(row =>
    ({ ...row, busy: false as const, progress: "restored" as const, requestKey: row.workflowId })));
}

function copyButton(html: string) {
  return /<button[^>]*aria-label="复制工作流"[^>]*>/.exec(html)?.[0] ?? "";
}

describe("workflow workspace ownership", () => {
  it("owns the workflow business topbar, view tabs, copy, discard and run controls", async () => {
    const graph = useWorkflowGraphStore();
    stubCopyRows(graph);
    await graph.createWorkflow();
    const html = await render();
    for (const label of ["工作流配置", "编辑工作流名称", "主流程", "运行历史",
      "复制工作流", "工作流设置尚未接入", "会话预加载数据", "配置尚未正式接纳"])
      expect(html).toContain(label);
    expect(html).toContain('data-region="run-controls"');
    expect(html).toContain('data-region="canvas"');
    expect(html).not.toContain('data-region="history"');
  });
  it("keeps original requests in top-level notices and a stable toolbar entry while the shell keeps legacy records", async () => {
    const graph = useWorkflowGraphStore();
    stubCopyRows(graph);
    await graph.createWorkflow();
    const html = await render();
    expect(html).toContain('aria-label="工作流原请求"');
    expect(html).not.toContain("model-pending-banner");
    const app = readFileSync(new URL("../App.vue", import.meta.url), "utf8");
    expect(app).toContain("旧原请求");
    for (const businessLabel of ["复制工作流", "编辑工作流名称", "运行历史", "再次只读核实"])
      expect(app).not.toContain(businessLabel);
  });
  it("shows the editable name form and discard action only for a dirty draft", async () => {
    const graph = useWorkflowGraphStore();
    stubCopyRows(graph);
    await graph.createWorkflow();
    const html = await render();
    expect(html).toContain("编辑工作流名称");
    expect(html).toContain("配置尚未正式接纳");
    expect(html).toContain("放弃工作流修改");
    expect(html).not.toContain('aria-label="取消修改名称"');
  });
  it("marks a formal fixed revision without current configuration as read-only", async () => {
    const graph = useWorkflowGraphStore();
    stubCopyRows(graph);
    const id = await graph.createWorkflow();
    const entry = graph.entries[id]!;
    entry.fixed_only = true;
    const html = await render();
    expect(html).toContain("固定修订 · 只读");
  });
  it("keeps copy available for a formally saved archived source (G-A)", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    stubCopyRows(graph);
    const id = await graph.createWorkflow();
    workspace.markWorkflowSaved(id);
    workspace.setWorkflowManagement(id, true);
    const html = await render();
    // Editing stays read-only, but copying the formal current remains allowed.
    expect(html).toContain("已归档 · 只读");
    expect(graph.locked).toBe(true);
    expect(graph.copyLocked).toBe(false);
    expect(copyButton(html)).not.toContain("disabled");
  });
  it("disables copy without a formally saved current", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    stubCopyRows(graph);
    const id = await graph.createWorkflow();
    expect(copyButton(await render())).toContain("disabled"); // local draft
    workspace.markWorkflowSaved(id);
    graph.entries[id]!.fixed_only = true;
    expect(copyButton(await render())).toContain("disabled"); // fixed-only source
    delete graph.entries[id]!.fixed_only;
    workspace.setWorkflowManagement(id, true, true);
    expect(copyButton(await render())).toContain("disabled"); // deletion still pending
  });
  it("disables copy from the new copy channel while a result is pending", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    const id = await graph.createWorkflow();
    workspace.markWorkflowSaved(id);
    const evidence = memoryRequestEvidence();
    const request: WorkflowConfigurationCopyRequest = {
      source_workflow_definition_id: id, expected_source_definition_revision: 1,
      expected_source_edit_sequence: 1, target_workflow_definition_id: crypto.randomUUID(),
      target_name: "副本", idempotency_key: crypto.randomUUID(),
    };
    await evidence.ports.write({ operation: workflowConfigurationCopyOperation,
      idempotency_key: request.idempotency_key, owner_kind: workflowConfigurationCopyOwnerKind,
      owner_id: id, request: request as unknown as Record<string, unknown> });
    graph.installWorkflowConfigurationCopyApplication(
      createWorkflowConfigurationCopy({ evidence: evidence.ports }));
    graph.copyPendingRequests(); // lazily restores the durable pending channel for the source
    await vi.waitFor(() => expect(graph.copyPendingRequests()[0]?.pending).toBe(true));
    expect(graph.copyLocked).toBe(true);
    const html = await render();
    expect(useWorkbenchNoticesStore().notices.some(row => row.reason.includes("配置复制原请求证据尚未结案"))).toBe(true);
    expect(copyButton(html)).toContain("disabled");
  });
  it("shows a persistent rejected-proposal state with view and dismiss actions", async () => {
    const graph = useWorkflowGraphStore();
    graph.catalog = [editNodeType];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    await configuration.settled(workflowId);
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError(
      "rejected", "工作流正在修改，请等待当前请求结束后按原基线重试", 409, "workflow_modification_busy"));
    expect(await graph.patchNode(nodeId, { config: { text: "rejected proposal" } }))
      .toMatchObject({ outcome: "failed" });
    const html = await render();
    // The failure stays visible in the workspace, independent of the five-second notice.
    expect(html).toContain("本次修改未生效（业务未发送）");
    expect(html).toContain("工作流正在修改");
    expect(html).toContain("查看失败提案");
    expect(html).toContain("放弃并关闭");
    expect(html).toContain("workflow-edit-state");
  });
  it("shows a persistent blocked-delete impact list with locations and a dismiss action", async () => {
    const graph = useWorkflowGraphStore();
    const targetType: GraphNodeType = { component_id: "test.block-target", component_version: "1",
      display_name: "阻止目标", category: "Test", default_config: {}, config_schema: { type: "object", properties: {} },
      inputs: [], outputs: [], executable: true, is_output: false };
    const consumerType: GraphNodeType = { component_id: "test.block-consumer", component_version: "1",
      display_name: "引用者", category: "Test", default_config: { target_id: "" },
      config_schema: { type: "object", properties: { target_id: { type: "string" } } },
      inputs: [], outputs: [], executable: true, is_output: false,
      config_node_references: [{ config_field: "target_id", multiple: false, allow_empty: true, target_types: [] }] };
    graph.catalog = [targetType, consumerType];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    const targetId = (await graph.addNode("test.block-target@1", { x: 0, y: 0 }))!;
    await configuration.settled(workflowId);
    const consumerId = (await graph.addNode("test.block-consumer@1", { x: 100, y: 0 }))!;
    await configuration.settled(workflowId);
    await graph.patchNode(consumerId, { config: { target_id: targetId } });
    graph.selectNodes([targetId]);
    expect(await graph.removeSelection()).toMatchObject({ outcome: "failed" });
    const html = await render();
    expect(html).toContain("删除被阻止：仍有存活引用");
    expect(html).toContain("引用者");
    expect(html).toContain("target_id");
    expect(html).toContain("/nodes/1/config/target_id");
    expect(html).toContain("关闭修改阻止提示");
    graph.dismissEditBlock();
    expect(await render()).not.toContain("删除被阻止");
  });
  it("renders a read-only preview with outcome, run conditions and G21 occupants", async () => {
    const graph = useWorkflowGraphStore();
    graph.catalog = [editNodeType];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    await graph.addNode("sample.node@1", { x: 0, y: 0 });
    await configuration.settled(workflowId);
    const occupant = crypto.randomUUID(), chain = crypto.randomUUID();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(workflowPreviewFixture({
      baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: true,
        occupying_sessions: [{ workflow_session_id: occupant, active_chain_run_id: chain }],
        has_effective_session: true },
    })))));
    expect(await graph.previewConfiguration(workflowId)).toBe(true);
    const html = await render();
    expect(html).toContain("保存预演（只读）");
    expect(html).toContain("不取得保存资格");
    expect(html).toContain("将正式保存");
    expect(html).toContain("已检查：执行包锁、定义修订");
    expect(html).toContain("未检查：资源占用");
    expect(html).toContain(`占用：会话 ${occupant} · 活动链 ${chain}`);
    expect(html).toContain("重新预演");
  });
  it("renders an invalid-structure preview as blocked with the located backend diagnostic", async () => {
    const graph = useWorkflowGraphStore();
    graph.catalog = [editNodeType];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    await graph.addNode("sample.node@1", { x: 0, y: 0 });
    await configuration.settled(workflowId);
    // Real backend payload: flat diagnostics use `code`, an undecidable change
    // classification omits the outcome, and a skipped compile omits its extras.
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      contract_version: 2, read_only: true, reserves_save_right: false,
      structure: { ok: false, diagnostics: [{ code: "graph_missing_edge_node",
        message: "数据边目标节点不存在", phase: "save", path: "/edges/0/target_node_id",
        node_id: crypto.randomUUID(), expected: "existing-node", actual: null }] },
      declared_references: { ok: true, diagnostics: [] },
      compilation: { ok: false, skipped: true, diagnostics: [] },
      baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: false,
        occupying_sessions: [], has_effective_session: false },
      changes: { categories: null, would_outcome: null, diagnostics: [] },
      run_conditions: { package_lock_available: true,
        checked: ["structure", "compilation", "package_lock_membership"],
        not_checked: ["G21 run lock re-check at submit"] },
      notes: ["preview writes nothing and reserves no save right"],
    }))));
    expect(await graph.previewConfiguration(workflowId)).toBe(true);
    const html = await render();
    expect(html).toContain("不满足保存结构/引用条件");
    expect(html).toContain("不可判定");
    expect(html).toContain("graph_missing_edge_node");
    expect(html).toContain("/edges/0/target_node_id");
    expect(html).toContain("已检查：保存结构、编译、执行包锁成员");
    expect(html).toContain("未检查：G21 run lock re-check at submit");
    expect(html).toContain("不取得保存资格");
  });
  it("renders a G21 save rejection with occupant session and chain identities", async () => {
    const graph = useWorkflowGraphStore();
    graph.catalog = [editNodeType];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    await configuration.settled(workflowId);
    const occupant = crypto.randomUUID(), chain = crypto.randomUUID();
    const error = new WorkbenchApiError("rejected",
      "工作流会话正在运行或被占用，配置修改被阻止", 409, "configuration_run_locked");
    error.diagnostics = [{ reason_code: "configuration_run_locked", code: "configuration_run_locked",
      message: "工作流会话正在运行", phase: "save", path: "/baseline/run_lock",
      occupying_sessions: [{ workflow_session_id: occupant, active_chain_run_id: chain }] }];
    configuration.update.mockRejectedValueOnce(error);
    expect(await graph.patchNode(nodeId, { title: "blocked" })).toMatchObject({ outcome: "failed" });
    const html = await render();
    expect(html).toContain("最近一次保存/编辑失败 · configuration_run_locked");
    expect(html).toContain("保存校验");
    expect(html).toContain("/baseline/run_lock");
    expect(html).toContain(`占用：会话 ${occupant} · 活动链 ${chain}`);
    expect(html).toContain("关闭保存失败提示");
  });
  it("renders the retained original prepare failure without a fake success", async () => {
    installDebtCleanupEvidence();
    const graph = useWorkflowGraphStore();
    graph.catalog = [editNodeType];
    const workflowId = await graph.createWorkflow();
    await graph.reconcile(workflowId);
    const view: GraphSession = { schema_version: 2, execution_model: "graph",
      workflow_session_id: crypto.randomUUID(), workflow_definition_id: workflowId,
      definition_revision: graph.document!.revision, revision: 1, data_revision: 0, head_revision: 1,
      status: "idle", can_submit: true, available_actions: ["close"], nodes: [], chains: [],
      outputs: [], data: { revision: 0, values: {} } };
    graph.entries[workflowId]!.saved_revision = view.definition_revision;
    graph.entries[workflowId]!.saved_document = graphClone(graph.document!);
    graph.entries[workflowId]!.session_id = view.workflow_session_id;
    graph.views[view.workflow_session_id] = view;
    stubGraphApplicationFetch(vi.fn(async (path: string) => {
      if (path.endsWith(`/sessions/${view.workflow_session_id}`))
        return new Response(JSON.stringify(view));
      if (path.endsWith("/runs")) return new Response(JSON.stringify({
        error: { reason_code: "package_missing_dependency", message: "package dependency missing",
          diagnostics: [{ code: "package_missing_dependency", reason_code: "package_missing_dependency",
            phase: "prepare", path: "/package_lock/plugin", message: "缺少执行包依赖 plugin@2",
            expected: "plugin@2", actual: null }] } }), { status: 500 });
      throw new Error(`unexpected fixture path: ${path}`);
    }));
    await graph.submitPrimary();
    const html = await render();
    expect(html).toContain("最近一次启动运行失败 · 运行准备");
    expect(html).toContain("执行包依赖缺失");
    expect(html).toContain("/package_lock/plugin");
    expect(html).toContain("原始错误响应");
    expect(graph.session?.status).toBe("idle");
  });
  it("presents a configuration-copy issue without inserting rows before the canvas", async () => {
    const graph = useWorkflowGraphStore();
    await graph.createWorkflow();
    stubCopyRows(graph, [{ workflowId: crypto.randomUUID(), title: "复制来源工作流",
      error: "原复制已确认，目标现不可用", busy: false, pending: true }]);
    const html = await render();
    expect(useWorkbenchNoticesStore().notices).toContainEqual(expect.objectContaining({
      title: "复制来源工作流", reason: "原复制已确认，目标现不可用", severity: "major",
    }));
    expect(html).not.toContain("原复制已确认，目标现不可用");
    expect(html).not.toContain("model-pending-banner");
    const app = readFileSync(new URL("../App.vue", import.meta.url), "utf8");
    expect(app).not.toContain("配置复制原请求待核实");
  });
  it("keeps an external formal-version conflict visible with explicit read and abandon actions", async () => {
    const graph = useWorkflowGraphStore();
    graph.catalog = [editNodeType];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    await configuration.settled(workflowId);
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError("rejected",
      "配置编辑依据已变化；请重新读取工作流", 409, "stale_configuration_basis"));
    expect(await graph.patchNode(nodeId, { title: "external conflict edit" }))
      .toMatchObject({ outcome: "failed" });
    // The conflict stays after the transient notice is dismissed, independent of it.
    const notices = useWorkbenchNoticesStore();
    notices.notices.splice(0, notices.notices.length);
    const html = await render();
    expect(html).toContain("正式版本已被外部修改（可能由外部 Agent 修改）");
    expect(html).toContain("stale_configuration_basis");
    expect(html).toContain("本地基线：编辑序");
    expect(html).toContain("请先重新读取正式配置，再决定合并或放弃");
    expect(html).toContain("重新读取正式配置");
    expect(html).toContain("放弃本地提案");
    expect(html).toContain("外部正式版本：编辑序");
    expect(html).toContain("本次修改未生效（正式版本已被外部修改）");
    expect(html).toContain("查看失败提案");

    // An explicit read adopts the external formal current and keeps the local proposal.
    const accepted = configuration.application.accepted(workflowId)!;
    vi.spyOn(graph.management.queries, "definition").mockResolvedValueOnce({
      ...graphClone(accepted), edit_sequence: accepted.edit_sequence + 1,
      revision: accepted.revision + 1, name: "外部 Agent 正式配置" } as never);
    expect(await graph.reloadAcceptedConfiguration(workflowId)).toBe(true);
    const resolved = await render();
    expect(resolved).toContain("已重新读取正式版本；本地未提交提案仍保留");
    expect(resolved).toContain("外部正式版本：编辑序");
    expect(graph.activeRejectedEdit).not.toBeNull();

    graph.dismissRejectedEdit();
    expect(await render()).not.toContain("正式版本已被外部修改");
  });
});
