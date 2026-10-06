<script setup lang="ts">
import { computed } from "vue";
import RolePlacementFields from "./RolePlacementFields.vue";
import SessionVariablesPanel from "./SessionVariablesPanel.vue";
import {
  defaultVariableValue, type PreparationNode, type RolePlacement, type VariableType,
} from "../domain/preparation";

const props = defineProps<{
  node: PreparationNode;
  workflowId: string;
  stage: "A" | "B";
  hasTextInput: boolean;
}>();
const emit = defineEmits<{ patch: [value: Record<string, unknown>] }>();
const config = computed(() => props.node.config as unknown as Record<string, unknown>);
const macroName = computed(() => `{{${config.value.name ?? ""}}}`);
const valueType = computed(() => config.value.valueType as VariableType);
const valueField = computed(() => props.node.kind === "variable-register" ? "initialValue" : "value");
const flags = [
  { value: "i", label: "忽略大小写" },
  { value: "m", label: "多行" },
  { value: "s", label: "点匹配换行" },
  { value: "x", label: "详细模式" },
  { value: "a", label: "ASCII" },
];
function changeType(event: Event) {
  const type = (event.target as HTMLSelectElement).value as VariableType;
  emit("patch", {
    valueType: type,
    [valueField.value]: props.node.kind === "variable-register" && !config.value.hasInitialValue
      ? null : defaultVariableValue(type),
    ...(props.node.kind === "variable-assign" && !["integer", "number"].includes(type)
      ? { operation: "set" } : {}),
  });
}
function toggleFlag(value: string, checked: boolean) {
  const current = config.value.flags as string[];
  emit("patch", { flags: checked ? [...current, value] : current.filter((flag) => flag !== value) });
}
function changeValue(event: Event) {
  const target = event.target as HTMLInputElement;
  if (["integer", "number"].includes(valueType.value) && target.value === "") return;
  emit("patch", { [valueField.value]: valueType.value === "string" ? target.value
    : valueType.value === "boolean" ? target.checked : Number(target.value) });
}
</script>

<template>
  <template v-if="node.kind === 'text'">
    <label class="increment-field"><span>文本</span><textarea :value="config.text as string" rows="7" @input="emit('patch', { text: ($event.target as HTMLTextAreaElement).value })"></textarea></label>
  </template>
  <template v-else-if="node.kind === 'text-to-prompt'">
    <RolePlacementFields :model-value="config.presentation as RolePlacement" @update:model-value="emit('patch', { presentation: $event })" />
  </template>
  <template v-else-if="node.kind === 'prompt-to-text'">
    <label class="increment-field"><span>条目分隔符</span><textarea :value="config.separator as string" rows="2" @input="emit('patch', { separator: ($event.target as HTMLTextAreaElement).value })"></textarea></label>
  </template>
  <template v-else-if="node.kind === 'regex' || node.kind === 'variable-replace'">
    <label class="increment-field"><span>数据类型</span><select :value="config.mode" @change="emit('patch', { mode: ($event.target as HTMLSelectElement).value })"><option value="text">文本</option><option value="prompt">提示词</option></select></label>
    <template v-if="node.kind === 'regex'">
      <label class="increment-field"><span>表达式 · python-re-v1</span><textarea :value="config.pattern as string" rows="4" spellcheck="false" @input="emit('patch', { pattern: ($event.target as HTMLTextAreaElement).value })"></textarea></label>
      <label class="increment-field"><span>替换文本</span><textarea :value="config.replacement as string" rows="4" spellcheck="false" @input="emit('patch', { replacement: ($event.target as HTMLTextAreaElement).value })"></textarea></label>
      <label class="increment-field"><span>替换范围</span><select :value="config.replaceMode" @change="emit('patch', { replaceMode: ($event.target as HTMLSelectElement).value })"><option value="all">全部</option><option value="first">首处</option></select></label>
      <fieldset class="increment-flags"><legend>Flags</legend><label v-for="flag in flags" :key="flag.value" class="increment-checkbox"><input type="checkbox" :checked="(config.flags as string[]).includes(flag.value)" @change="toggleFlag(flag.value, ($event.target as HTMLInputElement).checked)" /><code>{{ flag.value }}</code><span>{{ flag.label }}</span></label></fieldset>
    </template>
  </template>
  <template v-else-if="node.kind === 'variable-register' || node.kind === 'variable-assign'">
    <label class="increment-field"><span>变量名称</span><input :value="config.name as string" @input="emit('patch', { name: ($event.target as HTMLInputElement).value })" /><code v-if="config.name">{{ macroName }}</code></label>
    <label class="increment-field"><span>变量类型</span><select :value="config.valueType" @change="changeType"><option value="string">字符串</option><option value="integer">整数</option><option value="number">数值</option><option value="boolean">布尔</option></select></label>
    <label v-if="node.kind === 'variable-register'" class="increment-checkbox"><input type="checkbox" :checked="config.hasInitialValue as boolean" :disabled="hasTextInput" @change="emit('patch', { hasInitialValue: ($event.target as HTMLInputElement).checked, initialValue: ($event.target as HTMLInputElement).checked ? defaultVariableValue(valueType) : null })" /><span>配置初始值</span></label>
    <label v-else class="increment-field"><span>操作</span><select :value="config.operation" @change="emit('patch', { operation: ($event.target as HTMLSelectElement).value })"><option value="set">设置</option><option value="add" :disabled="!['integer', 'number'].includes(valueType)">加</option><option value="subtract" :disabled="!['integer', 'number'].includes(valueType)">减</option></select></label>
    <div v-if="hasTextInput" class="increment-value-source">值来源：文本连接</div>
    <template v-else-if="node.kind === 'variable-assign' || config.hasInitialValue">
      <label v-if="valueType === 'boolean'" class="increment-checkbox"><input type="checkbox" :checked="config[valueField] as boolean" @change="changeValue" /><span>{{ node.kind === 'variable-register' ? '初始值' : '操作值' }}：{{ config[valueField] ? "true" : "false" }}</span></label>
      <label v-else class="increment-field"><span>{{ node.kind === 'variable-register' ? '初始值' : '操作值' }}</span><textarea v-if="valueType === 'string'" :value="config[valueField] as string" rows="4" @input="changeValue"></textarea><input v-else type="number" :step="valueType === 'integer' ? 1 : 'any'" :value="config[valueField] as number" @input="changeValue" /></label>
    </template>
    <div v-else class="increment-value-source">初始值：未赋值</div>
    <SessionVariablesPanel :workflow-id="workflowId" :stage="stage" :name="config.name as string" />
  </template>
</template>

<style scoped>
.increment-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
  color: #a7aab2;
  font-size: 11px;
}
.increment-field input,
.increment-field textarea,
.increment-field select {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid #48484d;
  border-radius: 3px;
  color: #d5d7dc;
  background: #202024;
  font-size: 12px;
  line-height: 1.55;
}
.increment-field input,
.increment-field select { height: 30px; }
.increment-field code { overflow-wrap: anywhere; }
.increment-checkbox {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 22px;
  font-size: 11px;
}
.increment-checkbox input { width: 14px; height: 14px; margin: 0; accent-color: #8cc9b3; }
.increment-flags {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin: 0;
  padding: 8px 0 0;
  border: 0;
  border-top: 1px solid #434348;
}
.increment-flags legend { color: #a7aab2; font-size: 11px; }
.increment-flags code { width: 12px; color: #d7bb92; }
.increment-value-source { color: #aab9ae; font-size: 11px; }
</style>
