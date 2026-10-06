<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  AppWindow,
  Circle,
  Cable,
  File,
  List,
  Save,
  Settings2,
  Variable,
  Workflow,
  BookOpen,
  Undo2,
  Square,
  Plus,
  GitBranch,
} from "lucide-vue-next";
import WorkbenchCanvas from "./components/WorkbenchCanvas.vue";
import PreparationWorkbench from "./components/PreparationWorkbench.vue";
import WorkbenchRunControls from "./components/WorkbenchRunControls.vue";
import ErrorToast from "./components/ErrorToast.vue";
import ExposureDialog from "./components/ExposureDialog.vue";
import WorkbenchSessions from "./components/WorkbenchSessions.vue";
import WorkbenchHistory from "./components/WorkbenchHistory.vue";
import ProviderSidebar from "./components/ProviderSidebar.vue";
import ContentSidebar from "./components/ContentSidebar.vue";
import ContentLibrary from "./components/ContentLibrary.vue";
import NodeObservationDialog from "./components/NodeObservationDialog.vue";
import { legacyUiUrl, graphChatInterfaceUrl, workbenchUserInterfaceUrl } from "./adapters/legacyUi";
import { useWorkspaceStore } from "./stores/workspace";
import { usePreparationStore } from "./stores/preparation";
import { useWorkbenchRuntimeStore } from "./stores/workbenchRuntime";
import { useWorkbenchPersistenceStore } from "./stores/workbenchPersistence";
import type { PreparationNodeKind, PreparationStage } from "./domain/preparation";
import { useModelConfigurationStore } from "./stores/modelConfiguration";
import { useExposuresStore } from "./stores/exposures";
import { useWorkflowGraphStore } from "./stores/workflowGraph";
import GraphWorkbench from "./components/GraphWorkbench.vue";
import GraphRunControls from "./components/GraphRunControls.vue";
import GraphSessions from "./components/GraphSessions.vue";
import GraphHistory from "./components/GraphHistory.vue";

const workspace = useWorkspaceStore();
const preparation = usePreparationStore();
const runtime = useWorkbenchRuntimeStore();
const persistence = useWorkbenchPersistenceStore();
const models = useModelConfigurationStore();
const exposures = useExposuresStore();
const graph = useWorkflowGraphStore();
persistence.initialize();
const preparationStage = ref<PreparationStage | null>(null);
const initialPreparationNodeId = ref<string | null>(null);
const exposureStage = ref<PreparationStage | null>(null);
const observationStage = ref<"A" | "B" | "Output" | null>(null);
const workspaceView = ref<"canvas" | "history">("canvas");
const legacyContentEditor = computed(() => workspace.sidebarSection === "content"
  && !graph.isGeneric(workspace.activeWorkflowId));
const dirty = computed(
  () => workspace.activeWorkflow.state === "draft",
);
const savedLabel = computed(() => persistence.status === "blocked" ? "未保存"
  : persistence.status === "pending" || dirty.value ? "保存中" : "已保存");
function nodeCount(workflowId: string) {
  if (graph.isGeneric(workflowId)) return graph.entries[workflowId].document.nodes.length;
  if (!workspace.workflows.find(workflow => workflow.id === workflowId)?.nodeCount) return 0;
  return (workspace.drafts[workflowId]?.nodes.length ?? 0)
    + preparation.getDraft(workflowId, "A").nodes.length
    + (preparation.isUnified(workflowId) ? 0 : preparation.getDraft(workflowId, "B").nodes.length)
    + (models.drafts.find(draft => draft.workflowId === workflowId)?.nodes.length ?? 0);
}
const userInterfaceUrl = computed(() => {
  if (graph.isGeneric(workspace.activeWorkflowId)) return graph.document ? graphChatInterfaceUrl(
    legacyUiUrl, graph.document.workflow_definition_id, graph.active?.session_id ?? null,
  ) : null;
  const binding = runtime.runtimes[workspace.activeWorkflowId];
  return binding ? workbenchUserInterfaceUrl(
    legacyUiUrl, binding.sessionId, binding.promptSelection ?? null, binding.modelSelection ?? null,
    exposures.referenceFor(workspace.activeWorkflowId),
  ) : null;
});
const chatInterfaceReady = computed(() => userInterfaceUrl.value !== null
  && (!graph.isGeneric(workspace.activeWorkflowId)
    || Number(graph.active?.saved_revision) > 0 && !graph.active?.pending));
