<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { RefreshCw, Save } from "lucide-vue-next";
import type { GraphDocument, GraphNode } from "../domain/workflowGraph";
import { graphClone, graphObject } from "../domain/workflowGraph";
import { isModelSourceConfiguration, isCapacityModelSourceConfiguration, isModelCapacity,
  isModelParameters, modelFieldsExtensionId, capacityModelFieldsExtensionId, providerIdentity,
  sameProviderIdentity, type ModelParameters, type ModelSourceConfiguration,
  type ModelCapacity } from "../domain/workflowModelResources";
import { useWorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";
import { useProviderResources } from "../application/workflowResources";

const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk(), resources = useProviderResources();
const records = resources.records, loading = resources.loading, resourceError = resources.error;
const selected = ref(""), model = ref(""), maxTokens = ref(""), temperature = ref("");
const windowTokens = ref(""), outputReserve = ref(""), summaryMaxTokens = ref(""), coldInputLimit = ref("");
const capacityAware = computed(() => props.node.component_version === "2");
const status = ref(""), basis = ref<WorkflowNodeConfigurationRequest | null>(null);
const identityKey = (value: { scope: string; resource_id: string }) => JSON.stringify([value.scope, value.resource_id]);
const sourceConfig = computed<(ModelSourceConfiguration & { capacity?: ModelCapacity }) | null>(() => (capacityAware.value
  ? isCapacityModelSourceConfiguration(props.node.config) : isModelSourceConfiguration(props.node.config))
  ? props.node.config as unknown as ModelSourceConfiguration & { capacity?: ModelCapacity } : null);
const chosen = computed(() => records.value.find(record => identityKey(record) === selected.value));
const disabled = computed(() => !!props.disabled || sdk.locked.value || !basis.value);
const diagnosis = computed(() => {
  if (!sourceConfig.value) return "模型源配置无效";
  const record = records.value.find(row => sameProviderIdentity(providerIdentity(row), sourceConfig.value!.reference));
  if (!record) return "所选供应商资源缺失或尚未读取";
  if (!record.value.enabled) return "所选供应商已停用";
  if (!record.value.credential_ref) return "所选供应商未配置后端凭据引用";
  return "后端环境凭据在运行准备时核实";
});
watch(() => [sdk.lifecycle.value, props.node.node_binding_id, props.node.component_version, props.node.config], () => {
  status.value = "";
  const config = sourceConfig.value;
  const parameters = config?.parameters ?? (graphObject(props.node.config.parameters) ? props.node.config.parameters : {});
  const capacity = config?.capacity ?? (graphObject(props.node.config.capacity) ? props.node.config.capacity : {});
  const numericDraft = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? String(value) : "";
  selected.value = config ? identityKey(config.reference) : "";
  model.value = typeof parameters.model === "string" ? parameters.model : "";
  maxTokens.value = numericDraft(parameters.max_tokens);
  temperature.value = numericDraft(parameters.temperature);
  windowTokens.value = numericDraft(capacity.context_window_tokens);
  outputReserve.value = numericDraft(capacity.output_reserve_tokens);
  summaryMaxTokens.value = numericDraft(capacity.summary_max_tokens);
  coldInputLimit.value = numericDraft(capacity.max_cold_input_tokens);
  basis.value = { workflowId: sdk.workflowId.value, nodeId: props.node.node_binding_id,
    componentId: props.node.component_id, componentVersion: props.node.component_version,
    expectedConfig: graphClone(props.node.config), patch: {},
    extensionId: capacityAware.value ? capacityModelFieldsExtensionId : modelFieldsExtensionId,
    lifecycle: sdk.lifecycle.value };
}, { immediate: true, deep: true });
onMounted(() => { void resources.refresh(); });
function apply() {
  if (disabled.value || !basis.value || !chosen.value) { status.value = "请先选择已有供应商资源"; return; }
  const parameters: ModelParameters = { model: model.value.trim(), thinking: "disabled", stream: false };
  if (maxTokens.value !== "") parameters.max_tokens = Number(maxTokens.value);
  if (temperature.value !== "") parameters.temperature = Number(temperature.value);
  if (!isModelParameters(parameters)) { status.value = "模型名称或参数无效"; return; }
  const capacity = { context_window_tokens: Number(windowTokens.value), output_reserve_tokens: Number(outputReserve.value),
    summary_max_tokens: Number(summaryMaxTokens.value), max_cold_input_tokens: Number(coldInputLimit.value) };
  if (capacityAware.value && (!isModelCapacity(capacity) || parameters.max_tokens === undefined
    || capacity.output_reserve_tokens < parameters.max_tokens)) {
    status.value = "上下文窗口、输出预留、摘要输出或冷输入上限无效"; return;
  }
  const result = sdk.configureNode({ ...graphClone(basis.value),
    patch: { reference: providerIdentity(chosen.value), parameters, ...(capacityAware.value ? { capacity } : {}) } });
  status.value = result ? result.workflowId === basis.value.workflowId ? "模型配置已更新" : "模型配置已写入新副本"
    : "配置依据已变化或当前不可编辑，请重新选择节点";
}
</script>
<template>
  <form class="model-source-fields" aria-label="模型源配置" @submit.prevent="apply">
    <label>供应商当前资源
      <div class="resource-row">
        <select v-model="selected" :disabled="disabled">
          <option value="">选择供应商</option>
          <option v-if="selected && !chosen" :value="selected">缺失引用 · {{ sourceConfig?.reference.resource_id }}</option>
          <option v-for="record in records" :key="identityKey(record)" :value="identityKey(record)">
            {{ record.value.name }} · {{ record.scope }} · s{{ record.update_sequence }}{{ record.value.enabled ? '' : '（已停用）' }}
          </option>
        </select>
        <button type="button" title="刷新当前资源" aria-label="刷新当前资源" :disabled="loading" @click="resources.refresh()"><RefreshCw :size="14" /></button>
      </div>
    </label>
    <label>模型名称<input v-model="model" :disabled="disabled" required autocomplete="off" /></label>
    <label>最大输出 tokens<input v-model="maxTokens" :disabled="disabled" :required="capacityAware" type="number" min="1" max="8192" step="1" placeholder="未指定" /></label>
    <label>temperature<input v-model="temperature" :disabled="disabled" type="number" min="0" max="2" step="0.1" placeholder="未指定" /></label>
    <fieldset v-if="capacityAware" :disabled="disabled">
      <legend>上下文容量</legend>
      <label>上下文窗口 tokens<input v-model="windowTokens" aria-label="上下文窗口 tokens" type="number" min="1" step="1" required /></label>
      <label>输出预留 tokens<input v-model="outputReserve" aria-label="输出预留 tokens" type="number" min="1" step="1" required /></label>
      <label>摘要最大输出 tokens<input v-model="summaryMaxTokens" aria-label="摘要最大输出 tokens" type="number" min="1" step="1" required /></label>
      <label>摘要冷输入上限 tokens<input v-model="coldInputLimit" aria-label="摘要冷输入上限 tokens" type="number" min="1" step="1" required /></label>
    </fieldset>
    <p role="status">{{ diagnosis }}</p>
    <p v-if="resourceError" class="warning" role="status">{{ resourceError }}</p>
    <p v-if="status" role="status">{{ status }}</p>
    <button type="submit" :disabled="disabled || !chosen"><Save :size="14" />应用模型配置</button>
  </form>
</template>
<style scoped>
.model-source-fields { display:grid; gap:10px; min-width:0; font-size:12px; }
label { display:grid; gap:5px; min-width:0; } .resource-row { display:flex; gap:5px; min-width:0; }
fieldset { display:grid; gap:10px; margin:0; padding:10px 0 0; border:0; border-top:1px solid #505059; min-width:0; }
legend { padding:0; color:#b9bec6; }
input,select { width:100%; min-width:0; box-sizing:border-box; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; }
button { display:flex; align-items:center; justify-content:center; gap:6px; padding:6px; border:1px solid #64776d; border-radius:3px; }
p { margin:0; color:#b9bec6; overflow-wrap:anywhere; } .warning { color:#e4a0a0; } :disabled { opacity:.55; }
</style>
