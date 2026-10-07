<script setup lang="ts">
import { computed } from "vue";
import { graphEnumLabel, graphObject } from "../domain/workflowGraph";
import RolePlacementFields from "./RolePlacementFields.vue";
import type { RolePlacement } from "../domain/promptPresentation";
const props = defineProps<{ schema: Record<string, unknown>; value: Record<string, unknown>; disabled?: boolean }>();
const emit = defineEmits<{ change: [value: Record<string, unknown>] }>();
const fields = computed(() => Object.entries(graphObject(props.schema.properties) ? props.schema.properties : {})
  .map(([key, definition]) => ({ key, definition: graphObject(definition) ? definition : {} })));
function patch(key: string, value: unknown) { emit("change", { ...props.value, [key]: value }); }
function numeric(key: string, event: Event) {
  const input = event.target as HTMLInputElement;
  if (!input.value && !(Array.isArray(props.schema.required) && props.schema.required.includes(key))) {
    const value = { ...props.value }; delete value[key]; emit("change", value);
  } else if (Number.isFinite(input.valueAsNumber)) patch(key, input.valueAsNumber);
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
    <template v-for="field in fields" :key="field.key">
      <fieldset v-if="field.definition.type === 'object' && presentation(value[field.key])" :disabled="disabled">
        <RolePlacementFields :model-value="value[field.key] as RolePlacement" :label="String(field.definition.title ?? field.key)" @update:model-value="patch(field.key, $event)" />
      </fieldset>
      <fieldset v-else-if="field.definition.type === 'object' && field.definition.properties">
        <legend>{{ field.definition.title ?? field.key }}</legend>
        <GraphSchemaFields :schema="field.definition" :value="objectValue(value[field.key])" :disabled="disabled" @change="patch(field.key, $event)" />
      </fieldset>
      <label v-else>
        <span>{{ field.definition.title ?? field.key }}</span>
        <select v-if="enums(field.definition).length" :value="JSON.stringify(value[field.key])" :disabled="disabled" @change="patch(field.key, JSON.parse(($event.target as HTMLSelectElement).value))">
          <option v-for="option in enums(field.definition)" :key="JSON.stringify(option)" :value="JSON.stringify(option)">{{ graphEnumLabel(field.key, field.definition, option) }}</option>
        </select>
        <input v-else-if="field.definition.type === 'boolean'" type="checkbox" :checked="!!value[field.key]" :disabled="disabled" @change="patch(field.key, ($event.target as HTMLInputElement).checked)" />
        <input v-else-if="['number', 'integer'].includes(String(field.definition.type))" type="number" :value="value[field.key]" :min="typeof field.definition.minimum === 'number' ? field.definition.minimum : undefined" :max="typeof field.definition.maximum === 'number' ? field.definition.maximum : undefined" :step="field.definition.type === 'integer' ? 1 : 'any'" :disabled="disabled" @change="numeric(field.key, $event)" />
        <textarea v-else-if="field.definition.type === 'string'" :value="String(value[field.key] ?? '')" rows="3" :disabled="disabled" @change="patch(field.key, ($event.target as HTMLTextAreaElement).value)" />
        <textarea v-else :value="JSON.stringify(value[field.key], null, 2) ?? 'null'" rows="4" :disabled="disabled" @change="json(field.key, $event)" />
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
</style>
