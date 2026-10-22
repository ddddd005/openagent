import { afterEach, describe, expect, it, vi } from "vitest";
import { readPendingWorkflowEvidence } from "./requestEvidenceApi";

afterEach(() => vi.unstubAllGlobals());
const row = () => ({ owner_kind: "workflow-configuration", owner_id: crypto.randomUUID(),
  operation: "definition.update", idempotency_key: crypto.randomUUID(), request_digest: "a".repeat(64),
  resolution: "pending" });
const page = (items: unknown[], next_cursor: unknown = null) =>
  ({ schema_version: 1, kind: "workflow.request-evidence-page", items, next_cursor });
describe("workflow-only pending evidence summaries", () => {
  it("uses the bounded named read-only query and preserves exact cursor coordinates", async () => {
    const item = row(), { request_digest: _digest, resolution: _resolution, ...cursor } = item;
    const fetch = vi.fn(async (_path: string, _init: RequestInit) => new Response(JSON.stringify(page([item], cursor))));
    vi.stubGlobal("fetch", fetch);
    expect(await readPendingWorkflowEvidence({ limit: 2, cursor })).toEqual(page([item], cursor));
    expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
      operation: "request-evidence.pending", parameters: { limit: 2, cursor },
    });
    expect(fetch.mock.calls[0]?.[0]).toBe("/api/graph/queries");
  });
  it.each(["body", "foreign-owner", "digest", "duplicate", "cursor"] as const)(
    "rejects an invalid %s page without accepting partial rows", async variant => {
      const item = row();
      const items = variant === "body" ? [{ ...item, request: { secret: "synthetic" } }]
        : variant === "foreign-owner" ? [{ ...item, owner_kind: "provider-credential" }]
          : variant === "digest" ? [{ ...item, request_digest: "invalid" }]
            : variant === "duplicate" ? [item, item] : [item];
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(
        page(items, variant === "cursor" ? { owner_kind: item.owner_kind, owner_id: item.owner_id,
          operation: item.operation, idempotency_key: "different" } : null)))));
      await expect(readPendingWorkflowEvidence()).rejects.toMatchObject({ kind: "unavailable" });
    });
});
