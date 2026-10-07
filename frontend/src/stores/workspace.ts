import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { graphClone } from "../domain/workflowGraph";
import type { WorkspaceViewToken, WorkspaceWorkflow } from "../domain/workspace";

export const useWorkspaceStore = defineStore("workspace", () => {
  const workflows = ref<WorkspaceWorkflow[]>([]);
  const selectedWorkflowId = ref("");
  const activeWorkflowId = ref("");
  const viewVersion = ref(0);
  const interactionMode = ref<"select" | "pan">("select");
  const sidebarSection = ref<"workflows" | "providers" | "content">("workflows");
  const emptyWorkflow: WorkspaceWorkflow = { id: "", title: "工作流", description: "", nodeCount: 0 };
  const activeWorkflow = computed(() => workflows.value.find(row => row.id === activeWorkflowId.value) ?? emptyWorkflow);

  function currentViewToken(): WorkspaceViewToken {
    return { workflowId: activeWorkflowId.value, version: viewVersion.value };
  }
  function isCurrentView(token: WorkspaceViewToken) {
    return token.workflowId === activeWorkflowId.value && token.version === viewVersion.value;
  }
  function selectWorkflow(id: string) {
    if (workflows.value.some(row => row.id === id)) selectedWorkflowId.value = id;
  }
  function openWorkflow(id: string) {
    if (!workflows.value.some(row => row.id === id && !row.copyPending)) return;
    selectedWorkflowId.value = id;
    activeWorkflowId.value = id;
    interactionMode.value = "select";
    viewVersion.value++;
  }
  function registerGraphWorkflow(workflow: WorkspaceWorkflow) {
    if (workflows.value.some(row => row.id === workflow.id)) return false;
    workflows.value.push(graphClone(workflow));
    return true;
  }
  function updateWorkflowProjection(id: string, title: string, nodeCount: number) {
    const workflow = workflows.value.find(row => row.id === id);
    if (!workflow) return false;
    workflow.title = title;
    workflow.nodeCount = nodeCount;
    return true;
  }
  function setCopyPending(id: string, pending: boolean) {
    const workflow = workflows.value.find(row => row.id === id);
    if (!workflow) return false;
    workflow.copyPending = pending;
    return true;
  }
  function markWorkflowSaved(id: string, title?: string) {
    const workflow = workflows.value.find(row => row.id === id);
    if (!workflow) return false;
    workflow.state = "saved";
    if (title !== undefined) workflow.title = title;
    return true;
  }
  function removeWorkflow(id: string) {
    if (activeWorkflowId.value === id) return false;
    workflows.value = workflows.value.filter(row => row.id !== id);
    if (selectedWorkflowId.value === id) selectedWorkflowId.value = activeWorkflowId.value;
    return true;
  }
  function restoreCatalog(values: WorkspaceWorkflow[], active?: string, selected?: string) {
    workflows.value = graphClone(values);
    const fallback = values.find(row => !row.copyPending)?.id ?? values[0]?.id ?? "";
    activeWorkflowId.value = values.some(row => row.id === active && !row.copyPending) ? active! : fallback;
    selectedWorkflowId.value = values.some(row => row.id === selected) ? selected! : activeWorkflowId.value;
    viewVersion.value++;
  }
  function initializeFreshWorkspace(workflow: WorkspaceWorkflow) {
    restoreCatalog([workflow], workflow.id, workflow.id);
    interactionMode.value = "select";
    sidebarSection.value = "workflows";
  }
  return { workflows, selectedWorkflowId, activeWorkflowId, activeWorkflow, viewVersion,
    interactionMode, sidebarSection, currentViewToken, isCurrentView, selectWorkflow, openWorkflow,
    registerGraphWorkflow, updateWorkflowProjection, setCopyPending, markWorkflowSaved, removeWorkflow,
    restoreCatalog, initializeFreshWorkspace };
});
