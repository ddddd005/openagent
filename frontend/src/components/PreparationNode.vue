<script setup lang="ts">
import { computed } from "vue";
import { Handle, Position } from "@vue-flow/core";
import {
  AlertCircle,
  ArrowRightLeft,
  Braces,
  Database,
  FileText,
  Files,
  Layers,
  Library,
  ListOrdered,
  MessageSquare,
  Regex,
  SquarePen,
  Type,
  Wrench,
} from "lucide-vue-next";
import { preparationPorts, type PreparationNode as GraphNode, type PreparationPort } from "../domain/preparation";

const props = defineProps<{
  data: {
    node: GraphNode; errorCount: number;
    connectedInputs?: string[]; connectedOutputs?: string[];
    invalidInputs?: string[]; invalidOutputs?: string[];
  };
  selected?: boolean;
}>();
const kinds = {
  "prompt-item": { label: "提示词条目", icon: FileText, color: "#86c8b0" },
  "prompt-group": { label: "提示词条目组", icon: Files, color: "#86c8b0" },
  "prompt-collect": { label: "提示词汇总", icon: ListOrdered, color: "#86c8b0" },
  tool: { label: "工具", icon: Wrench, color: "#ddbd77" },
  "tool-collect": { label: "工具汇总", icon: Library, color: "#ddbd77" },
  context: { label: "上下文", icon: Database, color: "#90bce3" },
  assemble: { label: "提示词装配", icon: Layers, color: "#d8a5b2" },
  "root-input": { label: "当前输入", icon: MessageSquare, color: "#a7b3c1" },
  text: { label: "文本", icon: Type, color: "#a7b3c1" },
  "text-to-prompt": { label: "文本转提示词", icon: ArrowRightLeft, color: "#86c8b0" },
  "prompt-to-text": { label: "提示词转文本", icon: ArrowRightLeft, color: "#a7b3c1" },
  regex: { label: "正则", icon: Regex, color: "#e1a787" },
  "variable-register": { label: "变量注册", icon: Braces, color: "#c7bb78" },
  "variable-assign": { label: "变量赋值", icon: SquarePen, color: "#c7bb78" },
  "variable-replace": { label: "变量替换", icon: Braces, color: "#c7bb78" },
  "global-content": { label: "全局内容引用", icon: Library, color: "#bdcf94" },
  "session-data-read": { label: "会话数据读取", icon: Database, color: "#90bce3" },
  "session-data-write": { label: "会话数据写入", icon: SquarePen, color: "#90bce3" },
  "json-to-text": { label: "JSON 转文本", icon: ArrowRightLeft, color: "#a7b3c1" },
};
const kind = computed(() => kinds[props.data.node.kind]);
function displayedPorts(direction: "input" | "output", connected: string[]): PreparationPort[] {
  const ports = preparationPorts(props.data.node).filter((port) => port.direction === direction);
  return [...ports, ...[...new Set(connected)].filter((id) => !ports.some((port) => port.id === id))
    .map((id) => ({ id, direction, type: id, label: `${id}（不兼容）` }))];
}
const inputs = computed(() => displayedPorts("input", props.data.connectedInputs ?? []));
const outputs = computed(() => displayedPorts("output", props.data.connectedOutputs ?? []));
const stalePort = (port: PreparationPort) => !preparationPorts(props.data.node)
  .some((current) => current.direction === port.direction && current.id === port.id);
const summary = computed(() => {
  const config = props.data.node.config as unknown as Record<string, unknown>;
  switch (props.data.node.kind) {
    case "prompt-item":
    case "root-input":
    case "text":
      return typeof config.text === "string" && config.text.trim()
        ? config.text
        : "未填写文本";
    case "prompt-group":
      return `${Array.isArray(config.members) ? config.members.length : 0} 个有序条目`;
    case "prompt-collect":
    case "tool-collect":
      return `${Array.isArray(config.inputs) ? config.inputs.length : 0} 个有序来源`;
    case "tool": {
      const ref = config.toolRef as { name?: string; version?: string };
      return `${ref.name ?? "未绑定"} · ${ref.version ?? ""}`;
    }
    case "context": {
      const node = props.data.node;
      return node.kind === "context" && node.config.source === "backend-read"
        ? `${node.config.floors.length} 楼层 · ${node.config.floors.reduce((sum, floor) => sum + floor.items.length, 0)} 份上下文材料`
        : "正式上下文未读取";
    }
    case "assemble":
      return "消息集合 · 来源定位";
    case "regex":
      return `${config.mode === "text" ? "文本" : "提示词"} · ${config.pattern || "空表达式"}`;
    case "variable-register":
      return config.name ? `{{${config.name}}} · ${config.valueType}` : "未命名变量";
    case "variable-assign":
      return `${config.name ? `{{${config.name}}}` : "未指定变量"} · ${config.operation === "set" ? "设置" : config.operation === "add" ? "加" : "减"}`;
    case "variable-replace":
      return config.mode === "text" ? "文本变量替换" : "提示词变量替换";
    case "text-to-prompt":
      return "文本 → 提示词";
    case "prompt-to-text":
      return "提示词 → 文本";
    default:
      return "";
  }
});
</script>

