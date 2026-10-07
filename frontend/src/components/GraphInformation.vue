<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { ArrowLeft, ArrowRight, BookOpen, RefreshCw } from "lucide-vue-next";
import { createWorkflowInformationAccess } from "../application/workflowInformation";
import {
  graphInformationScopes, isGraphInformationBinding,
  type GraphInformationBinding, type GraphInformationPage, type GraphRegistrationPage,
} from "../domain/graphInformation";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import ContextMaintenanceFact from "./ContextMaintenanceFact.vue";

const props = defineProps<{ sessionId?: string; chainId?: string; nodeId?: string; initialKind?: string }>();
const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
const kind = ref(props.initialKind ?? "");
const directory = ref<GraphRegistrationPage | null>(null);
const directoryLoading = ref(false), directoryError = ref<string | null>(null);
const directoryCursors = ref<(string | undefined)[]>([undefined]);
const selected = ref<GraphInformationBinding | null>(null);
const scope = ref<"live" | "history">("history");
const page = ref<GraphInformationPage | null>(null);
const reading = ref(false), readError = ref<string | null>(null);
const readCursors = ref<(string | undefined)[]>([undefined]);
const scopes = computed(() => selected.value ? graphInformationScopes(selected.value) : []);
const kinds = [
  ["", "全部登记"], ["information_binding", "调用信息源"], ["information_source", "信息源声明"],
  ["node", "节点"], ["data_type", "数据类型"], ["executor", "执行器"],
  ["pause_support", "暂停支持"], ["service", "宿主服务"], ["frontend_extension", "前端扩展"],
];
const basis = () => JSON.stringify([workspace.activeWorkflowId, props.sessionId, props.chainId, props.nodeId,
  kind.value, props.sessionId ? graph.views[props.sessionId]?.revision : null,
  props.sessionId ? graph.views[props.sessionId]?.head_revision : null]);
const directoryAccess = createWorkflowInformationAccess(basis), readerAccess = createWorkflowInformationAccess(basis);
let directoryGeneration = 0, readerGeneration = 0;
function clearReader() {
  readerGeneration++; readerAccess.invalidate(); selected.value = null; page.value = null;
  readError.value = null; reading.value = false; readCursors.value = [undefined];
}
async function list(cursor?: string, backwards = false) {
  const token = ++directoryGeneration;
  directoryLoading.value = true; directoryError.value = null;
  try {
    const result = await directoryAccess.registrations({
      ...(props.sessionId ? { session_id: props.sessionId } : {}),
      ...(props.sessionId && props.chainId ? { chain_id: props.chainId } : {}),
      ...(props.sessionId && props.nodeId ? { node_id: props.nodeId } : {}),
      ...(kind.value ? { kind: kind.value } : {}), limit: 50, ...(cursor ? { cursor } : {}),
    });
    if (!result || token !== directoryGeneration) return;
    clearReader();
    directory.value = result;
    if (!cursor) directoryCursors.value = [undefined];
    else if (backwards) directoryCursors.value.pop();
    else directoryCursors.value.push(cursor);
  } catch (failure) {
    if (token === directoryGeneration) directoryError.value = failure instanceof Error ? failure.message : "登记目录读取失败";
  } finally { if (token === directoryGeneration) directoryLoading.value = false; }
}
async function read(cursor?: string, backwards = false) {
  if (!selected.value || !props.sessionId) return;
  const token = ++readerGeneration, binding = selected.value;
  reading.value = true; readError.value = null; page.value = null;
  try {
    const result = await readerAccess.information(props.sessionId, binding, scope.value, cursor);
    if (!result || token !== readerGeneration || selected.value !== binding) return;
    page.value = result;
    if (!cursor || result.status === "reset") readCursors.value = [undefined];
    else if (backwards) readCursors.value.pop();
    else readCursors.value.push(cursor);
  } catch (failure) {
    if (token === readerGeneration) readError.value = failure instanceof Error ? failure.message : "信息读取失败";
  } finally { if (token === readerGeneration) reading.value = false; }
}
function choose(binding: GraphInformationBinding) {
  clearReader(); selected.value = binding;
  scope.value = binding.availability === "live" && graphInformationScopes(binding).includes("live")
    ? "live" : graphInformationScopes(binding).includes("history") ? "history" : "live";
  void read();
}
function scopeChanged(event: Event) {
  scope.value = (event.target as HTMLSelectElement).value as "live" | "history";
  readCursors.value = [undefined]; void read();
}
const label = (item: Record<string, unknown>) => Object.entries(item).map(([key, value]) => `${key}: ${String(value)}`).join(" · ");
watch(() => [workspace.activeWorkflowId, props.sessionId, props.chainId, props.nodeId, kind.value], () => {
  clearReader(); directory.value = null; directoryCursors.value = [undefined]; void list();
}, { immediate: true });
onUnmounted(() => { directoryGeneration++; directoryAccess.invalidate(); clearReader(); });
</script>