const chatInterfaceTitle = computed(() => {
  if (chatInterfaceReady.value) return "聊天前端";
  if (!graph.isGeneric(workspace.activeWorkflowId)) return runtime.runtimes[workspace.activeWorkflowId]?.sessionId
    ? "聊天入口地址或会话身份不可用" : "选择已有会话后打开聊天前端";
  if (!userInterfaceUrl.value) return "聊天入口地址或工作流身份不可用";
  return graph.active?.pending ? "核实原请求后打开聊天前端" : "保存工作流后打开聊天前端";
});
function openPreparation(stage: PreparationStage) {
  workspaceView.value = "canvas";
  initialPreparationNodeId.value = null;
  preparationStage.value = stage;
}
function addPreparationNode(stage: PreparationStage, kind: PreparationNodeKind) {
  const id = preparation.addNode(workspace.activeWorkflowId, stage, kind);
  if (!id) return;
  openPreparation(stage);
  initialPreparationNodeId.value = id;
}
function changeWorkspaceView(view: "canvas" | "history") {
  preparationStage.value = null;
  workspaceView.value = view;
}

watch(
  () => workspace.activeWorkflowId,
  (workflowId) => {
    if (graph.isGeneric(workflowId)) void graph.activate(workflowId);
    else void runtime.activateWorkflow(workflowId);
  },
  { immediate: true },
);
watch(
  () => workspace.viewVersion,
  () => {
    preparationStage.value = null;
    exposureStage.value = null;
    observationStage.value = null;
    workspaceView.value = "canvas";
    models.selectedNodeId = null;
  },
);
watch(
  () => [workspace.activeWorkflowId, preparationStage.value] as const,
  ([workflowId, stage], [previousWorkflowId, previousStage]) => {
    if (
      previousStage &&
      (previousWorkflowId !== workflowId || previousStage !== stage)
    ) {
      preparation.clearContextCache(previousWorkflowId, previousStage);
    }
    if (previousWorkflowId !== workflowId) {
      preparation.clearContextCache(previousWorkflowId, "A");
      preparation.clearContextCache(previousWorkflowId, "B");
    }
  },
  { flush: "sync" },
);
watch(() => models.error, (failure) => {
  if (failure) runtime.notify(failure.kind, failure.reason, models.pending?.body.idempotency_key ?? "模型配置");
});
watch(() => exposures.error, (failure) => {
  if (failure) runtime.notify(failure.kind, failure.reason, exposures.pending?.idempotency_key ?? "注册声明");
});
</script>

