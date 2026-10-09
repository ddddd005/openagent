<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from "vue";
import { KeyRound, RefreshCw, Trash2 } from "lucide-vue-next";
import { createLocalDraftChannel } from "../adapters/localDraftsApi";
import { createProviderCredentialEditor } from "../application/providerCredentialEditing";
import type { CredentialReference } from "../domain/workflowModelResources";

const props = defineProps<{ owner: string; reference: CredentialReference | null; disabled?: boolean }>();
const emit = defineEmits<{ saved: [reference: CredentialReference]; blocked: [value: boolean];
  mutation: [value: boolean]; dirty: [value: boolean] }>();
const channel = createLocalDraftChannel({ scope: "management", kind: "intent", key: `credential:${props.owner}`,
  workflow_definition_id: null, workflow_session_id: null });
const editor = createProviderCredentialEditor(channel, reference => emit("saved", reference));
const { secret, busy, ready, available, error, state, pending, blocked } = editor;
watch(blocked, value => emit("blocked", value), { immediate: true });
watch(computed(() => busy.value || !!pending.value), value => emit("mutation", value), { immediate: true });
watch(secret, value => emit("dirty", value.length > 0));
watch(() => props.reference, reference => {
  if (typeof window === "undefined") ready.value = true;
  else void editor.load(reference);
}, { immediate: true });
function save(remove = false) { if (!props.disabled) return editor.save(remove); }
onBeforeUnmount(editor.dispose);
</script>
<template>
  <section class="provider-credential-fields" aria-label="私有 API Key">
    <label>API Key<input v-model="secret" type="password" autocomplete="new-password" aria-label="API Key"
      :disabled="disabled || blocked || !available" maxlength="8192" /></label>
    <div class="credential-actions">
      <button type="button" :disabled="disabled || blocked || !available || !secret" @click="save()"><KeyRound :size="14" />保存 Key</button>
      <button v-if="reference?.startsWith('local:') && state?.state === 'saved'" type="button"
        title="删除本机 Key" aria-label="删除本机 Key" :disabled="disabled || blocked" @click="save(true)"><Trash2 :size="14" /></button>
      <button v-if="pending" type="button" :disabled="busy" @click="editor.reconcile"><RefreshCw :size="14" />核实原 Key 请求</button>
    </div>
    <p v-if="state" role="status">{{ state.state === 'saved' ? '凭据可用' : '凭据缺失' }} · g{{ state.generation }}</p>
    <p v-if="ready && !available" role="status">当前宿主不支持本机凭据保护，仍可使用环境凭据。</p>
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
