<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ArrowDown, ArrowUp, Plus, Save, Trash2 } from "lucide-vue-next";
import RolePlacementFields from "./RolePlacementFields.vue";
import LorebookKeywordFields from "./LorebookKeywordFields.vue";
import type { RolePlacement } from "../domain/promptPresentation";
import { graphClone, type GraphDocument, type GraphNode } from "../domain/workflowGraph";
import { isLorebookConfig, lorebookBindingErrors, lorebookLimits, lorebookVariableBindings,
  moveLorebookEntry, newLorebookEntry, type LorebookConfig, type LorebookEntry } from "../domain/lorebook";
import { tavernExtensionsFor } from "../plugins/tavernFrontendManifest";
import { useWorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";

const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk();
const draft = ref<LorebookConfig | null>(null), basis = ref<WorkflowNodeConfigurationRequest | null>(null);
const status = ref(""), error = ref("");
const group = computed(() => props.node.component_id === "lorebook.group");
const entries = computed(() => !draft.value ? [] : "entries" in draft.value ? draft.value.entries : [draft.value.entry]);
const disabled = computed(() => !!props.disabled || sdk.locked.value || !draft.value || !basis.value);
const bindings = computed(() => lorebookVariableBindings(props.document, props.node.node_binding_id));
const missingBindings = computed(() => draft.value?.object_keys.filter(key =>
  !bindings.value.some(binding => binding.object_key === key)) ?? []);
const bindingErrors = computed(() => lorebookBindingErrors(props.document, props.node.node_binding_id, draft.value?.object_keys ?? []));
watch(() => [sdk.workflowId.value, sdk.lifecycle.value, sdk.packages.value, props.node.node_binding_id,
  props.node.component_id, props.node.component_version, props.node.config], () => {
  error.value = ""; status.value = "";
  const declarations = tavernExtensionsFor(sdk.packages.value);
  const extension = declarations.find(row => row.component_id === props.node.component_id
    && row.component_version === props.node.component_version);
  draft.value = extension && isLorebookConfig(props.node.config, props.node.component_id)
    ? graphClone(props.node.config) : null;
  basis.value = extension ? {
    workflowId: sdk.workflowId.value, nodeId: props.node.node_binding_id,
    componentId: props.node.component_id, componentVersion: props.node.component_version,
    expectedConfig: graphClone(props.node.config), patch: {}, extensionId: extension.extension_id,
    lifecycle: sdk.lifecycle.value,
  } : null;
}, { immediate: true, deep: true });
function patchEntry(index: number, patch: Partial<LorebookEntry>) {
  if (disabled.value || !entries.value[index]) return;
  Object.assign(entries.value[index]!, patch);
  error.value = ""; status.value = "";
}
function presentation(index: number, value: RolePlacement) { patchEntry(index, { presentation: value }); }
function add() {
  if (disabled.value || !draft.value || !("entries" in draft.value)
    || draft.value.entries.length >= lorebookLimits.entries) return;
  const order = draft.value.entries.length;
  draft.value.entries.push(newLorebookEntry(order));
  status.value = ""; error.value = "";
}
function remove(index: number) {
  if (disabled.value || !draft.value || !("entries" in draft.value)) return;
  draft.value.entries.splice(index, 1); status.value = ""; error.value = "";
}
function move(index: number, offset: number) {
  if (disabled.value || !draft.value || !("entries" in draft.value)) return;
  if (!moveLorebookEntry(draft.value.entries, index, offset)) return;
  status.value = ""; error.value = "";
}
function binding(key: string, checked: boolean) {
  if (disabled.value || !draft.value) return;
  draft.value.object_keys = checked ? [...draft.value.object_keys, key] : draft.value.object_keys.filter(value => value !== key);
  status.value = ""; error.value = "";
}
function apply() {
  if (disabled.value || !draft.value || !basis.value) { error.value = "Lorebook 配置无效或当前不可编辑"; return; }
  if (!isLorebookConfig(draft.value, props.node.component_id)) {
    error.value = "检查条目身份、关键词数量、扫描深度、概率和呈现字段；尚未应用"; return;
  }
  if (bindingErrors.value.length) { error.value = bindingErrors.value.join("；"); return; }
  const patch = "entry" in draft.value ? { entry: graphClone(draft.value.entry), object_keys: [...draft.value.object_keys] }
    : { entries: graphClone(draft.value.entries), object_keys: [...draft.value.object_keys] };
  const result = sdk.configureNode({ ...graphClone(basis.value), patch });
  status.value = result ? result.workflowId === basis.value.workflowId ? "Lorebook 配置已更新" : "Lorebook 配置已写入新副本"
    : "配置依据已变化或当前不可编辑，请重新选择节点";
}
</script>
<template>
  <form class="lorebook-fields" aria-label="Lorebook 配置" @submit.prevent="apply">
    <p v-if="!draft" class="warning" role="status">Lorebook 配置无效，原配置保持不变</p>
    <fieldset :disabled="disabled">
      <section v-for="(entry, index) in entries" :key="entry.id" class="lorebook-entry" :aria-label="`Lorebook 条目 ${index + 1}`">
        <header>
          <strong>{{ entry.name || `条目 ${index + 1}` }}</strong>
          <template v-if="group">
            <button type="button" title="上移条目" :aria-label="`条目 ${index + 1} 上移`" :disabled="disabled || index === 0" @click="move(index, -1)"><ArrowUp :size="14" /></button>
            <button type="button" title="下移条目" :aria-label="`条目 ${index + 1} 下移`" :disabled="disabled || index === entries.length - 1" @click="move(index, 1)"><ArrowDown :size="14" /></button>
            <button type="button" title="移除条目" :aria-label="`条目 ${index + 1} 移除`" :disabled="disabled" @click="remove(index)"><Trash2 :size="14" /></button>
          </template>
        </header>
        <small :title="entry.id" class="entry-id">{{ entry.id }}</small>
        <label>名称<input :value="entry.name" type="text" :aria-label="`条目 ${index + 1} 名称`" @input="patchEntry(index, { name: ($event.target as HTMLInputElement).value })" /></label>
        <label>正文<textarea :value="entry.text" rows="4" :aria-label="`条目 ${index + 1} 正文`" @input="patchEntry(index, { text: ($event.target as HTMLTextAreaElement).value })" /></label>
        <div class="field-row">
          <label>模式<select :value="entry.mode" :aria-label="`条目 ${index + 1} 模式`" @change="patchEntry(index, { mode: ($event.target as HTMLSelectElement).value as LorebookEntry['mode'] })">
            <option value="keyword">关键词触发</option><option value="constant">常驻</option>
          </select></label>
          <label>扫描深度<input :value="entry.scan_depth" type="number" min="0" :max="lorebookLimits.scanDepth" step="1" :aria-label="`条目 ${index + 1} 扫描深度`" @input="patchEntry(index, { scan_depth: ($event.target as HTMLInputElement).valueAsNumber })" /></label>
        </div>
        <template v-if="entry.mode === 'keyword'">
          <LorebookKeywordFields :model-value="entry.primary_keywords" label="主关键词" :label-prefix="`条目 ${index + 1}`" :disabled="disabled" @update:model-value="patchEntry(index, { primary_keywords: $event })" />
          <label>组合规则<select :value="entry.keyword_rule" :aria-label="`条目 ${index + 1} 组合规则`" @change="patchEntry(index, { keyword_rule: ($event.target as HTMLSelectElement).value as LorebookEntry['keyword_rule'] })">
            <option value="primary_or_secondary">主 OR 副</option><option value="primary_and_secondary">主 AND 副</option><option value="primary_and_not_secondary">主 AND NOT 副</option>
          </select></label>
          <LorebookKeywordFields :model-value="entry.secondary_keywords" label="副关键词" :label-prefix="`条目 ${index + 1}`" :disabled="disabled" @update:model-value="patchEntry(index, { secondary_keywords: $event })" />
          <div class="toggle-row">
            <label><input :checked="entry.case_sensitive" type="checkbox" :aria-label="`条目 ${index + 1} 区分大小写`" @change="patchEntry(index, { case_sensitive: ($event.target as HTMLInputElement).checked })" />区分大小写</label>
            <label><input :checked="entry.recursive" type="checkbox" :aria-label="`条目 ${index + 1} 递归触发`" @change="patchEntry(index, { recursive: ($event.target as HTMLInputElement).checked })" />递归触发</label>
          </div>
        </template>
        <div class="field-row probability-row">
          <label class="toggle"><input :checked="entry.probability_enabled" type="checkbox" :aria-label="`条目 ${index + 1} 概率开关`" @change="patchEntry(index, { probability_enabled: ($event.target as HTMLInputElement).checked })" />概率开启</label>
          <label>概率 %<input :value="entry.probability" type="number" min="0" max="100" step="any" :disabled="disabled || !entry.probability_enabled" :aria-label="`条目 ${index + 1} 概率值`" @input="patchEntry(index, { probability: ($event.target as HTMLInputElement).valueAsNumber })" /></label>
        </div>
        <RolePlacementFields :model-value="entry.presentation" label="注入呈现" depth-label="插入深度" :label-prefix="`条目 ${index + 1}`" @update:model-value="presentation(index, $event)" />
      </section>
      <button v-if="group" type="button" class="add-entry" :disabled="disabled || entries.length >= lorebookLimits.entries" @click="add"><Plus :size="14" />新增条目</button>
      <section v-if="draft" class="variable-bindings" aria-label="只读变量绑定">
        <strong>变量对象 · 只读</strong>
        <label v-for="object in bindings" :key="object.object_key" class="toggle">
          <input type="checkbox" :checked="draft.object_keys.includes(object.object_key)" :aria-label="`读取变量对象 ${object.object_key}`" @change="binding(object.object_key, ($event.target as HTMLInputElement).checked)" />
          <span>{{ object.object_key }}</span>
        </label>
        <label v-for="key in missingBindings" :key="key" class="toggle warning">
          <input type="checkbox" checked :aria-label="`移除无效变量对象 ${key}`" @change="binding(key, false)" /><span>{{ key }} · 无读取权限或类型不匹配</span>
        </label>
        <small v-if="!bindings.length && !missingBindings.length">无已授权变量对象</small>
      </section>
      <div class="lifecycle"><span>生命周期</span><output>per_request / never</output></div>
    </fieldset>
    <p v-for="diagnosis in bindingErrors" :key="diagnosis" class="warning" role="status">{{ diagnosis }}</p>
    <p v-if="error" class="warning" role="status">{{ error }}</p>
    <p v-if="status" role="status">{{ status }}</p>
    <button type="submit" :disabled="disabled"><Save :size="14" />应用 Lorebook 配置</button>
  </form>
</template>
<style scoped>
.lorebook-fields { display:grid; gap:10px; min-width:0; font-size:12px; }
fieldset { display:grid; gap:12px; min-width:0; margin:0; padding:0; border:0; }
.lorebook-entry { display:grid; gap:10px; min-width:0; border-bottom:1px solid #505059; padding-bottom:12px; }
header { display:flex; align-items:center; gap:4px; min-width:0; }
strong { flex:1; min-width:0; font-weight:500; overflow-wrap:anywhere; }
header > button { flex:0 0 26px; width:26px; height:26px; padding:3px; }
button { display:flex; align-items:center; justify-content:center; gap:5px; padding:6px; border:1px solid #64776d; border-radius:3px; font:inherit; }
header > button { border-color:#505059; }
.entry-id { font:10px/1.5 Consolas,monospace; color:#949ba1; overflow-wrap:anywhere; }
label { display:grid; gap:5px; min-width:0; color:#bec3c7; }
input[type="text"],input[type="number"],select,textarea { box-sizing:border-box; width:100%; min-width:0; padding:6px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; font:inherit; }
input[type="text"],input[type="number"],select { height:30px; }
textarea { resize:vertical; line-height:1.6; }
.field-row { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; align-items:end; min-width:0; }
.toggle-row { display:flex; gap:12px; flex-wrap:wrap; }
.toggle,.toggle-row label { display:flex; align-items:center; gap:6px; min-height:30px; }
input[type="checkbox"] { width:14px; height:14px; flex:0 0 14px; margin:0; accent-color:#8cc9b3; }
.variable-bindings { display:grid; gap:5px; min-width:0; }
.variable-bindings span { min-width:0; overflow-wrap:anywhere; }
.variable-bindings strong { font-size:11px; }
.variable-bindings small { color:#949ba1; }
.lifecycle { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:6px; color:#a9afb6; font-size:11px; }
output { color:#bdd0c6; font:11px Consolas,monospace; }
p { margin:0; overflow-wrap:anywhere; color:#b9bec6; line-height:1.5; }
.warning { color:#e4a0a0; }
:disabled { opacity:.55; }
</style>
