<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from "vue";
import { MarkerType, VueFlow, type VueFlowStore } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import {
  Hand,
  Minus,
  MousePointer2,
  Plus,
  Redo2,
  Save,
  Scan,
  Undo2,
  Workflow,
} from "lucide-vue-next";
import MainFlowNode from "./MainFlowNode.vue";
import ModelProviderNode from "./ModelProviderNode.vue";
import CanvasNodeMenu from "./CanvasNodeMenu.vue";
import { useCanvasNodeMenu } from "../composables/useCanvasNodeMenu";
import { preparationMenuItems } from "../domain/canvasMenu";
import type { PreparationNodeKind, PreparationStage } from "../domain/preparation";
import { usePreparationStore } from "../stores/preparation";
import { useWorkspaceStore } from "../stores/workspace";
import { useModelConfigurationStore } from "../stores/modelConfiguration";
import { dependencyTarget, isCanvasModelConnection, MAX_MODEL_NODES, type CanvasModelConnection } from "../domain/modelConfiguration";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";

const emit = defineEmits<{
  "open-preparation": [stage: "A" | "B"];
  "open-exposure": [stage: "A" | "B"];
  "open-observation": [stage: "A" | "B" | "Output"];
  "add-node": [stage: PreparationStage, kind: PreparationNodeKind];
}>();
const workspace = useWorkspaceStore();
const preparation = usePreparationStore();
const models = useModelConfigurationStore();
const modelDraft = computed(() => models.getDraft(workspace.activeWorkflowId));
const canvas = ref<HTMLElement | null>(null);
const nodeMenu = useCanvasNodeMenu(canvas);
const menuGroups = computed(() => [
  ...(modelDraft.value ? [{ label: "模型", items: [{ id: "model-provider", label: "模型提供", disabled: modelDraft.value.nodes.length >= MAX_MODEL_NODES }] }] : []),
  ...workspace.nodes.flatMap((node) => {
  const stage = node.data.stage;
  if (stage !== "A" && stage !== "B") return [];
  return [{
    label: `Agent ${stage}`,
    items: preparationMenuItems(preparation.getDraft(workspace.activeWorkflowId, stage))
      .map((item) => ({ ...item, id: `${stage}:${item.id}` })),
  }];
})]);
function addFromMenu(id: string) {
  const item = menuGroups.value.flatMap((group) => group.items).find((item) => item.id === id);
  if (!item || item.disabled) return;
  if (id === "model-provider") {
    const point = nodeMenu.menu.value;
    if (!point || !flow.value) return;
    const position = flow.value.screenToFlowCoordinate({ x: point.x, y: point.y });
    models.addNode(workspace.activeWorkflowId, position);
    nodeMenu.close();
    return;
  }
  const [stage, kind] = id.split(":") as [PreparationStage, PreparationNodeKind];
  nodeMenu.close();
  emit("add-node", stage, kind);
}
function validConnection(connection: CanvasModelConnection) {
  return isCanvasModelConnection(modelDraft.value, workspace.edges, connection);
}
const flow = shallowRef<VueFlowStore | null>(null);
const flowId = computed(
  () => `workbench-${workspace.activeWorkflowId}-${workspace.viewVersion}`,
);
const flowNodes = computed(() =>
  [...workspace.nodes.map((node) => ({
    id: node.id,
    type: "main-flow",
    position: { ...node.position },
    selected: workspace.selectedNodeIds.includes(node.id),
    data: { ...node.data },
    connectable: node.data.stage !== "Output",
  })), ...(modelDraft.value?.nodes ?? []).map((node) => ({
    id: node.id, type: "model-provider", position: { ...node.position },
    selected: models.selectedNodeId === node.id, data: { node }, connectable: true,
  }))],
);
const flowEdges = computed(() =>
  [...workspace.edges.map((edge) => ({
    ...edge,
    type: "smoothstep",
    selected: workspace.selectedEdgeIds.includes(edge.id),
    updatable: false,
    style: {
      stroke: workspace.selectedEdgeIds.includes(edge.id)
        ? "#c7d3dd"
        : "#7f919b",
      strokeWidth: 1.5,
    },
    markerEnd: {
      type: MarkerType.ArrowClosed,
      width: 16,
      height: 16,
      color: "#7f919b",
    },
  })), ...(modelDraft.value?.edges ?? []).map((edge) => ({
    id: edge.id, source: edge.source,
    target: dependencyTarget(edge.target_binding_id === AGENT_BINDINGS.A ? "A" : "B"),
    sourceHandle: "model-out", targetHandle: "model-in", type: "smoothstep",
    updatable: false, style: { stroke: "#c39dbe", strokeWidth: 1.5, strokeDasharray: "5 4" },
  }))],
);

