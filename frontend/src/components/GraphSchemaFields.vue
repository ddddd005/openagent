<script setup lang="ts">
import { computed, ref } from "vue";
import { graphEnumLabel, graphObject } from "../domain/workflowGraph";
import RolePlacementFields from "./RolePlacementFields.vue";
import type { RolePlacement } from "../domain/promptPresentation";
import { useBoundFormDraft } from "../composables/useBoundFormDraft";
import FormDraftNotice from "./FormDraftNotice.vue";
const props = defineProps<{ schema: Record<string, unknown>; value: Record<string, unknown>; disabled?: boolean;
  draftScope?: string; draftBasis?: Record<string, unknown> }>();
const emit = defineEmits<{ change: [value: Record<string, unknown>] }>();
const raw = ref<Record<string, string>>({});
const localDraft = useBoundFormDraft({
  id: () => props.draftScope ?? "",
  basis: () => ({ ...props.draftBasis, schema: props.schema, value: props.value }),
  capture: () => raw.value,
  reset: () => { raw.value = {}; },
  restore: value => {
    if (!graphObject(value) || !Object.values(value).every(item => typeof item === "string"))
      throw new Error("字段草稿格式不兼容，原文仍保留");
    raw.value = value;
  },
});
const fields = computed(() => Object.entries(graphObject(props.schema.properties) ? props.schema.properties : {})
  .map(([key, definition]) => ({ key, definition: graphObject(definition) ? definition : {} }))
  .sort((a, b) => Number(["text", "content", "body"].includes(b.key)) - Number(["text", "content", "body"].includes(a.key))));
const locked = computed(() => props.disabled || localDraft.loading.value || localDraft.conflict.value);
function patch(key: string, value: unknown) {
  if (locked.value) return;
  const next = { ...props.value, [key]: value };
  if (!localDraft.adoptBasis({ ...props.draftBasis, schema: props.schema, value: next })) return;
  emit("change", next);
}
function remember(key: string, event: Event) { raw.value[key] = (event.target as HTMLInputElement).value; }
function text(key: string, json = false) {
  return raw.value[key] ?? (json ? JSON.stringify(props.value[key], null, 2) ?? "null" : String(props.value[key] ?? ""));
}
function numeric(key: string, event: Event) {
  const input = event.target as HTMLInputElement;
  if (!input.value && !(Array.isArray(props.schema.required) && props.schema.required.includes(key))) {
    const value = { ...props.value }; delete value[key];
    if (localDraft.adoptBasis({ ...props.draftBasis, schema: props.schema, value })) emit("change", value);
  } else if (input.value.trim() && Number.isFinite(Number(input.value))) {
    input.setCustomValidity(""); patch(key, Number(input.value));
  } else { input.setCustomValidity("请输入完整的数字"); input.reportValidity(); }
}
function json(key: string, event: Event) {
  const input = event.target as HTMLTextAreaElement;
  try { patch(key, JSON.parse(input.value)); input.setCustomValidity(""); }
  catch { input.setCustomValidity("请输入有效 JSON"); input.reportValidity(); }
}
function enums(definition: Record<string, unknown>): unknown[] {
  return Array.isArray(definition.enum) ? definition.enum : [];
}
function objectValue(value: unknown): Record<string, unknown> { return graphObject(value) ? value : {}; }
function presentation(value: unknown): value is RolePlacement {
  return graphObject(value) && ["system", "user", "assistant"].includes(String(value.role))
    && ["before", "middle", "after"].includes(String(value.placement))
    && typeof value.order === "number" && typeof value.enabled === "boolean"
    && (value.depth === null || typeof value.depth === "number");
}
</script>

