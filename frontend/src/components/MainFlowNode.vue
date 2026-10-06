<script setup lang="ts">
import { computed } from "vue";
import { Handle, Position } from "@vue-flow/core";
import { Bot, FileOutput, Info, RadioTower, Settings2 } from "lucide-vue-next";
import type { MainFlowNodeData } from "../domain/workspace";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";

const props = defineProps<{
  id: string;
  data: MainFlowNodeData;
  selected?: boolean;
  flat?: boolean;
}>();
const emit = defineEmits<{
  "open-preparation": [stage: "A" | "B"];
  "open-exposure": [stage: "A" | "B"];
  "open-observation": [stage: "A" | "B" | "Output"];
}>();
const runtime = useWorkbenchRuntimeStore();
const accent = computed(
  () =>
    ({ A: "#79c9b6", B: "#89b8ed", Output: "#dfba73" })[props.data.stage],
);
</script>

<template>
  <article
    class="main-flow-node"
    :class="{ 'main-flow-node-selected': selected, 'main-flow-node-running': runtime.nodeStatus(data.stage) === 'running' }"
    :style="{ '--main-flow-accent': accent }"
    :aria-label="data.title"
  >
    <header class="main-flow-node-header">
      <component :is="data.stage === 'Output' ? FileOutput : Bot" :size="17" />
      <strong>{{ data.title }}</strong>
      <span class="main-flow-node-kind" :title="runtime.nodeStatus(data.stage)">{{
        runtime.nodeStatus(data.stage)
      }}</span>
    </header>
    <div class="main-flow-node-ports">
      <div class="main-flow-node-port main-flow-node-input">
        <div class="main-flow-business-input">
          <Handle
            id="in"
            type="target"
            :position="Position.Left"
            :connectable="false"
            :aria-label="`${data.title} 输入`"
          />
          <span>输入</span>
        </div>
        <div v-if="data.stage !== 'Output'" class="main-flow-model-input">
          <Handle id="model-in" type="target" :position="Position.Left" :connectable="true" :aria-label="`${data.title} 模型输入`" />
          <span>模型</span>
        </div>
        <div v-if="data.stage !== 'Output'" class="main-flow-model-input">
          <Handle id="prompt-in" type="target" :position="Position.Left" :connectable="true" :aria-label="`${data.title} 提示词输入`" />
          <span>提示词</span>
        </div>
      </div>
      <div
        v-if="data.stage !== 'Output'"
        class="main-flow-node-port main-flow-node-output"
      >
        <div class="main-flow-node-output-row">
          <span>业务输出</span>
          <Handle
            id="out"
            type="source"
            :position="Position.Right"
            :connectable="false"
            :aria-label="`${data.title} 业务输出`"
          />
        </div>
        <div class="main-flow-node-output-row main-flow-node-context-output">
          <span>上下文</span>
          <Handle
            id="agent-context"
            type="source"
            :position="Position.Right"
            :connectable="false"
            :aria-label="`${data.title} 上下文输出`"
            title="跨轮归档上下文"
          />
        </div>
      </div>
    </div>
    <footer class="main-flow-node-footer nodrag nowheel">
      <button type="button" :title="`${data.title} 运行详情`" :aria-label="`${data.title} 运行详情`"
        @click.stop="emit('open-observation', data.stage)" @dblclick.stop><Info :size="13" /></button>
      <template v-if="data.stage !== 'Output'">
        <button
          type="button"
          :title="`${data.title} 上下文装配`"
          :aria-label="`${data.title} 上下文装配`"
          @click.stop="emit('open-preparation', data.stage)"
          @dblclick.stop
        >
          <Settings2 :size="13" />
          <span>{{ flat ? "装配预览" : "上下文装配" }}</span>
        </button>
        <button
          type="button"
          :title="`${data.title} 信息注册`"
          :aria-label="`${data.title} 信息注册`"
          @click.stop="emit('open-exposure', data.stage)"
          @dblclick.stop
        >
          <RadioTower :size="13" />
          <span>注册</span>
        </button>
      </template>
      <template v-else>
        <span class="main-flow-node-dot"></span>
        <span>业务输出</span>
      </template>
    </footer>
  </article>
</template>

<style scoped>
.main-flow-node {
  width: 212px;
  height: 151px;
  color: #dce1e7;
  background: #262a30;
  border: 1px solid #4a515a;
  border-top: 2px solid var(--main-flow-accent);
  border-radius: 6px;
  box-shadow: 0 5px 18px rgb(0 0 0 / 16%);
  transition: border-color 120ms;
}
.main-flow-node-selected {
  border-color: var(--main-flow-accent);
  box-shadow: 0 0 0 1px var(--main-flow-accent), 0 5px 18px rgb(0 0 0 / 16%);
}
.main-flow-node-running,
.main-flow-node-running.main-flow-node-selected {
  border-color: #55d98a;
  box-shadow: 0 0 0 2px #55d98a, 0 5px 18px rgb(0 0 0 / 16%);
}
.main-flow-node-header {
  display: flex;
  align-items: center;
  gap: 9px;
  height: 42px;
  padding: 0 14px;
  border-bottom: 1px solid #3a414a;
}
.main-flow-node-header svg {
  flex: 0 0 auto;
  color: var(--main-flow-accent);
}
.main-flow-node-header strong {
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
}
.main-flow-node-kind {
  margin-left: auto;
  max-width: 72px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #909ba6;
  font-size: 10px;
}
.main-flow-node-ports {
  display: grid;
  grid-template-columns: 1fr 1fr;
  height: 70px;
  align-items: center;
}
.main-flow-node-port {
  position: relative;
  color: #aeb8c3;
  font-size: 12px;
}
.main-flow-node-input {
  padding-left: 15px;
}
.main-flow-model-input {
  position: relative;
  margin-top: 12px;
  color: #d0a8c7;
  font-size: 10px;
}
.main-flow-model-input :deep(.vue-flow__handle) {
  background: #c39dbe;
  cursor: crosshair;
}
.main-flow-node-output {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-right: 15px;
  text-align: right;
}
.main-flow-node-output-row {
  position: relative;
  font-size: 10px;
}
.main-flow-node-context-output {
  color: #91a4b9;
}
.main-flow-node-port :deep(.vue-flow__handle) {
  width: 8px;
  height: 8px;
  min-width: 8px;
  min-height: 8px;
  border: 1.5px solid #252a30;
  background: var(--main-flow-accent);
  cursor: default;
}
.main-flow-business-input {
  position: relative;
}
.main-flow-node-input :deep(.vue-flow__handle) {
  left: -20px;
}
.main-flow-node-output :deep(.vue-flow__handle) {
  right: -5px;
}
.main-flow-node-output-row :deep(.vue-flow__handle) {
  right: -20px;
}
.main-flow-node-footer {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 36px;
  padding: 0 8px;
  border-top: 1px solid #353c45;
  color: #86939f;
  font-size: 11px;
}
.main-flow-node-footer button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  height: 26px;
  padding: 0 6px;
  border-radius: 3px;
  font-size: 10px;
  white-space: nowrap;
}
.main-flow-node-footer button:first-child {
  color: #b6d8cb;
}
.main-flow-node-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #87939e;
}
</style>
