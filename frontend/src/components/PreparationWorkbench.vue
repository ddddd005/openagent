<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, shallowRef, watch } from "vue";
import { MarkerType, VueFlow, type VueFlowStore } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import {
  AlertCircle,
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Check,
  ChevronRight,
  FileJson,
  Hand,
  Minus,
  MousePointer2,
  Plus,
  RefreshCw,
  Redo2,
  Scan,
  Trash2,
  Undo2,
  Wrench,
} from "lucide-vue-next";
import PreparationNode from "./PreparationNode.vue";
import MainFlowNode from "./MainFlowNode.vue";
import ModelProviderNode from "./ModelProviderNode.vue";
import { useWorkspaceStore } from "../stores/workspace";
import { useModelConfigurationStore } from "../stores/modelConfiguration";
import { dependencyTarget } from "../domain/modelConfiguration";
import { useGlobalContentStore } from "../stores/globalContent";
import IncrementalNodeFields from "./IncrementalNodeFields.vue";
import CanvasNodeMenu from "./CanvasNodeMenu.vue";
import { useCanvasNodeMenu } from "../composables/useCanvasNodeMenu";
import { preparationMenuItems, preparationNodeCatalog } from "../domain/canvasMenu";
import RolePlacementFields from "./RolePlacementFields.vue";
import {
  getToolDefinition,
  preparationPorts,
  createPromptMember,
  type CanonicalContextMessage,
  type PreparationNode as GraphNode,
  type RolePlacement,
} from "../domain/preparation";
import { usePreparationStore } from "../stores/preparation";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import type { WorkbenchContextScope } from "../adapters/workbenchContext";
import { useWorkbenchContextStore } from "../stores/workbenchContext";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";

const props = defineProps<{ workflowId: string; stage: "A" | "B"; unified?: boolean; initialSelectedId?: string | null }>();
const emit = defineEmits<{ back: []; "open-exposure": [stage: "A" | "B"]; "open-observation": [stage: "A" | "B" | "Output"] }>();
const workspace = useWorkspaceStore();
const models = useModelConfigurationStore();
const content = useGlobalContentStore();
const modelDraft = computed(() => models.getDraft(props.workflowId));
const previewStage = ref<"A" | "B">(props.stage);
if (typeof window !== "undefined") void content.initialize();
const preparation = usePreparationStore();
const context = useWorkbenchContextStore();
const runtime = useWorkbenchRuntimeStore();
const persistence = useWorkbenchPersistenceStore();
let alive = true;
const flow = shallowRef<VueFlowStore | null>(null);
const initialFitPending = ref(true);
const selectedId = ref<string | null>(props.initialSelectedId ?? null);
const selectedEdgeId = ref<string | null>(null);
const activeView = ref<"graph" | "preview" | "diagnostics">("graph");
const previewTab = ref<"messages" | "materials" | "tools" | "context">("messages");
const previewSource = ref<"local" | "server">("local");
const mode = ref<"select" | "pan">("select");
const canvas = ref<HTMLElement | null>(null);
const nodeMenu = useCanvasNodeMenu(canvas);
const draft = computed(() => preparation.getDraft(props.workflowId, props.stage));
const preview = computed(() => preparation.getPreview(props.workflowId, previewStage.value));
const diagnostics = computed(() => {
  const combined = [...preview.value.diagnostics, ...(props.unified
    ? preparation.getPreview(props.workflowId, previewStage.value === "A" ? "B" : "A").diagnostics : []), ...context.diagnostics];
  return combined.filter((entry, index) => combined.findIndex((candidate) =>
    candidate.code === entry.code && candidate.nodeId === entry.nodeId
    && candidate.field === entry.field && candidate.edgeId === entry.edgeId) === index);
});
const selectedNode = computed(() => draft.value.nodes.find((node) => node.id === selectedId.value));
const selectedContextCollection = computed(() => selectedNode.value?.kind === "context" ? {
  schemaVersion: 1, kind: "preparation-collection", source: selectedNode.value.config.source,
  items: selectedNode.value.config.floors.flatMap((floor) => floor.items),
} : null);
const selectedEdge = computed(() => draft.value.edges.find((edge) => edge.id === selectedEdgeId.value));
const config = computed(() => (selectedNode.value?.config ?? {}) as unknown as Record<string, unknown>);
const nodeErrors = computed(() => diagnostics.value.filter((item) => item.nodeId === selectedId.value));
const flowId = computed(() => `preparation-${props.workflowId}-${props.stage}`);
const menuGroups = computed(() => [{ items: preparationMenuItems(draft.value) },
  ...(props.unified ? [{ label: "模型", items: [{ id: "model-provider", label: "模型提供", disabled: false }] }] : [])]);
