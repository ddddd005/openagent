<script setup lang="ts">
import { computed } from "vue";
import GraphSchemaFields from "./GraphSchemaFields.vue";
import { useInstalledWorkbenchFrontendHost } from "../application/workbenchFrontendHost";
import { graphObject, type GraphDocument, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
const props = defineProps<{ node: GraphNode; definition: GraphNodeType; document: GraphDocument; catalog?: GraphNodeType[]; disabled?: boolean }>();
const emit = defineEmits<{ change: [value: Record<string, unknown>];
  sourceChange: [edgeId: string | null, source: { nodeId: string; portId: string } | null] }>();
const frontendHost = useInstalledWorkbenchFrontendHost();
const editors = computed(() => frontendHost.extensions.value.filter(row => row.declaration.binding.slot === "node-fields"
  && row.declaration.binding.target.component_id === props.node.component_id
  && row.declaration.binding.target.component_version === props.node.component_version));
const configurationFields = computed(() => new Set(editors.value.flatMap(row => row.configurationFields)));
const schema = computed(() => ({ ...props.definition.config_schema,
  properties: Object.fromEntries(Object.entries(graphObject(props.definition.config_schema.properties)
    ? props.definition.config_schema.properties : {}).filter(([key]) => !configurationFields.value.has(key))) }));
function sourceChange(edgeId: string | null, source: { nodeId: string; portId: string } | null) {
  emit("sourceChange", edgeId, source);
}
</script>
<template>
  <section class="graph-node-configuration">
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
