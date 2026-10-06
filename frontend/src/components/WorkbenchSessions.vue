<script setup lang="ts">
import { onMounted } from "vue";
import { Plus, RefreshCw, MessageSquare, AlertTriangle } from "lucide-vue-next";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";
const runtime = useWorkbenchRuntimeStore();
onMounted(() => void runtime.loadSessions());
const dateLabel = (value: string | null) => value
  ? new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "";
</script>
<template>
  <section class="workbench-sessions" aria-label="工作流会话">
    <header>
      <h3>会话</h3>
      <button title="刷新会话列表" aria-label="刷新会话列表" :disabled="runtime.sessionsLoading" @click="runtime.loadSessions()">
        <RefreshCw :size="14" :class="{ spinning: runtime.sessionsLoading }" />
      </button>
      <button title="新建会话" aria-label="新建会话" :disabled="!runtime.executable || !!runtime.busy || !!runtime.selectionPending" @click="runtime.createSession()">
        <Plus :size="15" />
      </button>
    </header>
    <p v-if="runtime.selectionPending" class="pending-note"><AlertTriangle :size="13" />会话选择待核实</p>
    <button v-for="row in runtime.pendingRequests.filter(row => !row.sessionId)" :key="row.requestId" class="pending-note unbound-request"
      @click="runtime.restoreUnboundSession()">
      <AlertTriangle :size="13" />未绑定请求待核实 <code>{{ row.requestId.slice(0, 8) }}</code>
    </button>
    <div class="session-list">
      <button v-for="row in runtime.sessions" :key="row.workflow_session_id" class="session-row"
        :class="{ active: runtime.sessionId === row.workflow_session_id }"
        :aria-pressed="runtime.sessionId === row.workflow_session_id"
        :title="row.workflow_session_id" :disabled="!runtime.executable || !!runtime.selectionPending"
        @click="runtime.selectSession(row.workflow_session_id)">
        <MessageSquare :size="15" />
        <span class="session-label"><code>{{ row.workflow_session_id.slice(0, 8) }}</code><small>{{ dateLabel(row.created_at) }}</small></span>
        <span v-if="runtime.pendingRequests.some(pending => pending.sessionId === row.workflow_session_id)" class="pending-marker" title="原请求待核实">待核实</span>
        <small v-else>{{ row.mode === "offline" ? "离线" : "DeepSeek" }}</small>
      </button>
      <p v-if="!runtime.sessions.length" class="empty">{{ runtime.sessionsLoading ? "读取中" : "暂无会话" }}</p>
    </div>
  </section>
</template>
<style scoped>
.workbench-sessions { border-top: 1px solid #3f3f45; min-height: 0; display: flex; flex-direction: column; color: #bfc4ce; }
header { display: flex; align-items: center; gap: 4px; padding: 10px 12px 6px; }
h3 { font-size: 12px; font-weight: 600; margin: 0; flex: 1; }
header button { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; padding: 0; border: 1px solid transparent; background: transparent; color: #a2a8b3; border-radius: 4px; }
button { cursor: pointer; }
button:disabled { cursor: default; opacity: .45; }
header button:hover:not(:disabled) { border-color: #48484d; background: #303036; }
.session-list { min-height: 0; max-height: 260px; overflow-y: auto; padding: 0 6px 6px; }
.session-row { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 46px; padding: 7px 8px; text-align: left; background: transparent; border: 1px solid transparent; border-radius: 4px; color: inherit; }
.session-row:hover:not(:disabled) { background: #2c2c31; }
.session-row.active { background: #35423b; border-color: #52675a; }
.session-label { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 3px; }
code { font-size: 11px; }
small { color: #8f98a5; font-size: 10px; white-space: nowrap; }
.pending-marker { color: #d5b781; font-size: 10px; }
.pending-note { display: flex; align-items: center; gap: 5px; padding: 0 12px; font-size: 11px; color: #d5b781; }
.unbound-request { background: transparent; border: 0; min-height: 30px; text-align: left; }
.empty { color: #8b919c; font-size: 12px; padding: 10px; margin: 0; }
.spinning { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
