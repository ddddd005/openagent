<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from "vue";
import { Check, GitBranch, RefreshCw } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { resolveGraphRunInputs, type GraphRunDetail } from "../domain/workflowGraph";
import GraphInformation from "./GraphInformation.vue";
import { contextMaintenanceDiagnosis } from "../domain/contextMaintenance";
import LorebookEvaluationFacts from "./LorebookEvaluationFacts.vue";
const graph = useWorkflowGraphStore();
const selected = ref<string | null>(null);
const detail = ref<GraphRunDetail | null>(null);
const error = ref<string | null>(null);
const loading = ref(false);
const history = computed(() => [...(graph.session?.inherited_history ?? []), ...(graph.session?.chains ?? [])]);
const candidates = computed(() => graph.session ? graph.candidates[graph.session.workflow_session_id]?.candidates ?? [] : []);
const chain = computed(() => detail.value?.chain ?? history.value.find(row => row.chain_run_id === selected.value));
const outputs = computed(() => detail.value?.outputs ?? graph.session?.outputs.filter(output => !selected.value || output.chain_run_id === selected.value) ?? []);
function inputEvidence(run: GraphRunDetail["node_runs"][number]) {
  return { inputs: run.input_values, referenced_inputs: detail.value ? resolveGraphRunInputs(detail.value, run) : {},
    references: run.input_refs, controls: run.control_refs ?? [], reads: run.reads };
}
let generation = 0;
watch(() => graph.session?.workflow_session_id, () => { selected.value = null; });
watch(() => [graph.session?.workflow_session_id, graph.session?.revision, graph.session?.head_revision], () => {
  if (graph.session?.can_submit) void graph.loadCandidates();
}, { immediate: true });
watch(() => [graph.session?.workflow_session_id, selected.value,
  history.value.find(row => row.chain_run_id === selected.value)?.revision] as const, async ([sessionId, chainId]) => {
  const version = ++generation; detail.value = null; error.value = null; loading.value = false;
  if (!sessionId || !chainId) return;
  loading.value = true;
  try {
    const record = await graph.management.queries.run(sessionId, chainId);
    if (version === generation) detail.value = record;
  }
  catch (failure) { if (version === generation) error.value = failure instanceof Error ? failure.message : "历史读取失败"; }
  finally { if (version === generation) loading.value = false; }
});
onUnmounted(() => { generation++; });
</script>
<template>
  <section class="graph-history" aria-label="工作流运行历史">
    <aside><header>运行历史</header><button v-for="run in history" :key="run.chain_run_id" type="button" :class="{ active: selected === run.chain_run_id }" @click="selected = run.chain_run_id"><code>{{ run.chain_run_id.slice(0,8) }}</code><span>{{ run.status }}<template v-if="run.workflow_session_id && run.workflow_session_id !== graph.session?.workflow_session_id"> · 复制来源</template></span></button></aside>
    <main>
      <section class="graph-candidates" aria-label="工作流候选"><header><span>候选</span><button type="button" title="刷新候选" aria-label="刷新候选" :disabled="graph.locked || !graph.session" @click="graph.loadCandidates()"><RefreshCw :size="14" /></button></header>
        <div v-for="candidate in candidates" :key="candidate.candidate_id" class="graph-candidate"><button class="candidate-result" type="button" :class="{ active: selected === candidate.chain_run_id }" @click="selected = candidate.chain_run_id"><code>{{ candidate.chain_run_id.slice(0,8) }}</code><span>{{ candidate.selected ? '当前候选' : '候选' }} · 数据 r{{ candidate.data_revision }}</span><small :title="candidate.source_workflow_definition_id">定义 {{ candidate.source_workflow_definition_id.slice(0,8) }} r{{ candidate.source_definition_revision }}</small><small v-if="candidate.diagnostic" class="error">{{ candidate.diagnostic.message }}</small></button><button type="button" title="恢复候选完整状态" aria-label="恢复候选完整状态" :disabled="graph.locked || !graph.session?.can_submit || !candidate.can_select" @click="graph.changeCandidate(candidate.candidate_id)"><Check :size="14" /></button><button type="button" title="从候选新建分叉会话" aria-label="从候选新建分叉会话" :disabled="graph.locked || !graph.session?.can_submit || !candidate.can_select" @click="graph.changeCandidate(candidate.candidate_id, true)"><GitBranch :size="14" /></button></div>
        <p v-if="!candidates.length">暂无已完成候选</p>
      </section>
      <p v-if="loading">读取中</p><p v-if="error" class="error">{{ error }}</p><p v-if="chain">定义 r{{ chain.definition_revision }} · {{ chain.status }} · 会话 {{ chain.workflow_session_id }}</p>
      <p v-if="contextMaintenanceDiagnosis(chain?.diagnostic)" class="error" role="status">{{ contextMaintenanceDiagnosis(chain?.diagnostic) }}</p>
      <pre v-if="chain?.diagnostic" class="error">{{ JSON.stringify(chain.diagnostic, null, 2) }}</pre>
      <GraphInformation v-if="selected && graph.session" :session-id="graph.session.workflow_session_id" :chain-id="selected" initial-kind="information_binding" />
      <section v-for="run in detail?.node_runs ?? []" :key="run.run_id" class="node-history">
        <header>{{ run.node_binding_id.slice(0,8) }} · {{ run.status }}</header>
        <LorebookEvaluationFacts :reads="run.reads" />
        <details><summary>输入与读取依据</summary><pre>{{ JSON.stringify(inputEvidence(run), null, 2) }}</pre></details>
        <details v-if="run.effects.length"><summary>会话写入</summary><pre>{{ JSON.stringify(run.effects, null, 2) }}</pre></details>
        <p v-if="contextMaintenanceDiagnosis(run.diagnostic)" class="error" role="status">{{ contextMaintenanceDiagnosis(run.diagnostic) }}</p>
        <pre v-if="run.diagnostic" class="error">{{ JSON.stringify(run.diagnostic, null, 2) }}</pre>
      </section>
      <section v-for="output in outputs" :key="output.output_id"><header>{{ output.node_binding_id.slice(0,8) }} / {{ output.port_id }}</header><pre>{{ JSON.stringify(output.payload, null, 2) }}</pre></section><p v-if="!outputs.length && !loading">暂无结果</p>
    </main>
  </section>