<template>
  <section class="graph-information" aria-label="统一登记与信息读取">
    <header><strong>登记目录</strong><button type="button" title="重新列举登记" aria-label="重新列举登记" :disabled="directoryLoading" @click="clearReader(); list()"><RefreshCw :size="14" /></button></header>
    <select v-model="kind" aria-label="登记类别"><option v-for="[value, text] in kinds" :key="value" :value="value">{{ text }}</option></select>
    <p v-if="directoryError" class="error" role="status">{{ directoryError }}</p>
    <p v-if="directoryLoading">读取目录中</p>
    <p v-if="directory && !directory.items.length">暂无登记</p>
    <details v-for="(item, index) in directory?.items ?? []" :key="`${item.kind}/${index}`" class="registration">
      <summary>{{ item.kind }} · {{ label(item.registration_ref) }}<small>{{ item.package_id ?? '平台' }}{{ item.package_version ? `@${item.package_version}` : '' }} · {{ item.availability }}</small></summary>
      <pre>{{ JSON.stringify(item, null, 2) }}</pre>
      <button v-if="isGraphInformationBinding(item)" type="button" class="read-command"
        :disabled="!props.sessionId || !['live', 'history'].includes(item.availability)"
        @click="choose(item)"><BookOpen :size="14" />读取本次调用</button>
    </details>
    <nav class="pagination" aria-label="登记目录分页">
      <button type="button" title="上一页登记" aria-label="上一页登记" :disabled="directoryLoading || directoryCursors.length < 2" @click="list(directoryCursors[directoryCursors.length - 2], true)"><ArrowLeft :size="14" /></button>
      <span>第 {{ directoryCursors.length }} 页</span>
      <button type="button" title="下一页登记" aria-label="下一页登记" :disabled="directoryLoading || !directory?.next_cursor" @click="list(directory?.next_cursor ?? undefined)"><ArrowRight :size="14" /></button>
    </nav>
    <section v-if="selected" class="information-page" aria-label="所选调用信息">
      <header><strong>{{ selected.declaration.channel_id }}</strong><button type="button" title="重新读取本次调用" aria-label="重新读取本次调用" :disabled="reading" @click="read()"><RefreshCw :size="14" /></button></header>
      <small>{{ selected.declaration.format_id }}@{{ selected.declaration.format_version }} · generation {{ selected.generation }}</small>
      <dl><dt>原会话</dt><dd>{{ selected.owner.workflow_session_id }}</dd><dt>运行</dt><dd>{{ selected.owner.chain_run_id }}</dd><dt>节点</dt><dd>{{ selected.owner.node_binding_id }}</dd><dt>节点调用</dt><dd>{{ selected.owner.node_run_id }}</dd></dl>
      <select :value="scope" aria-label="信息来源范围" :disabled="reading" @change="scopeChanged"><option v-for="value in scopes" :key="value" :value="value">{{ value === 'live' ? '活动来源' : '历史来源' }}</option></select>
      <p v-if="readError" class="error" role="status">{{ readError }}</p>
      <p v-if="reading">读取信息中</p>
      <p v-if="page?.status === 'gap'" class="notice" role="status">来源存在缺口，本页不代表完整历史。</p>
      <p v-if="page?.status === 'reset'" class="notice" role="status">来源要求重取，原游标已重置。</p>
      <p v-if="page && !page.items.length">本页无内容</p>
      <template v-for="(item, index) in page?.items ?? []" :key="index">
        <ContextMaintenanceFact v-if="page?.format_id === 'workflow.executor-facts'" :item="item" />
        <pre v-else>{{ JSON.stringify(item, null, 2) }}</pre>
      </template>
      <nav class="pagination" aria-label="信息分页">
        <button type="button" title="上一页信息" aria-label="上一页信息" :disabled="reading || readCursors.length < 2" @click="read(readCursors[readCursors.length - 2], true)"><ArrowLeft :size="14" /></button>
        <span>第 {{ readCursors.length }} 页<span v-if="page"> · {{ page.status }}</span></span>
        <button type="button" title="下一页信息" aria-label="下一页信息" :disabled="reading || !page?.next_cursor" @click="read(page?.next_cursor ?? undefined)"><ArrowRight :size="14" /></button>
      </nav>
    </section>
  </section>
</template>

<style scoped>
.graph-information { display:grid; gap:9px; min-width:0; font-size:11px; }
header { display:flex; align-items:center; justify-content:space-between; gap:8px; }
strong { font-size:12px; font-weight:500; overflow-wrap:anywhere; }
button { display:inline-flex; align-items:center; justify-content:center; gap:5px; min-width:26px; height:26px; padding:4px; border-radius:3px; }
button:disabled { opacity:.4; }
button:not(:disabled):hover { background:#414148; }
select { min-width:0; width:100%; padding:5px; color:#d8dde0; border:1px solid #505059; background:#222226; border-radius:3px; }
.registration { border-bottom:1px solid #45454d; padding:7px 0; }
summary { overflow-wrap:anywhere; line-height:1.6; cursor:pointer; }
small { display:block; color:#a8b2ae; overflow-wrap:anywhere; line-height:1.6; }
pre { margin:8px 0; padding:8px 0; white-space:pre-wrap; overflow-wrap:anywhere; font:11px/1.6 Consolas,monospace; }
.read-command { margin:4px 0; font-size:11px; }
.pagination { display:flex; justify-content:space-between; align-items:center; gap:8px; }
.information-page { display:grid; gap:8px; border-top:1px solid #53535b; padding-top:12px; min-width:0; }
dl { margin:0; display:grid; grid-template-columns:56px minmax(0,1fr); gap:4px; font-size:10px; }
dt { color:#a8b2ae; } dd { margin:0; overflow-wrap:anywhere; }
p { margin:4px 0; line-height:1.6; color:#abb4b1; overflow-wrap:anywhere; }
.error { color:#e4a0a0; } .notice { color:#dec99d; }
</style>
