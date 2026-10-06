import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { usePreparationStore } from "./preparation";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { compilePromptItems, compilePreparationProgram } from "../domain/preparation";
import { isBackendPreparationProgram } from "../domain/preparationProgram";
import { createWorkspaceDrafts, MAIN_WORKFLOW_ID, workspaceWorkflows } from "../fixtures/workflows";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { CORE_SESSION_NOTE } from "../domain/workbenchResources";
import { createPreparationDraft } from "../fixtures/preparation";
import { createPreparationNode } from "../domain/preparation";
import { readWorkbench } from "../adapters/workbenchPersistence";

beforeEach(() => setActivePinia(createPinia()));
describe("unified workflow", () => {
  it("preserves v5 explicit cross-Agent contexts, fills missing defaults and retains them through v6", () => {
    const a = createPreparationDraft(MAIN_WORKFLOW_ID, "A"), b = createPreparationDraft(MAIN_WORKFLOW_ID, "B");
    const cross = a.nodes.find(n => n.kind === "context")!;
    cross.config.sourceBindingId = AGENT_BINDINGS.B;
    const defaultA = createPreparationNode("context", { x: 0, y: 0 }); a.nodes.push(defaultA);
    const defaultB = b.nodes.find(n => n.kind === "context")!;
    const legacy = { schemaVersion: 5, kind: "fixed-workbench", revision: 1, savedAt: "2026-10-01T00:00:00Z",
      activeWorkflowId: MAIN_WORKFLOW_ID, selectedWorkflowId: MAIN_WORKFLOW_ID,
      layouts: Object.fromEntries(Object.entries(createWorkspaceDrafts()).map(([id, draft]) => [id,
        Object.fromEntries(draft.nodes.map(node => [node.id, node.position]))])),
      preparations: [a, b], registrations: [], catalog: workspaceWorkflows,
      runtime: { schemaVersion: 1, kind: "workbench-runtime", selectedSessions: [], sessions: [], selectionPending: null },
      models: { schemaVersion: 1, drafts: [], pending: null },
      exposures: { schemaVersion: 1, publications: [], pending: null },
    };
    let raw = JSON.stringify(legacy);
    const storage = { getItem: () => raw, setItem: (_key: string, value: string) => { raw = value; } };
    const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
    expect(persistence.status).toBe("saved");
    const preparation = usePreparationStore();
    const draft = preparation.getDraft(MAIN_WORKFLOW_ID, "A");
    expect(draft.nodes.find(n => n.id === cross.id)?.config).toMatchObject({ sourceBindingId: AGENT_BINDINGS.B });
    expect(draft.nodes.find(n => n.id === defaultA.id)?.config).toMatchObject({ sourceBindingId: AGENT_BINDINGS.A });
    expect(draft.nodes.find(n => n.id === defaultB.id)?.config).toMatchObject({ sourceBindingId: AGENT_BINDINGS.B });
    const plan = compilePromptItems(preparation.getExecutionDraft(MAIN_WORKFLOW_ID, "A"));
    if (plan.config.schema_version !== 2) throw new Error("missing compiled preparation");
    expect(plan.config.preparation.nodes.find(n => n.node_id === cross.id)?.config).toEqual({ binding_id: AGENT_BINDINGS.B });
    expect(persistence.save()).toBe(true); expect(readWorkbench(storage).value?.schemaVersion).toBe(6);
    persistence.$dispose(); setActivePinia(createPinia());
    const recovered = useWorkbenchPersistenceStore(); recovered.initialize(storage);
    expect(recovered.status).toBe("saved");
    expect(usePreparationStore().getDraft(MAIN_WORKFLOW_ID, "A").nodes.find(n => n.id === cross.id)?.config)
      .toMatchObject({ sourceBindingId: AGENT_BINDINGS.B });
    recovered.$dispose();
  });
  it("preserves legacy instance IDs and slices actual assembly inputs", () => {
    const preparation = usePreparationStore();
    const a = preparation.getDraft(MAIN_WORKFLOW_ID, "A");
    const b = preparation.getDraft(MAIN_WORKFLOW_ID, "B");
    const ids = [...a.nodes, ...b.nodes].map(n => n.id);
    preparation.enableUnified(MAIN_WORKFLOW_ID);
    expect(preparation.getDraft(MAIN_WORKFLOW_ID, "A").nodes.map(n => n.id)).toEqual(ids);
    for (const stage of ["A", "B"] as const) {
      const graph = preparation.getExecutionDraft(MAIN_WORKFLOW_ID, stage);
      expect(graph.nodes.filter(n => n.kind === "assemble")).toHaveLength(1);
      const plan = compilePromptItems(graph);
      expect(plan.diagnostics.filter(d => d.severity === "error")).toEqual([]);
      expect(plan.config.schema_version).toBe(2);
      if (plan.config.schema_version === 2) expect(isBackendPreparationProgram(plan.config.preparation)).toBe(true);
    }
  });
  it("allows multiple sources and cross-assembly edges using stable bindings", () => {
    const preparation = usePreparationStore();
    preparation.enableUnified(MAIN_WORKFLOW_ID);
    const graph = preparation.getDraft(MAIN_WORKFLOW_ID, "A");
    const extra = preparation.addNode(MAIN_WORKFLOW_ID, "A", "context")!;
    preparation.updateNodeConfig(MAIN_WORKFLOW_ID, "A", extra, { sourceBindingId: AGENT_BINDINGS.B });
    const assembly = graph.nodes.find(n => n.kind === "assemble")!;
    const old = graph.edges.find(e => e.target === assembly.id && e.targetHandle === "context")!;
    preparation.removeEdge(MAIN_WORKFLOW_ID, "A", old.id);
    preparation.connect(MAIN_WORKFLOW_ID, "A", { source: extra, target: assembly.id,
      sourceHandle: "context", targetHandle: "context" });
    const plan = compilePromptItems(preparation.getExecutionDraft(MAIN_WORKFLOW_ID, "A"));
    expect(plan.diagnostics.filter(d => d.severity === "error")).toEqual([]);
    if (plan.config.schema_version === 2) expect(plan.config.preparation.nodes.find(n => n.node_id === extra)?.config)
      .toEqual({ binding_id: AGENT_BINDINGS.B });
  });
  it("keeps session data typed and converts explicitly to text", () => {
    const preparation = usePreparationStore();
    const read = preparation.addNode(MAIN_WORKFLOW_ID, "A", "session-data-read")!;
    const conversion = preparation.addNode(MAIN_WORKFLOW_ID, "A", "json-to-text")!;
    expect(preparation.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find(n => n.id === read)?.config)
      .toEqual({ definition: CORE_SESSION_NOTE });
    expect(preparation.connect(MAIN_WORKFLOW_ID, "A", {
      source: read, target: conversion, sourceHandle: "json", targetHandle: "json",
    })).toBe(true);
  });
  it("publishes exactly the same typed ports as the canvas", () => {
    const preparation = usePreparationStore();
    const source = preparation.addNode(MAIN_WORKFLOW_ID, "A", "text")!;
    const id = preparation.addNode(MAIN_WORKFLOW_ID, "A", "text-to-prompt")!;
    preparation.connect(MAIN_WORKFLOW_ID, "A", { source, target: id, sourceHandle: "text", targetHandle: "text" });
    preparation.setNodePublicOutput(MAIN_WORKFLOW_ID, "A", id, "prompt", true);
    const compiled = compilePreparationProgram(preparation.getDraft(MAIN_WORKFLOW_ID, "A"));
    const node = compiled.nodes.find(node => node.node_id === id)!;
    expect(node.output_ports).toEqual(["prompt"]);
    expect(node.public_outputs).toEqual(["prompt"]);
    expect(isBackendPreparationProgram(compiled)).toBe(true);
    node.public_outputs = ["text"];
    expect(isBackendPreparationProgram(compiled)).toBe(false);
  });
  it("explicit save produces a saved workflow and its next edit makes another copy", () => {
    let raw: string | null = null;
    const storage = { getItem: () => raw, setItem: (_key: string, value: string) => { raw = value; } };
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(storage);
    const preparation = usePreparationStore();
    const workspace = useWorkspaceStore();
    const node = preparation.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find(n => n.kind === "prompt-item")!;
    preparation.updateNodeConfig(MAIN_WORKFLOW_ID, "A", node.id, { text: "edited" });
    const firstCopy = workspace.activeWorkflowId;
    expect(workspace.activeWorkflow.state).toBe("draft");
    expect(persistence.saveWorkflow()).toBe(true);
    expect(workspace.activeWorkflow.state).toBe("saved");
    preparation.updateNodeConfig(firstCopy, "A", node.id, { text: "next" });
    expect(workspace.activeWorkflowId).not.toBe(firstCopy);
    expect(preparation.getDraft(firstCopy, "A").nodes.find(n => n.id === node.id)?.config).toMatchObject({ text: "edited" });
    persistence.discardDraft();
    expect(workspace.activeWorkflowId).toBe(firstCopy);
    persistence.$dispose();
    vi.useRealTimers();
  });
});
