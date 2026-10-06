<script setup lang="ts">
import { computed, ref } from "vue";
import { Plus, Trash2 } from "lucide-vue-next";
import { frontendSources } from "../domain/frontendWiring";
import type { GraphDocument, GraphNode, GraphNodeType } from "../domain/workflowGraph";
const props = defineProps<{ node: GraphNode; document: GraphDocument; catalog: GraphNodeType[]; disabled?: boolean }>();
const emit = defineEmits<{ change: [edgeId: string | null, source: { nodeId: string; portId: string } | null] }>();
const selected = ref("");
const sources = computed(() => frontendSources(props.document, props.catalog).filter(row => row.compatible));
const incoming = computed(() => props.document.edges.filter(edge => edge.target_node_id === props.node.node_binding_id
  && edge.target_port_id === "content").sort((a, b) => a.order - b.order));
const key = (nodeId: string, portId: string) => JSON.stringify({ nodeId, portId });
function replace(edgeId: string | null, value: string) {
  if (value) emit("change", edgeId, JSON.parse(value));
}
function add() { replace(null, selected.value); selected.value = ""; }
</script>
<template>
  <fieldset class="frontend-sources"><legend>展示文本来源 · TEXT@2</legend>
    <div v-for="edge in incoming" :key="edge.edge_id" class="source-row">
      <select :value="key(edge.source_node_id, edge.source_port_id)" :disabled="disabled" :aria-label="`文本来源 ${edge.order + 1}`" @change="replace(edge.edge_id, ($event.target as HTMLSelectElement).value)">
        <option v-if="!sources.some(row => row.node.node_binding_id === edge.source_node_id && row.port.port_id === edge.source_port_id)" :value="key(edge.source_node_id, edge.source_port_id)">缺失或不兼容来源 · {{ edge.source_node_id }} / {{ edge.source_port_id }}</option>
        <option v-for="row in sources" :key="key(row.node.node_binding_id, row.port.port_id)" :value="key(row.node.node_binding_id, row.port.port_id)">{{ row.node.title }} / {{ row.port.port_id }}</option>
      </select>
      <button type="button" :disabled="disabled" title="移除此文本来源" aria-label="移除此文本来源" @click="emit('change', edge.edge_id, null)"><Trash2 :size="13" /></button>
    </div>
    <div class="source-row"><select v-model="selected" :disabled="disabled" aria-label="新增文本来源"><option value="">选择文本来源</option><option v-for="row in sources" :key="key(row.node.node_binding_id, row.port.port_id)" :value="key(row.node.node_binding_id, row.port.port_id)">{{ row.node.title }} / {{ row.port.port_id }}</option></select>
      <button type="button" :disabled="disabled || !selected" title="添加文本来源" aria-label="添加文本来源" @click="add"><Plus :size="13" /></button>
    </div>
  </fieldset>
</template>
<style scoped>
fieldset { margin:0; padding:8px; border:1px solid #46464d; min-width:0; display:grid; gap:7px; }
legend { font-size:11px; padding:0 3px; }
.source-row { display:flex; gap:4px; min-width:0; }
select { flex:1; min-width:0; width:100%; padding:5px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; font-size:11px; }
button { display:grid; place-items:center; padding:4px; border-radius:3px; }
button:disabled { opacity:.4; }
</style>
