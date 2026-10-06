import { afterEach, describe, expect, it, vi } from "vitest";
import { AGENT_BINDINGS, isPublicSession, workbenchRequest, type PublicSession } from "./workbenchApi";

export const SID = "00000000-0000-4000-8000-000000000001";
export const RUN = "00000000-0000-4000-8000-000000000002";
export const CHAIN = "00000000-0000-4000-8000-000000000003";
export function publicSession(status = "idle", actions: string[] = []): PublicSession {
  const idle = status === "idle";
  return {
    workflow_session_id: SID, revision: 1, mode: "offline", can_submit: idle,
    available_actions: actions,
    nodes: [
      { node_binding_id: AGENT_BINDINGS.A, label: "A", status, run_id: idle ? null : RUN, revision: idle ? null : 1 },
      { node_binding_id: AGENT_BINDINGS.B, label: "B", status: "idle", run_id: null, revision: null },
      { node_binding_id: "7be319b8-30bd-4674-b7bf-d1cf54a1a10a", label: "Output", status: "idle", run_id: null, revision: null },
    ],
    chains: idle ? [] : [{ chain_run_id: CHAIN, status }],
    messages: [],
  };
}
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
  it("validates target, identities, node fields and chain/message shapes", () => {
    expect(isPublicSession(publicSession(), SID)).toBe(true);
    const view = publicSession("paused", ["resume"]);
    expect(isPublicSession(view, SID)).toBe(true);
    expect(isPublicSession(view, RUN)).toBe(false);
    expect(isPublicSession({ ...view, nodes: [null] }, SID)).toBe(false);
    view.nodes[0].run_id = "private";
    expect(isPublicSession(view, SID)).toBe(false);
    expect(isPublicSession({ ...publicSession(), chains: [null] }, SID)).toBe(false);
    expect(isPublicSession({ ...publicSession(), messages: [{ role: "tool" }] }, SID)).toBe(false);
  });
});
