<script setup lang="ts">
import { ref } from "vue";
import { Plus } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../../stores/workflowGraph";
import { createTavernDemo } from "./tavernDemo";

const graph = useWorkflowGraphStore(), error = ref("");
function create() {
  if (graph.locked || graph.catalogLoading || graph.catalogError) return;
  error.value = "";
  try {
    const doc = createTavernDemo(graph.catalog, graph.executionPackageLock);
    if (!graph.createWorkflowDraft(doc, "酒馆独立聊天展示")) error.value = "当前不可创建酒馆草稿";
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "酒馆示例不可用"; }
}
</script>
<template>
  <section class="tavern-chat-panel" aria-label="酒馆聊天入口">
    <header><strong>酒馆聊天</strong></header>
    <div class="chat-actions">
      <button type="button" title="创建酒馆示例" aria-label="创建酒馆示例"
        :disabled="graph.locked || graph.catalogLoading || !!graph.catalogError" @click="create"><Plus :size="14" />新建</button>
    </div>
    <p v-if="error" class="warning" role="status">{{ error }}</p>
  </section>
</template>
<style scoped>
.tavern-chat-panel { display:grid; gap:9px; min-width:0; padding:12px; border-top:1px solid #45454e; font-size:12px; }
header { display:flex; min-width:0; }
strong { font-weight:500; }
.chat-actions { display:flex; flex-wrap:wrap; gap:6px; min-width:0; }
button,a { display:inline-flex; align-items:center; justify-content:center; gap:5px; min-height:28px; box-sizing:border-box; padding:5px 8px; border:1px solid #64776d; border-radius:3px; color:#dedee4; background:transparent; font:inherit; text-decoration:none; }
:disabled,[aria-disabled="true"] { opacity:.55; }
[aria-disabled="true"] { pointer-events:none; }
p { margin:0; overflow-wrap:anywhere; line-height:1.5; color:#a4abb4; }
.warning { color:#e4a0a0; }
</style>
