<script setup lang="ts">
import { computed } from "vue";
import { thinkingSummaries } from "../domain/modelPresentation";
const props = defineProps<{ value: unknown }>();
const summaries = computed(() => thinkingSummaries(props.value));
</script>
<template>
  <details v-if="summaries.length" class="thinking-summary" aria-label="思考摘要">
    <summary>思考摘要</summary>
    <p v-for="(text, index) in summaries" :key="index">{{ text }}</p>
  </details>
</template>
<style scoped>
.thinking-summary { min-width:0; margin:8px 0; border-left:2px solid #799e91; padding:0 10px; font-size:12px; }
summary { cursor:pointer; color:#aac3b8; }
p { margin:8px 0; line-height:1.65; white-space:pre-wrap; overflow-wrap:anywhere; color:#b9bec6; }
</style>
