"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const [workbench, backend, workflow, session, evidence] = process.argv.slice(2);
const report = { page_errors: [], console_errors: [], asset_errors: [], mutation_requests: [], layouts: [] };
let browser, page;

async function screenshot(label) {
  const layout = await page.evaluate(() => {
    const visible = element => {
      const box = element.getBoundingClientRect(), style = getComputedStyle(element);
      return box.width > 0 && box.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const elements = [...document.querySelectorAll(".model-source-fields input,.model-source-fields select,.model-source-fields button,.mes_thinking")].filter(visible);
    return { width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      controls: elements.map(element => {
        const box = element.getBoundingClientRect();
        return { label: element.getAttribute("aria-label") ?? element.tagName,
          left: box.left, right: box.right, width: box.width };
      }) };
  });
  assert.ok(layout.scrollWidth <= layout.width + 1, JSON.stringify(layout));
  assert.ok(layout.controls.every(box => box.left >= -1 && box.right <= layout.width + 1 && box.width > 0), JSON.stringify(layout));
  report.layouts.push({ label, ...layout });
  await page.screenshot({ path: path.join(evidence, label + ".png"), fullPage: true });
}

(async () => {
  fs.mkdirSync(evidence, { recursive: true });
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route("**/*", async route => {
    if (![workbench, backend].includes(new URL(route.request().url()).origin)) {
      report.asset_errors.push(route.request().url());
      return route.abort();
    }
    await route.continue();
  });
  context.on("request", request => {
    if (new URL(request.url()).pathname.endsWith("/commands")) report.mutation_requests.push(request.postData());
  });
  context.on("response", response => {
    if (response.status() >= 400) report.asset_errors.push({ url: response.url(), status: response.status() });
  });
  page = await context.newPage();
  page.on("pageerror", error => report.page_errors.push(String(error)));
  page.on("console", message => { if (message.type() === "error") report.console_errors.push(message.text()); });
  await page.goto(workbench + "/");
  await page.waitForFunction(() => !document.querySelector('[aria-label="创建串行 Agent 示例"]')?.disabled);
  const canvas = page.locator(".graph-canvas");
  await canvas.click({ button: "right", position: { x: 80, y: 80 } });
  await page.getByRole("menuitem", { name: "添加节点", exact: true }).click();
  const menu = page.getByRole("menu", { name: "添加节点", exact: true });
  const catalog = await (await context.request.get(backend + "/api/graph/node-types/v2")).json();
  const source = catalog.node_types.find(row => row.component_id === "models.source" && row.component_version === "4");
  assert.ok(source);
  await menu.getByRole("menuitem", { name: "模型", exact: true }).click();
  await page.getByRole("menu", { name: "模型节点", exact: true }).getByRole("menuitem", { name: "模型来源", exact: true }).click();
  const profile = page.getByRole("dialog", { name: "添加模型来源", exact: true });
  await profile.getByRole("button", { name: "Gemini", exact: true }).click();
  await profile.getByRole("button", { name: "添加节点", exact: true }).click();
  await page.getByRole("button", { name: "定位全部节点", exact: true }).click();
  await page.locator(".vue-flow__node").first().click();
  const fields = page.getByRole("form", { name: "模型源配置", exact: true });
  await fields.waitFor();
  const providerValue = await fields.locator("select").first().locator("option")
    .filter({ hasText: "Offline Gemini" }).getAttribute("value");
  assert.ok(providerValue);
  await fields.locator("select").first().selectOption(providerValue);
  await fields.getByLabel("模型名称", { exact: true }).fill("gemini-3-flash-preview");
  await fields.getByLabel("思考模式", { exact: true }).selectOption("level");
  await fields.getByLabel("思考强度", { exact: true }).selectOption("medium");
  await fields.getByRole("button", { name: "应用模型配置", exact: true }).click();
  await fields.getByText("模型配置已更新", { exact: true }).waitFor();
  await screenshot("gemini-config-1440");
  await page.setViewportSize({ width: 1024, height: 1000 });
  await page.getByRole("button", { name: "定位全部节点", exact: true }).click();
  await screenshot("gemini-config-1024");
  await page.reload();
  await page.getByRole("button", { name: "定位全部节点", exact: true }).click();
  await page.locator(".vue-flow__node").first().click();
  assert.equal(await page.getByLabel("思考强度", { exact: true }).inputValue(), "medium");
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  await page.getByRole("button", { name: "新增供应商资源", exact: true }).click();
  await page.getByLabel("供应商协议", { exact: true }).selectOption("gemini");
  const form = page.locator(".current-provider-panel form");
  assert.equal(await form.getByLabel("后端凭据引用", { exact: true }).inputValue(), "env:GEMINI_API_KEY");
  assert.equal(await form.getByLabel("服务地址", { exact: true }).inputValue(),
    "https://generativelanguage.googleapis.com/v1beta");
  await screenshot("gemini-provider-1024");

  await page.goto(`${backend}/tavern/?graph_workflow=${workflow}&graph_session=${session}`);
  await page.locator("#chat .mes").last().waitFor();
  assert.equal(await page.locator("#chat .mes").count(), 4);
  const summaries = page.locator(".mes_thinking:not([hidden])");
  assert.equal(await summaries.count(), 2);
  assert.equal(await summaries.first().getAttribute("open"), null);
  await summaries.first().locator("summary").click();
  const text = await page.locator("#chat").innerText();
  assert.ok(text.includes("Visible summary 0") && text.includes("Gemini accepted answer"));
  assert.ok(!text.includes("synthetic-fixture") && !text.includes("thoughtSignature") && !text.includes("provider_metadata"));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await screenshot("gemini-summary-1440");
  await page.setViewportSize({ width: 390, height: 844 });
  await screenshot("gemini-summary-390");
  await page.reload();
  await page.locator("#chat .mes").last().waitFor();
  assert.equal(await page.locator(".mes_thinking:not([hidden])").count(), 2);
  assert.equal(await page.locator(".mes_thinking:not([hidden])").first().getAttribute("open"), null);
  assert.deepEqual(report.page_errors, []);
  assert.deepEqual(report.console_errors, []);
  assert.deepEqual(report.asset_errors, []);
  assert.deepEqual(report.mutation_requests, []);
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ screenshots: report.layouts.length, mutations: 0, errors: 0 }));
})().catch(async error => {
  report.failure = String(error.stack ?? error);
  if (page) await page.screenshot({ path: path.join(evidence, "failure.png"), fullPage: true }).catch(() => {});
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.error(error);
  process.exitCode = 1;
}).finally(async () => { if (browser) await browser.close(); });
