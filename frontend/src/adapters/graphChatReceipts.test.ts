import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

const scope: Record<string, any> = {};
runInNewContext(readFileSync(new URL("../../../backend/src/phase1_agent/static/graph-chat-core.js", import.meta.url), "utf8"), scope);
const { GraphChatClient, commandEnvelope, httpFailure } = scope.GraphChat;
const workflow = "00000000-0000-4000-8000-000000000011";
const session = "00000000-0000-4000-8000-000000000012";
const another = "00000000-0000-4000-8000-000000000013";
const chain = "00000000-0000-4000-8000-000000000014";
const key = "00000000-0000-4000-8000-000000000015";
const storageKey = `workflow-chat:v1:${workflow}`;
function consumer(revision = 8) {
  return { schema_version: 1, kind: "workflow.consumer", workflow_definition_id: workflow,
    definition_revision: 1, workflow_session_id: session, session_revision: revision, status: "succeeded",
    can_submit: true, available_actions: ["close"], inputs: [], nodes: [], outputs: [], history: [], diagnostics: [] };
}
function pending(action = "start") {
  return action === "create"
    ? { action, workflow_session_id: null, path: "/api/graph/consumer/sessions",
      body: { workflow_definition_id: workflow, definition_revision: 1, idempotency_key: key } }
    : { action, workflow_session_id: session, path: `/api/graph/sessions/${session}/consumer/runs`,
      body: { expected_revision: 1, idempotency_key: key, inputs: { text: "original" } } };
}
function storage(original: any) {
  const values = new Map([[storageKey, JSON.stringify({ schema_version: 1, kind: "workflow.chat-client",
    workflow_definition_id: workflow, workflow_session_id: session, pending: original })]]);
  return { getItem: (name: string) => values.get(name) ?? null,
    setItem: vi.fn((name: string, value: string) => { values.set(name, value); }) };
}
function client(request: any, original = pending()) {
  const saved = storage(original), keyFactory = vi.fn();
  const value = new GraphChatClient({ workflowId: workflow, storage: saved, request, keyFactory });
  value.accept(consumer());
  return { value, saved, original, keyFactory };
}
function matched(original: any) {
  const request = commandEnvelope(original, workflow);
  const receipt = { workflow_definition_id: workflow, definition_revision: 1,
    workflow_session_id: original.action === "create" ? another : session,
    session_revision: original.action === "create" ? 1 : 2, chain_run_id: original.action === "create" ? null : chain,
    status: original.action === "create" ? "idle" : "prepared", operation: original.action === "create" ? "create" : "start",
    idempotency_key: key };
  return { schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
    reason_code: "receipt_matched", receipt: { schema_version: 1, kind: "workflow.application-receipt",
      operation: request.operation, operation_scope: "consumer", idempotency_key: key,
      request_sha256: "a".repeat(64), authority: "service_receipt",
      target: original.workflow_session_id ? { session_id: original.workflow_session_id } : {},
      accepted: { ...receipt } }, result: { receipt } };
}

