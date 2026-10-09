<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from "vue";
import { Check, GitBranch, RefreshCw, X } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { graphUuid, resolveGraphRunInputs, type GraphDocument, type GraphRunDetail } from "../domain/workflowGraph";
import GraphInformation from "./GraphInformation.vue";
import { contextMaintenanceDiagnosis } from "../domain/contextMaintenance";
import LorebookEvaluationFacts from "./LorebookEvaluationFacts.vue";
import GraphResult from "./GraphResult.vue";
import { modelPresentationJson } from "../domain/modelPresentation";
import { nodeDisplayTitle } from "../domain/nodeCatalog";
import { resultStatusLabel } from "../domain/resultPresentation";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
const graph = useWorkflowGraphStore();
const persistence = useWorkbenchPersistenceStore();
const selected = ref<string | null>(null);
const detail = ref<GraphRunDetail | null>(null);
const error = ref<string | null>(null);
const loading = ref(false);
const informationOpen = ref(false);
const frozenDefinition = ref<GraphDocument | null>(null), definitionError = ref(""), actionError = ref("");
const confirming = ref<{ id: string; fork: boolean; basis: string } | null>(null);
const history = computed(() => [...(graph.session?.inherited_history ?? []), ...(graph.session?.chains ?? [])]);
const candidates = computed(() => graph.session ? graph.candidates[graph.session.workflow_session_id]?.candidates ?? [] : []);
const chain = computed(() => detail.value?.chain ?? history.value.find(row => row.chain_run_id === selected.value));
const outputs = computed(() => detail.value?.outputs ?? graph.session?.outputs.filter(output => !selected.value || output.chain_run_id === selected.value) ?? []);
const candidateBasis = computed(() => JSON.stringify([graph.session?.workflow_session_id, graph.session?.revision,
  graph.session?.data_revision, graph.session?.head_revision, candidates.value]));
