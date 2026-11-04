<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  List,
  Settings2,
  Variable,
  Workflow,
  Undo2,
  Square,
  Copy,
  Pencil,
  ListChecks,
  RefreshCw,
  FileSearch,
  X,
} from "lucide-vue-next";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkbenchNoticesStore } from "../stores/workbenchNotices";
import GraphWorkbench from "./GraphWorkbench.vue";
import GraphRunControls from "./GraphRunControls.vue";
import GraphHistory from "./GraphHistory.vue";
import GraphSessionPreloads from "./GraphSessionPreloads.vue";
import GraphDiagnosticList from "./GraphDiagnosticList.vue";
import { useVolatileFormState } from "../composables/useVolatileFormState";
import { diagnosticPhaseLabel, occupyingSessionLabels } from "../domain/diagnosticPresentation";

const workspace = useWorkspaceStore();
const graph = useWorkflowGraphStore();
const workspaceView = ref<"canvas" | "history" | "preloads">("canvas");
const requestPanelOpen = ref(false);
type RequestIssue = { id: string; title: string; message: string; busy: boolean;
  pending: boolean; run?: () => unknown; abandon?: () => unknown };
const requestIssues = computed<RequestIssue[]>(() => {
  const title = (id: string) => workspace.workflows.find(row => row.id === id)?.title ?? `工作流 ${id}`;
  const rows: RequestIssue[] = [
    ...graph.configurationPendingRequests().map(row => ({
      id: `workflow-configuration:${row.workflowId}:definition.update:${row.requestKey}`, title: title(row.workflowId),
      message: row.error || "配置原请求证据尚未结案，业务结果尚未核实", busy: row.busy,
      pending: true, run: () => graph.reconcileConfiguration(row.workflowId),
      abandon: () => confirmAbandon(row.workflowId),
    })),
    ...graph.managementPendingRequests().map(row => ({
      id: `workflow-management:${row.workflowId}:${row.requestOperation}:${row.requestKey}`, title: title(row.workflowId),
      message: row.error || "管理原请求证据尚未结案，业务结果尚未核实", busy: row.busy,
      pending: row.pending, run: row.pending ? () => graph.reconcile(row.workflowId) : undefined,
    })),
    ...graph.copyPendingRequests().map(row => ({
      id: `workflow-configuration-copy:${row.workflowId}:definition.copy:${row.requestKey}`, title: row.title,
      message: row.error || "配置复制原请求证据尚未结案，业务结果尚未核实", busy: row.busy,
      pending: row.pending, run: row.pending ? () => graph.reconcileCopy(row.workflowId) : undefined,
    })),
  ];
  for (const row of graph.discoveredPendingRequests()) {
    const identity = JSON.stringify([row.owner_kind, row.owner_id, row.operation, row.idempotency_key]);
    const localId = `${row.owner_kind}:${row.owner_id}:${row.operation}:${row.idempotency_key}`;
    const local = rows.find(issue => issue.id === localId);
    if (local) rows.splice(rows.indexOf(local), 1);
    rows.push({ id: `${localId}:${row.request_digest}`, title: title(row.owner_id),
      message: graph.recoveryErrors[identity] || local?.message || (graph.supportedDiscoveredRequest(row)
        ? `${row.operation} · 证据尚未结案，业务结果尚未核实`
        : `${row.operation} · 当前入口不支持核实此操作，原证据保留`),
      busy: graph.discoveredRequestBusy(row), pending: true,
      run: graph.supportedDiscoveredRequest(row) ? () => graph.reconcileDiscoveredRequest(row) : undefined,
      ...(row.owner_kind === "workflow-configuration" && row.operation === "definition.update"
        ? { abandon: () => confirmAbandon(row.owner_id) } : {}) });
  }
  if (graph.pendingDiscoveryError)
    rows.push({ id: "discovery", title: "工作流原请求发现失败", message: graph.pendingDiscoveryError,
      pending: false, busy: graph.pendingDiscoveryBusy, run: () => graph.discoverPendingRequests() });
  return rows;
});
const notices = useWorkbenchNoticesStore(), presentedIssues = new Map<string, string>();
watch(requestIssues, rows => {
  for (const row of rows) {
    const signature = JSON.stringify([row.title, row.message]);
    if (row.busy || presentedIssues.get(row.id) === signature) continue;
    presentedIssues.set(row.id, signature);
    notices.publish({ id: `workflow-request:${row.id}`, severity: row.pending ? "major" : "warning",
      title: row.title, reason: row.message, requestId: "",
      action: { label: "查看原请求", run: () => { requestPanelOpen.value = true; } } });
  }
}, { immediate: true });
const dirty = computed(() => workspace.activeWorkflow.state === "draft");
// S1-03: persistently visible edit outcome state for the active workflow. These rows
// stay until the user handles them; they are not the five-second notices.
const editStateError = ref("");
const showRejectedProposal = ref(false);
const rejectedEdit = computed(() => graph.activeRejectedEdit);
/** Q11-2: a formal current changed by another writer stays visible until handled. */
const externalConflict = computed(() => graph.activeExternalConflict);
const requestState = computed(() => graph.activeConfigurationRequest);
const derivationAsk = computed(() => graph.activeDerivationAsk);
const editBlock = computed(() => graph.activeEditBlock);
const editFailure = computed(() => graph.activeEditFailure);
const runFailure = computed(() => graph.activeRunFailure);
const previewState = computed(() => graph.activePreview);
const sessionLabels = computed<Record<string, string>>(() => Object.fromEntries(
  (graph.sessionRows[workspace.activeWorkflowId] ?? []).map(row =>
    [row.workflow_session_id, row.name || `会话 ${row.workflow_session_id.slice(0, 8)}`])));
