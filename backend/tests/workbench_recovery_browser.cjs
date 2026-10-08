"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const [base, backend, encodedWidth, evidence] = process.argv.slice(2);
const width = Number(encodedWidth);
const primary = "workflow-workbench:fixed-base:v1";
const isolated = "workflow-workbench:isolated-graph:v7";
const report = { width, page_errors: [], console_errors: [], asset_errors: [], mutation_requests: [], queries: [], scenarios: [] };
let browser, page;

async function records() {
  return page.evaluate(({ primary, isolated }) => ({
    primary: localStorage.getItem(primary), isolated: localStorage.getItem(isolated),
  }), { primary, isolated });
}
async function layout(label) {
  const geometry = await page.evaluate(() => {
    const banner = document.querySelector(".storage-recovery").getBoundingClientRect();
    const items = [...document.querySelectorAll(".storage-recovery > span,.storage-recovery > button")]
      .map(element => {
        const box = element.getBoundingClientRect();
        return { x: box.x, right: box.right, y: box.y, bottom: box.bottom };
      });
    return { width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      banner: { x: banner.x, right: banner.right, y: banner.y, bottom: banner.bottom }, items };
  });
  assert.ok(geometry.scrollWidth <= geometry.width + 1, JSON.stringify(geometry));
  for (const box of geometry.items) {
    assert.ok(box.x >= geometry.banner.x && box.right <= geometry.banner.right, JSON.stringify(geometry));
    assert.ok(box.y >= geometry.banner.y && box.bottom <= geometry.banner.bottom, JSON.stringify(geometry));
  }
  for (let index = 1; index < geometry.items.length; index++) {
    const previous = geometry.items[index - 1], current = geometry.items[index];
    assert.ok(current.x >= previous.right || current.y >= previous.bottom, JSON.stringify(geometry));
  }
  await page.screenshot({ path: path.join(evidence, `${label}-${width}.png`), fullPage: true });
  return geometry;
}
async function exportRaw(name, raw) {
  const received = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出原记录", exact: true }).click();
  const download = await received;
  const target = path.join(evidence, `${name}-export.json`);
  await download.saveAs(target);
  assert.equal(await download.failure(), null);
  assert.equal(fs.readFileSync(target, "utf8"), raw);
}

