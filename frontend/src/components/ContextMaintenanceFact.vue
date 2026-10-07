<script setup lang="ts">
import { computed } from "vue";
import { contextMaintenanceSummary } from "../domain/contextMaintenance";
const props = defineProps<{ item: unknown }>();
const summary = computed(() => contextMaintenanceSummary(props.item));
</script>
<template>
  <section v-if="summary" class="maintenance-fact" aria-label="上下文维护信息">
    <strong>{{ summary.title }}</strong>
    <p v-for="(line, index) in summary.lines" :key="index">{{ line }}</p>
    <details><summary>原始证据</summary><pre>{{ JSON.stringify(item, null, 2) }}</pre></details>
  </section>
  <pre v-else>{{ JSON.stringify(item, null, 2) }}</pre>
</template>
<style scoped>
.maintenance-fact { padding:10px 0; border-bottom:1px solid #45454d; min-width:0; font-size:11px; }
strong { font-size:12px; font-weight:500; } p { margin:6px 0; line-height:1.6; overflow-wrap:anywhere; color:#b7c7bd; }
details { margin-top:8px; } summary { cursor:pointer; color:#a8b2ae; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font:11px/1.6 Consolas,monospace; margin:8px 0; }
</style>
