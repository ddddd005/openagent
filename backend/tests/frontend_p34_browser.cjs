"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "../.."), dist = path.join(root, "frontend/dist");
const [fixturePath, evidencePath] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(fixturePath ?? path.join(root, ".local/current-node-catalog.json"), "utf8").replace(/^\uFEFF/, ""));
const evidence = evidencePath ?? path.join(root, ".local/p34-browser"), base = "http://127.0.0.1:5183";
const clone = value => JSON.parse(JSON.stringify(value));
const uuid = n => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const address = { scope: "management", kind: "workbench", key: "workbench:v1",
  workflow_definition_id: null, workflow_session_id: null };
const records = new Map(), draftsReceipts = new Map(), privateReceipts = new Map(), credentials = new Map();
const commands = [], errors = [], unexpected = [], report = { scenarios: [], screenshots: [], layouts: [] };
let browser, page, loseKeyResponse = true, secretRequests = 0;
function provider(n, protocol) {
  return { envelope_version: 1, scope: "workspace", type_id: "workflow.chat-provider", resource_id: uuid(n),
    data_schema_version: protocol === "chat" ? 1 : 2, update_sequence: 1, value: {
      name: protocol === "chat" ? "Browser Chat" : "Browser Gemini", protocol,
      base_url: "https://mock.invalid/v1", enabled: true,
      credential_ref: protocol === "chat" ? "env:DEEPSEEK_API_KEY" : "env:GEMINI_API_KEY",
    } };
}
const providers = [provider(301, "chat"), provider(302, "gemini")];
const identity = p => Object.fromEntries(["envelope_version", "scope", "type_id", "resource_id"].map(k => [k, p[k]]));
function source(n, protocol) {
  const type = fixture.node_types.find(row => row.component_id === "models.source" && row.component_version === (protocol === "chat" ? "2" : "4"));
  assert.ok(type);
  return { node_binding_id: uuid(n), component_id: type.component_id, component_version: type.component_version,
    title: protocol === "chat" ? "Chat source" : "Gemini source", position: { x: protocol === "chat" ? 70 : 420, y: 80 },
    config: { reference: identity(providers[protocol === "chat" ? 0 : 1]), parameters: {
      model: protocol === "chat" ? "browser-chat" : "gemini-2.5-flash", thinking: "disabled", max_tokens: 1024, stream: false,
    }, capacity: { context_window_tokens: 100000, output_reserve_tokens: 1024, summary_max_tokens: 128, max_cold_input_tokens: 100000 } } };
}
const docA = { schema_version: 2, workflow_definition_id: uuid(101), revision: 1, name: "Main flow",
  nodes: [source(11, "chat"), source(12, "gemini")], edges: [], execution_roots: [], object_bindings: [], control_edges: [],
  package_lock: clone(fixture.execution_package_lock ?? fixture.package_lock) };
const docB = { ...clone(docA), workflow_definition_id: uuid(102), name: "Other flow", nodes: [] };
const documents = new Map([[docA.workflow_definition_id, clone(docA)], [docB.workflow_definition_id, clone(docB)]]);
const session = { schema_version: 2, execution_model: "graph", workflow_session_id: uuid(201),
  workflow_definition_id: docA.workflow_definition_id, definition_revision: 1, revision: 1, data_revision: 0,
  head_revision: 1, status: "idle", can_submit: true, nodes: [], outputs: [], chains: [],
  data: { revision: 0, values: {} }, available_actions: [] };
function seed(target, value, sequence = 1) {
  const record = { schema_version: 1, kind: "workflow.local-draft", address: clone(target), format_version: 1,
    sequence, updated_at: "2026-10-09T00:00:00Z", value: clone(value), deleted: value === null };
  records.set(JSON.stringify(target), record); return record;
}
seed(address, { schemaVersion: 7, kind: "graph-workbench", revision: 1, savedAt: "2026-10-09T00:00:00Z",
  activeWorkflowId: docA.workflow_definition_id, selectedWorkflowId: docA.workflow_definition_id,
  catalog: [docA, docB].map(doc => ({ id: doc.workflow_definition_id, title: doc.name, state: "saved",
    description: "", nodeCount: doc.nodes.length })),
  graph: { schema_version: 1, entries: Object.fromEntries([docA, docB].map(doc => [
    doc.workflow_definition_id, { document: clone(doc), saved_document: clone(doc), saved_revision: 1,
      session_id: doc === docA ? session.workflow_session_id : null, pending: null },
  ])) } });
