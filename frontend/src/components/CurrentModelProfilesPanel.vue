<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Archive, Plus, RefreshCw, Save, X } from "lucide-vue-next";
import { useModelProfiles } from "../application/modelProfiles";
import { listProviderModels, type DiscoveredModel } from "../adapters/providerConfigurationApi";
import { graphClone, graphJsonSignature } from "../domain/workflowGraph";
import { isCurrentModelProfile, modelParameterDiagnostic, modelProfileDiagnostic,
  newModelProfile, newProvider, providerIdentity, sameProviderIdentity, type CurrentModelProfile,
  type CurrentProvider, type ProviderIdentity } from "../domain/workflowModelResources";
import { useResourceFormEditor } from "../composables/useResourceFormEditor";
import RawNumberInput from "./RawNumberInput.vue";
import { listModelProfiles } from "../adapters/workflowModelProfilesApi";

const props = defineProps<{ providers: CurrentProvider[]; mutatingProvider?: ProviderIdentity | null; disabled?: boolean }>();
type RawFields = "context_length_tokens" | "compaction_threshold_percent" | "output_limit_tokens" |
  "context_window_tokens" | "input_limit_tokens" | "max_output_tokens" | "budget";
type ProfileForm = CurrentModelProfile & { raw: Record<RawFields, string>; context_mode: "custom" | "model" };
const profileForm = (record: CurrentModelProfile): ProfileForm => ({ ...graphClone(record),
  context_mode: record.value.runtime.context_length_tokens === null ? "model" : "custom",
  raw: { context_length_tokens: String(record.value.runtime.context_length_tokens
      ?? (record.value.model.capacity.context_window_tokens || 300_000)),
    compaction_threshold_percent: String(record.value.runtime.compaction_threshold_bps / 100),
    output_limit_tokens: String(record.value.runtime.output_limit_tokens),
    context_window_tokens: String(record.value.model.capacity.context_window_tokens),
    input_limit_tokens: String(record.value.model.capacity.input_limit_tokens),
    max_output_tokens: String(record.value.model.capacity.max_output_tokens),
    budget: record.value.runtime.thinking === "disabled" ? "-1"
      : record.value.runtime.thinking.mode === "budget" ? String(record.value.runtime.thinking.budget) : "-1" } });
const profiles = useModelProfiles();
const editor = useResourceFormEditor<ProfileForm>(
  () => profileForm(newModelProfile(props.providers[0] ?? newProvider())));
const { form, expectedSequence } = editor;
const submitting = ref(false), status = ref(""), discovered = ref<DiscoveredModel[]>([]), discovering = ref(false);
const validity = ref<Partial<Record<RawFields, boolean>>>({});
const locked = computed(() => !!props.disabled || profiles.locked.value || submitting.value);
const provider = computed(() => form.value && props.providers.find(row =>
  sameProviderIdentity(providerIdentity(row), form.value!.value.provider)));
const protocol = computed(() => provider.value?.value.protocol ?? "chat");
const providerMutating = computed(() => !!provider.value && !!props.mutatingProvider
  && sameProviderIdentity(providerIdentity(provider.value), props.mutatingProvider));
const providerKey = (row: CurrentProvider) => JSON.stringify([row.scope, row.resource_id]);
const selectedProvider = computed({ get: () => provider.value ? providerKey(provider.value) : "",
  set: (key: string) => {
    const selected = props.providers.find(row => providerKey(row) === key);
    if (!selected || !form.value) return;
    form.value.value.provider = providerIdentity(selected);
    form.value.value.runtime.thinking = "disabled";
  } });
const thinkingMode = computed({ get: () => form.value?.value.runtime.thinking === "disabled" ? "disabled"
  : form.value?.value.runtime.thinking.mode ?? "disabled",
  set: (mode: string) => {
    if (!form.value) return;
    form.value.value.runtime.thinking = mode === "budget" ? { mode, budget: -1, include_summary: true }
      : mode === "level" ? { mode, level: "low", include_summary: true } : "disabled";
  } });
