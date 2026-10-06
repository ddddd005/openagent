<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import {
  VueFlow,
  useVueFlow,
  type Connection,
  type Node,
} from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { MiniMap } from "@vue-flow/minimap";
import {
  Minus,
  Plus,
  Scan,
  Undo2,
  Redo2,
  Group,
  Ungroup,
  Trash2,
  Hand,
  MousePointer2,
  ChevronRight,
  CornerUpLeft,
  ListChecks,
} from "lucide-vue-next";
import WorkflowNode from "./WorkflowNode.vue";
import { useEditorStore } from "../stores/editor";

const editor = useEditorStore();
const { fitView, zoomIn, zoomOut, getSelectedNodes, getNodes } = useVueFlow({
  id: "prompt-editor",
});
const mode = ref<"select" | "pan">("select");
const multiple = ref(false);
const scopeTitle = computed(
  () => editor.draft.nodes.find((n) => n.id === editor.scope)?.title,
);
const flowNodes = computed(() =>
  editor.visibleNodes.map((n) => ({
    id: n.id,
    type: "workflow",
    position: { ...editor.draft.layout[n.id] },
    selected: editor.selected.includes(n.id),
    data: {
      node: n,
      invalidPorts: editor.diagnostics
        .filter((d) => d.nodeId === n.id)
        .map((d) => d.portId),
    },
  })),
);
const flowEdges = computed(() =>
  editor.visibleEdges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.sourcePort,
    targetHandle: e.targetPort,
    type: "default",
    selected: editor.selectedEdge === e.id,
    class: editor.diagnostics.some((d) => d.edgeId === e.id)
      ? "invalid-edge"
      : "",
    style: {
      stroke: editor.diagnostics.some((d) => d.edgeId === e.id)
        ? "#e48080"
        : "#737d91",
      strokeWidth: 1.7,
    },
  })),
);
function connect(connection: Connection) {
  if (connection.sourceHandle && connection.targetHandle)
    editor.connect(
      connection.source,
      connection.sourceHandle,
      connection.target,
      connection.targetHandle,
    );
}
function select({
  node,
  event,
}: {
  node: Node;
  event: MouseEvent | TouchEvent;
}) {
  if (multiple.value || ("shiftKey" in event && event.shiftKey))
    editor.selected = editor.selected.includes(node.id)
      ? editor.selected.filter((id) => id !== node.id)
      : [...editor.selected, node.id];
  else editor.selected = [node.id];
  editor.selectedEdge = null;
}
function keyboard(event: KeyboardEvent) {
  if (
    (event.target as HTMLElement).closest(
      "input, textarea, select, [contenteditable]",
    )
  )
    return;
  const mod = event.ctrlKey || event.metaKey;
  if (event.key.startsWith("Arrow") && editor.selected.length) {
    event.preventDefault();
    event.stopPropagation();
    const step = event.shiftKey ? 20 : 5;
    editor.move(
      editor.selected.map((id) => ({
        id,
        position: {
          x:
            editor.draft.layout[id].x +
            (event.key === "ArrowRight"
              ? step
              : event.key === "ArrowLeft"
                ? -step
                : 0),
          y:
            editor.draft.layout[id].y +
            (event.key === "ArrowDown"
              ? step
              : event.key === "ArrowUp"
                ? -step
                : 0),
        },
      })),
    );
  } else if (mod && event.key.toLowerCase() === "z") {
    event.preventDefault();
    event.shiftKey ? editor.redo() : editor.undo();
  } else if (mod && event.key.toLowerCase() === "y") {
    event.preventDefault();
    editor.redo();
  } else if (mod && event.key.toLowerCase() === "g") {
    event.preventDefault();
    if (editor.groupable) editor.group();
  } else if (mod && event.key.toLowerCase() === "s") {
    event.preventDefault();
    editor.save();
  } else if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    editor.removeSelection();
  } else if (event.key === "Escape") {
    editor.selected = [];
    editor.selectedEdge = null;
  }
}
watch(
  () =>
    getNodes.value
      .map((n) => `${n.id}:${n.dimensions.width}:${n.dimensions.height}`)
      .join("|"),
  async () => {
    await nextTick();
    if (
      getNodes.value.length &&
      getNodes.value.every(
        (n) => n.dimensions.width > 0 && n.dimensions.height > 0,
      )
    )
      fitView({ padding: 0.18 });
  },
);
</script>

