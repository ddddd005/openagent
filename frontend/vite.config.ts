import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

export function devBackendTarget(configuredPort?: string): string {
  const value = configuredPort ?? "8765";
  const port = Number(value);
  if (!Number.isInteger(port) || String(port) !== value || port < 1024 || port > 65535)
    throw new Error("OPENAGENT_API_PORT must be a decimal loopback port between 1024 and 65535.");
  return `http://127.0.0.1:${port}`;
}

const backendTarget = devBackendTarget(process.env.OPENAGENT_API_PORT);

export default defineConfig({
  plugins: [vue()],
  server: {
    host: "127.0.0.1", port: 5178, strictPort: false,
    proxy: {
      "/api": {
        target: backendTarget,
        changeOrigin: true,
        headers: { Origin: backendTarget },
        bypass(request, response) {
          const host = request.headers.host ?? "";
          const origin = request.headers.origin;
          const sameOrigin = /^((127\.0\.0\.1)|(localhost)):[0-9]{1,5}$/.test(host)
            && origin === `http://${host}`;
          if (!/^((127\.0\.0\.1)|(localhost)):[0-9]{1,5}$/.test(host)
            || (request.method === "POST" ? !sameOrigin : origin !== undefined && !sameOrigin)) {
            if (response) {
              response.statusCode = 403;
              response.setHeader("Content-Type", "application/json");
              response.end(JSON.stringify({ error: { code: "invalid_origin" } }));
            } else request.destroy();
            return false;
          }
        },
      },
    },
  },
  test: { include: ["src/**/*.test.ts"], environment: "node" },
});
