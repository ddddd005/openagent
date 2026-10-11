import { describe, expect, it, vi } from "vitest";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone } from "../domain/workflowGraph";
import { createSessionMetadata } from "./sessionMetadata";
import type { SessionMetadata, SessionMetadataSaveRequest } from "../adapters/sessionMetadataApi";

const sid = "00000000-0000-4000-8000-000000000001";
const sibling = "00000000-0000-4000-8000-000000000002";
const definition = "00000000-0000-4000-8000-000000000003";
function record(sessionId = sid, name = "", sequence = 0): SessionMetadata {
  return { schema_version: 1, kind: "workflow.session-metadata", workflow_definition_id: definition,
    workflow_session_id: sessionId, name, metadata_sequence: sequence };
}
function fixture() {
  const records = new Map<string, SessionMetadata>(), pending = new Map<string, string | null>();
  const writes: (string | null)[] = [];
  let unknown = false, failRead = false, hold: Promise<void> | null = null;
  const read = vi.fn(async (sessionId: string) => {
    if (failRead) throw new Error("Controlled read failure");
    return graphClone(records.get(sessionId) ?? record(sessionId));
  });
  const save = vi.fn(async (request: SessionMetadataSaveRequest) => {
    expect(JSON.parse(pending.get(request.session_id)!)).toEqual(request);
    if (hold) await hold;
    const accepted = record(request.session_id, request.name.trim(), request.expected_metadata_sequence + 1);
    records.set(request.session_id, accepted);
    if (unknown) throw new WorkbenchApiError("unknown", "Controlled lost response");
    return graphClone(accepted);
  });
  const readReceipt = vi.fn(async (request: SessionMetadataSaveRequest) =>
    record(request.session_id, request.name.trim(), request.expected_metadata_sequence + 1));
  const metadata = createSessionMetadata(sessionId => ({
    read, save, readReceipt, readPending: () => pending.get(sessionId) ?? null,
    writePending(value) { writes.push(value); pending.set(sessionId, value); },
  }));
  return { metadata, read, save, readReceipt, records, pending, writes,
    set unknown(value: boolean) { unknown = value; }, set failRead(value: boolean) { failRead = value; },
    set hold(value: Promise<void> | null) { hold = value; } };
}
describe("formal session names", () => {
  it("writes original evidence before saving and updates only the formal metadata", async () => {
    const f = fixture(), entry = f.metadata.forSession(sid);
    await entry.load();
    expect(await entry.save(" Formal ")).toBe(true);
    expect(f.writes).toHaveLength(2);
    const request = JSON.parse(f.writes[0]!);
    expect(request).toMatchObject({ session_id: sid, name: " Formal ", expected_metadata_sequence: 0 });
    expect(Object.keys(request).sort()).toEqual(["expected_metadata_sequence", "idempotency_key", "name", "session_id"]);
    expect(entry.record.value).toEqual(record(sid, "Formal", 1));
    expect(entry.pending.value).toBeNull();
    expect(await entry.save("Formal")).toBe(true);
    expect(f.save).toHaveBeenCalledTimes(1);
  });
  it("retains unknown evidence and reconciles the exact request without resubmitting", async () => {
    const f = fixture(), entry = f.metadata.forSession(sid);
    await entry.load(); f.unknown = true;
    expect(await entry.save("Unknown")).toBe(false);
    const original = graphClone(entry.pending.value);
    expect(entry.record.value?.name).toBe("");
    expect(entry.locked.value).toBe(true);
    expect(await entry.save("Replacement")).toBe(false);
    expect(await entry.reconcile()).toBe(true);
    expect(f.readReceipt.mock.calls[0]![0]).toEqual(original);
    expect(f.save).toHaveBeenCalledTimes(1);
    expect(entry.record.value?.name).toBe("Unknown");
    expect(entry.pending.value).toBeNull();
  });
  it("isolates late completion to the original session", async () => {
    const f = fixture(), first = f.metadata.forSession(sid), second = f.metadata.forSession(sibling);
    await Promise.all([first.load(), second.load()]);
    let release!: () => void;
    f.hold = new Promise(resolve => { release = resolve; });
    const saving = first.save("First");
    await Promise.resolve();
    expect(second.locked.value).toBe(false);
    second.observe(record(sibling, "Sibling", 7));
    release();
    expect(await saving).toBe(true);
    expect(first.record.value?.name).toBe("First");
    expect(second.record.value).toEqual(record(sibling, "Sibling", 7));
  });
  it("does not turn a confirmed save into failure when refresh fails", async () => {
    const f = fixture(), entry = f.metadata.forSession(sid);
    await entry.load(); f.failRead = true;
    expect(await entry.save("Confirmed")).toBe(true);
    expect(entry.completed.value?.name).toBe("Confirmed");
    expect(entry.record.value?.name).toBe("Confirmed");
    expect(entry.pending.value).toBeNull();
    expect(entry.error.value).toBe("");
    expect(entry.readError.value).toBe("Controlled read failure");
  });
  it("rejects stale metadata observations and leaves identical runtime projections stable", async () => {
    const f = fixture(), entry = f.metadata.forSession(sid);
    entry.observe(record(sid, "Current", 4));
    const accepted = entry.record.value;
    entry.observe(record(sid, "Current", 4));
    expect(entry.record.value).toBe(accepted);
    entry.observe(record(sid, "Old", 3));
    expect(entry.record.value?.name).toBe("Current");
    await entry.refresh();
    expect(entry.record.value?.metadata_sequence).toBe(4);
  });
  it("can save a blank name and never persists ordinary invalid editing", async () => {
    const f = fixture(), entry = f.metadata.forSession(sid);
    f.records.set(sid, record(sid, "Old", 1));
    await entry.load();
    expect(await entry.save("x".repeat(129))).toBe(false);
    expect(f.writes).toHaveLength(0);
    expect(await entry.save("")).toBe(true);
    expect(entry.record.value).toEqual(record(sid, "", 2));
  });
  it("keeps definitive rejection separate from formal metadata", async () => {
    const f = fixture(), entry = f.metadata.forSession(sid);
    await entry.load();
    f.save.mockRejectedValueOnce(new WorkbenchApiError("rejected", "Stale metadata", 409, "stale_metadata_sequence"));
    expect(await entry.save("Rejected")).toBe(false);
    expect(entry.record.value).toEqual(record());
    expect(entry.pending.value).toBeNull();
    expect(entry.error.value).toBe("Stale metadata");
  });
});
