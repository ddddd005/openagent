import { afterEach, describe, expect, it, vi } from "vitest";
import { isContentMetadataSaveRequest, readContentMetadata, readContentMetadataReceipt,
  saveContentMetadata, validContentDisplayName, type ContentMetadataSaveRequest } from "./contentMetadataApi";
import { newPromptResource, promptIdentity } from "../domain/workflowPromptResources";
import { graphReceiptReadResponse } from "../testUtils/graphApplicationServer";

afterEach(() => vi.unstubAllGlobals());
function request(): ContentMetadataSaveRequest {
  return { reference: promptIdentity(newPromptResource()), display_name: "Formal name",
    expected_metadata_sequence: 3, idempotency_key: crypto.randomUUID() };
}
function resultFor(value: ContentMetadataSaveRequest) {
  return { reference: value.reference, display_name: value.display_name,
    metadata_sequence: value.expected_metadata_sequence + 1 };
}
function envelope(value: ContentMetadataSaveRequest, result = resultFor(value)) {
  return { schema_version: 1, kind: "workflow.application-command", result,
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "content.metadata.update",
      operation_scope: "management", idempotency_key: value.idempotency_key,
      authority: "service_receipt", request_sha256: "a".repeat(64), target: {}, accepted: result } };
}
describe("prompt display metadata adapter", () => {
  it("uses independent name query, command and receipt paths with exact identity", async () => {
    const original = request();
    const fetcher = vi.fn(async (path: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body));
      if (path === "/api/graph/queries") {
        expect(body).toEqual({ operation: "content.metadata.read", parameters: { reference: original.reference } });
        return new Response(JSON.stringify(resultFor(original)));
      }
      expect(body).toEqual({ operation: "content.metadata.update", parameters: original });
      return path === "/api/graph/receipts/read"
        ? graphReceiptReadResponse(body.operation, body.parameters, resultFor(original))
        : new Response(JSON.stringify(envelope(original)));
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await readContentMetadata(original.reference)).toEqual(resultFor(original));
    expect(await saveContentMetadata(original)).toEqual(resultFor(original));
    expect(await readContentMetadataReceipt(original)).toEqual(resultFor(original));
    expect(fetcher.mock.calls.map(([path]) => path))
      .toEqual(["/api/graph/queries", "/api/graph/commands", "/api/graph/receipts/read"]);
  });
  it.each(["scope", "uuid", "name", "sequence", "extra"])("keeps a wrong %s acceptance unknown", async field => {
    const original = request(), result = resultFor(original);
    const wrong = { ...result };
    if (field === "scope") wrong.reference = { ...result.reference, scope: "project" };
    if (field === "uuid") wrong.reference = { ...result.reference, resource_id: crypto.randomUUID() };
    if (field === "name") wrong.display_name = "Different";
    if (field === "sequence") wrong.metadata_sequence++;
    const value = field === "extra" ? { ...wrong, extra: true } : wrong;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(envelope(original, value)))));
    await expect(saveContentMetadata(original)).rejects.toMatchObject({ kind: "unknown" });
  });
  it("rejects an invalid read shape or another scope instead of inventing a label", async () => {
    const original = request();
    for (const wrong of [{ ...resultFor(original), reference: { ...original.reference, scope: "project" } },
      { ...resultFor(original), extra: true }, { ...resultFor(original), metadata_sequence: -1 }]) {
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(wrong))));
      await expect(readContentMetadata(original.reference)).rejects.toMatchObject({ kind: "unavailable" });
    }
  });
  it("counts Unicode code points and rejects malformed or lossy requests before dispatch", async () => {
    const original = request(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect(validContentDisplayName("\u{1f680}".repeat(128))).toBe(true);
    expect(validContentDisplayName("\u{1f680}".repeat(129))).toBe(false);
    expect(validContentDisplayName("\ud800")).toBe(false);
    expect(validContentDisplayName("\udc00")).toBe(false);
    for (const wrong of [{ ...original, display_name: "x".repeat(129) },
      { ...original, display_name: " padded " }, { ...original, display_name: "\ud800" },
      { ...original, expected_metadata_sequence: true }, { ...original, expected_metadata_sequence: Number.MAX_SAFE_INTEGER },
      { ...original, idempotency_key: original.idempotency_key.toUpperCase() }, { ...original, extra: true },
      { ...original, [Symbol("lost")]: true }]) {
      expect(isContentMetadataSaveRequest(wrong)).toBe(false);
      await expect(saveContentMetadata(wrong as ContentMetadataSaveRequest)).rejects.toMatchObject({ kind: "rejected" });
    }
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("reports actual metadata CAS rejection", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ error: { code: "stale_metadata_sequence", message: "Changed name" } }), { status: 409 })));
    await expect(saveContentMetadata(request())).rejects.toMatchObject({ kind: "rejected", code: "stale_metadata_sequence" });
  });
});
