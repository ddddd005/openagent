"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const [base, backend, encodedWidth, evidence] = process.argv.slice(2);
const width = Number(encodedWidth), storageKey = "workflow-workbench:fixed-base:v1";
const report = { width, page_errors: [], asset_errors: [], console_errors: [], mutation_requests: [], layouts: [] };
let browser, page;

async function snapshot() {
  return page.evaluate(key => JSON.parse(localStorage.getItem(key)), storageKey);
}
async function active() {
  const cache = await snapshot();
  return { id: cache.activeWorkflowId, entry: cache.graph.entries[cache.activeWorkflowId] };
}
async function stable() {
  await page.waitForFunction(key => {
    const cache = JSON.parse(localStorage.getItem(key));
    return cache && cache.graph.entries[cache.activeWorkflowId] && !cache.graph.entries[cache.activeWorkflowId].pending;
  }, storageKey);
  await page.waitForTimeout(200);
}
async function unselected() {
  assert.equal(await page.getByRole("button", { name: "复制选中节点", exact: true }).isDisabled(), true);
  assert.equal(await page.locator(".vue-flow__node.selected,.vue-flow__edge.selected").count(), 0);
  assert.equal(await page.locator(".graph-inspector > header strong").textContent(), "运行结果");
  assert.equal(await page.getByRole("menu").count(), 0);
}
async function layout(label) {
  const geometry = await page.evaluate(() => {
    const box = selector => {
      const value = document.querySelector(selector).getBoundingClientRect();
      return { x: value.x, y: value.y, width: value.width, height: value.height,
        bottom: value.bottom, right: value.right };
    };
    return { toolbar: box(".graph-toolbar"), canvas: box(".graph-canvas"), inspector: box(".graph-inspector"),
      scrollWidth: document.documentElement.scrollWidth, width: innerWidth,
      workbenches: document.querySelectorAll('[aria-label="工作流节点工作台"]').length,
      vueflow: document.querySelectorAll(".graph-canvas > .vue-flow").length,
      tools: [...document.querySelectorAll(".graph-toolbar-actions button")].map(button =>
        button.getAttribute("aria-label") || button.textContent) };
  });
  assert.equal(geometry.workbenches, 1);
  assert.equal(geometry.vueflow, 1);
  assert.ok(geometry.scrollWidth <= geometry.width + 1, JSON.stringify(geometry));
  assert.ok(geometry.toolbar.bottom <= geometry.canvas.y + 1, JSON.stringify(geometry));
  assert.ok(geometry.canvas.right <= geometry.inspector.x + 1, JSON.stringify(geometry));
  const first = report.layouts[0];
  if (first) {
    assert.deepEqual(geometry.tools, first.geometry.tools);
    for (const key of ["toolbar", "canvas", "inspector"])
      for (const measure of ["x", "y", "width", "height"])
        assert.ok(Math.abs(geometry[key][measure] - first.geometry[key][measure]) <= 1,
          JSON.stringify({ label, key, measure, geometry, original: first.geometry }));
  }
  report.layouts.push({ label, geometry });
  await page.screenshot({ path: path.join(evidence, `${label}-${width}.png`), fullPage: true });
}
async function selectFirstNode() {
  await page.getByRole("button", { name: "定位全部节点", exact: true }).click();
  await page.waitForTimeout(250);
  await page.locator(".vue-flow__node").first().click();
  await page.waitForFunction(() => !document.querySelector('[aria-label="复制选中节点"]').disabled);
}
async function openWorkflow(id) {
  await page.getByRole("button", { name: "工作流", exact: true }).click();
  const cache = await snapshot();
  const index = cache.catalog.findIndex(row => row.id === id);
  assert.ok(index >= 0);
  await page.locator(".workbench-workflow-item").nth(index).dblclick();
  await stable();
  assert.equal((await active()).id, id);
}