function fitCanvas() {
  if (!flowNodes.value.length || flow.value?.id !== flowId.value) return;
  if (
    flow.value.getNodes.value.length !== flowNodes.value.length ||
    flow.value.getNodes.value.some(
      (node) => node.dimensions.width <= 0 || node.dimensions.height <= 0,
    )
  )
    return;
  void flow.value.fitView({ padding: 0.25, maxZoom: 1.05 });
}

function attachFlow(instance: VueFlowStore) {
  if (instance.id !== flowId.value) return;
  const token = workspace.currentViewToken();
  flow.value = instance;

  instance.onNodesChange((changes) => {
    if (!workspace.isCurrentView(token)) return;
    const selection = changes.filter((change) => change.type === "select");
    if (!selection.length) return;
    const ids = new Set(workspace.selectedNodeIds);
    selection.forEach((change) =>
      change.selected ? ids.add(change.id) : ids.delete(change.id),
    );
    for (const change of selection) {
      if (!modelDraft.value?.nodes.some((node) => node.id === change.id)) continue;
      if (change.selected) models.selectedNodeId = change.id;
      else if (models.selectedNodeId === change.id) models.selectedNodeId = null;
    }
    workspace.selectNodes([...ids], token);
  });
  instance.onEdgesChange((changes) => {
    if (!workspace.isCurrentView(token)) return;
    const selection = changes.filter((change) => change.type === "select");
    if (!selection.length) return;
    const ids = new Set(workspace.selectedEdgeIds);
    selection.forEach((change) =>
      change.selected ? ids.add(change.id) : ids.delete(change.id),
    );
    workspace.selectEdges([...ids], token);
  });
  function moved(nodes: { id: string; position: { x: number; y: number } }[]) {
    if (!workspace.isCurrentView(token)) return;
    workspace.moveNodes(
      nodes.map((node) => ({ id: node.id, position: { ...node.position } })),
      token,
    );
    for (const move of nodes) {
      const model = modelDraft.value?.nodes.find((node) => node.id === move.id);
      if (model && Number.isFinite(move.position.x) && Number.isFinite(move.position.y))
        model.position = { ...move.position };
    }
  }
  instance.onNodeDragStop(({ nodes }) => moved(nodes));
  instance.onSelectionDragStop(({ nodes }) => moved(nodes));
  instance.onConnect((connection) => {
    if (workspace.isCurrentView(token)) models.connect(
      workspace.activeWorkflowId, connection.source, connection.target,
      connection.sourceHandle ?? null, connection.targetHandle ?? null,
    );
  });
  instance.onPaneClick(() => {
    if (workspace.isCurrentView(token)) {
      workspace.clearSelection();
      models.selectedNodeId = null;
    }
  });
  instance.onNodesInitialized(() => {
    if (workspace.isCurrentView(token)) fitCanvas();
  });
  void nextTick().then(() => {
    if (workspace.isCurrentView(token)) fitCanvas();
  });
}

function keyboard(event: KeyboardEvent) {
  if (
    (event.target as HTMLElement).closest(
      "input, textarea, select, [contenteditable]",
    )
  )
    return;
  const modified = event.ctrlKey || event.metaKey;
  if (modified && event.key.toLowerCase() === "z") {
    event.preventDefault();
    event.shiftKey ? workspace.redo() : workspace.undo();
  } else if (modified && event.key.toLowerCase() === "y") {
    event.preventDefault();
    workspace.redo();
  } else if (event.key === "Escape") {
    workspace.clearSelection();
    models.selectedNodeId = null;
    flow.value?.removeSelectedElements();
  } else if (event.key.startsWith("Arrow") && workspace.selectedNodeIds.length) {
    event.preventDefault();
    const step = event.shiftKey ? 20 : 5;
    workspace.moveNodes(
      workspace.nodes
        .filter((node) => workspace.selectedNodeIds.includes(node.id))
        .map((node) => ({
          id: node.id,
          position: {
            x:
              node.position.x +
              (event.key === "ArrowRight"
                ? step
                : event.key === "ArrowLeft"
                  ? -step
                  : 0),
            y:
              node.position.y +
              (event.key === "ArrowDown"
                ? step
                : event.key === "ArrowUp"
                  ? -step
                  : 0),
          },
        })),
    );
  }
}

