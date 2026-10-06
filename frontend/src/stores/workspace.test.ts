import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkspaceStore } from "./workspace";
import {
  EMPTY_WORKFLOW_ID,
  MAIN_WORKFLOW_ID,
} from "../fixtures/workflows";

beforeEach(() => setActivePinia(createPinia()));

describe("workflow workbench state", () => {
  it("opens the fixed main flow by default without run or binding authority", () => {
    const workspace = useWorkspaceStore();
    expect(workspace.activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
    expect(workspace.selectedWorkflowId).toBe(MAIN_WORKFLOW_ID);
    expect(workspace.nodes.map((node) => node.data.stage)).toEqual([
      "A",
      "B",
      "Output",
    ]);
    expect(workspace.edges).toHaveLength(2);
    expect(workspace.edges.map((edge) => [edge.source, edge.target])).toEqual([
      [workspace.nodes[0].id, workspace.nodes[1].id],
      [workspace.nodes[1].id, workspace.nodes[2].id],
    ]);
    expect(workspace.nodes.every((node) => node.id.startsWith("frontend:"))).toBe(
      true,
    );
    expect(workspace.drafts[MAIN_WORKFLOW_ID]).not.toHaveProperty("revision");
    expect(workspace.drafts[MAIN_WORKFLOW_ID]).not.toHaveProperty("runId");
    expect(workspace.drafts[MAIN_WORKFLOW_ID]).not.toHaveProperty("binding");
    expect(workspace.dirty).toBe(false);
  });

  it("separates a list selection from the currently opened graph", () => {
    const workspace = useWorkspaceStore();
    const ids = workspace.nodes.map((node) => node.id);
    const version = workspace.viewVersion;
    workspace.selectNodes([ids[0]]);
    workspace.selectWorkflow(EMPTY_WORKFLOW_ID);
    expect(workspace.selectedWorkflowId).toBe(EMPTY_WORKFLOW_ID);
    expect(workspace.activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
    expect(workspace.nodes.map((node) => node.id)).toEqual(ids);
    expect(workspace.selectedNodeIds).toEqual([ids[0]]);
    expect(workspace.viewVersion).toBe(version);
  });

  it("opens a genuinely empty workflow and clears only temporary state", () => {
    const workspace = useWorkspaceStore();
    workspace.selectNodes([workspace.nodes[0].id]);
    workspace.selectEdges([workspace.edges[0].id]);
    workspace.interactionMode = "pan";
    workspace.openWorkflow(EMPTY_WORKFLOW_ID);
    expect(workspace.activeWorkflowId).toBe(EMPTY_WORKFLOW_ID);
    expect(workspace.nodes).toEqual([]);
    expect(workspace.edges).toEqual([]);
    expect(workspace.selectedNodeIds).toEqual([]);
    expect(workspace.selectedEdgeIds).toEqual([]);
    expect(workspace.interactionMode).toBe("select");
    expect(workspace.canUndo).toBe(false);
    expect(workspace.canRedo).toBe(false);
    expect(workspace.dirty).toBe(false);
  });

  it("preserves a workflow layout, identity, edges and undo history across opens", () => {
    const workspace = useWorkspaceStore();
    const node = workspace.nodes[0];
    const initialPosition = { ...node.position };
    const ids = workspace.nodes.map((current) => current.id);
    const edges = JSON.stringify(workspace.edges);
    workspace.moveNodes([{ id: node.id, position: { x: 145, y: 315 } }]);
    expect(workspace.dirty).toBe(true);
    workspace.openWorkflow(EMPTY_WORKFLOW_ID);
    workspace.undo();
    workspace.redo();
    workspace.moveNodes([{ id: node.id, position: { x: 900, y: 900 } }]);
    expect(workspace.nodes).toEqual([]);
    expect(workspace.histories[EMPTY_WORKFLOW_ID].past).toEqual([]);
    workspace.openWorkflow(MAIN_WORKFLOW_ID);
    expect(workspace.nodes[0].position).toEqual({ x: 145, y: 315 });
    expect(workspace.nodes.map((current) => current.id)).toEqual(ids);
    expect(JSON.stringify(workspace.edges)).toBe(edges);
    expect(workspace.canUndo).toBe(true);
    workspace.undo();
    expect(workspace.nodes[0].position).toEqual(initialPosition);
    expect(workspace.dirty).toBe(false);
    expect(workspace.canRedo).toBe(true);
    workspace.openWorkflow(EMPTY_WORKFLOW_ID);
    workspace.openWorkflow(MAIN_WORKFLOW_ID);
    workspace.redo();
    expect(workspace.nodes[0].position).toEqual({ x: 145, y: 315 });
    expect(workspace.dirty).toBe(true);
  });

  it("rejects late canvas events even when reopening the same workflow", () => {
    const workspace = useWorkspaceStore();
    const node = workspace.nodes[0];
    const edgeId = workspace.edges[0].id;
    const token = workspace.currentViewToken();
    workspace.openWorkflow(EMPTY_WORKFLOW_ID);
    workspace.openWorkflow(MAIN_WORKFLOW_ID);
    workspace.selectNodes([node.id], token);
    workspace.selectEdges([edgeId], token);
    workspace.moveNodes([{ id: node.id, position: { x: 999, y: 999 } }], token);
    expect(workspace.selectedNodeIds).toEqual([]);
    expect(workspace.selectedEdgeIds).toEqual([]);
    expect(workspace.nodes[0].position).toEqual({ x: 80, y: 180 });
    expect(workspace.canUndo).toBe(false);
    expect(workspace.isCurrentView(token)).toBe(false);
  });

  it("does not create history for invalid or unchanged moves", () => {
    const workspace = useWorkspaceStore();
    const node = workspace.nodes[0];
    workspace.moveNodes([
      { id: "unknown", position: { x: 42, y: 42 } },
      { id: node.id, position: { ...node.position } },
      { id: workspace.nodes[1].id, position: { x: NaN, y: Infinity } },
    ]);
    expect(workspace.canUndo).toBe(false);
    expect(workspace.dirty).toBe(false);
    workspace.selectWorkflow("unknown");
    workspace.openWorkflow("unknown");
    expect(workspace.activeWorkflowId).toBe(MAIN_WORKFLOW_ID);
    expect(workspace.viewVersion).toBe(0);
  });

  it("copies move payloads and clears redo after a new layout edit", () => {
    const workspace = useWorkspaceStore();
    const position = { x: 190, y: 280 };
    const id = workspace.nodes[0].id;
    workspace.moveNodes([{ id, position }]);
    position.x = 950;
    expect(workspace.nodes[0].position).toEqual({ x: 190, y: 280 });
    workspace.undo();
    workspace.moveNodes([{ id, position: { x: 205, y: 310 } }]);
    expect(workspace.canRedo).toBe(false);
  });

  it("never touches existing draft storage or calls network actions", () => {
    const getItem = vi.fn();
    const setItem = vi.fn();
    const fetch = vi.fn();
    vi.stubGlobal("localStorage", { getItem, setItem });
    vi.stubGlobal("fetch", fetch);
    try {
      const workspace = useWorkspaceStore();
      workspace.selectWorkflow(EMPTY_WORKFLOW_ID);
      workspace.openWorkflow(EMPTY_WORKFLOW_ID);
      workspace.openWorkflow(MAIN_WORKFLOW_ID);
      workspace.selectNodes([workspace.nodes[0].id]);
      workspace.moveNodes([
        { id: workspace.nodes[0].id, position: { x: 95, y: 210 } },
      ]);
      workspace.undo();
      workspace.redo();
      expect(getItem).not.toHaveBeenCalled();
      expect(setItem).not.toHaveBeenCalled();
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps only saved-catalog projections when restoring a current-only workspace", () => {
    const workspace = useWorkspaceStore(), id = crypto.randomUUID();
    workspace.restoreCatalog([{ id, title: "Current workspace", description: "", nodeCount: 0, state: "draft" }]);
    workspace.restoreLayouts({ [id]: {} }, id, id);
    expect(workspace.workflows.map(workflow => workflow.id)).toEqual([id]);
    expect(Object.keys(workspace.drafts)).toEqual([id]);
    expect(Object.keys(workspace.initialLayouts)).toEqual([id]);
    expect(Object.keys(workspace.histories)).toEqual([id]);
    expect(workspace.drafts[MAIN_WORKFLOW_ID]).toBeUndefined();
    expect(workspace.activeWorkflowId).toBe(id);
    expect(workspace.selectedWorkflowId).toBe(id);
    expect(workspace.nodes).toEqual([]);
    expect(workspace.edges).toEqual([]);
    expect(workspace.canUndo).toBe(false);
    expect(workspace.dirty).toBe(false);
  });
});