const flowNodes = computed(() => [...draft.value.nodes.map((node) => ({
  id: node.id,
  type: "preparation",
  position: { ...node.position },
  selected: node.id === selectedId.value,
  data: {
    node,
    errorCount: diagnostics.value.filter((item) => item.nodeId === node.id && item.severity === "error").length,
    connectedInputs: draft.value.edges.filter((edge) => edge.target === node.id).map((edge) => edge.targetHandle),
    connectedOutputs: draft.value.edges.filter((edge) => edge.source === node.id).map((edge) => edge.sourceHandle),
    invalidInputs: draft.value.edges.filter((edge) => edge.target === node.id
      && diagnostics.value.some((item) => item.edgeId === edge.id && item.severity === "error")).map((edge) => edge.targetHandle),
    invalidOutputs: draft.value.edges.filter((edge) => edge.source === node.id
      && diagnostics.value.some((item) => item.edgeId === edge.id && item.severity === "error")).map((edge) => edge.sourceHandle),
  },
})), ...(props.unified ? workspace.nodes.map(node => ({
  id: node.id, type: "main-flow", position: { ...node.position }, selected: node.id === selectedId.value,
  data: { ...node.data },
})) : []), ...(props.unified ? modelDraft.value?.nodes ?? [] : []).map(node => ({
  id: node.id, type: "model-provider", position: { ...node.position }, selected: node.id === selectedId.value,
  data: { node },
}))]);
const flowEdges = computed(() => [...draft.value.edges, ...(props.unified ? workspace.edges : []),
  ...(props.unified ? modelDraft.value?.edges ?? [] : []).map(edge => ({
    id: edge.id, source: edge.source, target: dependencyTarget(edge.target_binding_id === AGENT_BINDINGS.A ? "A" : "B"),
    sourceHandle: "model-out", targetHandle: "model-in",
  }))].map((edge) => {
  const invalid = diagnostics.value.some((item) => item.edgeId === edge.id && item.severity === "error");
  const color = invalid ? "#dc8f8f" : edge.id === selectedEdgeId.value ? "#d1ded8" : "#82968e";
  return {
    ...edge,
    type: "smoothstep",
    selected: edge.id === selectedEdgeId.value,
    style: { stroke: color, strokeWidth: invalid ? 2 : 1.5 },
    markerEnd: { type: MarkerType.ArrowClosed, color, width: 15, height: 15 },
  };
}));
const presentation = computed(() => config.value.presentation as RolePlacement | undefined);
type Member = {
  id: string;
  itemId: string;
  revision: number;
  name: string;
  text: string;
  presentation: RolePlacement;
};
const members = computed(() => (config.value.members ?? []) as Member[]);
const inputIds = computed(() => (config.value.inputs ?? []) as string[]);
const toolDefinition = computed(() => {
  if (selectedNode.value?.kind !== "tool") return null;
  return getToolDefinition(config.value.toolRef as { name: string; version: string });
});
const contextScope = computed<WorkbenchContextScope | null>(() => {
  const session = runtime.session;
  if (runtime.workflowId !== props.workflowId || !session
    || session.ref_revision === undefined || !session.head_commit_id) return null;
  const binding = selectedNode.value?.kind === "context" ? selectedNode.value.config.sourceBindingId : null;
  const sourceStage = binding === AGENT_BINDINGS.B ? "B" : binding === AGENT_BINDINGS.A ? "A" : previewStage.value;
  return {
    workflowId: props.workflowId, stage: sourceStage,
    sessionId: session.workflow_session_id, nodeBindingId: AGENT_BINDINGS[sourceStage],
    sessionRevision: session.revision, refRevision: session.ref_revision,
    headCommitId: session.head_commit_id,
  };
});
const contextStatus = computed(() => context.loading === "read" ? "读取中"
  : context.error ? "读取失败" : context.read ? "已读取"
    : contextScope.value ? "未读取" : "未绑定会话");
const serverMessages = computed(() => context.serverPreview?.preparation?.s0 ?? []);

function canonicalText(message: CanonicalContextMessage) {
  return message.blocks.map((block) => block.kind === "text"
    ? String(block.text) : displayJson(block)).join("\n");
}

function serverMessageProtected(message: CanonicalContextMessage) {
  return message.blocks.some((block) => block.kind !== "text")
    || context.serverPreview?.preparation?.protected_blocks.some(
      (locator) => locator.message_id === message.message_id,
    );
}

function serverMessageEphemeral(message: CanonicalContextMessage) {
  const value = context.serverPreview?.preparation;
  return value?.root_locator.message_id === message.message_id
    || value?.prompt_message_ids.includes(message.message_id);
}

function changePreviewSource(source: "local" | "server") {
  previewSource.value = source;
  if (source === "server" && contextScope.value && !context.serverPreview && !context.loading)
    void context.preview(previewStage.value);
}

function patch(patch: Record<string, unknown>) {
  if (!selectedNode.value) return;
  preparation.updateNodeConfig(props.workflowId, props.stage, selectedNode.value.id, patch);
}
function patchJsonValue(event: Event) {
  try { patch({ value: JSON.parse((event.target as HTMLTextAreaElement).value) }); }
  catch { runtime.notify("rejected", "会话数据不是有效的 JSON", "节点配置"); }
}

