// Independent verifier acceptance for step 2 (V2-01..V2-04) on a real browser.
//
// This file is verifier-owned and independent from backend/tests/graph_interaction_probe.mjs.
// It starts only a test-scoped Vite dev server and a headless Chromium from the
// pre-installed runtime (TAVERN_CHROMIUM_EXECUTABLE). The product API, product services,
// product database and model calls are never contacted: every non-fixture request is
// aborted, and the only HTTP fixture answers workflow request-evidence reads with an
// empty result so the store's per-workflow outboxes do not keep the editor locked.
//
// The scenario scripts only RECORD state snapshots; all acceptance assertions live in
// backend/tests/test_graph_interaction_acceptance.py so this harness stays a recorder.
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

async function gitHead() {
  const child = await import("node:child_process");
  return new Promise(resolve => child.execFile("git", ["rev-parse", "HEAD"], { cwd: root },
    (error, stdout) => resolve(error ? null : stdout.trim())));
}
async function sha256(file) {
  const value = await fs.readFile(file);
  const { createHash } = await import("node:crypto");
  return createHash("sha256").update(value).digest("hex");
}

// ---------------------------------------------------------------------------
// In-browser fixture: real GraphWorkbench.vue, real pinia stores, real Vue Flow,
// in-memory documents only.
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

const acc = window.acc = { ready: false, wb: null, errors: [], badApi: [] };

