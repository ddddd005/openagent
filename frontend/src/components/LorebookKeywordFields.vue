<script setup lang="ts">
import { Plus, Trash2 } from "lucide-vue-next";
import { lorebookLimits } from "../domain/lorebook";
const props = defineProps<{ modelValue: string[]; label: string; labelPrefix: string; disabled?: boolean }>();
const emit = defineEmits<{ "update:modelValue": [value: string[]] }>();
function replace(index: number, text: string) {
  if (props.disabled) return;
  emit("update:modelValue", props.modelValue.map((value, at) => at === index ? text : value));
}
function add() {
  if (!props.disabled && props.modelValue.length < lorebookLimits.keywords)
    emit("update:modelValue", [...props.modelValue, ""]);
}
function remove(index: number) {
  if (!props.disabled) emit("update:modelValue", props.modelValue.filter((_value, at) => at !== index));
}
</script>
<template>
  <section class="lorebook-keywords" :aria-label="`${labelPrefix} ${label}列表`">
    <header>
      <span>{{ label }}</span>
      <button type="button" :title="`新增${label}`" :aria-label="`${labelPrefix} 新增${label}`"
        :disabled="disabled || modelValue.length >= lorebookLimits.keywords" @click="add"><Plus :size="14" /></button>
    </header>
    <div v-for="(keyword, index) in modelValue" :key="index" class="keyword-row">
      <input :value="keyword" type="text" :aria-label="`${labelPrefix} ${label} ${index + 1}`"
        :disabled="disabled" @input="replace(index, ($event.target as HTMLInputElement).value)" />
      <button type="button" :title="`移除${label}`" :aria-label="`${labelPrefix} 移除${label} ${index + 1}`"
        :disabled="disabled" @click="remove(index)"><Trash2 :size="14" /></button>
    </div>
    <span v-if="!modelValue.length" class="empty">无关键词</span>
  </section>
</template>
<style scoped>
.lorebook-keywords { display:grid; gap:5px; min-width:0; }
header,.keyword-row { display:flex; align-items:center; gap:5px; min-width:0; }
header > span { flex:1; font-size:11px; color:#b7bdc0; }
input { flex:1; width:100%; min-width:0; height:28px; box-sizing:border-box; padding:4px 6px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; font:inherit; font-size:12px; }
button { display:grid; place-items:center; flex:0 0 26px; width:26px; height:26px; padding:3px; border:1px solid #505059; border-radius:3px; }
.empty { color:#8f949a; font-size:11px; line-height:24px; }
:disabled { opacity:.55; }
</style>
