import { afterEach, describe, expect, it, vi } from "vitest";
import { assertEventPayload, readWorkflowEvent } from "./workflowEvents";

afterEach(() => vi.unstubAllGlobals());
describe("safe event metadata query", () => {
  it("rejects values that JSON.stringify would silently rewrite before an outbox is created", () => {
    const cycle: Record<string, unknown> = {}; cycle.self = cycle;
    for (const payload of [{ edit: undefined }, { edit: NaN }, { edit: Infinity }, { edit: () => "coerced" },
      { edit: new Date() }, { edit: [undefined] }, { edit: new Array(1) }, cycle]) {
      expect(() => assertEventPayload(payload)).toThrow("JSON");
    }
    expect(() => assertEventPayload({ edit: { value: ["text", 1, null, true] } })).not.toThrow();
  });
  it("accepts inherited source ownership but rejects payload/private fields and wrong sources", async () => {
    const target = { sessionId: crypto.randomUUID(), definitionId: crypto.randomUUID(), definitionRevision: 2 },
      source = { workflow_definition_id: crypto.randomUUID(), workflow_session_id: crypto.randomUUID(), definition_revision: 1 },
      chainId = crypto.randomUUID(), node = crypto.randomUUID();
    const value = { schema_version: 1, kind: "workflow.event-run", workflow_definition_id: target.definitionId,
      definition_revision: target.definitionRevision, workflow_session_id: target.sessionId, session_revision: 3,
      source, chain_run_id: chainId, execution_kind: "event", event: { event_id: "edit", schema_version: 1,
        audience: "consumer", idempotency_key: crypto.randomUUID() }, status: "succeeded", revision: 2,
      targets: [node], ordered_nodes: [node], completed_nodes: [node], next_node_index: 1, diagnostic: null };
    const fetcher = vi.fn(async () => new Response(JSON.stringify(value))); vi.stubGlobal("fetch", fetcher);
    expect((await readWorkflowEvent(target, chainId)).source).toEqual(source);
    expect(fetcher).toHaveBeenCalledWith("/api/graph/queries", expect.objectContaining({ body: JSON.stringify({
      operation: "event.read", parameters: { session_id: target.sessionId, chain_id: chainId },
    }) }));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...value, payload: { private: true } }))));
    await expect(readWorkflowEvent(target, chainId)).rejects.toThrow("概况");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...value, source: { ...source, workflow_session_id: "latest" } }))));
    await expect(readWorkflowEvent(target, chainId)).rejects.toThrow("来源");
  });
});
