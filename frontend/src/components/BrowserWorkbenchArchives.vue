<script setup lang="ts">
import { ref } from "vue";
import { Archive, Download, X } from "lucide-vue-next";
import { browserArchiveSource, browserArchiveValue, listBrowserWorkbenchArchives } from "../adapters/browserWorkbenchArchives";
import type { LocalDraftRecord } from "../adapters/localDraftsApi";

const dialog = ref<HTMLDialogElement | null>(null), records = ref<LocalDraftRecord[]>([]);
const loading = ref(false), error = ref("");
async function open() {
  dialog.value?.showModal();
  loading.value = true; error.value = "";
  try { records.value = await listBrowserWorkbenchArchives(); }
  catch (failure) { error.value = failure instanceof Error ? failure.message : "旧版存档读取失败"; }
  finally { loading.value = false; }
}
function download(record: LocalDraftRecord) {
  const value = browserArchiveValue(record);
  const url = URL.createObjectURL(new Blob([value.raw], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url; link.download = `workbench-archive-${value.raw_sha256.slice(0, 12)}.json`;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>
<template>
  <button class="workbench-tool-button" type="button" title="旧版存档" aria-label="旧版存档" @click="open"><Archive :size="15" /></button>
  <Teleport to="body">
    <dialog ref="dialog" class="browser-archives-dialog" aria-labelledby="browser-archives-title">
      <header><h2 id="browser-archives-title">旧版存档</h2>
        <button type="button" title="关闭旧版存档" aria-label="关闭旧版存档" @click="dialog?.close()"><X :size="16" /></button></header>
      <div class="browser-archives-body">
        <p v-if="loading" role="status">读取中</p>
        <p v-else-if="error" class="warning" role="status">{{ error }}</p>
        <p v-else-if="!records.length">暂无旧版存档</p>
        <ul v-else>
          <li v-for="record in records" :key="record.address.key">
            <div><strong>{{ browserArchiveSource(browserArchiveValue(record).source_key) }}</strong>
              <time>{{ new Date(record.updated_at).toLocaleString() }}</time></div>
            <button type="button" title="导出原始存档" aria-label="导出旧版存档" @click="download(record)"><Download :size="15" /></button>
          </li>
        </ul>
      </div>
    </dialog>
  </Teleport>
</template>
<style scoped>
.browser-archives-dialog { width:460px; max-width:calc(100vw - 24px); max-height:calc(100dvh - 24px); padding:0;
  box-sizing:border-box; border:1px solid #57575e; border-radius:6px; color:#dedee4; background:#29292f; font-size:12px; }
.browser-archives-dialog::backdrop { background:#0008; }
header { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:12px 16px; border-bottom:1px solid #44444c; }
h2 { margin:0; font-size:15px; font-weight:500; }
button { display:inline-flex; align-items:center; justify-content:center; min-height:30px; min-width:30px; padding:5px; border-radius:3px; }
.browser-archives-body { padding:12px 16px; max-height:calc(100dvh - 100px); overflow:auto; }
p { margin:0; overflow-wrap:anywhere; }
ul { list-style:none; padding:0; margin:0; }
li { display:flex; align-items:center; gap:10px; padding:10px 0; border-bottom:1px solid #44444c; }
li > div { display:grid; flex:1; min-width:0; gap:5px; }
strong { font-weight:500; overflow-wrap:anywhere; }
time { color:#a4abb4; font-size:11px; }
.warning { color:#e4a0a0; }
</style>
