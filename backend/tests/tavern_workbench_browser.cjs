"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const [base, seedPath] = process.argv.slice(2);
const seed = JSON.parse(fs.readFileSync(seedPath, "utf8"));
const storageKey = "workflow-workbench:fixed-base:v1";
const report = { width: seed.width, page_errors: [], asset_errors: [], requests: [], overflow: [], overlaps: [],
  console_errors: [], native_calls: false };
let browser, page;
const snapshot = async () => page.evaluate(key => JSON.parse(localStorage.getItem(key)), storageKey);
const mutationCount = () => report.requests.filter(row => new URL(row.url).pathname.endsWith("/commands")).length;
async function active() {
  const value = await snapshot();
  return value.graph.entries[value.activeWorkflowId];
}
async function stable() {
  await page.waitForTimeout(350);
  await page.waitForFunction(key => {
    const value = JSON.parse(localStorage.getItem(key));
    return value && !value.graph.entries[value.activeWorkflowId].pending;
  }, storageKey);
}
async function selectNode(id) {
  await page.getByRole("button", { name: "定位全部节点", exact: true }).click();
  await page.waitForTimeout(350);
  await page.locator(`.vue-flow__node[data-id="${id}"]`).click();
}
async function checkLayout(label) {
  const measured = await page.evaluate(() => {
    const overflow = [], overlaps = [];
    if (document.documentElement.scrollWidth > innerWidth + 1)
      overflow.push({ kind: "document", width: document.documentElement.scrollWidth, viewport: innerWidth });
    const panel = document.querySelector(".current-content-sidebar");
    if (!panel) return { overflow, overlaps };
    const boundary = panel.getBoundingClientRect();
    const controls = [...panel.querySelectorAll("button,input,select,textarea,a")].filter(element => {
      const style = getComputedStyle(element), box = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" && box.width && box.height
        && box.top < innerHeight && box.bottom > 0;
    });
    for (const element of controls) {
      const box = element.getBoundingClientRect();
      if (box.left < boundary.left - 1 || box.right > boundary.right + 1)
        overflow.push({ kind: "control", label: element.getAttribute("aria-label") ?? element.textContent, left: box.left, right: box.right });
    }
    for (let first = 0; first < controls.length; first++) for (let second = first + 1; second < controls.length; second++) {
      const a = controls[first].getBoundingClientRect(), b = controls[second].getBoundingClientRect();
      if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
        && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1)
        overlaps.push({ a: controls[first].getAttribute("aria-label") ?? controls[first].textContent,
          b: controls[second].getAttribute("aria-label") ?? controls[second].textContent });
    }
    return { overflow, overlaps };
  });
  report.overflow.push(...measured.overflow.map(value => ({ label, ...value })));
  report.overlaps.push(...measured.overlaps.map(value => ({ label, ...value })));
  await page.screenshot({ path: path.join(seed.evidence, `${label}-${seed.width}.png`), fullPage: true });
}
(async () => {
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width: seed.width, height: 1000 } });
  await context.addInitScript(({ key, cache, origin }) => {
    if (location.origin !== origin) return;
    if (!localStorage.getItem(key)) localStorage.setItem(key, JSON.stringify(cache));
  }, { key: storageKey, cache: seed.cache, origin: base });
  await context.route("**/*", async route => {
    const url = route.request().url();
    if (!url.startsWith(base + "/")) {
      report.asset_errors.push({ url, reason: "external_request" });
      await route.abort();
    } else await route.continue();
  });
  context.on("page", opened => {
    opened.on("pageerror", error => report.page_errors.push(String(error)));
    opened.on("console", message => { if (message.type() === "error") report.console_errors.push(message.text()); });
  });
  context.on("request", request => {
    if (request.url().startsWith(base + "/api/graph/")) {
      let body = null;
      try { body = request.postDataJSON(); } catch {}
      report.requests.push({ url: request.url(), method: request.method(), body });
      if (["run.start", "consumer.run.start", "consumer.event.submit", "event.submit"].includes(body?.operation))
        report.native_calls = true;
    }
  });
  context.on("response", response => {
    const pathname = new URL(response.url()).pathname;
    if (response.status() >= 400 && (pathname.startsWith("/workbench/assets/")
      || pathname.startsWith("/assets/") || pathname.startsWith("/tavern/")))
      report.asset_errors.push({ url: response.url(), status: response.status() });
  });
  page = await context.newPage();
  await page.goto(base + "/workbench/");
  await page.getByRole("button", { name: "内容", exact: true }).click();
  const sidebar = page.locator(".current-content-sidebar");
  const lorebook = sidebar.getByRole("region", { name: "全局 Lorebook 资源" });
  const groups = sidebar.getByRole("region", { name: "新版提示词当前资源" });
  const launch = sidebar.getByRole("region", { name: "酒馆聊天入口" });
  await launch.getByRole("link", { name: "打开酒馆前端" }).waitFor();
  await page.waitForFunction(() => {
    const anchor = document.querySelector('[aria-label="打开酒馆前端"]');
    return anchor?.getAttribute("href");
  });
  const originalLink = new URL(await launch.getByRole("link", { name: "打开酒馆前端" }).getAttribute("href"));
  assert.equal(originalLink.origin, base);
  assert.equal(originalLink.pathname, "/tavern/");
  assert.equal(originalLink.searchParams.get("graph_workflow"), seed.cache.activeWorkflowId);
  assert.equal(originalLink.searchParams.get("graph_session"), seed.original_session);
  report.original_link = originalLink.href;
  const beforeLaunch = mutationCount();
  const opened = context.waitForEvent("page");
  await launch.getByRole("link", { name: "打开酒馆前端" }).click();
  const tavern = await opened;
  await tavern.waitForURL(report.original_link);
  await tavern.waitForFunction(() => document.querySelector("#transcript-status")?.textContent === "2 条展示记录"
    && !document.querySelector("#send_textarea")?.disabled);
  assert.equal(tavern.url(), report.original_link);
  assert.equal(await tavern.locator("#sessions").inputValue(), seed.original_session);
  assert.ok((await tavern.locator("#chat").textContent()).includes("Browser acceptance original input."));
  assert.ok((await tavern.locator("#chat").textContent()).includes("Native accepted answer"));
  assert.equal(await tavern.locator("#error").textContent(), "");
  assert.equal(mutationCount(), beforeLaunch);
  report.launch_commands = mutationCount() - beforeLaunch;
  report.popup_session = await tavern.locator("#sessions").inputValue();
  await tavern.screenshot({ path: path.join(seed.evidence, `exact-tavern-launch-${seed.width}.png`), fullPage: true });
  await tavern.close();

  await lorebook.getByRole("button", { name: "新增 Lorebook 资源" }).click();
  const bookForm = lorebook.getByRole("form", { name: "全局 Lorebook 表单" });
  await bookForm.getByLabel("Lorebook 资源名称").fill("Workbench shared book");
  await bookForm.getByRole("button", { name: "新增条目", exact: true }).click();
  await bookForm.getByLabel("条目 1 正文").fill("WORKBENCH_BOOK_ORIGINAL");
  await bookForm.getByLabel("条目 1 模式").selectOption("constant");
  const createdBook = await bookForm.locator("label").filter({ hasText: "资源 UUID" }).locator("output").textContent();
  const bookEntry = await bookForm.locator(".lorebook-entry small").first().textContent();
  await checkLayout("lorebook-create");
  await bookForm.getByRole("button", { name: "保存全局 Lorebook", exact: true }).click();
  await bookForm.waitFor({ state: "detached" });
  await lorebook.locator("li > button").filter({ hasText: "Workbench shared book" }).click();
  await bookForm.getByLabel("条目 1 正文").fill("WORKBENCH_BOOK_EDITED");
  assert.equal(await bookForm.locator(".lorebook-entry small").first().textContent(), bookEntry);
  await bookForm.getByRole("button", { name: "保存全局 Lorebook", exact: true }).click();
  await bookForm.waitFor({ state: "detached" });
  report.book_id = createdBook.trim(); report.book_entry_id = bookEntry.trim();

  await groups.getByRole("button", { name: "新增提示词资源" }).click();
  const promptForm = groups.getByRole("form", { name: "提示词当前资源表单" });
  await promptForm.getByRole("button", { name: "新增条目", exact: true }).click();
  await promptForm.getByLabel("条目 1 正文").fill("WORKBENCH_GROUP_FIRST");
  await promptForm.getByRole("button", { name: "新增条目", exact: true }).click();
  await promptForm.getByLabel("条目 2 正文").fill("WORKBENCH_GROUP_SECOND");
  await promptForm.getByLabel("条目 2 Role").selectOption("user");
  await promptForm.getByLabel("条目 2 生命周期").selectOption("context_once");
  await promptForm.getByLabel("条目 2 精简许可").selectOption("allowed");
  const originalGroup = await promptForm.locator("label").filter({ hasText: "资源 UUID" }).locator("output").textContent();
  const originalMembers = await promptForm.locator(".member-id").allTextContents();
  await promptForm.getByRole("button", { name: "保存当前资源", exact: true }).click();
  await promptForm.waitFor({ state: "detached" });
  await groups.getByRole("button", { name: `复制提示词组 workspace ${originalGroup.trim()}` }).click();
  const copiedGroup = await promptForm.locator("label").filter({ hasText: "资源 UUID" }).locator("output").textContent();
  const copiedMembers = await promptForm.locator(".member-id").allTextContents();
  assert.notEqual(copiedGroup, originalGroup);
  assert.ok(copiedMembers.every(id => !originalMembers.includes(id)));
  const beforeCopyReferences = await active();
  assert.equal(beforeCopyReferences.document.nodes.find(row => row.node_binding_id === seed.prompt_node).config.reference.resource_id,
    seed.cache.graph.entries[seed.cache.activeWorkflowId].document.nodes.find(row => row.node_binding_id === seed.prompt_node).config.reference.resource_id);
  await promptForm.getByLabel("条目 2 上移").click();
  assert.equal(await promptForm.getByLabel("条目 1 正文").inputValue(), "WORKBENCH_GROUP_SECOND");
  assert.equal(await promptForm.getByLabel("条目 1 生命周期").inputValue(), "context_once");
  assert.equal(await promptForm.getByLabel("条目 1 精简许可").inputValue(), "allowed");
  assert.equal(await promptForm.locator(".member-id").first().textContent(), copiedMembers[1]);
  await checkLayout("prompt-copy-reorder");
  await promptForm.getByRole("button", { name: "保存当前资源", exact: true }).click();
  await promptForm.waitFor({ state: "detached" });
  report.prompt_original = originalGroup.trim(); report.prompt_copy = copiedGroup.trim();

  for (const nodeId of seed.lorebook_nodes) {
    await selectNode(nodeId);
    const fields = page.getByRole("form", { name: "全局 Lorebook 节点配置" });
    await fields.getByLabel("全局 Lorebook 选择").selectOption({
      value: JSON.stringify([1, "workspace", "workflow.tavern.lorebook", report.book_id]),
    });
    await fields.getByRole("button", { name: "应用 Lorebook 配置" }).click();
    await stable();
  }
  await selectNode(seed.prompt_node);
  const reference = page.getByRole("form", { name: "提示词引用配置" });
  await reference.getByLabel("提示词当前资源").selectOption({
    value: JSON.stringify([1, "workspace", "workflow.prompt-resource", report.prompt_copy]),
  });
  await reference.getByRole("button", { name: "应用提示词引用" }).click();
  await stable();
  const referenced = await active();
  assert.equal(referenced.document.nodes.find(row => row.node_binding_id === seed.prompt_node).config.reference.resource_id, report.prompt_copy);
  assert.ok(seed.lorebook_nodes.every(id => referenced.document.nodes.find(row => row.node_binding_id === id).config.reference.resource_id === report.book_id));
  await page.getByRole("button", { name: "保存工作流", exact: true }).click();
  await stable();
  await page.waitForFunction(key => {
    const value = JSON.parse(localStorage.getItem(key)), entry = value.graph.entries[value.activeWorkflowId];
    return entry.saved_revision >= 2;
  }, storageKey);
  report.reference_workflow = (await snapshot()).activeWorkflowId;
  await checkLayout("saved-explicit-references");

  await page.getByRole("button", { name: "工作流", exact: true }).click();
  await page.locator(".workbench-workflow-item").filter({ has: page.locator("strong").filter({ hasText: seed.cache.catalog[0].title }) }).first().dblclick();
  await stable();
  await page.getByRole("button", { name: "内容", exact: true }).click();
  await page.waitForFunction(expected => document.querySelector('.current-content-sidebar [aria-label="打开酒馆前端"]')?.getAttribute("href") === expected,
    report.original_link);
  const beforeDraft = await snapshot();
  const oldEntry = JSON.stringify(beforeDraft.graph.entries[seed.cache.activeWorkflowId]);
  const mutatingBefore = mutationCount();
  await launch.getByRole("button", { name: "创建酒馆示例" }).click();
  await stable();
  const created = await snapshot(), fresh = created.graph.entries[created.activeWorkflowId];
  assert.notEqual(created.activeWorkflowId, seed.cache.activeWorkflowId);
  assert.equal(JSON.stringify(created.graph.entries[seed.cache.activeWorkflowId]), oldEntry);
  assert.equal(fresh.saved_revision, 0); assert.equal(fresh.session_id, null); assert.equal(fresh.pending, null);
  const model = fresh.document.nodes.find(row => row.component_id === "models.source");
  assert.equal(model.config.reference.resource_id, ""); assert.equal(model.config.parameters.model, "");
  assert.equal(await launch.locator('[aria-label="打开酒馆前端"]').getAttribute("href"), null);
  assert.equal(mutationCount(), mutatingBefore);
  report.new_draft_commands = mutationCount() - mutatingBefore;
  report.new_draft = created.activeWorkflowId;
  await checkLayout("new-unconfigured-tavern-draft");
  assert.equal(report.native_calls, false);
  assert.deepEqual(report.page_errors, []); assert.deepEqual(report.asset_errors, []);
  assert.deepEqual(report.console_errors, []);
  assert.deepEqual(report.overflow, []); assert.deepEqual(report.overlaps, []);
  fs.writeFileSync(path.join(seed.evidence, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ width: seed.width, evidence: seed.evidence, book: report.book_id, copy: report.prompt_copy }));
})().catch(async error => {
  report.failure = String(error?.stack ?? error);
  if (page) await page.screenshot({ path: path.join(seed.evidence, "failure.png"), fullPage: true }).catch(() => {});
  fs.writeFileSync(path.join(seed.evidence, "result.json"), JSON.stringify(report, null, 2));
  console.error(error); process.exitCode = 1;
}).finally(async () => { if (browser) await browser.close(); });
