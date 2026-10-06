<script setup lang="ts">
import { computed } from "vue";
import { Handle, Position } from "@vue-flow/core";
import {
  Braces,
  Layers,
  ListOrdered,
  Combine,
  Regex,
  BookOpen,
  TextCursorInput,
  ArrowRightLeft,
  ChevronRight,
} from "lucide-vue-next";
import type { DraftNode } from "../domain/draft";
import { fixtureCatalog } from "../fixtures/catalog";
const props = defineProps<{
  id: string;
  data: { node: DraftNode; invalidPorts: string[] };
  selected?: boolean;
}>();
const icon = computed(
  () =>
    ({
      items: ListOrdered,
      collect: Combine,
      macro: Braces,
      regex: Regex,
      context: BookOpen,
      assemble: Layers,
      "to-text": ArrowRightLeft,
      group: Layers,
    })[props.data.node.kind] ?? TextCursorInput,
);
const descriptor = computed(() =>
  fixtureCatalog.find((n) => n.kind === props.data.node.kind),
);
</script>

<template>
  <article
    class="workflow-node"
    :class="{ 'is-selected': selected, 'has-error': data.invalidPorts.length }"
    :style="{ '--node-color': descriptor?.color ?? '#8c94a3' }"
  >
    <header class="node-title">
      <component :is="icon" :size="16" /><strong>{{ data.node.title }}</strong
      ><span class="node-version">{{
        data.node.kind === "group" ? "组" : "夹具"
      }}</span>
    </header>
    <div class="node-description">
      {{ descriptor?.description ?? "本地子图 · 稳定接口映射" }}
    </div>
    <div class="node-port-area">
      <div
        v-for="port in data.node.inputs"
        :key="port.id"
        class="port-row input-port"
        :class="{ invalid: data.invalidPorts.includes(port.id) }"
      >
        <Handle
          :id="port.id"
          type="target"
          :position="Position.Left"
          :aria-label="`${data.node.title} 输入 ${port.label}`"
        />
        <span>{{ port.label }}</span
        ><code>{{ port.type }}</code>
      </div>
      <div
        v-for="port in data.node.outputs"
        :key="port.id"
        class="port-row output-port"
      >
        <span>{{ port.label }}</span
        ><code>{{ port.type }}</code>
        <Handle
          :id="port.id"
          type="source"
          :position="Position.Right"
          :aria-label="`${data.node.title} 输出 ${port.label}`"
        />
      </div>
    </div>
    <footer class="node-footer">
      <span>{{
        data.node.kind === "group"
          ? "双击进入组"
          : Object.values(data.node.config)[0] || "本地配置"
      }}</span
      ><ChevronRight :size="13" />
    </footer>
  </article>
</template>
