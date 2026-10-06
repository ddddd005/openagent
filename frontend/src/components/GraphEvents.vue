<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { RefreshCw } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { isGraphEventBindings } from "../domain/workflowGraph";
import { assertEventPayload, readWorkflowEventBindings, type WorkflowEventBindings } from "../application/workflowEvents";

const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
const draft = ref("[]"), payload = ref("{}"), selected = ref(""), error = ref<string | null>(null);
const packet = ref<WorkflowEventBindings | null>(null), loading = ref(false);
let generation = 0;
const basis = () => JSON.stringify([workspace.activeWorkflowId, graph.document?.workflow_definition_id,
  graph.document?.revision, graph.session?.workflow_session_id, graph.session?.definition_revision, graph.session?.revision]);
const key = (id: string, version: number) => JSON.stringify([id, version]);
const binding = computed(() => packet.value?.bindings.find(row => key(row.event_id, row.schema_version) === selected.value));
async function refresh() {
  const token = ++generation, fixedBasis = basis(); packet.value = null; loading.value = false; error.value = null;
  const session = graph.session;
  if (!session) return;
  loading.value = true;
  try {
    const value = await readWorkflowEventBindings({ sessionId: session.workflow_session_id,
      definitionId: session.workflow_definition_id, definitionRevision: session.definition_revision });
    if (token === generation && basis() === fixedBasis) packet.value = value;
  } catch (failure) {
    if (token === generation && basis() === fixedBasis) error.value = failure instanceof Error ? failure.message : "事件目录读取失败";
  } finally { if (token === generation) loading.value = false; }
}
function apply() {
  try {
    const value: unknown = JSON.parse(draft.value);
    if (!isGraphEventBindings(value, graph.document?.nodes)) throw new Error("事件绑定、目标节点或本地对象 schema 无效");
    graph.setEventBindings(value); error.value = null;
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "事件 JSON 无效"; }
}
async function submit() {
  if (!binding.value) return;
  try {
    const value: unknown = JSON.parse(payload.value); assertEventPayload(value); error.value = null;
    if (await graph.submitEvent(binding.value.event_id, binding.value.schema_version, value)) await refresh();
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "事件载荷 JSON 无效"; }
}
watch(() => [graph.document?.workflow_definition_id, JSON.stringify(graph.document?.event_bindings ?? [])],
  () => { draft.value = JSON.stringify(graph.document?.event_bindings ?? [], null, 2); }, { immediate: true });
watch(() => [workspace.activeWorkflowId, graph.session?.workflow_session_id, graph.session?.definition_revision], () => {
  selected.value = ""; payload.value = "{}";
});
watch(basis, () => { void refresh(); }, { immediate: true, flush: "sync" });
onUnmounted(() => { generation++; });
</script>
<template>
  <section class="graph-events" aria-label="局部事件">
    <label>事件绑定 JSON<textarea v-model="draft" rows="12" spellcheck="false" :disabled="graph.locked" /></label>
    <button type="button" :disabled="graph.locked || !graph.document" @click="apply">应用事件声明</button>
    <p>声明事件后，普通回合仅从显式执行根开始；事件处理目标及其依赖由绑定单独声明。</p>
    <header><strong>已发布事件</strong><button type="button" title="刷新事件目录" aria-label="刷新事件目录" :disabled="loading || !graph.session" @click="refresh"><RefreshCw :size="13" /></button></header>
    <label>事件<select v-model="selected" :disabled="graph.locked || loading"><option value="">选择事件版本</option>
      <option v-for="row in packet?.bindings ?? []" :key="key(row.event_id, row.schema_version)" :value="key(row.event_id, row.schema_version)">{{ row.display_name }} · {{ row.event_id }}@{{ row.schema_version }} · {{ row.audience }}</option>
    </select></label>
    <details v-if="binding"><summary>载荷 schema / 目标节点</summary><pre>{{ JSON.stringify({ payload_schema: binding.payload_schema, target_node_ids: binding.target_node_ids }, null, 2) }}</pre></details>
    <label>事件载荷 JSON<textarea v-model="payload" rows="6" spellcheck="false" :disabled="graph.locked" /></label>
    <button type="button" :disabled="graph.locked || !packet?.can_submit || !binding" @click="submit">提交局部事件</button>
    <p v-if="!graph.session">尚未选择工作流会话。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>
<style scoped>
.graph-events { display:grid; gap:10px; min-width:0; font-size:12px; }
label { display:grid; gap:5px; min-width:0; } header { display:flex; align-items:center; justify-content:space-between; }
textarea,select { width:100%; min-width:0; box-sizing:border-box; padding:6px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; font:12px Consolas,monospace; }
textarea { resize:vertical; } button { padding:6px; border:1px solid #505059; border-radius:3px; } button:disabled { opacity:.45; }
p,pre,summary { font-size:11px; line-height:1.6; overflow-wrap:anywhere; } pre { white-space:pre-wrap; }
.error { color:#e4a0a0; }
</style>
