<script setup lang="ts">
import { computed } from "vue";
import { GitBranch, RotateCcw, Check, RefreshCw, Eye, X } from "lucide-vue-next";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";
import { sessionHistoryText } from "../adapters/workbenchSessions";
const runtime = useWorkbenchRuntimeStore();
const messages = computed(() => runtime.session?.messages ?? []);
const actionLocked = computed(() => !!runtime.busy || !!runtime.unknown || runtime.refreshRequired);
const latestReply = computed(() => messages.value.at(-1)?.role === "assistant" ? messages.value.at(-1) : null);
const rerollChain = computed(() => runtime.session?.recovery_chain_run_id
  ?? latestReply.value?.reply_candidates?.find(row => row.selected)?.chain_run_id
  ?? messages.value.at(-1)?.chain_run_id ?? null);
const preview = computed(() => runtime.candidatePreview);
const forkLocked = computed(() => actionLocked.value || runtime.session?.chains.some(row =>
  !["succeeded", "superseded", "closed"].includes(row.status)) === true);
function canForkMessage(messageId: string) {
  const index = messages.value.findIndex(row => row.visible_message_id === messageId);
  return messages.value[index]?.role === "assistant" || messages.value[index + 1]?.role === "assistant";
}
function displayedPayload(messageId: string, payload: unknown) {
  return preview.value?.messageId === messageId ? preview.value.candidate.payload : payload;
}
</script>
<template>
  <section class="workbench-history" aria-label="消息历史">
    <header class="history-toolbar">
      <h2>消息历史</h2>
      <code v-if="runtime.sessionId">{{ runtime.sessionId }}</code>
      <span class="toolbar-spacer" />
      <button v-if="runtime.session?.can_reroll && rerollChain" :disabled="actionLocked" @click="runtime.reroll(rerollChain)">
        <RotateCcw :size="14" />重抽样
      </button>
      <button title="刷新消息历史" aria-label="刷新消息历史" :disabled="!runtime.sessionId" @click="runtime.refresh()"><RefreshCw :size="15" /></button>
    </header>
    <div class="history-scroll">
      <div v-if="!messages.length" class="history-empty">{{ runtime.sessionId ? "暂无消息" : "尚未选择会话" }}</div>
      <article v-for="message in messages" :key="message.visible_message_id" class="history-message">
        <div class="message-header">
          <strong>{{ message.role === "user" ? "用户" : "回复" }}</strong>
          <code>{{ message.visible_message_id.slice(0, 8) }}</code>
          <span v-if="preview?.messageId === message.visible_message_id" class="preview-marker"><Eye :size="12" />候选预览</span>
          <span class="toolbar-spacer" />
          <button title="从此消息分叉" aria-label="从此消息分叉" :disabled="forkLocked || !canForkMessage(message.visible_message_id)" @click="runtime.forkAt(message.visible_message_id, preview?.messageId === message.visible_message_id ? preview.candidate.candidate_id : undefined)"><GitBranch :size="14" /></button>
        </div>
        <pre>{{ sessionHistoryText(displayedPayload(message.visible_message_id, message.payload)) }}</pre>
        <div v-if="message.reply_candidates?.length" class="candidate-controls">
          <button v-for="(candidate, index) in message.reply_candidates" :key="candidate.candidate_id"
            :class="{ selected: candidate.selected, previewed: preview?.candidate.candidate_id === candidate.candidate_id }"
            :title="candidate.candidate_id" @click="runtime.previewCandidate(message.visible_message_id, candidate.candidate_id)">
            <Check v-if="candidate.selected" :size="12" /><Eye v-else :size="12" />{{ index + 1 }}
          </button>
          <template v-if="preview?.messageId === message.visible_message_id">
            <button v-if="!preview.candidate.selected" :disabled="actionLocked || !runtime.session?.can_select_candidates || latestReply?.visible_message_id !== message.visible_message_id" @click="runtime.selectCandidate(preview.candidate.candidate_id)"><Check :size="13" />选用</button>
            <button title="结束候选预览" aria-label="结束候选预览" @click="runtime.previewCandidate(message.visible_message_id, null)"><X :size="13" /></button>
          </template>
        </div>
      </article>
    </div>
  </section>
</template>
<style scoped>
.workbench-history { flex: 1; height: 100%; min-width: 0; min-height: 0; display: flex; flex-direction: column; background: #222226; color: #d0d1d8; }
.history-toolbar { display: flex; align-items: center; gap: 10px; min-height: 44px; padding: 0 16px; border-bottom: 1px solid #414146; }
h2 { font-size: 13px; margin: 0; font-weight: 600; }
code { font-size: 10px; color: #8f98a5; white-space: nowrap; }
.toolbar-spacer { flex: 1; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 5px; height: 28px; padding: 0 7px; border: 1px solid #48484d; border-radius: 4px; background: #2a2a30; color: #bfc4ce; font-size: 11px; cursor: pointer; }
button:disabled { opacity: .4; cursor: default; }
button:hover:not(:disabled) { background: #36363d; }
.history-scroll { flex: 1; min-height: 0; overflow: auto; padding: 16px 20px; }
.history-empty { padding: 40px; text-align: center; color: #8b919c; font-size: 12px; }
.history-message { max-width: 1000px; margin: 0 auto; padding: 15px 0 19px; border-bottom: 1px solid #3d3d43; }
.message-header { display: flex; align-items: center; gap: 10px; min-height: 28px; }
strong { font-size: 12px; }
pre { font-family: inherit; font-size: 13px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; margin: 9px 0 11px; }
.candidate-controls { display: flex; align-items: center; gap: 5px; }
.candidate-controls .selected { border-color: #60896e; color: #a2c7b0; }
.candidate-controls .previewed { background: #303942; border-color: #617a93; }
.preview-marker { display: inline-flex; align-items: center; gap: 4px; font-size: 10px; color: #97b9d7; }
</style>