const runFailureTitle = computed(() => {
  const operation = { start: "启动运行", control: "运行控制", agent: "运行操作", close: "关闭运行" }[
    runFailure.value?.operation ?? "control"] ?? "运行操作";
  return `最近一次${operation}失败`;
});
const runFailureRaw = computed(() => {
  const failure = runFailure.value;
  if (!failure) return "";
  return JSON.stringify({ reason: failure.reason, code: failure.code, status: failure.status,
    phase: failure.phase, diagnostics: failure.diagnostics }, null, 2);
});
const previewOutcomeLabels: Record<string, string> = {
  saved: "将正式保存", unchanged: "无变化", confirmation_required: "需要派生确认",
  derived: "将派生新工作流", blocked: "不满足保存条件", fixed: "按统一配置保存",
  adjustable: "按可调配置保存", declined: "将被拒绝", failed: "将失败",
};
function previewOutcomeText(outcome: string | null): string {
  return outcome ? previewOutcomeLabels[outcome] ?? outcome : "不可判定（保存结构或声明引用不通过）";
}
const previewCategoryLabels: Record<string, string> = {
  fixed: "统一配置", adjustable: "可调配置", display: "显示", prompt: "提示词", config: "节点配置",
  session: "会话", structure: "结构",
};
function previewCategoryText(categories: string[] | null): string {
  return categories?.length ? categories.map(category => previewCategoryLabels[category] ?? category).join("、")
    : categories ? "无" : "不可判定";
}
const runConditionLabels: Record<string, string> = {
  package_lock: "执行包锁", package_lock_membership: "执行包锁成员", run_lock: "运行锁/占用",
  definition_revision: "定义修订", baseline_counters: "基线计数", structure: "保存结构",
  declared_references: "声明引用", compilation: "编译", inputs: "运行输入",
  session: "会话状态", resources: "资源占用",
};
function runConditionText(value: string): string {
  return runConditionLabels[value] ?? value;
}
function previewOccupants(preview: NonNullable<typeof previewState.value>["preview"]): string[] {
  return preview ? occupyingSessionLabels(preview.baseline.occupying_sessions, sessionLabels.value) : [];
}
function previewDiagnostics(preview: NonNullable<typeof previewState.value>["preview"]) {
  if (!preview) return [];
  return [...preview.structure.diagnostics, ...preview.declared_references.diagnostics,
    ...(preview.compilation.diagnostics ?? []), ...(preview.changes.diagnostics ?? [])];
}
function previewSaveCondition(preview: NonNullable<typeof previewState.value>["preview"]): string {
  if (!preview) return "不可判定";
  return preview.structure.ok && preview.declared_references.ok
    ? "满足保存结构/引用条件（提交仍重新执行 CAS、G21 与资源检查）"
    : "不满足保存结构/引用条件；本次提交会被拒绝";
}
async function previewSave() {
  editStateError.value = "";
  await graph.previewConfiguration();
}
function dismissEditFailure() {
  editStateError.value = "";
  graph.dismissEditFailure();
}
function dismissRunFailure() {
  editStateError.value = "";
  graph.dismissRunFailure();
}
function dismissPreview() {
  editStateError.value = "";
  graph.dismissPreview();
}
// R4 (approved 2026-10-22): a normal save in flight is not a user-facing warning.
// The in-flight and business-confirmed transitions of an ordinary edit stay silent;
// only states that need the user to act or verify remain visible.
const TERMINAL_OR_PENDING = new Set(["evidence-unknown", "result-unknown",
  "acceptance-failed", "closure-failed"]);
