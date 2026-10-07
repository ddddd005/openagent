<script setup lang="ts">
import { computed } from "vue";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { frontendExtensionIdentity } from "../domain/frontendExtensions";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
const graph = useWorkflowGraphStore();
const host = createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock);
const providerPanelIdentity = frontendExtensionIdentity(modelFrontendExtensions[0]!);
const providerPanel = computed(() => host.extensions.value.find(row =>
  frontendExtensionIdentity(row.declaration) === providerPanelIdentity));
</script>

<template>
  <section class="provider-sidebar" aria-label="普通图供应商">
    <header class="workbench-panel-heading"><h1>供应商</h1></header>
    <div class="provider-sidebar-content">
      <component v-if="providerPanel" :is="providerPanel.component"
        :key="frontendExtensionIdentity(providerPanel.declaration)" />
      <p v-else-if="graph.catalogLoading" class="provider-sidebar-status" role="status">读取中</p>
      <div v-else class="provider-sidebar-status provider-sidebar-warning" role="status">
        <p>供应商资源不可用</p>
        <p>{{ graph.catalogError ?? "当前节点目录未提供已启用的可信供应商面板" }}</p>
      </div>
      <p v-for="issue in host.issues.value" :key="issue" class="provider-sidebar-status provider-sidebar-warning" role="status">{{ issue }}</p>
    </div>
  </section>
</template>

<style scoped>
.provider-sidebar { display:flex; flex:1; flex-direction:column; min-width:0; min-height:0; }
.provider-sidebar-content { flex:1; min-height:0; overflow:auto; }
.provider-sidebar-status { margin:0; padding:10px 12px; color:#a4abb4; font-size:11px; line-height:1.6; overflow-wrap:anywhere; }
.provider-sidebar-status p { margin:0; }
.provider-sidebar-warning { color:#f4b4ae; }
</style>