</template>
<style scoped>
.graph-history { display:flex; flex:1; height:100%; min-width:0; min-height:0; background:#252529; }
aside { width:220px; flex:0 0 220px; overflow:auto; border-right:1px solid #45454c; }
header { padding:12px; font-size:12px; border-bottom:1px solid #43434a; }
button { display:flex; flex-direction:column; gap:5px; text-align:left; width:100%; padding:12px; font-size:11px; border-bottom:1px solid #3d3d44; }
button.active { background:#35423b; }
main { flex:1; min-width:0; overflow:auto; padding:16px; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; line-height:1.6; padding:12px; }
p { color:#a3a3ad; font-size:12px; }
.error { color:#e0a0a0; }
details { padding:10px; font-size:11px; color:#b3c8be; border-bottom:1px solid #404047; }
.node-history { margin-bottom:12px; }
.graph-candidates { border-bottom:1px solid #45454c; padding-bottom:12px; margin-bottom:12px; }
.graph-candidates > header { display:flex; justify-content:space-between; align-items:center; padding:0 0 8px; }
.graph-candidates > header button,.graph-candidate > button:not(.candidate-result) { width:28px; flex:0 0 28px; padding:5px; align-items:center; justify-content:center; border:0; }
.graph-candidate { display:flex; align-items:center; gap:4px; border-bottom:1px solid #414148; }
.candidate-result { flex:1; min-width:0; border:0; padding:8px; }
.candidate-result span,.candidate-result small { overflow-wrap:anywhere; }
button:disabled { opacity:.4; }
</style>
