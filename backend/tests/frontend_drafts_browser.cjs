"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const [fixturePath, evidencePath] = process.argv.slice(2);
const root = path.resolve(__dirname, "../.."), dist = path.join(root, "frontend/dist");
const evidence = evidencePath ?? path.join(root, ".local/p12-browser");
const fixture = JSON.parse(fs.readFileSync(fixturePath ?? path.join(root, ".local/current-node-catalog.json"), "utf8").replace(/^\uFEFF/, ""));
const clone = value => JSON.parse(JSON.stringify(value));
const uuid = value => `00000000-0000-4000-8000-${String(value).padStart(12, "0")}`;
const workflowA = uuid(101), workflowB = uuid(102), workflowC = uuid(103), sessionC = uuid(203);
const regexId = uuid(11), textId = uuid(12), presentationId = uuid(32);
const base = "http://127.0.0.1:5183";
const workbenchAddress = { scope: "management", kind: "workbench", key: "workbench:v1",
  workflow_definition_id: null, workflow_session_id: null };
const records = new Map(), receipts = new Map(), calls = [];
const report = { scenarios: [], screenshots: [], page_errors: [], unexpected_requests: [], layouts: [] };
let browser, page, refuseForm = false;

if (!fixture.frontend_extensions.some(row => row.extension_id === "workflow.tavern.presentation-fields")) {
  fixture.frontend_extensions.push({ schema_version: 2, host_protocol_version: 1,
    extension_id: "workflow.tavern.presentation-fields", kind: "field-editor",
    entrypoint: "workflow.tavern.workbench.presentation-fields", component_id: "tavern.chat.presentation",
    component_version: "1", package_id: "workflow.tavern", package_version: "1.2.0",
    binding: { surface: "workbench", slot: "node-fields",
      target: { component_id: "tavern.chat.presentation", component_version: "1" } } });
}
function node(component, id, title, x, y) {
  const type = fixture.node_types.find(row => row.component_id === component);
  assert.ok(type, component);
  return { node_binding_id: id, component_id: component, component_version: type.component_version,
    config: clone(type.default_config), title, position: { x, y },
    public_outputs: component === "tavern.chat.presentation" ? ["display"] : [] };
}
function document(id, name, nodes) {
  return { schema_version: 2, workflow_definition_id: id, revision: 1, name, nodes, edges: [],
    object_bindings: [], package_lock: clone(fixture.execution_package_lock ?? fixture.package_lock),
    control_edges: [] };
}
const documents = [
  document(workflowA, "Drafts A", [
    node("tools.regex", regexId, "Raw JSON", 60, 60),
    node("tools.text", textId, "Long Text", 410, 60),
    node("tools.current-input", uuid(13), "Current Input", 60, 300),
    node("tavern.chat.presentation", uuid(14), "Unsaved Tavern", 410, 300),
  ]),
  document(workflowB, "Drafts B", [node("tools.text", uuid(21), "Other Text", 60, 60)]),
  document(workflowC, "Saved Tavern", [node("tools.current-input", uuid(31), "Current Input", 60, 60),
    node("tavern.chat.presentation", presentationId, "Tavern Entry", 410, 60)]),
];
function session(document, id) {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: id,
    workflow_definition_id: document.workflow_definition_id, definition_revision: 1, revision: 1,
    data_revision: 0, head_revision: 1, status: "idle", can_submit: true,
    nodes: document.nodes.map(row => ({ node_binding_id: row.node_binding_id, label: row.title,
      status: "idle", run_id: null, revision: null, outputs: {}, diagnostic: null })),
    chains: [], outputs: [], data: { revision: 0, values: {} }, available_actions: [] };
}
const sessions = [session(documents[2], sessionC)];
function seed(address, value, sequence = 1) {
  const record = { schema_version: 1, kind: "workflow.local-draft", address: clone(address),
    format_version: 1, sequence, updated_at: "2026-10-09T00:00:00Z",
    value: clone(value), deleted: value === null };
  records.set(JSON.stringify(address), record);
  return record;
}
seed(workbenchAddress, { schemaVersion: 7, kind: "graph-workbench", revision: 1,
  savedAt: "2026-10-09T00:00:00Z", activeWorkflowId: workflowA, selectedWorkflowId: workflowA,
  catalog: documents.map(doc => ({ id: doc.workflow_definition_id, title: doc.name,
    description: "", nodeCount: doc.nodes.length, state: doc.workflow_definition_id === workflowC ? "saved" : "draft" })),
  graph: { schema_version: 1, entries: Object.fromEntries(documents.map((doc, index) => [
    doc.workflow_definition_id, { document: clone(doc), saved_revision: index === 2 ? 1 : 0,
      ...(index === 2 ? { saved_document: clone(doc) } : {}),
      session_id: index === 2 ? sessionC : null, pending: null },
  ])) } });
