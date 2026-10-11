import { afterEach, describe, expect, it, vi } from "vitest";
import { isSessionMetadataSaveRequest, readSessionMetadata, readSessionMetadataReceipt,
  saveSessionMetadata, validSessionName, type SessionMetadataSaveRequest } from "./sessionMetadataApi";

const sid = "00000000-0000-4000-8000-000000000001";
const definition = "00000000-0000-4000-8000-000000000002";
function request(): SessionMetadataSaveRequest {
  return { session_id: sid, name: " Saved ", expected_metadata_sequence: 3,
    idempotency_key: "00000000-0000-4000-8000-000000000003" };
}
function result() {
  return { schema_version: 1, kind: "workflow.session-metadata",
    workflow_definition_id: definition, workflow_session_id: sid, name: "Saved", metadata_sequence: 4 };
}
function envelope() {
  const value = result();
  return { schema_version: 1, kind: "workflow.application-command", result: value,
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "session.metadata.update",
      operation_scope: "management", idempotency_key: request().idempotency_key,
      request_sha256: "a".repeat(64), authority: "service_receipt", target: { session_id: sid },
      accepted: { workflow_definition_id: definition, workflow_session_id: sid, name: "Saved", metadata_sequence: 4 } } };
}
afterEach(() => vi.unstubAllGlobals());
describe("session metadata transport", () => {
  it("submits the exact formal name command and validates its accepted metadata", async () => {
    const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify(envelope())));
    vi.stubGlobal("fetch", fetch);
    expect(await saveSessionMetadata(request())).toEqual(result());
    expect(fetch.mock.calls[0]![0]).toBe("/api/graph/commands");
    expect(JSON.parse(fetch.mock.calls[0]![1]!.body as string)).toEqual({
      operation: "session.metadata.update", parameters: request() });
  });
  it("reads an exact target without a mutation", async () => {
    const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify(result())));
    vi.stubGlobal("fetch", fetch);
    expect(await readSessionMetadata(sid)).toEqual(result());
    expect(JSON.parse(fetch.mock.calls[0]![1]!.body as string)).toEqual({
      operation: "session.metadata.read", parameters: { session_id: sid } });
  });
  it.each(["target", "operation", "accepted", "result"] as const)("retains unknown evidence on mismatched %s", async field => {
    const value = envelope();
    if (field === "target") value.receipt.target.session_id = definition;
    if (field === "operation") value.receipt.operation = "session.archive";
    if (field === "accepted") value.receipt.accepted.metadata_sequence = 5;
    if (field === "result") value.result.name = "Other";
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value))));
    await expect(saveSessionMetadata(request())).rejects.toMatchObject({ kind: "unknown" });
  });
  it("reconciles only through receipt reading and does not resubmit", async () => {
    const accepted = envelope();
    const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
      reason_code: "receipt_matched", receipt: accepted.receipt, result: accepted.result })));
    vi.stubGlobal("fetch", fetch);
    expect(await readSessionMetadataReceipt(request())).toEqual(result());
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]![0]).toBe("/api/graph/receipts/read");
    expect(JSON.parse(fetch.mock.calls[0]![1]!.body as string).parameters).toEqual(request());
  });
  it("keeps an unresolved original request unresolved", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-receipt-read", outcome: "unresolved",
      reason_code: "receipt_missing" }))));
    await expect(readSessionMetadataReceipt(request())).rejects.toMatchObject({ kind: "unknown", code: "receipt_missing" });
  });
  it("validates blank, Unicode, invalid controls and independent sequence limits", () => {
    expect(validSessionName("")).toBe(true);
    expect(validSessionName("\u{1f642}".repeat(128))).toBe(true);
    expect(validSessionName("\u{1f642}".repeat(129))).toBe(false);
    expect(validSessionName("\ud800")).toBe(false);
    expect(validSessionName("bad\n")).toBe(false);
    expect(isSessionMetadataSaveRequest({ ...request(), expected_metadata_sequence: true })).toBe(false);
    expect(isSessionMetadataSaveRequest({ ...request(), expected_metadata_sequence: Number.MAX_SAFE_INTEGER })).toBe(false);
  });
});
