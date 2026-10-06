<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { RefreshCw, Save } from "lucide-vue-next";
import { useWorkbenchVariablesStore } from "../stores/workbenchVariables";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";
import { validVariableValue, type VariableValue } from "../adapters/workbenchVariables";

const props = defineProps<{ workflowId: string; stage: "A" | "B"; name: string }>();
const variables = useWorkbenchVariablesStore();
const runtime = useWorkbenchRuntimeStore();
const value = ref("");
const booleanValue = ref(false);
const localError = ref<string | null>(null);
const row = computed(() => variables.read?.values.find((entry) => entry.name === props.name));
const unavailable = computed(() => !variables.scope || variables.loading || !!runtime.busy
  || !!runtime.unknown || runtime.refreshRequired);
watch(() => [props.workflowId, props.stage, props.name], () => {
  variables.activate(props.workflowId, props.stage);
  localError.value = null;
}, { immediate: true });
watch(() => variables.signature, () => {
  if (variables.scope && !runtime.unknown) void variables.refresh();
}, { immediate: true });
watch(row, () => {
  value.value = row.value?.assigned ? String(row.value.value) : "";
  booleanValue.value = row.value?.value === true;
  localError.value = null;
});
onUnmounted(() => variables.leave());
async function save() {
  if (!row.value || unavailable.value) return;
  const next: VariableValue = row.value.type === "boolean" ? booleanValue.value
    : row.value.type === "string" ? value.value : Number(value.value);
  if (row.value.type !== "string" && row.value.type !== "boolean" && !value.value.trim()
    || !validVariableValue(next, row.value.type)) {
    localError.value = "当前值与变量类型不匹配";
    return;
  }
  localError.value = null;
  await variables.assign(row.value, next);
}
</script>

<template>
  <section class="session-variable">
    <div class="variable-header">
      <span>会话当前值</span>
      <button type="button" class="icon-button" title="刷新会话变量" aria-label="刷新会话变量"
        :disabled="unavailable" @click="variables.refresh()"><RefreshCw :size="14" /></button>
    </div>
    <template v-if="row">
      <label v-if="row.type === 'boolean'" class="boolean-value">
        <input v-model="booleanValue" type="checkbox" :disabled="unavailable" />{{ name }}
      </label>
      <label v-else>{{ name }}
        <textarea v-if="row.type === 'string'" v-model="value" rows="3" :disabled="unavailable" />
        <input v-else v-model="value" type="number" :step="row.type === 'integer' ? 1 : 'any'" :disabled="unavailable" />
      </label>
      <div class="variable-actions">
        <span>{{ row.assigned ? row.source === 'default' ? '初始值' : '已赋值' : '未赋值' }}</span>
        <button type="button" class="icon-button" title="保存会话当前值" aria-label="保存会话当前值"
          :disabled="unavailable" @click="save()"><Save :size="14" /></button>
      </div>
    </template>
    <p v-else class="variable-status">{{ variables.loading ? '读取中' : variables.scope
      ? variables.read ? '无对应变量' : '未读取' : '未绑定会话' }}</p>
    <p v-if="localError || variables.error" class="variable-error" role="alert">{{ localError || variables.error }}</p>
  </section>
</template>

<style scoped>
.session-variable { border-top: 1px solid #3d4541; padding-top: 12px; margin-top: 12px; }
.variable-header, .variable-actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.variable-header { margin-bottom: 10px; font-size: 12px; color: #d7ddd8; }
label { display: grid; gap: 5px; font-size: 12px; color: #b6c0b9; overflow-wrap: anywhere; }
input, textarea { box-sizing: border-box; width: 100%; min-width: 0; border: 1px solid #4e5752; border-radius: 4px; padding: 7px; background: #232925; color: #e3e8e4; font: inherit; }
.boolean-value { display: flex; align-items: center; }
.boolean-value input { width: 16px; }
.variable-actions { margin-top: 6px; font-size: 11px; color: #a9b5ac; }
.icon-button { display: grid; place-items: center; width: 28px; height: 28px; flex-shrink: 0; border: 1px solid #49534d; border-radius: 4px; background: #303934; color: #e3e8e4; cursor: pointer; }
.icon-button:disabled { opacity: .45; cursor: default; }
.variable-status { font-size: 12px; color: #a9b5ac; }
.variable-error { font-size: 12px; color: #eea4a4; overflow-wrap: anywhere; }
</style>
