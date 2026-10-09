"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const [base, evidence, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const dist = path.resolve(__dirname, "../../frontend/dist");
const workflowKey = "workflow-workbench:fixed-base:v1";
const preferenceKey = "workflow-workbench:sidebar-collapsed:v1";
const report = { scenarios: [], layouts: [], page_errors: [], console_errors: [], mutations: [] };
let browser, page;

async function ready() {
  await page.waitForFunction(key => {
    const saved = JSON.parse(localStorage.getItem(key));
    return saved?.graph.entries[saved.activeWorkflowId]
      && !document.querySelector('[aria-label="创建串行 Agent 示例"]').disabled;
  }, workflowKey);
}

async function workflow() {
  return page.evaluate(key => {
    const saved = JSON.parse(localStorage.getItem(key));
    return { active: saved.activeWorkflowId, selected: saved.selectedWorkflowId, graph: saved.graph };
  }, workflowKey);
}

async function layout(label, collapsed) {
  const geometry = await page.evaluate(() => {
    const box = selector => {
      const rect = document.querySelector(selector).getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, right: rect.right };
    };
    return {
      sidebar: box(".workbench-sidebar"), main: box(".workbench-main"), canvas: box(".graph-canvas"),
      rail: box(".workbench-navigation-rail"), width: innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      overflows: [...document.querySelectorAll(".workbench-navigation-rail button")].filter(button =>
        button.scrollWidth > button.clientWidth || button.scrollHeight > button.clientHeight)
        .map(button => button.getAttribute("aria-label")),
    };
  });
  assert.ok(geometry.sidebar.right <= geometry.main.x + 1, JSON.stringify(geometry));
  assert.ok(geometry.canvas.width > 0, JSON.stringify(geometry));
  assert.deepEqual(geometry.overflows, []);
  if (collapsed) assert.equal(geometry.sidebar.width, 44);
  if (geometry.width >= 1024) assert.ok(geometry.scrollWidth <= geometry.width + 1);
  const toggle = page.locator(".workbench-sidebar-toggle");
  assert.equal(await toggle.getAttribute("aria-expanded"), String(!collapsed));
  assert.equal(await toggle.getAttribute("aria-controls"), "workbench-sidebar-panel");
  assert.equal(await page.locator("#workbench-sidebar-panel").isVisible(), !collapsed);
  await page.screenshot({ path: path.join(evidence, label + ".png") });
  report.layouts.push({ label, collapsed, ...geometry });
  return geometry;
}

