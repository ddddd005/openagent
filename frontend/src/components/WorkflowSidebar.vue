<script setup lang="ts">
import { computed, ref } from "vue";
import { Archive, ArrowRight, Circle, File, Plus, RefreshCw, Trash2, Workflow } from "lucide-vue-next";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import GraphSessions from "./GraphSessions.vue";

const workspace = useWorkspaceStore();
const graph = useWorkflowGraphStore();
const showArchived = ref(false), navigationError = ref("");
const listedWorkflows = computed(() => workspace.workflows.filter(row => !row.deleted && (showArchived.value || !row.archived)));
const formalCount = computed(() => workspace.formalWorkflowCount(showArchived.value));
async function manageWorkflow(id: string, action: "archive" | "delete") {
  const message = action === "archive"
    ? "归档此工作流并停止所属运行？归档数据仅供查看，不能恢复执行。"
    : "删除此工作流及所属会话、运行和历史？全局资源与私有 Key 不会删除。";
  if (!window.confirm(message)) return;
  navigationError.value = "";
  if (!await graph.manageWorkflow(id, action)) navigationError.value = "目标操作未完成，请查看实际错误或核实原请求。";
}
let navigationGeneration = 0;
function leaveEditor(change: () => void) {
  const token = ++navigationGeneration, workflowId = workspace.activeWorkflowId;
  if (token !== navigationGeneration || workflowId !== workspace.activeWorkflowId) return;
  navigationError.value = "";
  change();
}
function nodeCount(workflowId: string) {
  const workflow = workspace.workflows.find(row => row.id === workflowId);
  return workflow?.nodeCount ?? 0;
}
function openWorkflow(id: string) { leaveEditor(() => workspace.openWorkflow(id)); }
</script>

<template>
  <section class="workbench-workflow-panel" aria-labelledby="workflow-heading">
    <header class="workbench-panel-heading">
      <h1 id="workflow-heading">工作流</h1>
      <span class="workbench-workflow-count">{{ formalCount }}</span>
      <button class="workbench-tool-button" type="button" title="刷新工作流目录" aria-label="刷新工作流目录"
        :disabled="graph.definitionsLoading" @click="graph.loadDefinitions()"><RefreshCw :size="14" /></button>
      <button class="workbench-tool-button" type="button" title="显示已归档工作流" aria-label="显示已归档工作流"
        :aria-pressed="showArchived" @click="showArchived = !showArchived"><Archive :size="14" /></button>
      <button class="workbench-tool-button" type="button" title="新建工作流" aria-label="新建工作流"
        @click="leaveEditor(() => graph.createWorkflow())"><Plus :size="15" /></button>
    </header>
    <ul class="workbench-workflow-list" aria-label="工作流列表">
      <li v-for="workflow in listedWorkflows" :key="workflow.id" class="workflow-row">
        <button class="workbench-workflow-item"
          :class="{ 'is-selected': workspace.selectedWorkflowId === workflow.id, 'is-open': workspace.activeWorkflowId === workflow.id }"
          type="button" :disabled="workflow.copyPending" :aria-pressed="workspace.selectedWorkflowId === workflow.id"
          :aria-current="workspace.activeWorkflowId === workflow.id ? 'page' : undefined" :title="workflow.title"
          @click="workspace.selectWorkflow(workflow.id)" @dblclick="openWorkflow(workflow.id)"
          @keydown.enter.prevent="openWorkflow(workflow.id)">
          <span class="workbench-workflow-icon" aria-hidden="true">
            <Workflow v-if="workflow.nodeCount > 0" :size="18" /><File v-else :size="18" />
          </span>
          <span class="workbench-workflow-copy">
            <strong>{{ workflow.title }}</strong>
            <span class="workbench-workflow-meta">
              <span>{{ nodeCount(workflow.id) }} 个节点</span>
              <span v-if="workflow.archived">已归档 · 只读</span>
              <span v-if="workflow.copyPending">复制待核实</span>
              <span v-if="workspace.activeWorkflowId === workflow.id" class="workbench-open-indicator">
                <Circle :size="5" fill="currentColor" :stroke-width="0" />当前打开
              </span>
            </span>
          </span>
        </button>
        <button v-if="workspace.selectedWorkflowId === workflow.id && !workflow.archived"
          class="workbench-tool-button workflow-archive" type="button" title="归档工作流" aria-label="归档工作流"
          :disabled="workflow.copyPending || !!graph.busy || !!graph.entries[workflow.id]?.pending || !graph.entries[workflow.id]?.saved_revision"
          @click.stop="manageWorkflow(workflow.id, 'archive')"><Archive :size="13" />
        </button>
        <button v-if="workspace.selectedWorkflowId === workflow.id" class="workbench-tool-button workflow-delete"
          type="button" title="删除工作流" aria-label="删除工作流"
          :disabled="workflow.copyPending || !!graph.busy || !!graph.entries[workflow.id]?.pending || !graph.entries[workflow.id]?.saved_revision"
          @click.stop="manageWorkflow(workflow.id, 'delete')"><Trash2 :size="13" />
        </button>
        <button v-if="workspace.selectedWorkflowId === workflow.id && workspace.activeWorkflowId !== workflow.id"
          class="workflow-switch" type="button" :disabled="workflow.copyPending || !!graph.busy"
          :aria-label="`切换到 ${workflow.title}`" @click.stop="openWorkflow(workflow.id)">
          <ArrowRight :size="13" />切换
        </button>
      </li>
    </ul>
    <p v-if="navigationError" class="navigation-warning" role="status">{{ navigationError }}</p>
    <GraphSessions />
  </section>
</template>

<style scoped>
.workflow-row { position:relative; }
.workflow-row:has(.workflow-delete) .workbench-workflow-item { padding-right:70px; }
.workflow-row:has(.workflow-switch) .workbench-workflow-item { padding-right:132px; }
.workflow-switch { position:absolute; right:10px; top:24px; display:flex; align-items:center; gap:4px; padding:5px; font-size:11px; }
.workflow-row:has(.workflow-delete) .workflow-switch { right:68px; }
.workflow-archive { position:absolute; right:36px; top:22px; }
.workflow-delete { position:absolute; right:8px; top:22px; }
.navigation-warning { padding:6px 10px; margin:0; font-size:11px; color:#e4a0a0; overflow-wrap:anywhere; }
</style>
