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
  X,
} from "lucide-vue-next";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkbenchNoticesStore } from "../stores/workbenchNotices";
import GraphWorkbench from "./GraphWorkbench.vue";
import GraphRunControls from "./GraphRunControls.vue";
import GraphHistory from "./GraphHistory.vue";
import GraphSessionPreloads from "./GraphSessionPreloads.vue";
import { useVolatileFormState } from "../composables/useVolatileFormState";

const workspace = useWorkspaceStore();
const graph = useWorkflowGraphStore();
const workspaceView = ref<"canvas" | "history" | "preloads">("canvas");
const requestPanelOpen = ref(false);
type RequestIssue = { id: string; title: string; message: string; busy: boolean;
  pending: boolean; run?: () => unknown };
const requestIssues = computed<RequestIssue[]>(() => {
  const title = (id: string) => workspace.workflows.find(row => row.id === id)?.title ?? `工作流 ${id}`;
  const rows: RequestIssue[] = [
    ...Object.entries(graph.entries).flatMap(([id, entry]) => entry.pending && !graph.busy
      ? [{ id: `legacy:${id}:${entry.pending.body.idempotency_key}`, title: title(id), message: graph.receiptChecks[id] ?? "原请求待核实",
        busy: !!graph.busy, pending: true, run: () => graph.reconcile(id) }] : []),
    ...graph.configurationPendingRequests().map(row => ({
      id: `workflow-configuration:${row.workflowId}:definition.update:${row.requestKey}`, title: title(row.workflowId),
      message: row.error || "配置原请求证据尚未结案，业务结果尚未核实", busy: row.busy,
      pending: true, run: () => graph.reconcileConfiguration(row.workflowId),
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
      run: graph.supportedDiscoveredRequest(row) ? () => graph.reconcileDiscoveredRequest(row) : undefined });
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
function rename() {
  const context = nameForm.context.value, name = workflowName.value;
  if (context === nameForm.context.value && name === workflowName.value
    && graph.renameWorkflow(name)) renaming.value = false;
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
        <span class="workbench-topbar-divider" aria-hidden="true"></span>
        <GraphRunControls />
      </div>
    </header>
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
.workflow-workspace { display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
.request-count { min-width:12px; font-size:10px; }
.workflow-request-panel { position:fixed; z-index:390; top:52px; right:18px; width:420px; max-width:calc(100vw - 36px); max-height:calc(100vh - 70px); overflow:auto; background:#27272c; color:#eee; border:1px solid #64646e; box-shadow:0 4px 14px #0005; padding:12px; font-size:12px; }
.workflow-request-panel header { display:flex; align-items:center; gap:10px; }
.workflow-request-panel header strong { flex:1; }
.workflow-request-panel header button { display:flex; padding:4px; }
.workflow-request-row { display:grid; gap:7px; padding:12px 0; border-bottom:1px solid #505059; overflow-wrap:anywhere; }
.workflow-request-row button { justify-self:start; text-decoration:underline; }
.workflow-unsaved { display:flex;align-items:center;gap:6px;color:#e4c85f;font-size:11px;white-space:nowrap; }
.workflow-rename { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:8px 12px; border-bottom:1px solid #505059; font-size:12px; }
.workflow-rename label { display:flex; align-items:center; gap:8px; min-width:0; }
.workflow-rename input { min-width:0; padding:5px; background:#222226; border:1px solid #505059; color:inherit; }
.workflow-rename button { display:flex; align-items:center; gap:4px; padding:5px; }
</style>
