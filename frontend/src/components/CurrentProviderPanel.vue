<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { Archive, Plus, RefreshCw, Save, X } from "lucide-vue-next";
import { useProviderResources } from "../application/workflowResources";
import { cloneProvider, newProvider, type CurrentProvider, type ProviderProtocol, type CredentialReference } from "../domain/workflowModelResources";
import { useResourceFormEditor } from "../composables/useResourceFormEditor";
import ProviderCredentialFields from "./ProviderCredentialFields.vue";
import CurrentModelProfilesPanel from "./CurrentModelProfilesPanel.vue";
import { providerIdentity, sameProviderIdentity } from "../domain/workflowModelResources";
import type { ProviderCredentialRequest } from "../adapters/providerConfigurationApi";
import { readCurrentProvider } from "../adapters/workflowResourcesApi";
import { graphJsonSignature } from "../domain/workflowGraph";

const resources = useProviderResources();
const records = resources.records, loading = resources.loading;
const submitting = ref(false);
const credentialBlocked = ref(false), credentialDirty = ref(false), credentialMutating = ref(false);
const credentialFields = ref<InstanceType<typeof ProviderCredentialFields> | null>(null);
const locked = computed(() => resources.locked.value || submitting.value || credentialMutating.value);
const error = resources.error, pending = resources.pending, busy = resources.busy;
const editor = useResourceFormEditor<CurrentProvider>(newProvider);
const { form, expectedSequence } = editor;
const status = ref("");
const credentialBasis = computed(() => form.value ? {
  identity: providerIdentity(form.value), expected_sequence: expectedSequence.value,
  provider_seed: { name: form.value.value.name, protocol: form.value.value.protocol,
    base_url: form.value.value.base_url, enabled: form.value.value.enabled },
} : null);
const mutatingProvider = computed(() => {
  const original = resources.pending.value?.record;
  if (original) return providerIdentity(original);
  return (submitting.value || credentialMutating.value) && form.value ? providerIdentity(form.value) : null;
});
onBeforeUnmount(resources.setCompletionHandler(async request => {
  editor.acceptSubmitted(request.record, request.expected_sequence);
  status.value = "供应商已保存";
  return true;
}));
function edit(record: CurrentProvider | null) {
  if (locked.value) return;
  status.value = "";
  editor.edit(record);
}
async function save() {
  if (!form.value || locked.value || credentialBlocked.value) return;
  const saveKey = credentialDirty.value;
  status.value = "";
  submitting.value = true;
  const record = cloneProvider(form.value), sequence = expectedSequence.value;
  try {
    if (saveKey) await credentialFields.value?.submit();
    else await resources.save(record, sequence);
  } finally { submitting.value = false; }
}
function changeProtocol(event: Event) {
  if (!form.value || locked.value || expectedSequence.value !== 0) return;
  const protocol = (event.target as HTMLSelectElement).value as ProviderProtocol;
  const value = form.value.value;
  const previousDefault = value.protocol === "gemini"
    ? "https://generativelanguage.googleapis.com/v1beta" : "https://api.deepseek.com";
  if (value.base_url === previousDefault) value.base_url = protocol === "gemini"
    ? "https://generativelanguage.googleapis.com/v1beta" : "https://api.deepseek.com";
  if (value.name === "DeepSeek" || value.name === "Gemini") value.name = protocol === "gemini" ? "Gemini" : "DeepSeek";
  value.protocol = protocol;
  form.value.data_schema_version = 3;
}
async function acceptCredential(reference: CredentialReference, provider: CurrentProvider, request: ProviderCredentialRequest) {
  if (!form.value || !sameProviderIdentity(providerIdentity(form.value), request.identity))
    throw new Error("Key 已生效，但原供应商表单不再打开；重新打开后核实原 Key 请求");
  const sameForm = request.provider_seed !== null
    && graphJsonSignature(credentialBasis.value?.provider_seed) === graphJsonSignature(request.provider_seed);
  if (provider.update_sequence === request.expected_sequence + 1
    && editor.acceptCredential(provider, sameForm ? expectedSequence.value : request.expected_sequence, provider)) {
    form.value.value.credential_ref = reference;
    status.value = "供应商配置与 Key 已保存";
  } else if (form.value.value.credential_ref !== reference || expectedSequence.value !== provider.update_sequence) {
    status.value = "Key 原请求已核实，但供应商版本已变化；请重新读取已保存配置，未覆盖表单";
    throw new Error(status.value);
  }
  await resources.refresh();
}
async function abandon() {
  await resources.abandon(window.confirm("归档并解除原资源请求的本机锁定？这不会撤销已发生的保存，也不会重新提交。"));
}
async function reloadSaved() {
  if (!form.value || locked.value || credentialBlocked.value || credentialDirty.value
    || !window.confirm("读取已保存供应商配置并替换当前输入？")) return;
  submitting.value = true;
  try {
    const record = await readCurrentProvider(providerIdentity(form.value));
    if (!record) throw new Error("生效供应商不存在；原表单保留，请新增供应商");
    const restored = await editor.reloadSaved(record, true);
    if (restored) resources.error.value = "";
    status.value = restored ? "已读取生效供应商配置" : "当前表单已变化，未替换";
  } catch (failure) { status.value = failure instanceof Error ? failure.message : "读取生效供应商失败，原表单保留"; }
  finally { submitting.value = false; }
}
onMounted(() => { void resources.refresh(); });
</script>
<template>
  <section class="current-provider-panel" aria-label="新版供应商当前资源">
    <header>
      <strong>模型供应商</strong>
      <button type="button" title="刷新供应商" aria-label="刷新供应商" :disabled="loading" @click="resources.refresh()"><RefreshCw :size="15" /></button>
      <button type="button" title="新增供应商" aria-label="新增供应商" :disabled="locked" @click="edit(null)"><Plus :size="16" /></button>
    </header>
    <p v-if="loading" role="status">读取中</p>
    <p v-else-if="!records.length && !error">暂无供应商当前资源</p>
    <button v-if="resources.legacyRaw.value" type="button" :disabled="busy || submitting" @click="resources.reconcileLegacyPending()"><RefreshCw :size="14" />核实旧供应商原请求</button>
    <ul>
      <li v-for="record in records" :key="record.scope + record.resource_id">
        <button type="button" :disabled="locked" @click="edit(record)">
          <strong>{{ record.value.name }}</strong><span>{{ record.value.enabled ? record.value.protocol === 'gemini' ? 'Gemini' : 'Chat' : '已停用' }} · s{{ record.update_sequence }}</span>
          <small>{{ record.scope }} · {{ record.value.base_url }}</small>
        </button>
      </li>
    </ul>
    <form v-if="form" aria-label="供应商配置" autocomplete="off" @submit.prevent="save">
      <header><strong>{{ expectedSequence ? '编辑供应商' : '新增供应商' }}</strong>
        <button type="button" title="重新读取生效供应商配置" aria-label="重新读取生效供应商配置"
          :disabled="locked || credentialBlocked || credentialDirty" @click="reloadSaved"><RefreshCw :size="15" /></button>
        <button type="button" title="取消编辑" aria-label="取消编辑" :disabled="locked" @click="editor.close()"><X :size="15" /></button>
      </header>
      <fieldset :disabled="locked">
        <label>名称<input v-model="form.value.name" aria-label="供应商名称" required maxlength="128" /></label>
        <label>协议<select :value="form.value.protocol" aria-label="供应商协议" :disabled="expectedSequence !== 0" @change="changeProtocol">
          <option value="chat">Chat Completions</option><option value="gemini">Gemini generateContent</option>
        </select></label>
        <label>服务地址<input v-model="form.value.base_url" aria-label="供应商服务地址" type="url" required maxlength="2048" autocomplete="off" /></label>
        <label class="enabled"><input v-model="form.value.enabled" type="checkbox" />启用</label>
        <details><summary>技术详情</summary><label>范围<output>{{ form.scope }}</output></label><label>资源 UUID<output>{{ form.resource_id }}</output></label></details>
      </fieldset>
      <ProviderCredentialFields v-if="credentialBasis" ref="credentialFields"
        :key="form.scope + ':' + form.resource_id" :owner="form.scope + ':' + form.resource_id"
        :basis="credentialBasis"
        :reference="form.value.credential_ref" :disabled="locked" :accept-saved="acceptCredential"
        @blocked="credentialBlocked = $event" @dirty="credentialDirty = $event" @mutation="credentialMutating = $event" />
      <button type="submit" :disabled="locked || credentialBlocked"><Save :size="14" />保存供应商</button>
    </form>
    <div v-if="pending" class="warning" role="status">
      <p>资源提交结果待核实</p>
      <button type="button" :disabled="busy" @click="resources.reconcile()"><RefreshCw :size="14" />核实原资源请求</button>
      <button type="button" :disabled="busy" @click="abandon"><Archive :size="14" />归档并解除锁定</button>
    </div>
    <p v-if="error" class="warning" role="status">{{ error }}</p>
    <p v-if="status" role="status">{{ status }}</p>
    <CurrentModelProfilesPanel :providers="records" :mutating-provider="mutatingProvider" />
  </section>
