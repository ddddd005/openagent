import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { graphClone } from "../domain/workflowGraph";
import type { WorkspaceViewToken, WorkspaceWorkflow } from "../domain/workspace";
import type { WorkflowDefinitionSummary } from "../domain/workflowDirectory";

export const useWorkspaceStore = defineStore("workspace", () => {
  const workflows = ref<WorkspaceWorkflow[]>([]);
  const selectedWorkflowId = ref("");
  const activeWorkflowId = ref("");
  const viewVersion = ref(0);
  const interactionMode = ref<"select" | "pan">("select");
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
  /** Replace the formal directory projection with summary rows.
   *
   * Only formal summary rows are projected here. Local drafts, pending copies and
   * rows whose deletion is still being verified are retained untouched; a
   * directory refresh never turns a summary into an editor entry or removes an
   * original request that is still waiting for verification. Because the
   * refresh requests the complete formal directory (archived included), a
   * previously projected formal row that is now absent means the workflow was
   * deleted elsewhere; it must disappear from the directory and its formal count.
   * Locally created rows that have not yet been seen in a summary stay, so an
   * in-flight summary cannot win over a newer local acceptance.
   */
  function applyDefinitionSummaries(rows: readonly WorkflowDefinitionSummary[],
    options: { retainMissing?: (workflowId: string) => boolean } = {}) {
    for (const row of rows) {
      const existing = workflows.value.find(item => item.id === row.workflow_definition_id);
      if (existing) {
        if (existing.deleted || existing.copyPending) continue;
        if (existing.state !== "draft") existing.title = row.display_name;
        existing.nodeCount = existing.state === "draft" ? existing.nodeCount : row.node_count;
        // Archiving is one-way in the workflow domain: a stale directory row may
        // not clear an archive flag confirmed by a management receipt.
        existing.archived = existing.archived === true || row.archived;
        existing.state = existing.state === "draft" ? "draft" : "saved";
        existing.fixedDefinitionRevision = row.fixed_definition_revision;
        existing.editSequence = row.edit_sequence;
        existing.hasCurrentConfiguration = row.has_current_configuration;
        existing.sessionCount = row.session_count;
        existing.fromSummary = true;
        continue;
      }
      workflows.value.push({
        id: row.workflow_definition_id, title: row.display_name, description: "",
        nodeCount: row.node_count, state: "saved", archived: row.archived,
        fixedDefinitionRevision: row.fixed_definition_revision, editSequence: row.edit_sequence,
        hasCurrentConfiguration: row.has_current_configuration, sessionCount: row.session_count,
        fromSummary: true,
      });
    }
    const present = new Set(rows.map(row => row.workflow_definition_id));
    const removed = new Set(workflows.value
      .filter(row => !present.has(row.id) && row.fromSummary === true && row.state === "saved"
        && !row.copyPending && !row.deleted && !(options.retainMissing?.(row.id)))
      .map(row => row.id));
    if (!removed.size) return;
    workflows.value = workflows.value.filter(row => !removed.has(row.id));
    if (removed.has(activeWorkflowId.value)) {
      const fallback = workflows.value.find(row => row.state === "saved" && !row.deleted
        && !row.copyPending)?.id ?? workflows.value.find(row => !row.copyPending)?.id ?? "";
      activeWorkflowId.value = fallback;
      viewVersion.value++;
    }
    if (!workflows.value.some(row => row.id === selectedWorkflowId.value))
      selectedWorkflowId.value = activeWorkflowId.value;
  }
  /** Formal directory count; drafts, unconfirmed copies and deletion-pending rows are excluded. */
  function formalWorkflowCount(includeArchived = false) {
    return workflows.value.filter(row => row.state === "saved" && !row.deleted && !row.copyPending
      && (includeArchived || !row.archived)).length;
  }
  function setCopyPending(id: string, pending: boolean) {
    const workflow = workflows.value.find(row => row.id === id);
    if (!workflow) return false;
    workflow.copyPending = pending;
    return true;
  }
  function setWorkflowManagement(id: string, archived: boolean, deleted = false) {
    const workflow = workflows.value.find(row => row.id === id);
    if (!workflow) return false;
    workflow.archived = archived;
    workflow.deleted = deleted;
    return true;
  }
  /** Record the accepted formal current counters on the directory row. */
  function setWorkflowCurrent(id: string, fixedDefinitionRevision: number, editSequence: number) {
    const workflow = workflows.value.find(row => row.id === id);
    if (!workflow) return false;
    workflow.fixedDefinitionRevision = fixedDefinitionRevision;
    workflow.editSequence = editSequence;
    workflow.hasCurrentConfiguration = true;
    return true;
  }
  function markWorkflowSaved(id: string, title?: string) {
    const workflow = workflows.value.find(row => row.id === id);
    if (!workflow) return false;
    workflow.state = "saved";
    if (title !== undefined) workflow.title = title;
    return true;
  }
  function markWorkflowDraft(id: string) {
    const workflow = workflows.value.find(row => row.id === id);
    if (!workflow) return false;
    workflow.state = "draft";
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
  }
  return { workflows, selectedWorkflowId, activeWorkflowId, activeWorkflow, viewVersion,
    interactionMode, currentViewToken, isCurrentView, selectWorkflow, openWorkflow,
    registerGraphWorkflow, updateWorkflowProjection, applyDefinitionSummaries, formalWorkflowCount,
    setCopyPending, setWorkflowManagement, setWorkflowCurrent, markWorkflowSaved, markWorkflowDraft, removeWorkflow,
    restoreCatalog, initializeFreshWorkspace };
});
