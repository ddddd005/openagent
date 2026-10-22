<script setup lang="ts">
import { AlertCircle, CheckCircle2, Info, TriangleAlert, X } from "lucide-vue-next";
import { useWorkbenchNoticesStore } from "../stores/workbenchNotices";

const runtime = useWorkbenchNoticesStore();
const icons = { info: Info, success: CheckCircle2, warning: TriangleAlert, major: AlertCircle };
const labels = { info: "通知", success: "成功", warning: "警告", major: "重大问题" };
</script>

<template>
  <Teleport to="body">
    <div class="workbench-notice-stack" aria-label="项目通知">
      <div v-for="notice in runtime.notices" :key="notice.id" class="workbench-error-toast"
        :class="`notice-${notice.severity}`" :role="notice.severity === 'major' ? 'alert' : 'status'">
        <component :is="icons[notice.severity]" :size="18" aria-hidden="true" />
        <div class="notice-content">
          <strong>{{ labels[notice.severity] }} · {{ notice.title }}</strong>
          <span>{{ notice.reason }}</span>
          <code v-if="notice.requestId">请求：{{ notice.requestId }}</code>
          <button v-if="notice.action" type="button" @click="notice.action.run">{{ notice.action.label }}</button>
        </div>
        <button type="button" class="notice-dismiss" title="关闭通知" aria-label="关闭通知" @click="runtime.dismiss(notice.id)">
          <X :size="16" />
        </button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.workbench-notice-stack { position:fixed; z-index:400; top:12px; left:50%; transform:translateX(-50%); width:460px; max-width:calc(100vw - 40px); max-height:calc(100vh - 24px); overflow:auto; display:grid; gap:8px; pointer-events:none; }
.workbench-error-toast { display:flex; align-items:flex-start; gap:9px; padding:12px 14px; color:#fff; border:1px solid transparent; border-radius:4px; box-shadow:0 4px 12px #0003; pointer-events:auto; }
.notice-info { background:#175c9c; border-color:#104a80; }
.notice-success { background:#247544; border-color:#195d34; }
.notice-warning { background:#ffe38b; border-color:#c99c21; color:#382a00; }
.notice-major { background:#b42630; border-color:#921b23; }
.workbench-error-toast svg { flex-shrink:0; margin-top:1px; }
.notice-content { display:grid; gap:4px; min-width:0; flex:1; }
.workbench-error-toast button { color:inherit; cursor:pointer; }
.notice-content button { justify-self:start; text-decoration:underline; font-size:12px; }
.notice-dismiss { display:flex; flex-shrink:0; padding:2px; }
.workbench-error-toast strong { font-size:13px; }
.workbench-error-toast span,.workbench-error-toast code { font-size:12px; overflow-wrap:anywhere; }
</style>
