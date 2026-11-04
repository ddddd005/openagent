// Real browser reproduction of canvas connection / selection / deletion behavior.
//
// Isolation: starts only a test-scoped Vite dev server and a headless Chromium from
// the pre-installed runtime (TAVERN_CHROMIUM_EXECUTABLE). The product API, product
// services, product database and model calls are never contacted: every request
// outside the test Vite origin is aborted, and the fixture seeds the real Pinia
// stores in memory (no configuration application installed, so the store's documented
// in-memory acceptance fallback is exercised for edits).
//
// Evidence: writes graph-interaction-report.json plus screenshots into the evidence
// directory given as argv[2] (the pytest --basetemp child directory).
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const evidence = process.argv[2];
assert.ok(evidence, "An isolated evidence directory is required");
await fs.mkdir(evidence, { recursive: true });
const { chromium } = createRequire(import.meta.url)("playwright");
const { createServer } = await import(pathToFileURL(path.join(root, "frontend/node_modules/vite/dist/node/index.js")));
const { default: vue } = await import(pathToFileURL(path.join(root, "frontend/node_modules/@vitejs/plugin-vue/dist/index.mjs")));

async function sha256(file) {
  try { return crypto.createHash("sha256").update(await fs.readFile(path.join(root, file))).digest("hex"); }
  catch { return null; }
}
async function gitHead() {
  try {
    const raw = (await fs.readFile(path.join(root, ".git/HEAD"), "utf8")).trim();
    const match = /^ref:\s*(.+)$/.exec(raw);
    if (!match) return raw;
    return (await fs.readFile(path.join(root, ".git", match[1].trim()), "utf8")).trim();
  } catch { return null; }
}
async function packageVersion(relative) {
  try { return JSON.parse(await fs.readFile(path.join(root, relative, "package.json"), "utf8")).version ?? null; }
  catch { return null; }
}

