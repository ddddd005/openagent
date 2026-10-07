<script setup lang="ts">
import type { RolePlacement } from "../domain/promptPresentation";

const props = defineProps<{
  modelValue: RolePlacement;
  label?: string;
  errors?: string[];
}>();
const emit = defineEmits<{
  "update:modelValue": [value: RolePlacement];
}>();

function update(patch: Partial<RolePlacement>) {
  emit("update:modelValue", { ...props.modelValue, ...patch });
}

function changePlacement(placement: RolePlacement["placement"]) {
  update({
    placement,
    depth: placement === "middle" ? (props.modelValue.depth ?? 0) : null,
  });
}
</script>

<template>
  <fieldset class="presentation-fields nodrag nowheel">
    <legend v-if="label">{{ label }}</legend>
    <div class="presentation-role-row">
      <label>
        <span>Role</span>
        <select
          :value="modelValue.role"
          aria-label="消息 Role"
          @change="update({ role: ($event.target as HTMLSelectElement).value as RolePlacement['role'] })"
        >
          <option value="system">system</option>
          <option value="user">user</option>
          <option value="assistant">assistant</option>
        </select>
      </label>
      <label class="presentation-enabled">
        <input
          type="checkbox"
          :checked="modelValue.enabled"
          @change="update({ enabled: ($event.target as HTMLInputElement).checked })"
        />
        <span>启用</span>
      </label>
    </div>
    <div class="presentation-position">
      <span>位置</span>
      <div class="presentation-segmented" role="group" aria-label="装配位置">
        <button
          v-for="option in ([['before', '上下文前'], ['middle', '上下文中'], ['after', '上下文后']] as const)"
          :key="option[0]"
          type="button"
          :class="{ active: modelValue.placement === option[0] }"
          :aria-pressed="modelValue.placement === option[0]"
          @click="changePlacement(option[0])"
        >{{ option[1] }}</button>
      </div>
    </div>
    <div class="presentation-number-row">
      <label v-if="modelValue.placement === 'middle'">
        <span>深度</span>
        <input
          type="number"
          min="0"
          step="1"
          :value="modelValue.depth"
          aria-label="中区深度"
          @input="update({ depth: ($event.target as HTMLInputElement).valueAsNumber })"
        />
      </label>
      <label>
        <span>顺序</span>
        <input
          type="number"
          min="0"
          step="1"
          :value="modelValue.order"
          aria-label="装配顺序"
          @input="update({ order: ($event.target as HTMLInputElement).valueAsNumber })"
        />
      </label>
    </div>
    <p v-for="error in errors" :key="error" class="presentation-error">{{ error }}</p>
  </fieldset>
</template>

<style scoped>
.presentation-fields {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  padding: 0;
  margin: 0;
  border: 0;
}
.presentation-fields legend {
  margin-bottom: 10px;
  padding: 0;
  color: #d7d9dc;
  font-size: 12px;
  font-weight: 600;
}
.presentation-fields label,
.presentation-position {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 5px;
  color: #a6a9b0;
  font-size: 11px;
}
.presentation-role-row,
.presentation-number-row {
  display: flex;
  gap: 10px;
}
.presentation-role-row > label:first-child,
.presentation-number-row > label {
  flex: 1;
}
.presentation-fields .presentation-enabled {
  flex-direction: row;
  align-items: center;
  align-self: flex-end;
  gap: 6px;
  height: 30px;
  flex: 0 0 60px;
}
.presentation-enabled input {
  width: 14px;
  height: 14px;
  padding: 0;
  margin: 0;
  accent-color: #8cc9b3;
}
.presentation-fields input[type="number"],
.presentation-fields select {
  height: 30px;
  padding: 3px 7px;
  font-size: 12px;
  border-color: #444447;
  background: #202023;
}
.presentation-segmented {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  width: 100%;
  height: 30px;
  border: 1px solid #454548;
  border-radius: 4px;
  overflow: hidden;
}
.presentation-segmented button {
  padding: 0 2px;
  color: #aaaeb5;
  font-size: 10px;
  white-space: nowrap;
}
.presentation-segmented button + button {
  border-left: 1px solid #454548;
}
.presentation-segmented button.active {
  color: #c2dfd2;
  background: #35453d;
}
.presentation-error {
  color: #f1a2a2;
  font-size: 11px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
</style>
