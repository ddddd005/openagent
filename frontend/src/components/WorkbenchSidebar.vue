<script setup lang="ts">
import { ref, watch } from "vue";
import { BookOpen, Cable, PanelLeftClose, PanelLeftOpen, Workflow } from "lucide-vue-next";
import { useWorkbenchSidebar } from "../composables/useWorkbenchSidebar";
import WorkflowSidebar from "./WorkflowSidebar.vue";
import ProviderSidebar from "./ProviderSidebar.vue";
import ContentSidebar from "./ContentSidebar.vue";

type SidebarSection = "workflows" | "providers" | "content";
const emit = defineEmits<{ collapsed: [value: boolean] }>();
const section = ref<SidebarSection>("workflows");
const { sidebarCollapsed, toggleSidebar, expandSidebar } = useWorkbenchSidebar();
watch(sidebarCollapsed, value => emit("collapsed", value), { immediate: true });
function openSection(value: SidebarSection) {
  section.value = value;
  expandSidebar();
}
</script>

<template>
  <aside class="workbench-sidebar" aria-label="工作区导航">
    <nav class="workbench-navigation-rail" aria-label="主导航">
      <button class="workbench-tool-button workbench-sidebar-toggle" type="button"
        :title="sidebarCollapsed ? '展开左侧栏' : '收起左侧栏'"
        :aria-label="sidebarCollapsed ? '展开左侧栏' : '收起左侧栏'"
        :aria-expanded="!sidebarCollapsed" aria-controls="workbench-sidebar-panel" @click="toggleSidebar">
        <PanelLeftOpen v-if="sidebarCollapsed" :size="18" aria-hidden="true" />
        <PanelLeftClose v-else :size="18" aria-hidden="true" />
      </button>
      <button class="workbench-rail-button" :class="{ 'is-active': section === 'workflows' }"
        type="button" title="工作流" aria-label="工作流"
        :aria-current="section === 'workflows' ? 'page' : undefined" @click="openSection('workflows')">
        <Workflow :size="19" /><span>工作流</span>
      </button>
      <button class="workbench-rail-button" :class="{ 'is-active': section === 'providers' }"
        type="button" title="供应商" aria-label="供应商"
        :aria-current="section === 'providers' ? 'page' : undefined" @click="openSection('providers')">
        <Cable :size="19" /><span>供应商</span>
      </button>
      <button class="workbench-rail-button" :class="{ 'is-active': section === 'content' }"
        type="button" title="内容" aria-label="内容"
        :aria-current="section === 'content' ? 'page' : undefined" @click="openSection('content')">
        <BookOpen :size="19" /><span>内容</span>
      </button>
    </nav>
    <div id="workbench-sidebar-panel" v-show="!sidebarCollapsed" class="workbench-sidebar-panel">
      <WorkflowSidebar v-show="section === 'workflows'" />
      <ProviderSidebar v-show="section === 'providers'" />
      <ContentSidebar v-show="section === 'content'" />
    </div>
  </aside>
</template>
