<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { RefreshCw, Save } from "lucide-vue-next";
import type { GraphDocument, GraphNode } from "../domain/workflowGraph";
import { graphClone, graphJsonSignature, graphObject } from "../domain/workflowGraph";
import { capacityModelFieldsExtensionId, capacityGeminiModelFieldsExtensionId, isModelSourceConfiguration,
  modelProfileIdentity, sameModelProfileIdentity, type ModelProfileIdentity } from "../domain/workflowModelResources";
import { useModelProfiles } from "../application/modelProfiles";
import { useWorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";
import { useBoundFormDraft } from "../composables/useBoundFormDraft";
import FormDraftNotice from "./FormDraftNotice.vue";

const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk(), profiles = useModelProfiles();
const selected = ref(""), status = ref(""), basis = ref<WorkflowNodeConfigurationRequest | null>(null);
const protocol = computed(() => props.node.component_version === "6" ? "gemini" : "chat");
const identityKey = (value: { scope: string; resource_id: string }) => JSON.stringify([value.scope, value.resource_id]);
const choices = computed(() => profiles.records.value.filter(row => row.value.protocol === protocol.value));
const chosen = computed(() => choices.value.find(row => identityKey(row) === selected.value));
const diagnosis = computed(() => {
  if (!isModelSourceConfiguration(props.node.config)) return "模型配置引用无效，请选择已保存的全局配置";
  const reference: ModelProfileIdentity = props.node.config.reference;
  const saved = profiles.records.value.find(row => sameModelProfileIdentity(modelProfileIdentity(row), reference));
  if (!saved) return "全局模型配置缺失，请在模型供应商中创建配置";
  if (saved.value.protocol !== protocol.value) return "配置协议不匹配，请选择对应协议的模型来源";
  if (!saved.value.enabled) return "全局模型配置已停用，请在模型供应商中启用";
  return `${saved.value.name} · ${saved.value.parameters.model}`;
});
const localDraft = useBoundFormDraft({
  id: () => `node:${props.document.workflow_definition_id}:${props.node.node_binding_id}:model-reference`,
  basis: () => ({ workflowId: props.document.workflow_definition_id, nodeId: props.node.node_binding_id,
    component: props.node.component_id, version: props.node.component_version, config: props.node.config }),
  capture: () => ({ selected: selected.value }),
  restore: value => {
    if (!graphObject(value) || Object.keys(value).length !== 1 || typeof value.selected !== "string")
      throw new Error("模型引用草稿格式无效，原记录仍保留");
    selected.value = value.selected;
  },
});
const disabled = computed(() => !!props.disabled || sdk.locked.value || !basis.value || localDraft.loading.value || localDraft.conflict.value);
watch(() => [sdk.lifecycle.value, props.node.node_binding_id, props.node.component_version, props.node.config], () => {
  selected.value = isModelSourceConfiguration(props.node.config) ? identityKey(props.node.config.reference) : "";
  status.value = "";
  basis.value = { workflowId: sdk.workflowId.value, nodeId: props.node.node_binding_id,
    componentId: props.node.component_id, componentVersion: props.node.component_version,
    expectedConfig: graphClone(props.node.config), patch: {},
    extensionId: protocol.value === "gemini" ? capacityGeminiModelFieldsExtensionId : capacityModelFieldsExtensionId,
    lifecycle: sdk.lifecycle.value };
}, { immediate: true, deep: true });
async function apply() {
  if (disabled.value || !basis.value || !chosen.value) { status.value = "请选择全局模型配置"; return; }
  const request = graphClone(basis.value), choice = graphClone(chosen.value), selector = selected.value;
  if (!await localDraft.flush()) { status.value = "模型引用草稿尚未写入本机"; return; }
  if (disabled.value || selected.value !== selector || graphJsonSignature(basis.value) !== graphJsonSignature(request)
    || graphJsonSignature(chosen.value) !== graphJsonSignature(choice)) {
    status.value = "选择依据已变化，请重新确认"; return;
  }
  const patch = { reference: modelProfileIdentity(choice) };
  const result = sdk.configureNode({ ...request, patch });
  const adopted = !result || result.workflowId !== request.workflowId || result.nodeId !== request.nodeId
    || localDraft.adoptBasis({ workflowId: props.document.workflow_definition_id, nodeId: request.nodeId,
      component: request.componentId, version: request.componentVersion, config: patch });
  await nextTick();
  if (props.node.node_binding_id === request.nodeId) status.value = result
    ? adopted ? "模型引用已更新" : "模型引用已更新，草稿依据尚未同步"
    : "当前不可编辑，请重新选择节点";
}
onMounted(() => { void profiles.refresh(); });
</script>
<template>
  <form class="model-source-fields" aria-label="模型配置引用" @submit.prevent="apply">
    <FormDraftNotice :message="localDraft.message.value" :conflict="localDraft.conflict.value"
      @restore="localDraft.restoreConflict" @discard="localDraft.discardConflict" />
    <label>全局模型配置
      <div class="resource-row"><select v-model="selected" :disabled="disabled" aria-label="全局模型配置">
        <option value="">选择模型配置</option>
        <option v-if="selected && !chosen" :value="selected">配置缺失或协议不匹配</option>
        <option v-for="record in choices" :key="identityKey(record)" :value="identityKey(record)">
          {{ record.value.name }} · {{ record.value.parameters.model }}{{ record.value.enabled ? '' : '（已停用）' }}
        </option>
      </select><button type="button" title="刷新模型配置" aria-label="刷新模型配置" :disabled="profiles.loading.value" @click="profiles.refresh"><RefreshCw :size="14" /></button></div>
    </label>
    <p role="status">{{ diagnosis }}</p>
    <p v-if="profiles.error.value || status" class="warning" role="status">{{ status || profiles.error.value }}</p>
    <button type="submit" :disabled="disabled || !chosen"><Save :size="14" />应用模型引用</button>
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
