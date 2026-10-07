import { afterEach, describe, expect, it, vi } from "vitest";
import { workbenchRequest } from "./workbenchApi";
afterEach(() => vi.unstubAllGlobals());
describe("same-origin workbench adapter", () => {
  it("classifies mutation connection loss as unknown and GET failure as unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("PRIVATE body")));
    await expect(workbenchRequest("/api/health")).rejects.toMatchObject({ kind: "unavailable" });
    await expect(workbenchRequest("/api/sessions", { body: {} })).rejects.toMatchObject({ kind: "unknown" });
  });
  it("keeps public reason codes and HTTP status, never raw private text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: "conflict", reason_code: "stale_revision", message: "PRIVATE trace" },
    }), { status: 409 })));
    await expect(workbenchRequest("/api/command", { body: {} })).rejects.toMatchObject({
      kind: "rejected", status: 409, code: "stale_revision",
    });
    await expect(workbenchRequest("/api/command", { body: {} })).rejects.not.toThrow("PRIVATE");
  });
  it("treats malformed successful response and mutation 500 as unknown", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response("not-json", { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: "internal_error" } }), { status: 500 }));
    vi.stubGlobal("fetch", fetcher);
    await expect(workbenchRequest("/api/run", { body: {} })).rejects.toMatchObject({ kind: "unknown" });
    await expect(workbenchRequest("/api/run", { body: {} })).rejects.toMatchObject({ kind: "unknown" });
  });
  it("classifies read-only POST failures as unavailable without changing its HTTP method", async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(new Response("not-json", { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: "internal_error" } }), { status: 500 }));
    vi.stubGlobal("fetch", fetcher);
    for (let index = 0; index < 3; index++)
      await expect(workbenchRequest("/api/context/read", { body: {}, readOnly: true }))
        .rejects.toMatchObject({ kind: "unavailable" });
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: "POST", body: "{}" });
  });
});
