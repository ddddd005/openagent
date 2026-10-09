<script setup lang="ts">
import { Plus, RefreshCw, Check, X } from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
const graph = useWorkflowGraphStore();
const workspace = useWorkspaceStore();
const persistence = useWorkbenchPersistenceStore(), confirming = ref(false);
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
    <header><h3>会话</h3><button type="button" title="刷新会话" aria-label="刷新会话" :disabled="!!graph.busy" @click="graph.refresh()"><RefreshCw :size="14" /></button><button type="button" title="新建会话" aria-label="新建会话" :disabled="graph.locked" @click="graph.createSession()"><Plus :size="15" /></button></header>
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
    <div class="graph-session-list"><button v-for="session in graph.sessions[workspace.activeWorkflowId] ?? []" :key="session.workflow_session_id" class="graph-session-row" :class="{ active: graph.active?.session_id === session.workflow_session_id }" type="button" :disabled="graph.locked" @click="graph.selectSession(session.workflow_session_id)"><code>{{ session.workflow_session_id.slice(0,8) }}</code><small :title="session.workflow_definition_id">{{ session.status }} · 定义 {{ session.workflow_definition_id.slice(0,8) }} r{{ session.definition_revision }}</small></button><p v-if="!graph.sessions[workspace.activeWorkflowId]?.length">暂无会话</p></div>
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
small,p { font-size:10px; color:#a1a1ac; }
p { padding:3px 5px; }
.revision-state { display:flex; flex-wrap:wrap; align-items:center; gap:6px; padding:5px 10px; font-size:10px; color:#b9bec6; }
.revision-state p { margin:0; line-height:1.5; overflow-wrap:anywhere; color:#e4c85f; }
.revision-state button { display:inline-flex; align-items:center; gap:4px; padding:4px; }
</style>