// Only the management request-evidence reads are answered locally; every other /api
// request is recorded and rejected.
const realFetch = window.fetch ? window.fetch.bind(window) : null;
window.fetch = function (input, init) {
  const url = String(input);
  if (url === "/api/graph/queries") {
    let operation = "";
    try { operation = JSON.parse(String(init && init.body) || "{}").operation || ""; } catch (error) { operation = ""; }
    if (operation === "request-evidence.read")
      return Promise.resolve(new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }));
    if (operation === "request-evidence.pending")
      return Promise.resolve(new Response(JSON.stringify({ schema_version: 1,
        kind: "workflow.request-evidence-page", items: [], next_cursor: null }), { status: 200,
        headers: { "Content-Type": "application/json" } }));
    acc.badApi.push(url + ":" + operation);
    return Promise.reject(new Error("unexpected graph query in interaction acceptance"));
  }
  if (url.startsWith("/api/")) {
    acc.badApi.push(url);
    return Promise.reject(new Error("product API must not be called by the interaction acceptance"));
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
const incompatibleType = TYPE("test.incompatible", "1", "Incompatible", [PORT("prompt", "PROMPT", 2)], []);
const addableType = TYPE("tools.text", "1", "Text", [], []);
const catalog = [sourceType, sinkType, incompatibleType, addableType];

function nodeFor(type, x, y) {
  return { node_binding_id: crypto.randomUUID(), component_id: type.component_id,
    component_version: type.component_version, title: type.display_name,
    position: { x: x, y: y }, config: {} };
}
function edgeFor(source, target, order) {
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

const firstNodes = { A: nodeFor(sourceType, 60, 80), B: nodeFor(sinkType, 560, 80),
  C: nodeFor(sourceType, 60, 360), E: nodeFor(sinkType, 560, 620),
  D: nodeFor(incompatibleType, 560, 360) };
const selectedEdge = edgeFor(firstNodes.C, firstNodes.E, 0);
const firstDoc = documentFor(first, "acceptance first",
  [firstNodes.A, firstNodes.B, firstNodes.C, firstNodes.D, firstNodes.E], [selectedEdge]);
const secondNodes = { A: nodeFor(sourceType, 60, 80), B: nodeFor(sinkType, 560, 80) };
const secondDoc = documentFor(second, "acceptance second", [secondNodes.A, secondNodes.B], []);
graph.catalog = catalog;

acc.first = first;
acc.second = second;
acc.nodes = { A: firstNodes.A.node_binding_id, B: firstNodes.B.node_binding_id,
  C: firstNodes.C.node_binding_id, D: firstNodes.D.node_binding_id, E: firstNodes.E.node_binding_id };
acc.secondNodes = { A: secondNodes.A.node_binding_id, B: secondNodes.B.node_binding_id };
acc.edgeSel = selectedEdge.edge_id;
acc.firstDoc = graphClone(firstDoc);
acc.secondDoc = graphClone(secondDoc);

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

function valueOf(value) {
  return value && typeof value === "object" && value.__v_isRef ? value.value : value;
}
function handleState(handle) {
  if (!handle) return null;
  return { nodeId: handle.nodeId ?? null, id: handle.id ?? null, type: handle.type ?? null };
}
function nodeIdFor(key) {
  const table = workspace.activeWorkflowId === second ? acc.secondNodes : acc.nodes;
  return table[key];
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
    }) };
}
function workbenchState() {
  const wb = acc.wb;
  const state = wb && wb.$ && wb.$.setupState ? wb.$.setupState : null;
  const flow = state ? valueOf(state.flow) : null;
  const message = document.querySelector(".graph-connection-message");
  return {
    activeWorkflowId: workspace.activeWorkflowId,
    hasWorkbench: !!wb,
    hasFlow: !!flow,
    connectOnClick: flow ? valueOf(flow.connectOnClick) : null,
    connecting: state ? valueOf(state.connecting) : null,
    connectionMessage: state ? valueOf(state.connectionMessage) : null,
    editFailure: state ? valueOf(state.editFailure) : null,
    selectedNodeIds: graph.selectedNodeIds.slice(),
    selectedEdgeId: graph.selectedEdgeId,
    editorLocked: graph.editorLocked,
    notices: notices.notices.map(function (row) { return { title: row.title, kind: row.kind, reason: row.reason }; }),
    flow: flow ? { connectionStartHandle: handleState(valueOf(flow.connectionStartHandle)),
      connectionEndHandle: handleState(valueOf(flow.connectionEndHandle)),
      connectionClickStartHandle: handleState(valueOf(flow.connectionClickStartHandle)),
      connectionStatus: valueOf(flow.connectionStatus) } : null,
    dom: { connectionMessage: message ? message.textContent : null,
      warningTexts: Array.prototype.map.call(document.querySelectorAll(".graph-warning"),
        function (el) { return el.textContent; }),
      connectingHandles: Array.prototype.map.call(document.querySelectorAll(".vue-flow__handle.connecting"),
        function (el) { return el.getAttribute("data-nodeid") + "/" + el.getAttribute("data-handleid"); }),
      connectionPath: !!document.querySelector(".vue-flow__connection-path"),
      selectedNodesDom: Array.prototype.map.call(document.querySelectorAll(".vue-flow__node.selected"),
        function (el) { return el.getAttribute("data-id"); }),
      selectedEdgesDom: Array.prototype.map.call(document.querySelectorAll(".vue-flow__edge.selected"),
        function (el) { return el.getAttribute("data-id"); }),
      addMenuOpen: !!document.querySelector("[data-canvas-node-menu]"),
      trashButtons: document.querySelectorAll('button[aria-label="删除选中"]').length,
      focused: document.activeElement ? (document.activeElement.getAttribute("aria-label")
        || document.activeElement.className || document.activeElement.tagName) : null },
    document: documentState(),
  };
}
acc.snapshot = workbenchState;

