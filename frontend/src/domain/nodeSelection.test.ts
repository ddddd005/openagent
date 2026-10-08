import { describe, expect, it } from "vitest";
import { captureNodeSelection, nodeSelectionAllowed, nodeSelectionIssue, type NodeSelectionContext } from "./nodeSelection";
import { graphClone, newGraph, type GraphNodeType } from "./workflowGraph";

function setup(): NodeSelectionContext {
  const source: GraphNodeType = { component_id: "models.source", component_version: "3", display_name: "Gemini",
    category: "Models", config_schema: {}, default_config: {}, inputs: [], outputs: [], executable: true, is_output: false };
  const document = newGraph("test");
  document.nodes.push({ node_binding_id: crypto.randomUUID(), component_id: "models.source", component_version: "1",
    title: "My model", position: { x: 0, y: 0 }, config: { token: "keep" }, public_outputs: ["output"] });
  return { workflowId: document.workflow_definition_id, viewVersion: 0, sessionId: null, document,
    catalog: [source, { ...source, component_version: "1" }, { ...source, component_id: "models.chat" }],
    packageLock: [], locked: false, catalogLoading: false, catalogError: null };
}
describe("node selection custody", () => {
  it("captures a copied position and only permits executable declarations in the creation family", () => {
    const context = setup(), before = graphClone(context), position = { x: 20, y: 40 };
    const request = captureNodeSelection(context, "models.source", { position })!;
    position.x = 90;
    expect(request.position).toEqual({ x: 20, y: 40 });
    expect(nodeSelectionAllowed(request, context, "models.source@3")).toBe(true);
    expect(nodeSelectionAllowed(request, context, "models.chat@3")).toBe(false);
    expect(nodeSelectionAllowed(request, context, "models.source@4")).toBe(false);
    expect(nodeSelectionAllowed(request, { ...context, catalog: context.catalog.map(row => ({ ...row, executable: false })) }, "models.source@3")).toBe(false);
    expect(context).toEqual(before);
  });
  it("requires reset acknowledgement for an actual replacement and does not mutate the old node", () => {
    const context = setup(), before = graphClone(context.document);
    const request = captureNodeSelection(context, "models.source", { nodeId: context.document!.nodes[0]!.node_binding_id })!;
    expect(nodeSelectionAllowed(request, context, "models.source@3")).toBe(false);
    expect(nodeSelectionAllowed(request, context, "models.source@1", true)).toBe(false);
    expect(nodeSelectionAllowed(request, context, "models.source@3", true)).toBe(true);
    expect(nodeSelectionAllowed(request, context, "models.chat@3", true)).toBe(true);
    expect(context.document).toEqual(before);
  });
  it("rejects stale workflow, view, session, graph, catalog, package lock and locked state", () => {
    const context = setup(), request = captureNodeSelection(context, "models.source", { position: { x: 0, y: 0 } })!;
    const edited = graphClone(context.document)!;
    edited.nodes[0]!.config = { token: "changed" };
    const revisions = graphClone(context.document)!;
    revisions.revision++;
    const removed = graphClone(context.document)!;
    removed.nodes = [];
    const changed: NodeSelectionContext[] = [
      { ...context, workflowId: "other" }, { ...context, viewVersion: 1 }, { ...context, sessionId: "other" },
      { ...context, document: null }, { ...context, document: edited }, { ...context, document: removed },
      { ...context, document: revisions }, { ...context, catalog: [] },
      { ...context, packageLock: [{ package_id: "workflow.models", version: "2" }] },
      { ...context, locked: true }, { ...context, catalogLoading: true }, { ...context, catalogError: "offline" },
    ];
    for (const value of changed) {
      expect(nodeSelectionIssue(request, value)).toBeTruthy();
      expect(nodeSelectionAllowed(request, value, "models.source@3")).toBe(false);
    }
  });
  it("does not capture requests for absent nodes or unavailable editing state", () => {
    const context = setup();
    expect(captureNodeSelection(context, "models.source", { nodeId: "missing" })).toBeNull();
    for (const patch of [{ locked: true }, { catalogLoading: true }, { catalogError: "offline" }, { document: null }])
      expect(captureNodeSelection({ ...context, ...patch }, "models.source", { position: { x: 0, y: 0 } })).toBeNull();
  });
});
