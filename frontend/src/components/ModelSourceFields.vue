<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { RefreshCw, Save } from "lucide-vue-next";
import type { GraphDocument, GraphNode } from "../domain/workflowGraph";
import { graphClone, graphJsonSignature } from "../domain/workflowGraph";
import { modelFieldsExtensionId, isModelSourceConfiguration,
  modelProfileIdentity, sameModelProfileIdentity, type ModelProfileIdentity } from "../domain/workflowModelResources";
import { useModelProfiles } from "../application/modelProfiles";
import { useWorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";

const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk(), profiles = useModelProfiles();
const selected = ref(""), status = ref(""), basis = ref<WorkflowNodeConfigurationRequest | null>(null);
const identityKey = (value: { scope: string; resource_id: string }) => JSON.stringify([value.scope, value.resource_id]);
const choices = computed(() => profiles.records.value);
const chosen = computed(() => choices.value.find(row => identityKey(row) === selected.value));
const diagnosis = computed(() => {
  if (!isModelSourceConfiguration(props.node.config)) return "模型配置引用无效，请选择已保存的全局配置";
  const reference: ModelProfileIdentity = props.node.config.reference;
  const saved = profiles.records.value.find(row => sameModelProfileIdentity(modelProfileIdentity(row), reference));
  if (!saved) return "全局模型配置缺失，请在模型供应商中创建配置";
  if (!saved.value.enabled) return "全局模型配置已停用，请在模型供应商中启用";
  return `${saved.value.name} · ${saved.value.model.id}`;
});
const disabled = computed(() => !!props.disabled || sdk.locked.value || !basis.value);
function current(request: WorkflowNodeConfigurationRequest) {
  return sdk.workflowId.value === request.workflowId && props.document.workflow_definition_id === request.workflowId
    && props.node.node_binding_id === request.nodeId && props.node.component_id === request.componentId
    && props.node.component_version === request.componentVersion && sdk.lifecycle.value === request.lifecycle;
}
watch(() => [sdk.lifecycle.value, props.node.node_binding_id, props.node.component_version, props.node.config], () => {
  selected.value = isModelSourceConfiguration(props.node.config) ? identityKey(props.node.config.reference) : "";
  status.value = "";
  basis.value = { workflowId: sdk.workflowId.value, nodeId: props.node.node_binding_id,
    componentId: props.node.component_id, componentVersion: props.node.component_version,
    expectedConfig: graphClone(props.node.config), patch: {},
    extensionId: modelFieldsExtensionId,
    lifecycle: sdk.lifecycle.value };
}, { immediate: true, deep: true });
async function apply() {
  if (disabled.value || !basis.value || !chosen.value) { status.value = "请选择全局模型配置"; return; }
  const request = graphClone(basis.value), choice = graphClone(chosen.value), selector = selected.value;
  if (disabled.value || selected.value !== selector || graphJsonSignature(basis.value) !== graphJsonSignature(request)
    || graphJsonSignature(chosen.value) !== graphJsonSignature(choice)) {
    status.value = "选择依据已变化，请重新确认"; return;
  }
  const patch = { reference: modelProfileIdentity(choice) };
  const submission = { ...request, patch };
  let result;
  try { result = await sdk.configureNode(submission); }
  catch (failure) {
    if (current(submission)) status.value = failure instanceof Error ? failure.message : "模型引用提交失败";
    return;
  }
  await nextTick();
  if (current(submission)) status.value = result
    ? result.outcome === "cancelled" ? "已取消；工作流配置未修改"
      : result.workflowId === request.workflowId ? "模型引用已保存到当前工作流" : "模型引用已保存到新工作流"
    : "当前不可编辑，请重新选择节点";
}
onMounted(() => { void profiles.refresh(); });
</script>
<template>
  <form class="model-source-fields" aria-label="模型配置引用" @submit.prevent="apply">
    <label>全局模型配置
      <div class="resource-row"><select v-model="selected" :disabled="disabled" aria-label="全局模型配置">
        <option value="">选择模型配置</option>
        <option v-if="selected && !chosen" :value="selected">配置缺失</option>
        <option v-for="record in choices" :key="identityKey(record)" :value="identityKey(record)">
          {{ record.value.name }} · {{ record.value.model.id }}{{ record.value.enabled ? '' : '（已停用）' }}
        </option>
      </select><button type="button" title="刷新模型配置" aria-label="刷新模型配置" :disabled="profiles.loading.value" @click="profiles.refresh"><RefreshCw :size="14" /></button></div>
    </label>
    <p role="status">{{ diagnosis }}</p>
    <p v-if="profiles.error.value || status" class="warning" role="status">{{ status || profiles.error.value }}</p>
    <button type="submit" :disabled="disabled || !chosen"><Save :size="14" />保存模型引用</button>
  </form>
</template>
<style scoped>
.model-source-fields { display:grid; gap:10px; min-width:0; font-size:12px; }
label { display:grid; gap:5px; min-width:0; } .resource-row { display:flex; gap:5px; min-width:0; }
.resource-row button { flex:0 0 28px; height:28px; }
select { min-width:0; width:100%; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; }
button { display:flex; gap:6px; align-items:center; justify-content:center; padding:6px; border-radius:3px; }
p { margin:0; overflow-wrap:anywhere; color:#b9bec6; } .warning { color:#e4a0a0; } :disabled { opacity:.55; }
</style>