(async () => {
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width, height: 1000 } });
  await context.route("**/*", async route => {
    const origin = new URL(route.request().url()).origin;
    if (![base, backend].includes(origin)) {
      report.asset_errors.push({ url: route.request().url(), reason: "external" });
      return route.abort();
    }
    return route.continue();
  });
  context.on("request", request => {
    if (new URL(request.url()).pathname.endsWith("/commands"))
      report.mutation_requests.push({ url: request.url(), body: request.postDataJSON() });
  });
  context.on("response", response => {
    if (response.status() >= 400)
      report.asset_errors.push({ url: response.url(), status: response.status() });
  });
  page = await context.newPage();
  page.on("pageerror", error => report.page_errors.push(String(error)));
  page.on("console", message => { if (message.type() === "error") report.console_errors.push(message.text()); });
  assert.equal((await page.goto(base + "/")).status(), 200);
  await page.getByRole("button", { name: "创建串行 Agent 示例", exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector('[aria-label="创建串行 Agent 示例"]').disabled);
  await stable();
  const fresh = await active();
  assert.equal(fresh.entry.document.schema_version, 2);
  assert.equal(fresh.entry.document.nodes.length, 0);
  await unselected();
  await layout("fresh-empty");

  await page.getByRole("button", { name: "创建串行 Agent 示例", exact: true }).click();
  await stable();
  const serial = await active();
  assert.equal(serial.entry.document.schema_version, 2);
  assert.ok(serial.entry.document.nodes.length > 0);
  await unselected();
  await layout("serial-populated");
  await selectFirstNode();
  assert.equal(await page.getByRole("button", { name: "复制选中节点", exact: true }).isDisabled(), false);
  await page.getByRole("button", { name: "新建工作流", exact: true }).click();
  await stable();
  const empty = await active();
  assert.equal(empty.entry.document.schema_version, 2);
  assert.equal(empty.entry.document.nodes.length, 0);
  await unselected();
  await layout("new-empty-after-node-selection");

  await openWorkflow(serial.id);
  await unselected();
  await selectFirstNode();
  await page.getByRole("button", { name: "内容", exact: true }).click();
  await page.getByRole("button", { name: "创建酒馆示例", exact: true }).click();
  await stable();
  const tavern = await active();
  assert.equal(tavern.entry.document.schema_version, 2);
  assert.ok(tavern.entry.document.nodes.some(row => row.component_id === "tavern.chat.presentation"));
  assert.equal(tavern.entry.saved_revision, 0);
  assert.equal(tavern.entry.session_id, null);
  await unselected();
  const launch = page.locator('.current-content-sidebar [aria-label="打开酒馆前端"]');
  assert.equal(await launch.getAttribute("href"), null);
  await page.getByRole("button", { name: "工作流", exact: true }).click();
  await layout("tavern-populated");

  await selectFirstNode();
  await openWorkflow(empty.id);
  await unselected();
  await layout("empty-after-tavern");
  const canvas = page.locator(".graph-canvas");
  await canvas.click({ button: "right", position: { x: 80, y: 80 } });
  await page.getByRole("menuitem", { name: "添加节点", exact: true }).click();
  const menu = page.getByRole("menu", { name: "添加节点", exact: true });
  const menuNames = await menu.getByRole("menuitem").allTextContents();
  for (const expected of ["全局 Lorebook 引用", "全局 Lorebook 触发",
    "Read tavern chat display state", "Append tavern chat display references", "Present tavern chat display references"])
    assert.ok(menuNames.includes(expected), JSON.stringify(menuNames));
  report.node_menu = menuNames;
  await page.getByRole("button", { name: "新建工作流", exact: true }).click();
  await stable();
  await unselected();
  assert.equal((await active()).entry.document.schema_version, 2);

  report.workflow_ids = { fresh: fresh.id, serial: serial.id, empty: empty.id, tavern: tavern.id };
  assert.deepEqual(report.page_errors, []);
  assert.deepEqual(report.asset_errors, []);
  assert.deepEqual(report.console_errors, []);
  assert.deepEqual(report.mutation_requests, []);
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ width, evidence, scenarios: report.layouts.length }));
})().catch(async error => {
  report.failure = String(error.stack || error);
  if (page) await page.screenshot({ path: path.join(evidence, "failure.png"), fullPage: true }).catch(() => {});
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.error(error);
  process.exitCode = 1;
}).finally(async () => { if (browser) await browser.close(); });
