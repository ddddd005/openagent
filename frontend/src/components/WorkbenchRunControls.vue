<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { MoreHorizontal, Pause, Play, RefreshCw, RotateCcw, SquarePen, X } from "lucide-vue-next";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";

const props = defineProps<{ workflowId: string }>();
const runtime = useWorkbenchRuntimeStore();
const editing = ref(false);
const menuOpen = ref(false);
const rootText = ref("");
const additionalRequests = ref(1);
const additionalAttempts = ref(4);
const canSecondary = computed(() => !runtime.busy && !runtime.unknown);
watch(() => [props.workflowId, runtime.sessionId] as const, () => {
  editing.value = false;
  menuOpen.value = false;
  rootText.value = "";
}, { flush: "sync" });

function openInput() {
  rootText.value = runtime.inputText;
  editing.value = true;
}
function run() {
  if (runtime.control.action === "start" && !runtime.inputText.trim()) openInput();
  else void runtime.submitPrimary();
}
async function saveInput(start: boolean) {
  runtime.setInputText(rootText.value);
  editing.value = false;
  if (start) await runtime.submitPrimary();
}
</script>

<template>
  <div class="run-controls" :data-workflow-id="workflowId">
    <span
      v-if="runtime.session"
      class="run-scope"
      :title="`会话 ${runtime.sessionId}，当前打开工作流`"
    >{{ runtime.session.mode === "offline" ? "离线" : "非离线" }} · {{ runtime.sessionId?.slice(0, 8) }}</span>
    <button
      v-if="runtime.executable"
      class="run-icon-button"
      type="button"
      title="当前输入"
      aria-label="编辑 Agent A 当前输入"
      :disabled="!!runtime.busy || !!runtime.unknown"
      @click="openInput"
    ><SquarePen :size="14" /></button>
    <button
      class="run-button"
      :class="{ 'is-pause': runtime.control.action === 'interrupt' || runtime.busy === 'interrupt' }"
      type="button"
      :disabled="runtime.locked"
      :title="runtime.executable ? `控制当前打开工作流${runtime.sessionId ? `的会话 ${runtime.sessionId}` : '的固定 A/B 流程'}` : '空工作流没有正式可执行目标'"
      @click="run"
    >
      <Pause v-if="runtime.control.action === 'interrupt' || runtime.busy === 'interrupt'" :size="14" fill="currentColor" />
      <Play v-else :size="14" fill="currentColor" />
      <span>{{ runtime.statusLabel }}</span>
    </button>
    <button v-if="runtime.refreshRequired && !runtime.unknown" class="run-icon-button" type="button" title="重读权威状态" aria-label="重读权威状态" :disabled="!!runtime.busy" @click="runtime.refresh()"><RefreshCw :size="14" /></button>
    <template v-if="runtime.unknown">
      <button class="run-icon-button" type="button" title="重读权威状态" aria-label="重读权威状态" :disabled="!!runtime.busy" @click="runtime.refresh()"><RefreshCw :size="14" /></button>
      <button
        class="run-icon-button"
        type="button"
        :title="runtime.unknown.replayable ? `以原请求 ${runtime.unknown.requestId} 核实` : '会话创建缺少幂等查询，需人工核实'"
        aria-label="以原请求身份核实提交"
        :disabled="!!runtime.busy"
        @click="runtime.replayUnknown()"
      ><RotateCcw :size="14" /></button>
    </template>
    <div v-if="runtime.secondaryActions.length" class="run-menu-anchor">
      <button class="run-icon-button" type="button" title="运行操作" aria-label="运行操作" :aria-expanded="menuOpen" @click="menuOpen = !menuOpen"><MoreHorizontal :size="16" /></button>
      <div v-if="menuOpen" class="run-menu" aria-label="运行操作">
        <div v-if="runtime.secondaryActions.includes('extend_budget')" class="budget-fields">
          <label>请求增额<input v-model.number="additionalRequests" type="number" min="0" max="64" step="1" /></label>
          <label>尝试增额<input v-model.number="additionalAttempts" type="number" min="0" max="256" step="1" /></label>
          <button type="button" :disabled="!canSecondary" @click="runtime.submitSecondary('extend_budget', additionalRequests, additionalAttempts)">增加预算</button>
        </div>
        <button v-if="runtime.secondaryActions.includes('retry_archive')" type="button" :disabled="!canSecondary" @click="runtime.submitSecondary('retry_archive')">归档重试</button>
        <button v-if="runtime.secondaryActions.includes('retry_publish')" type="button" :disabled="!canSecondary" @click="runtime.submitSecondary('retry_publish')">交付重试</button>
      </div>
    </div>
  </div>
  <Teleport to="body">
    <div v-if="editing" class="run-dialog-shade" @click.self="editing = false">
      <section class="run-input-dialog" role="dialog" aria-modal="true" aria-labelledby="run-input-heading">
        <header>
          <h2 id="run-input-heading">Agent A · 当前输入</h2>
          <button class="run-icon-button" type="button" title="关闭" aria-label="关闭当前输入" @click="editing = false"><X :size="17" /></button>
        </header>
        <textarea v-model="rootText" rows="7" autofocus aria-label="Agent A 当前输入" />
        <footer>
          <button type="button" @click="saveInput(false)">应用</button>
          <button class="run-button" type="button" :disabled="!rootText.trim() || runtime.locked || runtime.control.action !== 'start'" @click="saveInput(true)"><Play :size="14" fill="currentColor" />启动</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.run-controls { display:flex; align-items:center; gap:5px; position:relative; white-space:nowrap; }
