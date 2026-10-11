<script setup lang="ts">
import { computed } from "vue";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { frontendExtensionIdentity } from "../domain/frontendExtensions";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
const providerDeclaration = modelFrontendExtensions[0]!;
const host = createWorkbenchFrontendHost(() => [providerDeclaration], () => [
  { package_id: providerDeclaration.package_id, version: providerDeclaration.package_version },
]);
const providerPanelIdentity = frontendExtensionIdentity(providerDeclaration);
const providerPanel = computed(() => host.extensions.value.find(row =>
  frontendExtensionIdentity(row.declaration) === providerPanelIdentity));
</script>

<template>
  <section class="provider-sidebar" aria-label="全局供应商">
    <header class="workbench-panel-heading"><h1>供应商</h1></header>
    <div class="provider-sidebar-content">
      <component v-if="providerPanel" :is="providerPanel.component"
        :key="frontendExtensionIdentity(providerPanel.declaration)" />
      <div v-else class="provider-sidebar-status provider-sidebar-warning" role="status">
        <p>供应商资源不可用</p>
        <p>可信供应商面板未安装</p>
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
