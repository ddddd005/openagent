<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { Archive, RefreshCw, Save } from "lucide-vue-next";
import { useContentMetadata } from "../application/contentMetadata";
import type { PromptIdentity } from "../domain/workflowPromptResources";

const props = defineProps<{ reference: PromptIdentity; disabled?: boolean; fallback?: string }>();
const metadata = useContentMetadata(), text = ref("");
const entry = computed(() => metadata.forReference(props.reference));
let preservingInput: typeof entry.value | null = null;
const locked = computed(() => props.disabled || entry.value.locked.value || entry.value.loading.value
  || !!entry.value.readError.value || !entry.value.record.value);
watch(entry, value => { text.value = ""; void value.load(); }, { immediate: true });
watch(() => entry.value.record.value, value => {
  if (value && preservingInput !== entry.value) text.value = value.display_name;
}, { immediate: true });
async function save() {
  if (locked.value) return;
  const target = entry.value;
  await target.save(text.value);
}
async function abandon() {
  const target = entry.value;
  await target.abandon(window.confirm("归档并解除原名称请求的锁定？这不会撤销已发生的保存，也不会重新提交。"));
}
async function refresh() {
  const target = entry.value;
  if (target.busy.value || target.pending.value || target.loading.value) return;
  preservingInput = target.record.value ? target : null;
  try { await target.refresh(); await nextTick(); }
  finally { if (preservingInput === target) preservingInput = null; }
}
</script>
<template>
  <section class="content-metadata-fields" aria-label="提示词正式名称">
    <label>显示名称<input v-model="text" aria-label="提示词显示名称" :placeholder="fallback ?? ''"
      :disabled="locked" @blur="save" /></label>
    <button type="button" title="保存显示名称" aria-label="保存提示词显示名称" :disabled="locked" @click="save"><Save :size="14" /></button>
    <button type="button" title="刷新显示名称" aria-label="刷新提示词名称"
      :disabled="entry.loading.value || entry.busy.value || !!entry.pending.value" @click="refresh"><RefreshCw :size="14" /></button>
    <p v-if="entry.readError.value" role="status">{{ entry.readError.value }}</p>
    <div v-if="entry.pending.value" role="status">
      <span>名称提交结果待核实</span>
      <button type="button" aria-label="核实原提示词名称请求" :disabled="entry.busy.value" @click="entry.reconcile()"><RefreshCw :size="14" /></button>
      <button type="button" aria-label="归档原提示词名称请求" :disabled="entry.busy.value" @click="abandon"><Archive :size="14" /></button>
    </div>
    <p v-if="entry.error.value" role="status">{{ entry.error.value }}</p>
  </section>
</template>
<style scoped>
section { display:grid; grid-template-columns:minmax(0,1fr) 28px 28px; gap:6px; align-items:end; min-width:0; font-size:11px; }
label { display:grid; gap:5px; min-width:0; }
input { width:100%; min-width:0; box-sizing:border-box; padding:6px; border:1px solid #505059; border-radius:3px; background:#222226; color:#dedee4; font:inherit; }
button { display:grid; place-items:center; width:28px; height:28px; border:1px solid #505059; border-radius:3px; }
p, div { grid-column:1/-1; margin:0; color:#e4a0a0; overflow-wrap:anywhere; }
div { display:flex; align-items:center; gap:6px; }
:disabled { opacity:.55; }
</style>