watch(
  () => workspace.viewVersion,
  () => {
    flow.value = null;
    nodeMenu.close();
  },
  { flush: "sync" },
);
</script>

<template>
  <section
    ref="canvas"
    class="workbench-canvas"
    tabindex="0"
    :aria-label="`${workspace.activeWorkflow.title}画布`"
    @keydown.capture="keyboard"
    @contextmenu="nodeMenu.contextMenu"
    @pointermove="nodeMenu.pointerMove"
    @pointerleave="nodeMenu.pointerLeave"
  >
    <VueFlow
      :id="flowId"
      :key="flowId"
      :nodes="flowNodes"
      :edges="flowEdges"
      :min-zoom="0.35"
      :max-zoom="2"
      :delete-key-code="null"
      :disable-keyboard-a11y="true"
      :nodes-connectable="true"
      :is-valid-connection="validConnection"
      :edges-updatable="false"
      :connect-on-click="false"
      :zoom-on-double-click="false"
      :multi-selection-key-code="'Shift'"
      :pan-on-drag="workspace.interactionMode === 'pan' ? [0, 1] : [1]"
      :nodes-draggable="workspace.interactionMode === 'select'"
      :selection-on-drag="workspace.interactionMode === 'select'"
      :apply-default="true"
      :fit-view-on-init="true"
      @init="attachFlow"
    >
      <template #node-main-flow="props">
        <MainFlowNode
          v-bind="props"
          @open-preparation="emit('open-preparation', $event)"
          @open-exposure="emit('open-exposure', $event)"
          @open-observation="emit('open-observation', $event)"
        />
      </template>
      <template #node-model-provider="props">
        <ModelProviderNode v-bind="props" />
      </template>
      <Background :gap="22" :size="1" pattern-color="#3b4147" />
    </VueFlow>

    <div v-if="!workspace.nodes.length" class="workbench-canvas-empty">
      <Workflow :size="26" :stroke-width="1.4" />
      <span>{{ workspace.activeWorkflow.title }}</span>
    </div>

    <div class="workbench-canvas-tools" aria-label="画布工具">
      <div class="workbench-canvas-tool-group" role="group" aria-label="操作模式">
        <button
          type="button"
          :class="{ 'workbench-canvas-tool-active': workspace.interactionMode === 'select' }"
          aria-label="选择模式"
          title="选择模式"
          :aria-pressed="workspace.interactionMode === 'select'"
          @click="workspace.interactionMode = 'select'"
        >
          <MousePointer2 :size="17" />
        </button>
        <button
          type="button"
          :class="{ 'workbench-canvas-tool-active': workspace.interactionMode === 'pan' }"
          aria-label="平移模式"
          title="平移模式"
          :aria-pressed="workspace.interactionMode === 'pan'"
          @click="workspace.interactionMode = 'pan'"
        >
          <Hand :size="17" />
        </button>
      </div>
      <span class="workbench-canvas-tool-divider"></span>
      <button
        type="button"
        aria-label="撤销移动"
        title="撤销移动"
        :disabled="!workspace.canUndo"
        @click="workspace.undo"
      >
        <Undo2 :size="17" />
      </button>
      <button
        type="button"
        aria-label="重做移动"
        title="重做移动"
        :disabled="!workspace.canRedo"
        @click="workspace.redo"
      >
        <Redo2 :size="17" />
      </button>
      <span class="workbench-canvas-tool-divider"></span>
      <button
        type="button"
        aria-label="缩小"
        title="缩小"
        @click="flow?.zoomOut()"
      >
        <Minus :size="17" />
      </button>
      <button
        type="button"
        aria-label="放大"
        title="放大"
        @click="flow?.zoomIn()"
      >
        <Plus :size="17" />
      </button>
      <button
        type="button"
        aria-label="适应画布"
        title="适应画布"
        :disabled="!workspace.nodes.length"
        @click="fitCanvas"
      >
        <Scan :size="17" />
      </button>
      <button
        v-if="modelDraft && (modelDraft.nodes.length || modelDraft.backendRevision)"
        type="button"
        title="保存模型配置到后端"
        aria-label="保存模型配置到后端"
        :disabled="models.locked"
        @click="models.saveModels(workspace.activeWorkflowId)"
      ><Save :size="17" /></button>
    </div>
    <div class="workbench-canvas-summary">
      <span>{{ flowNodes.length }} 节点</span>
      <span class="workbench-canvas-summary-divider"></span>
      <span>{{ flowEdges.length }} 连线</span>
    </div>
    <CanvasNodeMenu
      :state="nodeMenu.menu.value"
      :groups="menuGroups"
      @add="nodeMenu.open('add', nodeMenu.menu.value ?? undefined)"
      @back="nodeMenu.open('context', nodeMenu.menu.value ?? undefined)"
      @select="addFromMenu"
    />
    <div v-if="models.diagnostics.length && modelDraft" class="model-diagnostics" role="status">
      <span v-for="diagnostic in models.diagnostics" :key="`${diagnostic.target}:${diagnostic.code}`">{{ diagnostic.code }} · {{ diagnostic.target }}</span>
    </div>
  </section>
