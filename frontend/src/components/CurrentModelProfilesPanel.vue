<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Archive, Plus, RefreshCw, Save, X } from "lucide-vue-next";
import { useModelProfiles } from "../application/modelProfiles";
import { listProviderModels, type DiscoveredModel } from "../adapters/providerConfigurationApi";
import { graphClone, graphJsonSignature } from "../domain/workflowGraph";
import { geminiThinkingCapability, isCurrentModelProfile, modelProfileDiagnostic, newModelProfile, newProvider,
  providerIdentity, sameProviderIdentity, type CurrentModelProfile, type CurrentProvider, type ProviderIdentity, type ThinkingLevel } from "../domain/workflowModelResources";
import { useResourceFormEditor } from "../composables/useResourceFormEditor";
import RawNumberInput from "./RawNumberInput.vue";
import { listModelProfiles } from "../adapters/workflowModelProfilesApi";

const props = defineProps<{ providers: CurrentProvider[]; mutatingProvider?: ProviderIdentity | null; disabled?: boolean }>();
type RawFields = "max_tokens" | "temperature" | "budget" | "context_window_tokens" |
  "output_reserve_tokens" | "summary_max_tokens" | "max_cold_input_tokens";
type ProfileForm = CurrentModelProfile & { raw: Record<RawFields, string> };
const profileForm = (record: CurrentModelProfile): ProfileForm => ({ ...graphClone(record),
  raw: { max_tokens: String(record.value.parameters.max_tokens ?? ""), temperature: String(record.value.parameters.temperature ?? ""),
    budget: record.value.parameters.thinking === "disabled" ? "-1"
      : record.value.parameters.thinking.mode === "budget" ? String(record.value.parameters.thinking.budget) : "-1",
    ...Object.fromEntries(Object.entries(record.value.capacity).map(([key, value]) => [key, String(value)])),
  } as Record<RawFields, string> });
const profiles = useModelProfiles();
const editor = useResourceFormEditor<ProfileForm>(
  () => profileForm(newModelProfile(props.providers[0] ?? newProvider())));
const { form, expectedSequence } = editor;
const submitting = ref(false), status = ref(""), discovered = ref<DiscoveredModel[]>([]), discovering = ref(false);
const validity = ref<Partial<Record<RawFields, boolean>>>({});
const locked = computed(() => !!props.disabled || profiles.locked.value || submitting.value);
const provider = computed(() => form.value && props.providers.find(row =>
  sameProviderIdentity(providerIdentity(row), form.value!.value.provider)));
const providerMutating = computed(() => !!provider.value && !!props.mutatingProvider
  && sameProviderIdentity(providerIdentity(provider.value), props.mutatingProvider));
const providerKey = (row: CurrentProvider) => JSON.stringify([row.scope, row.resource_id]);
const selectedProvider = computed({ get: () => provider.value ? providerKey(provider.value) : "",
  set: (key: string) => {
    const selected = props.providers.find(row => providerKey(row) === key);
    if (!selected || !form.value) return;
    form.value.value.provider = providerIdentity(selected);
    form.value.value.protocol = selected.value.protocol;
    form.value.value.parameters.thinking = "disabled";
  } });
const capability = computed(() => form.value ? geminiThinkingCapability(form.value.value.parameters.model) : null);
const thinkingMode = computed({ get: () => form.value?.value.parameters.thinking === "disabled" ? "disabled"
  : form.value?.value.parameters.thinking.mode ?? "disabled",
  set: (mode: string) => {
    if (!form.value) return;
    form.value.value.parameters.thinking = mode === "budget" ? { mode, budget: -1, include_summary: true }
      : mode === "level" ? { mode, level: capability.value?.levels?.[0] ?? "low", include_summary: true } : "disabled";
  } });