const showRequestState = computed(() => {
  const state = requestState.value;
  return !!state && (TERMINAL_OR_PENDING.has(state.progress) || !!state.error);
});
function requestProgressText(progress: string): string {
  if (progress === "evidence-unknown") return "原请求证据写入结果未知：业务未发送，证据是否持久化待核实";
  if (progress === "request-in-flight") return "原请求正在提交，等待服务端结果";
  if (progress === "result-unknown") return "业务结果未知：未收到提交回执，保留原请求等待核实";
  if (progress === "business-confirmed") return "业务已确认，正在完成正式接纳与证据结案";
  if (progress === "acceptance-failed") return "保存已成功，当前视图接纳失败";
  if (progress === "closure-failed") return "保存已成功，证据待结案";
  if (progress === "abandoned") return "原请求已终止，不会再执行";
  return "原请求仍未结束";
}
// The whole edit-state block is a floating overlay: it must not take part in the page
// layout, otherwise every appear/disappear resizes the canvas (the reported vertical
// jitter). A new visible set auto-expands it so a failure is not hidden by a collapse.
const editStateOpen = ref(true);
const editStateVisible = computed(() => !!externalConflict.value || !!rejectedEdit.value
  || showRequestState.value || !!derivationAsk.value || !!editBlock.value || !!editFailure.value
  || !!runFailure.value || !!previewState.value || !!editStateError.value);
const editStateCount = computed(() => [externalConflict.value, rejectedEdit.value, derivationAsk.value,
  editBlock.value, editFailure.value, runFailure.value, previewState.value,
  showRequestState.value ? requestState.value : null, editStateError.value || null].filter(Boolean).length);
const editStateSignature = computed(() => [!!externalConflict.value, !!rejectedEdit.value,
  !!derivationAsk.value, !!editBlock.value, !!editFailure.value, !!runFailure.value,
  !!previewState.value, showRequestState.value, !!editStateError.value].map(flag => flag ? "1" : "0").join(""));
watch(editStateSignature, signature => { if (signature !== "000000000") editStateOpen.value = true; });
async function confirmAbandon(workflowId: string) {
  if (!window.confirm("结束等待将由服务端仲裁原请求：已提交则接纳正式结果；未提交则终止且不会再执行。继续吗？")) return;
  editStateError.value = "";
  if (!await graph.abandonConfiguration(workflowId))
    editStateError.value = "服务端暂无法仲裁该请求，结果仍未决；未解除阻塞，也未假称取消。";
}
async function retryClosure(workflowId = workspace.activeWorkflowId) {
  editStateError.value = "";
  if (!await graph.reconcileConfiguration(workflowId))
    editStateError.value = "重试结案未完成，原请求仍待核实。";
}
async function reloadAccepted(workflowId = workspace.activeWorkflowId) {
  editStateError.value = "";
  if (!await graph.reloadAcceptedConfiguration(workflowId))
    editStateError.value = "重新读取正式配置未完成，编辑仍被禁止。";
}
async function confirmDerived() {
  editStateError.value = "";
  if (!await graph.confirmDerivation())
    editStateError.value = "派生确认未完成或原提案基线已变化；请按提示重新编辑当前工作流。";
}
function declineDerived() {
  editStateError.value = "";
  if (!graph.declineDerivation()) editStateError.value = "没有可放弃的待确认派生请求。";
}
function dismissRejected() {
  showRejectedProposal.value = false;
  graph.dismissRejectedEdit();
}
/** Q11-2: abandon the retained local proposal plus the external-change record. */
function dismissExternalConflict() {
  showRejectedProposal.value = false;
  graph.dismissRejectedEdit();
  graph.dismissExternalConflict();
}
function dismissEditBlock() {
  editStateError.value = "";
  graph.dismissEditBlock();
}
function blockerLocation(item: NonNullable<typeof editBlock.value>["items"][number]): string {
  const parts: string[] = [item.kind];
  if (item.edgeId) parts.push(`连线 ${item.edgeId}`);
  if (item.portId) parts.push(`端口 ${item.portId}`);
  if (item.objectKey) parts.push(`对象 ${item.objectKey}`);
  return parts.join(" · ");
}
const navigationError = ref("");
let navigationGeneration = 0;
function leaveEditor(change: () => void) {
  const token = ++navigationGeneration, workflowId = workspace.activeWorkflowId;
  if (token !== navigationGeneration || workflowId !== workspace.activeWorkflowId) return;
  navigationError.value = "";
  change();
}
const renaming = ref(false), workflowName = ref("");
const nameForm = useVolatileFormState({
  id: () => graph.document ? `workflow:${graph.document.workflow_definition_id}:name` : "",
  basis: () => ({ workflowId: graph.document?.workflow_definition_id ?? null }),
  reset: () => { workflowName.value = graph.document?.name ?? ""; },
});
function openRename() { renaming.value = true; }
async function rename() {
  const context = nameForm.context.value, name = workflowName.value;
  if (context !== nameForm.context.value || name !== workflowName.value) return;
  navigationError.value = "";
  try {
    const result = await graph.renameWorkflow(name);
    if (result.outcome === "accepted") renaming.value = false;
    else if (result.outcome === "failed") navigationError.value = result.error.message;
  } catch (failure) {
    navigationError.value = failure instanceof Error ? failure.message : "工作流改名未完成";
  }
}
function discard() {
  if (!window.confirm(graph.active?.saved_document
    ? "移除尚未正式创建的工作流？"
    : "移除当前仅存在于内存中的工作流？")) return;
  graph.discardDraft();
}
function changeWorkspaceView(view: "canvas" | "history" | "preloads") {
  leaveEditor(() => { workspaceView.value = view; });
}
watch(
  () => workspace.viewVersion,
  () => {
    workspaceView.value = "canvas";
    renaming.value = false;
  },
);
</script>

