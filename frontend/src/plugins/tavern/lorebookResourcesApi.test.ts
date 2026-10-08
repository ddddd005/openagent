import { afterEach, describe, expect, it, vi } from "vitest";
import { graphClone } from "../../domain/workflowGraph";
import { newLorebookEntry } from "../../domain/lorebook";
import { lorebookIdentity, newLorebookResource } from "./lorebookResources";
import { listCurrentLorebooks, readCurrentLorebook, readCurrentLorebookReceipt, saveCurrentLorebook,
  type LorebookSaveRequest } from "./lorebookResourcesApi";

afterEach(() => vi.unstubAllGlobals());
const request = (): LorebookSaveRequest => ({
  record: newLorebookResource(), expected_sequence: 0, idempotency_key: crypto.randomUUID(),
});
function envelope(body: LorebookSaveRequest, overrides = {}) {
  const result = { reference: lorebookIdentity(body.record), update_sequence: body.record.update_sequence, deleted: false, ...overrides };
  const receipt = { schema_version: 1, kind: "workflow.application-receipt", operation: "resource.save",
    operation_scope: "management", idempotency_key: body.idempotency_key,
    authority: "service_receipt", request_sha256: "a".repeat(64), target: {}, accepted: result };
  return { schema_version: 1, kind: "workflow.application-command", result, receipt };
}
describe("lorebook named resource transport", () => {
  it("queries only the tavern resource type and distinguishes full-identity scopes", async () => {
    const workspace = newLorebookResource(), project = { ...graphClone(workspace), scope: "project:one" };
    const fetcher = vi.fn(async (_url: string, options: RequestInit) => {
      const body = JSON.parse(String(options.body));
      return new Response(JSON.stringify(body.operation === "resource.list" ? [workspace, project] : project));
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await listCurrentLorebooks()).toEqual([workspace, project]);
    expect(await readCurrentLorebook(lorebookIdentity(project))).toEqual(project);
    expect(JSON.parse(String(fetcher.mock.calls[0]![1].body))).toEqual({
      operation: "resource.list", parameters: { type_id: "workflow.tavern.lorebook" } });
  });
  it("rejects cross-scope reads, unsupported resources and invalid identities", async () => {
    const body = request();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...body.record, scope: "project" }))));
    await expect(readCurrentLorebook(lorebookIdentity(body.record))).rejects.toMatchObject({ kind: "unavailable" });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([{ ...body.record, data_schema_version: 2 }]))));
    await expect(listCurrentLorebooks()).rejects.toMatchObject({ kind: "unavailable" });
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(readCurrentLorebook({ ...lorebookIdentity(body.record), scope: "session" }))
      .rejects.toMatchObject({ kind: "rejected" });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("saves member content and uses a receipt-only read with exactly the original body/key", async () => {
    const body = request();
    body.record.value.entries = [newLorebookEntry()];
    const fetcher = vi.fn(async (url: string, options: RequestInit) => {
      expect(JSON.parse(String(options.body))).toEqual({ operation: "resource.save", parameters: body });
      const accepted = envelope(body);
      return new Response(JSON.stringify(url.includes("/receipts/") ? {
        schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
        reason_code: "receipt_matched", result: accepted.result, receipt: accepted.receipt,
      } : accepted));
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await saveCurrentLorebook(body)).toEqual(envelope(body).result);
    expect(await readCurrentLorebookReceipt(body)).toEqual(envelope(body).result);
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(["/api/graph/commands", "/api/graph/receipts/read"]);
  });
  it.each(["scope", "sequence", "deleted", "extra"])("retains an unknown mismatched %s receipt", async problem => {
    const body = request(), overrides: Record<string, unknown> = {};
    if (problem === "scope") overrides.reference = { ...lorebookIdentity(body.record), scope: "project" };
    if (problem === "sequence") overrides.update_sequence = 2;
    if (problem === "deleted") overrides.deleted = true;
    if (problem === "extra") overrides.extra = true;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(envelope(body, overrides)))));
    await expect(saveCurrentLorebook(body)).rejects.toMatchObject({ kind: "unknown" });
  });
  it("rejects malformed original requests and preserves explicit CAS rejection", async () => {
    const body = request(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(saveCurrentLorebook({ ...body, expected_sequence: 1 })).rejects.toMatchObject({ kind: "rejected" });
    await expect(saveCurrentLorebook({ ...body, idempotency_key: "invalid" })).rejects.toMatchObject({ kind: "rejected" });
    await expect(saveCurrentLorebook({ ...body, [Symbol("hidden")]: true })).rejects.toMatchObject({ kind: "rejected" });
    expect(fetcher).not.toHaveBeenCalled();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "stale_revision" } }), { status: 409 })));
    await expect(saveCurrentLorebook(body)).rejects.toMatchObject({ kind: "rejected", code: "stale_revision" });
  });
});
