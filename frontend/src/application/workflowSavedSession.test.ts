import { describe, expect, it, vi } from "vitest";
import { createWorkflowManagement } from "./workflowManagement";
import { graphClone, newGraph, type GraphEntry } from "../domain/workflowGraph";

function fixture() {
  const document = newGraph("saved", []);
  const entry: GraphEntry = { document, saved_document: graphClone(document), saved_revision: 1, session_id: null, pending: null };
  const dispatch = vi.fn(async (_workflowId: string, _request: unknown) => undefined);
  const management = createWorkflowManagement({ entry: () => entry, observedSession: () => undefined,
    observe: value => value, dispatch, commitSaved: () => true, canSubmit: () => true });
  return { entry, dispatch, management };
}
describe("saved-only session creation", () => {
  it("creates against the saved revision without publishing a dirty editor or starting a run", async () => {
    const { entry, dispatch, management } = fixture();
    entry.document.name = "unpublished";
    await management.commands.createSavedSession({ workflowId: "editor" }, {
      definitionId: entry.document.workflow_definition_id, definitionRevision: 1 });
    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(dispatch.mock.calls[0]![1]).toMatchObject({ path: "/api/graph/sessions", action: "create",
      body: { workflow_definition_id: entry.document.workflow_definition_id, definition_revision: 1 } });
    expect(entry.document.name).toBe("unpublished");
  });
  it("refuses a changed saved basis without sending any mutation", async () => {
    const { entry, dispatch, management } = fixture();
    await expect(management.commands.createSavedSession({ workflowId: "editor" }, {
      definitionId: entry.document.workflow_definition_id, definitionRevision: 2 })).rejects.toThrow("已变化");
    expect(dispatch).not.toHaveBeenCalled();
  });
});
