import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

const scope: Record<string, any> = {};
runInNewContext(readFileSync(new URL("../../../backend/src/phase1_agent/static/graph-chat-core.js", import.meta.url), "utf8"), scope);
const { GraphChatClient } = scope.GraphChat;
const workflow = "00000000-0000-4000-8000-000000000011";
const session = "00000000-0000-4000-8000-000000000012";
const other = "00000000-0000-4000-8000-000000000013";
let index = 100;
const keyFactory = () => `00000000-0000-4000-8000-${String(index++).padStart(12, "0")}`;
const clone = (value: any) => JSON.parse(JSON.stringify(value));
function port() {
  const records = new Map<string, any>();
  const read = vi.fn(async (address: any) => clone(records.get(JSON.stringify(address)) ?? null));
  const save = vi.fn(async (request: any) => {
    const key = JSON.stringify(request.address), previous = records.get(key);
    if ((previous?.sequence ?? 0) !== request.expected_sequence) throw Object.assign(new Error("stale"), { definite: true });
    const record = { schema_version: 1, kind: "workflow.local-draft", address: clone(request.address),
      format_version: 1, sequence: request.expected_sequence + 1, updated_at: "2026-10-09T00:00:00Z",
      value: clone(request.value), deleted: request.value === null };
    records.set(key, record); return clone(record);
  });
  return { records, read, save };
}
const state = (sid: string | null, pending: any = null) => ({ schema_version: 1, kind: "workflow.chat-client",
  workflow_definition_id: workflow, workflow_session_id: sid, pending });
const pending = (sid = session) => ({ action: "start", workflow_session_id: sid,
  path: `/api/graph/sessions/${sid}/consumer/runs`,
  body: { expected_revision: 1, idempotency_key: keyFactory(), inputs: { text: "original" } } });
function client(draftPort = port(), sid: string | null = session, request: any = vi.fn(async () => { throw new Error("unexpected"); })) {
  return new GraphChatClient({ workflowId: workflow, sessionId: sid, draftPort, request, keyFactory });
}

