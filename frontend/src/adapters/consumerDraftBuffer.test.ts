import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { afterEach, describe, expect, it, vi } from "vitest";

const scope: Record<string, any> = {
  setTimeout: (...args: Parameters<typeof setTimeout>) => setTimeout(...args),
  clearTimeout: (timer: ReturnType<typeof setTimeout>) => clearTimeout(timer),
};
runInNewContext(readFileSync(new URL("../../../backend/src/phase1_agent/static/graph-chat-core.js", import.meta.url), "utf8"), scope);
const { createDraftBuffer, GraphChatClient } = scope.GraphChat;
const workflow = "00000000-0000-4000-8000-000000000011";
const session = "00000000-0000-4000-8000-000000000012";
const address = { scope: "consumer", kind: "chat-input", key: "tavern:message:1",
  workflow_definition_id: workflow, workflow_session_id: session };
afterEach(() => { vi.useRealTimers(); });

describe("consumer raw draft buffering", () => {
  it("coalesces rapid input to one latest-value write after the short delay", async () => {
    vi.useFakeTimers();
    const write = vi.fn(async () => {}), onState = vi.fn(), buffer = createDraftBuffer({ write, onState });
    for (let index = 0; index < 50; index++) buffer.stage(address, "raw:" + index);
    expect(buffer.hasUnsaved()).toBe(true);
    await vi.advanceTimersByTimeAsync(199);
    expect(write).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(write).toHaveBeenCalledExactlyOnceWith(address, "raw:49");
    expect(buffer.hasUnsaved()).toBe(false);
    expect(onState.mock.calls.at(-1)?.[1]).toEqual({ status: "saved", error: "" });
  });
  it("keeps only the latest edit behind an in-flight write and flush waits for both receipts", async () => {
    vi.useFakeTimers();
    let finish!: () => void;
    const write = vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }))
      .mockResolvedValue(undefined);
    const buffer = createDraftBuffer({ write });
    buffer.stage(address, "original");
    await vi.advanceTimersByTimeAsync(200);
    for (let index = 0; index < 40; index++) buffer.stage(address, { json: "{partial:" + index });
    const flushed = vi.fn();
    const operation = buffer.flush().then(flushed);
    await Promise.resolve();
    expect(write).toHaveBeenCalledTimes(1);
    expect(flushed).not.toHaveBeenCalled();
    finish(); await operation;
    expect(write.mock.calls.map(row => row[1])).toEqual(["original", { json: "{partial:39" }]);
    expect(flushed).toHaveBeenCalledOnce();
    expect(buffer.hasUnsaved()).toBe(false);
  });
  it("flushes before business/session work and stops it when the local write is not confirmed", async () => {
    vi.useFakeTimers();
    const write = vi.fn().mockRejectedValueOnce(new Error("disk failed")).mockResolvedValue(undefined);
    const buffer = createDraftBuffer({ write }), business = vi.fn();
    buffer.stage(address, "unsubmitted raw");
    await expect(buffer.flush().then(business)).rejects.toThrow("disk failed");
    expect(business).not.toHaveBeenCalled();
    expect(buffer.hasUnsaved()).toBe(true);
    expect(buffer.status(address)).toEqual({ status: "blocked", error: "disk failed" });
    await buffer.flush().then(business);
    expect(business).toHaveBeenCalledOnce();
    expect(write.mock.calls.map(row => row[1])).toEqual(["unsubmitted raw", "unsubmitted raw"]);
  });
  it("keeps session-addressed drafts separate and does not rely on pagehide to save", async () => {
    vi.useFakeTimers();
    const write = vi.fn(async () => {}), buffer = createDraftBuffer({ write });
    const other = { ...address, workflow_session_id: "00000000-0000-4000-8000-000000000013" };
    buffer.stage(address, "first session");
    buffer.stage(other, "second session");
    await buffer.flush();
    expect(write.mock.calls).toEqual([[address, "first session"], [other, "second session"]]);
    buffer.stage(address, "not yet saved");
    buffer.dispose();
    await vi.advanceTimersByTimeAsync(200);
    expect(write).toHaveBeenCalledTimes(2);
    expect(buffer.hasUnsaved()).toBe(true);
  });
  it("reconciles the unknown original save receipt before persisting the latest text", async () => {
    vi.useFakeTimers();
    let record: any = null, first = true, key = 100;
    const save = vi.fn(async (request: any, reconcile: boolean) => {
      if (reconcile) return record;
      record = { schema_version: 1, kind: "workflow.local-draft", address: request.address,
        format_version: 1, sequence: request.expected_sequence + 1, updated_at: "2026-10-09T00:00:00Z",
        value: request.value, deleted: request.value === null };
      if (first) { first = false; throw new Error("response unknown"); }
      return record;
    });
    const client = new GraphChatClient({ workflowId: workflow, sessionId: session, request: vi.fn(),
      keyFactory: () => `00000000-0000-4000-8000-${String(key++).padStart(12, "0")}`,
      draftPort: { read: async () => null, save } });
    const buffer = createDraftBuffer({ write: (target: any, raw: any) => client.saveDraft(target, raw) });
    buffer.stage(address, "original");
    await expect(buffer.flush()).rejects.toThrow("unknown");
    buffer.stage(address, "latest");
    await buffer.flush();
    expect(save.mock.calls.map(row => [row[0].value, row[1]])).toEqual([
      ["original", false], ["original", true], ["latest", false],
    ]);
    expect(client.draftIntents.size).toBe(0);
    expect(buffer.hasUnsaved()).toBe(false);
  });
});