.run-scope { font-size:10px; color:#a5a5ad; }
.run-button,.run-icon-button { height:26px; display:inline-flex; align-items:center; justify-content:center; gap:5px; border:1px solid #454549; border-radius:4px; background:#2a2a2d; cursor:pointer; }
.run-button { min-width:70px; padding:0 8px; color:#7fc89f; font-size:12px; }
.run-button.is-pause { color:#e4c764; border-color:#817247; }
.run-icon-button { width:26px; padding:0; color:#b7b7bf; }
button:disabled { opacity:.5; cursor:default; }
.run-menu-anchor { position:relative; }
.run-menu { position:absolute; z-index:30; top:31px; right:0; width:205px; background:#29292c; border:1px solid #454549; box-shadow:0 3px 10px #0005; padding:8px; border-radius:4px; display:grid; gap:6px; }
.run-menu button { padding:6px; background:#333336; border:1px solid #4d4d52; border-radius:3px; text-align:left; color:#dededf; }
.budget-fields { display:grid; gap:6px; }
.budget-fields label { display:flex; align-items:center; justify-content:space-between; gap:8px; font-size:12px; }
.budget-fields input { width:65px; border:1px solid #4d4d52; padding:3px; background:#202022; color:#dededf; }
.run-dialog-shade { position:fixed; inset:0; z-index:200; background:#10182066; display:grid; place-items:center; }
.run-input-dialog { width:480px; max-width:calc(100vw - 40px); background:#29292c; color:#dededf; border:1px solid #505056; border-radius:6px; padding:16px; box-shadow:0 12px 35px #0005; }
.run-input-dialog header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
.run-input-dialog h2 { margin:0; font-size:15px; }
.run-input-dialog textarea { width:100%; box-sizing:border-box; resize:vertical; border:1px solid #4d4d52; background:#202022; padding:8px; color:#dededf; font-size:13px; }
.run-input-dialog footer { display:flex; justify-content:flex-end; gap:8px; margin-top:12px; }
.run-input-dialog footer > button:not(.run-button) { padding:4px 12px; border:1px solid #4d4d52; background:#333336; color:#dededf; border-radius:4px; }
</style>
