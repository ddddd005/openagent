<script setup lang="ts">
import { computed } from "vue";
import { Handle, Position } from "@vue-flow/core";
import { AlertCircle, Box, Flag } from "lucide-vue-next";
import { nodePorts, type GraphNode, type GraphNodeType, type GraphPort } from "../domain/workflowGraph";
import { nodeDisplayTitle, nodeProfileLabel, nodeTypeLabel } from "../domain/nodeCatalog";
const props = defineProps<{ selected?: boolean; data: { node: GraphNode; definition?: GraphNodeType;
  displayTitle?: string; status: string; inputs: string[]; outputs: string[]; error: boolean; executionRoot?: boolean } }>();
function ports(direction: "inputs" | "outputs"): GraphPort[] {
  const known = nodePorts(props.data.definition, props.data.node, direction);
  return [...known, ...[...new Set(props.data[direction])].filter(id => !known.some(port => port.port_id === id))
    .map(id => ({ port_id: id, data_type: "?", required: false, multiple: false }))];
}
const inputs = computed(() => ports("inputs"));
const outputs = computed(() => ports("outputs"));
const summary = computed(() => typeof props.data.node.config.text === "string" ? props.data.node.config.text
  : props.data.definition ? nodeTypeLabel(props.data.definition) : props.data.node.component_id);
const profile = computed(() => props.data.definition ? nodeProfileLabel(props.data.definition) : "");
</script>

<template>
  <article class="graph-node" :class="{ selected, running: data.status === 'running', invalid: data.error, missing: !data.definition }">
    <Handle id="__control_in" type="target" :position="Position.Top" :connectable="false" class="control-anchor" />
    <Handle id="__control_out" type="source" :position="Position.Bottom" :connectable="false" class="control-anchor" />
    <header><Flag v-if="data.definition?.is_output" :size="15" /><Box v-else :size="15" /><strong>{{ data.displayTitle ?? nodeDisplayTitle(data.node, data.definition) }}</strong><AlertCircle v-if="data.error || !data.definition" :size="15" /></header>
    <p>{{ summary || '—' }}</p>
    <div v-if="profile && profile !== '标准'" class="node-profile" :title="`${data.node.component_id}@${data.node.component_version}`">{{ profile }}</div>
    <div class="ports">
      <div class="port-column">
        <div v-for="port in inputs" :key="port.port_id" class="port-row input">
          <Handle :id="port.port_id" type="target" :position="Position.Left" />
          <span>{{ port.port_id }}</span><small>{{ port.data_type }}</small>
        </div>
      </div>
      <div class="port-column">
        <div v-for="port in outputs" :key="port.port_id" class="port-row output">
          <small>{{ port.data_type }}</small><span>{{ port.port_id }}</span>
          <Handle :id="port.port_id" type="source" :position="Position.Right" />
        </div>
      </div>
    </div>
    <footer>{{ data.status }}<span v-if="data.executionRoot">执行根</span><span v-if="data.definition && !data.definition.executable">未接入执行器</span><span v-if="!data.definition">类型缺失</span></footer>
  </article>
</template>

<style scoped>
.graph-node { width:240px; border:1px solid #606068; border-radius:5px; background:#2b2b30; color:#dddde3; box-shadow:0 3px 8px #0003; font-size:12px; }
.control-anchor { opacity:0; pointer-events:none; }
.graph-node.selected { border-color:#c5d4d0; box-shadow:0 0 0 1px #c5d4d0; }
.graph-node.running { border-color:#48d284; box-shadow:0 0 0 2px #48d284; }
.graph-node.invalid,.graph-node.missing { border-color:#d17e7e; }
header { display:flex; align-items:center; gap:7px; min-height:34px; padding:0 11px; border-bottom:1px solid #45454d; background:#36363c; border-radius:4px 4px 0 0; }
strong { flex:1; min-width:0; overflow-wrap:anywhere; font-weight:500; padding:6px 0; }
p { margin:9px 11px; color:#aab4b0; height:32px; overflow:hidden; white-space:pre-line; overflow-wrap:anywhere; line-height:16px; }
.node-profile { margin:0 11px 8px; font-size:10px; color:#b8b5a4; overflow-wrap:anywhere; line-height:15px; }
.ports { display:flex; justify-content:space-between; min-height:28px; }
.port-column { max-width:50%; }
.port-row { display:flex; position:relative; align-items:center; gap:5px; min-height:27px; padding:0 11px; }
.port-row small { font-size:9px; color:#89978e; }
.output { justify-content:flex-end; }
.vue-flow__handle { width:8px; height:8px; background:#8bc4aa; border-color:#26382d; }
footer { display:flex; justify-content:space-between; gap:4px; padding:5px 11px; border-top:1px solid #45454d; font-size:10px; color:#9999a3; }
</style>
