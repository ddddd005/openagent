<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  AppWindow,
  List,
  Settings2,
  Variable,
  Workflow,
  Undo2,
  Square,
  Copy,
  Pencil,
} from "lucide-vue-next";
import ErrorToast from "./components/ErrorToast.vue";
import WorkbenchSidebar from "./components/WorkbenchSidebar.vue";
import { chatUiUrl, graphChatInterfaceUrl } from "./adapters/chatUi";
import { useWorkspaceStore } from "./stores/workspace";
import { useWorkbenchPersistenceStore } from "./stores/workbenchPersistence";
import { useWorkflowGraphStore } from "./stores/workflowGraph";
import GraphWorkbench from "./components/GraphWorkbench.vue";
import GraphRunControls from "./components/GraphRunControls.vue";
import GraphHistory from "./components/GraphHistory.vue";
import { useVolatileFormState } from "./composables/useVolatileFormState";
import { useWorkbenchFrontendHost } from "./composables/useWorkbenchFrontendHost";

const workspace = useWorkspaceStore();
const persistence = useWorkbenchPersistenceStore();
const graph = useWorkflowGraphStore();
useWorkbenchFrontendHost();
const sidebarCollapsed = ref(false);
const navigationError = ref("");
let navigationGeneration = 0;
function leaveEditor(change: () => void) {
  const token = ++navigationGeneration, workflowId = workspace.activeWorkflowId;
  if (token !== navigationGeneration || workflowId !== workspace.activeWorkflowId) return;
  navigationError.value = "";
  change();
}
void persistence.initialize();
const workspaceView = ref<"canvas" | "history">("canvas");
const dirty = computed(
  () => workspace.activeWorkflow.state === "draft",
);
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
const userInterfaceUrl = computed(() => graph.document ? graphChatInterfaceUrl(
  chatUiUrl, graph.document.workflow_definition_id, graph.active?.session_id ?? null,
) : null);
const chatInterfaceReady = computed(() => userInterfaceUrl.value !== null
  && Number(graph.active?.saved_revision) > 0 && !graph.pending);
const chatInterfaceTitle = computed(() => {
  if (chatInterfaceReady.value) return "聊天前端";
  if (!userInterfaceUrl.value) return "聊天入口地址或工作流身份不可用";
  return graph.pending ? "核实原请求后打开聊天前端" : "正式工作流保存暂不可用";
});
function changeWorkspaceView(view: "canvas" | "history") {
  leaveEditor(() => { workspaceView.value = view; });
}

watch(
  () => [persistence.ready, workspace.activeWorkflowId] as const,
  ([ready, workflowId]) => {
    if (ready && workflowId) void graph.activate(workflowId);
  },
  { immediate: true },
);
watch(
  () => workspace.viewVersion,
  () => {
    workspaceView.value = "canvas";
    renaming.value = false;
  },
);
</script>

