import { describe, expect, it } from "vitest";
import { isSavedWorkbench, type SavedWorkbench } from "./workbenchPersistence";
import { graphClone, newGraph } from "../domain/workflowGraph";
function fixture(): SavedWorkbench {
  const document = newGraph("Current workspace"), id = document.workflow_definition_id;
  return { schemaVersion: 7, kind: "graph-workbench", revision: 1, savedAt: "2026-10-11T00:00:00Z",
    activeWorkflowId: id, selectedWorkflowId: id,
    catalog: [{ id, title: document.name, description: "", nodeCount: 0, state: "saved" }],
    graph: { schema_version: 1, entries: { [id]: {
      document, saved_revision: 1, session_id: null, pending: null,
    } } } };
}
describe("current backend workbench format", () => {
  it("validates one current snapshot without browser migrations", () => {
    const value = fixture();
    expect(isSavedWorkbench(value)).toBe(true);
    expect(isSavedWorkbench({ ...value, schemaVersion: 6, kind: "unified-workbench" })).toBe(false);
    expect(isSavedWorkbench({ ...value, schemaVersion: 99 })).toBe(false);
  });
  it("accepts an empty directory after the last definition is deleted", () => {
    expect(isSavedWorkbench({ ...fixture(), activeWorkflowId: "", selectedWorkflowId: "", catalog: [],
      graph: { schema_version: 1, entries: {} } })).toBe(true);
  });
  it("requires exact catalog membership and valid management flags", () => {
    const value = fixture(), id = value.activeWorkflowId;
    expect(isSavedWorkbench({ ...value, activeWorkflowId: crypto.randomUUID() })).toBe(false);
    value.catalog[0].archived = true; value.catalog[0].deleted = false;
    expect(isSavedWorkbench(value)).toBe(true);
    expect(isSavedWorkbench({ ...value, graph: { schema_version: 1, entries: {} } })).toBe(false);
    value.graph.entries[id].pending = { action: "definition-delete",
      path: `/api/graph/definitions/${id}/delete`,
      body: { expected_revision: 1, idempotency_key: crypto.randomUUID() } };
    expect(isSavedWorkbench(value)).toBe(true);
  });
  it("rejects retired product nodes rather than reconstructing a compatible workspace", () => {
    const value = fixture(), entry = value.graph.entries[value.activeWorkflowId];
    entry.document.nodes.push({ node_binding_id: crypto.randomUUID(), component_id: "workflow.text",
      component_version: "1", title: "Old", position: { x: 0, y: 0 }, config: {} });
    expect(isSavedWorkbench(value)).toBe(false);
    const current = graphClone(value);
    current.graph.entries[value.activeWorkflowId].document.nodes[0].component_id = "independent.current";
    expect(isSavedWorkbench(current)).toBe(true);
  });
});