// ---------------------------------------------------------------------------
// In-browser fixture: real GraphWorkbench and real stores, in-memory document only.
// ---------------------------------------------------------------------------
const fixture = `
import { computed, createApp, defineComponent, h, nextTick } from "vue";
import { createPinia } from "pinia";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import GraphWorkbench from "/src/components/GraphWorkbench.vue";
import { useWorkflowGraphStore } from "/src/stores/workflowGraph";
import { useWorkspaceStore } from "/src/stores/workspace";
import { useWorkbenchNoticesStore } from "/src/stores/workbenchNotices";
import { createWorkbenchFrontendHost } from "/src/application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "/src/application/workbenchFrontendHost";
import { workflowFrontendSdkKey } from "/src/plugins/workflowFrontendSdk";
import { graphClone } from "/src/domain/workflowGraph";

const probe = window.probe = { ready: false, wb: null, errors: [], unexpectedApiRequests: [] };

// The workbench store eagerly initialises per-workflow management request evidence
// outboxes on first render. Answer only those evidence reads with empty fixtures so
// the editor is not left locked; no other product API endpoint is reachable from
// this isolated fixture.
const realFetch = window.fetch ? window.fetch.bind(window) : null;
window.fetch = function (input, init) {
  const url = String(input);
  if (url === "/api/graph/queries") {
    let operation = "";
    try { operation = JSON.parse(String(init && init.body) || "{}").operation || ""; } catch (error) { operation = ""; }
    if (operation === "request-evidence.read")
      return Promise.resolve(new Response("[]", { status: 200,
        headers: { "Content-Type": "application/json" } }));
    if (operation === "request-evidence.pending")
      return Promise.resolve(new Response(JSON.stringify({ schema_version: 1,
        kind: "workflow.request-evidence-page", items: [], next_cursor: null }), { status: 200,
        headers: { "Content-Type": "application/json" } }));
    probe.unexpectedApiRequests.push(url + ":" + operation);
    return Promise.reject(new Error("unexpected graph query in interaction probe"));
  }
  if (url.startsWith("/api/")) {
    probe.unexpectedApiRequests.push(url);
    return Promise.reject(new Error("product API must not be called by the interaction probe"));
  }
  return realFetch(input, init);
};

const PORT = function (port_id, data_type, data_schema_version) {
  return { port_id: port_id, data_type: data_type, data_schema_version: data_schema_version || 2,
    required: false, multiple: false };
};
const TYPE = function (component_id, component_version, display_name, inputs, outputs) {
  return { component_id: component_id, component_version: component_version, display_name: display_name,
    category: "basic", config_schema: { properties: {} }, default_config: {},
    inputs: inputs, outputs: outputs, is_output: false, executable: true };
};
const sourceType = TYPE("tools.current-input", "1", "Current input", [], [PORT("text", "TEXT", 2)]);
const sinkType = TYPE("tools.output", "1", "Output", [PORT("text", "TEXT", 2)], []);
const mismatchType = TYPE("test.incompatible", "1", "Incompatible", [PORT("prompt", "PROMPT", 2)], []);
const addableType = TYPE("tools.text", "1", "Text", [], []);
const catalog = [sourceType, sinkType, mismatchType, addableType];

function graphNode(type, x, y) {
  return { node_binding_id: crypto.randomUUID(), component_id: type.component_id,
    component_version: type.component_version, title: type.display_name,
    position: { x: x, y: y }, config: {} };
}
function graphEdge(source, target, order) {
  return { edge_id: crypto.randomUUID(), source_node_id: source.node_binding_id,
    source_port_id: "text", target_node_id: target.node_binding_id,
    target_port_id: "text", order: order };
}
function documentFor(workflowId, name, nodes, edges) {
  return { schema_version: 3, workflow_definition_id: workflowId, revision: 1, edit_sequence: 1,
    name: name, nodes: nodes, edges: edges, object_bindings: [], control_edges: [],
    package_lock: [], event_bindings: [] };
}

const pinia = createPinia();
const graph = useWorkflowGraphStore(pinia);
const workspace = useWorkspaceStore(pinia);
const notices = useWorkbenchNoticesStore(pinia);
graph.setPersistenceGuard(function () { return true; });

const first = await graph.createWorkflow();
const second = await graph.createWorkflow();

const firstNodes = { A: graphNode(sourceType, 60, 80), B: graphNode(sinkType, 560, 80),
  C: graphNode(sourceType, 60, 360), D: graphNode(mismatchType, 560, 360),
  E: graphNode(sinkType, 560, 620) };
const edgeSel = graphEdge(firstNodes.C, firstNodes.E, 0);
const firstDoc = documentFor(first, "interaction fixture",
  [firstNodes.A, firstNodes.B, firstNodes.C, firstNodes.D, firstNodes.E], [edgeSel]);
const secondNodes = { A: graphNode(sourceType, 60, 80), B: graphNode(sinkType, 560, 80) };
const secondDoc = documentFor(second, "second workflow", [secondNodes.A, secondNodes.B], []);
graph.catalog = catalog;

probe.first = first;
probe.second = second;
probe.catalog = catalog;
probe.nodes = { A: firstNodes.A.node_binding_id, B: firstNodes.B.node_binding_id,
  C: firstNodes.C.node_binding_id, D: firstNodes.D.node_binding_id, E: firstNodes.E.node_binding_id };
probe.secondNodes = { A: secondNodes.A.node_binding_id, B: secondNodes.B.node_binding_id };
probe.edgeSel = edgeSel.edge_id;
probe.firstDoc = graphClone(firstDoc);
probe.secondDoc = graphClone(secondDoc);

function seed(workflowId, document) {
  graph.entries[workflowId].document = graphClone(document);
  graph.entries[workflowId].saved_document = graphClone(document);
  graph.entries[workflowId].saved_revision = 1;
  graph.entries[workflowId].session_id = null;
  graph.entries[workflowId].pending = null;
  graph.entries[workflowId].diagnostics = [];
  graph.history[workflowId] = { past: [], future: [] };
}
seed(first, firstDoc);
seed(second, secondDoc);
workspace.openWorkflow(second);

function readValue(value) {
  return value && typeof value === "object" && value.__v_isRef ? value.value : value;
}
function handleState(handle) {
  if (!handle) return null;
  return { nodeId: handle.nodeId ?? null, id: handle.id ?? null, type: handle.type ?? null };
}
function nodeIdFor(key) {
  return (workspace.activeWorkflowId === second ? probe.secondNodes : probe.nodes)[key];
}
function documentState() {
  const document = graph.document;
  if (!document) return null;
  return { workflow_definition_id: document.workflow_definition_id, revision: document.revision,
    name: document.name,
    nodes: document.nodes.map(function (node) {
      return { id: node.node_binding_id, component: node.component_id + "@" + node.component_version,
        x: node.position.x, y: node.position.y };
    }),
    edges: document.edges.map(function (edge) {
      return { edge_id: edge.edge_id, source_node_id: edge.source_node_id,
        source_port_id: edge.source_port_id, target_node_id: edge.target_node_id,
        target_port_id: edge.target_port_id, order: edge.order };
    }),
    control_edges: (document.control_edges || []).map(function (edge) { return edge.edge_id; }),
    event_bindings: (document.event_bindings || []).map(function (binding) { return binding.event_id; }) };
}
function workbenchState() {
  const wb = probe.wb;
  const state = wb && wb.$ && wb.$.setupState ? wb.$.setupState : null;
  const flow = state ? readValue(state.flow) : null;
  const message = document.querySelector(".graph-connection-message");
  const dom = {
    connectionMessage: message ? message.textContent : null,
    warningTexts: Array.prototype.map.call(document.querySelectorAll(".graph-warning"),
      function (el) { return el.textContent; }),
    connectingHandles: Array.prototype.map.call(document.querySelectorAll(".vue-flow__handle.connecting"),
      function (el) { return el.getAttribute("data-nodeid") + "/" + el.getAttribute("data-handleid"); }),
    connectionPath: !!document.querySelector(".vue-flow__connection-path"),
    selectedNodesDom: Array.prototype.map.call(document.querySelectorAll(".vue-flow__node.selected"),
      function (el) { return el.getAttribute("data-id"); }),
    selectedEdgesDom: Array.prototype.map.call(document.querySelectorAll(".vue-flow__edge.selected"),
      function (el) { return el.getAttribute("data-id"); }),
    flows: document.querySelectorAll(".vue-flow").length,
    focused: document.activeElement ? (document.activeElement.getAttribute("aria-label")
      || document.activeElement.className || document.activeElement.tagName) : null,
  };
  return {
    activeWorkflowId: workspace.activeWorkflowId,
    hasWorkbench: !!wb,
    hasFlow: !!flow,
    connectOnClick: flow ? readValue(flow.connectOnClick) : null,
    connecting: state ? readValue(state.connecting) : null,
    connectionMessage: state ? readValue(state.connectionMessage) : null,
    editFailure: state ? readValue(state.editFailure) : null,
    selectedNodeIds: graph.selectedNodeIds.slice(),
    selectedEdgeId: graph.selectedEdgeId,
    editorLocked: graph.editorLocked,
    notices: notices.notices.map(function (row) { return row.title + ":" + row.reason; }),
    flow: flow ? {
      connectionStartHandle: handleState(readValue(flow.connectionStartHandle)),
      connectionEndHandle: handleState(readValue(flow.connectionEndHandle)),
      connectionClickStartHandle: handleState(readValue(flow.connectionClickStartHandle)),
      connectionStatus: readValue(flow.connectionStatus),
    } : null,
    dom: dom,
    document: documentState(),
  };
}
probe.snapshot = workbenchState;

function settle() {
  return new Promise(function (resolve) {
    requestAnimationFrame(function () { requestAnimationFrame(function () { requestAnimationFrame(resolve); }); });
  });
}
probe.settle = settle;

probe.waitUnlocked = async function () {
  for (let attempt = 0; attempt < 400; attempt++) {
    if (!graph.editorLocked) return true;
    await nextTick();
    await new Promise(function (resolve) { requestAnimationFrame(resolve); });
  }
  return false;
};
probe.reset = async function () {
  // A workflow switch remounts VueFlow (component :key), the same lifecycle the product
  // uses when the user opens another workflow; it also drops any pending connection
  // state so every scenario starts from a known place.
  workspace.openWorkflow(probe.second);
  await nextTick();
  workspace.openWorkflow(probe.first);
  await nextTick();
  graph.busy = null;
  graph.applicationBusy = false;
  if (probe.restoreConnect) probe.restoreConnect();
  seed(probe.first, probe.firstDoc);
  seed(probe.second, probe.secondDoc);
  graph.selectedNodeIds = [];
  graph.selectedEdgeId = null;
  notices.notices.splice(0, notices.notices.length);
  if (probe.wb && probe.wb.$.setupState) probe.wb.$.setupState.editFailure = "";
  await probe.waitUnlocked();
  await nextTick();
  await settle();
  return workbenchState();
};
probe.switchTo = async function (workflowId) {
  workspace.openWorkflow(workflowId);
  await nextTick();
  await probe.waitUnlocked();
  await settle();
  return workbenchState();
};
probe.openAddMenu = async function () {
  const button = document.querySelector('.graph-toolbar button[aria-label="添加节点"]');
  if (!button) return false;
  button.click();
  await nextTick();
  await settle();
  return !!document.querySelector("[data-canvas-node-menu]");
};
probe.chooseAddFamily = async function (group, itemText) {
  const groupButton = document.querySelector('[data-canvas-node-menu] [data-group-id="' + group + '"]');
  if (!groupButton) return { ok: false, reason: "group-missing" };
  groupButton.click();
  await nextTick();
  await settle();
  const items = Array.prototype.slice.call(
    document.querySelectorAll("[data-node-submenu] button, .canvas-node-menu-list button"));
  const target = items.find(function (button) { return button.textContent.trim() === itemText; });
  if (!target) return { ok: false, reason: "item-missing",
    items: items.map(function (button) { return button.textContent.trim(); }) };
  target.click();
  await settle();
  return { ok: true };
};
probe.handleRect = function (key, direction, handleId) {
  const nodeId = nodeIdFor(key);
  if (!nodeId) return null;
  const element = document.querySelector('.vue-flow__node[data-id="' + nodeId + '"] ' +
    (direction === "target" ? ".target" : ".source") + '[data-handleid="' + handleId + '"]');
  if (!element) return null;
  const box = element.getBoundingClientRect();
  return { x: box.x, y: box.y, width: box.width, height: box.height,
    cx: box.x + box.width / 2, cy: box.y + box.height / 2 };
};
probe.rect = function (selector) {
  const element = document.querySelector(selector);
  if (!element) return null;
  const box = element.getBoundingClientRect();
  return { x: box.x, y: box.y, width: box.width, height: box.height,
    cx: box.x + box.width / 2, cy: box.y + box.height / 2 };
};
probe.nodeRect = function (key) {
  const nodeId = nodeIdFor(key);
  return nodeId ? probe.rect('.vue-flow__node[data-id="' + nodeId + '"]') : null;
};
probe.edgePoint = function (edgeId) {
  const edge = document.querySelector('.vue-flow__edge[data-id="' + edgeId + '"]');
  if (!edge) return null;
  const path = edge.querySelector(".vue-flow__edge-interaction") || edge.querySelector(".vue-flow__edge-path");
  if (!path || typeof path.getTotalLength !== "function") return null;
  const local = path.getPointAtLength(path.getTotalLength() / 2);
  const point = new DOMPoint(local.x, local.y).matrixTransform(path.getScreenCTM());
  return { x: point.x, y: point.y };
};
probe.canvasRect = function () { return probe.rect(".graph-canvas"); };
probe.setDiagnostics = async function (nodeKey) {
  graph.entries[probe.first].diagnostics = [{ reason_code: "fixture.state",
    message: "fixture diagnostic", node_id: nodeIdFor(nodeKey), dependent_outputs: [], phase: "edit" }];
  await nextTick();
  await settle();
};
probe.setBusy = async function (value) {
  graph.busy = value;
  await nextTick();
  await settle();
};
// Fixture-only document mutation used to simulate a concurrent edit that lands between
// the drag validation and the release, exercising the real store-call admission path.
probe.injectEdge = async function (sourceKey, targetKey) {
  graph.entries[probe.first].document.edges.push({ edge_id: crypto.randomUUID(),
    source_node_id: probe.nodes[sourceKey], source_port_id: "text",
    target_node_id: probe.nodes[targetKey], target_port_id: "text", order: 0 });
  await nextTick();
  await settle();
};
// Stub the store save port only inside the fixture to observe how the component handles a
// real "failed" save result (persistent step-1 feedback) separately from admission hints.
const originalConnect = graph.connect;
probe.stubConnectFailure = async function (message) {
  graph.connect = async function () { return { outcome: "failed", error: new Error(message) }; };
  await nextTick();
  await settle();
};
probe.restoreConnect = function () { graph.connect = originalConnect; };

const app = createApp(defineComponent({
  setup: function () {
    const host = createWorkbenchFrontendHost(function () { return graph.frontendExtensions; },
      function () { return graph.packageLock; });
    app.provide(workbenchFrontendHostKey, host);
    app.provide(workflowFrontendSdkKey, {
      workflowId: computed(function () { return workspace.activeWorkflowId; }),
      document: computed(function () { return graph.document; }),
      catalog: computed(function () { return graph.catalog; }),
      packages: computed(function () { return graph.packageLock; }),
      session: computed(function () { return graph.session; }),
      locked: computed(function () { return graph.editorLocked; }),
      lifecycle: computed(function () { return "fixture"; }),
      configureNode: async function () { return null; },
    });
    return function () { return h(GraphWorkbench); };
  },
}));
app.use(pinia);
app.mixin({ mounted: function () {
  const name = this.$options.__name || this.$options.name;
  if (name === "GraphWorkbench") probe.wb = this;
} });
app.config.errorHandler = function (error) {
  probe.errors.push(String(error && error.stack || error));
  console.error(error);
};
window.addEventListener("unhandledrejection", function (event) {
  probe.errors.push("unhandledrejection: " + String(event.reason && event.reason.stack || event.reason));
});
app.mount("#app");
probe.ready = true;
`;