<template>
  <div class="workbench-shell" :class="{ 'is-sidebar-collapsed': sidebarCollapsed }">
    <WorkbenchSidebar @collapsed="sidebarCollapsed = $event" />

    <main class="workbench-main" aria-label="工作流工作台">
      <header class="workbench-topbar">
        <div class="workbench-current-workflow">
          <Workflow :size="15" aria-hidden="true" />
          <h2>{{ workspace.activeWorkflow.title }}</h2>
          <span v-if="workspace.activeWorkflow.archived" class="workflow-unsaved">已归档 · 只读</span>
          <span v-if="workspace.activeWorkflow.deleted" class="workflow-unsaved">已删除 · 原请求待核实</span>
          <button class="workbench-tool-button" type="button" title="工作流名称" aria-label="编辑工作流名称" :disabled="graph.locked || !graph.document" @click="openRename"><Pencil :size="13" /></button>
          <span v-if="dirty" class="workflow-unsaved" role="status">
            <Square :size="11" fill="currentColor" :stroke-width="0" />配置尚未正式接纳
          </span>
        </div>
        <div class="workbench-topbar-actions" aria-label="工作流配置">
          <div class="workbench-view-tabs" role="tablist" aria-label="工作区视图">
            <button class="workbench-tool-button" type="button" role="tab" title="主流程" aria-label="主流程" :aria-selected="workspaceView === 'canvas'" @click="changeWorkspaceView('canvas')"><Workflow :size="15" /></button>
            <button class="workbench-tool-button" type="button" role="tab" title="运行历史" aria-label="运行历史" :aria-selected="workspaceView === 'history'" :disabled="!graph.session" @click="changeWorkspaceView('history')"><List :size="15" /></button>
          </div>
          <button class="workbench-tool-button" type="button" title="复制工作流" aria-label="复制工作流" :disabled="graph.locked" @click="leaveEditor(() => graph.copyWorkflow())"><Copy :size="15" /></button>
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
            disabled
            title="全局变量注册尚未接入"
            aria-label="全局变量注册尚未接入"
          >
            <Variable :size="16" />
          </button>
          <span class="workbench-topbar-divider" aria-hidden="true"></span>
          <GraphRunControls />
        </div>
      </header>
      <div v-if="persistence.error" class="workbench-save-error" role="status">{{ persistence.error }}</div>
      <form v-if="renaming" class="workflow-rename" @submit.prevent="rename">
        <label>工作流名称<input v-model="workflowName" :disabled="graph.locked" required maxlength="128" aria-label="工作流名称" /></label>
        <button type="submit" :disabled="graph.locked">保存名称</button><button type="button" aria-label="取消修改名称" @click="renaming = false">取消</button>
      </form>
      <div v-if="navigationError" class="workbench-save-error" role="status">{{ navigationError }}</div>
      <div v-if="persistence.unreadableLegacyRequests" class="workbench-save-error" role="status">
        {{ persistence.unreadableLegacyRequests }} 条旧原请求无法安全解析；旧记录已保留，未重发
      </div>
      <div v-if="persistence.unreadableLegacyRecord" class="workbench-save-error" role="status">
        旧工作台记录无法按只读方式解析；原记录保留，未恢复编辑内容或重发请求
      </div>
      <div v-for="request in persistence.legacyRequests" :key="request.key" class="workbench-save-error model-pending-banner" role="status">
        <span>旧原请求 · {{ request.workflowId }} · {{ request.result ?? request.error ?? (request.status === 'checking' ? '正在只读核实' : '待核实') }}</span>
        <button type="button" :disabled="request.status === 'checking'" @click="persistence.verifyLegacyRequest(request.key)">
          {{ request.status === 'verified' ? '再次核实' : '核实原请求' }}
        </button>
      </div>
      <div v-for="(entry, workflowId) in graph.entries" :key="workflowId" v-show="entry.pending" class="workbench-save-error model-pending-banner" role="status">
        <span>{{ workspace.workflows.find(w => w.id === workflowId)?.title ?? workflowId }} · {{ graph.receiptChecks[String(workflowId)] ?? "原请求待核实" }}</span>
        <button type="button" :disabled="!!graph.busy" @click="graph.reconcile(String(workflowId))">再次只读核实</button>
      </div>
      <div v-for="request in graph.configurationPendingRequests()" :key="request.workflowId" class="workbench-save-error model-pending-banner" role="status">
        <span>{{ workspace.workflows.find(w => w.id === request.workflowId)?.title ?? request.workflowId }} · {{ request.error || "配置原请求待核实" }}</span>
        <button type="button" :disabled="request.busy" @click="graph.reconcileConfiguration(request.workflowId)">再次只读核实</button>
      </div>
      <div v-for="request in graph.managementPendingRequests()" :key="`management:${request.workflowId}`" class="workbench-save-error model-pending-banner" role="status">
        <span>{{ workspace.workflows.find(w => w.id === request.workflowId)?.title ?? request.workflowId }} · {{ request.error || "管理原请求待核实" }}</span>
        <button v-if="request.pending" type="button" :disabled="request.busy" @click="graph.reconcile(request.workflowId)">只读核实原管理请求</button>
      </div>
      <div class="workbench-canvas-region">
        <GraphHistory v-if="workspaceView === 'history'" />
        <GraphWorkbench v-else />
      </div>
    </main>

    <nav class="workbench-interface-rail" aria-label="工作区切换">
      <a
        class="workbench-tool-button"
        :href="chatInterfaceReady ? userInterfaceUrl ?? undefined : undefined"
        :aria-disabled="!chatInterfaceReady"
        target="_blank"
        rel="noopener noreferrer"
        :title="chatInterfaceTitle"
        aria-label="打开聊天前端"
      >
        <AppWindow :size="18" />
      </a>
    </nav>
    <ErrorToast />
  </div>
</template>

<style scoped>
.model-pending-banner { display: flex; flex-wrap:wrap; align-items: center; gap: 8px 15px; }
.model-pending-banner span { min-width:0; overflow-wrap:anywhere; }
.model-pending-banner button { flex-shrink:0; font-size: 11px; text-decoration: underline; }
.workflow-unsaved { display:flex;align-items:center;gap:6px;color:#e4c85f;font-size:11px;white-space:nowrap; }
.workflow-rename { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:8px 12px; border-bottom:1px solid #505059; font-size:12px; }
.workflow-rename label { display:flex; align-items:center; gap:8px; min-width:0; }
.workflow-rename input { min-width:0; padding:5px; background:#222226; border:1px solid #505059; color:inherit; }
.workflow-rename button { display:flex; align-items:center; gap:4px; padding:5px; }
</style>