<template>
  <section class="canvas-section" aria-label="提示词配置画布">
    <div class="canvas-toolbar">
      <div class="breadcrumbs">
        <button @click="editor.leaveGroup">{{ editor.draft.title }}</button
        ><ChevronRight :size="14" /><span>{{ scopeTitle || "主配置" }}</span
        ><span class="subtle-badge">本地草稿</span>
      </div>
      <div class="toolbar-actions">
        <button
          class="icon-button"
          :class="{ active: multiple }"
          title="多选节点"
          aria-label="多选节点"
          :aria-pressed="multiple"
          @click="multiple = !multiple"
        >
          <ListChecks :size="17" />
        </button>
        <button
          class="icon-button"
          title="撤销 (Ctrl+Z)"
          aria-label="撤销"
          :disabled="!editor.past.length"
          @click="editor.undo"
        >
          <Undo2 :size="16" />
        </button>
        <button
          class="icon-button"
          title="重做 (Ctrl+Shift+Z)"
          aria-label="重做"
          :disabled="!editor.future.length"
          @click="editor.redo"
        >
          <Redo2 :size="16" />
        </button>
        <span class="toolbar-divider"></span>
        <button
          class="icon-button"
          title="建组 (Ctrl+G)"
          aria-label="建组"
          :disabled="!editor.groupable"
          @click="editor.group"
        >
          <Group :size="17" />
        </button>
        <button
          class="icon-button"
          title="解组"
          aria-label="解组"
          :disabled="editor.currentNode?.kind !== 'group'"
          @click="editor.ungroup"
        >
          <Ungroup :size="17" />
        </button>
        <button
          class="icon-button"
          title="删除选中"
          aria-label="删除选中"
          :disabled="!editor.selected.length && !editor.selectedEdge"
          @click="editor.removeSelection"
        >
          <Trash2 :size="16" />
        </button>
      </div>
    </div>
    <div
      class="canvas"
      tabindex="0"
      aria-label="节点画布"
      @keydown.capture="keyboard"
    >
      <VueFlow
        id="prompt-editor"
        :nodes="flowNodes"
        :edges="flowEdges"
        :min-zoom="0.55"
        :max-zoom="2"
        :disable-keyboard-a11y="true"
        :delete-key-code="null"
        :selection-key-code="'Shift'"
        :multi-selection-key-code="'Shift'"
        :pan-on-drag="mode === 'pan' ? true : [1, 2]"
        :nodes-draggable="mode === 'select'"
        :selection-on-drag="mode === 'select'"
        :apply-default="true"
        :fit-view-on-init="true"
        @nodes-initialized="() => fitView({ padding: 0.18 })"
        @connect="connect"
        @node-click="select"
        @node-double-click="
          ({ node }) =>
            node.data.node.kind === 'group' && editor.enterGroup(node.id)
        "
        @selection-end="
          () => (editor.selected = getSelectedNodes.map((n) => n.id))
        "
        @node-drag-stop="
          ({ nodes }) =>
            editor.move(nodes.map((n) => ({ id: n.id, position: n.position })))
        "
        @edge-click="
          ({ edge }) => {
            editor.selected = [];
            editor.selectedEdge = edge.id;
          }
        "
        @pane-click="
          () => {
            editor.selected = [];
            editor.selectedEdge = null;
          }
        "
      >
        <template #node-workflow="props"
          ><WorkflowNode v-bind="props"
        /></template>
        <Background :gap="22" :size="1" pattern-color="#363c46" />
        <MiniMap
          :width="138"
          :height="94"
          position="bottom-right"
          :pannable="true"
          :zoomable="true"
          :node-color="'#566174'"
          :mask-color="'rgba(18,21,26,.65)'"
        />
      </VueFlow>
      <button
        v-if="editor.scope !== 'root'"
        class="leave-group"
        @click="editor.leaveGroup"
      >
        <CornerUpLeft :size="15" /> 返回主配置
      </button>
      <div class="canvas-controls">
        <div class="segmented">
          <button
            class="icon-button"
            :class="{ active: mode === 'select' }"
            aria-label="选择模式"
            title="选择模式"
            @click="mode = 'select'"
          >
            <MousePointer2 :size="17" /></button
          ><button
            class="icon-button"
            :class="{ active: mode === 'pan' }"
            aria-label="平移模式"
            title="平移模式"
            @click="mode = 'pan'"
          >
            <Hand :size="17" />
          </button>
        </div>
        <button
          class="icon-button"
          aria-label="缩小"
          title="缩小"
          @click="zoomOut()"
        >
          <Minus :size="17" />
        </button>
        <button
          class="icon-button"
          aria-label="放大"
          title="放大"
          @click="zoomIn()"
        >
          <Plus :size="17" />
        </button>
        <button
          class="icon-button"
          aria-label="适应画布"
          title="适应画布"
          @click="fitView({ padding: 0.14 })"
        >
          <Scan :size="17" />
        </button>
      </div>
      <div class="canvas-caption">
        {{ editor.visibleNodes.length }} 节点 <span>·</span>
        {{ editor.visibleEdges.length }} 连线
      </div>
    </div>
  </section>
</template>