<template>
  <div class="graph-schema-fields">
    <FormDraftNotice :message="localDraft.message.value" :conflict="localDraft.conflict.value"
      @restore="localDraft.restoreConflict" @discard="localDraft.discardConflict" />
    <template v-for="field in fields" :key="field.key">
      <fieldset v-if="field.definition.type === 'object' && presentation(value[field.key])" :disabled="locked">
        <RolePlacementFields :model-value="value[field.key] as RolePlacement" :label="String(field.definition.title ?? field.key)"
          :draft-scope="draftScope ? draftScope + ':' + field.key : undefined"
          :draft-basis="{ ...draftBasis, schema: field.definition }" @update:model-value="patch(field.key, $event)" />
      </fieldset>
      <fieldset v-else-if="field.definition.type === 'object' && field.definition.properties" :disabled="locked">
        <legend>{{ field.definition.title ?? field.key }}</legend>
        <GraphSchemaFields :schema="field.definition" :value="objectValue(value[field.key])" :disabled="disabled"
          :draft-scope="draftScope ? draftScope + ':' + field.key : undefined" :draft-basis="draftBasis" @change="patch(field.key, $event)" />
      </fieldset>
      <details v-else-if="['array', 'object'].includes(String(field.definition.type)) && !enums(field.definition).length">
        <summary>{{ field.definition.title ?? field.key }} · 高级 JSON</summary>
        <textarea :aria-label="`${field.key} 高级 JSON`" :value="text(field.key, true)" rows="4" :disabled="locked"
          @input="remember(field.key, $event)" @change="json(field.key, $event)" />
      </details>
      <label v-else>
        <span>{{ field.definition.title ?? field.key }}</span>
        <output v-if="field.definition.readOnly === true || field.definition.type === 'null'">{{ value[field.key] === null ? '未启用' : String(value[field.key] ?? '') }}</output>
        <select v-else-if="enums(field.definition).length" :value="JSON.stringify(value[field.key])" :disabled="locked" @change="patch(field.key, JSON.parse(($event.target as HTMLSelectElement).value))">
          <option v-for="option in enums(field.definition)" :key="JSON.stringify(option)" :value="JSON.stringify(option)">{{ graphEnumLabel(field.key, field.definition, option) }}</option>
        </select>
        <input v-else-if="field.definition.type === 'boolean'" type="checkbox" :checked="!!value[field.key]" :disabled="locked" @change="patch(field.key, ($event.target as HTMLInputElement).checked)" />
        <input v-else-if="['number', 'integer'].includes(String(field.definition.type))" type="text" inputmode="decimal" :value="text(field.key)" :disabled="disabled || localDraft.loading.value || localDraft.conflict.value" @input="remember(field.key, $event)" @change="numeric(field.key, $event)" />
        <textarea v-else-if="field.definition.type === 'string'" :value="text(field.key)" rows="3" :disabled="disabled || localDraft.loading.value || localDraft.conflict.value" @input="remember(field.key, $event)" @change="patch(field.key, ($event.target as HTMLTextAreaElement).value)" />
        <textarea v-else :value="text(field.key, true)" rows="4" :disabled="disabled || localDraft.loading.value || localDraft.conflict.value" @input="remember(field.key, $event)" @change="json(field.key, $event)" />
      </label>
    </template>
  </div>
</template>

<style scoped>
.graph-schema-fields { display:grid; gap:11px; min-width:0; }
label { display:grid; gap:5px; min-width:0; font-size:12px; color:#c9c9cf; }
input,textarea,select { box-sizing:border-box; width:100%; min-width:0; border:1px solid #515157; background:#222226; color:#e0e0e5; padding:6px; border-radius:3px; font:inherit; }
input[type=checkbox] { width:16px; height:16px; accent-color:#77bca0; }
textarea { resize:vertical; }
fieldset { min-width:0; margin:0; padding:10px; border:1px solid #44444b; }
legend { font-size:12px; padding:0 4px; }
:disabled { opacity:.55; }
details { min-width:0; font-size:12px; } summary { color:#b4beb9; cursor:pointer; margin-bottom:5px; overflow-wrap:anywhere; }
</style>