function workbench() { return records.get(JSON.stringify(address)).value; }
function active() { const state = workbench(); return state.graph.entries[state.activeWorkflowId]; }
function credentialStatus(reference) {
  return { schema_version: 1, kind: "workflow.credential-status", credential_ref: reference,
    generation: credentials.get(reference)?.generation ?? 0,
    state: reference.startsWith("env:") || credentials.get(reference)?.state === "saved" ? "saved" : "missing" };
}
function result(operation, parameters, value, accepted) {
  return { schema_version: 1, kind: "workflow.application-command", result: clone(value), receipt: {
    schema_version: 1, kind: "workflow.application-receipt", operation, operation_scope: "management",
    idempotency_key: parameters.idempotency_key, authority: "service_receipt", request_sha256: "a".repeat(64),
    target: parameters.session_id ? { session_id: parameters.session_id } : {}, accepted,
  } };
}
function draftResult(parameters, record) {
  return result("draft.save", parameters, record, { draft_address: record.address, draft_sequence: record.sequence,
    draft_format_version: 1, deleted: record.deleted });
}
async function waitFor(predicate, message) {
  const deadline = Date.now() + 12000;
  while (!predicate()) { assert.ok(Date.now() < deadline, message); await new Promise(done => setTimeout(done, 25)); }
}
async function open(name) {
  const row = page.locator(".workbench-workflow-item").filter({ has: page.getByText(name, { exact: true }) });
  await row.click();
  const old = await page.locator(".workbench-current-workflow h2").textContent();
  if (old !== name) {
    await page.getByRole("button", { name: "切换到 " + name, exact: true }).click();
    await page.waitForFunction(name => document.querySelector(".workbench-current-workflow h2")?.textContent === name, name);
  }
}
async function shot(name) {
  const layout = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
    overflow: [...document.querySelectorAll("button,input,select")].filter(el => el.getBoundingClientRect().width > 0
      && el.closest(".current-provider-panel,.tavern-chat-panel,.workflow-rename"))
      .filter(el => el.scrollWidth > el.clientWidth + 3 && el.tagName === "BUTTON").map(el => el.textContent) }));
  assert.deepEqual(layout.overflow, []);
  if (layout.width >= 1024) assert.ok(layout.scroll <= layout.width + 1);
  report.layouts.push({ name, ...layout });
  await page.screenshot({ path: path.join(evidence, name + ".png"), fullPage: true });
  report.screenshots.push(name + ".png");
}
(async () => {
  fs.mkdirSync(evidence, { recursive: true });
  browser = await chromium.launch({ headless: true, ...(process.env.TAVERN_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route("**/*", async route => {
    const req = route.request(), url = new URL(req.url());
    if (url.origin !== base) { unexpected.push(url.href); return route.abort(); }
    if (url.pathname.startsWith("/api/")) {
      const body = req.postDataJSON(), { operation, parameters: p } = body;
      if (url.pathname === "/api/graph/queries") {
        let value;
        if (operation === "catalog.node-types") value = fixture;
        else if (operation === "draft.read") value = records.get(JSON.stringify(p.address)) ?? null;
        else if (operation === "draft.list") value = { schema_version: 1, kind: "workflow.local-drafts",
          records: [...records.values()].filter(row => !p.kind || p.kind === row.address.kind) };
        else if (operation === "resource.list") value = p.type_id === "workflow.chat-provider" ? providers : [];
        else if (operation === "definition.sessions") value = p.workflow_definition_id === docA.workflow_definition_id ? [session] : [];
        else if (operation === "session.read") value = session;
        else if (operation === "definition.read") value = documents.get(p.workflow_definition_id);
        else if (operation === "credential.protection") value = { schema_version: 1, kind: "workflow.credential-protection", available: true, mode: "current-user" };
        else if (operation === "credential.status") value = credentialStatus(p.credential_ref);
        else if (operation === "credential.receipt") value = privateReceipts.get(p.request.idempotency_key) ?? null;
        else if (operation === "provider.models.list") {
          const provider = providers.find(row => row.resource_id === p.identity.resource_id);
          assert.equal(p.expected_sequence, provider.update_sequence);
          value = { schema_version: 1, kind: "workflow.provider-models", reference: p.identity,
            update_sequence: provider.update_sequence, protocol: provider.value.protocol,
            models: [{ model: "gemini-2.5-flash", adapted: true, input_token_limit: 500000,
              output_token_limit: 8192, context_window_tokens: null },
            { model: "future-unknown", adapted: false, input_token_limit: null, output_token_limit: null, context_window_tokens: null }] };
        } else throw new Error("Unexpected query " + operation);
        return route.fulfill({ json: clone(value) });
      }
      if (url.pathname === "/api/graph/receipts/read") {
        const prior = draftsReceipts.get(p.idempotency_key);
        assert.ok(prior); return route.fulfill({ json: { schema_version: 1, kind: "workflow.application-receipt-read",
          outcome: "matched", reason_code: "receipt_matched", receipt: prior.receipt, result: prior.result } });
      }
      commands.push(operation);
      if (operation === "draft.save") {
        assert.ok(!JSON.stringify(p).includes("fake-browser-key"), "Key must never enter public draft persistence");
        const existing = records.get(JSON.stringify(p.address));
        if ((existing?.sequence ?? 0) !== p.expected_sequence) return route.fulfill({ status: 409, json: { error: { code: "stale_draft_sequence" } } });
        const saved = seed(p.address, p.value, p.expected_sequence + 1), response = draftResult(p, saved);
        draftsReceipts.set(p.idempotency_key, response); return route.fulfill({ json: response });
      }
      if (operation === "credential.save") {
        secretRequests++; assert.equal(p.secret, "fake-browser-key");
        assert.equal(p.expected_generation, 0);
        credentials.set(p.credential_ref, { generation: 1, state: "saved" });
        const saved = credentialStatus(p.credential_ref);
        privateReceipts.set(p.idempotency_key, saved);
        if (loseKeyResponse) { loseKeyResponse = false; return route.abort(); }
        return route.fulfill({ json: result(operation, p, saved, saved) });
      }
      if (operation === "resource.save") {
        const index = providers.findIndex(row => row.resource_id === p.record.resource_id);
        assert.equal(providers[index].update_sequence, p.expected_sequence);
        providers[index] = clone(p.record);
        return route.fulfill({ json: result(operation, p, p.record, { reference: identity(p.record), update_sequence: p.record.update_sequence }) });
      }
      if (operation === "definition.save") {
        assert.equal(p.expected_revision, documents.get(p.document.workflow_definition_id)?.revision ?? 0);
        documents.set(p.document.workflow_definition_id, clone(p.document));
        return route.fulfill({ json: result(operation, p, p.document, { workflow_definition_id: p.document.workflow_definition_id,
          revision: p.document.revision, definition_revision: p.document.revision, package_lock: p.document.package_lock }) });
      }
      if (operation === "session.rebind") {
        assert.equal(p.expected_revision, session.revision);
        Object.assign(session, { definition_revision: p.definition_revision, revision: session.revision + 1 });
        const accepted = Object.fromEntries(["workflow_session_id", "workflow_definition_id", "definition_revision",
          "revision", "data_revision", "head_revision", "status"].map(key => [key, session[key]]));
        return route.fulfill({ json: result(operation, p, session, accepted) });
      }
      throw new Error("Unexpected business mutation " + operation);
    }
    const file = path.resolve(dist, "." + (url.pathname === "/" ? "/index.html" : decodeURIComponent(url.pathname)));
    assert.ok(file.startsWith(dist + path.sep));
    return route.fulfill({ contentType: ({ ".html": "text/html", ".js": "text/javascript", ".css": "text/css" })[path.extname(file)]
      ?? "application/octet-stream", body: fs.readFileSync(file) });
  });
  page = await context.newPage(); page.on("pageerror", error => errors.push(String(error)));
  page.on("dialog", dialog => dialog.accept());
  await page.goto(base);
  await page.locator('.vue-flow__node[data-id="' + uuid(11) + '"]').waitFor();
  await open("Other flow"); assert.equal(workbench().graph.entries[docA.workflow_definition_id].document.name, "Main flow");
  await open("Main flow");
  report.scenarios.push("Single selection plus explicit switch preserves the other workflow");
  await page.getByRole("button", { name: "编辑工作流名称", exact: true }).click();
  await page.getByRole("textbox", { name: "工作流名称", exact: true }).fill("Edited flow");
  await page.locator(".workflow-rename").getByRole("button", { name: "应用", exact: true }).click();
  await waitFor(() => active().document.name === "Edited flow", "Same-identity name edit not saved locally");
  assert.equal(workbench().catalog.length, 2); assert.equal(active().document.workflow_definition_id, docA.workflow_definition_id);
  await page.getByRole("button", { name: "保存工作流", exact: true }).click();
  await waitFor(() => active().saved_revision === 2, "Publication did not complete");
  assert.equal(session.definition_revision, 1);
  assert.ok(!commands.includes("session.copy") && !commands.includes("session.rebind"));
  report.scenarios.push("Normal editing keeps identity; publishing r2 leaves selected session on r1");
  await page.getByRole("button", { name: "采用已保存修订", exact: true }).click();
  await shot("revision-confirmation-desktop");
  await page.getByRole("button", { name: "确认采用", exact: true }).click();
  await waitFor(() => session.definition_revision === 2, "Explicit revision adoption failed");
  report.scenarios.push("Session adoption is a separate confirmed rebind operation");
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  await page.locator(".current-provider-panel li button").filter({ hasText: "Browser Chat" }).click();
  const key = page.getByRole("textbox", { name: "API Key", exact: true });
  await key.fill("fake-browser-key");
  await page.getByRole("button", { name: "保存 Key", exact: true }).click();
  await page.getByRole("button", { name: "核实原 Key 请求", exact: true }).waitFor();
  assert.equal(await key.inputValue(), "");
  assert.equal(await page.getByRole("button", { name: "保存当前资源", exact: true }).isDisabled(), true);
  await shot("private-key-pending-desktop");
  await page.reload();
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  await page.locator(".current-provider-panel li button").filter({ hasText: "Browser Chat" }).click();
  await page.getByRole("button", { name: "核实原 Key 请求", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector(".current-provider-panel button[type=submit]")?.disabled);
  await page.getByRole("button", { name: "保存当前资源", exact: true }).click();
  await waitFor(() => providers[0].data_schema_version === 3, "Private Key reference not explicitly published");
  assert.equal(secretRequests, 1); assert.ok(providers[0].value.credential_ref.startsWith("local:"));
  assert.ok(!JSON.stringify([...records.values()]).includes("fake-browser-key"));
  report.scenarios.push("Lost Key response survives reload; original receipt reconciled with no Key replay; binding saved separately");
  await page.getByRole("button", { name: "内容", exact: true }).click();
  await page.getByRole("button", { name: "创建酒馆示例", exact: true }).click();
  await waitFor(() => active().document.nodes.length === 12 && active().saved_revision === 0, "Chat tavern draft not created");
  assert.equal(active().document.nodes.find(n => n.component_id === "models.source").component_version, "2");
  assert.equal(active().document.nodes.find(n => n.component_id === "agents.execute").component_version, "4");
  report.scenarios.push("Chat preset uses valid current config and creates a detached complete 12-node draft without running");
  await page.getByRole("button", { name: "工作流", exact: true }).click(); await open("Edited flow");
  await page.getByRole("button", { name: "内容", exact: true }).click();
  await page.getByLabel("酒馆模型协议", { exact: true }).selectOption("gemini");
  await page.getByRole("button", { name: "创建酒馆示例", exact: true }).click();
  await waitFor(() => active().document.nodes.some(n => n.component_id === "agents.execute" && n.component_version === "8"),
    "Gemini tavern draft not created");
  assert.equal(active().document.nodes.length, 12); assert.equal(active().document.object_bindings.length, 2);
  await page.getByRole("button", { name: "定位全部节点", exact: true }).click();
  const modelId = active().document.nodes.find(n => n.component_id === "models.source").node_binding_id;
  await page.locator(`.vue-flow__node[data-id="${modelId}"]`).click();
  const inspector = page.locator(".graph-inspector");
  assert.equal(await inspector.getByText("Browser Chat", { exact: false }).count(), 0);
  await inspector.getByRole("button", { name: "获取上游模型列表", exact: true }).click();
  const choices = inspector.getByLabel("上游模型列表", { exact: true });
  await choices.waitFor();
  assert.equal(await choices.locator('option[value="future-unknown"]').isDisabled(), true);
  await choices.selectOption("gemini-2.5-flash");
  assert.equal(await inspector.getByLabel("输出预留 tokens", { exact: true }).inputValue(), "8192");
  assert.equal(await inspector.getByLabel("上下文窗口 tokens", { exact: true }).inputValue(), "100000");
  report.scenarios.push("Gemini preset uses exact v4/v8; protocol filtering and discovery preserve independent limits and unknown-model refusal");
  await shot("gemini-models-desktop");
  await page.setViewportSize({ width: 1024, height: 768 }); await shot("gemini-models-1024");
  await page.setViewportSize({ width: 390, height: 844 }); await shot("gemini-models-390");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByLabel("酒馆预置模型配置", { exact: true }).selectOption("unconfigured");
  const previous = workbench().activeWorkflowId;
  await page.getByRole("button", { name: "创建酒馆示例", exact: true }).click();
  await waitFor(() => workbench().activeWorkflowId !== previous, "Unconfigured draft not created");
  assert.equal(active().document.nodes.find(n => n.component_id === "models.source").config.parameters.model, "");
  const unconfigured = workbench().activeWorkflowId;
  await page.getByRole("button", { name: "放弃工作流修改", exact: true }).click();
  await waitFor(() => !workbench().graph.entries[unconfigured], "Unpublished workflow was not really discarded");
  report.scenarios.push("Explicit unconfigured template is not called runnable and discarding really removes its unpublished draft");
  assert.ok(!commands.includes("run.start") && !commands.includes("event.submit"), "Acceptance must never run a model or tool");
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
  Object.assign(report, { page_errors: errors, unexpected_requests: unexpected, secret_requests: secretRequests,
    business_commands: commands.filter(operation => operation !== "draft.save") });
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; })
  .finally(async () => { if (browser) await browser.close(); });
