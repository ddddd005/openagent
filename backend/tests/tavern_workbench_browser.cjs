"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const [base, seedPath] = process.argv.slice(2);
const seed = JSON.parse(fs.readFileSync(seedPath, "utf8"));
const report = { width: seed.width, page_errors: [], asset_errors: [], requests: [], overflow: [], overlaps: [],
  console_errors: [], native_calls: false };
let browser, page;
const mutationCount = () => report.requests.filter(row => new URL(row.url).pathname.endsWith("/commands")
  && !row.body?.operation?.startsWith("request-evidence.")).length;
async function query(operation, parameters) {
  return page.evaluate(async body => {
    const response = await fetch("/api/graph/queries", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Query failed: ${response.status}`);
    return response.json();
  }, { operation, parameters });
}
async function active() {
  return { document: await query("definition.read", { identity: seed.workflow_id }) };
}
async function saveConfiguration(button) {
  const response = page.waitForResponse(value => {
    if (!value.url().endsWith("/api/graph/commands")) return false;
    return value.request().postDataJSON()?.operation === "definition.update";
  });
  await button.click();
  const accepted = await response;
  assert.equal(accepted.status(), 200);
  return (await accepted.json()).result;
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
      if (["session.create", "run.start", "run.control", "event.submit"].includes(body?.operation)
        || body?.operation?.startsWith("consumer."))
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
  await page.getByRole("heading", { name: seed.workflow_name, exact: true }).waitFor();
  for (const label of ["打开聊天前端", "打开酒馆前端", "创建酒馆工作流", "创建串行 Agent 示例", "创建上下文精简串行示例"])
    assert.equal(await page.getByLabel(label, { exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "前端展示", exact: true }).count(), 0);
  await page.getByRole("button", { name: "内容", exact: true }).click();
  const sidebar = page.locator(".current-content-sidebar");
  const lorebook = sidebar.getByRole("region", { name: "全局 Lorebook 资源" });
  const groups = sidebar.getByRole("region", { name: "新版提示词当前资源" });
  await lorebook.waitFor();
  await groups.waitFor();
  assert.equal(await sidebar.getByRole("region", { name: "酒馆聊天入口" }).count(), 0);
  assert.deepEqual(await query("definition.sessions", { workflow_definition_id: seed.workflow_id }), []);

  await lorebook.getByRole("button", { name: "新增 Lorebook 资源" }).click();
  const bookForm = lorebook.getByRole("form", { name: "全局 Lorebook 表单" });
  await bookForm.getByLabel("Lorebook 资源名称").fill("Workbench shared book");
  await bookForm.getByRole("button", { name: "新增条目", exact: true }).click();
  await bookForm.getByLabel("条目 1 正文").fill("WORKBENCH_BOOK_ORIGINAL");
  await bookForm.getByLabel("条目 1 模式").selectOption("constant");
  const createdBook = await bookForm.locator("label").filter({ hasText: "资源 UUID" }).locator("output").textContent();
  const bookEntry = await bookForm.locator(".lorebook-entry small").first().textContent();
  await checkLayout("lorebook-create");
  await bookForm.getByRole("button", { name: "创建全局 Lorebook", exact: true }).click();
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
  const originalMembers = await promptForm.locator(".prompt-member details code").allTextContents();
  await promptForm.getByRole("button", { name: "创建提示词资源", exact: true }).click();
  await promptForm.waitFor({ state: "detached" });
  await groups.getByRole("button", { name: `复制提示词组 workspace ${originalGroup.trim()}` }).click();
  const copiedGroup = await promptForm.locator("label").filter({ hasText: "资源 UUID" }).locator("output").textContent();
  const copiedMembers = await promptForm.locator(".prompt-member details code").allTextContents();
  assert.notEqual(copiedGroup, originalGroup);
  assert.ok(copiedMembers.every(id => !originalMembers.includes(id)));
  const beforeCopyReferences = await active();
  assert.equal(beforeCopyReferences.document.nodes.find(row => row.node_binding_id === seed.prompt_node).config.reference.resource_id,
    seed.original_prompt_id);
  await promptForm.getByLabel("条目 2 上移").click();
  assert.equal(await promptForm.getByLabel("条目 1 正文").inputValue(), "WORKBENCH_GROUP_SECOND");
  assert.equal(await promptForm.getByLabel("条目 1 生命周期").inputValue(), "context_once");
  assert.equal(await promptForm.getByLabel("条目 1 精简许可").inputValue(), "allowed");
  assert.equal(await promptForm.locator(".prompt-member details code").first().textContent(), copiedMembers[1]);
  await checkLayout("prompt-copy-reorder");
  await promptForm.getByRole("button", { name: "创建提示词资源", exact: true }).click();
  await promptForm.waitFor({ state: "detached" });
  report.prompt_original = originalGroup.trim(); report.prompt_copy = copiedGroup.trim();

  for (const nodeId of seed.lorebook_nodes) {
    await selectNode(nodeId);
    const fields = page.getByRole("form", { name: "全局 Lorebook 节点配置" });
    await fields.getByLabel("全局 Lorebook 选择").selectOption({
      value: JSON.stringify([1, "workspace", "workflow.tavern.lorebook", report.book_id]),
    });
    await saveConfiguration(fields.getByRole("button", { name: "保存 Lorebook 配置", exact: true }));
  }
  await selectNode(seed.prompt_node);
  const reference = page.getByRole("form", { name: "提示词引用配置" });
  await reference.getByLabel("提示词当前资源").selectOption({
    value: JSON.stringify([1, "workspace", "workflow.prompt-resource", report.prompt_copy]),
  });
  await saveConfiguration(reference.getByRole("button", { name: "保存提示词引用", exact: true }));
  const referenced = await active();
  assert.equal(referenced.document.nodes.find(row => row.node_binding_id === seed.prompt_node).config.reference.resource_id, report.prompt_copy);
  assert.ok(seed.lorebook_nodes.every(id => referenced.document.nodes.find(row => row.node_binding_id === id).config.reference.resource_id === report.book_id));
  assert.ok(referenced.document.revision >= 2);
  report.reference_workflow = seed.workflow_id;
  await checkLayout("saved-explicit-references");

  await page.getByRole("button", { name: "工作流", exact: true }).click();
  const oldEntry = JSON.stringify((await active()).document);
  const mutatingBefore = mutationCount();
  const created = await saveConfiguration(page.getByRole("button", { name: "新建工作流", exact: true }));
  report.new_workflow = created.workflow_definition_id;
  assert.notEqual(report.new_workflow, seed.workflow_id);
  assert.equal(JSON.stringify((await active()).document), oldEntry);
  const fresh = await query("definition.read", { identity: report.new_workflow });
  assert.deepEqual(fresh.nodes, []); assert.deepEqual(fresh.edges, []); assert.deepEqual(fresh.object_bindings, []);
  assert.deepEqual(await query("definition.sessions", { workflow_definition_id: report.new_workflow }), []);
  assert.equal(mutationCount(), mutatingBefore + 1);
  assert.equal(context.pages().length, 1);
  await checkLayout("ordinary-new-workflow");
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
