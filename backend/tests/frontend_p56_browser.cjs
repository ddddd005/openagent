"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");
const { chromium } = require("playwright");
const { createLocalDraftPort } = require("./local_draft_port.cjs");
const root = path.resolve(__dirname, "../.."), dist = path.join(root, "frontend/dist");
const [fixturePath, evidencePath] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const evidence = evidencePath ?? path.join(root, ".local/p56-browser");
const base = "http://127.0.0.1:5184", clone = value => JSON.parse(JSON.stringify(value));
const uid = n => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const drafts = createLocalDraftPort(), mutations = [], queries = [], errors = [], unexpected = [];
const report = { scenarios: [], screenshots: [], layouts: [] };
const address = { scope: "management", kind: "workbench", key: "workbench:v1",
  workflow_definition_id: null, workflow_session_id: null };
function node(component, version, n, x, y, patch = {}) {
  const type = fixture.node_types.find(row => row.component_id === component && row.component_version === version);
  assert.ok(type, component + "@" + version);
  return { node_binding_id: uid(n), component_id: component, component_version: version,
    title: component, config: { ...clone(type.default_config), ...patch }, position: { x, y }, public_outputs: [] };
}
const group = node("prompts.group", "2", 11, 60, 60);
group.config.members[0].metadata = { keep: "member evidence" };
const text = node("tools.text", "1", 12, 370, 60, { text: "Editable text" });
const currentInput = node("tools.current-input", "1", 13, 60, 400, { input_name: "message" });
const output = node("tools.output", "1", 14, 700, 60);
output.title = "Current edited title"; output.public_outputs = ["output"];
const doc = { schema_version: 2, workflow_definition_id: uid(101), revision: 2, name: "User flow",
  nodes: [group, text, currentInput, output], edges: [{ edge_id: uid(301),
    source_node_id: group.node_binding_id, source_port_id: "output",
    target_node_id: text.node_binding_id, target_port_id: "missing-old-port", order: 0 }],
  object_bindings: [], control_edges: [{ edge_id: uid(302),
    source_node_id: currentInput.node_binding_id, target_node_id: output.node_binding_id }],
  package_lock: clone(fixture.execution_package_lock ?? fixture.package_lock) };
const frozen = clone(doc); frozen.revision = 1; frozen.nodes[3].title = "Frozen historical title";
const chain = { chain_run_id: uid(401), workflow_definition_id: doc.workflow_definition_id,
  workflow_session_id: uid(201), definition_revision: 1, revision: 1, status: "succeeded",
  targets: [output.node_binding_id], completed_nodes: [output.node_binding_id], node_run_ids: [uid(402)], diagnostic: null };
const payload = { schema_version: 2, kind: "workflow.model-result", content: "Visible answer",
  thinking_summary: "Visible thinking summary", provider_metadata: { parts: [{ thoughtSignature: "NEVER-SHOW-SIGNATURE" }] } };
const artifact = { output_id: uid(403), node_binding_id: output.node_binding_id, port_id: "output",
  chain_run_id: chain.chain_run_id, payload };
const session = { schema_version: 2, execution_model: "graph", workflow_session_id: uid(201),
  workflow_definition_id: doc.workflow_definition_id, definition_revision: 1, revision: 2, data_revision: 1,
  head_revision: 2, status: "succeeded", can_submit: true, nodes: [
    { node_binding_id: output.node_binding_id, label: "Frozen historical title", status: "succeeded",
      run_id: uid(402), revision: 1, outputs: { output: payload }, diagnostic: null },
  ], outputs: [artifact], chains: [chain], selected_chain_run_id: chain.chain_run_id,
  data: { revision: 1, values: {} }, available_actions: [] };
const candidate = { candidate_id: uid(404), chain_run_id: chain.chain_run_id, source_session_id: session.workflow_session_id,
  source_workflow_definition_id: doc.workflow_definition_id, source_definition_revision: 1,
  data_revision: 1, selected: true, can_select: true, diagnostic: null };
