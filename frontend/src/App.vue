<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  AppWindow,
  Circle,
  List,
  Save,
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
import { useBoundFormDraft } from "./composables/useBoundFormDraft";
import FormDraftNotice from "./components/FormDraftNotice.vue";
import { graphObject } from "./domain/workflowGraph";

const workspace = useWorkspaceStore();
const persistence = useWorkbenchPersistenceStore();
const graph = useWorkflowGraphStore();
const sidebarCollapsed = ref(false);
const navigationError = ref("");
let navigationGeneration = 0;
async function leaveEditor(change: () => void) {
  const token = ++navigationGeneration, workflowId = workspace.activeWorkflowId;
  if (!await persistence.save()) {
    navigationError.value = "当前编辑尚未写入本机，保留此页面";
    return;
  }
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
const nameDraft = useBoundFormDraft({
  id: () => graph.document ? `workflow:${graph.document.workflow_definition_id}:name` : "",
  basis: () => ({ workflowId: graph.document?.workflow_definition_id ?? null }),
  capture: () => ({ name: workflowName.value }),
  reset: () => { workflowName.value = graph.document?.name ?? ""; },
  restore: value => {
    if (!graphObject(value) || Object.keys(value).length !== 1 || typeof value.name !== "string")
      throw new Error("名称草稿格式不兼容，原文仍保留");
    workflowName.value = value.name;
  },
});
function openRename() { renaming.value = true; }
async function rename() {
  const context = nameDraft.context.value, name = workflowName.value;
  await leaveEditor(() => {
    if (context === nameDraft.context.value && name === workflowName.value
      && graph.renameWorkflow(name)) renaming.value = false;
  });
}
async function discard() {
  if (!window.confirm(graph.active?.saved_document ? "放弃当前图修改，恢复到草稿基准？未应用表单仍保留。" : "移除这个未发布工作流？不会删除已发布定义或会话。")) return;
  await persistence.discardDraft();
}
const savedLabel = computed(() => persistence.status === "blocked" ? "本机未保存"
  : persistence.status === "pending" ? "本机保存中" : "本机已保存");
const userInterfaceUrl = computed(() => graph.document ? graphChatInterfaceUrl(
  chatUiUrl, graph.document.workflow_definition_id, graph.active?.session_id ?? null,
) : null);
const chatInterfaceReady = computed(() => userInterfaceUrl.value !== null
  && Number(graph.active?.saved_revision) > 0 && !graph.active?.pending);
const chatInterfaceTitle = computed(() => {
  if (chatInterfaceReady.value) return "聊天前端";
  if (!userInterfaceUrl.value) return "聊天入口地址或工作流身份不可用";
  return graph.active?.pending ? "核实原请求后打开聊天前端" : "保存工作流后打开聊天前端";
});
async function changeWorkspaceView(view: "canvas" | "history") {
  await leaveEditor(() => { workspaceView.value = view; });
}

watch(
  () => [persistence.ready, workspace.activeWorkflowId] as const,
  ([ready, workflowId]) => {
    if (ready && persistence.status !== "blocked") void graph.activate(workflowId);
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
          <span
            v-if="!dirty"
            class="workbench-dirty-indicator"
            :class="{ 'is-dirty': dirty || persistence.status !== 'saved' }"
            :title="persistence.error ?? (persistence.savedAt ? `本机保存 ${persistence.savedAt}` : '尚未保存')"
          >
            <Circle :size="5" fill="currentColor" :stroke-width="0" />
            {{ savedLabel }}
          </span>
          <span v-else class="workflow-unsaved"><Square :size="11" fill="currentColor" :stroke-width="0" />未发布 · {{ savedLabel }}</span>
        </div>
        <div class="workbench-topbar-actions" aria-label="工作流配置">
          <div class="workbench-view-tabs" role="tablist" aria-label="工作区视图">
            <button class="workbench-tool-button" type="button" role="tab" title="主流程" aria-label="主流程" :aria-selected="workspaceView === 'canvas'" @click="changeWorkspaceView('canvas')"><Workflow :size="15" /></button>
            <button class="workbench-tool-button" type="button" role="tab" title="运行历史" aria-label="运行历史" :aria-selected="workspaceView === 'history'" :disabled="!graph.session" @click="changeWorkspaceView('history')"><List :size="15" /></button>
          </div>
          <button class="workbench-tool-button" type="button" title="保存工作流" aria-label="保存工作流" :disabled="persistence.status === 'blocked' || workspace.activeWorkflow.archived || workspace.activeWorkflow.deleted" @click="persistence.saveWorkflow()"><Save :size="15" /></button>
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
        <FormDraftNotice :message="nameDraft.message.value" :conflict="nameDraft.conflict.value"
          @restore="nameDraft.restoreConflict" @discard="nameDraft.discardConflict" />
        <label>工作流名称<input v-model="workflowName" :disabled="graph.locked || nameDraft.loading.value || nameDraft.conflict.value" required maxlength="128" aria-label="工作流名称" /></label>
        <button type="submit" :disabled="graph.locked || nameDraft.loading.value || nameDraft.conflict.value"><Save :size="14" />应用</button><button type="button" aria-label="取消修改名称" @click="renaming = false">取消</button>
      </form>
      <div v-if="navigationError" class="workbench-save-error" role="status">{{ navigationError }}</div>
      <div v-if="persistence.pendingCopy" class="workbench-save-error model-pending-banner" role="status">
        <span>工作流编辑副本等待会话复制确认</span>
        <button v-if="graph.entries[persistence.pendingCopy.id]?.pending" type="button" :disabled="!!graph.busy" @click="persistence.reconcileCopy()">核实复制</button>
        <button v-else-if="graph.canRetryRejectedCopy(persistence.pendingCopy.id)" type="button" :disabled="!!graph.busy" @click="graph.retryRejectedCopy(persistence.pendingCopy.id)">重试已拒绝复制</button>
        <span v-else>缺少原请求坐标，副本仍保留</span>
        <button v-if="graph.entries[persistence.pendingCopy.id] && !graph.entries[persistence.pendingCopy.id].pending" type="button" :disabled="!!graph.busy" @click="graph.discardRejectedCopy(persistence.pendingCopy.id)">放弃复制</button>
        <template v-for="diagnostic in graph.entries[persistence.pendingCopy.id]?.diagnostics ?? []" :key="diagnostic.node_id"><button v-if="diagnostic.node_id && diagnostic.reason_code.includes('state') && graph.canRetryRejectedCopy(persistence.pendingCopy.id)" type="button" :disabled="!!graph.busy" @click="graph.resetPrivateState(persistence.pendingCopy.id, diagnostic.node_id); graph.retryRejectedCopy(persistence.pendingCopy.id)">重建该节点私有状态并重试</button></template>
      </div>
      <div v-for="(entry, workflowId) in graph.entries" :key="workflowId" v-show="entry.pending" class="workbench-save-error model-pending-banner" role="status"><span>{{ workspace.workflows.find(w => w.id === workflowId)?.title }} · 原请求待核实</span><button type="button" :disabled="!!graph.busy" @click="graph.reconcile(String(workflowId))">核实原请求</button></div>
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