function displayJson(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function titleOf(id: string) {
  return draft.value.nodes.find((node) => node.id === id)?.title ?? id;
}

function fitCanvas() {
  if (flow.value?.id !== flowId.value || !flowNodes.value.length) return false;
  if (
    flow.value.getNodes.value.length !== flowNodes.value.length ||
    flow.value.getNodes.value.some((node) => node.dimensions.width <= 0 || node.dimensions.height <= 0)
  ) return false;
  void flow.value.fitView({ padding: 0.18, maxZoom: 0.85 });
  return true;
}

function fitInitialCanvas() {
  if (alive && initialFitPending.value && activeView.value === "graph" && fitCanvas())
    initialFitPending.value = false;
}

function attachFlow(instance: VueFlowStore) {
  if (!alive || instance.id !== flowId.value) return;
  flow.value = instance;
  const scope = { workflowId: props.workflowId, stage: props.stage };
  const current = () => alive && flow.value === instance && scope.workflowId === props.workflowId && scope.stage === props.stage && instance.id === flowId.value;
  instance.onNodeClick(({ node }) => {
    if (!current()) return;
    selectedId.value = node.id;
    selectedEdgeId.value = null;
  });
  instance.onEdgeClick(({ edge }) => {
    if (!current()) return;
    selectedId.value = null;
    selectedEdgeId.value = edge.id;
  });
  instance.onPaneClick(() => {
    if (!current()) return;
    selectedId.value = null;
    selectedEdgeId.value = null;
    nodeMenu.close();
  });
  instance.onNodeDragStop(({ nodes }) => {
    if (!current()) return;
    const moves = nodes.map(node => ({ id: node.id, position: { ...node.position } })).filter(move => {
      const previous = draft.value.nodes.find(node => node.id === move.id)?.position
        ?? workspace.drafts[scope.workflowId]?.nodes.find(node => node.id === move.id)?.position
        ?? modelDraft.value?.nodes.find(node => node.id === move.id)?.position;
      return previous && (previous.x !== move.position.x || previous.y !== move.position.y);
    });
    if (!moves.length) return;
    if (props.unified) persistence.mutateLegacy(scope.workflowId, target => {
      preparation.moveNodes(target, scope.stage, moves);
      const main = workspace.drafts[target];
      for (const move of moves) {
        const node = main.nodes.find(node => node.id === move.id);
        if (node) node.position = { ...move.position };
        if (models.drafts.find(draft => draft.workflowId === target)?.nodes.some(node => node.id === move.id))
          models.updateNode(target, move.id, { position: move.position });
      }
    });
    else preparation.moveNodes(scope.workflowId, scope.stage, moves);
  });
  instance.onConnect((connection) => {
    if (!current()) return;
    if (!connection.sourceHandle || !connection.targetHandle) return;
    if (connection.sourceHandle === "model-out") {
      models.connect(props.workflowId, connection.source, connection.target, connection.sourceHandle, connection.targetHandle);
      return;
    }
    preparation.connect(scope.workflowId, scope.stage, {
      source: connection.source,
      target: connection.target,
      sourceHandle: connection.sourceHandle,
      targetHandle: connection.targetHandle,
    });
  });
  instance.onNodesInitialized(() => {
    if (current()) fitInitialCanvas();
  });
  void nextTick().then(() => {
    if (current()) fitInitialCanvas();
  });
}

function addNode(id: string) {
  const item = menuGroups.value.flatMap(group => group.items).find(item => item.id === id);
  if (!item || item.disabled || !nodeMenu.menu.value || !flow.value) return;
  const position = flow.value.screenToFlowCoordinate(nodeMenu.menu.value);
  if (id === "model-provider") {
    models.addNode(props.workflowId, position);
    nodeMenu.close(true);
    return;
  }
  const result = preparation.addNode(props.workflowId, props.stage, id as GraphNode["kind"], position);
  if (!result) return;
  selectedId.value = result;
  selectedEdgeId.value = null;
  nodeMenu.close(true);
}

function removeSelection() {
  if (selectedNode.value) {
    preparation.removeNode(props.workflowId, props.stage, selectedNode.value.id);
    selectedId.value = null;
  } else if (selectedEdge.value) {
    preparation.removeEdge(props.workflowId, props.stage, selectedEdge.value.id);
    selectedEdgeId.value = null;
  }
}

function updateMember(index: number, change: Partial<Member>) {
  patch({ members: members.value.map((member, at) => at === index ? { ...member, ...change } : member) });
}

function addMember() {
  const member = createPromptMember();
  member.name = `条目 ${members.value.length + 1}`;
  member.presentation.order = members.value.length;
  patch({ members: [...members.value, member] });
}

function moveMember(index: number, offset: number) {
  const next = [...members.value];
  const destination = index + offset;
  if (destination < 0 || destination >= next.length) return;
  [next[index], next[destination]] = [next[destination]!, next[index]!];
  patch({ members: next });
}

function moveInput(index: number, offset: number) {
  const next = [...inputIds.value];
  const destination = index + offset;
  if (destination < 0 || destination >= next.length) return;
  [next[index], next[destination]] = [next[destination]!, next[index]!];
  patch({ inputs: next });
}

function locateDiagnostic(item: { nodeId?: string; edgeId?: string }) {
  selectedId.value = item.nodeId ?? null;
  selectedEdgeId.value = item.edgeId ?? null;
  activeView.value = "graph";
  void nextTick().then(() => {
    if (item.nodeId) void flow.value?.fitView({ nodes: [item.nodeId], padding: 0.7, maxZoom: 1 });
  });
}

function keyboard(event: KeyboardEvent) {
  if ((event.target as HTMLElement).closest("input, textarea, select, [contenteditable]")) return;
  if (event.key === "Escape") {
    nodeMenu.close();
    selectedId.value = null;
    selectedEdgeId.value = null;
  } else if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    removeSelection();
  } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
    event.preventDefault();
    if (props.unified) persistence.undoLegacy(props.workflowId, event.shiftKey);
    else event.shiftKey
      ? preparation.redo(props.workflowId, props.stage)
      : preparation.undo(props.workflowId, props.stage);
  }
}

watch(() => [props.workflowId, props.stage], (_, previous) => {
  flow.value = null;
  initialFitPending.value = true;
  if (workspace.activeWorkflow.sourceId !== previous[0]) {
    selectedId.value = null;
    selectedEdgeId.value = null;
  }
  nodeMenu.close();
  activeView.value = "graph";
  previewSource.value = "local";
}, { flush: "sync" });
watch(
  () => contextScope.value ? JSON.stringify(contextScope.value) : null,
  () => {
    context.activate(contextScope.value);
    if (contextScope.value) void context.refresh();
  },
  { immediate: true, flush: "sync" },
);
watch(() => context.error, (notice) => {
  if (notice) runtime.notify(notice.kind, notice.reason, "上下文读取/预览");
});
watch(
  () => flow.value?.getNodes.value.map((node) => [node.id, node.dimensions.width, node.dimensions.height]),
  () => {
    void nextTick().then(fitInitialCanvas);
  },
  { deep: true, flush: "post" },
);
watch(activeView, (view) => {
  nodeMenu.close();
  if (view === "graph") void nextTick().then(fitInitialCanvas);
});
watch(() => content.version, () => { context.serverPreview = null; });
watch(() => selectedNode.value?.id, () => {
  if (selectedId.value && !selectedNode.value) selectedId.value = null;
});
onUnmounted(() => {
  alive = false;
  flow.value = null;
  context.leave();
});
</script>