<template>
  <article
    class="preparation-node"
    :class="{ selected, invalid: data.errorCount > 0 }"
    :style="{ '--node-accent': kind.color }"
    :aria-label="data.node.title"
  >
    <header>
      <component :is="kind.icon" :size="15" />
      <strong>{{ data.node.title }}</strong>
      <AlertCircle
        v-if="data.errorCount"
        class="preparation-node-error"
        :size="15"
        :aria-label="`${data.errorCount} 项错误`"
      />
    </header>
    <div class="preparation-node-summary">{{ summary }}</div>
    <div class="preparation-node-ports">
      <div
        v-for="(port, index) in inputs"
        :key="`in-${port.id}`"
        class="preparation-node-port input"
        :style="{ top: `${index * 20}px` }"
      >
        <Handle
          :id="port.id"
          type="target"
          :position="Position.Left"
          :connectable="!port.runtimeOnly && !stalePort(port)"
          :class="{ 'runtime-only': port.runtimeOnly, 'invalid-port': stalePort(port) || data.invalidInputs?.includes(port.id) }"
          :title="port.runtimeOnly ? '跨轮读取已归档的 Agent 上下文' : port.label"
          :aria-label="`${data.node.title} ${port.label}输入`"
        />
        <span>{{ port.label }}</span>
      </div>
      <div
        v-for="(port, index) in outputs"
        :key="`out-${port.id}`"
        class="preparation-node-port output"
        :style="{ top: `${index * 20}px` }"
      >
        <span>{{ port.label }}</span>
        <Handle
          :id="port.id"
          type="source"
          :position="Position.Right"
          :connectable="!stalePort(port)"
          :class="{ 'invalid-port': stalePort(port) || data.invalidOutputs?.includes(port.id) }"
          :aria-label="`${data.node.title} ${port.label}输出`"
        />
      </div>
    </div>
    <footer>
      <span>{{ kind.label }}</span>
      <span v-if="data.errorCount" class="preparation-node-error">{{ data.errorCount }} 项错误</span>
      <span v-else>{{ data.node.kind === "context" ? (data.node.config.source === "backend-read" ? "已读取" : "未读取") : "工作流配置" }}</span>
    </footer>
  </article>
</template>

<style scoped>
.preparation-node {
  position: relative;
  width: 220px;
  height: 212px;
  color: #d9dade;
  border: 1px solid #515156;
  border-top: 2px solid var(--node-accent);
  border-radius: 5px;
  background: #29292d;
  box-shadow: 0 4px 15px rgb(0 0 0 / 15%);
}
.preparation-node.selected {
  border-color: var(--node-accent);
  box-shadow: 0 0 0 1px var(--node-accent);
}
.preparation-node.invalid {
  border-color: #d47e7e;
}
.preparation-node header {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 35px;
  padding: 0 10px;
  border-bottom: 1px solid #404045;
}
.preparation-node header svg {
  flex: 0 0 auto;
  color: var(--node-accent);
}
.preparation-node header strong {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
}
.preparation-node-summary {
  display: -webkit-box;
  height: 28px;
  margin: 7px 10px 0;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow-wrap: anywhere;
  color: #a8abb3;
  font-size: 11px;
  line-height: 14px;
}
.preparation-node-ports {
  position: relative;
  height: 104px;
  margin-top: 6px;
}
.preparation-node-port {
  position: absolute;
  display: flex;
  align-items: center;
  height: 18px;
  font-size: 10px;
  color: #c1c3cb;
}
.preparation-node-port.input {
  left: 0;
  padding-left: 11px;
}
.preparation-node-port.output {
  right: 0;
  padding-right: 11px;
}
.preparation-node-port :deep(.vue-flow__handle) {
  width: 9px;
  height: 9px;
  min-width: 9px;
  min-height: 9px;
  background: var(--node-accent);
  border: 1.5px solid #29292d;
}
.preparation-node-port.input :deep(.vue-flow__handle) {
  left: -5px;
}
.preparation-node-port :deep(.vue-flow__handle.runtime-only) {
  background: #5c6c7b;
  cursor: default;
}
.preparation-node-port.output :deep(.vue-flow__handle) {
  right: -5px;
}
.preparation-node.invalid :deep(.vue-flow__handle) {
  box-shadow: 0 0 0 1px #d47e7e;
}
.preparation-node-port :deep(.vue-flow__handle.invalid-port) {
  background: #d47e7e;
  box-shadow: 0 0 0 1px #d47e7e;
}
.preparation-node footer {
  position: absolute;
  right: 0;
  bottom: 0;
  left: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 26px;
  padding: 0 10px;
  border-top: 1px solid #3d3d42;
  color: #858993;
  font-size: 9px;
}
.preparation-node .preparation-node-error {
  color: #e59b9b;
}
</style>
