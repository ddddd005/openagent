import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const evidence = process.argv[2];
assert.ok(evidence, "An isolated evidence directory is required");
const { chromium } = createRequire(import.meta.url)("playwright");
const { createServer } = await import(pathToFileURL(path.join(root, "frontend/node_modules/vite/dist/node/index.js")));
const { default: vue } = await import(pathToFileURL(path.join(root, "frontend/node_modules/@vitejs/plugin-vue/dist/index.mjs")));

// Load the actual node component with delayed catalog data and no product APIs.
const entry = `
import { createApp, defineComponent, h, nextTick, ref } from "vue";
import { VueFlow, useVueFlow } from "@vue-flow/core";
import "@vue-flow/core/dist/style.css";
import GraphCanvasNode from "/src/components/GraphCanvasNode.vue";
const port = (port_id, data_type, data_schema_version = 2) =>
  ({ port_id, data_type, data_schema_version, multiple: false, required: true });
const agent = {
  component_id: "agents.execute", component_version: "10", display_name: "Agent execution",
  category: "Test", config_schema: {}, default_config: {}, executable: true, is_output: false,
  inputs: [port("prompt", "PROMPT", 6), port("model", "MODEL_BINDING", 5),
    port("compaction_policy", "CONTEXT_COMPACTION_POLICY")],
  outputs: [port("result", "TEXT"), port("receipts", "AGENT_RECEIPTS")],
};
const writeback = {
  ...agent, component_id: "context.merge", component_version: "6", display_name: "Writeback",
  inputs: [port("view", "CONTEXT_VIEW", 5), port("receipts", "AGENT_RECEIPTS")],
  outputs: [port("output", "CONTEXT_VIEW", 5), port("commit", "CONTEXT_COMMIT", 1)],
};
const source = { node_binding_id: "agent", component_id: "agents.execute", component_version: "10",
  title: "Agent execution", config: {}, position: { x: 50, y: 100 } };
const target = { node_binding_id: "writeback", component_id: "context.merge", component_version: "6",
  title: "Writeback", config: {}, position: { x: 550, y: 50 } };
createApp(defineComponent({
  setup() {
    const flow = useVueFlow({ id: "port-fixture" });
    const loaded = ref(false), sourceDefinition = ref(agent), targetDefinition = ref(writeback);
    const edges = ref([]);
    const data = () => ({
      node: source, definition: loaded.value ? sourceDefinition.value : undefined,
      displayTitle: "Agent execution", status: "idle",
      inputs: ["prompt", "model", "compaction_policy"], outputs: ["receipts"], error: false,
    });
    window.fixture = {
      load: async () => { loaded.value = true; await nextTick(); },
      reorder: async () => {
        sourceDefinition.value = { ...sourceDefinition.value,
          outputs: [...sourceDefinition.value.outputs].reverse() };
        targetDefinition.value = { ...targetDefinition.value,
          inputs: [...targetDefinition.value.inputs].reverse() };
        edges.value = [];
        await nextTick();
      },
      flow, starts: [], connections: [],
    };
    return () => h(VueFlow, {
      id: "port-fixture", nodes: [
        { id: "agent", type: "graph", position: source.position, data: data() },
        { id: "writeback", type: "graph", position: target.position,
          data: { node: target, definition: targetDefinition.value, displayTitle: "Writeback",
            status: "idle", inputs: [], outputs: [], error: false } },
      ], edges: edges.value, defaultViewport: { x: 40, y: 30, zoom: 1 },
      connectionRadius: 28, autoPanOnConnect: false,
      onConnectStart: event => window.fixture.starts.push({
        handleId: event.handleId, nodeId: event.nodeId,
        clicked: event.event.target.getAttribute("data-handleid"),
      }),
      onConnect: event => {
        window.fixture.connections.push(event);
        edges.value.push({ id: String(window.fixture.connections.length), ...event, type: "smoothstep" });
      },
    }, { "node-graph": props => h(GraphCanvasNode, props) });
  },
})).mount("#app");
`;

const server = await createServer({
  root: path.join(root, "frontend"), configFile: false, logLevel: "error",
  plugins: [vue(), {
    name: "isolated-graph-port-fixture",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url === "/") {
          res.setHeader("Content-Type", "text/html");
          res.end('<html><head><style>body{margin:0}#app{width:1440px;height:900px}</style></head><body><div id="app"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        } else if (req.url === "/fixture-entry.js") {
          server.transformRequest("/virtual-fixture.js").then(result => {
            res.setHeader("Content-Type", "text/javascript");
            res.end(result.code);
          }).catch(next);
        } else next();
      });
    },
    resolveId(id) { if (id === "/virtual-fixture.js") return "\0graph-port-fixture"; },
    load(id) { if (id === "\0graph-port-fixture") return entry; },
  }],
  server: { host: "127.0.0.1", port: 0, strictPort: false },
});

