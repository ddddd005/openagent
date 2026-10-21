import { describe, expect, it } from "vitest";
import { isWorkflowDefinitionSummary, isWorkflowSessionSummary, sessionSummaryFromView,
  type WorkflowDefinitionSummary, type WorkflowSessionSummary } from "./workflowDirectory";

const workflowId = "3f0e3a2a-2f5f-4b7c-9d1e-5a6b7c8d9e0f";
const sessionId = "8a7b6c5d-4e3f-4a2b-9c1d-0e9f8a7b6c5d";

function definitionSummary(overrides: Partial<WorkflowDefinitionSummary> = {}): WorkflowDefinitionSummary {
  return { workflow_definition_id: workflowId, display_name: "Workflow", fixed_definition_revision: 1,
    edit_sequence: 1, archived: false, node_count: 0, session_count: 0,
    has_current_configuration: true, ...overrides };
}
function sessionSummary(overrides: Partial<WorkflowSessionSummary> = {}): WorkflowSessionSummary {
  return { workflow_session_id: sessionId, workflow_definition_id: workflowId, definition_revision: 1,
    revision: 1, name: "Session", metadata_sequence: 0, archived: false,
    active_chain_run_id: null, status: "idle", ...overrides };
}

describe("workflow directory summary contract", () => {
  it("accepts the exact formal definition summary shape", () => {
    expect(isWorkflowDefinitionSummary(definitionSummary())).toBe(true);
    expect(isWorkflowDefinitionSummary(definitionSummary({ edit_sequence: null, has_current_configuration: false })))
      .toBe(true);
  });
  it("rejects a configuration document or extra fields as a summary row", () => {
    const document = { schema_version: 3, workflow_definition_id: workflowId, revision: 1,
      edit_sequence: 1, name: "Workflow", nodes: [], edges: [], object_bindings: [] };
    expect(isWorkflowDefinitionSummary(document)).toBe(false);
    expect(isWorkflowDefinitionSummary({ ...definitionSummary(), document })).toBe(false);
    expect(isWorkflowDefinitionSummary({ ...definitionSummary(), nodes: [] })).toBe(false);
  });
  it("keeps the no-current and edit_sequence flags consistent", () => {
    expect(isWorkflowDefinitionSummary(definitionSummary({ edit_sequence: null }))).toBe(false);
    expect(isWorkflowDefinitionSummary(definitionSummary({ has_current_configuration: false }))).toBe(false);
  });
  it("rejects fabricated identifiers, counts and archive flags", () => {
    expect(isWorkflowDefinitionSummary(definitionSummary({ workflow_definition_id: "not-a-uuid" }))).toBe(false);
    expect(isWorkflowDefinitionSummary(definitionSummary({ node_count: -1 }))).toBe(false);
    expect(isWorkflowDefinitionSummary(definitionSummary({ archived: "yes" as unknown as boolean }))).toBe(false);
  });
  it("accepts the exact formal session summary shape", () => {
    expect(isWorkflowSessionSummary(sessionSummary())).toBe(true);
    expect(isWorkflowSessionSummary(sessionSummary({ active_chain_run_id: "9f8e7d6c-5b4a-4321-8fed-cba987654321" })))
      .toBe(true);
  });
  it("rejects a full session view or unknown status as a session summary", () => {
    const view = { ...sessionSummary(), schema_version: 2, can_submit: true, nodes: [], chains: [] };
    expect(isWorkflowSessionSummary(view)).toBe(false);
    expect(isWorkflowSessionSummary(sessionSummary({ status: "unknown" }))).toBe(false);
    expect(isWorkflowSessionSummary(sessionSummary({ name: "bad\nname" }))).toBe(false);
  });
  it("projects an accepted full session view into a summary row without carrying detail", () => {
    const row = sessionSummaryFromView({
      workflow_session_id: sessionId, workflow_definition_id: workflowId, definition_revision: 2,
      revision: 7, status: "paused", can_submit: false, nodes: [], chains: [], outputs: [],
      data: { revision: 3, values: { private: true } },
    });
    expect(row).toEqual(sessionSummary({ definition_revision: 2, revision: 7, status: "paused", name: "" }));
    expect(row).not.toHaveProperty("nodes");
    expect(row).not.toHaveProperty("data");
  });
});