const server = await createServer({
  root: path.join(root, "frontend"), configFile: false, logLevel: "error",
  plugins: [vue(), {
    name: "isolated-graph-interaction-fixture",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (request.url === "/") {
          response.setHeader("Content-Type", "text/html");
          response.end('<html><head><style>html,body{margin:0;padding:0}'
            + '#app{width:1440px;height:900px;overflow:hidden}</style></head>'
            + '<body><div id="app"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        } else if (request.url === "/fixture-entry.js") {
          server.transformRequest("/virtual-interaction-fixture.js").then(result => {
            response.setHeader("Content-Type", "text/javascript");
            response.end(result.code);
          }).catch(next);
        } else next();
      });
    },
    resolveId(id) { if (id === "/virtual-interaction-fixture.js") return "\0graph-interaction-fixture"; },
    load(id) { if (id === "\0graph-interaction-fixture") return fixture; },
  }],
  server: { host: "127.0.0.1", port: 0, strictPort: false },
});

const report = {
  schema_version: 1,
  probe: "graph_interaction_probe.mjs",
  generated_at: new Date().toISOString(),
  baseline: {
    head: await gitHead(),
    environment: {
      chromium: process.env.TAVERN_CHROMIUM_EXECUTABLE ?? null,
      vue_flow_core: await packageVersion("frontend/node_modules/@vue-flow/core"),
      vite: await packageVersion("frontend/node_modules/vite"),
    },
    files: {
      "frontend/src/components/GraphWorkbench.vue": await sha256("frontend/src/components/GraphWorkbench.vue"),
      "frontend/src/stores/workflowGraph.ts": await sha256("frontend/src/stores/workflowGraph.ts"),
      "frontend/src/domain/graphConnections.ts": await sha256("frontend/src/domain/graphConnections.ts"),
      "frontend/src/application/workflowEditing.ts": await sha256("frontend/src/application/workflowEditing.ts"),
    },
  },
  scenarios: [], errors: [], unexpected_requests: [], fixture_errors: [], reproductions: {},
};

let browser, page;
const screenshot = async name => page.screenshot({ path: path.join(evidence, name + ".png") });
const settle = async () => page.evaluate(() => window.probe.settle());
const snapshot = async () => page.evaluate(() => window.probe.snapshot());
const reset = async () => { await page.evaluate(() => window.probe.reset()); await settle(); };

// S2-01 owns the connection lifecycle; S2-03 owns the selection integration. Both
// categories are asserted now; the categories filter only helps focused reruns.
const selectionScenarios = new Set([
  "selection-click-node", "selection-click-edge", "selection-node-click-clears-edge",
  "selection-edge-click-clears-nodes", "selection-ctrl-click-multi-nodes", "selection-box-multi-nodes",
  "selection-esc-clears", "selection-pane-click-clears", "selection-drag-release-keeps-selection",
  "edge-then-add-node-then-delete", "edge-then-locate-then-delete", "undo-after-add-node",
  "delete-node-clears-selection", "delete-edge-clears-selection",
  "delete-in-editor-input-keeps-node", "trash-button-deletes-selection", "locked-delete",
  "switch-workflow-clears-selection", "esc-closes-menu-before-clearing-selection",
  "handle-click-selection-observation",
]);
const categories = new Set((process.env.PROBE_CATEGORIES ?? "connection,selection")
  .split(",").map(value => value.trim()).filter(Boolean));

async function scenario(name, body) {
  const category = selectionScenarios.has(name) ? "selection" : "connection";
  const record = { name, category, steps: [], error: null };
  report.scenarios.push(record);
  if (!categories.has(category)) { record.skipped = true; return; }
  try {
    await reset();
    record.before = await snapshot();
    await body(record);
  } catch (error) {
    record.error = String(error && error.stack || error);
  }
  try { record.after = await snapshot(); } catch { /* the page may be gone */ }
}

const handleCenter = (key, direction, handleId) =>
  page.evaluate(([nodeKey, dir, id]) => window.probe.handleRect(nodeKey, dir, id), [key, direction, handleId]);
const nodeCenter = async key => {
  const rect = await page.evaluate(nodeKey => window.probe.nodeRect(nodeKey), key);
  if (!rect) throw new Error("node not found: " + key);
  return { x: rect.x + 18, y: rect.y + 12 };
};
const edgeCenter = async () => {
  const point = await page.evaluate(() => window.probe.edgePoint(window.probe.edgeSel));
  if (!point) throw new Error("pre-existing edge not found");
  return point;
};
const clickHandle = async (key, direction, handleId) => {
  const rect = await handleCenter(key, direction, handleId);
  if (!rect) throw new Error("handle not found: " + key + "." + handleId);
  await page.mouse.move(rect.cx, rect.cy);
  await page.mouse.down();
  await page.mouse.up();
  await settle();
};
const dragFromTo = async (fromKey, fromHandle, toKey, toDirection, toHandle, fromDirection = "source") => {
  const from = await handleCenter(fromKey, fromDirection, fromHandle);
  const to = await handleCenter(toKey, toDirection, toHandle);
  if (!from || !to) throw new Error("handle pair not found: " + fromKey + " -> " + toKey);
  await page.mouse.move(from.cx, from.cy);
  await page.mouse.down();
  await page.mouse.move(to.cx, to.cy, { steps: 14 });
  await settle();
  return to;
};
const release = async () => { await page.mouse.up(); await settle(); };
const canvasRect = () => page.evaluate(() => window.probe.canvasRect());
const focusWorkbench = () => page.locator(".graph-workbench").focus();
const focusCanvas = () => page.locator(".graph-canvas").focus();
const pressDelete = async () => { await focusWorkbench(); await page.keyboard.press("Delete"); await settle(); };
const pressEscape = async () => { await page.keyboard.press("Escape"); await settle(); };
const startDrag = async (key, direction, handleId) => {
  const rect = await handleCenter(key, direction, handleId);
  if (!rect) throw new Error("handle not found: " + key + "." + handleId);
  await page.mouse.move(rect.cx, rect.cy);
  await page.mouse.down();
  await settle();
  return rect;
};
const hoverHandle = async (key, direction, handleId) => {
  const rect = await handleCenter(key, direction, handleId);
  if (!rect) throw new Error("handle not found: " + key + "." + handleId);
  await page.mouse.move(rect.cx, rect.cy, { steps: 12 });
  await settle();
  return rect;
};
const moveToPoint = async (x, y) => { await page.mouse.move(x, y, { steps: 12 }); await settle(); };
const moveToBlank = async () => {
  const canvas = await canvasRect();
  await moveToPoint(canvas.x + canvas.width / 2, canvas.y + canvas.height / 2);
};