function settle() {
  return new Promise(function (resolve) {
    requestAnimationFrame(function () { requestAnimationFrame(function () { requestAnimationFrame(resolve); }); });
  });
}
acc.settle = settle;
acc.waitUnlocked = async function () {
  for (let attempt = 0; attempt < 400; attempt++) {
    if (!graph.editorLocked) return true;
    await nextTick();
    await new Promise(function (resolve) { requestAnimationFrame(resolve); });
  }
  return false;
};
acc.reset = async function () {
  // A workflow switch remounts VueFlow (the product lifecycle) and drops pending
  // connection/hint/menu state so every scenario starts known-clean.
  workspace.openWorkflow(second);
  await nextTick();
  workspace.openWorkflow(first);
  await nextTick();
  graph.busy = null;
  graph.applicationBusy = false;
  if (acc.restoreConnect) acc.restoreConnect();
  seed(first, acc.firstDoc);
  seed(second, acc.secondDoc);
  graph.selectedNodeIds = [];
  graph.selectedEdgeId = null;
  notices.notices.splice(0, notices.notices.length);
  if (acc.wb && acc.wb.$.setupState) acc.wb.$.setupState.editFailure = "";
  await acc.waitUnlocked();
  await nextTick();
  await settle();
  return workbenchState();
};
acc.switchTo = async function (workflowId) {
  workspace.openWorkflow(workflowId);
  await nextTick();
  await acc.waitUnlocked();
  await settle();
  return workbenchState();
};
acc.rect = function (selector) {
  const element = document.querySelector(selector);
  if (!element) return null;
  const box = element.getBoundingClientRect();
  return { x: box.x, y: box.y, width: box.width, height: box.height,
    cx: box.x + box.width / 2, cy: box.y + box.height / 2 };
};
acc.handleRect = function (key, direction, handleId) {
  const nodeId = nodeIdFor(key);
  if (!nodeId) return null;
  return acc.rect('.vue-flow__node[data-id="' + nodeId + '"] ' +
    (direction === "target" ? ".target" : ".source") + '[data-handleid="' + handleId + '"]');
};
acc.nodeRect = function (key) {
  const nodeId = nodeIdFor(key);
  return nodeId ? acc.rect('.vue-flow__node[data-id="' + nodeId + '"]') : null;
};
acc.edgePoint = function (edgeId) {
  const edge = document.querySelector('.vue-flow__edge[data-id="' + edgeId + '"]');
  if (!edge) return null;
  const trace = edge.querySelector(".vue-flow__edge-interaction") || edge.querySelector(".vue-flow__edge-path");
  if (!trace || typeof trace.getTotalLength !== "function") return null;
  const local = trace.getPointAtLength(trace.getTotalLength() / 2);
  const point = new DOMPoint(local.x, local.y).matrixTransform(trace.getScreenCTM());
  return { x: point.x, y: point.y };
};
acc.canvasRect = function () { return acc.rect(".graph-canvas"); };
acc.setBusy = async function (value) {
  graph.busy = value;
  await nextTick();
  await settle();
};
acc.setDiagnostics = async function (nodeKey) {
  graph.entries[first].diagnostics = [{ reason_code: "fixture.state", message: "fixture diagnostic",
    node_id: nodeIdFor(nodeKey), dependent_outputs: [], phase: "edit" }];
  await nextTick();
  await settle();
};
acc.injectEdge = async function (sourceKey, targetKey) {
  graph.entries[first].document.edges.push({ edge_id: crypto.randomUUID(),
    source_node_id: acc.nodes[sourceKey], source_port_id: "text",
    target_node_id: acc.nodes[targetKey], target_port_id: "text", order: 1 });
  await nextTick();
  await settle();
};
acc.staleSelection = async function () {
  graph.selectedNodeIds = [crypto.randomUUID()];
  graph.selectedEdgeId = crypto.randomUUID();
  await nextTick();
  await settle();
};
// The fixture stubs the store save port to observe the component's real failure branch
// (persistent step-1 editFailure) separately from admission hints.
const originalConnect = graph.connect;
acc.stubConnectFailure = async function (message) {
  graph.connect = async function () { return { outcome: "failed", error: new Error(message) }; };
  await nextTick();
  await settle();
};
acc.restoreConnect = function () { graph.connect = originalConnect; };
acc.openAddMenu = async function () {
  const button = document.querySelector('.graph-toolbar button[aria-label="添加节点"]');
  if (!button) return false;
  button.click();
  await nextTick();
  await settle();
  return !!document.querySelector("[data-canvas-node-menu]");
};
acc.chooseAddNode = async function () {
  const group = document.querySelector('[data-canvas-node-menu] [data-group-id="basic"]');
  if (!group) return { ok: false, reason: "group-missing" };
  group.click();
  await nextTick();
  await settle();
  const items = Array.prototype.slice.call(document.querySelectorAll("[data-canvas-node-menu] button"));
  const target = items.find(function (button) { return button.textContent.trim() === "文本"; });
  if (!target) return { ok: false, reason: "item-missing" };
  target.click();
  await settle();
  return { ok: true };
};

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
  if (name === "GraphWorkbench") acc.wb = this;
} });
app.config.errorHandler = function (error) {
  acc.errors.push(String(error && error.stack || error));
  console.error(error);
};
window.addEventListener("unhandledrejection", function (event) {
  acc.errors.push("unhandledrejection: " + String(event.reason && event.reason.stack || event.reason));
});
app.mount("#app");
acc.ready = true;
`;

const server = await createServer({
  root: path.join(root, "frontend"), configFile: false, logLevel: "error",
  plugins: [vue(), {
    name: "isolated-graph-interaction-acceptance",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (request.url === "/") {
          response.setHeader("Content-Type", "text/html");
          response.end('<html><head><style>html,body,#app{width:1600px;height:1000px;margin:0}'
            + '.graph-workbench{width:1600px;height:1000px}</style></head>'
            + '<body><div id="app"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        } else if (request.url === "/fixture-entry.js") {
          server.transformRequest("/virtual-interaction-acceptance.js").then(result => {
            response.setHeader("Content-Type", "text/javascript");
            response.end(result.code);
          }).catch(next);
        } else next();
      });
    },
    resolveId(id) { if (id === "/virtual-interaction-acceptance.js") return "\0graph-interaction-acceptance"; },
    load(id) { if (id === "\0graph-interaction-acceptance") return fixture; },
  }],
  server: { host: "127.0.0.1", port: 0, strictPort: false },
});

const report = {
  fatal: null,
  errors: [],
  unexpected_requests: [],
  unexpected_api_requests: [],
  fixture_errors: [],
  baseline: { head: null, files: {} },
  scenarios: [],
  screenshots: [],
};
let browser = null;

try {
  await server.listen();
  const port = server.httpServer.address().port;
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  report.baseline.head = await gitHead();
  report.baseline.files = {
    "frontend/src/components/GraphWorkbench.vue":
      await sha256(path.join(root, "frontend/src/components/GraphWorkbench.vue")),
    "frontend/src/stores/workflowGraph.ts":
      await sha256(path.join(root, "frontend/src/stores/workflowGraph.ts")),
    "frontend/src/domain/graphConnections.ts":
      await sha256(path.join(root, "frontend/src/domain/graphConnections.ts")),
  };

  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.on("pageerror", error => report.errors.push(String(error)));
  page.on("console", message => {
    if (message.type() === "error") report.fixture_errors.push({ scope: "console", message: message.text() });
  });
  page.on("requestfailed", request => {
    if (!request.url().startsWith(`http://127.0.0.1:${port}/`))
      report.fixture_errors.push({ scope: "requestfailed", url: request.url() });
  });
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" || url.port !== String(port) || url.pathname.startsWith("/api/")) {
      report.unexpected_requests.push(url.pathname);
      await route.abort();
    } else await route.continue();
  });
  await page.goto(`http://127.0.0.1:${port}/`);
  await page.waitForFunction(() => window.acc && window.acc.ready, null, { timeout: 30000 });
  await page.evaluate(() => window.acc.reset());

  const settle = async () => page.evaluate(() => window.acc.settle());
  const snapshot = async () => page.evaluate(() => window.acc.snapshot());
  const reset = async () => { await page.evaluate(() => window.acc.reset()); await settle(); };
  const screenshot = async name => {
    const file = path.join(evidence, name + ".png");
    await page.screenshot({ path: file });
    report.screenshots.push(name + ".png");
  };
  const handleRect = (key, direction, handleId) =>
    page.evaluate(([k, d, h]) => window.acc.handleRect(k, d, h), [key, direction, handleId]);
  const nodeRect = key => page.evaluate(k => window.acc.nodeRect(k), key);
  const edgePoint = edgeId => page.evaluate(id => window.acc.edgePoint(id), edgeId);
  const canvasRect = () => page.evaluate(() => window.acc.canvasRect());
  const focusWorkbench = () => page.locator(".graph-workbench").focus();
  const pressDelete = async () => { await focusWorkbench(); await page.keyboard.press("Delete"); await settle(); };
  const pressEscape = async () => { await page.keyboard.press("Escape"); await settle(); };
  const clickHandle = async (key, direction, handleId) => {
    const rect = await handleRect(key, direction, handleId);
    await page.mouse.click(rect.cx, rect.cy);
    await settle();
  };
  const startDrag = async (key, direction, handleId) => {
    const rect = await handleRect(key, direction, handleId);
    await page.mouse.move(rect.cx, rect.cy);
    await page.mouse.down();
    await page.mouse.move(rect.cx + 30, rect.cy + 30, { steps: 6 });
    await settle();
  };
  const moveToHandle = async (key, direction, handleId) => {
    const rect = await handleRect(key, direction, handleId);
    await page.mouse.move(rect.cx, rect.cy, { steps: 12 });
    await settle();
  };
  const moveToBlank = async () => {
    const canvas = await canvasRect();
    await page.mouse.move(canvas.x + 100, canvas.y + canvas.height - 120, { steps: 12 });
    await settle();
  };
  const release = async () => { await page.mouse.up(); await settle(); };
  const dragTo = async (fromKey, fromHandle, toKey, toHandle) => {
    await startDrag(fromKey, "source", fromHandle);
    await moveToHandle(toKey, "target", toHandle);
    await release();
  };
  const clickNode = async key => {
    const rect = await nodeRect(key);
    await page.mouse.click(rect.cx, rect.cy);
    await settle();
  };
  const clickEdge = async () => {
    const point = await edgePoint(await page.evaluate(() => window.acc.edgeSel));
    await page.mouse.click(point.x, point.y);
    await settle();
  };
  const clickBlank = async () => {
    const canvas = await canvasRect();
    await page.mouse.click(canvas.x + 100, canvas.y + canvas.height - 120);
    await settle();
  };

  async function scenario(name, body) {
    await reset();
    const row = { name, steps: [], before: await snapshot() };
    try { await body(row); } catch (error) { row.error = String(error && error.stack || error); }
    row.after = await snapshot();
    report.scenarios.push(row);
    if (row.error) report.fatal = report.fatal || ("scenario " + name + ": " + row.error);
    return row;
  }

  // ---------------- V2-01 / V2-02 / V2-03 connection lifecycle ----------------
  await scenario("click-handle-closed", async row => {
    await clickHandle("A", "source", "text");
    row.steps.push({ step: "after-click-source", state: await snapshot() });
    await clickHandle("B", "target", "text");
    row.steps.push({ step: "after-click-target", state: await snapshot() });
    await clickBlank();
    row.steps.push({ step: "after-blank", state: await snapshot() });
  });

  await scenario("drag-valid-edge", async row => {
    await dragTo("A", "text", "B", "text");
    row.steps.push({ step: "after-release", state: await snapshot() });
    await screenshot("v2-01-drag-valid");
  });

  await scenario("drag-esc-release-valid", async row => {
    await startDrag("A", "source", "text");
    await moveToHandle("B", "target", "text");
    row.steps.push({ step: "before-esc", state: await snapshot() });
    await pressEscape();
    row.steps.push({ step: "after-esc", state: await snapshot() });
    await moveToHandle("B", "target", "text");
    await release();
    row.steps.push({ step: "after-release-on-valid", state: await snapshot() });
    await dragTo("A", "text", "B", "text");
    row.steps.push({ step: "after-next-drag", state: await snapshot() });
    await screenshot("v2-02-esc-then-valid");
  });

  await scenario("drag-esc-release-blank", async row => {
    await startDrag("A", "source", "text");
    await pressEscape();
    row.steps.push({ step: "after-esc", state: await snapshot() });
    await moveToBlank();
    await release();
    row.steps.push({ step: "after-release-blank", state: await snapshot() });
  });

  await scenario("drag-release-blank", async row => {
    await startDrag("A", "source", "text");
    await moveToBlank();
    await release();
    row.steps.push({ step: "after-release", state: await snapshot() });
    await clickBlank();
    row.steps.push({ step: "after-blank-click", state: await snapshot() });
  });

  await scenario("drag-incompatible-target", async row => {
    await dragTo("A", "text", "D", "prompt");
    row.steps.push({ step: "after-incompatible-release", state: await snapshot() });
    await startDrag("A", "source", "text");
    row.steps.push({ step: "during-next-drag", state: await snapshot() });
    await moveToBlank();
    await release();
    row.steps.push({ step: "after-next-blank-release", state: await snapshot() });
  });

  await scenario("hint-lifecycle", async row => {
    await dragTo("A", "text", "D", "prompt");
    row.steps.push({ step: "after-incompatible-release", state: await snapshot() });
    await page.locator('button[aria-label="关闭连线提示"]').click();
    await settle();
    row.steps.push({ step: "after-dismiss", state: await snapshot() });
    await dragTo("A", "text", "D", "prompt");
    row.steps.push({ step: "after-second-incompatible", state: await snapshot() });
    await clickBlank();
    row.steps.push({ step: "after-blank", state: await snapshot() });
  });

  await scenario("switch-workflow-mid-gesture", async row => {
    await startDrag("A", "source", "text");
    await page.evaluate(() => window.acc.switchTo(window.acc.second));
    row.steps.push({ step: "after-switch", state: await snapshot() });
    await moveToBlank();
    await release();
    row.steps.push({ step: "after-release", state: await snapshot() });
    await dragTo("A", "text", "B", "text");
    row.steps.push({ step: "after-drag-on-second", state: await snapshot() });
    await page.evaluate(() => window.acc.switchTo(window.acc.first));
    row.steps.push({ step: "after-switch-back", state: await snapshot() });
  });

  await scenario("save-failure-persistent", async row => {
    await page.evaluate(() => window.acc.stubConnectFailure("fixture save failure"));
    await dragTo("A", "text", "B", "text");
    row.steps.push({ step: "after-failed-save", state: await snapshot() });
    await page.evaluate(() => window.acc.restoreConnect());
    await dragTo("A", "text", "B", "text");
    row.steps.push({ step: "after-successful-save", state: await snapshot() });
  });

  await scenario("admission-failure-hint", async row => {
    await page.evaluate(() => window.acc.injectEdge("A", "B"));
    row.steps.push({ step: "after-injected-edge", state: await snapshot() });
    await dragTo("A", "text", "B", "text");
    row.steps.push({ step: "after-admission-release", state: await snapshot() });
    await clickBlank();
    row.steps.push({ step: "after-blank", state: await snapshot() });
  });

  // ---------------- V2-04 selection / deletion integration ----------------
  await scenario("selection-mutual-exclusive", async row => {
    await clickNode("A");
    row.steps.push({ step: "after-node-click", state: await snapshot() });
    await clickEdge();
    row.steps.push({ step: "after-edge-click", state: await snapshot() });
    await clickNode("C");
    row.steps.push({ step: "after-node-click-2", state: await snapshot() });
  });

  await scenario("ctrl-multi-selection", async row => {
    await clickNode("A");
    row.steps.push({ step: "after-first-node", state: await snapshot() });
    const second = await nodeRect("C");
    await page.keyboard.down("Control");
    await page.mouse.click(second.cx, second.cy);
    await page.keyboard.up("Control");
    await settle();
    row.steps.push({ step: "after-ctrl-second", state: await snapshot() });
    await clickEdge();
    row.steps.push({ step: "after-edge-click", state: await snapshot() });
  });

  await scenario("edge-add-node-then-delete", async row => {
    await clickEdge();
    row.steps.push({ step: "after-edge-click", state: await snapshot() });
    const opened = await page.evaluate(() => window.acc.openAddMenu());
    assert.ok(opened, "add menu did not open");
    const chosen = await page.evaluate(() => window.acc.chooseAddNode());
    row.steps.push({ step: "after-add-node", state: await snapshot(), chosen });
    await pressDelete();
    row.steps.push({ step: "after-delete", state: await snapshot() });
    await screenshot("v2-04-edge-add-delete");
  });

  await scenario("edge-locate-then-delete", async row => {
    await page.evaluate(() => window.acc.setDiagnostics("D"));
    await clickEdge();
    row.steps.push({ step: "after-edge-click", state: await snapshot() });
    await page.locator(".graph-diagnostic").first().click();
    await settle();
    row.steps.push({ step: "after-locate", state: await snapshot() });
    await pressDelete();
    row.steps.push({ step: "after-delete", state: await snapshot() });
  });

  await scenario("trash-and-keyboard-same-target", async row => {
    await clickNode("A");
    row.steps.push({ step: "after-node-click", state: await snapshot() });
    await page.locator('button[aria-label="删除选中"]').first().click();
    await settle();
    row.steps.push({ step: "after-trash", state: await snapshot() });
    await clickNode("C");
    row.steps.push({ step: "after-node-click-2", state: await snapshot() });
    await pressDelete();
    row.steps.push({ step: "after-keyboard-delete", state: await snapshot() });
  });

  await scenario("stale-selection-no-change", async row => {
    await page.evaluate(() => window.acc.staleSelection());
    row.steps.push({ step: "after-stale-selection", state: await snapshot() });
    await pressDelete();
    row.steps.push({ step: "after-delete", state: await snapshot() });
  });

  await scenario("undo-prunes-selection", async row => {
    const opened = await page.evaluate(() => window.acc.openAddMenu());
    assert.ok(opened, "add menu did not open");
    const chosen = await page.evaluate(() => window.acc.chooseAddNode());
    row.steps.push({ step: "after-add-node", state: await snapshot(), chosen });
    await focusWorkbench();
    await page.keyboard.press("Control+z");
    await settle();
    row.steps.push({ step: "after-undo", state: await snapshot() });
    await pressDelete();
    row.steps.push({ step: "after-delete", state: await snapshot() });
  });

  await scenario("switch-workflow-clears-selection", async row => {
    await clickNode("A");
    row.steps.push({ step: "after-node-click", state: await snapshot() });
    await page.evaluate(() => window.acc.switchTo(window.acc.second));
    row.steps.push({ step: "after-switch", state: await snapshot() });
    await clickNode("A");
    row.steps.push({ step: "after-second-node-click", state: await snapshot() });
    await page.evaluate(() => window.acc.switchTo(window.acc.first));
    row.steps.push({ step: "after-switch-back", state: await snapshot() });
  });

  await scenario("locked-delete-keeps-selection", async row => {
    await clickNode("A");
    await page.evaluate(() => window.acc.setBusy("fixture-lock"));
    row.steps.push({ step: "after-lock", state: await snapshot() });
    await pressDelete();
    row.steps.push({ step: "after-delete-attempt", state: await snapshot() });
    await page.evaluate(() => window.acc.setBusy(null));
    row.steps.push({ step: "after-unlock", state: await snapshot() });
  });

  await scenario("esc-priority-menu-then-selection", async row => {
    await clickNode("A");
    row.steps.push({ step: "after-node-click", state: await snapshot() });
    await page.evaluate(() => window.acc.openAddMenu());
    row.steps.push({ step: "after-menu-open", state: await snapshot() });
    await pressEscape();
    row.steps.push({ step: "after-first-esc", state: await snapshot() });
    await pressEscape();
    row.steps.push({ step: "after-second-esc", state: await snapshot() });
  });

  report.fixture = await page.evaluate(() => ({
    nodes: window.acc.nodes, secondNodes: window.acc.secondNodes, edgeSel: window.acc.edgeSel,
    first: window.acc.first, second: window.acc.second }));
  report.fixture_errors.push(...await page.evaluate(() =>
    window.acc.errors.map(message => ({ scope: "fixture", message }))));
  report.unexpected_api_requests = await page.evaluate(() => window.acc.badApi);
} catch (error) {
  report.fatal = String(error && error.stack || error);
} finally {
  try { if (browser) await browser.close(); } catch (error) { report.errors.push("browser close: " + String(error)); }
  try { await server.close(); } catch (error) { report.errors.push("server close: " + String(error)); }
  await fs.writeFile(path.join(evidence, "acceptance-report.json"),
    JSON.stringify(report, null, 2), "utf-8");
}

if (report.fatal || report.errors.length) {
  console.error(JSON.stringify({ fatal: report.fatal, errors: report.errors }, null, 2));
  process.exit(1);
}
console.log(JSON.stringify({ scenarios: report.scenarios.map(row => row.name),
  screenshots: report.screenshots }, null, 2));
