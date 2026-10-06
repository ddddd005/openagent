import { describe, expect, it, vi } from "vitest";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { UserConfig } from "vite";
import configuration from "../../vite.config";

const proxy = (configuration as UserConfig).server!.proxy!["/api"];
if (typeof proxy !== "object" || !proxy.bypass) throw new Error("Missing development API origin guard");
const guard = proxy.bypass;
const request = (origin?: string) => ({
  method: "POST", headers: { host: "127.0.0.1:5179", ...(origin ? { origin } : {}) }, destroy: vi.fn(),
});

describe("development API origin guard", () => {
  it("preserves valid same-origin API requests", () => {
    const incoming = request("http://127.0.0.1:5179");
    expect(guard(incoming as unknown as IncomingMessage, undefined, proxy)).toBeUndefined();
    expect(incoming.destroy).not.toHaveBeenCalled();
  });
  it("returns an explicit HTTP rejection for foreign origins", () => {
    const response = { statusCode: 200, setHeader: vi.fn(), end: vi.fn() };
    expect(guard(request("http://foreign.invalid") as unknown as IncomingMessage,
      response as unknown as ServerResponse, proxy)).toBe(false);
    expect(response.statusCode).toBe(403);
    expect(response.end).toHaveBeenCalledWith(JSON.stringify({ error: { code: "invalid_origin" } }));
  });
  it("closes an invalid request even when an HTTP response object is unavailable", () => {
    const incoming = request();
    expect(guard(incoming as unknown as IncomingMessage, undefined, proxy)).toBe(false);
    expect(incoming.destroy).toHaveBeenCalledTimes(1);
  });
});