(async () => {
  fs.mkdirSync(evidence, { recursive: true });
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    assert.equal(url.origin, base);
    if (url.pathname === "/api/graph/queries") {
      const query = request.postDataJSON();
      if (query.operation === "catalog.node-types") return route.fulfill({ json: fixture });
      assert.ok(["resource.list", "definition.sessions"].includes(query.operation), JSON.stringify(query));
      return route.fulfill({ json: [] });
    }
    if (url.pathname.startsWith("/api/")) {
      report.mutations.push({ url: url.href, body: request.postData() });
      return route.abort();
    }
    const target = path.resolve(dist, "." + (url.pathname === "/" ? "/index.html" : decodeURIComponent(url.pathname)));
    assert.ok(target.startsWith(dist + path.sep), target);
    const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
      ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon" }[path.extname(target)];
    return route.fulfill({ body: fs.readFileSync(target), contentType: mime ?? "application/octet-stream" });
  });
  page = await context.newPage();
  page.on("pageerror", error => report.page_errors.push(String(error)));
  page.on("console", message => { if (message.type() === "error") report.console_errors.push(message.text()); });
  await page.goto(base + "/");
  await ready();
  await page.getByRole("button", { name: "创建串行 Agent 示例", exact: true }).click();
  await ready();
  const original = await workflow();
  assert.ok(original.graph.entries[original.active].document.nodes.length > 0);
  const expanded = await layout("desktop-expanded", false);
  const firstNode = page.locator(".vue-flow__node").first();
  await firstNode.click();
  const selected = await page.locator(".vue-flow__node.selected").getAttribute("data-id");
  const viewport = await page.locator(".vue-flow__viewport").getAttribute("style");
  await page.getByRole("button", { name: "收起左侧栏", exact: true }).click();
  const collapsed = await layout("desktop-collapsed", true);
  assert.ok(collapsed.canvas.width > expanded.canvas.width + 200);
  assert.equal(await page.locator(".vue-flow__node.selected").getAttribute("data-id"), selected);
  assert.equal(await page.locator(".vue-flow__viewport").getAttribute("style"), viewport);
  assert.deepEqual(await workflow(), original);
  report.scenarios.push("collapse grows the canvas without changing graph, selection or viewport");

  await page.getByRole("button", { name: "工作流", exact: true }).click();
  await layout("active-section-expanded", false);
  report.scenarios.push("clicking the already active navigation section reopens its panel");
  await page.getByRole("button", { name: "收起左侧栏", exact: true }).click();
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  await page.getByRole("button", { name: "新增供应商资源", exact: true }).click();
  const name = page.locator(".current-provider-panel").getByLabel("名称", { exact: true });
  await name.fill("Unsaved provider draft");
  await name.evaluate(element => { element.dataset.sidebarDraft = "same-form"; });
  await page.getByRole("button", { name: "收起左侧栏", exact: true }).click();
  await page.locator(".workbench-sidebar-toggle").focus();
  await page.keyboard.press("Enter");
  assert.equal(await name.inputValue(), "Unsaved provider draft");
  assert.equal(await name.getAttribute("data-sidebar-draft"), "same-form");
  await page.getByRole("button", { name: "收起左侧栏", exact: true }).click();
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  assert.equal(await name.inputValue(), "Unsaved provider draft");
  assert.equal(await name.getAttribute("data-sidebar-draft"), "same-form");
  await layout("provider-draft-preserved", false);
  report.scenarios.push("keyboard and active-section expansion preserve the same unsaved provider form");

  await page.getByRole("button", { name: "收起左侧栏", exact: true }).click();
  await page.getByRole("button", { name: "内容", exact: true }).click();
  assert.equal(await page.locator(".current-content-sidebar").isVisible(), true);
  await page.getByRole("button", { name: "收起左侧栏", exact: true }).click();
  await page.reload();
  await ready();
  await layout("collapsed-after-reload", true);
  assert.deepEqual(await workflow(), original);
  await page.getByRole("button", { name: "展开左侧栏", exact: true }).focus();
  await page.keyboard.press("Space");
  await page.reload();
  await ready();
  await layout("expanded-after-reload", false);
  report.scenarios.push("all navigation entries reopen panels and both layout states survive reload");

  for (const width of [1024, 390, 320]) {
    await page.setViewportSize({ width, height: width >= 1024 ? 768 : 844 });
    await layout("expanded-" + width, false);
    await page.getByRole("button", { name: "收起左侧栏", exact: true }).click();
    await layout("collapsed-" + width, true);
    await page.getByRole("button", { name: "工作流", exact: true }).click();
  }
  report.scenarios.push("controls stay framed at 1024/390/320 widths within the existing desktop workbench");

  await page.evaluate(key => localStorage.setItem(key, "invalid"), preferenceKey);
  await page.reload();
  await ready();
  assert.equal(await page.locator("#workbench-sidebar-panel").isVisible(), true);
  assert.deepEqual(await workflow(), original);
  await page.evaluate(() => {
    Storage.prototype.setItem = function() { throw new Error("storage unavailable"); };
  });
  await page.getByRole("button", { name: "收起左侧栏", exact: true }).click();
  assert.equal(await page.locator("#workbench-sidebar-panel").isVisible(), false);
  await page.getByRole("button", { name: "工作流", exact: true }).click();
  assert.equal(await page.locator("#workbench-sidebar-panel").isVisible(), true);
  report.scenarios.push("invalid preferences and failed preference writes do not block navigation");

  assert.deepEqual(report.mutations, []);
  assert.deepEqual(report.page_errors, []);
  assert.deepEqual(report.console_errors, []);
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ scenarios: report.scenarios.length, screenshots: report.layouts.length,
    mutations: 0, errors: 0 }));
})().catch(async error => {
  report.failure = String(error.stack ?? error);
  if (page) await page.screenshot({ path: path.join(evidence, "failure.png") }).catch(() => {});
  fs.mkdirSync(evidence, { recursive: true });
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.error(error);
  process.exitCode = 1;
}).finally(async () => { if (browser) await browser.close(); });
