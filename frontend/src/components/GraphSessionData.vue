<script setup lang="ts">
import { computed } from "vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useInstalledWorkbenchFrontendHost } from "../application/workbenchFrontendHost";
import ThinkingSummary from "./ThinkingSummary.vue";
import { modelPresentationJson } from "../domain/modelPresentation";
const graph = useWorkflowGraphStore();
const frontendHost = useInstalledWorkbenchFrontendHost();
const objects = computed(() => Object.entries(graph.session?.objects ?? {}).map(([key, object]) => ({ key, ...object })));
const renderers = computed(() => objects.value.flatMap(object => frontendHost.extensions.value
  .filter(row => row.declaration.binding.slot === "session-object"
    && row.declaration.binding.target.scope === "session"
    && row.declaration.binding.target.type_id === object.type_id
    && row.declaration.binding.target.schema_version === object.schema_version)
  .map(row => ({ ...row, objectKey: object.key }))));
</script>
<template>
  <section v-if="graph.session?.objects !== undefined" class="graph-session-data"><h3>会话对象</h3>
    <p v-if="!objects.length">此会话尚无对象绑定。</p>
    <details v-for="object in objects" :key="object.key"><summary>{{ object.key }} · {{ object.type_id }}@{{ object.schema_version }} · r{{ object.revision }}{{ object.deleted ? ' · 已删除' : '' }}</summary>
      <ThinkingSummary :value="object.value" /><pre>{{ modelPresentationJson(object.value) }}</pre><small>{{ object.binding.scope }} · {{ object.revision_id }}</small>
    </details>
    <component v-for="renderer in renderers" :key="`${renderer.declaration.extension_id}:${renderer.objectKey}`"
      :is="renderer.component" :object-key="renderer.objectKey" />
  </section>
</template>
<style scoped>
.graph-session-data { display:grid; gap:9px; border-top:1px solid #45454d; padding-top:10px; }
h3 { margin:0; font-size:12px; font-weight:500; }
p,summary,small { font-size:11px; line-height:1.6; overflow-wrap:anywhere; }
p,small { color:#b2b8b6; }
pre { margin:8px 0; font:12px Consolas,monospace; white-space:pre-wrap; overflow-wrap:anywhere; }
</style>