<template>
  <section class="workflow-workspace" aria-label="工作流工作区">
    <header class="workbench-topbar">
      <div class="workbench-current-workflow">
        <Workflow :size="15" aria-hidden="true" />
        <h2>{{ workspace.activeWorkflow.title }}</h2>
        <span v-if="workspace.activeWorkflow.archived" class="workflow-unsaved">已归档 · 只读</span>
        <span v-if="graph.fixedOnly" class="workflow-unsaved">固定修订 · 只读</span>
        <span v-if="workspace.activeWorkflow.deleted" class="workflow-unsaved">已删除 · 原请求待核实</span>
        <button class="workbench-tool-button" type="button" title="工作流名称" aria-label="编辑工作流名称" :disabled="graph.locked || !graph.document" @click="openRename"><Pencil :size="13" /></button>
        <span v-if="dirty" class="workflow-unsaved" role="status">
          <Square :size="11" fill="currentColor" :stroke-width="0" />配置尚未正式接纳
        </span>
      </div>
      <div class="workbench-topbar-actions" aria-label="工作流配置">
        <button class="workbench-tool-button" type="button" title="工作流原请求" aria-label="工作流原请求"
          :aria-expanded="requestPanelOpen" @click="requestPanelOpen = !requestPanelOpen">
          <ListChecks :size="15" /><span v-if="requestIssues.length" class="request-count">{{ requestIssues.length }}</span>
        </button>
        <div class="workbench-view-tabs" role="tablist" aria-label="工作区视图">
          <button class="workbench-tool-button" type="button" role="tab" title="主流程" aria-label="主流程" :aria-selected="workspaceView === 'canvas'" @click="changeWorkspaceView('canvas')"><Workflow :size="15" /></button>
          <button class="workbench-tool-button" type="button" role="tab" title="运行历史" aria-label="运行历史" :aria-selected="workspaceView === 'history'" :disabled="!graph.session" @click="changeWorkspaceView('history')"><List :size="15" /></button>
        </div>
        <button class="workbench-tool-button" type="button" title="复制工作流" aria-label="复制工作流" :disabled="graph.copyLocked" @click="leaveEditor(() => graph.copyWorkflow())"><Copy :size="15" /></button>
        <button v-if="dirty" class="workbench-tool-button" type="button" title="放弃工作流修改" aria-label="放弃工作流修改" @click="discard"><Undo2 :size="15" /></button>
        <button
          class="workbench-tool-button"
          type="button"
          disabled
          title="工作流设置尚未接入"
          aria-label="工作流设置尚未接入"
        >
          <Settings2 :size="15" />
        </button>
        <button
          class="workbench-tool-button"
          type="button"
          :disabled="!graph.document"
          title="会话预加载数据"
          aria-label="会话预加载数据"
          :aria-pressed="workspaceView === 'preloads'"
          @click="changeWorkspaceView('preloads')"
        >
          <Variable :size="16" />
        </button>
        <button
          class="workbench-tool-button"
          type="button"
          :disabled="!graph.document || previewState?.busy === true"
          title="预演保存（只读，不写、不取得保存资格）"
          aria-label="预演保存"
          @click="previewSave"
        >
          <FileSearch :size="15" />
        </button>
        <span class="workbench-topbar-divider" aria-hidden="true"></span>
        <GraphRunControls />
      </div>
    </header>
    <section v-if="editStateVisible && editStateOpen" class="workflow-edit-state"
      aria-label="工作流修改状态" role="status">
      <header class="edit-state-header">
        <strong>工作流修改状态</strong><span>{{ editStateCount }} 项</span>
        <button type="button" aria-label="收起工作流修改状态" title="收起工作流修改状态"
          @click="editStateOpen = false">收起</button>
      </header>
      <div v-if="externalConflict" class="edit-state-row" aria-label="正式版本外部修改冲突">
        <strong>正式版本已被外部修改（可能由外部 Agent 修改）</strong>
        <span>{{ externalConflict.reason }}</span>
        <small>目标：{{ externalConflict.targetLabel }} · {{ externalConflict.code }}</small>
        <small>本地基线：编辑序 {{ externalConflict.localEditSequence ?? "—" }} ·
          修订 {{ externalConflict.localRevision ?? "—" }}；外部正式版本：编辑序
          {{ externalConflict.externalEditSequence ?? "重新读取后显示" }} ·
          修订 {{ externalConflict.externalRevision ?? "—" }}</small>
        <small v-if="externalConflict.resolved">已重新读取正式版本；本地未提交提案仍保留，请明确决定合并或放弃（不会自动重提）。</small>
        <small v-else>本地未提交提案已保留；请先重新读取正式配置，再决定合并或放弃（不会自动重提、不自动覆盖）。</small>
        <div class="edit-state-actions">
          <button type="button" aria-label="重新读取正式配置（处理外部变更）" @click="reloadAccepted()">重新读取正式配置</button>
          <button v-if="externalConflict.proposal && !rejectedEdit" type="button" aria-label="查看本地冲突提案"
            @click="showRejectedProposal = !showRejectedProposal">查看本地提案</button>
          <button type="button" aria-label="放弃外部冲突提案" @click="dismissExternalConflict">放弃本地提案</button>
          <button type="button" aria-label="关闭外部冲突提示" @click="graph.dismissExternalConflict()">关闭提示</button>
        </div>
        <pre v-if="showRejectedProposal && externalConflict.proposal && !rejectedEdit"
          aria-label="本地冲突提案正文">{{ JSON.stringify(externalConflict.proposal, null, 2) }}</pre>
      </div>
      <div v-if="runFailure" class="edit-state-row" aria-label="最近一次运行失败">
        <strong>{{ runFailureTitle }} · {{ diagnosticPhaseLabel(runFailure.phase) }}</strong>
        <span>{{ runFailure.reason }}</span>
        <small>目标：{{ runFailure.targetLabel }}{{ runFailure.code ? ` · ${runFailure.code}` : "" }}{{ runFailure.status ? ` · HTTP ${runFailure.status}` : "" }}</small>
        <GraphDiagnosticList :diagnostics="runFailure.diagnostics" :session-labels="sessionLabels" />
        <details>
          <summary>原始错误响应</summary>
          <pre aria-label="原始运行错误响应">{{ runFailureRaw }}</pre>
        </details>
        <div class="edit-state-actions">
          <button type="button" aria-label="关闭运行失败提示" @click="dismissRunFailure">关闭提示</button>
        </div>
      </div>
      <div v-if="editFailure" class="edit-state-row" aria-label="最近一次保存失败">
        <strong>最近一次保存/编辑失败 · {{ editFailure.code ?? "未分类" }}</strong>
        <span>{{ editFailure.reason }}</span>
        <small>目标：{{ editFailure.targetLabel }}{{ editFailure.status ? ` · HTTP ${editFailure.status}` : "" }}</small>
        <GraphDiagnosticList :diagnostics="editFailure.diagnostics" :session-labels="sessionLabels" />
        <div class="edit-state-actions">
          <button type="button" aria-label="关闭保存失败提示" @click="dismissEditFailure">关闭提示</button>
        </div>
      </div>
      <div v-if="previewState" class="edit-state-row" aria-label="保存预演">
        <strong>保存预演（只读）</strong>
        <span v-if="previewState.busy">正在读取正式预演…</span>
        <template v-else-if="!previewState.preview">
          <span>{{ previewState.error || "预演失败" }}<template v-if="previewState.failure?.code"> [{{ previewState.failure.code }}]</template><template v-if="previewState.failure?.status"> · HTTP {{ previewState.failure.status }}</template></span>
          <GraphDiagnosticList v-if="previewState.failure?.diagnostics?.length"
            :diagnostics="previewState.failure.diagnostics" :session-labels="sessionLabels" />
        </template>
        <template v-else>
          <span class="preview-disclaimer">只读预演：不写业务、证据或幂等记录，不取得保存资格；预演通过不代表提交或运行一定成功，实际提交仍重新执行 CAS、运行锁与资源检查。</span>
          <small v-if="previewState.stale" class="preview-stale">预演后当前编辑已变化；以下结果对应旧文档，请重新预演。</small>
          <small>目标：{{ previewState.targetLabel }}</small>
          <dl class="preview-facts">
            <dt>保存条件</dt><dd>{{ previewSaveCondition(previewState.preview) }}</dd>
            <dt>保存结构</dt><dd>{{ previewState.preview.structure.ok ? "通过" : "不通过" }}</dd>
            <dt>声明引用</dt><dd>{{ previewState.preview.declared_references.ok ? "通过" : "不通过" }}</dd>
            <dt>编译</dt><dd>{{ previewState.preview.compilation.skipped ? "已跳过" : previewState.preview.compilation.ok ? "通过" : "不通过" }} · 有序节点 {{ previewState.preview.compilation.ordered_node_count }}</dd>
            <dt>变更</dt><dd>{{ previewOutcomeText(previewState.preview.changes.would_outcome) }} · {{ previewCategoryText(previewState.preview.changes.categories) }}</dd>
            <dt>派生确认</dt><dd>{{ previewState.preview.changes.would_require_confirmation ? "需要派生确认" : "不需要派生确认" }}{{ previewState.preview.changes.would_derive_after_confirmation ? " · 确认后派生新工作流" : "" }}</dd>
            <dt>正式基线</dt><dd>配置{{ previewState.preview.baseline.configuration_exists ? "存在" : "缺失" }} · 编辑序{{ previewState.preview.baseline.edit_sequence_matches ? "匹配" : "不匹配" }} · 运行锁{{ previewState.preview.baseline.run_locked ? "占用中" : "未占用" }} · 有效会话{{ previewState.preview.baseline.has_effective_session ? "有" : "无" }}</dd>
            <dt>运行条件</dt><dd>执行包锁{{ previewState.preview.run_conditions.package_lock_available ? "可用" : "不可用" }} · 已检查：{{ previewState.preview.run_conditions.checked.map(runConditionText).join("、") || "无" }} · 未检查：{{ previewState.preview.run_conditions.not_checked.map(runConditionText).join("、") || "无" }}</dd>
          </dl>
          <ul v-if="previewOccupants(previewState.preview).length" class="preview-occupants">
            <li v-for="(occupant, index) in previewOccupants(previewState.preview)" :key="index">占用：{{ occupant }}</li>
          </ul>
          <GraphDiagnosticList v-if="previewDiagnostics(previewState.preview).length"
            :diagnostics="previewDiagnostics(previewState.preview)" :session-labels="sessionLabels" />
          <p v-for="(note, index) in previewState.preview.notes" :key="index" class="preview-note">说明：{{ note }}</p>
        </template>
        <div class="edit-state-actions">
          <button type="button" aria-label="重新预演保存" :disabled="previewState.busy" @click="previewSave">重新预演</button>
          <button type="button" aria-label="关闭保存预演" @click="dismissPreview">关闭预演</button>
        </div>
      </div>
      <div v-if="editBlock" class="edit-state-row" aria-label="工作流修改被阻止">
        <strong>{{ editBlock.title }}</strong>
        <span>{{ editBlock.summary }}</span>
        <small>目标：{{ editBlock.targetLabel }}</small>
        <ul class="edit-block-list">
          <li v-for="(item, index) in editBlock.items" :key="`${item.kind}:${index}:${item.edgeId ?? item.objectKey ?? item.path ?? ''}`">
            <span>{{ item.message }}</span>
            <small v-if="item.path"> · 路径 {{ item.path }}</small>
            <small v-if="item.targetNodeTitle || item.targetNodeId"> · 目标 {{ item.targetNodeTitle || '未知节点' }}{{ item.targetNodeId ? `（${item.targetNodeId}）` : '' }}</small>
            <small> · {{ blockerLocation(item) }}</small>
          </li>
        </ul>
        <div class="edit-state-actions">
          <button type="button" aria-label="关闭修改阻止提示" @click="dismissEditBlock">关闭提示</button>
        </div>
      </div>
      <div v-if="rejectedEdit" class="edit-state-row">
        <strong>{{ externalConflict ? "本次修改未生效（正式版本已被外部修改）" : "本次修改未生效（业务未发送）" }}</strong>
        <span>{{ rejectedEdit.reason }}</span>
        <small>原基线：edit_sequence {{ rejectedEdit.request.expected_edit_sequence }} ·
          修订 {{ rejectedEdit.request.document.revision }} · key {{ rejectedEdit.request.idempotency_key }}</small>
        <div class="edit-state-actions">
          <button type="button" aria-label="查看失败提案" @click="showRejectedProposal = !showRejectedProposal">查看失败提案</button>
          <button type="button" aria-label="放弃失败提案" @click="dismissRejected">放弃并关闭</button>
        </div>
        <pre v-if="showRejectedProposal" aria-label="失败提案正文">{{ JSON.stringify(rejectedEdit.request.document, null, 2) }}</pre>
      </div>
      <div v-if="derivationAsk" class="edit-state-row" aria-label="待确认派生修改">
        <strong>待确认：此固定配置修改需要创建新的工作流</strong>
        <span>原工作流及会话保持不变；确认时会复核原提案基线，基线过期不会自动派生。</span>
        <div class="edit-state-actions">
          <button type="button" aria-label="确认派生修改" @click="confirmDerived">确认派生</button>
          <button type="button" aria-label="放弃派生修改" @click="declineDerived">放弃</button>
        </div>
      </div>
      <div v-if="showRequestState" class="edit-state-row" aria-label="工作流原请求状态">
        <strong>{{ requestProgressText(requestState!.progress) }}</strong>
        <span>{{ requestState!.error || (requestState!.pending
          ? "原请求证据尚未结案；不会自动重发，也不会自动换 key。"
          : "原请求已由服务端确认；请按当前阶段完成接纳或结案。") }}</span>
        <div class="edit-state-actions">
          <button v-if="requestState!.acceptanceFailed || requestState!.progress === 'acceptance-failed'"
            type="button" aria-label="重新读取正式配置" @click="reloadAccepted()">重新读取正式配置</button>
          <button v-if="requestState!.pending" type="button" aria-label="再次只读核实"
            @click="retryClosure()">再次只读核实</button>
          <button v-if="requestState!.pending" type="button" aria-label="结束等待（仲裁）"
            @click="confirmAbandon(workspace.activeWorkflowId)">结束等待（仲裁）</button>
          <button v-if="requestState!.progress === 'closure-failed'" type="button" aria-label="重试结案"
            @click="retryClosure()">重试结案</button>
        </div>
      </div>
      <p v-if="editStateError" class="edit-state-error" role="alert">{{ editStateError }}</p>
    </section>
    <button v-else-if="editStateVisible" class="workflow-edit-state-pill" type="button"
      aria-label="展开工作流修改状态" @click="editStateOpen = true">修改状态 · {{ editStateCount }} 项（展开）</button>
    <form v-if="renaming" class="workflow-rename" @submit.prevent="rename">
      <label>工作流名称<input v-model="workflowName" :disabled="graph.locked" required maxlength="128" aria-label="工作流名称" /></label>
      <button type="submit" :disabled="graph.locked">保存名称</button><button type="button" aria-label="取消修改名称" @click="renaming = false">取消</button>
    </form>
    <div v-if="navigationError" class="workbench-save-error" role="status">{{ navigationError }}</div>
    <Teleport to="body">
      <aside v-if="requestPanelOpen" class="workflow-request-panel" aria-label="工作流原请求核实">
        <header><strong>工作流原请求</strong>
          <button type="button" title="刷新原请求" aria-label="刷新原请求" :disabled="graph.pendingDiscoveryBusy" @click="graph.discoverPendingRequests()"><RefreshCw :size="15" /></button>
          <button type="button" title="关闭原请求面板" aria-label="关闭原请求面板" @click="requestPanelOpen = false"><X :size="16" /></button>
        </header>
        <p v-if="!requestIssues.length">暂无待核实的原请求</p>
        <section v-for="request in requestIssues" :key="request.id" class="workflow-request-row">
          <strong>{{ request.title }}</strong><span>{{ request.message }}</span>
          <button v-if="request.run" type="button" :disabled="request.busy" @click="request.run()">再次只读核实</button>
          <button v-if="request.abandon" type="button" :disabled="request.busy"
            aria-label="结束等待仲裁" @click="request.abandon()">结束等待（仲裁）</button>
        </section>
        <button v-if="graph.pendingDiscoveryCursor" type="button" :disabled="graph.pendingDiscoveryBusy" @click="graph.discoverPendingRequests(true)">加载更多原请求</button>
      </aside>
    </Teleport>
    <div class="workbench-canvas-region">
      <GraphHistory v-if="workspaceView === 'history'" />
      <GraphSessionPreloads v-else-if="workspaceView === 'preloads'" />
      <GraphWorkbench v-else />
    </div>
  </section>
