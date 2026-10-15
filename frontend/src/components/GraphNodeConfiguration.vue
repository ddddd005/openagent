<script setup lang="ts">
import { computed } from "vue";
import GraphSchemaFields from "./GraphSchemaFields.vue";
import { useInstalledWorkbenchFrontendHost } from "../application/workbenchFrontendHost";
import { graphNodeAllowsAdjustable, graphObject, type GraphDocument, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
import { isVariableRegistrationNode, variableRegistrationChange,
  variableRegistrationIssues } from "../domain/workflowVariableRegistration";
const props = defineProps<{ node: GraphNode; definition: GraphNodeType; document: GraphDocument; catalog?: GraphNodeType[]; disabled?: boolean }>();
const emit = defineEmits<{ change: [value: Record<string, unknown>];
  policyChange: [mode: "fixed" | "adjustable"] }>();
const frontendHost = useInstalledWorkbenchFrontendHost();
const editors = computed(() => frontendHost.extensions.value.filter(row => row.declaration.binding.slot === "node-fields"
  && row.declaration.binding.target.component_id === props.node.component_id
  && row.declaration.binding.target.component_version === props.node.component_version));
const configurationFields = computed(() => new Set(editors.value.flatMap(row => row.configurationFields)));
const objectAccesses = computed(() => (props.definition.object_accesses ?? [])
  .filter(access => !configurationFields.value.has(access.config_field)));
const registration = computed(() => isVariableRegistrationNode(props.node));
const issues = computed(() => registration.value ? variableRegistrationIssues(props.node.config) : {});
const schema = computed(() => {
  const properties = Object.fromEntries(Object.entries(graphObject(props.definition.config_schema.properties)
    ? props.definition.config_schema.properties : {}).filter(([key]) =>
      !configurationFields.value.has(key) && !objectAccesses.value.some(access => access.config_field === key)));
  if (registration.value) {
    const field = (key: string) => graphObject(properties[key]) ? properties[key] : {};
    properties.name = { ...field("name"), title: "变量名称" };
    properties.value_type = { ...field("value_type"), title: "值类型",
      "x-enum-labels": ["字符串", "整数", "数值", "布尔值"] };
    properties.has_initial = { ...field("has_initial"), title: "设置初值" };
    properties.initial_value = { ...field("initial_value"), title: "初值",
      type: props.node.config.has_initial === true ? props.node.config.value_type : "null" };
  }
  return { ...props.definition.config_schema, properties };
});
function changeConfiguration(value: Record<string, unknown>) {
  emit("change", registration.value ? variableRegistrationChange(props.node.config, value) : value);
}
function objectBindings(access: NonNullable<GraphNodeType["object_accesses"]>[number]) {
  return (props.document.object_bindings ?? []).filter(binding =>
    binding.type_id === access.type_id && binding.schema_version === access.schema_version
    && (!providesObject(access) || binding.registrar_node_id === props.node.node_binding_id)
    && (access.access === "write" || binding.readers.includes(props.node.node_binding_id))
    && (access.access === "read" || binding.writers.includes(props.node.node_binding_id)));
}
function providesObject(access: NonNullable<GraphNodeType["object_accesses"]>[number]) {
  return props.definition.provided_session_item_types?.some(type =>
    type.type_id === access.type_id && type.schema_version === access.schema_version);
}
function selectedObjectKeys(access: NonNullable<GraphNodeType["object_accesses"]>[number]) {
  const value = props.node.config[access.config_field];
  if (access.multiple)
    return Array.isArray(value) && value.every((key): key is string => typeof key === "string") ? value : null;
  return typeof value === "string" ? value : null;
}
function patchObjectKeys(access: NonNullable<GraphNodeType["object_accesses"]>[number], event: Event) {
  if (props.disabled || providesObject(access)) return;
  const select = event.target as HTMLSelectElement;
  const value = access.multiple
    ? [...select.selectedOptions].map(option => option.value).filter(Boolean)
    : select.value;
  emit("change", { ...props.node.config, [access.config_field]: value });
}
function hasSelectedObject(access: NonNullable<GraphNodeType["object_accesses"]>[number]) {
  const allowed = new Set(objectBindings(access).map(binding => binding.object_key));
  const selected = selectedObjectKeys(access);
  if (Array.isArray(selected)) return selected.every(key => allowed.has(key));
  return typeof selected === "string" && selected !== "" && allowed.has(selected);
}
const configurationMode = computed(() => graphNodeAllowsAdjustable(props.node, props.definition)
  && props.document.schema_version === 3
  ? props.node.configuration_policy?.prompt_mode ?? null : null);
</script>
<template>
  <section class="graph-node-configuration">
    <component v-for="editor in editors" :key="editor.declaration.extension_id" :is="editor.component"
      :node="node" :document="document" :catalog="catalog ?? []" :disabled="disabled" />
    <div v-if="configurationMode" class="configuration-mode">
      <span>配置模式</span>
      <div role="group" aria-label="配置模式" class="configuration-mode-options">
        <button type="button" :aria-pressed="configurationMode === 'fixed'" :disabled="disabled || configurationMode === 'fixed'"
          @click="emit('policyChange', 'fixed')">固定</button>
        <button type="button" :aria-pressed="configurationMode === 'adjustable'" :disabled="disabled || configurationMode === 'adjustable'"
          @click="emit('policyChange', 'adjustable')">可调</button>
      </div>
    </div>
    <label v-for="access in objectAccesses" :key="access.config_field" class="object-access-field">
      <span>{{ access.multiple ? "会话数据项（可多选）" : "会话数据项" }}</span>
      <select :value="selectedObjectKeys(access)" :multiple="access.multiple" :disabled="disabled || providesObject(access)"
        @change="patchObjectKeys(access, $event)">
        <option v-if="!access.multiple" value="">选择准确的数据项</option>
        <option v-for="binding in objectBindings(access)" :key="binding.object_key" :value="binding.object_key">
          {{ binding.object_key }}
        </option>
      </select>
      <small v-if="!hasSelectedObject(access)" role="status">当前键没有匹配的登记项和明确权限</small>
    </label>
    <GraphSchemaFields :schema="schema" :value="node.config" :disabled="disabled" :errors="issues"
      :identity="`node:${node.node_binding_id}:${node.component_id}@${node.component_version}`"
      @change="changeConfiguration" />
  </section>
</template>
<style scoped>
.graph-node-configuration { display:grid; gap:12px; min-width:0; }
.object-access-field { display:grid; gap:5px; min-width:0; font-size:12px; }
.configuration-mode { display:grid; gap:5px; font-size:12px; }
.configuration-mode-options { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); border:1px solid #505059; border-radius:4px; }
.configuration-mode-options button { min-width:0; padding:5px; border:0; border-radius:0; }
.configuration-mode-options [aria-pressed=true] { background:#35453d; color:#c2dfd2; }
.graph-resource-field { display:grid; gap:5px; font-size:12px; }
.graph-resource-field > span { display:flex; align-items:center; justify-content:space-between; }
select { width:100%; min-width:0; padding:6px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; }
button { display:grid; place-items:center; padding:4px; border-radius:3px; }
small { color:#e4a0a0; font-size:11px; overflow-wrap:anywhere; }
</style>
