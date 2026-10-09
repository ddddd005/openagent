<script setup lang="ts">
import { Download, Import } from "lucide-vue-next";
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
  <div v-if="persistence.error || persistence.legacyImportError || persistence.hasRawRecord" class="workbench-save-error storage-recovery" role="status">
    <span>{{ persistence.error || persistence.legacyImportError || (persistence.isolated ? "浏览器原记录已保留" : "检测到旧浏览器记录，尚未导入本机") }}</span>
    <button v-if="persistence.hasRawRecord" type="button" title="导出原始浏览器存档，不改写保存记录"
      aria-label="导出原记录" @click="exportRecord()"><Download :size="14" />导出原记录</button>
    <button v-if="persistence.canImportLegacy" type="button" title="将可验证的浏览器记录导入本机，保留原始记录，不执行待核实请求"
      aria-label="导入浏览器原记录" :disabled="persistence.importing" @click="persistence.importLegacy()"><Import :size="14" />{{ persistence.importing ? '导入中' : '导入浏览器记录' }}</button>
  </div>
</template>
<style scoped>
.storage-recovery { display:flex; align-items:center; flex-wrap:wrap; gap:8px 12px; }
.storage-recovery > span { flex:1; min-width:0; overflow-wrap:anywhere; }
button { display:flex; align-items:center; gap:6px; flex-shrink:0; padding:5px 7px; font-size:12px; border-radius:3px; }
</style>
