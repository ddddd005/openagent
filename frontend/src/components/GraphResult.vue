<script setup lang="ts">
import { computed } from "vue";
import { modelPresentationJson } from "../domain/modelPresentation";
import { resultTexts } from "../domain/resultPresentation";
import ThinkingSummary from "./ThinkingSummary.vue";
const props = defineProps<{ value: unknown; label?: string }>();
const texts = computed(() => resultTexts(props.value));
</script>
<template>
  <section class="graph-result" :aria-label="label ?? '结果'">
    <section v-for="(row, index) in texts" :key="index" class="result-text">
      <small>{{ row.label }}</small><p>{{ row.text || '（空正文）' }}</p>
    </section>
    <ThinkingSummary :value="value" />
    <details><summary>{{ texts.length ? '技术详情' : '数据详情（未提供正文展示）' }}</summary><pre>{{ modelPresentationJson(value) }}</pre></details>
  </section>
</template>
<style scoped>
.graph-result { display:grid; gap:8px; min-width:0; }
.result-text { min-width:0; }
small,summary { font-size:11px; color:#b4beb9; }
p,pre { margin:5px 0 0; white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; line-height:1.65; }
pre { font-size:11px; color:#bbc4bf; }
details { min-width:0; } summary { cursor:pointer; padding:5px 0; }
</style>