<template>
  <section class="preparation-workbench" tabindex="0" :aria-label="unified ? '工作流节点工作台' : `Agent ${stage} 提示词准备图`" @keydown.capture="keyboard">
    <header class="preparation-toolbar">
      <button v-if="!unified" type="button" class="preparation-icon-button" aria-label="返回主流程" title="返回主流程" @click="emit('back')">
        <ArrowLeft :size="16" />
      </button>
      <strong>{{ unified ? "工作流" : `Agent ${stage} · 上下文装配` }}</strong>
      <select v-if="unified" v-model="previewStage" aria-label="装配预览目标"><option value="A">Agent A</option><option value="B">Agent B</option></select>
      <div class="preparation-tabs" role="tablist" aria-label="准备图视图">
        <button type="button" role="tab" :aria-selected="activeView === 'graph'" :class="{ active: activeView === 'graph' }" @click="activeView = 'graph'">画布</button>
        <button type="button" role="tab" :aria-selected="activeView === 'preview'" :class="{ active: activeView === 'preview' }" @click="activeView = 'preview'">装配预览</button>
        <button type="button" role="tab" :aria-selected="activeView === 'diagnostics'" :class="{ active: activeView === 'diagnostics' }" @click="activeView = 'diagnostics'">
          诊断<span v-if="diagnostics.length" class="preparation-diagnostic-count">{{ diagnostics.length }}</span>
        </button>
      </div>
    </header>

    <div class="preparation-body">
      <div class="preparation-main">
        <div
          v-show="activeView === 'graph'"
          ref="canvas"
          class="preparation-canvas"
          tabindex="0"
          :aria-label="unified ? '工作流节点画布' : `Agent ${stage} 准备画布`"
          @contextmenu="nodeMenu.contextMenu"
          @pointermove="nodeMenu.pointerMove"
          @pointerleave="nodeMenu.pointerLeave"
        >
          <VueFlow
            :id="flowId"
            :key="flowId"
            :nodes="flowNodes"
            :edges="flowEdges"
            :min-zoom="0.2"
            :max-zoom="1.8"
            :delete-key-code="null"
            :disable-keyboard-a11y="true"
            :zoom-on-double-click="false"
            :connect-on-click="false"
            :pan-on-drag="mode === 'pan' ? [0, 1] : [1]"
            :nodes-draggable="mode === 'select'"
            :selection-on-drag="false"
            :apply-default="true"
            :fit-view-on-init="true"
            @init="attachFlow"
          >
            <template #node-preparation="nodeProps">
              <PreparationNode v-bind="nodeProps" />
            </template>
            <template #node-main-flow="nodeProps"><MainFlowNode v-bind="nodeProps" flat @open-preparation="previewStage = $event; activeView = 'preview'" @open-exposure="emit('open-exposure', $event)" @open-observation="emit('open-observation', $event)" /></template>
            <template #node-model-provider="nodeProps"><ModelProviderNode v-bind="nodeProps" /></template>
            <Background :gap="22" :size="1" pattern-color="#3b3b3f" />
          </VueFlow>
          <div class="preparation-add-wrap">
            <button type="button" class="preparation-add-button" aria-haspopup="menu" aria-keyshortcuts="Shift+A" :aria-expanded="nodeMenu.menu.value?.view === 'add'" @click="nodeMenu.open('add')">
              <Plus :size="15" /><span>添加节点</span>
            </button>
          </div>
          <div class="preparation-canvas-tools" aria-label="准备画布工具">
            <button type="button" :class="{ active: mode === 'select' }" aria-label="选择模式" title="选择模式" @click="mode = 'select'"><MousePointer2 :size="16" /></button>
            <button type="button" :class="{ active: mode === 'pan' }" aria-label="平移模式" title="平移模式" @click="mode = 'pan'"><Hand :size="16" /></button>
            <span class="preparation-tool-divider"></span>
            <button type="button" aria-label="撤销" title="撤销" :disabled="unified ? !persistence.legacyHistories[workflowId]?.past.length : !preparation.canUndo(workflowId, stage)" @click="unified ? persistence.undoLegacy(workflowId) : preparation.undo(workflowId, stage)"><Undo2 :size="16" /></button>
            <button type="button" aria-label="重做" title="重做" :disabled="unified ? !persistence.legacyHistories[workflowId]?.future.length : !preparation.canRedo(workflowId, stage)" @click="unified ? persistence.undoLegacy(workflowId, true) : preparation.redo(workflowId, stage)"><Redo2 :size="16" /></button>
            <span class="preparation-tool-divider"></span>
            <button type="button" aria-label="缩小" title="缩小" @click="flow?.zoomOut()"><Minus :size="16" /></button>
            <button type="button" aria-label="放大" title="放大" @click="flow?.zoomIn()"><Plus :size="16" /></button>
            <button type="button" aria-label="适应准备图" title="适应准备图" @click="fitCanvas"><Scan :size="16" /></button>
          </div>
          <span class="preparation-canvas-count">{{ draft.nodes.length }} 节点 · {{ draft.edges.length }} 连线</span>
          <CanvasNodeMenu
            :state="nodeMenu.menu.value"
            :groups="menuGroups"
            @add="nodeMenu.open('add', nodeMenu.menu.value ?? undefined)"
            @back="nodeMenu.open('context', nodeMenu.menu.value ?? undefined)"
            @select="addNode"
          />
        </div>

        <section v-if="activeView === 'preview'" class="preparation-preview" aria-label="装配预览">
          <header class="preparation-preview-header">
            <div v-if="previewSource === 'local'" class="preparation-tabs" role="tablist" aria-label="装配预览内容">
              <button type="button" role="tab" :aria-selected="previewTab === 'messages'" :class="{ active: previewTab === 'messages' }" @click="previewTab = 'messages'">消息 {{ preview.messages.length }}</button>
              <button type="button" role="tab" :aria-selected="previewTab === 'materials'" :class="{ active: previewTab === 'materials' }" @click="previewTab = 'materials'">材料 {{ preview.materials.length }}</button>
              <button type="button" role="tab" :aria-selected="previewTab === 'tools'" :class="{ active: previewTab === 'tools' }" @click="previewTab = 'tools'">工具</button>
              <button type="button" role="tab" :aria-selected="previewTab === 'context'" :class="{ active: previewTab === 'context' }" @click="previewTab = 'context'">上下文 {{ preview.context.items.length }}</button>
            </div>
            <span v-else class="preparation-muted">消息 {{ serverMessages.length }} · 临时预览</span>
            <div class="preparation-preview-source" role="group" aria-label="装配依据">
              <button type="button" :aria-pressed="previewSource === 'local'" :class="{ active: previewSource === 'local' }" @click="changePreviewSource('local')">本地</button>
              <button type="button" :aria-pressed="previewSource === 'server'" :class="{ active: previewSource === 'server' }" @click="changePreviewSource('server')">服务端</button>
              <button v-if="previewSource === 'server'" type="button" class="preparation-icon-button" aria-label="刷新服务端装配预览" title="刷新服务端装配预览" :disabled="!contextScope || !!context.loading" @click="context.preview(previewStage)"><RefreshCw :size="14" /></button>
            </div>
          </header>
          <div v-if="previewSource === 'server'" class="preparation-preview-scroll" aria-label="服务端装配预览">
            <p v-if="!contextScope" class="preparation-empty-state">未绑定可读取的会话</p>
            <p v-else-if="context.loading" class="preparation-empty-state" role="status">装配预览读取中</p>
            <p v-else-if="context.error" class="preparation-read-error" role="status">{{ context.error.reason }}</p>
            <p v-else-if="context.serverPreview?.status === 'pending'" class="preparation-empty-state" role="status">等待 Agent A 本轮业务结果</p>
            <template v-else-if="context.serverPreview?.status === 'ready'">
              <div class="preparation-preview-basis">
                <span>会话 {{ context.serverPreview.scope.workflow_session_id.slice(0, 8) }}</span>
                <span>选择 {{ context.serverPreview.scope.head_commit_id?.slice(0, 8) ?? "无" }}</span>
                <span>父回合 {{ context.serverPreview.scope.parent_turn_id?.slice(0, 8) ?? "无" }}</span>
              </div>
              <article v-for="(message, index) in serverMessages" :key="message.message_id" class="preparation-message">
                <header><span class="preparation-message-index">{{ index + 1 }}</span><strong>{{ message.role }}</strong><span v-if="serverMessageProtected(message)" class="preparation-protected-tag">协议保留</span><span v-if="serverMessageEphemeral(message)" class="preparation-ephemeral-tag">临时</span><span class="preparation-message-source">{{ message.source.kind }}</span></header>
                <pre>{{ canonicalText(message) }}</pre>
                <footer>{{ message.message_id }}</footer>
              </article>
            </template>
            <p v-else class="preparation-empty-state">预览尚未生成</p>
          </div>
          <div v-else-if="previewTab === 'messages'" class="preparation-preview-scroll">
            <p v-if="!preview.messages.length" class="preparation-empty-state">暂无可装配消息</p>
            <article v-for="(message, index) in preview.messages" :key="message.id" class="preparation-message">
              <header><span class="preparation-message-index">{{ index + 1 }}</span><strong>{{ message.role }}</strong><span v-if="message.protected" class="preparation-protected-tag">协议保留</span><button type="button" :title="message.nodeId" @click="locateDiagnostic({ nodeId: message.nodeId })">{{ titleOf(message.nodeId) }}</button></header>
              <pre>{{ message.text }}</pre>
              <footer>{{ message.materialId }}</footer>
            </article>
          </div>
          <div v-else-if="previewTab === 'materials'" class="preparation-preview-scroll">
            <p v-if="!preview.materials.length" class="preparation-empty-state">暂无材料</p>
            <pre v-for="(material, index) in preview.materials" :key="index" class="preparation-json-block">{{ displayJson(material) }}</pre>
          </div>
          <div v-else-if="previewTab === 'tools'" class="preparation-preview-scroll">
            <h3 class="preparation-section-label">工具描述</h3>
            <pre class="preparation-json-block">{{ displayJson(preview.toolDescriptions) }}</pre>
            <h3 class="preparation-section-label">参数 schema</h3>
            <pre class="preparation-json-block">{{ displayJson(preview.toolSchemas) }}</pre>
          </div>
          <div v-else class="preparation-preview-scroll" aria-label="归档上下文材料">
            <div class="preparation-context-heading"><span>{{ contextStatus }}</span><button type="button" class="preparation-icon-button" title="刷新归档上下文" aria-label="刷新归档上下文" :disabled="!contextScope || !!context.loading" @click="context.refresh()"><RefreshCw :size="14" /></button></div>
            <pre class="preparation-json-block">{{ displayJson(selectedContextCollection) }}</pre>
          </div>
        </section>

        <section v-if="activeView === 'diagnostics'" class="preparation-diagnostics" aria-label="准备图诊断">
          <div v-if="!diagnostics.length" class="preparation-empty-state preparation-valid-state"><Check :size="20" /><span>当前配置无诊断项</span></div>
          <button
            v-for="(item, index) in diagnostics"
            :key="`${item.code}-${index}`"
            type="button"
            class="preparation-diagnostic-row"
            :class="item.severity"
            @click="locateDiagnostic(item)"
          >
            <AlertCircle :size="17" />
            <span><strong>{{ item.code }}</strong><span>{{ item.message }}</span><small>{{ item.nodeId ? titleOf(item.nodeId) : item.edgeId ?? "装配" }}{{ item.field ? ` · ${item.field}` : "" }}</small></span>
            <ChevronRight :size="14" />
          </button>
        </section>
      </div>

      <aside class="preparation-inspector" aria-label="节点配置">
        <header class="preparation-inspector-header">
          <strong>{{ selectedNode ? "节点配置" : selectedEdge ? "连线配置" : "准备图" }}</strong>
          <button v-if="selectedNode || selectedEdge" type="button" class="preparation-icon-button danger" :disabled="selectedNode?.kind === 'assemble' && !unified" aria-label="删除选中项" title="删除选中项" @click="removeSelection"><Trash2 :size="15" /></button>
        </header>
        <div v-if="selectedNode" class="preparation-inspector-scroll">
          <label class="preparation-field">
            <span>名称</span>
            <input :value="selectedNode.title" @input="preparation.updateNodeTitle(workflowId, stage, selectedNode.id, ($event.target as HTMLInputElement).value)" />
          </label>
          <div class="preparation-node-meta"><code>{{ selectedNode.id }}</code><span>{{ preparationNodeCatalog.find(kind => kind.kind === selectedNode?.kind)?.label }}</span></div>
          <fieldset v-if="selectedNode.kind !== 'assemble'" class="preparation-public-outputs">
            <legend>公开输出</legend>
            <label v-for="port in preparationPorts(selectedNode).filter(p => p.direction === 'output')" :key="port.id" class="preparation-checkbox">
              <input type="checkbox" :checked="selectedNode.publicOutputs?.includes(port.id) ?? false"
                :disabled="(selectedNode.kind === 'session-data-read' || selectedNode.kind === 'session-data-write') && !selectedNode.config.definition.public"
                @change="preparation.setNodePublicOutput(workflowId, stage, selectedNode.id, port.id, ($event.target as HTMLInputElement).checked)" /><span>{{ port.label }}</span>
            </label>
          </fieldset>
          <div v-if="nodeErrors.length" class="preparation-field-errors">
            <p v-for="(item, index) in nodeErrors" :key="index"><strong>{{ item.code }}</strong> {{ item.message }}</p>
          </div>

          <template v-if="selectedNode.kind === 'prompt-item'">
            <label class="preparation-field"><span>提示词文本</span><textarea :value="config.text as string" rows="7" @input="patch({ text: ($event.target as HTMLTextAreaElement).value })"></textarea></label>
            <RolePlacementFields v-if="presentation" :model-value="presentation" @update:model-value="patch({ presentation: $event })" />
          </template>

          <template v-else-if="selectedNode.kind === 'prompt-group'">
            <label class="preparation-checkbox"><input type="checkbox" :checked="config.enabled as boolean" @change="patch({ enabled: ($event.target as HTMLInputElement).checked })" /><span>启用条目组</span></label>
            <div class="preparation-section-heading"><strong>有序条目 {{ members.length }}</strong><button type="button" class="preparation-icon-button" aria-label="添加提示词条目" title="添加提示词条目" @click="addMember"><Plus :size="15" /></button></div>
            <details v-for="(member, index) in members" :key="member.id" class="preparation-member" :open="index === 0">
              <summary><span>{{ index + 1 }}</span><strong>{{ member.name || "未命名条目" }}</strong><span class="preparation-member-role">{{ member.presentation.role }}</span></summary>
              <div class="preparation-member-actions">
                <button type="button" class="preparation-icon-button" aria-label="上移条目" title="上移条目" :disabled="index === 0" @click="moveMember(index, -1)"><ArrowUp :size="14" /></button>
                <button type="button" class="preparation-icon-button" aria-label="下移条目" title="下移条目" :disabled="index === members.length - 1" @click="moveMember(index, 1)"><ArrowDown :size="14" /></button>
                <button type="button" class="preparation-icon-button danger" aria-label="删除条目" title="删除条目" @click="patch({ members: members.filter((_, at) => at !== index) })"><Trash2 :size="14" /></button>
              </div>
              <label class="preparation-field"><span>条目名称</span><input :value="member.name" @input="updateMember(index, { name: ($event.target as HTMLInputElement).value })" /></label>
              <label class="preparation-field"><span>文本</span><textarea :value="member.text" rows="4" @input="updateMember(index, { text: ($event.target as HTMLTextAreaElement).value })"></textarea></label>
              <RolePlacementFields :model-value="member.presentation" @update:model-value="updateMember(index, { presentation: $event })" />
            </details>
          </template>

          <template v-else-if="selectedNode.kind === 'prompt-collect' || selectedNode.kind === 'tool-collect'">
            <div class="preparation-section-heading"><strong>输入顺序</strong><span class="preparation-muted">{{ inputIds.length }}</span></div>
            <div v-for="(id, index) in inputIds" :key="`${id}-${index}`" class="preparation-input-row">
              <span>{{ index + 1 }}</span><strong :title="id">{{ titleOf(id) }}</strong>
              <button type="button" class="preparation-icon-button" aria-label="上移来源" title="上移来源" :disabled="index === 0" @click="moveInput(index, -1)"><ArrowUp :size="14" /></button>
              <button type="button" class="preparation-icon-button" aria-label="下移来源" title="下移来源" :disabled="index === inputIds.length - 1" @click="moveInput(index, 1)"><ArrowDown :size="14" /></button>
            </div>
            <p v-if="!inputIds.length" class="preparation-empty-state">未连接来源</p>
          </template>

          <template v-else-if="selectedNode.kind === 'tool'">
            <div class="preparation-tool-identity"><Wrench :size="14" /><code>{{ (config.toolRef as { name: string }).name }}</code><span>{{ (config.toolRef as { version: string }).version }}</span></div>
            <section class="preparation-inspector-band">
              <h3>工具描述</h3>
              <pre class="preparation-tool-text">{{ toolDefinition?.description ?? "未找到工具定义" }}</pre>
              <RolePlacementFields :model-value="config.description as RolePlacement" @update:model-value="patch({ description: $event })" />
            </section>
            <section class="preparation-inspector-band">
              <h3><FileJson :size="14" />参数 schema</h3>
              <pre class="preparation-tool-text">{{ displayJson(toolDefinition?.parametersSchema) }}</pre>
              <RolePlacementFields :model-value="config.schema as RolePlacement" @update:model-value="patch({ schema: $event })" />
            </section>
          </template>

          <template v-else-if="selectedNode.kind === 'context'">
            <label class="preparation-field"><span>来源 Agent</span><select :value="config.sourceBindingId ?? AGENT_BINDINGS[stage]" @change="patch({ sourceBindingId: ($event.target as HTMLSelectElement).value })"><option :value="AGENT_BINDINGS.A">Agent A</option><option :value="AGENT_BINDINGS.B">Agent B</option></select></label>
            <div class="preparation-context-heading"><strong>{{ contextStatus }}</strong><button type="button" class="preparation-icon-button" title="读取归档上下文" aria-label="读取归档上下文" :disabled="!contextScope || !!context.loading" @click="context.refresh()"><RefreshCw :size="15" /></button></div>
            <dl class="preparation-context-info">
              <dt>来源 Agent</dt><dd>Agent {{ config.sourceBindingId === AGENT_BINDINGS.B ? "B" : "A" }}</dd>
              <dt>工作流</dt><dd><code>{{ workflowId }}</code></dd>
              <dt>正式读取</dt><dd>{{ config.source === "backend-read" ? "已读取" : "未读取" }}</dd>
              <dt>来源</dt><dd>{{ config.source }}</dd>
              <dt>会话</dt><dd><code>{{ context.read?.scope.workflow_session_id ?? "无" }}</code></dd>
              <dt>选中回合</dt><dd><code>{{ context.read?.scope.parent_turn_id ?? "无" }}</code></dd>
              <dt>楼层 / 消息</dt><dd>{{ context.read?.logical_floors.length ?? 0 }} / {{ context.read?.messages.length ?? 0 }}</dd>
            </dl>
            <p v-if="context.error" class="preparation-read-error">{{ context.error.reason }}</p>
            <h3 class="preparation-section-label">引用</h3>
            <pre class="preparation-json-block">{{ displayJson(config.reference) }}</pre>
            <h3 class="preparation-section-label">上下文集合</h3>
            <pre class="preparation-json-block">{{ displayJson(preview.context) }}</pre>
          </template>

          <template v-else-if="selectedNode.kind === 'global-content'">
            <label class="preparation-field"><span>全局内容</span><select :value="config.resourceId ?? ''" @change="patch({ resourceId: ($event.target as HTMLSelectElement).value || null })"><option value="">未绑定</option><option v-for="record in content.records" :key="record.resource_id" :value="record.resource_id">{{ record.name }} · r{{ record.revision }}{{ record.enabled ? '' : ' · 已停用' }}</option></select></label>
          </template>
          <template v-else-if="selectedNode.kind === 'session-data-read' || selectedNode.kind === 'session-data-write'">
            <label class="preparation-field"><span>已注册会话数据</span><select :value="selectedNode.config.definition.definition_id" @change="patch({ definition: content.definitions.find(d => d.definition_id === ($event.target as HTMLSelectElement).value) })"><option v-for="definition in content.definitions" :key="definition.definition_id" :value="definition.definition_id">{{ definition.name }} · {{ definition.key }}</option></select></label>
            <pre class="preparation-json-block">{{ displayJson(selectedNode.config.definition.schema) }}</pre>
            <label v-if="selectedNode.kind === 'session-data-write'" class="preparation-field"><span>JSON 值</span><textarea :value="displayJson(selectedNode.config.value)" @change="patchJsonValue($event)"></textarea></label>
          </template>

          <template v-else-if="selectedNode.kind === 'root-input'">
            <label class="preparation-field"><span>{{ stage === "A" ? "当前输入" : "A 业务结果预览" }}</span><textarea :value="config.text as string" rows="9" @input="patch({ text: ($event.target as HTMLTextAreaElement).value })"></textarea></label>
            <span class="preparation-state-label">不写入正式历史</span>
          </template>

          <IncrementalNodeFields
            v-else-if="['text', 'text-to-prompt', 'prompt-to-text', 'regex', 'variable-register', 'variable-assign', 'variable-replace'].includes(selectedNode.kind)"
            :key="selectedNode.id"
            :node="selectedNode"
            :workflow-id="workflowId"
            :stage="stage"
            :has-text-input="draft.edges.some(edge => edge.target === selectedNode?.id && edge.targetHandle === 'text')"
            @patch="patch"
          />

          <template v-else-if="selectedNode.kind === 'assemble'">
            <dl class="preparation-context-info"><dt>消息数</dt><dd>{{ preview.messages.length }}</dd><dt>材料数</dt><dd>{{ preview.materials.length }}</dd><dt>来源</dt><dd>local-preview</dd></dl>
            <button type="button" class="preparation-command" @click="activeView = 'preview'"><FileJson :size="15" /><span>查看装配结果</span></button>
          </template>
        </div>
        <div v-else-if="selectedEdge" class="preparation-inspector-scroll">
          <dl class="preparation-context-info"><dt>来源</dt><dd>{{ titleOf(selectedEdge.source) }}</dd><dt>输出</dt><dd>{{ selectedEdge.sourceHandle }}</dd><dt>目标</dt><dd>{{ titleOf(selectedEdge.target) }}</dd><dt>输入</dt><dd>{{ selectedEdge.targetHandle }}</dd></dl>
          <div class="preparation-field-errors"><p v-for="(item, index) in diagnostics.filter(item => item.edgeId === selectedEdge?.id)" :key="index"><strong>{{ item.code }}</strong> {{ item.message }}</p></div>
        </div>
        <div v-else class="preparation-inspector-scroll">
          <dl class="preparation-context-info"><dt>预览目标</dt><dd>Agent {{ previewStage }}</dd><dt>节点</dt><dd>{{ flowNodes.length }}</dd><dt>连线</dt><dd>{{ flowEdges.length }}</dd><dt>正式上下文</dt><dd>{{ contextStatus }}</dd><dt>配置</dt><dd><code>{{ draft.configId }}</code></dd></dl>
          <div class="preparation-empty-selection">未选中节点</div>
        </div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.preparation-workbench {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: #202023;
  color: #dadbe0;
  letter-spacing: 0;
}
.preparation-toolbar {
  display: flex;
  align-items: center;
  flex: 0 0 38px;
  gap: 7px;
  min-width: 0;
  padding: 0 8px;
  background: #29292c;
  border-bottom: 1px solid #414145;
}
.preparation-toolbar > strong {
  font-size: 11px;
  white-space: nowrap;
}
.preparation-breadcrumb {
  color: #9598a0;
  font-size: 11px;
  white-space: nowrap;
}
.preparation-muted {
  color: #9598a0;
  font-size: 10px;
}
.preparation-toolbar > .preparation-tabs {
  margin-left: auto;
}
.preparation-tabs {
  display: flex;
  flex: 0 0 auto;
  align-items: stretch;
  height: 100%;
}
.preparation-tabs button {
  display: flex;
  align-items: center;
  gap: 5px;
  height: 100%;
  padding: 0 9px;
  border-bottom: 2px solid transparent;
  color: #a5a7af;
  font-size: 11px;
  white-space: nowrap;
}
.preparation-tabs button.active {
  color: #c7e1d4;
  border-bottom-color: #86b7a0;
  background: #323735;
}
.preparation-draft-badge {
  margin-left: 5px;
  color: #c6b478;
  font-size: 10px;
  white-space: nowrap;
}
.preparation-diagnostic-count {
  min-width: 15px;
  padding: 1px 3px;
  border-radius: 3px;
  color: #f3b3b3;
  background: #59383b;
  font-size: 9px;
  text-align: center;
}
.preparation-body {
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
}
.preparation-main {
  flex: 1;
  min-width: 0;
  min-height: 0;
}
.preparation-canvas {
  position: relative;
  width: 100%;
  height: 100%;
  background: #1e1e21;
}
.preparation-canvas :deep(.vue-flow) {
  background: #1e1e21;
}
.preparation-icon-button {
  display: inline-flex;
  flex: 0 0 26px;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: 3px;
  color: #b2b5be;
}
.preparation-icon-button.danger {
  color: #d18e8e;
}
.preparation-add-wrap {
  position: absolute;
  top: 12px;
  left: 12px;
  z-index: 5;
}
.preparation-add-button,
.preparation-command {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 30px;
  padding: 0 10px;
  border: 1px solid #515156;
  border-radius: 4px;
  background: #2b2b2f;
  font-size: 11px;
}
.preparation-canvas-tools {
  position: absolute;
  bottom: 15px;
  left: 12px;
  display: flex;
  align-items: center;
  gap: 2px;
  height: 36px;
  padding: 3px 4px;
  border: 1px solid #48484e;
  border-radius: 4px;
  background: #2d2d31;
}
.preparation-canvas-tools button {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border-radius: 3px;
  color: #adb2bd;
}
.preparation-canvas-tools button.active {
  color: #bfdacb;
  background: #3b4942;
}
.preparation-tool-divider {
  width: 1px;
  height: 16px;
  margin: 0 3px;
  background: #48484d;
}
.preparation-canvas-count {
  position: absolute;
  bottom: 59px;
  left: 14px;
  color: #90959f;
  font-size: 10px;
  pointer-events: none;
}
.preparation-inspector {
  display: flex;
  flex: 0 0 294px;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  border-left: 1px solid #424247;
  background: #27272b;
}
.preparation-inspector-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex: 0 0 37px;
  padding: 0 12px;
  border-bottom: 1px solid #424247;
}
.preparation-inspector-header strong {
  font-size: 12px;
  font-weight: 600;
}
.preparation-inspector-scroll {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-height: 0;
  padding: 14px 12px 22px;
  overflow: auto;
}
.preparation-field {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 5px;
  color: #a7aab2;
  font-size: 11px;
}
.preparation-field input,
.preparation-field textarea {
  width: 100%;
  padding: 6px 8px;
  border-color: #48484d;
  background: #202024;
  font-size: 12px;
  line-height: 1.55;
}
.preparation-field input {
  height: 30px;
}
.preparation-node-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: #898e98;
  font-size: 10px;
  overflow-wrap: anywhere;
}
.preparation-node-meta code {
  font-size: 9px;
}
.preparation-field-errors {
  display: flex;
  flex-direction: column;
  gap: 7px;
  color: #eba3a3;
  font-size: 10px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.preparation-field-errors strong {
  font-weight: 600;
}
.preparation-checkbox {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}
.preparation-checkbox input {
  width: 14px;
  height: 14px;
  margin: 0;
  accent-color: #8cc9b3;
}
.preparation-section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 26px;
  font-size: 12px;
}
.preparation-section-heading strong {
  font-weight: 600;
}
.preparation-member {
  min-width: 0;
  border-top: 1px solid #434348;
  padding-top: 4px;
}
.preparation-member summary {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 29px;
  list-style: none;
  cursor: pointer;
  font-size: 11px;
}
.preparation-member summary::before {
  content: "+";
  color: #9da2ad;
}
.preparation-member[open] summary::before {
  content: "-";
}
.preparation-member summary > strong {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}
.preparation-member-role {
  color: #8da795;
  font-size: 9px;
}
.preparation-member-actions {
  display: flex;
  justify-content: flex-end;
  height: 28px;
  margin-bottom: 6px;
}
.preparation-member > .preparation-field {
  margin-bottom: 10px;
}
.preparation-member :deep(.presentation-fields) {
  margin-bottom: 9px;
}
.preparation-input-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 34px;
  border-bottom: 1px solid #3e3e43;
  color: #b1b4bd;
  font-size: 11px;
}
.preparation-input-row > strong {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}
.preparation-tool-identity {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #d8bd81;
  font-size: 11px;
}
.preparation-tool-identity > span {
  margin-left: auto;
  color: #a7a9b2;
  font-size: 10px;
}
.preparation-inspector-band {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  padding-top: 13px;
  border-top: 1px solid #434348;
}
.preparation-inspector-band h3 {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
}
.preparation-tool-text,
.preparation-json-block {
  max-width: 100%;
  margin: 0;
  padding: 10px;
  border: 1px solid #414146;
  border-radius: 3px;
  color: #aeb7c3;
  background: #222225;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 10px;
  line-height: 1.6;
}
.preparation-context-info {
  display: grid;
  grid-template-columns: 73px minmax(0, 1fr);
  gap: 10px 7px;
  margin: 0;
  font-size: 11px;
}
.preparation-context-info dt {
  color: #9196a0;
}
.preparation-context-info dd {
  margin: 0;
  color: #bfc4ce;
  overflow-wrap: anywhere;
}
.preparation-context-info code {
  font-size: 9px;
}
.preparation-section-label {
  font-size: 12px;
  font-weight: 500;
  color: #c8cad1;
}
.preparation-state-label {
  color: #9b9fba;
  font-size: 10px;
}
.preparation-empty-selection {
  padding: 34px 0;
  color: #818792;
  text-align: center;
  font-size: 12px;
}
.preparation-preview,
.preparation-diagnostics {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 0;
  background: #222226;
}
.preparation-preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex: 0 0 38px;
  padding: 0 12px;
  border-bottom: 1px solid #414146;
}
.preparation-preview-source,
.preparation-context-heading {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  font-size: 11px;
}
.preparation-preview-source {
  flex: 0 0 auto;
  margin-left: auto;
}
.preparation-preview-source button:not(.preparation-icon-button) {
  min-width: 44px;
  height: 26px;
  padding: 0 7px;
  color: #a7adb7;
  font-size: 11px;
}
.preparation-preview-source button.active {
  color: #c7e1d4;
  background: #35423b;
}
.preparation-context-heading {
  justify-content: space-between;
}
.preparation-context-heading strong {
  font-size: 12px;
  font-weight: 500;
}
.preparation-preview-basis {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  color: #929da8;
  font-size: 10px;
}
.preparation-read-error {
  color: #eba3a3;
  font-size: 11px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.preparation-ephemeral-tag {
  color: #97b9d7;
  font-size: 9px;
}
.preparation-message-source {
  margin-left: auto;
  color: #8f98a5;
  font-size: 10px;
}
.preparation-preview-scroll {
  display: flex;
  flex-direction: column;
  gap: 15px;
  min-height: 0;
  padding: 16px;
  overflow: auto;
}
.preparation-empty-state {
  padding: 22px 0;
  color: #8b919c;
  text-align: center;
  font-size: 12px;
}
.preparation-message {
  padding-bottom: 14px;
  border-bottom: 1px solid #414146;
}
.preparation-message header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 26px;
  margin-bottom: 7px;
  font-size: 11px;
}
.preparation-message header strong {
  color: #b2ceb9;
  font-weight: 500;
}
.preparation-message header button {
  margin-left: auto;
  min-width: 0;
  max-width: 60%;
  padding: 4px 6px;
  color: #9db6d1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10px;
}
.preparation-message-index {
  min-width: 17px;
  color: #888d98;
  text-align: center;
}
.preparation-protected-tag {
  color: #d5b781;
  font-size: 9px;
}
.preparation-message pre {
  margin: 0;
  color: #d1d4dc;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.preparation-message footer {
  margin-top: 9px;
  color: #747b87;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 9px;
  overflow-wrap: anywhere;
}
.preparation-diagnostics {
  overflow: auto;
}
.preparation-valid-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  margin-top: 36px;
  color: #a2c7b0;
}
.preparation-diagnostic-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 100%;
  padding: 15px;
  border-bottom: 1px solid #404045;
  color: #e2a0a0;
  text-align: left;
}
.preparation-diagnostic-row.warning {
  color: #d4bd84;
}
.preparation-diagnostic-row > svg {
  flex: 0 0 auto;
  margin-top: 2px;
}
.preparation-diagnostic-row > svg:last-child {
  margin-left: auto;
}
.preparation-diagnostic-row > span {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
  gap: 5px;
}
.preparation-diagnostic-row strong {
  font-size: 11px;
  font-weight: 500;
}
.preparation-diagnostic-row > span > span {
  color: #d0d1d8;
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.preparation-diagnostic-row small {
  color: #989daa;
  font-size: 10px;
  overflow-wrap: anywhere;
}
</style>
