<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from "vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { graphUuid, resolveGraphRunInputs, type GraphDocument, type GraphRunDetail } from "../domain/workflowGraph";
import GraphInformation from "./GraphInformation.vue";
import { contextMaintenanceDiagnosis } from "../domain/contextMaintenance";
import LorebookEvaluationFacts from "./LorebookEvaluationFacts.vue";
import GraphResult from "./GraphResult.vue";
import { modelPresentationJson } from "../domain/modelPresentation";
import { nodeDisplayTitle } from "../domain/nodeCatalog";
import { resultStatusLabel } from "../domain/resultPresentation";
const graph = useWorkflowGraphStore();
const selected = ref<string | null>(null);
const detail = ref<GraphRunDetail | null>(null);
const error = ref<string | null>(null);
const loading = ref(false);
const informationOpen = ref(false);
const frozenDefinition = ref<GraphDocument | null>(null), definitionError = ref("");
const history = computed(() => [...(graph.session?.inherited_history ?? []), ...(graph.session?.chains ?? [])]);
const chain = computed(() => detail.value?.chain ?? history.value.find(row => row.chain_run_id === selected.value));
const outputs = computed(() => detail.value?.outputs ?? graph.session?.outputs.filter(output => !selected.value || output.chain_run_id === selected.value) ?? []);
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
      <p v-if="loading">读取中</p><p v-if="error" class="error" role="status">{{ error }}</p><p v-if="definitionError" class="error" role="status">{{ definitionError }}；未使用当前编辑图替代。</p><p v-if="chain">工作流修订 r{{ chain.definition_revision }} · {{ resultStatusLabel(chain.status) }}</p>
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
button:disabled { opacity:.4; }
h3 { font-size:11px; font-weight:500; margin:8px 0; }
</style>