describe("consumer local draft custody", () => {
  it("preserves invalid and unfinished input across clients without browser persistence", async () => {
    const drafts = port(), first = client(drafts);
    const address = first.draftAddress("chat-input", "inputs:1", session);
    await first.saveDraft(address, { prompt: "{unfinished", count: "1.", text: "long draft" });
    const reopened = client(drafts);
    expect(await reopened.readDraft(address)).toEqual({ prompt: "{unfinished", count: "1.", text: "long draft" });
  });
  it("does not let a stored selection replace the explicitly requested session", async () => {
    const drafts = port(), first = client(drafts);
    await first.saveDraft(first.draftAddress("chat-client", "state"), state(other));
    const reopened = client(drafts);
    await reopened.restoreLocalState();
    expect(reopened.sessionId).toBe(session);
    expect(reopened.pending).toBeNull();
  });
  it("blocks a foreign pending request and leaves both its durable body and requested binding unchanged", async () => {
    const drafts = port(), first = client(drafts);
    const original = state(other, pending(other));
    await first.saveDraft(first.draftAddress("chat-client", "state"), original);
    const reopened = client(drafts);
    await expect(reopened.restoreLocalState()).rejects.toThrow("另一会话");
    expect(reopened.sessionId).toBe(session); expect(reopened.pending).toBeNull();
    expect(await first.readDraft(first.draftAddress("chat-client", "state"))).toEqual(original);
  });
  it("rejects wrong-address responses without accepting their sequence", async () => {
    const drafts = port(), first = client(drafts), address = first.draftAddress("chat-input", "inputs:1", session);
    await first.saveDraft(address, "draft");
    drafts.read.mockImplementationOnce(async () => ({ ...clone(drafts.records.get(JSON.stringify(address))),
      address: { ...address, workflow_session_id: other } }));
    const reopened = client(drafts);
    await expect(reopened.readDraft(address)).rejects.toThrow("归属");
  });
  it.each(["timestamp", "deleted"])("rejects malformed %s records without treating them as valid drafts", async field => {
    const drafts = port(), first = client(drafts), address = first.draftAddress("chat-input", "inputs:1", session);
    await first.saveDraft(address, "draft");
    const malformed = clone(drafts.records.get(JSON.stringify(address)));
    if (field === "timestamp") malformed.updated_at = "not-a-date";
    else malformed.deleted = true;
    drafts.read.mockImplementationOnce(async () => malformed);
    await expect(client(drafts).readDraft(address)).rejects.toThrow("格式");
  });
  it("fails before business dispatch when local intent cannot be confirmed", async () => {
    const drafts = port(), request = vi.fn(), value = client(drafts, null, request);
    drafts.save.mockRejectedValueOnce(new Error("disk failed"));
    await expect(value.command("create", { definition_revision: 1 })).rejects.toThrow("disk failed");
    expect(request).not.toHaveBeenCalled(); expect(value.busy).toBe(false);
  });
  it("serializes writes with CAS and never silently overwrites another client's revision", async () => {
    const drafts = port(), first = client(drafts), second = client(drafts);
    const address = first.draftAddress("chat-input", "inputs:1", session);
    await first.readDraft(address); await second.readDraft(address);
    await first.saveDraft(address, "first");
    await expect(second.saveDraft(address, "second")).rejects.toThrow("stale");
    expect(await first.readDraft(address)).toBe("first");
  });
  it("requires explicit legacy import and never executes the imported operation", async () => {
    const drafts = port(), request = vi.fn(), legacy = new Map([["workflow-chat:v1:" + workflow, JSON.stringify(state(session, pending()))]]);
    const value = new GraphChatClient({ workflowId: workflow, sessionId: session, draftPort: drafts, request, keyFactory,
      legacyStorage: { getItem: (key: string) => legacy.get(key), removeItem: (key: string) => legacy.delete(key) } });
    await expect(value.restoreLocalState()).rejects.toThrow("明确导入");
    await value.importLegacyPending();
    expect(value.pending.action).toBe("start"); expect(request).not.toHaveBeenCalled(); expect(legacy.size).toBe(0);
  });
  it("confirms intent before dispatch and only reads the old receipt after reopening an unknown request", async () => {
    const drafts = port(), calls: any[] = [];
    const request = vi.fn(async (path: string, options: any) => {
      calls.push([path, JSON.parse(options.body)]);
      const durable = [...drafts.records.values()][0];
      expect(durable.value.pending.action).toBe("create");
      throw new Error("transport unknown");
    });
    const first = client(drafts, null, request);
    await expect(first.command("create", { definition_revision: 1 })).rejects.toThrow("transport unknown");
    const reopened = client(drafts, null, request);
    await reopened.restoreLocalState();
    await expect(reopened.command("create")).rejects.toThrow("transport unknown");
    expect(calls.map(row => row[0])).toEqual(["/api/graph/consumer/commands", "/api/graph/consumer/receipts/read"]);
    expect(calls[1][1]).toEqual(calls[0][1]);
  });
  it("uses only consumer draft endpoints with exact-address application receipts by default", async () => {
    const memory = port(), request = vi.fn(async (path: string, options: any) => {
      const envelope = JSON.parse(options.body);
      if (path === "/api/graph/consumer/queries") {
        expect(envelope.operation).toBe("consumer.draft.read");
        return memory.read(envelope.parameters.address);
      }
      expect(path).toBe("/api/graph/consumer/commands");
      expect(envelope.operation).toBe("consumer.draft.save");
      const result = await memory.save(envelope.parameters);
      return { schema_version: 1, kind: "workflow.application-command", result,
        receipt: { schema_version: 1, kind: "workflow.application-receipt",
          operation: envelope.operation, operation_scope: "consumer", target: {},
          request_sha256: "1".repeat(64), authority: "service_receipt",
          idempotency_key: envelope.parameters.idempotency_key,
          accepted: { draft_address: result.address, draft_sequence: result.sequence,
            draft_format_version: 1, deleted: result.deleted } } };
    });
    const first = new GraphChatClient({ workflowId: workflow, sessionId: session, request, keyFactory });
    const address = first.draftAddress("chat-input", "message:1", session);
    await first.saveDraft(address, "raw");
    expect(await first.readDraft(address)).toBe("raw");
    expect(request.mock.calls.every(row => row[0].includes("/consumer/"))).toBe(true);
  });
  it.each(["authority", "request_sha256", "accepted"])("rejects a mismatched HTTP receipt %s and keeps the original draft intent", async field => {
    const memory = port(), request = vi.fn(async (path: string, options: any) => {
      const envelope = JSON.parse(options.body);
      if (path === "/api/graph/consumer/queries") return null;
      const result = await memory.save(envelope.parameters);
      const receipt: any = { schema_version: 1, kind: "workflow.application-receipt",
        operation: envelope.operation, operation_scope: "consumer", target: {},
        request_sha256: "1".repeat(64), authority: "service_receipt",
        idempotency_key: envelope.parameters.idempotency_key,
        accepted: { draft_address: result.address, draft_sequence: result.sequence,
          draft_format_version: 1, deleted: result.deleted } };
      if (field === "accepted") receipt.accepted.draft_address = { ...result.address, workflow_session_id: other };
      else receipt[field] = "wrong";
      return { schema_version: 1, kind: "workflow.application-command", result, receipt };
    });
    const first = new GraphChatClient({ workflowId: workflow, sessionId: session, request, keyFactory });
    const address = first.draftAddress("chat-input", "message:1", session);
    await expect(first.saveDraft(address, "original")).rejects.toThrow("不匹配");
    expect(first.draftIntents.get(JSON.stringify(address)).value).toBe("original");
  });
});
