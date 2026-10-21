<script setup lang="ts">
import { ref } from "vue";
import ErrorToast from "./components/ErrorToast.vue";
import WorkbenchSidebar from "./components/WorkbenchSidebar.vue";
import WorkflowWorkspace from "./components/WorkflowWorkspace.vue";
import { useWorkbenchPersistenceStore } from "./stores/workbenchPersistence";
import { useWorkflowFrontendHost } from "./composables/useWorkflowFrontendHost";

const persistence = useWorkbenchPersistenceStore();
useWorkflowFrontendHost();
const sidebarCollapsed = ref(false);
void persistence.initialize();
</script>

<template>
  <div class="workbench-shell" :class="{ 'is-sidebar-collapsed': sidebarCollapsed }">
    <WorkbenchSidebar @collapsed="sidebarCollapsed = $event" />

    <main class="workbench-main" aria-label="工作流工作台">
      <div v-if="persistence.error" class="workbench-save-error" role="status">{{ persistence.error }}</div>
      <div v-if="persistence.unreadableLegacyRequests" class="workbench-save-error" role="status">
        {{ persistence.unreadableLegacyRequests }} 条旧原请求无法安全解析；旧记录已保留，未重发
      </div>
      <div v-if="persistence.unreadableLegacyRecord" class="workbench-save-error" role="status">
        旧工作台记录无法按只读方式解析；原记录保留，未恢复编辑内容或重发请求
      </div>
      <div v-for="request in persistence.legacyRequests" :key="request.key" class="workbench-save-error" role="status">
        <span>旧原请求 · {{ request.workflowId }} · {{ request.result ?? request.error ?? (request.status === 'checking' ? '正在只读核实' : '待核实') }}</span>
        <button v-if="request.status !== 'retired'" type="button" :disabled="request.status === 'checking'" @click="persistence.verifyLegacyRequest(request.key)">
          {{ request.status === 'verified' ? '再次核实' : '核实原请求' }}
        </button>
      </div>
      <WorkflowWorkspace />
    </main>

    <ErrorToast />
  </div>
</template>
