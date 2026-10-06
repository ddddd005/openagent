import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

const scope: Record<string, any> = {};
runInNewContext(readFileSync(new URL("../../../backend/src/phase1_agent/static/graph-chat-core.js", import.meta.url), "utf8"), scope);
const { GraphChatClient, validateEventBinding, validateEventBindings, validateEventRun, httpFailure } = scope.GraphChat;
const workflow = "00000000-0000-4000-8000-000000000011", session = "00000000-0000-4000-8000-000000000012";
const node = "00000000-0000-4000-8000-000000000013", chain = "00000000-0000-4000-8000-000000000014";
const ancestor = "00000000-0000-4000-8000-000000000021", key = "00000000-0000-4000-8000-000000000015";
const binding = { event_id: "edit", schema_version: 1, display_name: "Edit state", audience: "consumer",
  target_node_ids: [node], payload_schema: { type: "object", properties: { edit: { type: "string" } }, required: ["edit"] } };
function consumer(revision = 1) {
  return { schema_version: 1, kind: "workflow.consumer", workflow_definition_id: workflow, definition_revision: 1,
    workflow_session_id: session, session_revision: revision, status: "idle", can_submit: false, available_actions: [],
    inputs: [], nodes: [], outputs: [], history: [], diagnostics: [] };
}
function packet() {
  return { schema_version: 1, kind: "workflow.event-bindings", workflow_definition_id: workflow, definition_revision: 1,
    workflow_session_id: session, session_revision: 1, can_submit: true, bindings: [binding] };
}
function storage() {
  const values = new Map<string, string>();
  return { getItem: (name: string) => values.get(name) ?? null,
    setItem: (name: string, value: string) => { values.set(name, value); } };
}
function client(request: any, saved = storage()) {
  const value = new GraphChatClient({ workflowId: workflow, sessionId: session, storage: saved, request, keyFactory: () => key });
  value.accept(consumer()); return { value, saved };
}
function receipt(envelope: any) {
  const accepted = { workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
    session_revision: 2, chain_run_id: chain, status: "prepared", operation: "event", idempotency_key: envelope.parameters.idempotency_key };
  const result = { schema_version: 1, kind: "workflow.consumer.receipt", receipt: accepted,
    consumer: { ...consumer(2), status: "prepared" } };
  return { schema_version: 1, kind: "workflow.application-command", result, receipt: {
    schema_version: 1, kind: "workflow.application-receipt", operation: "consumer.event.submit", operation_scope: "consumer",
    idempotency_key: envelope.parameters.idempotency_key, request_sha256: "a".repeat(64), authority: "service_receipt",
    target: { session_id: session }, accepted } };
}
function receiptRead(envelope: any) {
  const value = receipt(envelope);
  return { schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
    reason_code: "receipt_matched", receipt: value.receipt, result: { receipt: value.result.receipt } };
}
function eventRun() {
  return { schema_version: 1, kind: "workflow.event-run", workflow_definition_id: workflow, definition_revision: 1,
    workflow_session_id: session, session_revision: 2, source: { workflow_definition_id: ancestor, definition_revision: 3,
      workflow_session_id: ancestor }, chain_run_id: chain, execution_kind: "event",
    event: { event_id: "edit", schema_version: 1, audience: "consumer", idempotency_key: key }, status: "succeeded",
    revision: 2, targets: [node], ordered_nodes: [node], completed_nodes: [node], next_node_index: 1, diagnostic: null };
}
describe("public local event SDK", () => {
  it("allows declared event-only paths although ordinary runs cannot submit", async () => {
    const request = vi.fn(async (_path, init) => {
      const envelope = JSON.parse(init.body);
      return envelope.operation === "consumer.event.bindings" ? packet() : receipt(envelope);
    });
    const { value } = client(request); const available = await value.readEventBindings();
    expect(value.consumer.can_submit).toBe(false); expect(available.can_submit).toBe(true);
    await value.submitEvent(binding, { edit: "new state" });
    expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({ operation: "consumer.event.submit", parameters: {
      session_id: session, workflow_definition_id: workflow, definition_revision: 1, event_id: "edit",
      event_schema_version: 1, payload: { edit: "new state" }, expected_revision: 1, idempotency_key: key } });
    expect(value.pending).toBeNull();
  });
  it("reopens unknown event outboxes and reads the receipt for the exact original payload and key", async () => {
    const commands: any[] = [];
    const request = vi.fn(async (_path, init) => {
      const envelope = JSON.parse(init.body);
      if (envelope.operation === "consumer.event.bindings") return packet();
      commands.push(envelope);
      if (commands.length === 1) throw new Error("lost response after accepted effect");
      return receiptRead(envelope);
    });
    const { value, saved } = client(request);
    await expect(value.submitEvent(binding, { edit: "original" })).rejects.toThrow("lost response");
    const recovered = new GraphChatClient({ workflowId: workflow, storage: saved, request, keyFactory: () => "must-not-use" });
    expect(recovered.pending.action).toBe("event"); await recovered.command();
    expect(commands[1]).toEqual(commands[0]); expect(recovered.pending).toBeNull();
    expect(request.mock.calls.filter(([path]) => path === "/api/graph/consumer/commands")).toHaveLength(1);
    expect(request.mock.calls.filter(([path]) => path === "/api/graph/consumer/receipts/read")).toHaveLength(1);
  });
  it.each(["running", "prepared", "pausing", "paused"] as const)("rejects %s events without an event command", async status => {
    const request = vi.fn(async () => packet()), { value } = client(request);
    value.accept({ ...consumer(), status }); await value.readEventBindings();
    await expect(value.submitEvent(binding, {})).rejects.toThrow("稳定会话");
    expect(request).toHaveBeenCalledOnce(); expect(value.pending).toBeNull();
  });
  it("rejects private declarations, duplicate versions, reserved payload and external schema refs", async () => {
    expect(() => validateEventBindings({ ...packet(), bindings: [{ ...binding, audience: "management" }] },
      workflow, session, 1)).toThrow("未授权");
    expect(() => validateEventBindings({ ...packet(), bindings: [binding, binding] }, workflow, session, 1)).toThrow("重复");
    expect(() => validateEventBinding({ ...binding, payload_schema: { type: "object",
      properties: { edit: { $ref: "https://schema" } } } })).toThrow("本地引用");
    expect(validateEventBinding({ ...binding, payload_schema: { type: "object",
      properties: { literal: { const: { $ref: "https://data" }, examples: [{ $ref: "literal" }] } } } })).toBeTruthy();
    const request = vi.fn(), { value } = client(request);
    await expect(value.submitEvent(binding, { _workflow_frozen_resources: {} })).rejects.toThrow("载荷");
    expect(request).not.toHaveBeenCalled(); expect(value.pending).toBeNull();
  });
  it("rejects non-JSON public SDK values before cloning or creating an outbox", async () => {
    const cycle: Record<string, any> = {}; cycle.self = cycle;
    const request = vi.fn(), { value } = client(request);
    for (const payload of [{ edit: undefined }, { edit: NaN }, { edit: Infinity }, { edit: () => "coerced" },
      { edit: new Date() }, { edit: new Array(1) }, cycle]) {
      await expect(value.submitEvent(binding, payload)).rejects.toThrow("载荷");
    }
    expect(request).not.toHaveBeenCalled(); expect(value.pending).toBeNull();
  });
  it.each(["success", "definite-failure"] as const)("retains original request after obsolete %s", async outcome => {
    let complete!: (value: any) => void, reject!: (reason: any) => void;
    const request = vi.fn(async (_path, init) => JSON.parse(init.body).operation === "consumer.event.bindings"
      ? packet() : new Promise((resolve, fail) => { complete = resolve; reject = fail; }));
    const { value } = client(request); await value.readEventBindings();
    const submit = value.submitEvent(binding, { edit: "original" });
    value.generation++;
    if (outcome === "success") complete(receipt(JSON.parse(request.mock.calls[1][1].body)));
    else reject(httpFailure({ message: "stale" }, 409));
    await expect(submit).rejects.toThrow(); expect(value.pending.action).toBe("event");
    expect(value.consumer.session_revision).toBe(1);
  });
  it("fences late event directories and safe event reads across session changes", async () => {
    let complete!: (value: any) => void;
    const { value } = client(vi.fn(() => new Promise(resolve => { complete = resolve; })));
    const read = value.readEventBindings(); value.generation++; value.invalidateInformation();
    complete(packet()); expect(await read).toBeNull(); expect(value.eventBindings).toBeNull();
  });
  it("accepts safe inherited event metadata from another definition and rejects invalid source/private fields", () => {
    expect(validateEventRun(eventRun(), workflow, session, chain, 1).source.workflow_definition_id).toBe(ancestor);
    expect(() => validateEventRun({ ...eventRun(), source: { ...eventRun().source, workflow_session_id: "latest" } },
      workflow, session, chain, 1)).toThrow("概况");
    expect(() => validateEventRun({ ...eventRun(), payload: { private: true } }, workflow, session, chain, 1)).toThrow("概况");
  });
});