watch(candidateBasis, () => { confirming.value = null; });
function confirmCandidate(id: string, fork = false) { confirming.value = { id, fork, basis: candidateBasis.value }; actionError.value = ""; }
async function applyCandidate() {
  const fixed = confirming.value;
  if (!fixed || graph.locked || !await persistence.flushAll()) { actionError.value = "当前操作或本机保存未就绪"; return; }
  if (confirming.value !== fixed || candidateBasis.value !== fixed.basis) { actionError.value = "会话依据已变化，请重新选择候选"; return; }
  if (await graph.changeCandidate(fixed.id, fixed.fork)) confirming.value = null;
  else actionError.value = "操作未完成，请查看运行诊断或核实原请求；未自动重试";
}
function title(id: string) {
  const node = frozenDefinition.value?.nodes.find(node => node.node_binding_id === id);
  return node ? nodeDisplayTitle(node) : "历史节点（定义未读取）";
}
function displayedInputs(run: GraphRunDetail["node_runs"][number]) {
  const referenced = detail.value ? resolveGraphRunInputs(detail.value, run) : {};
  return { ...run.input_values, ...Object.fromEntries(Object.entries(referenced).map(([key, rows]) =>
    [key, rows.map(row => "payload" in row ? row.payload : { availability: "unavailable" })])) };
}
function inputEvidence(run: GraphRunDetail["node_runs"][number]) {
  return { inputs: run.input_values, referenced_inputs: detail.value ? resolveGraphRunInputs(detail.value, run) : {},
    references: run.input_refs, controls: run.control_refs ?? [], reads: run.reads };
}
let generation = 0;
watch(() => graph.session?.workflow_session_id, () => {
  selected.value = graph.session?.selected_chain_run_id ?? history.value.at(-1)?.chain_run_id ?? null;
}, { immediate: true });
watch(() => [graph.session?.workflow_session_id, graph.session?.revision, graph.session?.head_revision], () => {
  if (graph.session?.can_submit) void graph.loadCandidates();
}, { immediate: true });
watch(() => [graph.session?.workflow_session_id, selected.value,
  history.value.find(row => row.chain_run_id === selected.value)?.revision] as const, async ([sessionId, chainId]) => {
  const version = ++generation; informationOpen.value = false; detail.value = null; frozenDefinition.value = null; definitionError.value = ""; error.value = null; loading.value = false;
  if (!sessionId || !chainId) return;
  loading.value = true;
  try {
    const record = await graph.management.queries.run(sessionId, chainId);
    if (version !== generation) return;
    detail.value = record;
    const identity = record.chain.workflow_definition_id, revision = record.chain.definition_revision;
    try {
      if (!graphUuid(identity) || !Number.isSafeInteger(revision) || Number(revision) < 1) throw new Error("历史记录缺少精确冻结定义身份");
      const document = await graph.management.queries.definition(identity, revision);
      if (version === generation) frozenDefinition.value = document;
    } catch (failure) { if (version === generation) definitionError.value = failure instanceof Error ? failure.message : "历史定义版本缺失"; }
  }
  catch (failure) { if (version === generation) error.value = failure instanceof Error ? failure.message : "历史读取失败"; }
  finally { if (version === generation) loading.value = false; }
}, { immediate: true });
onUnmounted(() => { generation++; });
</script>
<template>
  <section class="graph-history" aria-label="工作流运行历史">
    <aside><header>运行历史</header><button v-for="(run, index) in history" :key="run.chain_run_id" type="button" :class="{ active: selected === run.chain_run_id }" @click="selected = run.chain_run_id"><strong>第 {{ index + 1 }} 次运行</strong><span>{{ resultStatusLabel(run.status) }}<template v-if="run.workflow_session_id && run.workflow_session_id !== graph.session?.workflow_session_id"> · 复制来源</template></span></button></aside>
    <main>
      <section class="graph-candidates" aria-label="工作流候选"><header><span>候选</span><button type="button" title="刷新候选" aria-label="刷新候选" :disabled="graph.locked || !graph.session" @click="graph.loadCandidates()"><RefreshCw :size="14" /></button></header>
        <div v-for="(candidate, index) in candidates" :key="candidate.candidate_id" class="graph-candidate"><button class="candidate-result" type="button" :class="{ active: selected === candidate.chain_run_id }" @click="selected = candidate.chain_run_id"><strong>查看候选 {{ index + 1 }}</strong><span>{{ candidate.selected ? '当前候选' : '候选' }} · 数据 r{{ candidate.data_revision }}</span><small>工作流修订 r{{ candidate.source_definition_revision }}</small><small v-if="candidate.diagnostic" class="error">{{ candidate.diagnostic.message }}</small></button><button type="button" title="恢复候选完整状态" aria-label="恢复候选完整状态" :disabled="graph.locked || !graph.session?.can_submit || !candidate.can_select" @click="confirmCandidate(candidate.candidate_id)"><Check :size="14" /></button><button type="button" title="从候选新建分叉会话" aria-label="从候选新建分叉会话" :disabled="graph.locked || !graph.session?.can_submit || !candidate.can_select" @click="confirmCandidate(candidate.candidate_id, true)"><GitBranch :size="14" /></button></div>
        <div v-if="confirming" class="candidate-confirmation" role="alert"><p>{{ confirming.fork ? '将创建并选中新会话，原会话和编辑草稿保留；不会自动运行。' : '将恢复当前会话的完整状态，包括会话数据、私有状态和历史定义；不会覆盖编辑草稿或重新运行工具。' }}</p><button type="button" :disabled="graph.locked" @click="applyCandidate"><Check :size="14" />{{ confirming.fork ? '确认新建分叉会话' : '确认完整恢复' }}</button><button type="button" aria-label="取消历史操作" @click="confirming = null"><X :size="14" /></button></div>
        <p v-if="!candidates.length">暂无已完成候选</p>
      </section>
      <p v-if="loading">读取中</p><p v-if="error" class="error" role="status">{{ error }}</p><p v-if="definitionError" class="error" role="status">{{ definitionError }}；未使用当前编辑图替代。</p><p v-if="actionError" class="error" role="status">{{ actionError }}</p><p v-if="chain">工作流修订 r{{ chain.definition_revision }} · {{ resultStatusLabel(chain.status) }}</p>
      <details v-if="chain"><summary>运行技术详情</summary><pre>{{ modelPresentationJson(chain) }}</pre></details>
      <p v-if="contextMaintenanceDiagnosis(chain?.diagnostic)" class="error" role="status">{{ contextMaintenanceDiagnosis(chain?.diagnostic) }}</p>
      <pre v-if="chain?.diagnostic" class="error">{{ JSON.stringify(chain.diagnostic, null, 2) }}</pre>
      <details v-if="selected && graph.session" @toggle="informationOpen = ($event.target as HTMLDetailsElement).open"><summary>登记与读取依据</summary><GraphInformation v-if="informationOpen" :session-id="graph.session.workflow_session_id" :chain-id="selected" initial-kind="information_binding" /></details>
      <section v-for="run in detail?.node_runs ?? []" :key="run.run_id" class="node-history">
        <header>{{ title(run.node_binding_id) }} · {{ resultStatusLabel(run.status) }}</header>
        <LorebookEvaluationFacts :reads="run.reads" />
        <section v-for="(value, port) in displayedInputs(run)" :key="port"><h3>输入 · {{ port }}</h3><GraphResult :value="value" /></section>
        <details><summary>输入与读取依据</summary><pre>{{ modelPresentationJson(inputEvidence(run)) }}</pre></details>
        <details v-if="run.effects.length"><summary>会话写入</summary><pre>{{ modelPresentationJson(run.effects) }}</pre></details>
        <p v-if="contextMaintenanceDiagnosis(run.diagnostic)" class="error" role="status">{{ contextMaintenanceDiagnosis(run.diagnostic) }}</p>
        <pre v-if="run.diagnostic" class="error">{{ JSON.stringify(run.diagnostic, null, 2) }}</pre>
      </section>
      <section v-for="output in outputs" :key="output.output_id"><header>{{ title(output.node_binding_id) }} / {{ output.port_id }}</header><GraphResult :value="output.payload" /></section><p v-if="!outputs.length && !loading">暂无结果</p>
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
.candidate-confirmation { display:flex; flex-wrap:wrap; gap:6px; padding:8px; border-left:2px solid #baae74; }
.candidate-confirmation p { flex-basis:100%; overflow-wrap:anywhere; }
.candidate-confirmation button { width:auto; flex-direction:row; align-items:center; padding:6px; border:1px solid #505059; border-radius:3px; }
h3 { font-size:11px; font-weight:500; margin:8px 0; }
</style>
