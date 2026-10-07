<script setup lang="ts">
import { computed } from "vue";
import { lorebookEvaluation, lorebookEvaluationLabels } from "../domain/lorebookEvaluation";
const props = defineProps<{ reads: readonly unknown[] }>();
const evaluations = computed(() => props.reads.map(lorebookEvaluation).filter(value => value !== null));
const result = (value: boolean | null) => value === null ? '未检查' : value ? '通过' : '未通过';
</script>
<template>
  <section v-for="(evaluation, index) in evaluations" :key="index" class="lorebook-evaluation" aria-label="Lorebook 触发结果">
    <header><strong>Lorebook</strong><span>递归追加 {{ evaluation.recursive_rounds }} 轮</span></header>
    <p v-if="!evaluation.entries.length">无条目</p>
    <details v-for="(entry, entryIndex) in evaluation.entries" :key="entry.entry_id">
      <summary>
        <span :title="entry.entry_id">{{ entry.name || `条目 ${entryIndex + 1}` }}</span>
        <strong :class="entry.status">{{ lorebookEvaluationLabels[entry.status] }}</strong>
        <small v-if="entry.activation_round !== null">{{ entry.activation_round === 0 ? '首次匹配' : `递归第 ${entry.activation_round} 轮` }}</small>
      </summary>
      <dl>
        <dt>条目 ID</dt><dd>{{ entry.entry_id }}</dd>
        <dt>概率</dt><dd>{{ result(entry.probability_passed) }}</dd>
        <dt>主关键词</dt><dd>{{ result(entry.primary_matched) }}</dd>
        <dt>副关键词</dt><dd>{{ result(entry.secondary_matched) }}</dd>
      </dl>
    </details>
  </section>
</template>
<style scoped>
.lorebook-evaluation { display:grid; gap:5px; min-width:0; padding:10px 12px; border-bottom:1px solid #45454c; font-size:11px; }
header { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; color:#b7c1bc; }
strong { font-size:11px; font-weight:500; }
details { border-bottom:1px solid #3e3e45; }
summary { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:7px 0; cursor:pointer; line-height:1.6; }
summary > span { flex:1; min-width:60px; overflow-wrap:anywhere; color:#d7dcda; }
summary small { color:#a8b1b0; }
.activated { color:#97c7b2; } .probability_rejected { color:#dec99d; } .keyword_miss,.disabled { color:#a4abb2; }
dl { display:grid; grid-template-columns:70px minmax(0,1fr); gap:5px; margin:0; padding:5px 0 10px; }
dt { color:#9eaaa6; } dd { margin:0; color:#c6cfcb; overflow-wrap:anywhere; }
p { margin:5px 0; color:#a4abb2; }
</style>
