<script setup lang="ts">
import { computed } from "vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { graphObject } from "../domain/workflowGraph";
import { useInstalledWorkbenchFrontendHost } from "../application/workbenchFrontendHost";
const graph = useWorkflowGraphStore();
const frontendHost = useInstalledWorkbenchFrontendHost();
const objects = computed(() => Object.entries(graph.session?.objects ?? {}).map(([key, object]) => ({ key, ...object })));
const renderers = computed(() => objects.value.flatMap(object => frontendHost.extensions.value
  .filter(row => row.declaration.binding.slot === "session-object"
    && row.declaration.binding.target.scope === "session"
    && row.declaration.binding.target.type_id === object.type_id
    && row.declaration.binding.target.schema_version === object.schema_version)
  .map(row => ({ ...row, objectKey: object.key }))));
const variables = computed(() => Object.entries(graph.session?.data.values ?? {}).map(([name, data]) => ({ name,
  value: graphObject(data) ? data.value : data, type: graphObject(data) ? String(data.type) : "unknown" })));
function write(name: string, event: Event) {
  const input = event.target as HTMLTextAreaElement;
  try { const value = JSON.parse(input.value); input.setCustomValidity(""); void graph.writeData({ [name]: value }); }
  catch { input.setCustomValidity("请输入有效 JSON 值"); input.reportValidity(); }
}
</script>
<template>
  <section v-if="graph.session?.objects !== undefined" class="graph-session-data"><h3>会话对象</h3>
    <p v-if="!objects.length">此会话尚无对象绑定。</p>
    <details v-for="object in objects" :key="object.key"><summary>{{ object.key }} · {{ object.type_id }}@{{ object.schema_version }} · r{{ object.revision }}{{ object.deleted ? ' · 已删除' : '' }}</summary>
      <pre>{{ JSON.stringify(object.value, null, 2) }}</pre><small>{{ object.binding.scope }} · {{ object.revision_id }}</small>
    </details>
    <component v-for="renderer in renderers" :key="`${renderer.declaration.extension_id}:${renderer.objectKey}`"
      :is="renderer.component" :object-key="renderer.objectKey" />
  </section>
  <section v-if="variables.length" class="graph-session-data"><h3>旧变量（历史机制）</h3><label v-for="variable in variables" :key="variable.name"><span>{{ variable.name }} · {{ variable.type }}</span><textarea :value="JSON.stringify(variable.value) ?? 'null'" rows="2" :disabled="graph.locked" @change="write(variable.name, $event)" /></label></section>
</template>
<style scoped>
.graph-session-data { display:grid; gap:9px; border-top:1px solid #45454d; padding-top:10px; }
h3 { margin:0; font-size:12px; font-weight:500; }
label { display:grid; gap:5px; font-size:11px; min-width:0; }
textarea { width:100%; min-width:0; box-sizing:border-box; resize:vertical; background:#202024; border:1px solid #52525a; color:#dedee5; padding:6px; font:12px Consolas,monospace; }
p,summary,small { font-size:11px; line-height:1.6; overflow-wrap:anywhere; }
p,small { color:#b2b8b6; }
pre { margin:8px 0; font:12px Consolas,monospace; white-space:pre-wrap; overflow-wrap:anywhere; }
</style>
