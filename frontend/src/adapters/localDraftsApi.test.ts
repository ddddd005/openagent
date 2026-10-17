import { afterEach, describe, expect, it, vi } from "vitest";
import { createLocalDraftChannel, isLocalDraftAddress, isLocalDraftRecord, readLocalDraft, readLocalDraftReceipt, saveLocalDraft, workbenchDraftAddress } from "./localDraftsApi";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { WorkbenchApiError } from "./workbenchApi";
import { graphClone } from "../domain/workflowGraph";

afterEach(() => vi.unstubAllGlobals());
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(accept => { resolve = accept; });
  return { promise, resolve };
}
describe("authoritative local draft transport", () => {
  it("serializes CAS writes, keeps tombstones and never retries a transport-unknown mutation", async () => {
    const memory = memoryLocalDraftStorage();
    const originalSave = memory.ports.save;
    let lose = true;
    memory.ports.save = vi.fn(async request => {
      const record = await originalSave(request);
      if (lose) { lose = false; throw new WorkbenchApiError("unknown", "receipt lost"); }
      return record;
    });
    const channel = createLocalDraftChannel(workbenchDraftAddress, memory.ports);
    await expect(channel.write({ raw: "{unfinished" })).rejects.toThrow("receipt lost");
    const frozen = channel.pending();
    expect(frozen).not.toBeNull();
    expect((await channel.write({ raw: "{unfinished" })).sequence).toBe(1);
    expect(memory.ports.save).toHaveBeenCalledTimes(1);
    expect(memory.ports.receipt).toHaveBeenCalledWith(frozen);
    expect((await channel.write(null)).deleted).toBe(true);
    expect((await channel.write({ raw: "new edit" })).sequence).toBe(3);
  });
  it("rejects stale CAS without overwriting a newer record", async () => {
    const memory = memoryLocalDraftStorage();
    const first = createLocalDraftChannel(workbenchDraftAddress, memory.ports);
    const second = createLocalDraftChannel(workbenchDraftAddress, memory.ports);
    await first.read(); await second.read();
    await first.write({ raw: "first window" });
    await expect(second.write({ raw: "second window" })).rejects.toMatchObject({ code: "stale_draft_sequence" });
    expect((await memory.ports.read(workbenchDraftAddress))?.value).toEqual({ raw: "first window" });
  });
  it("retains an idempotency-conflicted original rather than minting a new mutation key", async () => {
    const memory = memoryLocalDraftStorage();
    vi.mocked(memory.ports.save).mockRejectedValueOnce(
      new WorkbenchApiError("rejected", "key conflict", 409, "idempotency_conflict"));
    const channel = createLocalDraftChannel(workbenchDraftAddress, memory.ports);
    await expect(channel.write({ raw: "original" })).rejects.toMatchObject({ code: "idempotency_conflict" });
    const frozen = channel.pending();
    expect(frozen).not.toBeNull();
    await expect(channel.write({ raw: "next edit" })).rejects.toMatchObject({ kind: "unknown" });
    expect(memory.ports.save).toHaveBeenCalledOnce();
    expect(memory.ports.receipt).toHaveBeenCalledWith(frozen);
  });
  it("shares initial reads and queues fresh observations after a durable write", async () => {
    const memory = memoryLocalDraftStorage(), initial = deferred<null>();
    const originalRead = memory.ports.read;
    vi.mocked(memory.ports.read).mockImplementationOnce(() => initial.promise);
    const channel = createLocalDraftChannel(workbenchDraftAddress, memory.ports);
    const first = channel.read(), second = channel.read();
    expect(memory.ports.read).toHaveBeenCalledOnce();
    initial.resolve(null); await first; await second;
    const gate = deferred<void>(), dispatched = deferred<void>(), originalSave = memory.ports.save;
    vi.mocked(memory.ports.save).mockImplementationOnce(async request => {
      dispatched.resolve(); await gate.promise; return originalSave(request);
    });
    const writing = channel.write({ raw: "durable after slow write" });
    await dispatched.promise;
    const refreshing = channel.read(true);
    expect(memory.ports.read).toHaveBeenCalledOnce();
    gate.resolve(); expect((await writing).sequence).toBe(1);
    expect((await refreshing)?.value).toEqual({ raw: "durable after slow write" });
    expect(memory.ports.read).toHaveBeenCalledTimes(2);
    expect(await originalRead(workbenchDraftAddress)).toEqual(channel.record());
  });
  it("rejects invalid draft coordinates, including ownerless sessions and unsafe keys", () => {
    expect(isLocalDraftAddress(workbenchDraftAddress)).toBe(true);
    for (const key of [" ", " leading", "trailing ", "embedded\u0000control", "line\nbreak"])
      expect(isLocalDraftAddress({ ...workbenchDraftAddress, key })).toBe(false);
    expect(isLocalDraftAddress({ ...workbenchDraftAddress, workflow_session_id: crypto.randomUUID() })).toBe(false);
  });
  it("validates the native draft receipt and exact address, sequence and raw value", async () => {
    const memory = memoryLocalDraftStorage();
    const request = { address: workbenchDraftAddress, format_version: 1, value: { raw: "not JSON text {" },
      expected_sequence: 0, idempotency_key: crypto.randomUUID() };
    const record = await memory.ports.save(request);
    expect(isLocalDraftRecord(record)).toBe(true);
    const envelope = { schema_version: 1, kind: "workflow.application-command", result: record,
      receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "draft.save",
        operation_scope: "management", idempotency_key: request.idempotency_key,
        request_sha256: "a".repeat(64), authority: "service_receipt", target: {},
        accepted: { draft_address: request.address, draft_sequence: 1, draft_format_version: 1, deleted: false } } };
    const fetcher = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify({ schema_version: 1,
      kind: "workflow.application-receipt-read", outcome: "matched", reason_code: "receipt_matched",
      receipt: envelope.receipt, result: envelope.result })));
    vi.stubGlobal("fetch", fetcher);
    await expect(saveLocalDraft(request)).rejects.toMatchObject({ code: "local_draft_write_retired" });
    expect(fetcher).not.toHaveBeenCalled();
    expect(await readLocalDraftReceipt(request)).toEqual(record);
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({ operation: "draft.save", parameters: request });
    envelope.result.value = { raw: "foreign value" };
    await expect(readLocalDraftReceipt(request)).rejects.toMatchObject({ kind: "unknown" });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...graphClone(record),
      address: { ...record.address, key: "other" } }))));
    await expect(readLocalDraft(request.address)).rejects.toMatchObject({ kind: "unavailable" });
  });
});
