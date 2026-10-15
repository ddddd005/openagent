<script setup lang="ts">
import { computed } from "vue";
import { Plus } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
const props = defineProps<{ nodeId: string }>();
const graph = useWorkflowGraphStore();
const target = computed(() => graph.registrationTarget(props.nodeId));
</script>
<template>
  <section v-if="target" class="variable-registration" aria-label="变量注册">
    <output role="status">{{ target.state }}</output>
    <button type="button" :disabled="graph.locked || !target.canSubmit"
      @click="graph.submitVariableRegistration(nodeId)"><Plus :size="14" />注册变量及初值</button>
  </section>
</template>
<style scoped>
.variable-registration { display:grid; gap:6px; min-width:0; font-size:12px; }
output { overflow-wrap:anywhere; color:#b9c7bf; }
button { display:flex; align-items:center; justify-content:center; gap:5px; min-height:30px; padding:6px; border:1px solid #64776d; border-radius:3px; font:inherit; }
button:disabled { opacity:.45; }
</style>
