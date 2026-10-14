<script setup lang="ts">
import { MoreHorizontal, Pause, Play, RefreshCw, RotateCcw, SquarePen, X } from "lucide-vue-next";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { graphObject } from "../domain/workflowGraph";
import { readGraphInputDeclarations, type GraphInputDeclarations } from "../adapters/graphInputDeclarationsApi";
const graph = useWorkflowGraphStore();
const editing = ref(false);
const input = ref("");
const menuOpen = ref(false);
const applying = ref(false);
const declarations = ref<GraphInputDeclarations | null>(null), loadingInputs = ref(false), inputError = ref("");
const parsedInput = computed(() => { try { const value = JSON.parse(input.value); return graphObject(value) ? value : null; } catch { return null; } });
function plainText(name: string) {
  const value = parsedInput.value?.[name];
  return value === undefined || typeof value === "string" ? value ?? ""
    : graphObject(value) && value.kind === "workflow.text" && value.schema_version === 2 && typeof value.text === "string" ? value.text : null;
}
function editText(name: string, event: Event) {
  if (!parsedInput.value) return;
  const text = (event.target as HTMLTextAreaElement).value, old = parsedInput.value[name];
  input.value = JSON.stringify({ ...parsedInput.value, [name]: graphObject(old) ? { ...old, text } : text }, null, 2);
}
let inputGeneration = 0;
watch(() => [editing.value, graph.document, graph.active?.session_id] as const, async ([open, document]) => {
  const token = ++inputGeneration;
  declarations.value = null; inputError.value = ""; loadingInputs.value = false;
  if (!open || !document) return;
  loadingInputs.value = true;
  try { const value = await readGraphInputDeclarations(document); if (token === inputGeneration) declarations.value = value; }
  catch (failure) { if (token === inputGeneration) inputError.value = failure instanceof Error ? failure.message : "输入声明读取失败"; }
  finally { if (token === inputGeneration) loadingInputs.value = false; }
});
onBeforeUnmount(() => { inputGeneration++; });
watch(() => [graph.document?.workflow_definition_id, graph.active?.session_id,
  JSON.stringify(graph.active?.external_inputs ?? {})], () => {
  input.value = JSON.stringify(graph.active?.external_inputs ?? {}, null, 2);
}, { immediate: true });
const agentActions = computed(() => graph.session?.available_actions?.filter(action =>
  ["retry_archive", "retry_acceptance", "retry_failed_node"].includes(action)) ?? []);
const activeAgent = computed(() => graph.session?.nodes.find(node =>
  ["paused", "budget_exhausted", "archive_failed"].includes(node.status)));