function workbench() { return records.get(JSON.stringify(workbenchAddress)).value; }
function form(key) { return [...records.values()].find(row => row.address.kind === "form" && row.address.key === key); }
const regexKey = `node:${workflowA}:${regexId}:schema:tools.regex@1`;
const textKey = `node:${workflowA}:${textId}:schema:tools.text@1`;
function applicationReceipt(parameters, record, reconcile = false) {
  const receipt = { schema_version: 1, kind: "workflow.application-receipt", operation: "draft.save",
    operation_scope: "management", idempotency_key: parameters.idempotency_key,
    request_sha256: "a".repeat(64), authority: "service_receipt", target: {},
    accepted: { draft_address: record.address, draft_sequence: record.sequence,
      draft_format_version: record.format_version, deleted: record.deleted } };
  return reconcile ? { schema_version: 1, kind: "workflow.application-receipt-read",
    outcome: "matched", reason_code: "receipt_matched", receipt, result: record }
    : { schema_version: 1, kind: "workflow.application-command", receipt, result: record };
}
async function waitFor(check, description) {
  const deadline = Date.now() + 10000;
  while (!check()) {
    assert.ok(Date.now() < deadline, description);
    await new Promise(resolve => setTimeout(resolve, 30));
  }
}
async function ready() {
  await page.getByRole("button", { name: "创建串行 Agent 示例", exact: true }).waitFor();
  await page.waitForFunction(() => !window.document.querySelector('[aria-label="创建串行 Agent 示例"]').disabled);
  await page.locator(`.vue-flow__node[data-id="${regexId}"]`).waitFor();
}
async function select(id) {
  await page.getByRole("button", { name: "定位全部节点", exact: true }).click();
  await page.locator(`.vue-flow__node[data-id="${id}"]`).click();
  await page.locator('.graph-inspector').getByLabel("节点 UUID", { exact: true }).waitFor({ state: "attached" });
  await page.waitForFunction(id => window.document.querySelector(".graph-inspector input[readonly]")?.value === id, id);
  const advanced = page.locator(".graph-schema-fields details").filter({ has: page.getByLabel("flags 高级 JSON", { exact: true }) });
  if (await advanced.count() && !await advanced.evaluate(element => element.open)) await advanced.locator("summary").click();
}
async function openWorkflow(name) {
  await page.getByRole("button", { name: "工作流", exact: true }).click();
  await page.locator(".workbench-workflow-item").filter({ has: page.getByText(name, { exact: true }) }).dblclick();
  await page.waitForFunction(name => window.document.querySelector(".workbench-current-workflow h2")?.textContent === name, name);
}
async function screenshot(name) {
  const geometry = await page.evaluate(() => ({
    width: innerWidth, scrollWidth: window.document.documentElement.scrollWidth,
    shellMinWidth: getComputedStyle(window.document.querySelector(".workbench-shell")).minWidth,
    fieldOverflows: [...window.document.querySelectorAll(".graph-schema-fields textarea,.current-prompt-panel textarea")]
      .filter(element => element.getBoundingClientRect().width > 0)
      .filter(element => element.scrollWidth > element.clientWidth + 2).map(element => element.closest("label")?.textContent),
  }));
  assert.deepEqual(geometry.fieldOverflows, []);
  if (geometry.width >= 1024) assert.ok(geometry.scrollWidth <= geometry.width + 1, JSON.stringify(geometry));
  report.layouts.push({ name, ...geometry });
  await page.screenshot({ path: path.join(evidence, name + ".png"), fullPage: true });
  report.screenshots.push(name + ".png");
}
(async () => {
  fs.mkdirSync(evidence, { recursive: true });
  browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname === "/tavern/" && url.origin === "http://127.0.0.1:8765")
      return route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Bound Tavern</title>" });
    if (url.origin !== base) { report.unexpected_requests.push(url.href); return route.abort(); }
    if (url.pathname.startsWith("/api/")) {
      const body = request.postDataJSON(); calls.push({ path: url.pathname, body: clone(body) });
      const { operation, parameters } = body;
      if (url.pathname === "/api/graph/queries") {
        let value;
        if (operation === "catalog.node-types") value = fixture;
        else if (operation === "draft.read") value = records.get(JSON.stringify(parameters.address)) ?? null;
        else if (operation === "draft.list") value = { schema_version: 1, kind: "workflow.local-drafts",
          records: [...records.values()].filter(row => (!parameters.kind || row.address.kind === parameters.kind)
            && (!parameters.workflow_definition_id || row.address.workflow_definition_id === parameters.workflow_definition_id)) };
        else if (operation === "resource.list") value = [];
        else if (operation === "definition.sessions") value = sessions.filter(row => row.workflow_definition_id === parameters.workflow_definition_id);
        else if (operation === "session.read") value = sessions.find(row => row.workflow_session_id === parameters.session_id);
        else throw new Error("Unexpected management query: " + operation);
        return route.fulfill({ json: clone(value) });
      }
      assert.equal(operation, "draft.save", "browser interaction must not invoke a model or business mutation");
      const prior = receipts.get(parameters.idempotency_key);
      if (url.pathname === "/api/graph/receipts/read") {
        if (!prior) return route.fulfill({ json: { schema_version: 1, kind: "workflow.application-receipt-read",
          outcome: "unresolved", reason_code: "request_outcome_unknown" } });
        assert.deepEqual(prior.parameters, parameters);
        return route.fulfill({ json: applicationReceipt(parameters, prior.record, true) });
      }
      assert.equal(url.pathname, "/api/graph/commands");
      if (refuseForm && parameters.address.key === regexKey) return route.fulfill({ status: 403,
        json: { error: { code: "draft_write_failed", message: "Injected write rejection" } } });
      if (prior) {
        assert.deepEqual(prior.parameters, parameters);
        return route.fulfill({ json: applicationReceipt(parameters, prior.record) });
      }
      const sequence = records.get(JSON.stringify(parameters.address))?.sequence ?? 0;
      if (sequence !== parameters.expected_sequence) return route.fulfill({ status: 409,
        json: { error: { code: "stale_draft_sequence", message: "Other page saved this draft" } } });
      const record = seed(parameters.address, parameters.value, sequence + 1);
      receipts.set(parameters.idempotency_key, { parameters: clone(parameters), record: clone(record) });
      return route.fulfill({ json: applicationReceipt(parameters, record) });
    }
    const filename = path.resolve(dist, "." + (url.pathname === "/" ? "/index.html" : decodeURIComponent(url.pathname)));
    assert.ok(filename.startsWith(dist + path.sep), filename);
    const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
      ".png": "image/png", ".ico": "image/x-icon" }[path.extname(filename)] ?? "application/octet-stream";
    return route.fulfill({ contentType: mime, body: fs.readFileSync(filename) });
  });
  page = await context.newPage();
  page.on("pageerror", error => report.page_errors.push(String(error)));
  page.on("dialog", dialog => dialog.accept());
  await page.goto(base); await ready();
  assert.equal(await page.evaluate(() => localStorage.getItem("workflow-workbench:fixed-base:v1")), null);
  report.scenarios.push("App restores the exact workbench from named local draft storage");

  await select(regexId);
  const invalid = '["unfinished';
  await page.locator(".graph-inspector").getByLabel("flags 高级 JSON", { exact: true }).fill(invalid);
  await waitFor(() => form(regexKey)?.value?.raw?.flags === invalid, "Invalid JSON did not reach local draft storage");
  await select(textId);
  const longText = "Unsubmitted long node text {{literal}}\n".repeat(400);
  await page.locator(".graph-inspector").getByLabel("text", { exact: true }).fill(longText);
  await waitFor(() => form(textKey)?.value?.raw?.text === longText, "Long node text did not reach local draft storage");
  await select(regexId);
  assert.equal(await page.getByLabel("flags 高级 JSON", { exact: true }).inputValue(), invalid);
  await openWorkflow("Drafts B"); await openWorkflow("Drafts A"); await select(regexId);
  assert.equal(await page.getByLabel("flags 高级 JSON", { exact: true }).inputValue(), invalid);
  assert.deepEqual(workbench().graph.entries[workflowA].document.nodes.find(row => row.node_binding_id === regexId).config.flags, []);
  await select(textId);
  assert.equal(await page.getByLabel("text", { exact: true }).inputValue(), longText);
  report.scenarios.push("Invalid node JSON and long text survive node and workflow switches without a business submission");

  await page.getByRole("button", { name: "内容", exact: true }).click();
  await page.getByRole("button", { name: "新增提示词资源", exact: true }).click();
  await page.getByRole("button", { name: "新增条目", exact: true }).click();
  const promptText = "Unsubmitted global prompt content\n".repeat(500);
  await page.getByLabel("条目 1 正文", { exact: true }).fill(promptText);
  await waitFor(() => form("resource-panel:prompt")?.value?.raw?.form?.value.members[0]?.text === promptText, "Prompt form was not stored");
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  await page.getByRole("button", { name: "内容", exact: true }).click();
  assert.equal(await page.getByLabel("条目 1 正文", { exact: true }).inputValue(), promptText);
  await page.reload(); await ready();
  await page.getByRole("button", { name: "内容", exact: true }).click();
  await page.getByLabel("条目 1 正文", { exact: true }).waitFor();
  assert.equal(await page.getByLabel("条目 1 正文", { exact: true }).inputValue(), promptText);
  report.scenarios.push("Unsubmitted global prompt survives content/provider unmount and cold page reload");
  await screenshot("p12-desktop-prompt");

  await openWorkflow("Saved Tavern"); await select(presentationId);
  const launch = page.getByRole("link", { name: "打开酒馆前端", exact: true });
  await launch.waitFor();
  const href = new URL(await launch.getAttribute("href"));
  assert.equal(href.searchParams.get("graph_workflow"), workflowC);
  assert.equal(href.searchParams.get("graph_session"), sessionC);
  const popupPromise = context.waitForEvent("page");
  await launch.click();
  const popup = await popupPromise;
  await popup.waitForURL(url => url.pathname === "/tavern/");
  assert.equal(new URL(popup.url()).searchParams.get("graph_workflow"), workflowC);
  assert.equal(new URL(popup.url()).searchParams.get("graph_session"), sessionC);
  await popup.close();
  report.scenarios.push("The exact tavern presentation node opens its current workflow/session without creating or running anything");

  await openWorkflow("Drafts A"); await select(regexId);
  refuseForm = true;
  await page.getByLabel("flags 高级 JSON", { exact: true }).fill('["write failure');
  await page.getByRole("button", { name: "工作流", exact: true }).click();
  await page.locator(".workbench-workflow-item").filter({ has: page.getByText("Drafts B", { exact: true }) }).dblclick();
  await page.getByText(/draft_write_failed/).first().waitFor();
  assert.equal(await page.locator(".workbench-current-workflow h2").textContent(), "Drafts A");
  assert.equal(workbench().activeWorkflowId, workflowA);
  refuseForm = false;
  await openWorkflow("Drafts B");
  await openWorkflow("Drafts A"); await select(regexId);
  assert.equal(await page.getByLabel("flags 高级 JSON", { exact: true }).inputValue(), '["write failure');
  report.scenarios.push("A rejected local form write prevents workflow navigation; an explicit later retry preserves the editor contents");

  const original = clone(form(regexKey));
  const foreignValue = clone(original.value); foreignValue.raw.flags = '["other page"]';
  seed(original.address, foreignValue, original.sequence + 1);
  const localConflict = '["local conflict';
  await page.getByLabel("flags 高级 JSON", { exact: true }).fill(localConflict);
  await waitFor(() => [...records.values()].some(row => row.address.key.startsWith(regexKey + ":conflict:")
    && row.value?.raw?.flags === localConflict), "CAS conflict copy was not preserved");
  assert.equal(form(regexKey).value.raw.flags, '["other page"]');
  await page.getByText("其他页面更新了原草稿，当前编辑已独立保存；未覆盖任何一方", { exact: true }).waitFor();
  await screenshot("p12-desktop-conflict");
  report.scenarios.push("CAS conflict keeps the other page's record and saves local input under an independent conflict address");
  await page.getByRole("button", { name: "收起左侧栏", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await screenshot("p12-narrow-conflict");
  report.scenarios.push("390px screenshot records the existing 1024px desktop minimum width rather than claiming mobile support");
  assert.deepEqual(report.page_errors, []);
  assert.deepEqual(report.unexpected_requests, []);
  assert.ok(calls.filter(row => row.path === "/api/graph/commands").every(row => row.body.operation === "draft.save"));
  assert.equal(await page.evaluate(() => localStorage.getItem("workflow-workbench:fixed-base:v1")), null);
  report.management_requests = calls.length;
  report.business_mutations = 0;
  report.record_count = records.size;
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ scenarios: report.scenarios.length, screenshots: report.screenshots.length,
    business_mutations: 0, records: records.size, errors: 0 }));
})().catch(async failure => {
  report.failure = String(failure.stack ?? failure);
  if (page) await page.screenshot({ path: path.join(evidence, "failure.png"), fullPage: true }).catch(() => {});
  fs.mkdirSync(evidence, { recursive: true });
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.error(failure); process.exitCode = 1;
}).finally(async () => { if (browser) await browser.close(); });
