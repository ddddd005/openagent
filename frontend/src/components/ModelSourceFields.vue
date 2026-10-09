<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { RefreshCw, Save } from "lucide-vue-next";
import type { GraphDocument, GraphNode } from "../domain/workflowGraph";
import { graphClone, graphObject } from "../domain/workflowGraph";
import { isModelSourceConfiguration, isCapacityModelSourceConfiguration, isModelCapacity,
  isModelParameters, modelFieldsExtensionId, capacityModelFieldsExtensionId, providerIdentity,
  geminiModelFieldsExtensionId, capacityGeminiModelFieldsExtensionId, geminiThinkingCapability, modelParameterDiagnostic,
  sameProviderIdentity, type ModelParameters, type ModelSourceConfiguration,
  type ModelCapacity, type ModelThinking, type ThinkingLevel } from "../domain/workflowModelResources";
import { useWorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";
import { useProviderResources } from "../application/workflowResources";
import { useBoundFormDraft } from "../composables/useBoundFormDraft";
import FormDraftNotice from "./FormDraftNotice.vue";
import RawNumberInput from "./RawNumberInput.vue";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { listProviderModels, type DiscoveredModel } from "../adapters/providerConfigurationApi";

const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk(), resources = useProviderResources();
const records = resources.records, loading = resources.loading, resourceError = resources.error;
const selected = ref(""), model = ref(""), maxTokens = ref(""), temperature = ref("");
const windowTokens = ref(""), outputReserve = ref(""), summaryMaxTokens = ref(""), coldInputLimit = ref("");
const capacityAware = computed(() => ["2", "4"].includes(props.node.component_version));
const geminiRoute = computed(() => ["3", "4"].includes(props.node.component_version));
const thinkingMode = ref<"disabled" | "budget" | "level">("disabled"), thinkingBudget = ref("-1");
const thinkingLevel = ref<ThinkingLevel>("low"), includeSummary = ref(true);
const thinkingCapability = computed(() => geminiThinkingCapability(model.value.trim()));
const extensionId = computed(() => geminiRoute.value
  ? capacityAware.value ? capacityGeminiModelFieldsExtensionId : geminiModelFieldsExtensionId
  : capacityAware.value ? capacityModelFieldsExtensionId : modelFieldsExtensionId);
const status = ref(""), basis = ref<WorkflowNodeConfigurationRequest | null>(null);
const persistence = typeof window !== "undefined" ? useWorkbenchPersistenceStore() : null;
const numericValidity = ref<Record<string, boolean>>({});
const numericValid = computed(() => ["maxTokens", "temperature",
  ...(capacityAware.value ? ["windowTokens", "outputReserve", "summaryMaxTokens", "coldInputLimit"] : []),
  ...(geminiRoute.value && thinkingMode.value === "budget" ? ["thinkingBudget"] : []),
].every(key => numericValidity.value[key] !== false));
const identityKey = (value: { scope: string; resource_id: string }) => JSON.stringify([value.scope, value.resource_id]);
const sourceConfig = computed<(ModelSourceConfiguration & { capacity?: ModelCapacity }) | null>(() => (capacityAware.value
  ? isCapacityModelSourceConfiguration(props.node.config) : isModelSourceConfiguration(props.node.config))
  ? props.node.config as unknown as ModelSourceConfiguration & { capacity?: ModelCapacity } : null);
const chosen = computed(() => records.value.find(record => identityKey(record) === selected.value));
const compatibleRecords = computed(() => records.value.filter(record => record.value.protocol === (geminiRoute.value ? "gemini" : "chat")));
const discovered = ref<DiscoveredModel[]>([]), discovering = ref(false), discoveryError = ref("");
let discoveryGeneration = 0;
watch(() => [selected.value, JSON.stringify(chosen.value), props.document.workflow_definition_id,
  props.node.node_binding_id, props.node.component_version], () => {
  discoveryGeneration++; discovered.value = []; discoveryError.value = ""; discovering.value = false;
});
async function discover() {
  const provider = chosen.value;
  if (!provider?.value.enabled || !provider.value.credential_ref || discovering.value || disabled.value
    || provider.value.protocol !== (geminiRoute.value ? "gemini" : "chat")) return;
  const token = ++discoveryGeneration, fixed = JSON.stringify(provider);
  discovering.value = true; discoveryError.value = "";
  try {
    const rows = await listProviderModels(providerIdentity(provider), provider.update_sequence, provider.value.protocol);
    if (token === discoveryGeneration && JSON.stringify(chosen.value) === fixed) discovered.value = rows;
  } catch (failure) {
    if (token === discoveryGeneration) discoveryError.value = failure instanceof Error ? failure.message : "上游未提供可用模型列表";
  } finally { if (token === discoveryGeneration) discovering.value = false; }
}
function chooseDiscovered(event: Event) {
  const row = discovered.value.find(item => item.model === (event.target as HTMLSelectElement).value);
  if (!row?.adapted) return;
  model.value = row.model;
  const capability = geminiRoute.value ? geminiThinkingCapability(row.model) : null;
  thinkingMode.value = capability?.mode ?? "disabled";
  thinkingBudget.value = "-1"; thinkingLevel.value = capability?.levels?.[0] ?? "low";
  if (row.output_token_limit) {
    const maximum = Math.min(row.output_token_limit, 8192);
    maxTokens.value = String(maximum);
    if (capacityAware.value) outputReserve.value = String(Math.max(maximum, Number(outputReserve.value) || 0));
  }
  if (row.context_window_tokens) windowTokens.value = String(row.context_window_tokens);
  if (row.input_token_limit) coldInputLimit.value = String(row.input_token_limit);
}
const disabled = computed(() => !!props.disabled || sdk.locked.value || !basis.value || localDraft.loading.value || localDraft.conflict.value);
const diagnosis = computed(() => {
  if (!sourceConfig.value) return "模型源配置无效";
  const record = records.value.find(row => sameProviderIdentity(providerIdentity(row), sourceConfig.value!.reference));
  if (!record) return "所选供应商资源缺失或尚未读取";
  if (!record.value.enabled) return "所选供应商已停用";
  if (!record.value.credential_ref) return "所选供应商未配置后端凭据引用";
  if (record.value.protocol !== (geminiRoute.value ? "gemini" : "chat")) return "供应商协议与模型源节点版本不匹配";
  const parameterError = modelParameterDiagnostic(sourceConfig.value.parameters, record.value.protocol);
  if (parameterError) return parameterError;
  return "后端凭据在运行准备时核实";
});
function capture() {
  return { selected: selected.value, model: model.value, maxTokens: maxTokens.value, temperature: temperature.value,
    windowTokens: windowTokens.value, outputReserve: outputReserve.value, summaryMaxTokens: summaryMaxTokens.value,
    coldInputLimit: coldInputLimit.value, thinkingMode: thinkingMode.value, thinkingBudget: thinkingBudget.value,
    thinkingLevel: thinkingLevel.value, includeSummary: includeSummary.value };
}
const localDraft = useBoundFormDraft({
  id: () => `node:${props.document.workflow_definition_id}:${props.node.node_binding_id}:model`,
  basis: () => ({ workflowId: props.document.workflow_definition_id, nodeId: props.node.node_binding_id,
    component: props.node.component_id, version: props.node.component_version, config: props.node.config }),
  capture,
  restore: value => {
    if (!graphObject(value) || !["selected", "model", "maxTokens", "temperature", "windowTokens", "outputReserve",
      "summaryMaxTokens", "coldInputLimit", "thinkingMode", "thinkingBudget", "thinkingLevel"].every(key => typeof (value as Record<string, unknown>)[key] === "string"))
      throw new Error("模型表单草稿格式不兼容，原文仍保留");
    selected.value = value.selected; model.value = value.model; maxTokens.value = value.maxTokens;
    temperature.value = value.temperature; windowTokens.value = value.windowTokens;
    outputReserve.value = value.outputReserve; summaryMaxTokens.value = value.summaryMaxTokens;
    coldInputLimit.value = value.coldInputLimit; thinkingMode.value = value.thinkingMode;
    thinkingBudget.value = value.thinkingBudget; thinkingLevel.value = value.thinkingLevel;
    includeSummary.value = value.includeSummary;
  },
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
  const thinking = parameters.thinking;
  thinkingMode.value = graphObject(thinking) && ["budget", "level"].includes(String(thinking.mode))
    ? thinking.mode as "budget" | "level" : "disabled";
  thinkingBudget.value = graphObject(thinking) ? numericDraft(thinking.budget) || "-1" : "-1";
  thinkingLevel.value = graphObject(thinking) && ["minimal", "low", "medium", "high"].includes(String(thinking.level))
    ? thinking.level as ThinkingLevel : "low";
  includeSummary.value = graphObject(thinking) ? thinking.include_summary === true : true;
  windowTokens.value = numericDraft(capacity.context_window_tokens);
  outputReserve.value = numericDraft(capacity.output_reserve_tokens);
  summaryMaxTokens.value = numericDraft(capacity.summary_max_tokens);
  coldInputLimit.value = numericDraft(capacity.max_cold_input_tokens);
  basis.value = { workflowId: sdk.workflowId.value, nodeId: props.node.node_binding_id,
    componentId: props.node.component_id, componentVersion: props.node.component_version,
    expectedConfig: graphClone(props.node.config), patch: {},
    extensionId: extensionId.value,
    lifecycle: sdk.lifecycle.value };
}, { immediate: true, deep: true });
onMounted(() => { void resources.refresh(); });
async function apply() {
  if (disabled.value || !basis.value || !chosen.value) { status.value = "请先选择已有供应商资源"; return; }
  if (!numericValid.value) { status.value = "模型数字参数尚未填写完整或超出范围"; return; }
  const requestBasis = graphClone(basis.value), captured = capture(), provider = graphClone(chosen.value);
  const context = localDraft.context.value, capacityRoute = capacityAware.value, gemini = geminiRoute.value;
  if (!await (persistence ? persistence.flushAll() : localDraft.flush())) { status.value = "模型表单尚未写入本机"; return; }
  if (disabled.value || !numericValid.value || context !== localDraft.context.value
    || JSON.stringify(basis.value) !== JSON.stringify(requestBasis)
    || JSON.stringify(capture()) !== JSON.stringify(captured)
    || JSON.stringify(chosen.value) !== JSON.stringify(provider)) {
    status.value = "配置依据或输入已变化，尚未应用，请重新确认"; return;
  }
  const thinking: ModelThinking = captured.thinkingMode === "budget"
    ? { mode: "budget", budget: Number(captured.thinkingBudget), include_summary: captured.includeSummary }
    : captured.thinkingMode === "level"
      ? { mode: "level", level: captured.thinkingLevel, include_summary: captured.includeSummary } : "disabled";
  const parameters: ModelParameters = { model: captured.model.trim(), thinking, stream: false };
  if (captured.maxTokens !== "") parameters.max_tokens = Number(captured.maxTokens);
  if (captured.temperature !== "") parameters.temperature = Number(captured.temperature);
  if (!isModelParameters(parameters)) { status.value = "模型名称或参数无效"; return; }
  if (provider.value.protocol !== (gemini ? "gemini" : "chat")) {
    status.value = "供应商协议与模型源节点版本不匹配"; return;
  }
  const parameterError = modelParameterDiagnostic(parameters, provider.value.protocol);
  if (parameterError) { status.value = parameterError; return; }
  const capacity = { context_window_tokens: Number(captured.windowTokens), output_reserve_tokens: Number(captured.outputReserve),
    summary_max_tokens: Number(captured.summaryMaxTokens), max_cold_input_tokens: Number(captured.coldInputLimit) };
  if (capacityRoute && (!isModelCapacity(capacity) || parameters.max_tokens === undefined
    || capacity.output_reserve_tokens < parameters.max_tokens)) {
    status.value = "上下文窗口、输出预留、摘要输出或冷输入上限无效"; return;
  }
  const workflowId = requestBasis.workflowId, nodeId = requestBasis.nodeId;
  const patch = { reference: providerIdentity(provider), parameters, ...(capacityRoute ? { capacity } : {}) };
  const nextDraftBasis = { workflowId: props.document.workflow_definition_id, nodeId,
    component: requestBasis.componentId, version: requestBasis.componentVersion,
    config: { ...graphClone(requestBasis.expectedConfig), ...graphClone(patch) } };
  const result = sdk.configureNode({ ...requestBasis, patch });
  const adopted = !result || result.workflowId !== workflowId || result.nodeId !== nodeId
    || localDraft.adoptBasis(nextDraftBasis);
  const feedback = result ? result.workflowId === workflowId ? adopted ? "模型配置已更新"
    : "模型配置已更新，表单草稿依据尚未同步；原草稿保留" : "模型配置已写入新副本"
    : "配置依据已变化或当前不可编辑，请重新选择节点";
  await nextTick();
  if (props.node.node_binding_id === nodeId) status.value = feedback;
}
</script>
<template>
  <form class="model-source-fields" aria-label="模型源配置" @submit.prevent="apply">
    <FormDraftNotice :message="localDraft.message.value" :conflict="localDraft.conflict.value"
      @restore="localDraft.restoreConflict" @discard="localDraft.discardConflict" />
    <label>供应商当前资源
      <div class="resource-row">
        <select v-model="selected" :disabled="disabled">
          <option value="">选择供应商</option>
          <option v-if="selected && !compatibleRecords.some(row => identityKey(row) === selected)" :value="selected">
            {{ chosen ? '协议不匹配' : '缺失引用' }} · {{ sourceConfig?.reference.resource_id }}
          </option>
          <option v-for="record in compatibleRecords" :key="identityKey(record)" :value="identityKey(record)">
            {{ record.value.name }}{{ record.value.enabled ? '' : '（已停用）' }}
          </option>
        </select>
        <button type="button" title="刷新当前资源" aria-label="刷新当前资源" :disabled="loading" @click="resources.refresh()"><RefreshCw :size="14" /></button>
      </div>
    </label>
    <label>模型名称<input v-model="model" :disabled="disabled" required autocomplete="off" /></label>
    <div class="resource-row">
      <select v-if="discovered.length" value="" aria-label="上游模型列表" :disabled="disabled" @change="chooseDiscovered">
        <option value="">选择上游型号</option>
        <option v-for="row in discovered" :key="row.model" :value="row.model" :disabled="!row.adapted">{{ row.model }}{{ row.adapted ? '' : '（尚未适配）' }}</option>
      </select>
      <button type="button" title="获取上游模型列表" aria-label="获取上游模型列表" :disabled="disabled || !chosen || discovering" @click="discover"><RefreshCw :size="14" /></button>
    </div>
    <p v-if="discoveryError" class="warning" role="status">{{ discoveryError }}</p>
    <p v-if="geminiRoute && discovered.length" role="status">列表的输入上限与输出上限独立；总上下文窗口仍需明确填写。</p>
    <label>最大输出 tokens<RawNumberInput v-model:raw-value="maxTokens" :disabled="disabled" :required="capacityAware"
      :min="1" :max="8192" integer aria-label="最大输出 tokens" placeholder="未指定" @validity="numericValidity.maxTokens = $event" /></label>
    <label>temperature<RawNumberInput v-model:raw-value="temperature" :disabled="disabled" :required="false"
      :min="0" :max="2" aria-label="temperature" placeholder="未指定" @validity="numericValidity.temperature = $event" /></label>
    <fieldset v-if="geminiRoute" :disabled="disabled">
      <legend>思考</legend>
      <label>思考模式<select v-model="thinkingMode" aria-label="思考模式">
        <option value="disabled">关闭</option><option value="budget">Token 预算</option><option value="level">强度</option>
      </select></label>
      <label v-if="thinkingMode === 'budget'">思考预算 tokens<RawNumberInput v-model:raw-value="thinkingBudget" :disabled="disabled"
        aria-label="思考预算 tokens" :min="-1" :max="thinkingCapability?.budgetMax ?? 32768" integer
        @validity="numericValidity.thinkingBudget = $event" /></label>
      <label v-if="thinkingMode === 'level'">思考强度<select v-model="thinkingLevel" aria-label="思考强度">
        <option value="minimal">最少</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option>
      </select></label>
      <label v-if="thinkingMode !== 'disabled'" class="checkbox-field"><input v-model="includeSummary"
        type="checkbox" aria-label="请求思考摘要" />请求思考摘要</label>
    </fieldset>
    <fieldset v-if="capacityAware" :disabled="disabled">
      <legend>上下文容量</legend>
      <label>上下文窗口 tokens<RawNumberInput v-model:raw-value="windowTokens" :disabled="disabled"
        aria-label="上下文窗口 tokens" :min="1" integer @validity="numericValidity.windowTokens = $event" /></label>
      <label>输出预留 tokens<RawNumberInput v-model:raw-value="outputReserve" :disabled="disabled"
        aria-label="输出预留 tokens" :min="1" integer @validity="numericValidity.outputReserve = $event" /></label>
      <label>摘要最大输出 tokens<RawNumberInput v-model:raw-value="summaryMaxTokens" :disabled="disabled"
        aria-label="摘要最大输出 tokens" :min="1" integer @validity="numericValidity.summaryMaxTokens = $event" /></label>
      <label>摘要冷输入上限 tokens<RawNumberInput v-model:raw-value="coldInputLimit" :disabled="disabled"
        aria-label="摘要冷输入上限 tokens" :min="1" integer @validity="numericValidity.coldInputLimit = $event" /></label>
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
.resource-row button { flex:0 0 28px; min-height:28px; }
fieldset { display:grid; gap:10px; margin:0; padding:10px 0 0; border:0; border-top:1px solid #505059; min-width:0; }
legend { padding:0; color:#b9bec6; }
input,select { width:100%; min-width:0; box-sizing:border-box; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; }
button { display:flex; align-items:center; justify-content:center; gap:6px; padding:6px; border:1px solid #64776d; border-radius:3px; }
p { margin:0; color:#b9bec6; overflow-wrap:anywhere; } .warning { color:#e4a0a0; } :disabled { opacity:.55; }
.checkbox-field { display:flex; align-items:center; gap:6px; } .checkbox-field input { width:16px; height:16px; }
</style>
