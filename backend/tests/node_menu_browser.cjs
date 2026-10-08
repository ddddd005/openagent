"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const [base, backend, evidence, fixturePath] = process.argv.slice(2);
const fixture = fixturePath ? JSON.parse(fs.readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, "")) : null;
const dist = path.resolve(__dirname, "../../frontend/dist");
const storageKey = "workflow-workbench:fixed-base:v1";
const report = { page_errors: [], console_errors: [], asset_errors: [], mutation_requests: [], scenarios: [], layouts: [] };
let browser, page;

async function document() {
  return page.evaluate(key => {
    const value = JSON.parse(localStorage.getItem(key));
    return value.graph.entries[value.activeWorkflowId].document;
  }, storageKey);
}
async function ready() {
  await page.waitForFunction(key => {
    const value = JSON.parse(localStorage.getItem(key));
    return value?.graph.entries[value.activeWorkflowId]
      && !window.document.querySelector('[aria-label="创建串行 Agent 示例"]')?.disabled;
  }, storageKey);
}
async function fresh() {
  await page.getByRole("button", { name: "新建工作流", exact: true }).click();
  await ready();
}
async function openMenu(x = 80, y = 80) {
  if (page.viewportSize().width < 1024) {
    await page.locator(".graph-canvas").evaluate(element => element.focus({ preventScroll: true }));
    await page.keyboard.press("Shift+A");
  } else {
    await page.locator(".graph-canvas").click({ button: "right", position: { x, y } });
    await page.getByRole("menuitem", { name: "添加节点", exact: true }).click();
  }
  return page.getByRole("menu", { name: "添加节点", exact: true });
}
async function openFamily(category, family) {
  const menu = await openMenu();
  await menu.getByRole("menuitem", { name: category, exact: true }).click();
  await page.getByRole("menu", { name: category + "节点", exact: true })
    .getByRole("menuitem", { name: family, exact: true }).click();
}
async function selectNode(id) {
  await page.getByRole("button", { name: "定位全部节点", exact: true }).click();
  await page.locator(`.vue-flow__node[data-id="${id}"]`).click();
}
async function layout(label) {
  const geometry = await page.evaluate(() => {
    const visible = element => {
      const box = element.getBoundingClientRect(), style = getComputedStyle(element);
      return box.width > 0 && box.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    return { width: innerWidth, height: innerHeight, scrollWidth: window.document.documentElement.scrollWidth,
      boxes: [...window.document.querySelectorAll(".canvas-node-menu,dialog[open]")].filter(visible).map(element => {
        const box = element.getBoundingClientRect();
        return { left: box.left, top: box.top, right: box.right, bottom: box.bottom };
      }),
      overflows: [...window.document.querySelectorAll(".canvas-node-menu button,dialog[open] button,dialog[open] code")]
        .filter(visible).filter(element => element.scrollWidth > element.clientWidth + 1).map(element => element.textContent),
    };
  });
  if (geometry.width >= 1024) assert.ok(geometry.scrollWidth <= geometry.width + 1, JSON.stringify(geometry));
  assert.ok(geometry.boxes.every(box => box.left >= 0 && box.top >= 0
    && box.right <= geometry.width + 1 && box.bottom <= geometry.height + 1), JSON.stringify(geometry));
  assert.deepEqual(geometry.overflows, [], JSON.stringify(geometry));
  report.layouts.push({ label, ...geometry });
  await page.screenshot({ path: path.join(evidence, label + ".png") });
}
async function assertBinding(dialog, key, catalog) {
  assert.equal(await dialog.locator(".node-profile-binding code").textContent(), key);
  const type = catalog.find(row => `${row.component_id}@${row.component_version}` === key);
  assert.ok(type);
  const contracts = ["inputs", "outputs"].flatMap(direction => type[direction]
    .map(port => `${port.data_type}@${port.data_schema_version ?? 1}`));
  assert.deepEqual(await dialog.locator(".node-profile-ports dd").allTextContents(), contracts);
  assert.equal(await dialog.getByLabel("精确声明", { exact: true }).count(), 0);
  assert.equal(await dialog.getByRole("checkbox", { name: "高级兼容配置", exact: true }).count(), 0);
}

(async () => {
  fs.mkdirSync(evidence, { recursive: true });
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  let extraTypes = [], geminiOnly = false;
  context.on("request", request => {
    if (request.method() === "POST" && !["/api/graph/queries", "/api/graph/receipts/read"].includes(new URL(request.url()).pathname))
      report.mutation_requests.push(request.postData());
  });
  context.on("response", response => {
    if (response.status() >= 400) report.asset_errors.push({ url: response.url(), status: response.status() });
  });
  await context.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (![base, backend].includes(url.origin)) return route.abort();
    const query = url.pathname === "/api/graph/queries" ? route.request().postDataJSON() : null;
    if (fixture) {
      if (query) {
        if (query.operation === "catalog.node-types") {
          const value = JSON.parse(JSON.stringify(fixture));
          if (geminiOnly) value.node_types = value.node_types.filter(row =>
            row.component_id !== "models.source" || row.component_version === "4");
          value.node_types.push(...extraTypes);
          return route.fulfill({ json: value });
        }
        assert.ok(["resource.list", "definition.sessions"].includes(query.operation), JSON.stringify(query));
        return route.fulfill({ json: [] });
      }
      assert.ok(!url.pathname.startsWith("/api/"), url.href);
      const target = path.resolve(dist, "." + (url.pathname === "/" ? "/index.html" : decodeURIComponent(url.pathname)));
      assert.ok(target.startsWith(dist + path.sep), target);
      const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon" }[path.extname(target)];
      return route.fulfill({ body: fs.readFileSync(target), contentType: mime ?? "application/octet-stream" });
    }
    if (query?.operation === "catalog.node-types" && (extraTypes.length || geminiOnly)) {
      const response = await route.fetch(), value = await response.json();
      if (geminiOnly) value.node_types = value.node_types.filter(row =>
        row.component_id !== "models.source" || row.component_version === "4");
      value.node_types.push(...extraTypes);
      return route.fulfill({ response, json: value });
    }
    await route.continue();
  });
  const catalog = fixture?.node_types
    ?? (await (await context.request.get(backend + "/api/graph/node-types/v2")).json()).node_types;
  assert.equal(catalog.filter(row => row.executable).length, 44);
  assert.equal(new Set(catalog.filter(row => row.executable).map(row => row.component_id)).size, 41);
  page = await context.newPage();
  page.on("pageerror", error => report.page_errors.push(String(error)));
  page.on("console", message => { if (message.type() === "error") report.console_errors.push(message.text()); });
  await page.goto(base + "/");
  await ready();
  let menu = await openMenu();
  const categories = ["基础工具", "格式转换", "变量", "提示词", "模型", "Agent", "上下文", "酒馆", "前端展示"];
  assert.deepEqual(await menu.locator("[data-group-id]").evaluateAll(rows => rows.map(row => row.getAttribute("aria-label"))), categories);
  let familyCount = 0;
  for (const category of categories) {
    await menu.getByRole("menuitem", { name: category, exact: true }).hover();
    const names = await page.getByRole("menu", { name: category + "节点", exact: true }).getByRole("menuitem").allTextContents();
    familyCount += names.length;
    assert.ok(names.every(name => !/ · v[0-9]/.test(name)), JSON.stringify(names));
  }
  assert.equal(familyCount, 41);
  await layout("current-categories");
  await page.keyboard.press("Escape");
  report.scenarios.push("44 current declarations in 41 families and 9 categories");

  await page.getByRole("button", { name: "创建串行 Agent 示例", exact: true }).click();
  await ready();
  const demo = await document();
  assert.ok(demo.nodes.filter(row => row.component_id === "agents.execute").every(row => row.component_version === "4"));
  assert.ok(demo.object_bindings.filter(row => row.type_id === "workflow.effective-context").every(row => row.schema_version === 4));
  const agent = demo.nodes.find(row => row.title === "Agent A");
  await selectNode(agent.node_binding_id);
  await page.getByRole("button", { name: "切换节点配置", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "切换节点配置", exact: true });
  await assertBinding(dialog, "agents.execute@4", catalog);
  await layout("current-agent");
  await page.keyboard.press("Escape");
  await page.reload();
  await ready();
  assert.deepEqual(await document(), demo);
  report.scenarios.push("current native serial demo persists unchanged");

  await fresh();
  for (const [category, family, deepseek, gemini] of [
    ["模型", "模型来源", "models.source@2", "models.source@4"],
    ["模型", "模型调用", "models.chat@3", "models.chat@4"],
    ["Agent", "Agent 执行", "agents.execute@4", "agents.execute@8"],
  ]) {
    await openFamily(category, family);
    dialog = page.getByRole("dialog", { name: "添加" + family, exact: true });
    await assertBinding(dialog, deepseek, catalog);
    await dialog.getByRole("button", { name: "Gemini", exact: true }).click();
    await assertBinding(dialog, gemini, catalog);
    await layout(family === "模型来源" ? "gemini-model" : family === "模型调用" ? "gemini-chat" : "gemini-agent");
    for (let index = 0; index < 10; index++) {
      await page.keyboard.press("Tab");
      assert.equal(await page.evaluate(() => !!window.document.activeElement.closest("dialog[open]")), true);
    }
    await dialog.getByRole("button", { name: "添加节点", exact: true }).click();
    assert.equal((await document()).nodes.at(-1).component_version, gemini.split("@")[1]);
  }
  report.scenarios.push("current protocol choices and actual ports, with modal focus custody");

  const source = (await document()).nodes.find(row => row.component_id === "models.source");
  await selectNode(source.node_binding_id);
  await page.getByRole("button", { name: "切换节点配置", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "切换节点配置", exact: true });
  await dialog.getByRole("button", { name: "DeepSeek", exact: true }).click();
  assert.equal(await dialog.getByRole("button", { name: "确认切换", exact: true }).isDisabled(), true);
  await dialog.getByRole("checkbox", { name: "确认重置配置与公开输出", exact: true }).check();
  await dialog.getByRole("button", { name: "确认切换", exact: true }).click();
  assert.equal((await document()).nodes.find(row => row.node_binding_id === source.node_binding_id).component_version, "2");
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  assert.equal((await document()).nodes.find(row => row.node_binding_id === source.node_binding_id).component_version, "4");
  report.scenarios.push("acknowledged protocol replacement and undo");

  for (const [category, family, id, version] of [
    ["上下文", "读取 Agent 上下文", "context.output", "4"],
    ["上下文", "写回 Agent 上下文", "context.merge", "4"],
    ["提示词", "提示词条目", "prompts.item", "2"],
  ]) {
    await openFamily(category, family);
    assert.equal(await page.getByRole("dialog").count(), 0);
    const added = (await document()).nodes.at(-1);
    assert.equal(added.component_id, id);
    assert.equal(added.component_version, version);
  }
  report.scenarios.push("single current prompt/context declarations create directly");

  await page.locator(".graph-canvas").focus();
  await page.keyboard.press("Shift+A");
  await page.keyboard.press("ArrowRight");
  assert.equal(await page.evaluate(() => window.document.activeElement.textContent.trim()), "当前输入");
  await page.keyboard.press("End");
  assert.equal(await page.evaluate(() => window.document.activeElement.textContent.trim()), "输出");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Tab");
  assert.equal(await page.locator("[data-canvas-node-menu]").count(), 0);
  report.scenarios.push("keyboard category navigation and dismissal");

  for (const viewport of [{ width: 390, height: 844 }, { width: 320, height: 640 }, { width: 1024, height: 768 }]) {
    await page.setViewportSize(viewport);
    menu = await openMenu(8, 70);
    await menu.getByRole("menuitem", { name: "模型", exact: true }).click();
    await layout("menu-" + viewport.width);
    await page.getByRole("menu", { name: "模型节点", exact: true }).getByRole("menuitem", { name: "模型来源", exact: true }).click();
    dialog = page.getByRole("dialog", { name: "添加模型来源", exact: true });
    await dialog.getByRole("button", { name: "Gemini", exact: true }).click();
    await layout("profile-" + viewport.width);
    await page.keyboard.press("Escape");
  }
  report.scenarios.push("320/390/1024 menu and dialog framing");

  if (fixture) {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const retained = await document();
    const retiredId = await page.evaluate(key => {
      const value = JSON.parse(localStorage.getItem(key)), id = crypto.randomUUID();
      const current = value.graph.entries[value.activeWorkflowId];
      const retired = JSON.parse(JSON.stringify(current));
      retired.document.workflow_definition_id = id;
      retired.document.nodes.find(row => row.component_id === "models.source").component_version = "1";
      value.catalog.push({ id, title: "Retired test graph", description: "", nodeCount: retired.document.nodes.length });
      value.graph.entries[id] = retired;
      value.activeWorkflowId = value.selectedWorkflowId = id;
      localStorage.setItem(key, JSON.stringify(value));
      return id;
    }, storageKey);
    await page.reload();
    await ready();
    assert.deepEqual(await page.evaluate(({ key, id }) =>
      JSON.parse(localStorage.getItem(key)).graph.entries[id].document,
    { key: storageKey, id: retained.workflow_definition_id }), retained);
    assert.equal(await page.evaluate(({ key, id }) => {
      const value = JSON.parse(localStorage.getItem(key));
      return !!value.graph.entries[id] || value.catalog.some(row => row.id === id);
    }, { key: storageKey, id: retiredId }), false);
    report.scenarios.push("retired saved graph is removed on reopen while the current graph stays exact");
    extraTypes = ["7", "42"].map(version => ({ ...catalog.find(row => row.component_id === "tools.text"),
      component_id: "plugin.example", component_version: version, display_name: "External node" }));
    await page.getByRole("button", { name: "刷新目录和运行", exact: true }).click();
    await ready();
    await openFamily("其他", "External node");
    assert.equal(await page.getByRole("dialog").count(), 0);
    assert.equal((await document()).nodes.at(-1).component_version, "42");
    extraTypes = [];
    geminiOnly = true;
    await page.getByRole("button", { name: "刷新目录和运行", exact: true }).click();
    await ready();
    await openFamily("模型", "模型来源");
    assert.equal(await page.getByRole("dialog").count(), 0);
    assert.equal((await document()).nodes.at(-1).component_version, "4");
    report.scenarios.push("latest external declaration and single installed protocol");
  }
  assert.deepEqual(report.page_errors, []);
  assert.deepEqual(report.console_errors, []);
  assert.deepEqual(report.asset_errors, []);
  assert.deepEqual(report.mutation_requests, []);
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ scenarios: report.scenarios.length, screenshots: report.layouts.length, mutations: 0, errors: 0 }));
})().catch(async error => {
  report.failure = String(error.stack ?? error);
  if (page) await page.screenshot({ path: path.join(evidence, "failure.png") }).catch(() => {});
  fs.mkdirSync(evidence, { recursive: true });
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.error(error);
  process.exitCode = 1;
}).finally(async () => { if (browser) await browser.close(); });