<template>
  <div class="workbench-shell">
    <aside class="workbench-sidebar" aria-label="工作区导航">
      <nav class="workbench-navigation-rail" aria-label="主导航">
        <button
          class="workbench-rail-button"
          :class="{ 'is-active': workspace.sidebarSection === 'workflows' }"
          type="button"
          title="工作流"
          aria-label="工作流"
          :aria-current="workspace.sidebarSection === 'workflows' ? 'page' : undefined"
          @click="workspace.sidebarSection = 'workflows'"
        >
          <Workflow :size="19" />
          <span>工作流</span>
        </button>
        <button
          class="workbench-rail-button"
          :class="{ 'is-active': workspace.sidebarSection === 'providers' }"
          type="button"
          title="供应商"
          aria-label="供应商"
          :aria-current="workspace.sidebarSection === 'providers' ? 'page' : undefined"
          @click="workspace.sidebarSection = 'providers'"
        >
          <Cable :size="19" />
          <span>供应商</span>
        </button>
        <button class="workbench-rail-button" :class="{ 'is-active': workspace.sidebarSection === 'content' }" type="button" title="内容" aria-label="内容" :aria-current="workspace.sidebarSection === 'content' ? 'page' : undefined" @click="workspace.sidebarSection = 'content'"><BookOpen :size="19" /><span>内容</span></button>
      </nav>

      <section v-show="workspace.sidebarSection === 'workflows'" class="workbench-workflow-panel" aria-labelledby="workflow-heading">
        <header class="workbench-panel-heading">
          <h1 id="workflow-heading">工作流</h1>
          <span class="workbench-workflow-count">{{ workspace.workflows.length }}</span>
          <button class="workbench-tool-button" type="button" title="新建工作流" aria-label="新建工作流" @click="graph.createWorkflow()"><Plus :size="15" /></button>
          <button class="workbench-tool-button" type="button" title="创建串行 Agent 示例（模型资源与模型名待选择，配置后运行）" aria-label="创建串行 Agent 示例" :disabled="graph.catalogLoading || !graph.catalog.length" @click="graph.createSerialAgentExample()"><GitBranch :size="15" /></button>
        </header>

        <ul class="workbench-workflow-list" aria-label="工作流列表">
          <li v-for="workflow in workspace.workflows" :key="workflow.id">
            <button
              class="workbench-workflow-item"
              :class="{
                'is-selected': workspace.selectedWorkflowId === workflow.id,
                'is-open': workspace.activeWorkflowId === workflow.id,
              }"
              type="button"
              :disabled="workflow.copyPending"
              :aria-pressed="workspace.selectedWorkflowId === workflow.id"
              :aria-current="workspace.activeWorkflowId === workflow.id ? 'page' : undefined"
              :title="workflow.title"
              @click="workspace.selectWorkflow(workflow.id)"
              @dblclick="workspace.openWorkflow(workflow.id)"
              @keydown.enter.prevent="workspace.openWorkflow(workflow.id)"
            >
              <span class="workbench-workflow-icon" aria-hidden="true">
                <Workflow v-if="workflow.nodeCount > 0" :size="18" />
                <File v-else :size="18" />
              </span>
              <span class="workbench-workflow-copy">
                <strong>{{ workflow.title }}</strong>
                <span class="workbench-workflow-meta">
                  <span>{{ nodeCount(workflow.id) }} 个节点</span>
                  <span v-if="workflow.copyPending">复制待核实</span>
                  <span
                    v-if="workspace.activeWorkflowId === workflow.id"
                    class="workbench-open-indicator"
                  >
                    <Circle :size="5" fill="currentColor" :stroke-width="0" />
                    当前打开
                  </span>
                </span>
              </span>
            </button>
          </li>
        </ul>
        <GraphSessions v-if="graph.isGeneric(workspace.activeWorkflowId)" />
        <WorkbenchSessions v-else />
      </section>
      <ProviderSidebar v-if="workspace.sidebarSection === 'providers'" />
      <ContentSidebar v-if="workspace.sidebarSection === 'content'" />
    </aside>

    <main class="workbench-main" aria-label="工作流工作台">
      <header v-if="!legacyContentEditor" class="workbench-topbar">
        <div class="workbench-current-workflow">
          <Workflow :size="15" aria-hidden="true" />
          <h2>{{ workspace.activeWorkflow.title }}</h2>
          <span
            v-if="!dirty"
            class="workbench-dirty-indicator"
            :class="{ 'is-dirty': dirty || persistence.status !== 'saved' }"
            :title="persistence.error ?? (persistence.savedAt ? `本机保存 ${persistence.savedAt}` : '尚未保存')"
          >
            <Circle :size="5" fill="currentColor" :stroke-width="0" />
            {{ savedLabel }}
          </span>
          <span v-else class="workflow-unsaved"><Square :size="11" fill="currentColor" :stroke-width="0" />当前已编辑内容未保存</span>
        </div>
        <div class="workbench-topbar-actions" aria-label="工作流配置">
          <div class="workbench-view-tabs" role="tablist" aria-label="工作区视图">
            <button class="workbench-tool-button" type="button" role="tab" title="主流程" aria-label="主流程" :aria-selected="workspaceView === 'canvas'" @click="changeWorkspaceView('canvas')"><Workflow :size="15" /></button>
            <button class="workbench-tool-button" type="button" role="tab" title="运行历史" aria-label="运行历史" :aria-selected="workspaceView === 'history'" :disabled="graph.isGeneric(workspace.activeWorkflowId) ? !graph.session : !runtime.executable" @click="changeWorkspaceView('history')"><List :size="15" /></button>
          </div>
          <button class="workbench-tool-button" type="button" title="保存工作流" aria-label="保存工作流" :disabled="persistence.status === 'blocked'" @click="persistence.saveWorkflow()"><Save :size="15" /></button>
          <button v-if="!graph.isGeneric(workspace.activeWorkflowId)" class="workbench-tool-button" type="button" title="迁移为普通节点工作流" aria-label="迁移为普通节点工作流" :disabled="persistence.status === 'blocked' || !!graph.busy || !!runtime.busy || !!runtime.unknown || !!persistence.pendingCopy" @click="persistence.migrateWorkflow()"><GitBranch :size="15" /></button>
          <button v-if="dirty" class="workbench-tool-button" type="button" title="放弃工作流修改" aria-label="放弃工作流修改" @click="persistence.discardDraft()"><Undo2 :size="15" /></button>
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
          <GraphRunControls v-if="graph.isGeneric(workspace.activeWorkflowId)" />
          <WorkbenchRunControls v-else :workflow-id="workspace.activeWorkflowId" />
        </div>
      </header>
      <div v-if="persistence.error" class="workbench-save-error" role="status">{{ persistence.error }}</div>
      <div v-if="persistence.pendingCopy" class="workbench-save-error model-pending-banner" role="status">
        <span>工作流编辑副本等待会话复制确认</span>
        <button type="button" :disabled="!!runtime.busy" @click="persistence.reconcileCopy()">核实复制</button>
        <button v-if="graph.entries[persistence.pendingCopy.id] && !graph.entries[persistence.pendingCopy.id].pending" type="button" :disabled="!!graph.busy" @click="graph.discardRejectedCopy(persistence.pendingCopy.id)">放弃复制</button>
        <template v-for="diagnostic in graph.entries[persistence.pendingCopy.id]?.diagnostics ?? []" :key="diagnostic.node_id"><button v-if="diagnostic.node_id && diagnostic.reason_code.includes('state') && !graph.entries[persistence.pendingCopy.id].pending" type="button" :disabled="!!graph.busy" @click="graph.resetPrivateState(persistence.pendingCopy.id, diagnostic.node_id); graph.reconcile(persistence.pendingCopy.id)">重建该节点私有状态并重试</button></template>
      </div>
      <div v-for="(entry, workflowId) in graph.entries" :key="workflowId" v-show="entry.pending" class="workbench-save-error model-pending-banner" role="status"><span>{{ workspace.workflows.find(w => w.id === workflowId)?.title }} · 原请求待核实</span><button type="button" :disabled="!!graph.busy" @click="graph.reconcile(String(workflowId))">核实原请求</button></div>
      <div v-if="models.pending" class="workbench-save-error model-pending-banner" role="status">
        <span>模型配置提交结果待核实</span>
        <button type="button" :disabled="models.busy" @click="models.reconcile()">核实原请求</button>
      </div>
      <div v-if="exposures.pending" class="workbench-save-error model-pending-banner" role="status">
        <span>注册声明提交结果待核实</span>
        <button type="button" :disabled="exposures.busy" @click="exposures.reconcile()">核实原请求</button>
      </div>

      <div class="workbench-canvas-region">
        <ContentLibrary v-if="legacyContentEditor" view="editor" />
        <GraphHistory v-else-if="workspaceView === 'history' && graph.isGeneric(workspace.activeWorkflowId)" />
        <WorkbenchHistory v-else-if="workspaceView === 'history'" />
        <GraphWorkbench v-else-if="graph.isGeneric(workspace.activeWorkflowId)" />
        <PreparationWorkbench
          v-else-if="workspace.activeWorkflow.nodeCount"
          :workflow-id="workspace.activeWorkflowId"
          stage="A"
          unified
          :initial-selected-id="initialPreparationNodeId"
          @back="preparationStage = null"
          @open-exposure="exposureStage = $event"
          @open-observation="observationStage = $event"
        />
        <WorkbenchCanvas
          v-else
          @open-preparation="openPreparation"
          @open-exposure="exposureStage = $event"
          @open-observation="observationStage = $event"
          @add-node="addPreparationNode"
        />
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
    <NodeObservationDialog v-if="observationStage" :stage="observationStage" @close="observationStage = null" />
    <ExposureDialog
      v-if="exposureStage"
      :workflow-id="workspace.activeWorkflowId"
      :stage="exposureStage"
      @close="exposureStage = null"
    />
  </div>
</template>

<style scoped>
.model-pending-banner { display: flex; align-items: center; gap: 15px; }
.model-pending-banner button { font-size: 11px; text-decoration: underline; }
.workflow-unsaved { display:flex;align-items:center;gap:6px;color:#e4c85f;font-size:11px;white-space:nowrap; }
</style>
