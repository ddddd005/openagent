<script setup lang="ts">
import { Archive, Plus, RefreshCw, Check, Trash2, X } from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { useSessionMetadata } from "../application/sessionMetadata";
import { resultStatusLabel } from "../domain/resultPresentation";
const graph = useWorkflowGraphStore();
const workspace = useWorkspaceStore();
const metadata = useSessionMetadata(), name = ref("");
const selectedMetadata = computed(() => graph.session ? metadata.forSession(graph.session.workflow_session_id) : null);
watch(() => graph.sessions[workspace.activeWorkflowId] ?? [], sessions => {
  for (const session of sessions) metadata.forSession(session.workflow_session_id).observe({
    schema_version: 1, kind: "workflow.session-metadata",
    workflow_definition_id: session.workflow_definition_id, workflow_session_id: session.workflow_session_id,
    name: session.name ?? "", metadata_sequence: session.metadata_sequence ?? 0,
  });
}, { immediate: true, deep: true });
watch([() => graph.session?.workflow_session_id, () => graph.session?.name, () => graph.session?.metadata_sequence], () => {
  const session = graph.session, entry = selectedMetadata.value;
  if (session && entry) {
    entry.observe({ schema_version: 1, kind: "workflow.session-metadata",
      workflow_definition_id: session.workflow_definition_id, workflow_session_id: session.workflow_session_id,
      name: session.name ?? "", metadata_sequence: session.metadata_sequence ?? 0 });
    if (typeof window !== "undefined") void entry.load();
  }
}, { immediate: true });
watch([() => graph.session?.workflow_session_id, () => selectedMetadata.value?.record.value?.name,
  () => selectedMetadata.value?.record.value?.metadata_sequence], () => {
  name.value = selectedMetadata.value?.record.value?.name ?? "";
}, { immediate: true });
const nameLocked = computed(() => !selectedMetadata.value || selectedMetadata.value.locked.value
  || !selectedMetadata.value.record.value || selectedMetadata.value.loading.value || !!selectedMetadata.value.readError.value
  || !!graph.session?.archived || !!workspace.activeWorkflow.archived);
async function saveName() {
  if (!nameLocked.value) await selectedMetadata.value?.save(name.value);
}
function sessionName(sessionId: string, fallback: string) {
  return metadata.forSession(sessionId).record.value?.name || fallback;
}
async function abandonNameRequest() {
  const target = selectedMetadata.value;
  if (target) await target.abandon(window.confirm("归档并解除原会话名称请求的锁定？这不会撤销已发生的保存，也不会重新提交。"));
}
const persistence = useWorkbenchPersistenceStore(), confirming = ref(false);
const showArchived = ref(false);
const listedSessions = computed(() => (graph.sessions[workspace.activeWorkflowId] ?? [])
  .filter(row => showArchived.value || !row.archived));
async function manageSession(action: "archive" | "delete") {
  const sessionId = graph.session?.workflow_session_id;
  if (!sessionId || !window.confirm(action === "archive"
    ? "归档此会话并停止其运行？归档历史仅供查看，不能恢复执行。"
    : "删除此会话及所属运行和历史？此操作不能撤销。")) return;
  await graph.manageSession(sessionId, action);
}
const mismatch = computed(() => !!graph.session && (graph.session.definition_revision !== graph.active?.saved_revision
  || graph.session.workflow_definition_id !== graph.document?.workflow_definition_id));
const basis = computed(() => JSON.stringify([workspace.activeWorkflowId, graph.active?.saved_revision,
  graph.session?.workflow_session_id, graph.session?.revision]));
