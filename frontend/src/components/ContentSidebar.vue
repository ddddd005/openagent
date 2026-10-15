<script setup lang="ts">
import { computed } from "vue";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { frontendExtensionIdentity } from "../domain/frontendExtensions";
import { promptFrontendExtensions } from "../plugins/promptFrontendManifest";
import { currentTavernFrontendExtensions } from "../plugins/tavernFrontendManifest";
const promptDeclaration = promptFrontendExtensions[0]!;
const lorebookDeclaration = currentTavernFrontendExtensions[2]!;
const host = createWorkbenchFrontendHost(() => [promptDeclaration, lorebookDeclaration], () => [
  { package_id: promptDeclaration.package_id, version: promptDeclaration.package_version },
  { package_id: lorebookDeclaration.package_id, version: lorebookDeclaration.package_version },
]);
const promptPanelIdentity = frontendExtensionIdentity(promptDeclaration);
const promptPanel = computed(() => host.extensions.value.find(row =>
  frontendExtensionIdentity(row.declaration) === promptPanelIdentity));
const lorebookPanel = computed(() => host.extensions.value.find(row =>
  frontendExtensionIdentity(row.declaration) === frontendExtensionIdentity(lorebookDeclaration)));
</script>

<template>
  <section class="current-content-sidebar" aria-label="全局内容">
    <header class="workbench-panel-heading"><h1>内容</h1></header>
    <div class="current-content-sidebar-body">
      <component v-if="promptPanel" :is="promptPanel.component"
        :key="frontendExtensionIdentity(promptPanel.declaration)" />
      <div v-else class="content-sidebar-status content-sidebar-warning" role="status">
        <p>提示词资源不可用</p>
        <p>可信提示词面板未安装</p>
      </div>
      <component v-if="lorebookPanel" :is="lorebookPanel.component"
        :key="frontendExtensionIdentity(lorebookPanel.declaration)" />
      <p v-for="issue in host.issues.value" :key="issue"
        class="content-sidebar-status content-sidebar-warning" role="status">{{ issue }}</p>
    </div>
  </section>
</template>

<style scoped>
.current-content-sidebar { display:flex; flex:1; flex-direction:column; min-width:0; min-height:0; }
.current-content-sidebar-body { flex:1; min-height:0; overflow:auto; }
.content-sidebar-status { margin:0; padding:10px 12px; color:#a4abb4; font-size:11px; line-height:1.6; overflow-wrap:anywhere; }
.content-sidebar-status p { margin:0; }
.content-sidebar-warning { color:#f4b4ae; }
</style>
