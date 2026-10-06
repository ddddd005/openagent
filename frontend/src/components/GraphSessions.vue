<script setup lang="ts">
import { Plus, RefreshCw } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
const graph = useWorkflowGraphStore();
const workspace = useWorkspaceStore();
</script>
<template>
  <section class="graph-sessions" aria-label="工作流会话">
    <header><h3>会话</h3><button type="button" title="刷新会话" aria-label="刷新会话" :disabled="!!graph.busy" @click="graph.refresh()"><RefreshCw :size="14" /></button><button type="button" title="新建会话" aria-label="新建会话" :disabled="graph.locked" @click="graph.createSession()"><Plus :size="15" /></button></header>
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
</style>
