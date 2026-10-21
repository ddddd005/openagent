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
} from "lucide-vue-next";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import GraphWorkbench from "./GraphWorkbench.vue";
import GraphRunControls from "./GraphRunControls.vue";
import GraphHistory from "./GraphHistory.vue";
import { useVolatileFormState } from "../composables/useVolatileFormState";

const workspace = useWorkspaceStore();
const graph = useWorkflowGraphStore();
const workspaceView = ref<"canvas" | "history">("canvas");
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
function changeWorkspaceView(view: "canvas" | "history") {
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
    <form v-if="renaming" class="workflow-rename" @submit.prevent="rename">
      <label>工作流名称<input v-model="workflowName" :disabled="graph.locked" required maxlength="128" aria-label="工作流名称" /></label>
      <button type="submit" :disabled="graph.locked">保存名称</button><button type="button" aria-label="取消修改名称" @click="renaming = false">取消</button>
    </form>
    <div v-if="navigationError" class="workbench-save-error" role="status">{{ navigationError }}</div>
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
    <div v-for="request in graph.copyPendingRequests()" :key="`copy:${request.workflowId}`" class="workbench-save-error model-pending-banner" role="status">
      <span>{{ request.title }} · {{ request.error || "配置复制原请求待核实" }}</span>
      <button v-if="request.pending" type="button" :disabled="request.busy" @click="graph.reconcileCopy(request.workflowId)">再次只读核实</button>
    </div>
    <div class="workbench-canvas-region">
      <GraphHistory v-if="workspaceView === 'history'" />
      <GraphWorkbench v-else />
    </div>
  </section>
</template>

<style scoped>
.workflow-workspace { display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
.model-pending-banner { display: flex; flex-wrap:wrap; align-items: center; gap: 8px 15px; }
.model-pending-banner span { min-width:0; overflow-wrap:anywhere; }
.model-pending-banner button { flex-shrink:0; font-size: 11px; text-decoration: underline; }
.workflow-unsaved { display:flex;align-items:center;gap:6px;color:#e4c85f;font-size:11px;white-space:nowrap; }
.workflow-rename { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:8px 12px; border-bottom:1px solid #505059; font-size:12px; }
.workflow-rename label { display:flex; align-items:center; gap:8px; min-width:0; }
.workflow-rename input { min-width:0; padding:5px; background:#222226; border:1px solid #505059; color:inherit; }
.workflow-rename button { display:flex; align-items:center; gap:4px; padding:5px; }
</style>
