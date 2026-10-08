<script setup lang="ts">
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-vue-next";
import RolePlacementFields from "../../components/RolePlacementFields.vue";
import LorebookKeywordFields from "../../components/LorebookKeywordFields.vue";
import { graphClone } from "../../domain/workflowGraph";
import { lorebookLimits, moveLorebookEntry, newLorebookEntry, type LorebookEntry } from "../../domain/lorebook";

const props = defineProps<{ modelValue: LorebookEntry[]; disabled?: boolean }>();
const emit = defineEmits<{ "update:modelValue": [value: LorebookEntry[]] }>();
function patch(index: number, value: Partial<LorebookEntry>) {
  if (props.disabled || !props.modelValue[index]) return;
  const entries = graphClone(props.modelValue);
  Object.assign(entries[index]!, value);
  emit("update:modelValue", entries);
}
function add() {
  if (props.disabled || props.modelValue.length >= lorebookLimits.entries) return;
  emit("update:modelValue", [...graphClone(props.modelValue), newLorebookEntry(props.modelValue.length)]);
}
function remove(index: number) {
  if (props.disabled) return;
  emit("update:modelValue", graphClone(props.modelValue.filter((_, at) => at !== index)));
}
function move(index: number, offset: number) {
  if (props.disabled) return;
  const entries = graphClone(props.modelValue);
  if (moveLorebookEntry(entries, index, offset)) emit("update:modelValue", entries);
}
</script>

<template>
  <fieldset class="lorebook-entries" :disabled="disabled">
    <section v-for="(entry, index) in modelValue" :key="entry.id" class="lorebook-entry">
      <header>
        <strong>{{ entry.name || `条目 ${index + 1}` }}</strong>
        <button type="button" title="上移条目" :aria-label="`条目 ${index + 1} 上移`"
          :disabled="disabled || index === 0" @click="move(index, -1)"><ArrowUp :size="14" /></button>
        <button type="button" title="下移条目" :aria-label="`条目 ${index + 1} 下移`"
          :disabled="disabled || index === modelValue.length - 1" @click="move(index, 1)"><ArrowDown :size="14" /></button>
        <button type="button" title="移除条目" :aria-label="`条目 ${index + 1} 移除`"
          :disabled="disabled" @click="remove(index)"><Trash2 :size="14" /></button>
      </header>
      <small :title="entry.id">{{ entry.id }}</small>
      <label>名称<input :value="entry.name" type="text" :aria-label="`条目 ${index + 1} 名称`"
        @input="patch(index, { name: ($event.target as HTMLInputElement).value })" /></label>
      <label>正文<textarea :value="entry.text" rows="4" :aria-label="`条目 ${index + 1} 正文`"
        @input="patch(index, { text: ($event.target as HTMLTextAreaElement).value })" /></label>
      <div class="field-row">
        <label>模式<select :value="entry.mode" :aria-label="`条目 ${index + 1} 模式`"
          @change="patch(index, { mode: ($event.target as HTMLSelectElement).value as LorebookEntry['mode'] })">
          <option value="keyword">关键词触发</option><option value="constant">常驻</option>
        </select></label>
        <label>扫描深度<input :value="entry.scan_depth" type="number" min="0" :max="lorebookLimits.scanDepth"
          step="1" :aria-label="`条目 ${index + 1} 扫描深度`"
          @input="patch(index, { scan_depth: ($event.target as HTMLInputElement).valueAsNumber })" /></label>
      </div>
      <template v-if="entry.mode === 'keyword'">
        <LorebookKeywordFields :model-value="entry.primary_keywords" label="主关键词" :label-prefix="`条目 ${index + 1}`"
          :disabled="disabled" @update:model-value="patch(index, { primary_keywords: $event })" />
        <label>组合规则<select :value="entry.keyword_rule" :aria-label="`条目 ${index + 1} 组合规则`"
          @change="patch(index, { keyword_rule: ($event.target as HTMLSelectElement).value as LorebookEntry['keyword_rule'] })">
          <option value="primary_or_secondary">主 OR 副</option><option value="primary_and_secondary">主 AND 副</option>
          <option value="primary_and_not_secondary">主 AND NOT 副</option>
        </select></label>
        <LorebookKeywordFields :model-value="entry.secondary_keywords" label="副关键词" :label-prefix="`条目 ${index + 1}`"
          :disabled="disabled" @update:model-value="patch(index, { secondary_keywords: $event })" />
        <div class="toggles">
          <label><input :checked="entry.case_sensitive" type="checkbox" :aria-label="`条目 ${index + 1} 区分大小写`"
            @change="patch(index, { case_sensitive: ($event.target as HTMLInputElement).checked })" />区分大小写</label>
          <label><input :checked="entry.recursive" type="checkbox" :aria-label="`条目 ${index + 1} 递归触发`"
            @change="patch(index, { recursive: ($event.target as HTMLInputElement).checked })" />递归触发</label>
        </div>
      </template>
      <div class="field-row">
        <label class="toggle"><input :checked="entry.probability_enabled" type="checkbox" :aria-label="`条目 ${index + 1} 概率开关`"
          @change="patch(index, { probability_enabled: ($event.target as HTMLInputElement).checked })" />概率开启</label>
        <label>概率 %<input :value="entry.probability" type="number" min="0" max="100" step="any"
          :disabled="disabled || !entry.probability_enabled" :aria-label="`条目 ${index + 1} 概率值`"
          @input="patch(index, { probability: ($event.target as HTMLInputElement).valueAsNumber })" /></label>
      </div>
      <RolePlacementFields :model-value="entry.presentation" label="注入呈现" depth-label="插入深度"
        :label-prefix="`条目 ${index + 1}`" @update:model-value="patch(index, { presentation: $event })" />
    </section>
    <button type="button" :disabled="disabled || modelValue.length >= lorebookLimits.entries" @click="add">
      <Plus :size="14" />新增条目
    </button>
  </fieldset>
</template>

<style scoped>
.lorebook-entries { display:grid; gap:12px; min-width:0; margin:0; padding:0; border:0; font-size:12px; }
.lorebook-entry { display:grid; gap:10px; min-width:0; border-bottom:1px solid #505059; padding-bottom:12px; }
header { display:flex; align-items:center; gap:4px; min-width:0; }
strong { flex:1; min-width:0; font-weight:500; overflow-wrap:anywhere; }
button { display:flex; align-items:center; justify-content:center; gap:5px; padding:6px; border:1px solid #64776d; border-radius:3px; font:inherit; }
header button { flex:0 0 26px; width:26px; height:26px; padding:3px; border-color:#505059; }
small { font:10px/1.5 Consolas,monospace; color:#949ba1; overflow-wrap:anywhere; }
label { display:grid; gap:5px; min-width:0; color:#bec3c7; }
input[type="text"],input[type="number"],select,textarea { box-sizing:border-box; width:100%; min-width:0; padding:6px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; font:inherit; }
input[type="text"],input[type="number"],select { height:30px; }
textarea { resize:vertical; line-height:1.6; }
.field-row { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; align-items:end; min-width:0; }
.toggles { display:flex; gap:12px; flex-wrap:wrap; }
.toggle,.toggles label { display:flex; align-items:center; gap:6px; min-height:30px; }
input[type="checkbox"] { width:14px; height:14px; flex:0 0 14px; margin:0; accent-color:#8cc9b3; }
:disabled { opacity:.55; }
</style>
