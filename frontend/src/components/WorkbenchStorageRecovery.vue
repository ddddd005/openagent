<script setup lang="ts">
import { Download, FolderPlus } from "lucide-vue-next";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";

const persistence = useWorkbenchPersistenceStore();
function exportRecord() {
  const raw = persistence.rawRecord();
  if (raw === null) return;
  const url = URL.createObjectURL(new Blob([raw], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `workbench-original-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>
<template>
  <div v-if="persistence.error || persistence.isolated" class="workbench-save-error storage-recovery" role="status">
    <span>{{ persistence.error ?? "独立本机工作区 · 原记录未覆盖" }}</span>
    <button v-if="persistence.hasRawRecord" type="button" title="导出原始浏览器存档，不改写保存记录"
      aria-label="导出原记录" @click="exportRecord()"><Download :size="14" />导出原记录</button>
    <button v-if="persistence.canRecover" type="button" title="使用独立保存位置，保留原记录和原请求"
      aria-label="另建本机工作区" @click="persistence.recoverWorkspace()"><FolderPlus :size="14" />另建本机工作区</button>
  </div>
</template>
<style scoped>
.storage-recovery { display:flex; align-items:center; flex-wrap:wrap; gap:8px 12px; }
.storage-recovery > span { flex:1; min-width:0; overflow-wrap:anywhere; }
button { display:flex; align-items:center; gap:6px; flex-shrink:0; padding:5px 7px; font-size:12px; border-radius:3px; }
</style>