watch(basis, () => { confirming.value = false; });
async function adopt() {
  const fixed = basis.value;
  if (await persistence.flushAll() && confirming.value && basis.value === fixed
    && await graph.adoptRevision()) confirming.value = false;
}
</script>
<template>
  <section class="graph-sessions" aria-label="工作流会话">
    <header><h3>会话</h3>
      <button type="button" title="显示已归档会话" aria-label="显示已归档会话" :aria-pressed="showArchived" @click="showArchived = !showArchived"><Archive :size="14" /></button>
      <button type="button" title="刷新会话" aria-label="刷新会话" :disabled="!!graph.busy" @click="graph.refresh()"><RefreshCw :size="14" /></button>
      <button v-if="graph.session && !graph.session.archived" type="button" title="归档当前会话" aria-label="归档当前会话"
        :disabled="!!graph.busy || !!graph.pending" @click="manageSession('archive')"><Archive :size="14" /></button>
      <button v-if="graph.session" type="button" title="删除当前会话" aria-label="删除当前会话"
        :disabled="!!graph.busy || !!graph.pending" @click="manageSession('delete')"><Trash2 :size="14" /></button>
      <button type="button" title="新建会话" aria-label="新建会话" :disabled="graph.locked" @click="graph.createSession()"><Plus :size="15" /></button>
    </header>
    <div v-if="graph.active" class="revision-state" aria-label="工作流修订状态">
      <span>草稿基准 r{{ graph.active.saved_revision }} · 已发布 r{{ graph.active.latest_published_revision ?? graph.active.saved_revision }}</span>
      <span v-if="graph.session">当前会话 r{{ graph.session.definition_revision }}</span>
      <button type="button" title="核对最新已发布修订" aria-label="核对最新已发布修订" :disabled="graph.locked || !graph.active.saved_revision" @click="graph.refreshPublishedRevision()"><RefreshCw :size="13" /></button>
      <p v-if="(graph.active.latest_published_revision ?? 0) > graph.active.saved_revision" role="status">另一页面已发布新修订，当前草稿基准仍保留；保存冲突不会覆盖对方。</p>
      <button v-if="mismatch && !confirming" type="button" :disabled="graph.locked" @click="confirming = true"><Check :size="13" />采用已保存修订</button>
      <div v-if="confirming && mismatch" role="alert">
        <p>会话将迁移到当前保存修订。配置不兼容的节点可能要求明确重建状态；旧运行与编辑草稿保留。</p>
        <button type="button" :disabled="graph.locked" @click="adopt"><Check :size="13" />确认采用</button>
        <button type="button" aria-label="取消采用修订" @click="confirming = false"><X :size="13" /></button>
      </div>
    </div>
    <div class="graph-session-list"><button v-for="(session, index) in listedSessions" :key="session.workflow_session_id" class="graph-session-row" :class="{ active: graph.active?.session_id === session.workflow_session_id }" type="button" :disabled="!!graph.busy || !!graph.pending" @click="graph.selectSession(session.workflow_session_id)"><strong>{{ sessionName(session.workflow_session_id, `会话 ${index + 1}`) }}</strong><small>{{ session.archived ? '已归档 · 只读' : resultStatusLabel(session.status) }} · r{{ session.definition_revision }}</small></button><p v-if="!listedSessions.length">暂无会话</p></div>
    <details v-if="graph.session && selectedMetadata" class="session-details">
      <summary>会话名称与技术详情</summary>
      <label>会话名称<input v-model="name" aria-label="会话名称" :disabled="nameLocked" @blur="saveName" /></label>
      <button type="button" title="刷新会话名称" aria-label="刷新会话名称" :disabled="selectedMetadata.busy.value || selectedMetadata.loading.value" @click="selectedMetadata.refresh()"><RefreshCw :size="13" /></button>
      <p v-if="selectedMetadata.pending.value" role="status">会话名称保存结果待核实</p>
      <div v-if="selectedMetadata.pending.value" class="name-request-actions">
        <button type="button" title="核实原会话名称请求" aria-label="核实原会话名称请求" :disabled="selectedMetadata.busy.value" @click="selectedMetadata.reconcile()"><RefreshCw :size="13" /></button>
        <button type="button" title="归档原会话名称请求" aria-label="归档原会话名称请求" :disabled="selectedMetadata.busy.value" @click="abandonNameRequest"><Archive :size="13" /></button>
      </div>
      <p v-if="selectedMetadata.error.value || selectedMetadata.readError.value" role="alert">{{ selectedMetadata.error.value || selectedMetadata.readError.value }}</p>
      <code>{{ graph.session.workflow_session_id }}</code><code>{{ graph.session.workflow_definition_id }} · r{{ graph.session.definition_revision }}</code>
    </details>
  </section>
</template>
<style scoped>
.graph-sessions { border-top:1px solid #424249; margin-top:auto; min-height:110px; }
header { display:flex; align-items:center; gap:3px; padding:8px 10px; }
h3 { flex:1; margin:0; font-size:12px; font-weight:500; }
button { min-height:25px; min-width:25px; border-radius:3px; }
button:not(:disabled):hover { background:#36363e; }
:disabled { opacity:.45; }
.graph-session-list { max-height:250px; overflow:auto; padding:0 6px 8px; }
.graph-session-row { display:flex; flex-direction:column; align-items:flex-start; width:100%; gap:5px; padding:8px; margin-bottom:3px; text-align:left; border:1px solid transparent; }
.graph-session-row.active { background:#35423b; border-color:#52675a; }
code { font-size:11px; }
.graph-session-row strong { font-size:11px; overflow-wrap:anywhere; } .session-details { padding:8px 10px; font-size:11px; display:grid; gap:7px; } .session-details code { display:block; overflow-wrap:anywhere; padding:5px 0; }
.session-details label { display:grid; gap:5px; }
.session-details input { min-width:0; width:100%; box-sizing:border-box; padding:5px; }
.session-details button { width:max-content; }
.name-request-actions { display:flex; gap:5px; }
small,p { font-size:10px; color:#a1a1ac; }
p { padding:3px 5px; }
.revision-state { display:flex; flex-wrap:wrap; align-items:center; gap:6px; padding:5px 10px; font-size:10px; color:#b9bec6; }
.revision-state p { margin:0; line-height:1.5; overflow-wrap:anywhere; color:#e4c85f; }
.revision-state button { display:inline-flex; align-items:center; gap:4px; padding:4px; }
</style>
