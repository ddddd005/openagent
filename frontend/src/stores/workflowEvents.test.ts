import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { graphClone } from "../domain/workflowGraph";
import { frontendCatalog, frontendSession } from "../testUtils/frontendFixture";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

function fixture() {
  const graph = useWorkflowGraphStore(); graph.setPersistenceGuard(() => true); graph.catalog = graphClone(frontendCatalog);
  const id = graph.createWorkflow(), node = graph.addNode("tools.current-input@1", { x: 0, y: 0 })!;
  const binding = { event_id: "edit", schema_version: 1, display_name: "Edit", audience: "consumer" as const,
    target_node_ids: [node], payload_schema: { type: "object" as const } };
  graph.setEventBindings([binding]);
  const session = frontendSession(id); session.status = "idle";
  graph.entries[id].session_id = session.workflow_session_id; graph.views[session.workflow_session_id] = session;
  graph.entries[id].saved_revision = 1; graph.entries[id].saved_document = graphClone(graph.document!);
  return { graph, id, node, binding, session };
}
function response(result: Record<string, unknown>, operation?: string, parameters?: Record<string, unknown>) {
  if (!operation || !parameters) return new Response(JSON.stringify(result));
  const fields = ["workflow_definition_id", "definition_revision", "workflow_session_id", "revision", "data_revision",
    "head_revision", "head_commit_id", "status", "active_chain_run_id", "selected_chain_run_id"];
  return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command", result,
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation, operation_scope: "management",
      idempotency_key: parameters.idempotency_key, request_sha256: "a".repeat(64), authority: "service_receipt",
      target: { session_id: parameters.session_id },
      accepted: Object.fromEntries(fields.filter(key => key in result).map(key => [key, result[key]])) } }));
}
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose(); vi.unstubAllGlobals(); });
describe("local event management and original outbox", () => {
  it("submits only the declared event and exact chosen session without saving or creating a session", async () => {
    const { graph, id, session, binding } = fixture(), requests: Record<string, any>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_path, init) => {
      const request = JSON.parse(init.body); requests.push(request);
      if (request.operation === "event.bindings") return response({ schema_version: 1, kind: "workflow.event-bindings",
        workflow_definition_id: id, definition_revision: 1, workflow_session_id: session.workflow_session_id,
        session_revision: 1, can_submit: true, bindings: [binding] });
      return response({ ...session, revision: 2, status: "prepared", can_submit: false } as unknown as Record<string, unknown>,
        request.operation, request.parameters);
    }));
    expect(await graph.submitEvent("edit", 1, { edit: "new state" })).toBe(true);
    expect(requests.map(row => row.operation)).toEqual(["event.bindings", "event.submit"]);
    expect(requests[1].parameters).toMatchObject({ session_id: session.workflow_session_id,
      workflow_definition_id: id, definition_revision: 1, event_id: "edit", event_schema_version: 1,
      expected_revision: 1, payload: { edit: "new state" } });
    expect(graph.pending).toBeNull();
  });
  it.each(["paused", "running", "prepared"] as const)("rejects %s events before any request", async status => {
    const { graph, session } = fixture(); session.status = status;
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect(await graph.submitEvent("edit", 1, {})).toBe(false); expect(fetcher).not.toHaveBeenCalled();
    expect(graph.pending).toBeNull();
  });
  it("rejects unsaved definitions, unselected sessions and reserved payload without implicit setup", async () => {
    const { graph, id } = fixture(), fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect(await graph.submitEvent("edit", 1, { _workflow_frozen_resources: {} })).toBe(false);
    graph.entries[id].saved_document!.name = "previous definition";
    expect(await graph.submitEvent("edit", 1, {})).toBe(false);
    graph.entries[id].session_id = null;
    expect(await graph.submitEvent("edit", 1, {})).toBe(false); expect(fetcher).not.toHaveBeenCalled();
  });
  it("persists unknown event requests and restores their original payload and idempotency key", async () => {
    const { graph, id, session, binding } = fixture();
    vi.stubGlobal("fetch", vi.fn(async (_path, init) => {
      const request = JSON.parse(init.body);
      if (request.operation === "event.bindings") return response({ schema_version: 1, kind: "workflow.event-bindings",
        workflow_definition_id: id, definition_revision: 1, workflow_session_id: session.workflow_session_id,
        session_revision: 1, can_submit: true, bindings: [binding] });
      throw new Error("lost response");
    }));
    expect(await graph.submitEvent("edit", 1, { edit: "original" })).toBe(false);
    const original = graphClone(graph.pending!); expect(original.action).toBe("event");
    expect(graph.restoreSnapshot(graph.storeSnapshot())).toBe(true); expect(graph.pending).toEqual(original);
    const snapshot = graph.storeSnapshot();
    snapshot.entries[id].pending!.body.payload = { _workflow_frozen_resources: {} };
    expect(graph.restoreSnapshot(snapshot)).toBe(false);
  });
  it("preserves declaration targets across a saved-definition edit copy and leaves node copies outside the route", () => {
    const { graph, id, node, binding } = fixture(); graph.entries[id].session_id = null;
    useWorkspaceStore().markWorkflowSaved(id);
    const copied = graph.setEventBindings([{ ...binding, display_name: "Changed" }])!;
    expect(copied).not.toBe(id); expect(graph.document!.event_bindings![0].target_node_ids).toEqual([node]);
    expect(graph.entries[id].document.event_bindings![0].display_name).toBe("Edit");
    graph.selectedNodeIds = [node]; graph.duplicateSelection();
    expect(graph.document!.event_bindings![0].target_node_ids).toEqual([node]);
  });
});
