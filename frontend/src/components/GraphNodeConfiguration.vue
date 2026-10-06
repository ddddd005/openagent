<script setup lang="ts">
import { computed, watch } from "vue";
import { RefreshCw } from "lucide-vue-next";
import GraphSchemaFields from "./GraphSchemaFields.vue";
import { useInstalledWorkbenchFrontendHost } from "../application/workbenchFrontendHost";
import { graphObject, type GraphDocument, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
import { useModelConfigurationStore } from "../stores/modelConfiguration";
import { useGlobalContentStore } from "../stores/globalContent";
const props = defineProps<{ node: GraphNode; definition: GraphNodeType; document: GraphDocument; catalog?: GraphNodeType[]; disabled?: boolean }>();
const emit = defineEmits<{ change: [value: Record<string, unknown>];
  sourceChange: [edgeId: string | null, source: { nodeId: string; portId: string } | null] }>();
const models = useModelConfigurationStore();
const content = useGlobalContentStore();
const frontendHost = useInstalledWorkbenchFrontendHost();
const editors = computed(() => frontendHost.extensions.value.filter(row => row.declaration.binding.slot === "node-fields"
  && row.declaration.binding.target.component_id === props.node.component_id
  && row.declaration.binding.target.component_version === props.node.component_version));
const resourceField = computed(() => ({ "workflow.model-provider": "provider_id", "workflow.context": "source_node_id",
  "workflow.global-content": "resource_id" } as Record<string, string>)[props.node.component_id]);
const configurationFields = computed(() => new Set(editors.value.flatMap(row => row.configurationFields)));
const schema = computed(() => ({ ...props.definition.config_schema,
  properties: Object.fromEntries(Object.entries(graphObject(props.definition.config_schema.properties)
    ? props.definition.config_schema.properties : {}).filter(([key]) => key !== resourceField.value
      && !configurationFields.value.has(key))) }));
const choices = computed(() => props.node.component_id === "workflow.model-provider"
  ? models.providers.map(provider => ({ id: provider.provider_id, label: `${provider.name} · r${provider.revision}${provider.enabled ? "" : "（已停用）"}` }))
  : props.node.component_id === "workflow.global-content"
    ? content.records.map(record => ({ id: record.resource_id, label: `${record.name} · r${record.revision}${record.enabled ? "" : "（已停用）"}` }))
    : props.document.nodes.filter(node => node.component_id === "workflow.agent" && node.component_version === "2")
      .map(node => ({ id: node.node_binding_id, label: node.title })));
const selected = computed(() => String(props.node.config[resourceField.value ?? ""] ?? ""));
const label = computed(() => ({ provider_id: "供应商", source_node_id: "上下文来源", resource_id: "全局内容" } as Record<string,string>)[resourceField.value ?? ""]);
const error = computed(() => props.node.component_id === "workflow.model-provider" ? models.error?.reason
  : props.node.component_id === "workflow.global-content" ? content.error : null);
async function refresh() {
  if (props.node.component_id === "workflow.model-provider") await models.loadProviders();
  if (props.node.component_id === "workflow.global-content") await content.initialize();
}
function select(event: Event) {
  emit("change", { ...props.node.config, [resourceField.value!]: (event.target as HTMLSelectElement).value || null });
}
function sourceChange(edgeId: string | null, source: { nodeId: string; portId: string } | null) {
  emit("sourceChange", edgeId, source);
}
watch(() => props.node.component_id, () => { void refresh(); }, { immediate: true });
</script>
<template>
  <section class="graph-node-configuration">
    <label v-if="resourceField" class="graph-resource-field"><span>{{ label }}<button v-if="resourceField !== 'source_node_id'" type="button" title="刷新资源" aria-label="刷新资源" :disabled="models.loading || content.busy" @click="refresh"><RefreshCw :size="13" /></button></span>
      <select :value="selected" :disabled="disabled" @change="select"><option value="">未选择</option><option v-if="selected && !choices.some(option => option.id === selected)" :value="selected">缺失引用 · {{ selected }}</option><option v-for="option in choices" :key="option.id" :value="option.id">{{ option.label }}</option></select>
      <small v-if="error" role="status">{{ error }}</small>
    </label>
    <GraphSchemaFields :schema="schema" :value="node.config" :disabled="disabled" @change="emit('change', $event)" />
    <component v-for="editor in editors" :key="editor.declaration.extension_id" :is="editor.component"
      :node="node" :document="document" :catalog="catalog ?? []" :disabled="disabled"
      @change="sourceChange" />
  </section>
</template>
<style scoped>
.graph-node-configuration { display:grid; gap:12px; min-width:0; }
.graph-resource-field { display:grid; gap:5px; font-size:12px; }
.graph-resource-field > span { display:flex; align-items:center; justify-content:space-between; }
select { width:100%; min-width:0; padding:6px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; }
button { display:grid; place-items:center; padding:4px; border-radius:3px; }
small { color:#e4a0a0; font-size:11px; overflow-wrap:anywhere; }
</style>