const contextMode = computed({ get: () => form.value?.context_mode ?? "custom",
  set: (mode: string) => {
    if (!form.value) return;
    form.value.context_mode = mode === "model" ? "model" : "custom";
    form.value.value.runtime.context_length_tokens = mode === "model"
      ? null : Number(form.value.raw.context_length_tokens);
  } });
let generation = 0;
watch(() => [selectedProvider.value, graphJsonSignature(provider.value), form.value?.resource_id, providerMutating.value], () => {
  generation++; discovered.value = []; discovering.value = false; status.value = "";
});
onBeforeUnmount(profiles.setCompletionHandler(async request => {
  editor.acceptSubmitted(profileForm(request.record), request.expected_sequence,
    ({ raw: _raw, context_mode: _contextMode, ...record }) => record);
  return true;
}));
function edit(record: CurrentModelProfile | null) {
  if (!locked.value) { status.value = ""; editor.edit(record ? profileForm(record) : null); }
}
async function discover() {
  if (!provider.value || locked.value || providerMutating.value || discovering.value) return;
  if (!provider.value.value.enabled || !provider.value.value.credential_ref) {
    status.value = "请先在模型供应商中启用供应商并保存 Key"; return;
  }
  const fixed = graphClone(provider.value), token = ++generation;
  discovering.value = true; status.value = "";
  try {
    const models = await listProviderModels(providerIdentity(fixed), fixed.update_sequence, fixed.value.protocol);
    if (token === generation && graphJsonSignature(provider.value) === graphJsonSignature(fixed)) discovered.value = models;
  } catch (failure) {
    if (token === generation) status.value = failure instanceof Error ? failure.message : "上游型号读取失败";
  } finally { if (token === generation) discovering.value = false; }
}
function chooseModel(event: Event) {
  const row = discovered.value.find(item => item.model === (event.target as HTMLSelectElement).value);
  if (!row?.adapted || !form.value) return;
  form.value.value.model.id = row.model;
}
async function save() {
  if (!form.value || locked.value) return;
  status.value = "";
  if (!provider.value) { status.value = "请先保存供应商，再选择模型配置的供应商"; return; }
  const raw = form.value.raw;
  const required: RawFields[] = ["context_window_tokens", "input_limit_tokens", "max_output_tokens",
    "compaction_threshold_percent", "output_limit_tokens"];
  if (contextMode.value === "custom") required.push("context_length_tokens");
  if (thinkingMode.value === "budget") required.push("budget");
  if (required.some(key => !raw[key].trim() || validity.value[key] === false)) {
    status.value = "数字参数未填写完整或超出范围"; return;
  }
  const percentage = Number(raw.compaction_threshold_percent), basisPoints = Math.round(percentage * 100);
  if (percentage !== basisPoints / 100) { status.value = "压缩阈值最多精确到 0.01%"; return; }
  const { raw: _raw, context_mode: _contextMode, ...record } = graphClone(form.value);
  record.value.model.id = form.value.value.model.id;
  for (const key of ["context_window_tokens", "input_limit_tokens", "max_output_tokens"] as const)
    record.value.model.capacity[key] = Number(raw[key]);
  record.value.runtime.context_length_tokens = contextMode.value === "model" ? null : Number(raw.context_length_tokens);
  record.value.runtime.compaction_threshold_bps = basisPoints;
  record.value.runtime.output_limit_tokens = Number(raw.output_limit_tokens);
  if (record.value.runtime.thinking !== "disabled" && record.value.runtime.thinking.mode === "budget")
    record.value.runtime.thinking.budget = Number(raw.budget);
  const diagnostic = modelProfileDiagnostic(record.value);
  const parameterError = modelParameterDiagnostic(record.value.model.id, record.value.runtime.thinking, protocol.value);
  if (diagnostic || parameterError || !isCurrentModelProfile(record)) {
    status.value = diagnostic ?? parameterError ?? "模型配置字段无效"; return;
  }
  form.value.value = graphClone(record.value);
  submitting.value = true;
  try {
    await profiles.save(record, expectedSequence.value);
  } finally { submitting.value = false; }
}
async function abandon() {
  const confirmed = window.confirm("归档并解除此原请求的本机锁定？这不会撤销已发生的保存，也不会重新提交。");
  await profiles.abandon(confirmed);
}
async function reloadSaved() {
  if (!form.value || locked.value
    || !window.confirm("丢弃当前未提交表单并读取生效模型配置？")) return;
  const identity = [form.value.scope, form.value.resource_id];
  submitting.value = true;
  try {
    const records = await listModelProfiles();
    const saved = records.find(row => row.scope === identity[0] && row.resource_id === identity[1]);
    if (!saved) throw new Error("生效模型配置不存在；原表单保留，请新增模型配置");
    const restored = await editor.reloadSaved(profileForm(saved), true);
    if (restored) { profiles.error.value = ""; profiles.records.value = records; }
    status.value = restored ? "已读取生效模型配置" : "当前模型表单已变化，未替换";
  } catch (failure) { status.value = failure instanceof Error ? failure.message : "读取生效模型配置失败，原表单保留"; }
  finally { submitting.value = false; }
}
onMounted(() => { void profiles.refresh(); });
</script>
<template>
  <section class="model-profiles-panel" aria-label="全局模型配置">
    <header><strong>模型配置</strong>
      <button type="button" title="刷新模型配置" aria-label="刷新模型配置" :disabled="profiles.loading.value" @click="profiles.refresh"><RefreshCw :size="15" /></button>
      <button type="button" title="新增模型配置" aria-label="新增模型配置" :disabled="locked || !providers.length" @click="edit(null)"><Plus :size="16" /></button>
    </header>
    <button v-if="profiles.legacyRaw.value" type="button" :disabled="profiles.busy.value || submitting"
      @click="profiles.reconcileLegacyPending()"><RefreshCw :size="14" />核实旧模型配置原请求</button>
    <ul><li v-for="record in profiles.records.value" :key="record.scope + record.resource_id">
      <button type="button" :disabled="locked" @click="edit(record)"><strong>{{ record.value.name }}</strong>
        <small>{{ record.value.model.id }}{{ record.value.enabled ? '' : ' · 已停用' }}</small></button>
    </li></ul>
    <form v-if="form" @submit.prevent="save">
      <header><strong>{{ expectedSequence ? '编辑模型配置' : '新增模型配置' }}</strong>
        <button type="button" title="重新读取生效模型配置" aria-label="重新读取生效模型配置" :disabled="locked" @click="reloadSaved"><RefreshCw :size="15" /></button>
        <button type="button" title="关闭编辑" aria-label="关闭模型配置编辑" :disabled="locked" @click="editor.close"><X :size="15" /></button>
      </header>
      <fieldset :disabled="locked">
        <label>配置名称<input v-model="form.value.name" required maxlength="128" /></label>
        <label>供应商<select v-model="selectedProvider" required aria-label="模型配置供应商">
          <option value="">选择供应商</option><option v-for="row in providers" :key="providerKey(row)" :value="providerKey(row)">{{ row.value.name }}</option>
        </select></label>
        <label>模型名称<input v-model="form.value.model.id" required maxlength="256" /></label>
        <div class="actions">
          <select v-if="discovered.length" value="" aria-label="上游模型列表" @change="chooseModel">
            <option value="">选择上游型号</option><option v-for="row in discovered" :key="row.model" :value="row.model" :disabled="!row.adapted">{{ row.model }}{{ row.adapted ? '' : '（尚未适配）' }}</option>
          </select>
          <button type="button" title="获取上游模型列表" aria-label="获取上游模型列表" :disabled="!provider || providerMutating || discovering" @click="discover"><RefreshCw :size="14" /></button>
        </div>
        <template v-if="protocol === 'gemini'">
          <label>思考模式<select v-model="thinkingMode" aria-label="思考模式">
            <option value="disabled">关闭</option><option value="budget">Token 预算</option><option value="level">强度</option>
          </select></label>
          <label v-if="thinkingMode === 'budget'">思考预算 tokens<RawNumberInput v-model:raw-value="form.raw.budget" :disabled="locked" :min="-1" integer @validity="validity.budget = $event" /></label>
          <label v-if="form.value.runtime.thinking !== 'disabled' && form.value.runtime.thinking.mode === 'level'">思考强度
            <select v-model="form.value.runtime.thinking.level" aria-label="思考强度"><option v-for="level in ['minimal', 'low', 'medium', 'high']" :key="level" :value="level">{{ level }}</option></select>
          </label>
        </template>
        <label>上下文长度方式<select v-model="contextMode" aria-label="上下文长度方式">
          <option value="custom">自定义长度</option><option value="model">真实模型窗口</option>
        </select></label>
        <label v-if="contextMode === 'custom'">上下文长度 tokens<RawNumberInput v-model:raw-value="form.raw.context_length_tokens" :disabled="locked" :min="1" integer @validity="validity.context_length_tokens = $event" /></label>
        <label>压缩阈值 %<RawNumberInput v-model:raw-value="form.raw.compaction_threshold_percent" :disabled="locked" :min="0.01" :max="100" step="0.01" @validity="validity.compaction_threshold_percent = $event" /></label>
        <label>输出上限 tokens<RawNumberInput v-model:raw-value="form.raw.output_limit_tokens" :disabled="locked" :min="1" integer @validity="validity.output_limit_tokens = $event" /></label>
        <details><summary>真实模型容量</summary>
          <label>实际上下文窗口 tokens<RawNumberInput v-model:raw-value="form.raw.context_window_tokens" :disabled="locked" :min="1" integer @validity="validity.context_window_tokens = $event" /></label>
          <label>实际输入上限 tokens<RawNumberInput v-model:raw-value="form.raw.input_limit_tokens" :disabled="locked" :min="1" integer @validity="validity.input_limit_tokens = $event" /></label>
          <label>实际输出上限 tokens<RawNumberInput v-model:raw-value="form.raw.max_output_tokens" :disabled="locked" :min="1" integer @validity="validity.max_output_tokens = $event" /></label>
        </details>
        <label class="checkbox"><input v-model="form.value.enabled" type="checkbox" />启用</label>
      </fieldset>
      <button type="submit" :disabled="locked"><Save :size="14" />保存模型配置</button>
    </form>
    <div v-if="profiles.pending.value" role="status"><p>模型配置提交结果待核实，未重新提交</p>
      <div class="actions"><button type="button" :disabled="profiles.busy.value" @click="profiles.reconcile"><RefreshCw :size="14" />核实原请求</button>
        <button type="button" :disabled="profiles.busy.value" @click="abandon"><Archive :size="14" />归档并解除锁定</button></div>
    </div>
    <p v-if="status || profiles.error.value" class="warning" role="status">{{ status || profiles.error.value }}</p>
  </section>
</template>
<style scoped>
.model-profiles-panel { display:grid; gap:10px; min-width:0; border-top:1px solid #505059; padding-top:12px; }
header,.actions { display:flex; align-items:center; gap:6px; min-width:0; flex-wrap:wrap; } header strong { flex:1; overflow-wrap:anywhere; }
button { display:flex; gap:6px; align-items:center; padding:6px; border-radius:3px; } header button { width:28px; height:28px; justify-content:center; }
ul { list-style:none; padding:0; margin:0; } li>button { display:grid; width:100%; min-width:0; text-align:left; border-bottom:1px solid #505059; }
strong,small { overflow-wrap:anywhere; } small { color:#b9bec6; }
form,fieldset,label { display:grid; gap:8px; min-width:0; } fieldset { margin:0; padding:0; border:0; }
input,select { width:100%; min-width:0; box-sizing:border-box; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; }
.checkbox { display:flex; align-items:center; } .checkbox input { width:16px; height:16px; }
p { margin:0; overflow-wrap:anywhere; line-height:1.5; color:#b9bec6; } .warning { color:#e4a0a0; } :disabled { opacity:.55; }
</style>
