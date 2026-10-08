<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { RefreshCw, Save } from "lucide-vue-next";
import { graphClone, type GraphDocument, type GraphNode } from "../../domain/workflowGraph";
import { lorebookBindingErrors, lorebookVariableBindings } from "../../domain/lorebook";
import { useWorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../workflowFrontendSdk";
import { tavernExtensionsFor } from "../tavernFrontendManifest";
import { useLorebookResources } from "./lorebookResourceController";
import { isLorebookActivationConfig, isLorebookReferenceConfig, lorebookIdentity, lorebookIdentityKey,
  lorebookResourceLabel } from "./lorebookResources";

const props = defineProps<{ node: GraphNode; document: GraphDocument; disabled?: boolean }>();
const sdk = useWorkflowFrontendSdk(), resources = useLorebookResources();
const records = resources.records, loading = resources.loading, resourceError = resources.error;
const isReference = computed(() => props.node.component_id === "lorebook.global-reference");
const reference = computed(() => isLorebookReferenceConfig(props.node.config) ? props.node.config.reference : null);
const selected = ref(""), objectKeys = ref<string[] | null>(null), status = ref("");
const basis = ref<WorkflowNodeConfigurationRequest | null>(null);
const chosen = computed(() => records.value.find(row => lorebookIdentityKey(lorebookIdentity(row)) === selected.value));
const disabled = computed(() => !!props.disabled || sdk.locked.value || !basis.value
  || (isReference.value ? !reference.value : !objectKeys.value));
const bindings = computed(() => lorebookVariableBindings(props.document, props.node.node_binding_id));
const missingKeys = computed(() => objectKeys.value?.filter(key => !bindings.value.some(row => row.object_key === key)) ?? []);
const bindingErrors = computed(() => lorebookBindingErrors(props.document, props.node.node_binding_id, objectKeys.value ?? []));
const diagnosis = computed(() => {
  if (!reference.value) return "Lorebook 引用配置无效";
  const row = records.value.find(value => lorebookIdentityKey(lorebookIdentity(value)) === lorebookIdentityKey(reference.value!));
  if (!row) return "所选 Lorebook 资源缺失或尚未读取";
  return row.value.enabled ? `当前资源 · ${row.scope} · s${row.update_sequence}` : "所选 Lorebook 资源已停用";
});
watch(() => [sdk.workflowId.value, sdk.lifecycle.value, sdk.packages.value, props.node.node_binding_id,
  props.node.component_id, props.node.component_version, props.node.config], () => {
  status.value = "";
  selected.value = reference.value ? lorebookIdentityKey(reference.value) : "";
  objectKeys.value = isLorebookActivationConfig(props.node.config) ? [...props.node.config.object_keys] : null;
  const extension = tavernExtensionsFor(sdk.packages.value).find(row => row.component_id === props.node.component_id
    && row.component_version === props.node.component_version);
  basis.value = extension ? {
    workflowId: sdk.workflowId.value, nodeId: props.node.node_binding_id,
    componentId: props.node.component_id, componentVersion: props.node.component_version,
    expectedConfig: graphClone(props.node.config), patch: {}, extensionId: extension.extension_id,
    lifecycle: sdk.lifecycle.value,
  } : null;
}, { immediate: true, deep: true });
function binding(key: string, checked: boolean) {
  if (disabled.value || !objectKeys.value) return;
  objectKeys.value = checked ? [...new Set([...objectKeys.value, key])] : objectKeys.value.filter(value => value !== key);
}
function apply() {
  if (disabled.value || !basis.value) { status.value = "Lorebook 配置无效或当前不可编辑"; return; }
  if (isReference.value && !chosen.value) { status.value = "请先选择已有 Lorebook 资源"; return; }
  if (!isReference.value && bindingErrors.value.length) { status.value = bindingErrors.value.join("；"); return; }
  const patch = isReference.value ? { reference: lorebookIdentity(chosen.value!) } : { object_keys: [...objectKeys.value!] };
  const result = sdk.configureNode({ ...graphClone(basis.value), patch });
  status.value = result ? result.workflowId === basis.value.workflowId ? "Lorebook 配置已更新" : "Lorebook 配置已写入新副本"
    : "配置依据已变化或当前不可编辑，请重新选择节点";
}
onMounted(() => { if (isReference.value) void resources.refresh(); });
</script>

<template>
  <form class="global-lorebook-fields" aria-label="全局 Lorebook 节点配置" @submit.prevent="apply">
    <template v-if="isReference">
      <label>全局 Lorebook
        <div class="resource-row">
          <select v-model="selected" :disabled="disabled" aria-label="全局 Lorebook 选择">
            <option value="">选择 Lorebook 资源</option>
            <option v-if="selected && !chosen" :value="selected">缺失引用 · {{ reference?.scope }} · {{ reference?.resource_id }}</option>
            <option v-for="record in records" :key="lorebookIdentityKey(lorebookIdentity(record))"
              :value="lorebookIdentityKey(lorebookIdentity(record))">
              {{ lorebookResourceLabel(record) }} · {{ record.scope }} · s{{ record.update_sequence }}{{ record.value.enabled ? '' : '（已停用）' }}
            </option>
          </select>
          <button type="button" title="刷新 Lorebook 资源" aria-label="刷新 Lorebook 资源" :disabled="loading"
            @click="resources.refresh()"><RefreshCw :size="14" /></button>
        </div>
      </label>
      <p role="status">{{ diagnosis }}</p>
      <p v-if="resourceError" class="warning" role="status">{{ resourceError }}</p>
    </template>
    <fieldset v-else :disabled="disabled">
      <strong>变量对象 · 只读</strong>
      <p v-if="!objectKeys" class="warning" role="status">Lorebook 触发配置无效，原配置保持不变</p>
      <label v-for="object in bindings" :key="object.object_key" class="toggle">
        <input type="checkbox" :checked="objectKeys?.includes(object.object_key)" :aria-label="`读取变量对象 ${object.object_key}`"
          @change="binding(object.object_key, ($event.target as HTMLInputElement).checked)" />
        <span>{{ object.object_key }}</span>
      </label>
      <label v-for="key in missingKeys" :key="key" class="toggle warning">
        <input type="checkbox" checked :aria-label="`移除无效变量对象 ${key}`" @change="binding(key, false)" />
        <span>{{ key }} · 无读取权限或类型不匹配</span>
      </label>
      <small v-if="!bindings.length && !missingKeys.length">无已授权变量对象</small>
      <p v-for="error in bindingErrors" :key="error" class="warning" role="status">{{ error }}</p>
      <output>per_request / never</output>
    </fieldset>
    <p v-if="status" role="status">{{ status }}</p>
    <button type="submit" :disabled="disabled || isReference && !chosen"><Save :size="14" />应用 Lorebook 配置</button>
  </form>
</template>

<style scoped>
.global-lorebook-fields { display:grid; gap:10px; min-width:0; font-size:12px; }
label,fieldset { display:grid; gap:5px; min-width:0; }
fieldset { margin:0; padding:0; border:0; }
.resource-row { display:flex; gap:5px; min-width:0; }
select { flex:1; width:100%; min-width:0; box-sizing:border-box; padding:6px; background:#222226; color:#dedee4; border:1px solid #505059; border-radius:3px; }
button { display:flex; align-items:center; justify-content:center; gap:6px; padding:6px; border:1px solid #64776d; border-radius:3px; }
.resource-row button { flex:0 0 28px; width:28px; height:28px; }
.toggle { display:flex; align-items:center; gap:6px; }
.toggle span { min-width:0; overflow-wrap:anywhere; }
input[type="checkbox"] { width:14px; height:14px; flex:0 0 14px; margin:0; }
p { margin:0; color:#b9bec6; overflow-wrap:anywhere; }
small { color:#949ba1; }
output { color:#bdd0c6; font:11px Consolas,monospace; }
.warning { color:#e4a0a0; }
:disabled { opacity:.55; }
</style>