(async () => {
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const futureRaw = JSON.stringify({ schemaVersion: 99, kind: "future-workbench",
    text: "original \u4e16\u754c record", pending: { action: "start",
      path: "/api/graph/sessions/11111111-1111-4111-8111-111111111111/runs",
      body: { input: "Do not replay", idempotency_key: "22222222-2222-4222-8222-222222222222" } } });
  for (const scenario of [
    { name: "malformed", raw: "{broken" },
    { name: "unsupported", raw: futureRaw },
    { name: "malformed-isolated", raw: futureRaw, recovery: "{broken isolated" },
  ]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, acceptDownloads: true });
    await context.route("**/*", async route => {
      const origin = new URL(route.request().url()).origin;
      if (![base, backend].includes(origin)) {
        report.asset_errors.push({ url: route.request().url(), reason: "external" });
        return route.abort();
      }
      return route.continue();
    });
    context.on("request", request => {
      if (new URL(request.url()).pathname === "/api/graph/queries") {
        report.queries.push(request.postDataJSON());
      } else if (!["GET", "HEAD"].includes(request.method())) {
        report.mutation_requests.push({ url: request.url(), method: request.method() });
      }
    });
    context.on("response", response => {
      if (response.status() >= 400) report.asset_errors.push({ url: response.url(), status: response.status() });
    });
    await context.addInitScript(({ primary, isolated, raw, recovery }) => {
      if (localStorage.getItem(primary) === null) {
        localStorage.setItem(primary, raw);
        if (recovery !== undefined) localStorage.setItem(isolated, recovery);
      }
    }, { primary, isolated, raw: scenario.raw, recovery: scenario.recovery });
    page = await context.newPage();
    page.on("pageerror", error => report.page_errors.push(String(error)));
    page.on("console", message => { if (message.type() === "error") report.console_errors.push(message.text()); });
    assert.equal((await page.goto(base + "/")).status(), 200);
    await page.getByRole("button", { name: "导出原记录", exact: true }).waitFor();
    assert.ok((await page.locator(".storage-recovery").textContent()).includes("\u4fdd\u5b58\u8bb0\u5f55\u65e0\u6cd5\u8bfb\u53d6"));
    assert.equal(await page.getByRole("button", { name: "保存工作流", exact: true }).isDisabled(), true);
    assert.deepEqual(await records(), { primary: scenario.raw, isolated: scenario.recovery ?? null });
    const blockedLayout = await layout(`${scenario.name}-blocked`);
    await exportRaw(scenario.name, scenario.recovery ?? scenario.raw);
    if (scenario.recovery !== undefined) {
      assert.equal(await page.getByRole("button", { name: "另建本机工作区", exact: true }).count(), 0);
      await page.reload();
      await page.getByRole("button", { name: "导出原记录", exact: true }).waitFor();
      assert.deepEqual(await records(), { primary: scenario.raw, isolated: scenario.recovery });
      report.scenarios.push({ name: scenario.name, blockedLayout, isolated_preserved: true });
      await context.close();
      continue;
    }
    await page.getByRole("button", { name: "另建本机工作区", exact: true }).click();
    await page.waitForFunction(key => localStorage.getItem(key) !== null, isolated);
    const recovered = await records(), saved = JSON.parse(recovered.isolated);
    assert.equal(recovered.primary, scenario.raw);
    assert.equal(saved.schemaVersion, 7); assert.equal(saved.kind, "graph-workbench");
    assert.equal(saved.catalog.length, 1);
    const entry = saved.graph.entries[saved.activeWorkflowId];
    assert.equal(entry.document.schema_version, 2); assert.equal(entry.document.nodes.length, 0);
    assert.equal(entry.saved_revision, 0); assert.equal(entry.session_id, null); assert.equal(entry.pending, null);
    assert.equal(await page.getByRole("button", { name: "另建本机工作区", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "保存工作流", exact: true }).isDisabled(), false);
    await page.getByRole("button", { name: "新建工作流", exact: true }).click();
    await page.waitForFunction(key => JSON.parse(localStorage.getItem(key)).catalog.length === 2, isolated);
    const beforeReload = await records();
    await page.reload();
    await page.getByRole("button", { name: "导出原记录", exact: true }).waitFor();
    assert.deepEqual(await records(), beforeReload);
    await page.getByRole("button", { name: "供应商", exact: true }).click();
    await page.getByText("暂无供应商当前资源", { exact: true }).waitFor();
    const create = page.getByRole("button", { name: "新增供应商资源", exact: true });
    assert.equal(await create.isDisabled(), false);
    const recoveredLayout = await layout(`${scenario.name}-recovered-empty-providers`);
    await create.click();
    await page.getByRole("button", { name: "保存当前资源", exact: true }).waitFor();
    await page.getByRole("button", { name: "取消编辑", exact: true }).click();
    assert.equal((await records()).primary, scenario.raw);
    await exportRaw(`${scenario.name}-after-reload`, scenario.raw);
    report.scenarios.push({ name: scenario.name, blockedLayout, recoveredLayout, reloaded: true,
      original_preserved: true, export_exact: true, saved_catalog_count: 2, providers_empty: true });
    await context.close();
  }
  assert.deepEqual(report.page_errors, []); assert.deepEqual(report.console_errors, []);
  assert.deepEqual(report.asset_errors, []); assert.deepEqual(report.mutation_requests, []);
  const allowedQueries = new Set(["catalog.node-types", "definition.sessions", "resource.list"]);
  assert.ok(report.queries.every(query => allowedQueries.has(query.operation)),
    JSON.stringify(report.queries));
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ width, evidence, scenarios: report.scenarios.length }));
})().catch(async error => {
  report.failure = String(error.stack || error);
  if (page && !page.isClosed()) await page.screenshot({ path: path.join(evidence, "failure.png"), fullPage: true }).catch(() => {});
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.error(error);
  process.exitCode = 1;
}).finally(async () => { if (browser) await browser.close(); });