</template>

<style scoped>
.workflow-workspace { position: relative; display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
.request-count { min-width:12px; font-size:10px; }
.workflow-request-panel { position:fixed; z-index:390; top:52px; right:18px; width:420px; max-width:calc(100vw - 36px); max-height:calc(100vh - 70px); overflow:auto; background:#27272c; color:#eee; border:1px solid #64646e; box-shadow:0 4px 14px #0005; padding:12px; font-size:12px; }
.workflow-request-panel header { display:flex; align-items:center; gap:10px; }
.workflow-request-panel header strong { flex:1; }
.workflow-request-panel header button { display:flex; padding:4px; }
.workflow-request-row { display:grid; gap:7px; padding:12px 0; border-bottom:1px solid #505059; overflow-wrap:anywhere; }
.workflow-request-row button { justify-self:start; text-decoration:underline; }
.workflow-unsaved { display:flex;align-items:center;gap:6px;color:#e4c85f;font-size:11px;white-space:nowrap; }
/* Floating layers only: the edit state must never take part in the page layout, so a
   state change can never resize or move the canvas. */
.workflow-edit-state { position: absolute; z-index: 120; left: 10px; bottom: 10px; box-sizing: border-box; width: 560px; max-width: calc(100% - 20px); max-height: calc(100% - 60px); overflow: auto; display: grid; gap: 8px; padding: 8px 10px 10px; border: 1px solid #595963; border-radius: 4px; background: #2c2c31; box-shadow: 0 4px 14px #0006; font-size: 11px; color: #d7d7dd; }
.edit-state-header { position: sticky; top: -8px; display: flex; align-items: center; gap: 8px; margin: -8px -10px 0; padding: 8px 10px; background: #2c2c31; border-bottom: 1px solid #44444c; }
.edit-state-header strong { flex: 1; font-size: 12px; color: #e4c85f; }
.edit-state-header span { color: #a9a9b3; }
.edit-state-header button { flex: 0 0 auto; padding: 3px 8px; border: 1px solid #505059; border-radius: 3px; }
.workflow-edit-state-pill { position: absolute; z-index: 120; left: 10px; bottom: 10px; padding: 5px 10px; border: 1px solid #505059; border-radius: 3px; background: #2c2c31; color: #e4c85f; font-size: 11px; box-shadow: 0 3px 10px #0005; }
.edit-state-row { display:grid; gap:5px; min-width:0; }
.edit-state-row strong { font-size:12px; color:#e4c85f; }
.edit-state-row span, .edit-state-row small { overflow-wrap:anywhere; }
.edit-state-row pre { max-height:180px; overflow:auto; margin:0; padding:8px; background:#222226; font-size:10px; line-height:1.5; white-space:pre-wrap; }
.edit-state-actions { display:flex; flex-wrap:wrap; gap:6px; }
.edit-state-actions button { padding:4px 8px; border:1px solid #505059; border-radius:3px; }
.edit-block-list { margin:0; padding-left:16px; display:grid; gap:4px; }
.preview-disclaimer { color:#b8c4be; }
.preview-stale { color:#e4b59b; }
.preview-facts { display:grid; grid-template-columns:auto 1fr; gap:3px 10px; margin:0; overflow-wrap:anywhere; }
.preview-facts dt { color:#aaaab4; }
.preview-facts dd { margin:0; }
.preview-occupants { margin:0; padding-left:16px; color:#e4c85f; }
.preview-note { margin:0; color:#b8c4be; }
.edit-block-list li { overflow-wrap:anywhere; }
.edit-state-error { margin:0; color:#e4a0a0; overflow-wrap:anywhere; }
.workflow-rename { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:8px 12px; border-bottom:1px solid #505059; font-size:12px; }
.workflow-rename label { display:flex; align-items:center; gap:8px; min-width:0; }
.workflow-rename input { min-width:0; padding:5px; background:#222226; border:1px solid #505059; color:inherit; }
.workflow-rename button { display:flex; align-items:center; gap:4px; padding:5px; }
</style>
