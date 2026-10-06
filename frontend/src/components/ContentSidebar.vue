<script setup lang="ts">
import { computed } from "vue";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { frontendExtensionIdentity } from "../domain/frontendExtensions";
import { promptFrontendExtensions } from "../plugins/promptFrontendManifest";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import ContentLibrary from "./ContentLibrary.vue";

const workspace = useWorkspaceStore();
const graph = useWorkflowGraphStore();
const generic = computed(() => graph.isGeneric(workspace.activeWorkflowId));
const host = createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock);
const promptPanelIdentity = frontendExtensionIdentity(promptFrontendExtensions[0]!);
const promptPanel = computed(() => host.extensions.value.find(row =>
  frontendExtensionIdentity(row.declaration) === promptPanelIdentity));
</script>

<template>
  <ContentLibrary v-if="!generic" view="sidebar" />
  <section v-else class="current-content-sidebar" aria-label="普通图提示词">
    <header class="workbench-panel-heading"><h1>内容</h1></header>
    <div class="current-content-sidebar-body">
      <component v-if="promptPanel" :is="promptPanel.component"
        :key="frontendExtensionIdentity(promptPanel.declaration)" />
      <p v-else-if="graph.catalogLoading" class="content-sidebar-status" role="status">读取中</p>
      <div v-else class="content-sidebar-status content-sidebar-warning" role="status">
        <p>提示词资源不可用</p>
        <p>{{ graph.catalogError ?? "当前节点目录未提供已启用的可信提示词面板" }}</p>
      </div>
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
