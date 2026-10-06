<script setup lang="ts">
import { onBeforeUnmount, watch } from "vue";
import { AlertCircle } from "lucide-vue-next";
import { useWorkbenchNoticesStore } from "../stores/workbenchNotices";

const runtime = useWorkbenchNoticesStore();
const labels = { unavailable: "动作不可用", rejected: "请求被拒绝", unknown: "提交结果未知" };
let timer: ReturnType<typeof setTimeout> | undefined;
watch(() => runtime.error, (notice) => {
  if (timer) clearTimeout(timer);
  if (notice) timer = setTimeout(() => runtime.clearError(notice.sequence), 3000);
});
onBeforeUnmount(() => { if (timer) clearTimeout(timer); });
</script>

<template>
  <Teleport to="body">
    <div v-if="runtime.error" class="workbench-error-toast" role="alert">
      <AlertCircle :size="18" />
      <div>
        <strong>{{ labels[runtime.error.kind] }}</strong>
        <span>{{ runtime.error.reason }}</span>
        <code>请求：{{ runtime.error.requestId }}</code>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.workbench-error-toast { position:fixed; z-index:400; top:12px; left:50%; transform:translateX(-50%); width:460px; max-width:calc(100vw - 40px); display:flex; align-items:flex-start; gap:9px; padding:12px 14px; background:#b42630; color:#fff; border:1px solid #921b23; border-radius:4px; box-shadow:0 4px 12px #0003; }
.workbench-error-toast svg { flex-shrink:0; margin-top:1px; }
.workbench-error-toast div { display:grid; gap:4px; min-width:0; }
.workbench-error-toast strong { font-size:13px; }
.workbench-error-toast span,.workbench-error-toast code { font-size:12px; overflow-wrap:anywhere; }
</style>
