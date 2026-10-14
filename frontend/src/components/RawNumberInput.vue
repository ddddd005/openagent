<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { parseRawNumber } from "../domain/rawNumber";

defineOptions({ inheritAttrs: false });
const props = withDefaults(defineProps<{
  modelValue?: number | null;
  rawValue?: string;
  min?: number;
  max?: number;
  integer?: boolean;
  required?: boolean;
  disabled?: boolean;
  identity?: string;
}>(), { required: true });
const emit = defineEmits<{
  "update:modelValue": [value: number];
  "update:rawValue": [value: string];
  validity: [value: boolean];
}>();
const input = ref<HTMLInputElement | null>(null);
const initialValue = () => props.rawValue ?? (props.modelValue === null || props.modelValue === undefined ? "" : String(props.modelValue));
const raw = ref(initialValue());
watch(() => props.identity, () => { raw.value = initialValue(); }, { flush: "sync" });
const text = computed(() => props.rawValue ?? raw.value);
const parsed = computed(() => parseRawNumber(text.value, props));
const valid = computed(() => !!props.disabled || parsed.value.valid);
const validationMessage = computed(() => props.disabled ? "" : parsed.value.error);

function publishNumber() {
  const result = parseRawNumber(raw.value, props);
  if (result.valid && result.value !== null) emit("update:modelValue", result.value);
}
function edit(event: Event) {
  if (props.disabled) return;
  const value = (event.target as HTMLInputElement).value;
  raw.value = value;
  if (props.rawValue !== undefined) emit("update:rawValue", value);
  else publishNumber();
}
watch(() => props.modelValue, value => {
  if (props.rawValue !== undefined) return;
  const current = parseRawNumber(raw.value, props);
  if (!current.valid) return;
  if (current.valid && current.value === value) return;
  raw.value = value === null || value === undefined ? "" : String(value);
});
watch([valid, validationMessage], () => {
  emit("validity", valid.value);
  void nextTick(() => input.value?.setCustomValidity?.(valid.value ? "" : validationMessage.value || "数字字段尚未安全载入"));
}, { immediate: true, flush: "sync" });
defineExpose({ valid });
</script>

<template>
  <input ref="input" v-bind="$attrs" type="text" :inputmode="integer ? 'numeric' : 'decimal'"
    :value="text" :disabled="disabled" :required="required"
    :aria-invalid="!valid || undefined" :title="validationMessage || undefined" @input="edit" />
</template>

<style scoped>
input { box-sizing:border-box; width:100%; min-width:0; height:30px; padding:6px; color:#dedee4;
  background:#222226; border:1px solid #505059; border-radius:3px; font:inherit; }
input[aria-invalid="true"] { border-color:#c98282; }
input:disabled { opacity:.55; }
</style>