let browser;
const report = { errors: [], unexpected_requests: [], scenarios: [] };
try {
  await server.listen();
  const port = server.httpServer.address().port;
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  for (const zoom of [1, 1.75]) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("pageerror", error => report.errors.push(String(error)));
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.hostname !== "127.0.0.1" || url.port !== String(port) || url.pathname.startsWith("/api/")) {
        report.unexpected_requests.push(url.pathname);
        await route.abort();
      } else await route.continue();
    });
    await page.goto(`http://127.0.0.1:${port}/`);
    await page.waitForFunction(() => window.fixture?.flow.findNode("agent")?.dimensions.height > 0);
    await page.evaluate(zoom => window.fixture.flow.setViewport({ x: 40, y: 30, zoom }), zoom);
    const settle = () => page.evaluate(() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(resolve)))));
    const measure = () => page.evaluate(() => {
      const flow = window.fixture.flow;
      return Object.fromEntries(["agent", "writeback"].map(id => {
        const node = flow.findNode(id);
        const element = document.querySelector('.vue-flow__node[data-id="' + id + '"]');
        const bounds = element.getBoundingClientRect();
        const handles = [...element.querySelectorAll(".source, .target")].map(handle => {
          const box = handle.getBoundingClientRect(), type = handle.classList.contains("source") ? "source" : "target";
          const cached = node.handleBounds[type].find(row => row.id === handle.getAttribute("data-handleid"));
          return { id: handle.getAttribute("data-handleid"), type, domY: (box.top + box.bottom) / 2,
            cachedY: bounds.top + (cached.y + cached.height / 2) * flow.viewport.value.zoom };
        });
        return [id, { width: bounds.width, height: bounds.height, handles }];
      }));
    });
    await settle();
    const before = await measure();
    await page.evaluate(() => window.fixture.load());
    await page.locator('.vue-flow__node[data-id="agent"] .source[data-handleid="result"]').waitFor();
    for (const kind of ["catalog-loaded", "ports-reordered"]) {
      if (kind === "ports-reordered") await page.evaluate(() => window.fixture.reorder());
      await settle();
      const layout = await measure();
      for (const id of ["agent", "writeback"]) {
        assert.equal(layout[id].width, before[id].width);
        assert.equal(layout[id].height, before[id].height);
        for (const handle of layout[id].handles) {
          assert.ok(Math.abs(handle.domY - handle.cachedY) <= 1,
            `${kind} at zoom ${zoom}: ${id}.${handle.id} has stale handle coordinates`);
        }
      }
      const from = await page.locator('.vue-flow__node[data-id="agent"] .source[data-handleid="receipts"]').boundingBox();
      const to = await page.locator('.vue-flow__node[data-id="writeback"] .target[data-handleid="receipts"]').boundingBox();
      await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
      await page.mouse.down();
      await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 12 });
      const preview = await page.evaluate(() => ({
        start: window.fixture.starts.at(-1),
        viewport: window.fixture.flow.viewport.value,
        path: document.querySelector(".vue-flow__connection-path")?.getAttribute("d"),
      }));
      assert.equal(preview.start.handleId, "receipts");
      assert.equal(preview.start.clicked, "receipts");
      const start = /^M\s*([-\d.]+)[,\s]+([-\d.]+)/.exec(preview.path);
      assert.ok(start, "A drag preview path must be rendered");
      assert.ok(Math.abs(Number(start[2]) * zoom + preview.viewport.y - (from.y + from.height / 2)) <= 1,
        "The preview must start at the clicked receipts row");
      await page.mouse.up();
      const connection = await page.evaluate(() => window.fixture.connections.at(-1));
      assert.equal(connection.sourceHandle, "receipts");
      assert.equal(connection.targetHandle, "receipts");
      report.scenarios.push({ zoom, kind, before, layout, start: preview.start, connection });
      await page.screenshot({ path: path.join(evidence, `${kind}-${zoom}.png`) });
    }
    await page.close();
  }
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.unexpected_requests, []);
  console.log("4 canvas scenarios passed: delayed catalog and source/target reorder at two zoom levels.");
} catch (error) {
  report.failure = String(error.stack ?? error);
  throw error;
} finally {
  await fs.writeFile(path.join(evidence, "port-report.json"), JSON.stringify(report, null, 2));
  await browser?.close();
  await server.close();
}
