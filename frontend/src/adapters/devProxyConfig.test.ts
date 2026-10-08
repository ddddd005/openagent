import { describe, expect, it, vi } from "vitest";
import type { ProxyOptions } from "vite";
import config, { devBackendTarget } from "../../vite.config";

describe("current-source development proxy", () => {
  it("keeps the default backend on loopback", () => {
    expect(devBackendTarget()).toBe("http://127.0.0.1:8765");
  });

  it.each(["1024", "8765", "64032", "65535"])("uses the explicit backend port %s", port => {
    expect(devBackendTarget(port)).toBe(`http://127.0.0.1:${port}`);
  });

  it.each(["", " ", "0", "1023", "65536", "08765", "+8765", "8e3", "8765.5",
    "8765\n", "http://example.test:8765", "NaN"])("rejects invalid port %j", port => {
    expect(() => devBackendTarget(port)).toThrow("OPENAGENT_API_PORT");
  });

  it("uses the same target for API forwarding and its backend Origin", () => {
    const proxy = config.server!.proxy!["/api"] as ProxyOptions;
    expect(proxy.target).toBe(devBackendTarget(process.env.OPENAGENT_API_PORT));
    expect(proxy.headers).toEqual({ Origin: proxy.target });
  });

  it.each([
    { method: "GET", host: "127.0.0.1:5178", origin: undefined, allowed: true },
    { method: "GET", host: "localhost:64033", origin: "http://localhost:64033", allowed: true },
    { method: "POST", host: "127.0.0.1:64033", origin: "http://127.0.0.1:64033", allowed: true },
    { method: "POST", host: "127.0.0.1:5178", origin: undefined, allowed: false },
    { method: "POST", host: "127.0.0.1:5178", origin: "https://example.test", allowed: false },
    { method: "GET", host: "example.test:5178", origin: undefined, allowed: false },
    { method: "GET", host: "localhost:5178", origin: "http://localhost:8765", allowed: false },
  ])("preserves the browser request boundary for $method $host $origin", row => {
    const proxy = config.server!.proxy!["/api"] as ProxyOptions;
    const request = { method: row.method, headers: { host: row.host, origin: row.origin } };
    const response = { statusCode: 200, setHeader: vi.fn(), end: vi.fn() };
    proxy.bypass!(request as never, response as never, proxy);
    if (row.allowed) {
      expect(response.end).not.toHaveBeenCalled();
    } else {
      expect(response.statusCode).toBe(403);
      expect(response.end).toHaveBeenCalledWith(JSON.stringify({ error: { code: "invalid_origin" } }));
    }
  });
});
