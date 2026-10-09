<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Import, Plus, RefreshCw, Save, X } from "lucide-vue-next";
import { useProviderResources } from "../application/workflowResources";
import { cloneProvider, newProvider, type CurrentProvider, type ProviderProtocol, type CredentialReference } from "../domain/workflowModelResources";
import { useResourceFormDraft } from "../composables/useResourceFormDraft";
import FormDraftNotice from "./FormDraftNotice.vue";
import ProviderCredentialFields from "./ProviderCredentialFields.vue";

const resources = useProviderResources();
const records = resources.records, loading = resources.loading;
const submitting = ref(false);
const credentialBlocked = ref(false), credentialDirty = ref(false), credentialMutating = ref(false);
const locked = computed(() => resources.locked.value || submitting.value || localDraft.loading.value || localDraft.conflict.value);
const error = resources.error, pending = resources.pending, busy = resources.busy;
const editor = useResourceFormDraft<CurrentProvider>("provider", newProvider, record => record.value.name);
const { form, expectedSequence, localDraft, choices } = editor;
function edit(record: CurrentProvider | null) {
  if (locked.value) return;
  editor.edit(record);
}
async function save() {
  if (!form.value || locked.value || credentialBlocked.value || credentialDirty.value) return;
  submitting.value = true;
  const record = cloneProvider(form.value), sequence = expectedSequence.value;
  try {
    if (!await localDraft.flush()) return;
    if (await resources.save(record, sequence)) editor.close();
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
  if (value.credential_ref) value.credential_ref = protocol === "gemini" ? "env:GEMINI_API_KEY" : "env:DEEPSEEK_API_KEY";
  value.protocol = protocol;
  form.value.data_schema_version = protocol === "gemini" ? 2 : 1;
}
function bindCredential(reference: CredentialReference) {
  if (!form.value) return;
  form.value.data_schema_version = 3;
  form.value.value.credential_ref = reference;
}
onMounted(() => { void resources.refresh(); });
</script>
<template>
  <section class="current-provider-panel" aria-label="新版供应商当前资源">
    <header>
      <strong>模型供应商 · 当前资源</strong>
      <button type="button" title="刷新供应商" aria-label="刷新供应商" :disabled="loading" @click="resources.refresh()"><RefreshCw :size="15" /></button>
      <button type="button" title="新增供应商资源" aria-label="新增供应商资源" :disabled="locked" @click="edit(null)"><Plus :size="16" /></button>
    </header>
    <p v-if="loading" role="status">读取中</p>
    <p v-else-if="!records.length && !error">暂无供应商当前资源</p>
    <FormDraftNotice v-if="localDraft.message.value" :message="localDraft.message.value" :conflict="localDraft.conflict.value" @restore="localDraft.restoreConflict" @discard="localDraft.discardConflict" />
    <button v-if="resources.legacyRaw.value" type="button" :disabled="busy || submitting || localDraft.loading.value || localDraft.conflict.value" @click="resources.importLegacyPending()"><Import :size="14" />导入旧供应商请求</button>
    <select v-if="choices.length" aria-label="未提交供应商草稿" value="" :disabled="locked" @change="editor.resume(($event.target as HTMLSelectElement).value)">
      <option value="">未提交草稿</option><option v-for="choice in choices" :key="choice.id" :value="choice.id">{{ choice.label }}</option>
    </select>
    <ul>
      <li v-for="record in records" :key="record.scope + record.resource_id">
        <button type="button" :disabled="locked" @click="edit(record)">
          <strong>{{ record.value.name }}</strong><span>{{ record.value.enabled ? record.value.protocol === 'gemini' ? 'Gemini' : 'Chat' : '已停用' }} · s{{ record.update_sequence }}</span>
          <small>{{ record.scope }} · {{ record.value.base_url }}</small>
        </button>
      </li>
    </ul>
    <form v-if="form" @submit.prevent="save">
      <header><strong>{{ expectedSequence ? '更新当前资源' : '新增当前资源' }}</strong>
        <button type="button" title="取消编辑" aria-label="取消编辑" :disabled="locked" @click="editor.close()"><X :size="15" /></button>
      </header>
      <fieldset :disabled="locked || localDraft.loading.value || credentialMutating || credentialDirty">
        <label>名称<input v-model="form.value.name" required maxlength="128" /></label>
        <label>协议<select :value="form.value.protocol" aria-label="供应商协议" :disabled="expectedSequence !== 0" @change="changeProtocol">
          <option value="chat">Chat Completions</option><option value="gemini">Gemini generateContent</option>
        </select></label>
        <label>范围<output>{{ form.scope }}</output></label>
        <label>服务地址<input v-model="form.value.base_url" type="url" required maxlength="2048" autocomplete="off" /></label>
        <label>后端凭据引用<select v-model="form.value.credential_ref" aria-label="后端凭据引用">
          <option :value="null">未配置</option>
          <option v-if="form.value.credential_ref?.startsWith('local:')" :value="form.value.credential_ref">本机私有 Key</option>
          <option v-if="form.value.protocol === 'chat'" value="env:DEEPSEEK_API_KEY">env:DEEPSEEK_API_KEY</option>
          <option v-else value="env:GEMINI_API_KEY">env:GEMINI_API_KEY</option>
        </select></label>
        <label class="enabled"><input v-model="form.value.enabled" type="checkbox" />启用</label>
      </fieldset>
      <ProviderCredentialFields :key="form.scope + ':' + form.resource_id" :owner="form.scope + ':' + form.resource_id"
        :reference="form.value.credential_ref" :disabled="locked" @saved="bindCredential"
        @blocked="credentialBlocked = $event" @dirty="credentialDirty = $event" @mutation="credentialMutating = $event" />
      <button type="submit" :disabled="locked || credentialBlocked || credentialDirty"><Save :size="14" />保存当前资源</button>
    </form>
    <div v-if="pending" class="warning" role="status">
      <p>资源提交结果待核实</p>
      <button type="button" :disabled="busy" @click="resources.reconcile()"><RefreshCw :size="14" />核实原资源请求</button>
    </div>
    <p v-if="error" class="warning" role="status">{{ error }}</p>
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
