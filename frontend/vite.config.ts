import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    host: "127.0.0.1", port: 5178, strictPort: false,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
        headers: { Origin: "http://127.0.0.1:8765" },
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