let browser, page, missingDefinition = false, additionalSession = null;
function workbench() { return drafts.value("workbench:v1", null); }
function entry() { return workbench().graph.entries[doc.workflow_definition_id]; }
async function waitFor(predicate, message) {
  const deadline = Date.now() + 12000;
  while (!predicate()) { assert.ok(Date.now() < deadline, message); await new Promise(resolve => setTimeout(resolve, 25)); }
}
async function select(id) { await page.locator(`.vue-flow__node[data-id="${id}"] header`).click(); }
async function shot(name) {
  const layout = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
    overflow: [...document.querySelectorAll(".graph-input-dialog button,.graph-inspector button,.canvas-node-menu button")]
      .filter(el => el.getBoundingClientRect().width > 0 && el.scrollWidth > el.clientWidth + 3)
      .map(el => el.textContent) }));
  assert.deepEqual(layout.overflow, []);
  if (layout.width >= 1024) assert.ok(layout.scroll <= layout.width + 1);
  report.layouts.push({ name, ...layout });
  await page.screenshot({ path: path.join(evidence, name + ".png"), fullPage: true });
  report.screenshots.push(name + ".png");
}
async function handle(id, type, port) {
  const locator = page.locator(`.vue-flow__node[data-id="${id}"] .vue-flow__handle.${type}[data-handleid="${port}"]`);
  const box = await locator.boundingBox(); assert.ok(box);
  return { locator, box, x: box.x + box.width / 2, y: box.y + box.height / 2 };
}
async function drag(sourceId, targetId, offset = 0) {
  const a = await handle(sourceId, "source", "output"), b = await handle(targetId, "target", "input");
  await page.mouse.move(a.x + offset, a.y);
  await page.mouse.down();
  await page.mouse.move(a.x + 40, a.y + 25, { steps: 4 });
  return { a, b };
}
(async () => {
  fs.mkdirSync(evidence, { recursive: true });
  await drafts.operation("draft.save", { address, format_version: 1, expected_sequence: 0,
    idempotency_key: uid(999), value: { schemaVersion: 7, kind: "graph-workbench", revision: 1,
      savedAt: "2026-10-09T00:00:00Z", activeWorkflowId: doc.workflow_definition_id,
      selectedWorkflowId: doc.workflow_definition_id,
      catalog: [{ id: doc.workflow_definition_id, title: doc.name, state: "saved", description: "", nodeCount: 4 }],
      graph: { schema_version: 1, entries: { [doc.workflow_definition_id]: {
        document: clone(doc), saved_document: clone(doc), saved_revision: 2, session_id: session.workflow_session_id,
        external_inputs: { message: { schema_version: 2, kind: "workflow.text", text: "Original message" },
          undeclared: { keep: "original complex value" } }, pending: null,
      } } } } });
  browser = await chromium.launch({ headless: true, ...(process.env.TAVERN_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== base) { unexpected.push(url.href); return route.abort(); }
    if (url.pathname.startsWith("/api/")) {
      const { operation, parameters: p } = request.postDataJSON();
      if (operation.startsWith("draft.")) return route.fulfill({ json: await drafts.operation(operation, p) });
      if (url.pathname === "/api/graph/queries") {
        queries.push({ operation, parameters: clone(p) });
        let value;
        if (operation === "catalog.node-types") value = fixture;
        else if (operation === "resource.list") value = [];
        else if (operation === "definition.sessions") value = [session, ...(additionalSession ? [additionalSession] : [])];
        else if (operation === "session.read") value = session;
        else if (operation === "definition.inputs") value = { schema_version: 1, kind: "workflow.input-declarations",
          workflow_definition_id: doc.workflow_definition_id,
          inputs: [{ name: "message", data_type: "TEXT", required: true, conflict: false, node_ids: [currentInput.node_binding_id] }],
          required_for_run: null, diagnostics: [{ reason_code: "graph_required_input_missing", message: "Output is not connected yet" }] };
        else if (operation === "candidate.list") value = { schema_version: 1, kind: "workflow.graph-candidates",
          workflow_session_id: session.workflow_session_id, workflow_definition_id: doc.workflow_definition_id,
          definition_revision: 1, session_revision: session.revision, head_revision: session.head_revision, candidates: [candidate] };
        else if (operation === "run.read") value = { chain, outputs: [artifact], node_runs: [
          { run_id: uid(402), chain_run_id: chain.chain_run_id, workflow_session_id: session.workflow_session_id,
            node_binding_id: output.node_binding_id, status: "succeeded", input_values: {
              input: { schema_version: 2, kind: "workflow.text", text: "Historical input" } },
            input_refs: {}, output_refs: { output: artifact.output_id }, reads: [], effects: [], diagnostic: null },
        ] };
        else if (operation === "definition.read") {
          assert.equal(p.identity, doc.workflow_definition_id); assert.equal(p.revision, 1);
          if (missingDefinition) return route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Historical definition missing" } } });
          value = frozen;
        } else if (operation === "registration.list") value = { items: [], next_cursor: null };
        else throw new Error("Unexpected query " + operation);
        return route.fulfill({ json: clone(value) });
      }
      mutations.push({ operation, parameters: clone(p) });
      throw new Error("Unexpected business mutation " + operation);
    }
    const file = path.join(dist, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\//, ""));
    assert.ok(file.startsWith(dist + path.sep)); assert.ok(fs.existsSync(file));
    await route.fulfill({ path: file, contentType: file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html" });
  });
  page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  page.on("dialog", dialog => dialog.accept());
  await page.goto(base);
  await page.getByRole("button", { name: "添加节点", exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelectorAll(".vue-flow__edge-path").length === 2);
  assert.equal(await page.locator(".vue-flow__edge-path").evaluateAll(paths =>
    paths.filter(path => getComputedStyle(path).stroke === "rgb(209, 126, 126)").length), 1);
  report.scenarios.push("Loaded incompatible data edges remain visible in red alongside control edges");
  await select(group.node_binding_id);
  const body = page.getByLabel("条目 1 正文", { exact: true });
  await body.waitFor(); await body.fill("Unapplied prompt text");
  assert.equal(await page.locator('.graph-inspector input[readonly]').isVisible(), false);
  await shot("prompt-body-first");
  await select(text.node_binding_id); await select(group.node_binding_id);
  await body.waitFor(); assert.equal(await body.inputValue(), "Unapplied prompt text");
  await page.getByRole("button", { name: "应用条目", exact: true }).click();
  await waitFor(() => entry().document.nodes[0].config.members[0].text === "Unapplied prompt text", "Prompt patch did not persist");
  assert.equal(entry().document.nodes[0].config.members[0].id, group.config.members[0].id);
  assert.deepEqual(entry().document.nodes[0].config.members[0].metadata, group.config.members[0].metadata);
  await select(text.node_binding_id); await select(group.node_binding_id);
  assert.equal(await body.inputValue(), "Unapplied prompt text");
  assert.equal(await page.getByRole("button", { name: "应用条目", exact: true }).isEnabled(), true);
  report.scenarios.push("Prompt member editing, raw draft restore and apply preserve identity and metadata");

  const order = page.getByLabel("条目 1 装配顺序", { exact: true });
  const orderKey = `node:${doc.workflow_definition_id}:${group.node_binding_id}:prompt-members:member:${group.config.members[0].id}:order`;
  const appliedOrder = entry().document.nodes[0].config.members[0].presentation.order;
  await order.fill("1e-");
  await waitFor(() => drafts.value(orderKey, doc.workflow_definition_id)?.raw === "1e-", "Partial number was not preserved");
  assert.equal(await page.getByRole("button", { name: "应用条目", exact: true }).isDisabled(), true);
  await select(text.node_binding_id); await select(group.node_binding_id);
  assert.equal(await order.inputValue(), "1e-");
  assert.equal(entry().document.nodes[0].config.members[0].presentation.order, appliedOrder);
  await page.reload(); await page.getByRole("button", { name: "添加节点", exact: true }).waitFor();
  await select(group.node_binding_id);
  await page.waitForFunction(() => {
    const input = document.querySelector('[aria-label="条目 1 装配顺序"]');
    return input && !input.disabled;
  });
  assert.equal(await order.inputValue(), "1e-");
  assert.equal(await page.getByRole("button", { name: "应用条目", exact: true }).isDisabled(), true);
  await order.fill("5");
  await page.getByRole("button", { name: "应用条目", exact: true }).click();
  await waitFor(() => entry().document.nodes[0].config.members[0].presentation.order === 5, "Completed number was not applied");
  await select(text.node_binding_id); await select(group.node_binding_id);
  assert.equal(await order.inputValue(), "5");
  assert.equal(await page.getByRole("button", { name: "应用条目", exact: true }).isEnabled(), true);
  report.scenarios.push("Partial prompt numbers survive node switches and cold reload; incomplete values cannot be applied");

  await page.getByRole("button", { name: "添加节点", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索节点", exact: true }).fill("current-input");
  assert.equal(await page.locator(".canvas-node-menu [role=menuitem]").count(), 1);
  await shot("visible-add-search"); await page.keyboard.press("Escape");
  report.scenarios.push("Visible add button and family search without historical version menu");

  await page.getByRole("button", { name: "编辑本次输入", exact: true }).click();
  await page.getByLabel("运行输入 message", { exact: true }).waitFor();
  await page.getByLabel("运行输入 message", { exact: true }).fill("Form message");
  await page.locator(".graph-input-dialog").getByRole("button", { name: "应用", exact: true }).click();
  await waitFor(() => entry().external_inputs.message.text === "Form message", "Named input did not persist");
  assert.deepEqual(entry().external_inputs.undeclared, { keep: "original complex value" });
  await page.getByRole("button", { name: "编辑本次输入", exact: true }).click();
  assert.equal(await page.getByLabel("运行输入 message", { exact: true }).inputValue(), "Form message");
  await shot("named-inputs");
  await page.getByRole("button", { name: "关闭输入", exact: true }).click();
  report.scenarios.push("Declared inputs work for incomplete graphs and preserve unknown complex fields");

  await page.getByRole("button", { name: "定位全部节点", exact: true }).click();
  await page.waitForTimeout(300);
  const a = await handle(text.node_binding_id, "source", "output");
  assert.ok(a.box.width >= 10, "Zoomed hit box is still too small");
  await page.mouse.move(a.x + a.box.width * .35, a.y);
  assert.equal(await a.locator.evaluate(el => getComputedStyle(el).outlineStyle), "solid");
  let dragState = await drag(group.node_binding_id, output.node_binding_id);
  assert.ok(await dragState.b.locator.evaluate(el => el.classList.contains("incompatible")));
  await page.mouse.move(dragState.b.x, dragState.b.y, { steps: 8 }); await page.mouse.up();
  assert.equal(entry().document.edges.length, 1);
  dragState = await drag(text.node_binding_id, output.node_binding_id, a.box.width * .35);
  assert.ok(await dragState.b.locator.evaluate(el => el.classList.contains("compatible")));
  await page.mouse.move(dragState.b.x + dragState.b.box.width / 2 + 3, dragState.b.y, { steps: 8 });
  await page.mouse.up();
  await waitFor(() => entry().document.edges.length === 2, "Compatible endpoint snap did not connect");
  await page.waitForFunction(() => {
    const paths = [...document.querySelectorAll(".vue-flow__edge-path")];
    return paths.length === 3 && paths.every(path => path.getTotalLength() > 1);
  });
  assert.equal(await page.getByText("这条连线已存在", { exact: true }).count(), 0);
  await shot("connection-feedback");
  report.scenarios.push("Expanded hit box, hover ring, incompatibility and endpoint snap at fitted zoom");

  await page.getByRole("tab", { name: "运行历史", exact: true }).click();
  await page.getByText("Frozen historical title", { exact: false }).first().waitFor();
  assert.equal(await page.locator(".graph-history").getByText("Current edited title", { exact: false }).count(), 0);
  assert.ok(await page.getByText("Visible answer", { exact: true }).isVisible());
  assert.ok(await page.getByText("Historical input", { exact: true }).isVisible());
  assert.ok(!(await page.locator(".graph-history").textContent()).includes("NEVER-SHOW-SIGNATURE"));
  await page.locator(".graph-history .thinking-summary summary").click();
  await page.getByText("Visible thinking summary", { exact: true }).waitFor();
  const before = clone(entry().document);
  await page.getByRole("button", { name: "恢复候选完整状态", exact: true }).click();
  await page.getByRole("button", { name: "确认完整恢复", exact: true }).waitFor();
  assert.equal(mutations.length, 0); assert.deepEqual(entry().document, before);
  await page.getByRole("button", { name: "取消历史操作", exact: true }).click();
  await page.getByRole("button", { name: "从候选新建分叉会话", exact: true }).click();
  await page.getByRole("button", { name: "确认新建分叉会话", exact: true }).waitFor();
  assert.equal(mutations.length, 0);
  await page.getByRole("button", { name: "取消历史操作", exact: true }).click();
  await shot("frozen-history");
  report.scenarios.push("Frozen history titles, readable input/answer/summary and non-mutating confirmations");

  await page.locator(".graph-sessions summary").click();
  await page.locator(".graph-sessions").getByRole("textbox", { name: "本机显示名称", exact: true }).fill("Named conversation");
  await page.locator(".graph-sessions").getByRole("button", { name: "保存本机显示名称", exact: true }).click();
  await page.getByText("Named conversation", { exact: true }).waitFor();
  additionalSession = { ...clone(session), workflow_session_id: uid(202), status: "paused", can_submit: false };
  await page.getByRole("button", { name: "刷新会话", exact: true }).click();
  await page.locator(".graph-session-row").filter({ hasText: "已暂停" }).waitFor();
  const beforeArchive = drafts.calls.filter(row => row.kind === "save"
    && row.parameters.address.key.includes("management-label")).length;
  await page.getByRole("button", { name: "归档到本机目录", exact: true }).click();
  await page.getByText("会话未稳定或原请求待核实，暂不能归档", { exact: true }).waitFor();
  assert.equal(await page.locator(".workbench-workflow-list li").count(), 1);
  assert.equal(drafts.calls.filter(row => row.kind === "save"
    && row.parameters.address.key.includes("management-label")).length, beforeArchive);
  additionalSession = { ...additionalSession, revision: 3, status: "succeeded", can_submit: true };
  await page.getByRole("button", { name: "刷新会话", exact: true }).click();
  await page.locator(".graph-session-row").filter({ hasText: "已暂停" }).waitFor({ state: "hidden" });
  report.scenarios.push("Directory archive is blocked by another known paused session, not only the selected session");
  await page.getByRole("button", { name: "归档到本机目录", exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll(".workbench-workflow-list li").length === 0);
  assert.deepEqual(entry().document, before);
  await page.getByRole("button", { name: "显示已归档工作流", exact: true }).click();
  await page.getByRole("button", { name: "恢复到本机目录", exact: true }).click();
  await waitFor(() => [...drafts.calls].some(row => row.kind === "save"
    && row.parameters.address.key.includes("management-label") && row.parameters.value?.raw?.archived === false), "Restore not saved");
  report.scenarios.push("Local session naming and reversible directory archive retain facts and drafts");

  await page.reload(); await page.getByText("Named conversation", { exact: true }).waitFor();
  await select(group.node_binding_id); assert.equal(await body.inputValue(), "Unapplied prompt text");
  report.scenarios.push("Normal reload restores local labels and applied prompt form without replay");
  missingDefinition = true;
  await page.getByRole("tab", { name: "运行历史", exact: true }).click();
  await page.getByText("未使用当前编辑图替代。", { exact: false }).waitFor();
  assert.equal(await page.locator(".graph-history").getByText("Current edited title", { exact: false }).count(), 0);
  report.scenarios.push("Missing historical definition is diagnosed without substituting current titles");
  await page.getByRole("tab", { name: "主流程", exact: true }).click();
  for (const width of [1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.getByRole("button", { name: "编辑本次输入", exact: true }).click();
    const box = await page.locator(".graph-input-dialog").boundingBox(); assert.ok(box && box.x >= 0 && box.x + box.width <= width + 1);
    await shot("input-width-" + width);
    await page.getByRole("button", { name: "关闭输入", exact: true }).click();
  }
  report.scenarios.push("Input dialog fits desktop and narrow viewport; existing workbench minimum width recorded");
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); assert.deepEqual(mutations, []);
  report.page_errors = errors; report.unexpected_requests = unexpected; report.business_mutations = mutations;
  report.query_operations = [...new Set(queries.map(row => row.operation))];
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
})().catch(async error => {
  report.failure = String(error.stack ?? error);
  fs.mkdirSync(evidence, { recursive: true });
  if (page) await page.screenshot({ path: path.join(evidence, "failure.png"), fullPage: true }).catch(() => {});
  fs.writeFileSync(path.join(evidence, "result.json"), JSON.stringify(report, null, 2));
  console.error(error); process.exitCode = 1;
})
  .finally(async () => { if (browser) await browser.close(); });