</template>
<style scoped>
.current-provider-panel { display:grid; gap:10px; min-width:0; padding:12px; font-size:12px; }
header { display:flex; align-items:center; gap:6px; } header strong { flex:1; min-width:0; overflow-wrap:anywhere; }
button { display:flex; align-items:center; gap:6px; padding:6px; border-radius:3px; } header button { width:28px; height:28px; justify-content:center; }
ul { list-style:none; padding:0; margin:0; min-width:0; } li>button { display:grid; width:100%; min-width:0; grid-template-columns:minmax(0,1fr) auto; text-align:left; border-bottom:1px solid #505059; }
li strong { min-width:0; overflow-wrap:anywhere; } li span { white-space:nowrap; }
small { grid-column:1/-1; overflow-wrap:anywhere; color:#b9bec6; }
form { display:grid; gap:10px; min-width:0; border-top:1px solid #505059; padding-top:10px; }
fieldset { display:grid; gap:10px; border:0; padding:0; min-width:0; } label { display:grid; gap:5px; min-width:0; }
input,select { box-sizing:border-box; width:100%; min-width:0; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; }
.enabled { display:flex; align-items:center; gap:6px; } .enabled input { width:16px; height:16px; }
p { margin:0; color:#b9bec6; line-height:1.5; overflow-wrap:anywhere; } .warning p,.warning { color:#e4a0a0; } :disabled { opacity:.55; }
</style>