try {
  await server.listen();
  const port = server.httpServer.address().port;
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("pageerror", error => report.errors.push({ scope: "pageerror", message: String(error && error.stack || error) }));
  page.on("console", message => {
    if (message.type() === "error") report.fixture_errors.push({ scope: "console", message: message.text() });
  });
  page.on("response", response => {
    if (response.status() >= 400)
      report.fixture_errors.push({ scope: "response", status: response.status(), url: response.url() });
  });
  page.on("requestfailed", request => report.fixture_errors.push({ scope: "requestfailed",
    url: request.url(), error: request.failure() ? request.failure().errorText : null }));
  await page.addInitScript(() => {
    const originalFetch = window.fetch;
    window.__fetchStacks = [];
    window.fetch = function () {
      try { window.__fetchStacks.push({ url: String(arguments[0]), stack: String(new Error().stack) }); }
      catch (error) { /* ignore */ }
      return originalFetch.apply(window, arguments);
    };
  });
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" || url.port !== String(port)) {
      report.unexpected_requests.push(url.href);
      await route.abort();
      return;
    }
    await route.continue();
  });
  await page.goto(`http://127.0.0.1:${port}/`);
  await page.waitForFunction(() => window.probe?.ready === true, null, { timeout: 20000 });
  await page.evaluate(() => window.probe.reset());
  await page.waitForFunction(() => {
    const wb = window.probe.wb, state = wb && wb.$ && wb.$.setupState;
    const flow = state && (state.flow && state.flow.__v_isRef ? state.flow.value : state.flow);
    return !!flow && document.querySelectorAll(".vue-flow__node").length >= 5
      && document.querySelectorAll(".vue-flow__edge").length >= 1
      && window.probe.snapshot().editorLocked === false;
  }, null, { timeout: 20000 });
  report.fetch_stacks = await page.evaluate(() => window.__fetchStacks || []);
  await reset();
  report.ready = await snapshot();
  report.fixture = await page.evaluate(() => ({ nodes: window.probe.nodes,
    secondNodes: window.probe.secondNodes, edgeSel: window.probe.edgeSel,
    first: window.probe.first, second: window.probe.second }));
  await screenshot("00-ready");

  // -------------------------------------------------------------------------
  // Connection lifecycle after Q04-1/Q04-2: drag-only, cancelable, specific hints.
  // -------------------------------------------------------------------------
  await scenario("click-handle-no-pending", async record => {
    await clickHandle("A", "source", "text");
    record.steps.push({ step: "after-click", state: await snapshot() });
    await screenshot("01-click-handle-no-pending");
  });

  await scenario("click-handle-then-blank", async record => {
    await clickHandle("A", "source", "text");
    record.steps.push({ step: "after-click", state: await snapshot() });
    const canvas = await canvasRect();
    await page.mouse.click(canvas.x + 40, canvas.y + canvas.height - 40);
    await settle();
    record.steps.push({ step: "after-blank-click", state: await snapshot() });
  });

  await scenario("click-handle-then-valid-target", async record => {
    await clickHandle("A", "source", "text");
    await clickHandle("B", "target", "text");
    record.steps.push({ step: "after-target-click", state: await snapshot() });
    await screenshot("02-click-then-target-no-edge");
  });

  await scenario("click-handle-then-illegal-target", async record => {
    await clickHandle("A", "source", "text");
    await clickHandle("D", "target", "prompt");
    record.steps.push({ step: "after-illegal-target-click", state: await snapshot() });
  });

  await scenario("drag-valid-target", async record => {
    await dragFromTo("A", "text", "B", "target", "text");
    record.steps.push({ step: "during-drag-over-target", state: await snapshot() });
    await screenshot("03-drag-over-target");
    await release();
    record.steps.push({ step: "after-release-on-target", state: await snapshot() });
    await screenshot("04-drag-released-on-target");
  });

  await scenario("drag-release-on-blank", async record => {
    const canvas = await canvasRect();
    await startDrag("A", "source", "text");
    await moveToPoint(canvas.x + canvas.width / 2, canvas.y + canvas.height / 2);
    record.steps.push({ step: "during-drag-on-blank", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-release-on-blank", state: await snapshot() });
  });

  await scenario("drag-over-target-then-release-on-blank", async record => {
    await dragFromTo("A", "text", "B", "target", "text");
    record.steps.push({ step: "during-drag-over-compatible-target", state: await snapshot() });
    await moveToBlank();
    record.steps.push({ step: "moved-to-blank-still-pressed", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-release-on-blank", state: await snapshot() });
    await screenshot("05-blank-release-after-hover");
  });

  await scenario("drag-incompatible-target", async record => {
    await dragFromTo("A", "text", "D", "target", "prompt");
    record.steps.push({ step: "during-drag-over-incompatible-target", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-release-on-incompatible-target", state: await snapshot() });
    await screenshot("06-drag-incompatible-target");
  });

  await scenario("drag-incompatible-then-valid-drag", async record => {
    await dragFromTo("A", "text", "D", "target", "prompt");
    await release();
    record.steps.push({ step: "after-rejected-release", state: await snapshot() });
    await dragFromTo("A", "text", "B", "target", "text");
    record.steps.push({ step: "during-next-drag", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-next-drag", state: await snapshot() });
  });

  await scenario("drag-esc-preserves-selection", async record => {
    const point = await nodeCenter("C");
    await page.mouse.click(point.x, point.y);
    await settle();
    record.steps.push({ step: "before-drag", state: await snapshot() });
    await focusCanvas();
    await startDrag("A", "source", "text");
    await hoverHandle("B", "target", "text");
    record.steps.push({ step: "during-drag-over-target", state: await snapshot() });
    await pressEscape();
    record.steps.push({ step: "after-esc", state: await snapshot() });
    await moveToBlank();
    await release();
    record.steps.push({ step: "after-release-on-blank", state: await snapshot() });
    await screenshot("07-esc-preserves-selection");
  });

  await scenario("drag-esc-release-on-valid-target", async record => {
    await focusCanvas();
    await startDrag("A", "source", "text");
    await hoverHandle("B", "target", "text");
    record.steps.push({ step: "during-drag-over-target", state: await snapshot() });
    await pressEscape();
    record.steps.push({ step: "after-esc", state: await snapshot() });
    // Adversarial: move back over the same valid target before releasing (Q04-2).
    await hoverHandle("B", "target", "text");
    record.steps.push({ step: "after-esc-rehover-target", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-release-on-valid-target", state: await snapshot() });
    await screenshot("08-esc-release-on-valid-target");
    await dragFromTo("A", "text", "B", "target", "text");
    await release();
    record.steps.push({ step: "after-immediate-new-drag", state: await snapshot() });
    await screenshot("09-esc-then-new-drag");
  });

  await scenario("drag-esc-release-on-blank", async record => {
    await focusCanvas();
    await startDrag("A", "source", "text");
    await hoverHandle("B", "target", "text");
    await pressEscape();
    record.steps.push({ step: "after-esc", state: await snapshot() });
    await moveToBlank();
    await release();
    record.steps.push({ step: "after-release-on-blank", state: await snapshot() });
    await dragFromTo("A", "text", "B", "target", "text");
    await release();
    record.steps.push({ step: "after-next-legal-drag", state: await snapshot() });
    await screenshot("10-esc-release-blank-then-new-drag");
  });

  await scenario("drag-esc-after-focus-blur", async record => {
    await focusCanvas();
    await startDrag("A", "source", "text");
    await hoverHandle("B", "target", "text");
    record.steps.push({ step: "during-drag-over-target", state: await snapshot() });
    await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); });
    record.steps.push({ step: "after-focus-blur", state: await snapshot() });
    await pressEscape();
    record.steps.push({ step: "after-esc", state: await snapshot() });
    await moveToBlank();
    await release();
    record.steps.push({ step: "after-release", state: await snapshot() });
    await screenshot("10b-esc-without-focused-canvas");
  });

  await scenario("esc-on-idle-canvas", async record => {
    const point = await nodeCenter("A");
    await page.mouse.click(point.x, point.y);
    await settle();
    record.steps.push({ step: "before-esc", state: await snapshot() });
    await focusCanvas();
    await pressEscape();
    record.steps.push({ step: "after-esc", state: await snapshot() });
  });

  await scenario("drag-to-source-handle", async record => {
    await dragFromTo("A", "text", "C", "source", "text");
    record.steps.push({ step: "during-drag-over-source-handle", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-release-on-source-handle", state: await snapshot() });
    await screenshot("11-drag-to-source-handle");
  });

  await scenario("rejection-message-dismiss", async record => {
    await dragFromTo("A", "text", "D", "target", "prompt");
    await release();
    record.steps.push({ step: "after-rejected-release", state: await snapshot() });
    await page.locator('button[aria-label="关闭连线提示"]').click();
    await settle();
    record.steps.push({ step: "after-dismiss", state: await snapshot() });
  });

  await scenario("rejection-message-cleared-by-blank-click", async record => {
    await dragFromTo("A", "text", "D", "target", "prompt");
    await release();
    record.steps.push({ step: "after-rejected-release", state: await snapshot() });
    const canvas = await canvasRect();
    await page.mouse.click(canvas.x + 40, canvas.y + canvas.height - 40);
    await settle();
    record.steps.push({ step: "after-blank-click", state: await snapshot() });
  });

  await scenario("admission-failure-on-release-uses-hint", async record => {
    await startDrag("A", "source", "text");
    await hoverHandle("B", "target", "text");
    record.steps.push({ step: "during-drag-over-target", state: await snapshot() });
    await page.evaluate(() => window.probe.injectEdge("C", "B"));
    record.steps.push({ step: "after-concurrent-edit", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-release", state: await snapshot() });
    await screenshot("12-admission-failure-hint");
  });

  await scenario("input-occupied-target-message", async record => {
    await dragFromTo("A", "text", "B", "target", "text");
    await release();
    record.steps.push({ step: "after-first-edge", state: await snapshot() });
    await dragFromTo("C", "text", "B", "target", "text");
    record.steps.push({ step: "during-second-drag", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-second-release", state: await snapshot() });
    await screenshot("13-input-occupied-message");
  });

  await scenario("drag-target-to-source-handle", async record => {
    await dragFromTo("B", "text", "A", "source", "text", "target");
    record.steps.push({ step: "during-reverse-drag", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-reverse-release", state: await snapshot() });
  });

  await scenario("locked-drag-handle", async record => {
    await page.evaluate(() => window.probe.setBusy("fixture-lock"));
    record.steps.push({ step: "locked", state: await snapshot() });
    const from = await handleCenter("A", "source", "text");
    const to = await handleCenter("B", "target", "text");
    await page.mouse.move(from.cx, from.cy);
    await page.mouse.down();
    await page.mouse.move(to.cx, to.cy, { steps: 10 });
    await settle();
    record.steps.push({ step: "during-locked-drag", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-locked-release", state: await snapshot() });
    await page.evaluate(() => window.probe.setBusy(null));
    record.steps.push({ step: "after-unlock", state: await snapshot() });
  });

  await scenario("switch-workflow-clears-connection-state", async record => {
    const point = await nodeCenter("C");
    await page.mouse.click(point.x, point.y);
    await settle();
    await dragFromTo("A", "text", "D", "target", "prompt");
    await release();
    record.steps.push({ step: "rejected-message-and-selection", state: await snapshot() });
    await page.evaluate(() => window.probe.switchTo(window.probe.second));
    record.steps.push({ step: "after-switch-to-second", state: await snapshot() });
    await screenshot("14-switched-workflow");
    await dragFromTo("A", "text", "B", "target", "text");
    await release();
    record.steps.push({ step: "after-drag-on-second", state: await snapshot() });
    await page.evaluate(() => window.probe.switchTo(window.probe.first));
    record.steps.push({ step: "back-to-first-workflow", state: await snapshot() });
  });

  await scenario("mid-gesture-switch-creates-no-edge", async record => {
    await focusCanvas();
    await startDrag("A", "source", "text");
    await hoverHandle("B", "target", "text");
    record.steps.push({ step: "during-drag", state: await snapshot() });
    await page.evaluate(() => window.probe.switchTo(window.probe.second));
    record.steps.push({ step: "after-switch", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-release-on-new-workflow", state: await snapshot() });
    await page.evaluate(() => window.probe.switchTo(window.probe.first));
    record.steps.push({ step: "back-to-first", state: await snapshot() });
  });

  await scenario("save-failure-uses-persistent-feedback", async record => {
    await page.evaluate(() => window.probe.stubConnectFailure("fixture save failure"));
    record.steps.push({ step: "stubbed", state: await snapshot() });
    await dragFromTo("A", "text", "B", "target", "text");
    record.steps.push({ step: "during-drag", state: await snapshot() });
    await release();
    record.steps.push({ step: "after-release", state: await snapshot() });
    await page.evaluate(() => window.probe.restoreConnect());
    record.steps.push({ step: "after-restore", state: await snapshot() });
    await dragFromTo("A", "text", "B", "target", "text");
    await release();
    record.steps.push({ step: "after-reconnect", state: await snapshot() });
    await screenshot("15-save-failure-persistent-feedback");
  });

  // -------------------------------------------------------------------------
  // Q04-3/Q04-4 selection integration (S2-03) — asserted in the report flags.
  // -------------------------------------------------------------------------
  const clickNode = async key => {
    const point = await nodeCenter(key);
    await page.mouse.click(point.x, point.y);
    await settle();
  };
  const ctrlClickNode = async key => {
    const point = await nodeCenter(key);
    await page.keyboard.down("Control");
    await page.mouse.click(point.x, point.y);
    await page.keyboard.up("Control");
    await settle();
  };
  const clickEdge = async () => {
    const point = await edgeCenter();
    await page.mouse.click(point.x, point.y);
    await settle();
  };

  await scenario("selection-click-node", async record => {
    await clickNode("A");
    record.steps.push({ step: "after-node-click", state: await snapshot() });
  });

  await scenario("selection-click-edge", async record => {
    await clickEdge();
    record.steps.push({ step: "after-edge-click", state: await snapshot() });
    await screenshot("15-edge-selected");
  });

  await scenario("selection-node-click-clears-edge", async record => {
    await clickEdge();
    record.steps.push({ step: "after-edge-click", state: await snapshot() });
    await clickNode("A");
    record.steps.push({ step: "after-node-click", state: await snapshot() });
  });

  await scenario("selection-edge-click-clears-nodes", async record => {
    await clickNode("A");
    record.steps.push({ step: "after-node-click", state: await snapshot() });
    await clickEdge();
    record.steps.push({ step: "after-edge-click", state: await snapshot() });
  });

  await scenario("selection-ctrl-click-multi-nodes", async record => {
    await clickNode("A");
    record.steps.push({ step: "after-first-node", state: await snapshot() });
    await ctrlClickNode("C");
    record.steps.push({ step: "after-ctrl-add", state: await snapshot() });
    await ctrlClickNode("A");
    record.steps.push({ step: "after-ctrl-remove", state: await snapshot() });
    await screenshot("16-ctrl-click-multi-nodes");
  });

  await scenario("selection-box-multi-nodes", async record => {
    await clickEdge();
    record.steps.push({ step: "after-edge-click", state: await snapshot() });
    const rectA = await page.evaluate(() => window.probe.nodeRect("A"));
    const rectC = await page.evaluate(() => window.probe.nodeRect("C"));
    if (!rectA || !rectC) throw new Error("nodes A/C not found");
    const start = { x: Math.min(rectA.x, rectC.x) - 24, y: rectA.y - 24 };
    const end = { x: Math.max(rectA.x + rectA.width, rectC.x + rectC.width) + 24,
      y: rectC.y + rectC.height + 24 };
    await page.keyboard.down("Shift");
    await page.mouse.move(start.x, start.y);
    await page.mouse.down();
    await page.mouse.move(end.x, end.y, { steps: 14 });
    await settle();
    record.steps.push({ step: "during-box", state: await snapshot() });
    await page.mouse.up();
    await page.keyboard.up("Shift");
    await settle();
    record.steps.push({ step: "after-box", state: await snapshot() });
    await screenshot("17-box-multi-nodes");
  });

  await scenario("selection-esc-clears", async record => {
    await clickNode("A");
    record.steps.push({ step: "before-esc", state: await snapshot() });
    await focusWorkbench();
    await page.keyboard.press("Escape");
    await settle();
    record.steps.push({ step: "after-esc", state: await snapshot() });
  });

  await scenario("selection-pane-click-clears", async record => {
    await clickEdge();
    record.steps.push({ step: "before-pane-click", state: await snapshot() });
    const canvas = await canvasRect();
    await page.mouse.click(canvas.x + 30, canvas.y + canvas.height - 30);
    await settle();
    record.steps.push({ step: "after-pane-click", state: await snapshot() });
  });

  await scenario("selection-drag-release-keeps-selection", async record => {
    await clickNode("C");
    record.steps.push({ step: "before-drag", state: await snapshot() });
    await dragFromTo("A", "text", "B", "target", "text");
    await release();
    record.steps.push({ step: "after-connection-drag", state: await snapshot() });
  });

  await scenario("edge-then-add-node-then-delete", async record => {
    await clickEdge();
    record.steps.push({ step: "after-edge-click", state: await snapshot() });
    if (!await page.evaluate(() => window.probe.openAddMenu())) throw new Error("add menu did not open");
    const chosen = await page.evaluate(() => window.probe.chooseAddFamily("basic", "文本"));
    record.steps.push({ step: "after-add-node", state: await snapshot(), chosen });
    await pressDelete();
    record.steps.push({ step: "after-delete", state: await snapshot() });
    await screenshot("18-edge-add-node-delete");
  });

  await scenario("edge-then-locate-then-delete", async record => {
    await page.evaluate(() => window.probe.setDiagnostics("D"));
    await clickEdge();
    record.steps.push({ step: "after-edge-click", state: await snapshot() });
    await page.locator(".graph-diagnostic").first().click();
    await settle();
    record.steps.push({ step: "after-locate-click", state: await snapshot() });
    await pressDelete();
    record.steps.push({ step: "after-delete", state: await snapshot() });
    await screenshot("19-edge-locate-delete");
  });

  await scenario("undo-after-add-node", async record => {
    if (!await page.evaluate(() => window.probe.openAddMenu())) throw new Error("add menu did not open");
    const chosen = await page.evaluate(() => window.probe.chooseAddFamily("basic", "文本"));
    record.steps.push({ step: "after-add-node", state: await snapshot(), chosen });
    await focusWorkbench();
    await page.keyboard.press("Control+z");
    await settle();
    record.steps.push({ step: "after-undo", state: await snapshot() });
    await pressDelete();
    record.steps.push({ step: "after-delete", state: await snapshot() });
    await screenshot("20-undo-stale-selection");
  });

  await scenario("delete-node-clears-selection", async record => {
    await clickNode("A");
    record.steps.push({ step: "before-delete", state: await snapshot() });
    await pressDelete();
    record.steps.push({ step: "after-delete", state: await snapshot() });
  });

  await scenario("delete-edge-clears-selection", async record => {
    await clickEdge();
    record.steps.push({ step: "before-delete", state: await snapshot() });
    await pressDelete();
    record.steps.push({ step: "after-delete", state: await snapshot() });
  });

  await scenario("delete-in-editor-input-keeps-node", async record => {
    await clickNode("A");
    record.steps.push({ step: "after-node-click", state: await snapshot() });
    await page.locator(".graph-inspector input").first().click();
    await page.keyboard.press("Delete");
    await settle();
    record.steps.push({ step: "after-delete-in-input", state: await snapshot() });
  });

  await scenario("trash-button-deletes-selection", async record => {
    await clickNode("A");
    record.steps.push({ step: "after-node-click", state: await snapshot() });
    await page.locator('button[aria-label="删除选中"]').click();
    await settle();
    record.steps.push({ step: "after-trash-click", state: await snapshot() });
  });

  await scenario("locked-delete", async record => {
    await clickNode("A");
    record.steps.push({ step: "after-node-click", state: await snapshot() });
    await page.evaluate(() => window.probe.setBusy("fixture-lock"));
    record.steps.push({ step: "locked", state: await snapshot() });
    await pressDelete();
    record.steps.push({ step: "after-delete-request", state: await snapshot() });
    await screenshot("21-locked-delete");
    await page.evaluate(() => window.probe.setBusy(null));
    record.steps.push({ step: "after-unlock", state: await snapshot() });
  });

  await scenario("switch-workflow-clears-selection", async record => {
    await clickNode("A");
    record.steps.push({ step: "selected-on-first", state: await snapshot() });
    await page.evaluate(() => window.probe.switchTo(window.probe.second));
    record.steps.push({ step: "after-switch-to-second", state: await snapshot() });
    await page.evaluate(() => window.probe.switchTo(window.probe.first));
    record.steps.push({ step: "back-to-first", state: await snapshot() });
    await screenshot("22-switch-clears-selection");
  });

  await scenario("esc-closes-menu-before-clearing-selection", async record => {
    await clickNode("C");
    record.steps.push({ step: "after-node-click", state: await snapshot() });
    if (!await page.evaluate(() => window.probe.openAddMenu())) throw new Error("add menu did not open");
    record.steps.push({ step: "menu-open", state: await snapshot(),
      menuOpen: await page.evaluate(() => !!document.querySelector("[data-canvas-node-menu]")) });
    await pressEscape();
    record.steps.push({ step: "after-first-esc", state: await snapshot(),
      menuOpen: await page.evaluate(() => !!document.querySelector("[data-canvas-node-menu]")) });
    await focusWorkbench();
    await pressEscape();
    record.steps.push({ step: "after-second-esc", state: await snapshot() });
  });

  await scenario("handle-click-selection-observation", async record => {
    await clickHandle("A", "source", "text");
    record.steps.push({ step: "after-handle-click", state: await snapshot() });
  });

  // -------------------------------------------------------------------------
  // Q04-1/Q04-2 expectations (S2-01): every fact below must hold.
  // -------------------------------------------------------------------------
  const byName = name => report.scenarios.find(row => row.name === name);
  const step = (name, index) => byName(name)?.steps?.[index]?.state ?? null;
  const lastStep = name => {
    const row = byName(name);
    return row?.steps?.length ? row.steps[row.steps.length - 1].state : null;
  };
  const edges = state => state?.document?.edges ?? [];
  const nodes = state => state?.document?.nodes ?? [];
  const edgeCount = state => edges(state).length;
  const count = name => ({ before: edgeCount(byName(name)?.before), after: edgeCount(lastStep(name)) });
  const noConnectionState = state => !!state && !state.connecting
    && !state.flow?.connectionStartHandle && !state.flow?.connectionClickStartHandle;
  const messageOf = state => (state?.dom?.connectionMessage ?? null);
  const fixtureNodes = report.fixture.nodes;
  const hasEdge = (state, source, target) => edges(state).some(edge =>
    edge.source_node_id === source && edge.target_node_id === target);

  report.reproductions = {
    connect_on_click_disabled:
      report.ready.connectOnClick === false && report.ready.flow.connectionClickStartHandle === null,
    click_handle_creates_no_pending_state:
      noConnectionState(lastStep("click-handle-no-pending")) && !messageOf(lastStep("click-handle-no-pending")),
    click_handle_creates_no_edge:
      count("click-handle-no-pending").after === count("click-handle-no-pending").before,
    click_handle_then_blank_creates_no_edge:
      count("click-handle-then-blank").after === count("click-handle-then-blank").before
        && noConnectionState(lastStep("click-handle-then-blank")),
    click_handle_then_valid_target_creates_no_edge:
      count("click-handle-then-valid-target").after === count("click-handle-then-valid-target").before
        && !messageOf(lastStep("click-handle-then-valid-target")),
    click_handle_then_illegal_target_creates_no_edge:
      count("click-handle-then-illegal-target").after === count("click-handle-then-illegal-target").before
        && !messageOf(lastStep("click-handle-then-illegal-target")),
    drag_valid_target_shows_hint_then_creates_edge:
      messageOf(step("drag-valid-target", 0)) === "端口兼容，可连接"
        && count("drag-valid-target").after === count("drag-valid-target").before + 1,
    drag_valid_target_clears_hint_after_save:
      !messageOf(lastStep("drag-valid-target")) && noConnectionState(lastStep("drag-valid-target")),
    blank_release_creates_no_edge_and_no_hint:
      count("drag-release-on-blank").after === count("drag-release-on-blank").before
        && !messageOf(lastStep("drag-release-on-blank"))
        && noConnectionState(lastStep("drag-release-on-blank")),
    hover_then_blank_release_clears_hint:
      messageOf(step("drag-over-target-then-release-on-blank", 0)) === "端口兼容，可连接"
        && !messageOf(lastStep("drag-over-target-then-release-on-blank"))
        && count("drag-over-target-then-release-on-blank").after
          === count("drag-over-target-then-release-on-blank").before,
    incompatible_target_creates_no_edge_and_shows_reason:
      count("drag-incompatible-target").after === count("drag-incompatible-target").before
        && /端口不兼容/.test(String(messageOf(lastStep("drag-incompatible-target"))))
        && noConnectionState(lastStep("drag-incompatible-target")),
    rejected_hint_clears_when_next_connection_starts:
      messageOf(step("drag-incompatible-then-valid-drag", 1)) === "端口兼容，可连接"
        && count("drag-incompatible-then-valid-drag").after
          === count("drag-incompatible-then-valid-drag").before + 1,
    rejection_hint_dismissible:
      !!messageOf(step("rejection-message-dismiss", 0)) && !messageOf(lastStep("rejection-message-dismiss")),
    rejection_hint_cleared_by_blank_click:
      !!messageOf(step("rejection-message-cleared-by-blank-click", 0))
        && !messageOf(lastStep("rejection-message-cleared-by-blank-click")),
    esc_cancels_in_flight_gesture_and_keeps_selection:
      (function () {
        const scenario = byName("drag-esc-preserves-selection");
        const before = scenario.steps[0].state, afterEsc = scenario.steps[2].state;
        return before.selectedNodeIds.length === 1
          && afterEsc.selectedNodeIds.join() === before.selectedNodeIds.join()
          && noConnectionState(afterEsc) && !messageOf(afterEsc);
      })(),
    esc_then_release_over_valid_target_creates_no_edge:
      (function () {
        const scenario = byName("drag-esc-release-on-valid-target");
        const afterRelease = scenario.steps[3].state;
        return noConnectionState(afterRelease)
          && edgeCount(afterRelease) === edgeCount(scenario.before);
      })(),
    esc_then_next_drag_creates_edge:
      (function () {
        const scenario = byName("drag-esc-release-on-valid-target");
        return edgeCount(scenario.steps[4].state) === edgeCount(scenario.steps[3].state) + 1;
      })(),
    esc_then_release_on_blank_creates_no_edge_and_no_hint:
      (function () {
        const scenario = byName("drag-esc-release-on-blank");
        return edgeCount(scenario.steps[1].state) === edgeCount(scenario.before)
          && !messageOf(scenario.steps[1].state) && noConnectionState(scenario.steps[1].state);
      })(),
    esc_blank_then_next_drag_creates_edge:
      (function () {
        const scenario = byName("drag-esc-release-on-blank");
        return edgeCount(scenario.steps[2].state) === edgeCount(scenario.steps[1].state) + 1;
      })(),
    esc_cancels_even_without_focused_canvas:
      (function () {
        const scenario = byName("drag-esc-after-focus-blur");
        const afterEsc = scenario.steps[2].state;
        return noConnectionState(afterEsc) && !messageOf(afterEsc)
          && edgeCount(scenario.steps[3].state) === edgeCount(scenario.before);
      })(),
    idle_esc_clears_selection_without_connection:
      step("esc-on-idle-canvas", 0).selectedNodeIds.length === 1
        && lastStep("esc-on-idle-canvas").selectedNodeIds.length === 0
        && noConnectionState(lastStep("esc-on-idle-canvas")),
    release_on_source_handle_shows_direction_reason:
      count("drag-to-source-handle").after === count("drag-to-source-handle").before
        && /输出端口/.test(String(messageOf(lastStep("drag-to-source-handle")))),
    occupied_input_shows_specific_reason:
      /一条/.test(String(messageOf(step("input-occupied-target-message", 1))))
        && /一条/.test(String(messageOf(lastStep("input-occupied-target-message"))))
        && count("input-occupied-target-message").after
          === count("input-occupied-target-message").before + 1,
    admission_failure_uses_hint_not_persistent_error:
      (function () {
        const scenario = byName("admission-failure-on-release-uses-hint");
        const afterRelease = scenario.steps[2].state;
        return !!messageOf(afterRelease) && !afterRelease.editFailure
          && !hasEdge(afterRelease, fixtureNodes.A, fixtureNodes.B)
          && edgeCount(afterRelease) === edgeCount(scenario.before) + 1;
      })(),
    target_handle_start_reverse_drag_creates_edge:
      count("drag-target-to-source-handle").after === count("drag-target-to-source-handle").before + 1
        && hasEdge(lastStep("drag-target-to-source-handle"), fixtureNodes.A, fixtureNodes.B),
    locked_drag_starts_no_connection_and_creates_no_edge:
      (function () {
        const scenario = byName("locked-drag-handle");
        const during = scenario.steps[1].state;
        return noConnectionState(during)
          && edgeCount(scenario.steps[2].state) === edgeCount(scenario.before);
      })(),
    workflow_switch_clears_connection_state_and_hint:
      (function () {
        const state = step("switch-workflow-clears-connection-state", 1);
        return !!state && noConnectionState(state) && !messageOf(state);
      })(),
    drag_works_on_switched_workflow:
      edgeCount(step("switch-workflow-clears-connection-state", 2)) === 1,
    first_workflow_document_unchanged_after_switch_round_trip:
      (function () {
        const before = step("switch-workflow-clears-connection-state", 0);
        const after = step("switch-workflow-clears-connection-state", 3);
        return after.document.workflow_definition_id === before.document.workflow_definition_id
          && JSON.stringify(after.document) === JSON.stringify(before.document);
      })(),
    mid_gesture_switch_creates_no_edge_on_new_workflow:
      edgeCount(step("mid-gesture-switch-creates-no-edge", 2)) === 0,
    mid_gesture_switch_leaves_old_document_unchanged:
      (function () {
        const scenario = byName("mid-gesture-switch-creates-no-edge");
        const back = scenario.steps[3].state;
        return !hasEdge(back, fixtureNodes.A, fixtureNodes.B)
          && JSON.stringify(back.document) === JSON.stringify(scenario.steps[0].state.document);
      })(),
    save_failure_uses_persistent_feedback_not_connection_hint:
      (function () {
        const scenario = byName("save-failure-uses-persistent-feedback");
        const after = scenario.steps[2].state;
        return /fixture save failure/.test(String(after.editFailure))
          && !messageOf(after) && edgeCount(after) === edgeCount(scenario.before);
      })(),
    reconnect_after_save_failure_creates_edge:
      (function () {
        const scenario = byName("save-failure-uses-persistent-feedback");
        return edgeCount(scenario.steps[4].state) === edgeCount(scenario.steps[2].state) + 1;
      })(),
  };

  // -------------------------------------------------------------------------
  // Q04-3/Q04-4 selection expectations (S2-03): every fact below must hold.
  // -------------------------------------------------------------------------
  const observe = compute => { try { return compute(); } catch (error) { return { error: String(error) }; } };
  const selectedIds = state => state.selectedNodeIds;
  const scenarioSteps = name => byName(name)?.steps?.map(row => row.state) ?? [];
  report.selection_expectations = {
    node_click_selects_only_that_node:
      (function () {
        const state = step("selection-click-node", 0);
        return selectedIds(state).length === 1 && state.selectedEdgeId === null;
      })(),
    edge_click_selects_only_that_edge:
      (function () {
        const state = step("selection-click-edge", 0);
        return state.selectedEdgeId !== null && selectedIds(state).length === 0;
      })(),
    node_click_after_edge_clears_edge_selection:
      (function () {
        const scenario = byName("selection-node-click-clears-edge");
        const edge = scenario.steps[0].state, node = scenario.steps[1].state;
        return edge.selectedEdgeId !== null && node.selectedEdgeId === null && selectedIds(node).length === 1;
      })(),
    edge_click_after_node_clears_node_selection:
      (function () {
        const scenario = byName("selection-edge-click-clears-nodes");
        const node = scenario.steps[0].state, edge = scenario.steps[1].state;
        return selectedIds(node).length === 1 && edge.selectedEdgeId !== null && selectedIds(edge).length === 0;
      })(),
    ctrl_click_adds_node_to_selection:
      (function () {
        const state = step("selection-ctrl-click-multi-nodes", 1);
        return selectedIds(state).length === 2
          && selectedIds(state).includes(fixtureNodes.A) && selectedIds(state).includes(fixtureNodes.C)
          && state.selectedEdgeId === null;
      })(),
    ctrl_click_selected_node_removes_it:
      (function () {
        const state = step("selection-ctrl-click-multi-nodes", 2);
        return selectedIds(state).length === 1 && selectedIds(state).includes(fixtureNodes.C)
          && state.selectedEdgeId === null;
      })(),
    box_selection_collects_multiple_nodes_and_clears_edge:
      (function () {
        const scenario = byName("selection-box-multi-nodes");
        const edge = scenario.steps[0].state, box = scenario.steps[2].state;
        return edge.selectedEdgeId !== null && box.selectedEdgeId === null
          && selectedIds(box).length === 2
          && selectedIds(box).includes(fixtureNodes.A) && selectedIds(box).includes(fixtureNodes.C);
      })(),
    esc_clears_selection:
      (function () {
        const scenario = byName("selection-esc-clears");
        return selectedIds(scenario.steps[0].state).length === 1
          && selectedIds(scenario.steps[1].state).length === 0
          && lastStep("selection-esc-clears").selectedEdgeId === null;
      })(),
    pane_click_clears_selection:
      (function () {
        const scenario = byName("selection-pane-click-clears");
        return scenario.steps[0].state.selectedEdgeId !== null
          && lastStep("selection-pane-click-clears").selectedEdgeId === null
          && selectedIds(lastStep("selection-pane-click-clears")).length === 0;
      })(),
    connection_drag_release_keeps_existing_selection:
      (function () {
        const scenario = byName("selection-drag-release-keeps-selection");
        const before = scenario.steps[0].state, after = scenario.steps[1].state;
        return selectedIds(before).length === 1 && selectedIds(after).join() === selectedIds(before).join()
          && after.selectedEdgeId === null
          && edgeCount(after) === edgeCount(scenario.before) + 1;
      })(),
    add_node_clears_edge_selection:
      (function () {
        const scenario = byName("edge-then-add-node-then-delete");
        const edge = scenario.steps[0].state, added = scenario.steps[1].state;
        return edge.selectedEdgeId !== null && added.selectedEdgeId === null
          && selectedIds(added).length === 1
          && edgeCount(added) === edgeCount(scenario.before);
      })(),
    delete_after_add_removes_only_the_new_node:
      (function () {
        const scenario = byName("edge-then-add-node-then-delete");
        const after = lastStep("edge-then-add-node-then-delete"), before = scenario.before;
        return after.document.nodes.length === before.document.nodes.length
          && edgeCount(after) === edgeCount(before)
          && selectedIds(after).length === 0 && after.selectedEdgeId === null;
      })(),
    locate_clears_edge_selection:
      (function () {
        const scenario = byName("edge-then-locate-then-delete");
        const edge = scenario.steps[0].state, located = scenario.steps[1].state;
        return edge.selectedEdgeId !== null && located.selectedEdgeId === null
          && selectedIds(located).length === 1
          && selectedIds(located).includes(fixtureNodes.D);
      })(),
    delete_after_locate_removes_only_the_located_node:
      (function () {
        const scenario = byName("edge-then-locate-then-delete");
        const after = lastStep("edge-then-locate-then-delete"), before = scenario.before;
        return after.document.nodes.length === before.document.nodes.length - 1
          && edgeCount(after) === edgeCount(before)
          && selectedIds(after).length === 0;
      })(),
    undo_prunes_selection_of_removed_node:
      (function () {
        const state = step("undo-after-add-node", 1);
        if (!state) return null;
        const existing = new Set(state.document.nodes.map(node => node.id));
        return selectedIds(state).every(id => existing.has(id)) && selectedIds(state).length === 0;
      })(),
    delete_after_undo_is_a_no_change:
      (function () {
        const scenario = byName("undo-after-add-node");
        const undone = scenario.steps[1].state, deleted = scenario.steps[2].state;
        return JSON.stringify(undone.document) === JSON.stringify(deleted.document)
          && selectedIds(deleted).length === 0;
      })(),
    delete_node_clears_selection:
      (function () {
        const scenario = byName("delete-node-clears-selection");
        const after = lastStep("delete-node-clears-selection");
        return after.document.nodes.length === scenario.before.document.nodes.length - 1
          && selectedIds(after).length === 0 && after.selectedEdgeId === null;
      })(),
    delete_edge_clears_selection:
      (function () {
        const scenario = byName("delete-edge-clears-selection");
        const after = lastStep("delete-edge-clears-selection");
        return edgeCount(after) === edgeCount(scenario.before) - 1
          && after.document.nodes.length === scenario.before.document.nodes.length
          && after.selectedEdgeId === null;
      })(),
    delete_in_editor_input_does_not_delete_node:
      (function () {
        const scenario = byName("delete-in-editor-input-keeps-node");
        const after = lastStep("delete-in-editor-input-keeps-node");
        return after.document.nodes.length === scenario.before.document.nodes.length
          && selectedIds(after).join() === selectedIds(scenario.steps[0].state).join();
      })(),
    trash_button_deletes_selected_node_and_clears_selection:
      (function () {
        const scenario = byName("trash-button-deletes-selection");
        const after = lastStep("trash-button-deletes-selection");
        return after.document.nodes.length === scenario.before.document.nodes.length - 1
          && selectedIds(after).length === 0 && after.selectedEdgeId === null;
      })(),
    locked_delete_keeps_selection_and_persistent_failure:
      (function () {
        const scenario = byName("locked-delete");
        const selected = scenario.steps[0].state, after = scenario.steps[2].state;
        return !!after.editFailure && selectedIds(after).join() === selectedIds(selected).join()
          && after.document.nodes.length === scenario.before.document.nodes.length;
      })(),
    switch_workflow_clears_selection_and_connection_state:
      (function () {
        const scenario = byName("switch-workflow-clears-selection");
        const after = scenario.steps[1].state, back = scenario.steps[2].state;
        return selectedIds(scenario.steps[0].state).length === 1
          && selectedIds(after).length === 0 && after.selectedEdgeId === null
          && noConnectionState(after) && !messageOf(after)
          && selectedIds(back).length === 0;
      })(),
    esc_closes_menu_before_clearing_selection:
      (function () {
        const scenario = byName("esc-closes-menu-before-clearing-selection");
        const selected = scenario.steps[0].state, opened = scenario.steps[1], afterFirst = scenario.steps[2];
        return selectedIds(selected).length === 1 && opened.menuOpen === true
          && afterFirst.menuOpen === false
          && selectedIds(afterFirst.state).join() === selectedIds(selected).join()
          && selectedIds(lastStep("esc-closes-menu-before-clearing-selection")).length === 0;
      })(),
    handle_click_keeps_no_edge_selection_or_connection_state:
      (function () {
        const state = step("handle-click-selection-observation", 0);
        return noConnectionState(state) && state.selectedEdgeId === null;
      })(),
    selection_is_always_mutually_exclusive: observe(() =>
      report.scenarios.every(row => row.steps.every(row2 =>
        !(row2.state.selectedNodeIds.length && row2.state.selectedEdgeId !== null)))),
  };

  report.fixture_errors.push(...await page.evaluate(() =>
    window.probe.errors.map(message => ({ scope: "fixture", message }))));
  report.unexpected_api_requests = await page.evaluate(() => window.probe.unexpectedApiRequests);
  report.ran_categories = [...categories];
  report.notes = {
    fixture_boundary: "Real GraphWorkbench.vue, real Pinia workflow/workspace stores and real Vue Flow 1.48.2 run in headless Chromium. The graph document is seeded in memory. No configuration application is installed, so every accepted edit uses the store's in-memory acceptance fallback. Product API, services, database and models are never contacted.",
    evidence_read_stub: "The only HTTP fixture answers workflow management request-evidence reads with an empty result because the store initialises those outboxes on first render; without it the editor stays locked on the unavailable product API.",
    click_connect_decision: "Q04-1 closes click-to-connect: the VueFlow component sets connect-on-click=false and the probe asserts the store's connectOnClick is false and no click pending handle ever appears.",
    cancel_mechanism: "GraphWorkbench.cancelConnection uses the public VueFlow store action endConnection(event, false) to clear visible state, then ends the library's private pointer-down closure by dispatching a mousemove/mouseup far from every handle, and rejects any late connect event for the cancelled gesture. The probe verifies with real pointer/keyboard ordering that Esc followed by a re-hover and release over a valid target creates no edge, and that the next drag works.",
    selection_integration: "S2-03 moves every component selection write onto the store actions selectNodes/selectEdge/clearSelection/pruneSelection. Node clicks collapse to the clicked node unless Ctrl/Meta/Shift is held (Vue Flow multi-select changes are merged in onNodesChange); edge clicks clear node selection; blank clicks clear both; box selection produces a multi-node selection and clears any edge; add/locate clear the edge selection; switching workflows clears selection, connection state and hints; the trash button and Delete share removeSelection, and an invalid stored identity is not displayed.",
    event_timing: "Pointer gestures are checked with real ordering: a connection drag (mousedown on a source handle, mouseup on a target handle) ends without a trailing pane/node click that would alter the existing selection; a plain node click writes exactly one node selection; an edge click after a node click ends with only the edge selected (the library's node-deselect changes are processed before the edge click emits); Esc after a pending connection cancels the connection and keeps the selection.",
    save_failure_coverage: "A real save failure needs the production configuration application (product API), which this isolated fixture must not call. The component path awaits store.connect() through applyEdit, so a failed outcome uses the step-1 persistent editFailure; the probe exercises the save-failure component branch through a fixture-level stub of the store save port, and the separate admission-failure hint through a simulated concurrent edit before release.",
    switch_late_results: "The fixture switches workflows through workspace.openWorkflow without product reads. Store pruneSelection(workflowId) is a no-op for another workflow and the component keeps its navigation generation/current() token guards, so a late result cannot clear the new workflow's selection; the probe itself only covers the synchronous switch path.",
  };
  const scenarioErrors = report.scenarios.filter(row => row.error);
  if (scenarioErrors.length)
    report.scenario_errors = scenarioErrors.map(row => ({ name: row.name, category: row.category, error: row.error }));
  await fs.writeFile(path.join(evidence, "graph-interaction-report.json"),
    JSON.stringify(report, null, 2));
  if (report.errors.length || report.unexpected_requests.length || scenarioErrors.length) {
    console.error("graph interaction probe finished with recorded problems: "
      + JSON.stringify({ page_errors: report.errors.length,
        unexpected_requests: report.unexpected_requests, scenario_errors: report.scenario_errors ?? [] }, null, 2));
    process.exitCode = 1;
  } else {
    console.log("graph interaction probe completed: " + report.scenarios.length + " scenarios ("
      + Object.keys(report.reproductions).length + " connection facts, "
      + Object.keys(report.selection_expectations).length + " selection facts)");
  }

} catch (error) {
  report.fatal = String(error && error.stack || error);
  await fs.writeFile(path.join(evidence, "graph-interaction-report.json"),
    JSON.stringify(report, null, 2)).catch(() => {});
  throw error;
} finally {
  await browser?.close();
  await server.close();
}
