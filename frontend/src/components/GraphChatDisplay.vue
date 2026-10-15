<script setup lang="ts">
import { computed } from "vue";
import { chatContextValue } from "../domain/chatContext";
import { useWorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";

const props = defineProps<{ objectKey: string }>();
const sdk = useWorkflowFrontendSdk();
const object = computed(() => sdk.session.value?.objects?.[props.objectKey]);
const chat = computed(() => object.value ? chatContextValue(object.value) : null);
const roles = { user: "用户", assistant: "助手", system: "系统" };
</script>
<template>
  <section class="chat-display" aria-label="正式聊天数据">
    <header><strong>{{ objectKey }} · 聊天</strong></header>
    <p v-if="object?.deleted">此聊天项已删除。</p>
    <p v-else-if="chat === null" class="error" role="status">聊天数据契约无效。</p>
    <p v-else-if="!chat.rounds.length">尚无聊天记录。</p>
    <article v-for="(round, index) in chat?.rounds ?? []" :key="round.round_id">
      <header><strong>第 {{ index + 1 }} 轮</strong><small>{{ round.round_id }}</small></header>
      <p v-if="round.submitted_input !== null" class="message"><b>用户</b>{{ round.submitted_input }}</p>
      <div v-for="(candidate, candidateIndex) in round.candidates" :key="candidate.candidate_id"
        class="candidate" :data-selected="candidate.candidate_id === round.selected_candidate_id">
        <header><strong>候选 {{ candidateIndex + 1 }}{{ candidate.candidate_id === round.selected_candidate_id ? ' · 已选' : '' }}</strong>
          <small>{{ candidate.result_checkpoint_id ? '已保存' : '待成功收尾' }}</small></header>
        <p v-for="message in candidate.messages" :key="message.message_id" class="message">
          <b>{{ roles[message.role] }}</b>{{ message.text }}
        </p>
      </div>
    </article>
  </section>
</template>
<style scoped>
.chat-display { display:grid; gap:10px; min-width:0; font-size:11px; }
header { display:flex; align-items:flex-start; justify-content:space-between; gap:6px; min-width:0; }
strong,small { overflow-wrap:anywhere; min-width:0; }
small { color:#a7b5ae; font-size:9px; }
article { display:grid; gap:8px; border-top:1px solid #49494f; padding-top:9px; min-width:0; }
.candidate { min-width:0; border-left:2px solid #52525a; padding-left:8px; }
.candidate[data-selected="true"] { border-color:#7fbc99; }
p { margin:0; line-height:1.65; overflow-wrap:anywhere; }
.message { white-space:pre-wrap; color:#d8e1dc; }
b { display:block; font-size:10px; font-weight:500; color:#a7b5ae; }
.error { color:#e4a0a0; }
</style>
