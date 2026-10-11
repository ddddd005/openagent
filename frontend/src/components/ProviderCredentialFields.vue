<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from "vue";
import { RefreshCw, Trash2 } from "lucide-vue-next";
import { createLocalDraftChannel } from "../adapters/localDraftsApi";
import { createProviderCredentialEditor } from "../application/providerCredentialEditing";
import type { CredentialReference } from "../domain/workflowModelResources";
import type { CurrentProvider } from "../domain/workflowModelResources";
import type { ProviderCredentialRequest } from "../adapters/providerConfigurationApi";
import type { ProviderCredentialBasis } from "../application/providerCredentialEditing";
import { graphJsonSignature, graphObject } from "../domain/workflowGraph";

const props = defineProps<{ owner: string; reference: CredentialReference | null; basis: ProviderCredentialBasis; disabled?: boolean;
  acceptSaved: (reference: CredentialReference, provider: CurrentProvider, request: ProviderCredentialRequest) => Promise<void> }>();
const emit = defineEmits<{ blocked: [value: boolean];
  mutation: [value: boolean]; dirty: [value: boolean] }>();
const channel = createLocalDraftChannel({ scope: "management", kind: "intent", key: `credential:${props.owner}`,
  workflow_definition_id: null, workflow_session_id: null });
const editor = createProviderCredentialEditor({ ...channel, archive: async value => {
  const archive = createLocalDraftChannel({ scope: "management", kind: "form",
    key: `archive:provider-key:${props.owner}:${crypto.randomUUID()}`,
    workflow_definition_id: null, workflow_session_id: null });
  await archive.write({ schema_version: 1, kind: "workflow.provider-key-request-archive", request: value });
  const saved = await archive.read(true);
  if (!saved || saved.deleted || !graphObject(saved.value)
    || graphJsonSignature(saved.value.request) !== graphJsonSignature(value))
    throw new Error("原 Key 请求归档未核实，原记录仍保留");
} }, (reference, provider, request) => props.acceptSaved(reference, provider, request));
const { secret, busy, ready, available, error, state, pending, blocked } = editor;
watch(blocked, value => emit("blocked", value), { immediate: true });
watch(computed(() => busy.value || !!pending.value), value => emit("mutation", value), { immediate: true });
watch(secret, value => emit("dirty", value.length > 0), { immediate: true, flush: "sync" });
watch(() => [props.reference, graphJsonSignature(props.basis)], () => {
  if (editor.adoptSavedBasis(props.reference, props.basis)
    || editor.updateDraftBasis(props.reference, props.basis)) return;
  if (typeof window === "undefined") ready.value = true;
  else void editor.load(props.reference, props.basis);
}, { immediate: true });
function save(remove = false) { if (!props.disabled) return editor.save(remove); }
function submit() {
  if (editor.updateDraftBasis(props.reference, props.basis)) return editor.save();
}
defineExpose({ submit });
function abandon() {
  return editor.abandon(window.confirm("归档原 Key 请求并解除本机锁定？这不会撤销已发生的保存，也不会重新提交。"));
}
onBeforeUnmount(editor.dispose);
</script>
<template>
  <section class="provider-credential-fields" aria-label="私有 API Key">
    <label>API Key<input v-model="secret" type="password" autocomplete="off" name="provider-api-key"
      autocapitalize="none" :spellcheck="false" aria-label="API Key"
      :disabled="disabled || blocked || !available" maxlength="8192"
      :placeholder="state?.state === 'saved' ? '已保存；留空保持不变' : ''" /></label>
    <div class="credential-actions">
      <button v-if="reference?.startsWith('local:') && state?.state === 'saved'" type="button"
        title="删除本机 Key" aria-label="删除本机 Key" :disabled="disabled || blocked" @click="save(true)"><Trash2 :size="14" /></button>
      <button v-if="pending" type="button" :disabled="busy" @click="editor.reconcile"><RefreshCw :size="14" />核实原 Key 请求</button>
      <button v-if="pending" type="button" :disabled="busy" @click="abandon">归档并解除锁定</button>
    </div>
    <p v-if="state" role="status">{{ state.state === 'saved' ? '本机 Key 已保存' : 'Key 缺失，请填写并保存' }}</p>
    <p v-if="ready && !available" role="status">当前宿主不支持本机 Key 安全保护，无法保存或使用 Key。</p>
    <p v-if="error" role="status">{{ error }}</p>
  </section>
</template>
<style scoped>
.provider-credential-fields { display:grid; gap:8px; min-width:0; border-top:1px solid #505059; padding-top:8px; }
label { display:grid; gap:5px; min-width:0; }
input { box-sizing:border-box; min-width:0; width:100%; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; }
.credential-actions { display:flex; flex-wrap:wrap; gap:6px; }
button { display:flex; align-items:center; gap:5px; padding:6px; border:1px solid #64776d; border-radius:3px; }
p { margin:0; font-size:11px; line-height:1.5; overflow-wrap:anywhere; color:#b9bec6; }
:disabled { opacity:.55; }
</style>
