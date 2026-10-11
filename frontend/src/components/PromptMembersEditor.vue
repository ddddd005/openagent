<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-vue-next";
import RawNumberInput from "./RawNumberInput.vue";
import { movePromptMember, newPromptMember, type PromptMember, type PromptPresentation } from "../domain/workflowPromptResources";
const props = withDefaults(defineProps<{ members: PromptMember[]; version: 1 | 2; disabled?: boolean;
  draftScope: string; draftBasis: Record<string, unknown>; persistNumericDrafts?: boolean }>(), { persistNumericDrafts: true });
const emit = defineEmits<{ validity: [valid: boolean] }>();
const invalid = ref<Record<string, boolean>>({}), error = ref("");
const numbers = new Map<string, { adoptBasis(value: unknown): boolean }>();
function numberRef(key: string, value: unknown) {
  if (value && typeof (value as { adoptBasis?: unknown }).adoptBasis === "function") numbers.set(key, value as { adoptBasis(value: unknown): boolean });
  else numbers.delete(key);
}
function adoptBasis(value: unknown) {
  let adopted = true;
  for (const number of numbers.values()) if (!number.adoptBasis(value)) adopted = false;
  return adopted;
}
defineExpose({ adoptBasis });
const scope = (member: PromptMember, field: string) => `${props.draftScope}:member:${member.id}:${field}`;
const valid = computed(() => props.members.every(member => invalid.value[scope(member, "order")] !== true
  && (member.presentation.placement !== "middle" || invalid.value[scope(member, "depth")] !== true)));
watch(valid, value => emit("validity", value), { immediate: true });
function add() {
  if (props.disabled || props.members.length >= 1024 || !valid.value) return;
  const order = props.members.length ? Math.max(...props.members.map(row => row.presentation.order)) + 1 : 0;
  if (!Number.isSafeInteger(order)) { error.value = "条目顺序已达上限，先调整顺序"; return; }
  const member = newPromptMember("", props.version); member.presentation.order = order;
  props.members.push(member); error.value = "";
}
function move(index: number, offset: number) {
  if (!props.disabled && valid.value) movePromptMember(props.members, index, offset);
}
function placement(member: PromptMember, value: PromptPresentation["placement"]) {
  if (props.disabled) return;
  member.presentation.placement = value;
  member.presentation.depth = value === "middle" ? member.presentation.depth ?? 0 : null;
}
function protect(member: PromptMember) {
  if (props.disabled) return;
  if (props.version === 2 && (member.lifecycle === "per_request" || member.presentation.role === "system")) member.compaction = "never";
}
</script>
<template>
  <fieldset class="prompt-members" :disabled="disabled">
    <section v-for="(member, index) in members" :key="member.id" class="prompt-member">
      <header><strong>条目 {{ index + 1 }}</strong>
        <button type="button" title="上移条目" :aria-label="`条目 ${index + 1} 上移`" :disabled="disabled || !valid || index === 0" @click="move(index, -1)"><ArrowUp :size="15" /></button>
        <button type="button" title="下移条目" :aria-label="`条目 ${index + 1} 下移`" :disabled="disabled || !valid || index === members.length - 1" @click="move(index, 1)"><ArrowDown :size="15" /></button>
        <button type="button" title="移除条目" aria-label="移除条目" @click="!disabled && members.splice(index, 1)"><Trash2 :size="15" /></button>
      </header>
      <label>正文<textarea v-model="member.text" rows="5" :aria-label="`条目 ${index + 1} 正文`" /></label>
      <div class="row"><label>角色<select v-model="member.presentation.role" :aria-label="`条目 ${index + 1} Role`" @change="protect(member)"><option value="system">system</option><option value="user">user</option><option value="assistant">assistant</option></select></label>
        <label class="check"><input v-model="member.presentation.enabled" type="checkbox" />启用条目</label></div>
      <div v-if="version === 2" class="row"><label>进入上下文<select v-model="member.lifecycle" :aria-label="`条目 ${index + 1} 生命周期`" @change="protect(member)"><option value="per_request">每次装配</option><option value="context_once">仅一次</option></select></label>
        <label>精简许可<select v-model="member.compaction" :aria-label="`条目 ${index + 1} 精简许可`" :disabled="member.lifecycle !== 'context_once' || member.presentation.role === 'system'"><option value="never">保留原文</option><option value="allowed">允许精简</option></select></label></div>
      <label>位置<div class="segments" role="group" :aria-label="`条目 ${index + 1} 装配位置`"><button v-for="option in ([['before','上下文前'],['middle','上下文中'],['after','上下文后']] as const)" :key="option[0]" type="button" :aria-pressed="member.presentation.placement === option[0]" @click="placement(member, option[0])">{{ option[1] }}</button></div></label>
      <div class="row"><label v-if="member.presentation.placement === 'middle'">深度<RawNumberInput v-model="member.presentation.depth" :min="0" integer :disabled="disabled" :persist="persistNumericDrafts" :ref="value => numberRef(scope(member,'depth'), value)"
        :draft-scope="scope(member, 'depth')" :draft-basis="draftBasis" :aria-label="`条目 ${index + 1} 中区深度`" @validity="invalid[scope(member,'depth')] = !$event" /></label>
        <label>顺序<RawNumberInput v-model="member.presentation.order" integer :disabled="disabled" :persist="persistNumericDrafts" :ref="value => numberRef(scope(member,'order'), value)"
          :draft-scope="scope(member,'order')" :draft-basis="draftBasis" :aria-label="`条目 ${index + 1} 装配顺序`" @validity="invalid[scope(member,'order')] = !$event" /></label></div>
      <details><summary>条目技术详情</summary><code>{{ member.id }}</code><pre>{{ JSON.stringify(member.metadata, null, 2) }}</pre></details>
    </section>
    <p v-if="error" role="status">{{ error }}</p>
    <button type="button" :disabled="disabled || !valid || members.length >= 1024" @click="add"><Plus :size="14" />新增条目</button>
  </fieldset>
</template>
<style scoped>
.prompt-members { display:grid; gap:12px; border:0; margin:0; padding:0; min-width:0; font-size:12px; }
.prompt-member { display:grid; gap:10px; border-top:1px solid #505059; padding-top:10px; min-width:0; }
header,.row { display:flex; align-items:center; gap:6px; min-width:0; }
header strong,.row>label { flex:1; min-width:0; }
button { display:inline-flex; align-items:center; justify-content:center; gap:5px; padding:6px; border-radius:3px; min-height:28px; }
header button { width:28px; flex:0 0 28px; }
label { display:grid; gap:5px; min-width:0; }
textarea,select { width:100%; min-width:0; box-sizing:border-box; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; font:inherit; line-height:1.6; }
textarea { resize:vertical; } .check { display:flex; align-items:center; gap:5px; }
.row>.check { flex:0 0 auto; white-space:nowrap; }
.segments { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); border:1px solid #505059; border-radius:4px; }
.segments button { padding:4px 1px; min-width:0; font-size:10px; }
.segments [aria-pressed=true] { background:#35453d; color:#c2dfd2; }
details,code,pre { font-size:10px; color:#aab6b1; overflow-wrap:anywhere; }
pre { white-space:pre-wrap; } :disabled { opacity:.55; }
</style>
