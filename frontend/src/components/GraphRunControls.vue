<script setup lang="ts">
import { MoreHorizontal, Pause, Play, RefreshCw, RotateCcw, SquarePen, X } from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { graphObject } from "../domain/workflowGraph";
const graph = useWorkflowGraphStore();
const editing = ref(false);
const input = ref("");
const menuOpen = ref(false);
const additionalRequests = ref(1);
const additionalAttempts = ref(4);
const agentActions = computed(() => graph.session?.available_actions?.filter(action => ["extend_budget", "retry_archive", "retry_acceptance", "retry_failed_node"].includes(action)) ?? []);
const activeAgent = computed(() => graph.session?.nodes.find(node => node.budget
  && ["paused", "budget_exhausted", "archive_failed"].includes(node.status)));
watch(() => graph.session?.workflow_session_id, () => { editing.value = false; menuOpen.value = false; });
function openInput() { input.value = JSON.stringify(graph.active?.external_inputs ?? {}, null, 2); editing.value = true; }
function apply(event: Event) {
  const form = event.target as HTMLFormElement;
  const text = form.querySelector("textarea")!;
  try { const value = JSON.parse(input.value); if (!graphObject(value)) throw new Error();
    graph.setInputs(value); text.setCustomValidity(""); editing.value = false; }
  catch { text.setCustomValidity("输入须为 JSON 对象"); text.reportValidity(); }
}
</script>
<template>
  <div class="graph-run-controls">
    <span v-if="graph.session" :title="graph.session.workflow_session_id">{{ graph.session.workflow_session_id.slice(0,8) }}</span>
    <button type="button" title="本次输入" aria-label="编辑本次输入" :disabled="graph.locked" @click="openInput"><SquarePen :size="14" /></button>
    <button type="button" class="graph-primary" :class="{ paused: graph.primaryAction === 'pause' }" :disabled="graph.locked || !!graph.session && !graph.session.can_submit && graph.primaryAction === 'start'" @click="graph.submitPrimary()">
      <Pause v-if="graph.primaryAction === 'pause'" :size="14" fill="currentColor" /><Play v-else :size="14" fill="currentColor" />{{ graph.statusLabel }}
    </button>
    <button type="button" title="刷新运行" aria-label="刷新运行" :disabled="!!graph.busy" @click="graph.refresh()"><RefreshCw :size="14" /></button>
    <button v-if="graph.session?.available_actions?.includes('close')" type="button" title="关闭本次运行" aria-label="关闭本次运行" :disabled="graph.locked" @click="graph.closeRun()"><X :size="14" /></button>
    <button v-if="graph.pending" type="button" title="核实原请求" aria-label="核实原请求" :disabled="!!graph.busy" @click="graph.reconcile()"><RotateCcw :size="14" /></button>
    <div v-if="agentActions.length" class="graph-agent-menu-anchor"><button type="button" title="运行操作" aria-label="运行操作" :aria-expanded="menuOpen" @click="menuOpen = !menuOpen"><MoreHorizontal :size="15" /></button>
      <section v-if="menuOpen" class="graph-agent-menu" aria-label="运行操作"><strong v-if="activeAgent">{{ activeAgent.label }} · {{ activeAgent.run_id?.slice(0,8) }}</strong>
        <template v-if="agentActions.includes('extend_budget')"><label>请求增额<input v-model.number="additionalRequests" type="number" min="0" max="64" step="1" /></label><label>尝试增额<input v-model.number="additionalAttempts" type="number" min="0" max="256" step="1" /></label><button type="button" :disabled="graph.locked" @click="graph.submitAgentAction('extend_budget', additionalRequests, additionalAttempts)">增加预算</button></template>
        <button v-if="agentActions.includes('retry_archive')" type="button" :disabled="graph.locked" @click="graph.submitAgentAction('retry_archive')">归档重试</button>
        <button v-if="agentActions.includes('retry_acceptance')" type="button" :disabled="graph.locked" @click="graph.submitAgentAction('retry_acceptance')">结果接纳重试</button>
        <button v-if="agentActions.includes('retry_failed_node')" type="button" :disabled="graph.locked" title="使用本次运行的原输入重试失败节点，保留已成功结果" @click="graph.submitAgentAction('retry_failed_node')">重试失败节点</button>
      </section>
    </div>
  </div>
  <Teleport to="body"><div v-if="editing" class="graph-input-shade" @click.self="editing = false"><form class="graph-input-dialog" @submit.prevent="apply"><header><strong>本次输入</strong><button type="button" title="关闭" aria-label="关闭输入" @click="editing = false"><X :size="16" /></button></header><textarea v-model="input" rows="8" aria-label="具名输入 JSON" /><footer><button type="submit">应用</button></footer></form></div></Teleport>
</template>
<style scoped>
.graph-run-controls { display:flex; align-items:center; gap:5px; }
span { font-size:10px; color:#aaaab4; }
button { display:inline-flex; align-items:center; justify-content:center; gap:5px; height:26px; min-width:26px; border:1px solid #4a4a52; border-radius:4px; background:#2a2a2f; color:#b7b7bf; font-size:12px; }
.graph-primary { min-width:70px; padding:0 8px; color:#7fc89f; }
.paused { color:#e4c764; border-color:#817247; }
.graph-agent-menu-anchor { position:relative; }
.graph-agent-menu { position:absolute; right:0; top:31px; z-index:30; display:grid; gap:8px; width:200px; padding:10px; border:1px solid #50505a; border-radius:4px; background:#29292f; font-size:11px; }
.graph-agent-menu strong { font-weight:500; overflow-wrap:anywhere; }
.graph-agent-menu label { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.graph-agent-menu input { width:60px; padding:4px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; }
:disabled { opacity:.5; }
.graph-input-shade { position:fixed; inset:0; z-index:150; display:grid; place-items:center; background:#11111166; }
.graph-input-dialog { width:440px; padding:15px; border:1px solid #54545b; border-radius:5px; background:#2c2c32; color:#dedee5; }
header { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; font-size:13px; }
textarea { width:100%; box-sizing:border-box; padding:8px; border:1px solid #52525a; background:#202024; color:#dedee5; font:12px Consolas,monospace; resize:vertical; }
footer { display:flex; justify-content:flex-end; margin-top:8px; }
footer button { padding:0 12px; }
</style>