let generation = 0;
watch(() => [selectedProvider.value, graphJsonSignature(provider.value), form.value?.resource_id, providerMutating.value], () => {
  generation++; discovered.value = []; discovering.value = false; status.value = "";
});
onBeforeUnmount(profiles.setCompletionHandler(async request => {
  editor.acceptSubmitted(profileForm(request.record), request.expected_sequence, ({ raw: _raw, ...record }) => record);
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
  form.value.value.parameters.model = row.model;
  if (form.value.value.protocol === "gemini") thinkingMode.value = geminiThinkingCapability(row.model)?.mode ?? "disabled";
}
async function save() {
  if (!form.value || locked.value) return;
  status.value = "";
  if (!provider.value) { status.value = "请先保存供应商，再选择模型配置的供应商"; return; }
  const raw = form.value.raw;
  const required: RawFields[] = ["max_tokens", "context_window_tokens", "output_reserve_tokens", "summary_max_tokens", "max_cold_input_tokens"];
  if (thinkingMode.value === "budget") required.push("budget");
  if (required.some(key => !raw[key].trim() || validity.value[key] === false) || validity.value.temperature === false) {
    status.value = "数字参数未填写完整或超出范围"; return;
  }
  const { raw: _raw, ...record } = graphClone(form.value);
  record.value.parameters.max_tokens = Number(raw.max_tokens);
  if (raw.temperature.trim()) record.value.parameters.temperature = Number(raw.temperature);
  else delete record.value.parameters.temperature;
  if (record.value.parameters.thinking !== "disabled" && record.value.parameters.thinking.mode === "budget")
    record.value.parameters.thinking.budget = Number(raw.budget);
  for (const key of ["context_window_tokens", "output_reserve_tokens", "summary_max_tokens", "max_cold_input_tokens"] as const)
    record.value.capacity[key] = Number(raw[key]);
  const diagnostic = modelProfileDiagnostic(record.value);
  if (diagnostic || !isCurrentModelProfile(record)) { status.value = diagnostic ?? "模型配置字段无效"; return; }
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
        <small>{{ record.value.parameters.model }}{{ record.value.enabled ? '' : ' · 已停用' }}</small></button>
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
        <label>模型名称<input v-model="form.value.parameters.model" required maxlength="256" /></label>
        <div class="actions">
          <select v-if="discovered.length" value="" aria-label="上游模型列表" @change="chooseModel">
            <option value="">选择上游型号</option><option v-for="row in discovered" :key="row.model" :value="row.model" :disabled="!row.adapted">{{ row.model }}{{ row.adapted ? '' : '（尚未适配）' }}</option>
          </select>
          <button type="button" title="获取上游模型列表" aria-label="获取上游模型列表" :disabled="!provider || providerMutating || discovering" @click="discover"><RefreshCw :size="14" /></button>
        </div>
        <label>最大输出 tokens<RawNumberInput v-model:raw-value="form.raw.max_tokens" :disabled="locked" :min="1" :max="8192" integer @validity="validity.max_tokens = $event" /></label>
        <label>温度<RawNumberInput v-model:raw-value="form.raw.temperature" :disabled="locked" :required="false" :min="0" :max="2" @validity="validity.temperature = $event" /></label>
        <template v-if="form.value.protocol === 'gemini'">
          <label>思考模式<select v-model="thinkingMode" aria-label="思考模式">
            <option value="disabled">关闭</option><option value="budget">Token 预算</option><option value="level">强度</option>
          </select></label>
          <label v-if="thinkingMode === 'budget'">思考预算 tokens<RawNumberInput v-model:raw-value="form.raw.budget" :disabled="locked" :min="-1" :max="32768" integer @validity="validity.budget = $event" /></label>
          <label v-if="form.value.parameters.thinking !== 'disabled' && form.value.parameters.thinking.mode === 'level'">思考强度
            <select v-model="form.value.parameters.thinking.level" aria-label="思考强度"><option v-for="level in (capability?.levels ?? ['minimal', 'low', 'medium', 'high']) as ThinkingLevel[]" :key="level" :value="level">{{ level }}</option></select>
          </label>
          <label v-if="form.value.parameters.thinking !== 'disabled'" class="checkbox"><input v-model="form.value.parameters.thinking.include_summary" type="checkbox" />请求思考摘要</label>
        </template>
        <label>上下文窗口 tokens<RawNumberInput v-model:raw-value="form.raw.context_window_tokens" :disabled="locked" :min="1" integer @validity="validity.context_window_tokens = $event" /></label>
        <label>输出预留 tokens<RawNumberInput v-model:raw-value="form.raw.output_reserve_tokens" :disabled="locked" :min="1" integer @validity="validity.output_reserve_tokens = $event" /></label>
        <label>摘要最大输出 tokens<RawNumberInput v-model:raw-value="form.raw.summary_max_tokens" :disabled="locked" :min="1" :max="8192" integer @validity="validity.summary_max_tokens = $event" /></label>
        <label>摘要冷输入上限 tokens<RawNumberInput v-model:raw-value="form.raw.max_cold_input_tokens" :disabled="locked" :min="1" integer @validity="validity.max_cold_input_tokens = $event" /></label>
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
