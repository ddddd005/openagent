import { computed, ref } from "vue";
import { defineStore } from "pinia";
import type {
  WorkspaceDraft,
  WorkspaceHistory,
  WorkspaceLayout,
  WorkspaceNodeMove,
  WorkspaceViewToken,
  WorkspaceWorkflow,
} from "../domain/workspace";
import {
  createWorkspaceDrafts,
  MAIN_WORKFLOW_ID,
  workspaceWorkflows,
} from "../fixtures/workflows";
import { clonePreparation } from "../domain/preparation";
import type { WorkflowEditGuard } from "../domain/workflowIdentity";

function layoutOf(draft: WorkspaceDraft): WorkspaceLayout {
  return Object.fromEntries(
    draft.nodes.map((node) => [node.id, { ...node.position }]),
  );
}

export const useWorkspaceStore = defineStore("workspace", () => {
  const workflows = ref<WorkspaceWorkflow[]>(workspaceWorkflows.map((workflow) => ({ ...workflow, state: "saved" })));
  const drafts = ref(createWorkspaceDrafts());
  const initialLayouts = ref(Object.fromEntries(
    Object.entries(drafts.value).map(([id, draft]) => [id, layoutOf(draft)]),
  ));
  const histories = ref<Record<string, WorkspaceHistory>>(
    Object.fromEntries(
      workflows.value.map((workflow) => [workflow.id, { past: [], future: [] }]),
    ),
  );
  const selectedWorkflowId = ref(MAIN_WORKFLOW_ID);
  const activeWorkflowId = ref(MAIN_WORKFLOW_ID);
  const selectedNodeIds = ref<string[]>([]);
  const selectedEdgeIds = ref<string[]>([]);
  const viewVersion = ref(0);
  const interactionMode = ref<"select" | "pan">("select");
  const sidebarSection = ref<"workflows" | "providers" | "content">("workflows");
  let editGuard: WorkflowEditGuard | null = null;
  function setEditGuard(guard: WorkflowEditGuard) { editGuard = guard; }

  const activeWorkflow = computed(
    () => workflows.value.find((workflow) => workflow.id === activeWorkflowId.value)!,
  );
  const nodes = computed(() => drafts.value[activeWorkflowId.value].nodes);
  const edges = computed(() => drafts.value[activeWorkflowId.value].edges);
  const activeHistory = computed(
    () => histories.value[activeWorkflowId.value],
  );
  const canUndo = computed(() => activeHistory.value.past.length > 0);
  const canRedo = computed(() => activeHistory.value.future.length > 0);
  const dirty = computed(() =>
    nodes.value.some((node) => {
      const initial = initialLayouts.value[activeWorkflowId.value][node.id];
      return (
        !initial || node.position.x !== initial.x || node.position.y !== initial.y
      );
    }),
  );

  function currentViewToken(): WorkspaceViewToken {
    return {
      workflowId: activeWorkflowId.value,
      version: viewVersion.value,
    };
  }

  function isCurrentView(token: WorkspaceViewToken) {
    return (
      token.workflowId === activeWorkflowId.value &&
      token.version === viewVersion.value
    );
  }

  function selectWorkflow(id: string) {
    if (workflows.value.some((workflow) => workflow.id === id))
      selectedWorkflowId.value = id;
  }

  function clearSelection() {
    selectedNodeIds.value = [];
    selectedEdgeIds.value = [];
  }

  function openWorkflow(id: string) {
    if (!workflows.value.some((workflow) => workflow.id === id && !workflow.copyPending)) return;
    selectedWorkflowId.value = id;
    activeWorkflowId.value = id;
    clearSelection();
    interactionMode.value = "select";
    viewVersion.value += 1;
  }

  function selectNodes(
    ids: string[],
    token: WorkspaceViewToken = currentViewToken(),
  ) {
    if (!isCurrentView(token)) return;
    const validIds = new Set(nodes.value.map((node) => node.id));
    selectedNodeIds.value = [...new Set(ids)].filter((id) => validIds.has(id));
  }

  function selectEdges(
    ids: string[],
    token: WorkspaceViewToken = currentViewToken(),
  ) {
    if (!isCurrentView(token)) return;
    const validIds = new Set(edges.value.map((edge) => edge.id));
    selectedEdgeIds.value = [...new Set(ids)].filter((id) => validIds.has(id));
  }

  function restoreLayout(layout: WorkspaceLayout) {
    const id = activeWorkflowId.value;
    drafts.value[id] = {
      ...drafts.value[id],
      nodes: nodes.value.map((node) => ({
        ...node,
        position: { ...layout[node.id] },
      })),
    };
  }

  function moveNodes(
    moves: WorkspaceNodeMove[],
    token: WorkspaceViewToken = currentViewToken(),
  ) {
    if (!isCurrentView(token)) return;
    moveWorkflowNodes(activeWorkflowId.value, moves);
  }

  function moveWorkflowNodes(id: string, moves: WorkspaceNodeMove[]) {
    const draft = drafts.value[id];
    if (!draft) return;
    const positions = new Map(
      moves
        .filter(
          (move) =>
            Number.isFinite(move.position.x) &&
            Number.isFinite(move.position.y),
        )
        .map((move) => [move.id, { ...move.position }]),
    );
    if (
      !draft.nodes.some((node) => {
        const position = positions.get(node.id);
        return (
          position &&
          (position.x !== node.position.x || position.y !== node.position.y)
        );
      })
    )
      return;

    if (editGuard && !editGuard(id, target => moveWorkflowNodes(target, moves))) return;
    histories.value[id].past.push(layoutOf(draft));
    histories.value[id].past = histories.value[id].past.slice(-60);
    histories.value[id].future = [];
    drafts.value[id] = {
      ...draft,
      nodes: draft.nodes.map((node) => ({
        ...node,
        position: positions.get(node.id) ?? { ...node.position },
      })),
    };
  }

  function undo() {
    const previous = activeHistory.value.past.pop();
    if (!previous) return;
    activeHistory.value.future.push(
      layoutOf(drafts.value[activeWorkflowId.value]),
    );
    restoreLayout(previous);
  }

  function redo() {
    const next = activeHistory.value.future.pop();
    if (!next) return;
    activeHistory.value.past.push(
      layoutOf(drafts.value[activeWorkflowId.value]),
    );
    restoreLayout(next);
  }
  function exportLayouts() {
    return Object.fromEntries(Object.entries(drafts.value).map(([id, draft]) => [id, layoutOf(draft)]));
  }
  function markSaved() {
    for (const [id, draft] of Object.entries(drafts.value)) initialLayouts.value[id] = layoutOf(draft);
  }
  function restoreLayouts(layouts: Record<string, WorkspaceLayout>, active: string, selected: string) {
    for (const workflow of workflows.value) {
      const layout = layouts[workflow.id];
      drafts.value[workflow.id] = {
        ...drafts.value[workflow.id],
        nodes: Object.keys(layout ?? {}).length ? drafts.value[workflow.id].nodes.map((node) => ({ ...node, position: { ...layout[node.id] } })) : [],
      };
      histories.value[workflow.id] = { past: [], future: [] };
    }
    activeWorkflowId.value = active;
    selectedWorkflowId.value = selected;
    viewVersion.value += 1;
    clearSelection();
    markSaved();
  }
  function cloneWorkflow(sourceId: string, targetId: string) {
    const source = workflows.value.find(w => w.id === sourceId)!;
    workflows.value.push({ ...source, id: targetId, title: `${source.title}（编辑副本）`, state: "draft", sourceId });
    drafts.value[targetId] = clonePreparation(drafts.value[sourceId]!);
    initialLayouts.value[targetId] = layoutOf(drafts.value[targetId]!);
    histories.value[targetId] = { past: [], future: [] };
  }
  function registerGraphWorkflow(workflow: WorkspaceWorkflow) {
    if (workflows.value.some(row => row.id === workflow.id)) return false;
    workflows.value.push(clonePreparation(workflow));
    drafts.value[workflow.id] = { nodes: [], edges: [] };
    initialLayouts.value[workflow.id] = {};
    histories.value[workflow.id] = { past: [], future: [] };
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
    clearWorkflowHistory(id);
    return true;
  }
  function clearWorkflowHistory(id: string) {
    histories.value[id] = { past: [], future: [] };
  }
  function restoreWorkflowLayout(id: string, layout: WorkspaceLayout) {
    const draft = drafts.value[id];
    if (!draft) return false;
    drafts.value[id] = { ...draft, nodes: draft.nodes.map(node => ({
      ...node, position: layout[node.id] ? { ...layout[node.id] } : { ...node.position },
    })) };
    return true;
  }
  function removeWorkflow(id: string) {
    if (activeWorkflowId.value === id) return false;
    workflows.value = workflows.value.filter(row => row.id !== id);
    delete drafts.value[id];
    delete histories.value[id];
    delete initialLayouts.value[id];
    if (selectedWorkflowId.value === id) selectedWorkflowId.value = activeWorkflowId.value;
    return true;
  }
  function saveDraft() {
    if (activeWorkflow.value.state !== "draft") return;
    activeWorkflow.value.state = "saved";
    activeWorkflow.value.title = activeWorkflow.value.title.replace(/（编辑副本）$/, "（副本）");
    markSaved();
  }
  function restoreCatalog(values: WorkspaceWorkflow[]) {
    workflows.value = clonePreparation(values);
    const defaults = createWorkspaceDrafts();
    for (const workflow of workflows.value) {
      drafts.value[workflow.id] ??= clonePreparation(workflow.nodeCount ? defaults[MAIN_WORKFLOW_ID]! : defaults["frontend:empty-test"]!);
      histories.value[workflow.id] ??= { past: [], future: [] };
      initialLayouts.value[workflow.id] ??= layoutOf(drafts.value[workflow.id]!);
    }
  }

  return {
    workflows,
    drafts,
    initialLayouts,
    histories,
    selectedWorkflowId,
    activeWorkflowId,
    activeWorkflow,
    nodes,
    edges,
    selectedNodeIds,
    selectedEdgeIds,
    viewVersion,
    interactionMode,
    sidebarSection,
    canUndo,
    canRedo,
    dirty,
    currentViewToken,
    isCurrentView,
    selectWorkflow,
    openWorkflow,
    selectNodes,
    selectEdges,
    clearSelection,
    moveNodes,
    undo,
    redo,
    exportLayouts,
    restoreLayouts,
    markSaved,
    cloneWorkflow, saveDraft, restoreCatalog, setEditGuard,
    registerGraphWorkflow, updateWorkflowProjection, setCopyPending, markWorkflowSaved,
    clearWorkflowHistory, restoreWorkflowLayout, removeWorkflow,
  };
});
