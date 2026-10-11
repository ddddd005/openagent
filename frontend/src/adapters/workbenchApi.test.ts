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
  it.each([
    ["model_profile_unavailable", "全局模型配置不存在或不可读取"],
    ["model_profile_type_mismatch", "重新选择模型配置"],
    ["model_source_invalid", "只能引用全局模型配置"],
    ["model_source_protocol_mismatch", "选择同一协议的配置"],
    ["model_capacity_unknown", "手动填写"],
    ["model_capacity_invalid", "窗口、最大输出、输出预留和摘要上限"],
    ["model_output_reserve_unknown", "模型最大输出未配置"],
    ["model_provider_unavailable", "供应商配置不存在或不可读取"],
    ["model_provider_disabled", "供应商已停用"],
    ["model_provider_invalid", "检查名称、协议、服务地址和已保存 Key"],
    ["model_credential_unavailable", "在模型供应商中填写并保存"],
    ["credential_protection_unavailable", "无法保存或使用 Key"],
    ["model_credential_broker_unavailable", "检查后端初始化"],
    ["model_provider_type_mismatch", "重新选择"],
  ])("presents an actionable current-configuration error for %s", async (code, reason) => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { reason_code: code, message: "PRIVATE configuration" },
    }), { status: 400 }));
    vi.stubGlobal("fetch", fetcher);
    let failure;
    try { await workbenchRequest("/api/graph/commands", { body: {} }); } catch (error) { failure = error; }
    expect(failure).toMatchObject({ kind: "rejected", status: 400, code });
    expect(String(failure)).toContain(reason);
    expect(String(failure)).not.toContain("PRIVATE");
    expect(fetcher).toHaveBeenCalledTimes(1);
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
  it("preserves controlled failure steps and actionable guidance without changing unknown protection", async () => {
    const correlation = crypto.randomUUID();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { reason_code: "model_not_dispatched", message: "PRIVATE exception", correlation_id: correlation,
        diagnostics: [{ code: "model_not_dispatched", message: "Model request blocked",
          cause_code: "model_credential_unavailable", step: "credential_authorization",
          recovery: "save_provider_key", correlation_id: correlation, secret: "PRIVATE key" }] },
    }), { status: 500 })));
    let failure;
    try { await workbenchRequest("/api/graph/commands", { body: {} }); } catch (error) { failure = error; }
    expect(failure).toMatchObject({ kind: "unknown", correlationId: correlation, diagnostics: [{
      reason_code: "model_not_dispatched", cause_code: "model_credential_unavailable",
      step: "credential_authorization", recovery: "save_provider_key", correlation_id: correlation,
    }] });
    expect(String(failure)).toContain("在模型供应商中保存有效 Key");
    expect(String(failure)).toContain(correlation);
    expect(JSON.stringify(failure)).not.toContain("PRIVATE");
    expect(JSON.stringify(failure)).not.toContain("secret");
  });
  it("does not present unchecked recovery or correlation strings", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { reason_code: "internal_error", correlation_id: "PRIVATE key",
        diagnostics: [{ code: "internal_error", recovery: "PRIVATE key", step: "PRIVATE key", cause_code: "PRIVATE key" }] },
    }), { status: 500 })));
    await expect(workbenchRequest("/api/graph/commands", { body: {} })).rejects.not.toThrow("PRIVATE");
  });
});
