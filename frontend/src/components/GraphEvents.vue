<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { RefreshCw } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { isGraphEventBindings } from "../domain/workflowGraph";
import { assertEventPayload, readWorkflowEventBindings, type WorkflowEventBindings } from "../application/workflowEvents";
import { useBoundFormDraft } from "../composables/useBoundFormDraft";
import FormDraftNotice from "./FormDraftNotice.vue";

const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
const draft = ref("[]"), payload = ref("{}"), selected = ref(""), error = ref<string | null>(null);
const packet = ref<WorkflowEventBindings | null>(null), loading = ref(false);
const applying = ref(false), submitting = ref(false);
let generation = 0;
const basis = () => JSON.stringify([workspace.activeWorkflowId, graph.document?.workflow_definition_id,
  graph.document?.revision, graph.session?.workflow_session_id, graph.session?.definition_revision, graph.session?.revision]);
const key = (id: string, version: number) => JSON.stringify([id, version]);
const binding = computed(() => packet.value?.bindings.find(row => key(row.event_id, row.schema_version) === selected.value));
const declarationDraft = useBoundFormDraft({
  id: () => graph.document ? `event-bindings:${graph.document.workflow_definition_id}` : "",
  basis: () => ({ workflowId: graph.document?.workflow_definition_id ?? null, bindings: graph.document?.event_bindings ?? [] }),
  capture: () => ({ draft: draft.value }),
  restore: value => { if (typeof value?.draft !== "string") throw new Error("事件声明草稿格式不兼容"); draft.value = value.draft; },
});
const payloadDraft = useBoundFormDraft({
  id: () => graph.document ? `event-payload:${graph.document.workflow_definition_id}:${graph.active?.session_id ?? "none"}` : "",
  basis: () => ({ workflowId: graph.document?.workflow_definition_id ?? null, sessionId: graph.active?.session_id ?? null,
    revision: graph.session?.definition_revision ?? null }),
  capture: () => ({ selected: selected.value, payload: payload.value }),
  restore: value => {
    if (typeof value?.payload !== "string" || typeof value?.selected !== "string") throw new Error("事件载荷草稿格式不兼容");
    selected.value = value.selected; payload.value = value.payload;
  },
});
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
async function apply() {
  if (graph.locked || applying.value || declarationDraft.loading.value || declarationDraft.conflict.value) return;
  applying.value = true;
  const context = declarationDraft.context.value, originalBasis = basis();
  try {
    const value: unknown = JSON.parse(draft.value);
    if (!isGraphEventBindings(value, graph.document?.nodes)) throw new Error("事件绑定、目标节点或本地对象 schema 无效");
    if (!await declarationDraft.flush()) throw new Error("事件声明尚未写入本机");
    if (context !== declarationDraft.context.value || originalBasis !== basis() || graph.locked) return;
    declarationDraft.adoptBasis({ workflowId: graph.document?.workflow_definition_id ?? null, bindings: value });
    graph.setEventBindings(value); error.value = null;
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "事件 JSON 无效"; }
  finally { applying.value = false; }
}
async function submit() {
  if (!binding.value || graph.locked || submitting.value || payloadDraft.loading.value || payloadDraft.conflict.value) return;
  const originalBasis = basis(), target = { eventId: binding.value.event_id, version: binding.value.schema_version };
  submitting.value = true;
  try {
    const value: unknown = JSON.parse(payload.value); assertEventPayload(value); error.value = null;
    if (!await payloadDraft.flush()) throw new Error("事件载荷尚未写入本机");
    if (originalBasis !== basis() || graph.locked) return;
    if (await graph.submitEvent(target.eventId, target.version, value)) await refresh();
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "事件载荷 JSON 无效"; }
  finally { submitting.value = false; }
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
    <FormDraftNotice :message="declarationDraft.message.value" :conflict="declarationDraft.conflict.value"
      @restore="declarationDraft.restoreConflict" @discard="declarationDraft.discardConflict" />
    <label>事件绑定 JSON<textarea v-model="draft" rows="12" spellcheck="false" :disabled="graph.locked || applying || declarationDraft.loading.value || declarationDraft.conflict.value" /></label>
    <button type="button" :disabled="graph.locked || applying || !graph.document || declarationDraft.loading.value || declarationDraft.conflict.value" @click="apply">应用事件声明</button>
    <p>声明事件后，普通回合仅从显式执行根开始；事件处理目标及其依赖由绑定单独声明。</p>
    <header><strong>已发布事件</strong><button type="button" title="刷新事件目录" aria-label="刷新事件目录" :disabled="loading || !graph.session" @click="refresh"><RefreshCw :size="13" /></button></header>
    <FormDraftNotice :message="payloadDraft.message.value" :conflict="payloadDraft.conflict.value"
      @restore="payloadDraft.restoreConflict" @discard="payloadDraft.discardConflict" />
    <label>事件<select v-model="selected" :disabled="graph.locked || submitting || loading || payloadDraft.loading.value || payloadDraft.conflict.value"><option value="">选择事件版本</option>
      <option v-for="row in packet?.bindings ?? []" :key="key(row.event_id, row.schema_version)" :value="key(row.event_id, row.schema_version)">{{ row.display_name }} · {{ row.event_id }}@{{ row.schema_version }} · {{ row.audience }}</option>
    </select></label>
    <details v-if="binding"><summary>载荷 schema / 目标节点</summary><pre>{{ JSON.stringify({ payload_schema: binding.payload_schema, target_node_ids: binding.target_node_ids }, null, 2) }}</pre></details>
    <label>事件载荷 JSON<textarea v-model="payload" rows="6" spellcheck="false" :disabled="graph.locked || submitting || payloadDraft.loading.value || payloadDraft.conflict.value" /></label>
    <button type="button" :disabled="graph.locked || submitting || !packet?.can_submit || !binding || payloadDraft.loading.value || payloadDraft.conflict.value" @click="submit">提交局部事件</button>
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