</template>

<style scoped>
.workbench-canvas {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
  background: #1d2024;
  outline-offset: -3px;
}
.workbench-canvas :deep(.vue-flow) {
  background: #1d2024;
}
.workbench-canvas :deep(.vue-flow__node-main-flow) {
  border-radius: 6px;
}
.workbench-canvas :deep(.vue-flow__edge.selected .vue-flow__edge-path) {
  stroke: #c7d3dd;
}
.workbench-canvas :deep(.vue-flow__selection),
.workbench-canvas :deep(.vue-flow__nodesselection-rect) {
  border-color: #88b9a9;
  background: rgb(121 201 182 / 7%);
}
.workbench-canvas-empty {
  position: absolute;
  top: 50%;
  left: 50%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 13px;
  color: #73808b;
  font-size: 13px;
  transform: translate(-50%, -50%);
  pointer-events: none;
}
.workbench-canvas-tools {
  position: absolute;
  bottom: 19px;
  left: 20px;
  display: flex;
  align-items: center;
  gap: 3px;
  height: 42px;
  padding: 4px 6px;
  border: 1px solid #414852;
  border-radius: 6px;
  background: #292e35;
  box-shadow: 0 4px 12px rgb(0 0 0 / 13%);
}
.workbench-canvas-tools button {
  display: grid;
  flex: 0 0 32px;
  place-items: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border-radius: 4px;
  color: #b8c2cc;
}
.workbench-canvas-tools button:not(:disabled):hover {
  background: #3c434d;
}
.workbench-canvas-tools button.workbench-canvas-tool-active {
  color: #b2decd;
  background: #3b544d;
}
.workbench-canvas-tool-group {
  display: flex;
  gap: 2px;
}
.workbench-canvas-tool-divider {
  flex: 0 0 1px;
  width: 1px;
  height: 18px;
  margin: 0 4px;
  background: #48505a;
}
.workbench-canvas-summary {
  position: absolute;
  right: 20px;
  bottom: 25px;
  display: flex;
  align-items: center;
  gap: 11px;
  height: 22px;
  color: #84929d;
  font-size: 11px;
  pointer-events: none;
}
.workbench-canvas-summary-divider {
  width: 1px;
  height: 10px;
  background: #45505a;
}
.model-diagnostics {
  position: absolute;
  top: 12px;
  left: 16px;
  display: grid;
  max-width: 430px;
  gap: 4px;
  padding: 8px 10px;
  border-left: 2px solid #d89a87;
  background: #332829;
  color: #f2b9ab;
  font-size: 10px;
  overflow-wrap: anywhere;
  pointer-events: none;
}
</style>