watch(() => graph.session?.workflow_session_id, () => { editing.value = false; menuOpen.value = false; });
function openInput() { if (!input.value) input.value = JSON.stringify(graph.active?.external_inputs ?? {}, null, 2); editing.value = true; }
async function apply(event: Event) {
  if (applying.value || graph.editorLocked) return;
  applying.value = true;
  try { const value = JSON.parse(input.value); if (!graphObject(value)) throw new Error();
    if (graph.editorLocked) return;
    graph.setInputs(value);
    inputError.value = "";
    editing.value = false; }
  catch (failure) { inputError.value = failure instanceof SyntaxError ? "输入须为 JSON 对象"
    : failure instanceof Error && failure.message ? failure.message : "输入须为 JSON 对象"; }
  finally { applying.value = false; }
}
</script>
<template>
  <div class="graph-run-controls">
    <span v-if="graph.session">当前会话</span>
    <button type="button" title="本次输入" aria-label="编辑本次输入" :disabled="graph.editorLocked" @click="openInput"><SquarePen :size="14" /></button>
    <button type="button" class="graph-primary" :class="{ paused: graph.primaryAction === 'pause' }" :disabled="graph.primaryAction === 'start' ? graph.locked || !!graph.session && !graph.session.can_submit : graph.controlLocked || !graph.session?.available_actions?.includes(graph.primaryAction)" @click="graph.submitPrimary()">
      <Pause v-if="graph.primaryAction === 'pause'" :size="14" fill="currentColor" /><Play v-else :size="14" fill="currentColor" />{{ graph.statusLabel }}
    </button>
    <button type="button" title="刷新运行" aria-label="刷新运行" :disabled="!!graph.busy" @click="graph.refresh()"><RefreshCw :size="14" /></button>
    <button v-if="graph.session?.available_actions?.includes('close')" type="button" title="关闭本次运行" aria-label="关闭本次运行" :disabled="graph.controlLocked" @click="graph.closeRun()"><X :size="14" /></button>
    <button v-if="graph.pending" type="button" title="核实原请求" aria-label="核实原请求" :disabled="!!graph.busy" @click="graph.reconcile()"><RotateCcw :size="14" /></button>
    <div v-if="agentActions.length" class="graph-agent-menu-anchor"><button type="button" title="运行操作" aria-label="运行操作" :aria-expanded="menuOpen" @click="menuOpen = !menuOpen"><MoreHorizontal :size="15" /></button>
      <section v-if="menuOpen" class="graph-agent-menu" aria-label="运行操作"><strong v-if="activeAgent">{{ activeAgent.label }} · {{ activeAgent.run_id?.slice(0,8) }}</strong>
        <button v-if="agentActions.includes('retry_archive')" type="button" :disabled="graph.controlLocked" @click="graph.submitAgentAction('retry_archive')">归档重试</button>
        <button v-if="agentActions.includes('retry_acceptance')" type="button" :disabled="graph.controlLocked" @click="graph.submitAgentAction('retry_acceptance')">结果接纳重试</button>
        <button v-if="agentActions.includes('retry_failed_node')" type="button" :disabled="graph.controlLocked" title="使用本次运行的原输入重试失败节点，保留已成功结果" @click="graph.submitAgentAction('retry_failed_node')">重试失败节点</button>
      </section>
    </div>
  </div>
  <Teleport to="body"><div v-if="editing" class="graph-input-shade" @click.self="!applying && (editing = false)"><form class="graph-input-dialog" @submit.prevent="apply"><header><strong>本次输入</strong><button type="button" title="关闭" aria-label="关闭输入" :disabled="applying" @click="editing = false"><X :size="16" /></button></header>
    <p v-if="loadingInputs" role="status">读取输入字段中</p>
    <label v-for="field in declarations?.inputs ?? []" :key="field.name" class="named-input">{{ field.name }}<small>{{ field.conflict ? '声明冲突' : declarations?.required_for_run?.includes(field.name) ? '本次运行必填' : '可编辑字段' }}</small>
      <textarea v-if="field.data_type === 'TEXT' && plainText(field.name) !== null" :value="plainText(field.name) ?? ''" rows="4" :aria-label="`运行输入 ${field.name}`" :disabled="field.conflict || graph.editorLocked || applying" @input="editText(field.name, $event)" />
      <small v-else>复杂输入保留在高级 JSON 中。</small>
    </label>
    <p v-for="(issue, index) in declarations?.diagnostics ?? []" :key="index" role="status">{{ issue.message }}</p>
    <p v-if="inputError" role="status">{{ inputError }}</p>
    <details :open="!!inputError || !parsedInput || !declarations?.inputs.length"><summary>高级 JSON / 未声明输入</summary><textarea v-model="input" rows="8" aria-label="具名输入 JSON" :disabled="graph.editorLocked || applying" /></details>
    <footer><button type="submit" :disabled="graph.editorLocked || applying">应用到当前会话输入</button></footer></form></div></Teleport>
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
.graph-input-dialog { width:440px; max-width:calc(100vw - 32px); max-height:calc(100dvh - 48px); overflow:auto; box-sizing:border-box; padding:15px; border:1px solid #54545b; border-radius:5px; background:#2c2c32; color:#dedee5; }
.named-input { display:grid; gap:5px; font-size:12px; margin-bottom:12px; min-width:0; overflow-wrap:anywhere; }
small,summary { font-size:11px; color:#b8c4be; } p { font-size:11px; color:#e4b59b; overflow-wrap:anywhere; }
header { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; font-size:13px; }
textarea { width:100%; box-sizing:border-box; padding:8px; border:1px solid #52525a; background:#202024; color:#dedee5; font:12px Consolas,monospace; resize:vertical; }
footer { display:flex; justify-content:flex-end; margin-top:8px; }
footer button { padding:0 12px; }
</style>