describe("static graph chat receipt-only pending custody", () => {
  it.each([404, 410, 409, 503, "network"] as const)("retains raw pending on receipt read failure %s", async status => {
    const failure = status === "network" ? new Error("offline") : httpFailure({ message: "receipt read failed" }, status);
    const request = vi.fn().mockRejectedValue(failure), { value, saved, original, keyFactory } = client(request);
    const before = saved.getItem(storageKey);
    await expect(value.command("close", { expected_revision: 999 })).rejects.toThrow();
    expect(request).toHaveBeenCalledExactlyOnceWith("/api/graph/consumer/receipts/read", {
      method: "POST", body: JSON.stringify(commandEnvelope(original, workflow)) });
    expect(value.pending).toEqual(original); expect(saved.getItem(storageKey)).toBe(before);
    expect(saved.setItem).not.toHaveBeenCalled(); expect(keyFactory).not.toHaveBeenCalled();
    expect(value.consumer.session_revision).toBe(8); expect(value.busy).toBe(false);
  });

  it.each(["application_identity_missing", "native_receipt_missing", "request_mismatch", "receipt_invalid"])(
    "retains raw pending for unresolved %s", async reason => {
      const request = vi.fn().mockResolvedValue({ schema_version: 1, kind: "workflow.application-receipt-read",
        outcome: "unresolved", reason_code: reason }), { value, saved, original } = client(request);
      const before = saved.getItem(storageKey);
      await expect(value.command()).rejects.toThrow(reason);
      expect(value.pending).toEqual(original); expect(saved.getItem(storageKey)).toBe(before);
      expect(saved.setItem).not.toHaveBeenCalled();
    });

  it.each(["kind", "outcome", "reason", "extra", "live-consumer", "scope", "target", "operation", "key", "accepted", "digest"])(
    "retains raw pending for invalid proof %s", async field => {
      const original = pending(), proof: any = matched(original);
      if (field === "kind") proof.kind = "workflow.application-command";
      if (field === "outcome") proof.outcome = "accepted";
      if (field === "reason") proof.reason_code = "other_reason";
      if (field === "extra") proof.archive = {};
      if (field === "live-consumer") proof.result.consumer = consumer();
      if (field === "scope") proof.receipt.operation_scope = "management";
      if (field === "target") proof.receipt.target.session_id = another;
      if (field === "operation") proof.receipt.operation = "run.start";
      if (field === "key") proof.receipt.idempotency_key = another;
      if (field === "accepted") proof.receipt.accepted.session_revision++;
      if (field === "digest") proof.receipt.request_sha256 = "not-a-digest";
      const { value, saved } = client(vi.fn().mockResolvedValue(proof), original), before = saved.getItem(storageKey);
      await expect(value.command()).rejects.toThrow();
      expect(value.pending).toEqual(original); expect(saved.getItem(storageKey)).toBe(before);
      expect(saved.setItem).not.toHaveBeenCalled(); expect(value.consumer.session_revision).toBe(8);
    });

  it.each(["start", "create"])("restores the entire %s pending after confirmation persistence fails", async action => {
    const original = pending(action), { value, saved } = client(vi.fn().mockResolvedValue(matched(original)), original);
    const before = saved.getItem(storageKey), generation = value.generation, observation = value.consumer;
    saved.setItem.mockImplementationOnce(() => { throw new Error("storage full"); });
    await expect(value.command()).rejects.toThrow("storage full");
    expect(value.pending).toEqual(original); expect(value.sessionId).toBe(session);
    expect(value.generation).toBe(generation); expect(value.consumer).toBe(observation);
    expect(saved.getItem(storageKey)).toBe(before); expect(value.busy).toBe(false);
  });

  it.each([
    ["pending", "success"], ["pending", "failure"], ["session", "success"], ["session", "failure"],
    ["generation", "success"], ["generation", "failure"],
  ])("fences a late %s receipt read %s", async (change, outcome) => {
    let finish!: (value: any) => void, fail!: (error: any) => void;
    const request = vi.fn(() => new Promise((resolve, reject) => { finish = resolve; fail = reject; }));
    const { value, saved, original } = client(request), before = saved.getItem(storageKey), observation = value.consumer;
    const reading = value.command();
    const replacement = { ...original, body: { ...original.body, inputs: { text: "replacement with the same key" } } };
    if (change === "pending") value.pending = replacement;
    if (change === "session") value.sessionId = another;
    if (change === "generation") value.generation++;
    if (outcome === "success") finish(matched(original));
    else fail(httpFailure({ message: "old read failed" }, 409));
    await expect(reading).rejects.toThrow();
    expect(value.pending).toEqual(change === "pending" ? replacement : original);
    expect(value.sessionId).toBe(change === "session" ? another : session);
    expect(value.consumer).toBe(observation); expect(saved.getItem(storageKey)).toBe(before);
    expect(saved.setItem).not.toHaveBeenCalled();
  });

  it("confirms an unknown create without inventing a live consumer or requiring an available mutation", async () => {
    const original = pending("create"), request = vi.fn().mockResolvedValue(matched(original));
    const { value, saved, keyFactory } = client(request, original);
    value.application = { commands: [], queries: [] };
    value.registrations = [{ old: true }]; value.eventBindings = { old: true };
    const result = await value.command("start", { inputs: { text: "must not send" } });
    expect(result.receipt.workflow_session_id).toBe(another);
    expect(value.sessionId).toBe(another); expect(value.pending).toBeNull(); expect(value.consumer).toBeNull();
    expect(value.registrations).toEqual([]); expect(value.eventBindings).toBeNull();
    expect(JSON.parse(saved.getItem(storageKey)!)).toMatchObject({ workflow_session_id: another, pending: null });
    expect(request.mock.calls[0]![0]).toBe("/api/graph/consumer/receipts/read");
    expect(keyFactory).not.toHaveBeenCalled();
  });

  it("clears a definite rejection only for a fresh initial mutation", async () => {
    const saved = storage(null), request = vi.fn().mockRejectedValue(httpFailure({ message: "stale revision" }, 409));
    const value = new GraphChatClient({ workflowId: workflow, sessionId: session, storage: saved, request, keyFactory: () => key });
    value.accept(consumer(1));
    await expect(value.command("start")).rejects.toThrow("stale revision");
    expect(request.mock.calls[0]![0]).toBe("/api/graph/consumer/commands");
    expect(value.pending).toBeNull(); expect(JSON.parse(saved.getItem(storageKey)!)).toHaveProperty("pending", null);
  });
});
