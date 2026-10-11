"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");
const { chromium } = require("playwright");
const { createLocalDraftPort } = require("./local_draft_port.cjs");
const [seedPath, evidence] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(seedPath, "utf8").replace(/^\uFEFF/, ""));
const dist = path.resolve(__dirname, "../../frontend/dist"), base = "http://127.0.0.1:5188";
const clone = value => JSON.parse(JSON.stringify(value));
const providers = [], profiles = [], credentials = new Map(), accepted = new Map();
const report = { scenarios: [], page_errors: [], unexpected: [], key_saves: 0, provider_config_saves: 0,
  provider_receipt_reads: 0, profile_saves: 0, profile_dispatches: 0, layouts: [] };
let loseProfileResponse = false, unknownProfile = false, archiveFailure = false, browser;
let holdProviderResponse = false, releaseProviderResponse, loseProviderResponse = false;
let partialProviderSave = false;
const drafts = createLocalDraftPort();
const identity = record => Object.fromEntries(["envelope_version", "scope", "type_id", "resource_id"].map(key => [key, record[key]]));
function envelope(operation, request, result, reconciliation = false) {
  const receipt = { schema_version: 1, kind: "workflow.application-receipt", operation,
    operation_scope: "management", idempotency_key: request.idempotency_key,
    request_sha256: "a".repeat(64), authority: "service_receipt", target: {}, accepted: clone(result) };
  return reconciliation ? { schema_version: 1, kind: "workflow.application-receipt-read",
    outcome: "matched", reason_code: "receipt_matched", receipt, result }
    : { schema_version: 1, kind: "workflow.application-command", receipt, result };
}
async function waitFor(predicate, label) {
  const deadline = Date.now() + 12000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, label);
    await new Promise(resolve => setTimeout(resolve, 25));
  }
}
async function screenshot(page, name) {
  const layout = await page.locator(".current-provider-panel").evaluate(panel => ({
    viewport: innerWidth, width: panel.getBoundingClientRect().width,
    overflow: [...panel.querySelectorAll("button,input,select")].filter(el => {
      const box = el.getBoundingClientRect();
      return box.width > 0 && el.scrollWidth > el.clientWidth + 3;
    }).map(el => el.getAttribute("aria-label") || el.textContent.trim()),
  }));
  assert.deepEqual(layout.overflow, [], "Provider controls must fit");
  report.layouts.push({ name, ...layout });
  await page.screenshot({ path: path.join(evidence, name + ".png"), fullPage: true });
}
(async () => {
  browser = await chromium.launch({ headless: true, ...(process.env.TAVERN_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    try {
      assert.equal(url.origin, base, "No external connections permitted");
      if (!url.pathname.startsWith("/api/")) {
        const relative = url.pathname.replace(/^\/workbench\//, "/");
        const file = path.join(dist, relative === "/" ? "index.html" : relative.replace(/^\//, ""));
        assert.ok(file.startsWith(dist + path.sep));
        return route.fulfill({ path: file, contentType: file.endsWith(".js") ? "text/javascript"
          : file.endsWith(".css") ? "text/css" : "text/html" });
      }
      const body = request.postDataJSON(), op = body.operation, p = body.parameters;
      if (op.startsWith("draft.")) {
        if (op === "draft.save" && p.address.key.startsWith("archive:") && archiveFailure)
          return route.fulfill({ status: 500, json: { error: { reason_code: "internal_error" } } });
        return route.fulfill({ json: await drafts.operation(op, p, url.pathname.includes("/receipts/")) });
      }
      if (url.pathname === "/api/graph/queries") {
        let result;
        if (op === "catalog.node-types") result = fixture;
        else if (op === "resource.list") result = p.type_id === "workflow.chat-provider" ? providers
          : p.type_id === "workflow.model-profile" ? profiles : [];
        else if (op === "resource.read") result = providers.find(row => row.resource_id === p.identity.resource_id) ?? null;
        else if (op === "credential.protection") result = { schema_version: 1,
          kind: "workflow.credential-protection", available: true, mode: "current-user" };
        else if (op === "credential.status") result = credentials.get(p.credential_ref) ?? {
          schema_version: 1, kind: "workflow.credential-status", credential_ref: p.credential_ref, generation: 0, state: "missing" };
        else if (op === "provider.credential.receipt") {
          report.provider_receipt_reads++;
          const original = accepted.get(p.request.idempotency_key);
          assert.ok(original, "Original provider save must be recorded");
          assert.equal(original.op, "provider.credential.save");
          assert.deepEqual(p.request, original.request, "Provider reconciliation must preserve the entire original draft");
          result = original.result;
        }
        else if (op === "provider.models.list") result = { schema_version: 1, kind: "workflow.provider-models",
          reference: p.identity, update_sequence: p.expected_sequence, protocol: "chat", models: [{
            model: "discovered-chat", adapted: true, input_token_limit: 100000,
            output_token_limit: 50000, context_window_tokens: 200000 }] };
        else if (op === "registration.list") result = { items: [], next_cursor: null };
        else if (op === "definition.list") result = [];
        else if (op === "definition.sessions") result = [];
        else throw new Error("Unexpected query " + op);
        return route.fulfill({ json: clone(result) });
      }
      if (url.pathname.includes("/receipts/")) {
        const previous = accepted.get(p.idempotency_key);
        if (!previous) return route.fulfill({ json: { schema_version: 1,
          kind: "workflow.application-receipt-read", outcome: "unresolved", reason_code: "application_identity_missing" } });
        assert.equal(op, previous.op);
        assert.deepEqual(p, previous.request, "Reconciliation must keep original body");
        return route.fulfill({ json: envelope(op, p, previous.result, true) });
      }
      assert.equal(url.pathname, "/api/graph/commands");
      if (op === "provider.credential.save") {
        report.key_saves++;
        const existing = providers.findIndex(row => row.resource_id === p.identity.resource_id);
        assert.equal(p.expected_sequence, existing < 0 ? 0 : providers[existing].update_sequence);
        assert.equal(p.expected_generation, credentials.get(p.credential_ref)?.generation ?? 0);
        assert.ok(p.secret);
        assert.deepEqual(Object.keys(p.provider_seed).sort(), ["base_url", "enabled", "name", "protocol"]);
        const credential = { schema_version: 1, kind: "workflow.credential-status",
          credential_ref: p.credential_ref, generation: p.expected_generation + 1, state: "saved" };
        credentials.set(p.credential_ref, credential);
        const row = { ...p.identity, data_schema_version: 3, update_sequence: p.expected_sequence + 1,
          value: { ...p.provider_seed, credential_ref: p.credential_ref } };
        const partial = partialProviderSave;
        partialProviderSave = false;
        if (!partial) {
          if (existing < 0) providers.push(row); else providers[existing] = row;
        }
        const result = { schema_version: 1, kind: "workflow.provider-credential-save",
          outcome: partial ? "partial" : "saved",
          reason_code: partial ? "provider_binding_not_applied" : "provider_credential_saved", credential,
          provider: partial ? null : { reference: p.identity, update_sequence: row.update_sequence, deleted: false } };
        const { secret, ...original } = p;
        accepted.set(p.idempotency_key, { op, request: clone(original), result: clone(result) });
        if (holdProviderResponse) {
          holdProviderResponse = false;
          await new Promise(resolve => { releaseProviderResponse = resolve; });
        }
        if (loseProviderResponse) { loseProviderResponse = false; return route.abort("failed"); }
        return route.fulfill({ json: envelope(op, p, result) });
      }
      if (op === "resource.save") {
        if (p.record.type_id === "workflow.chat-provider") {
          report.provider_config_saves++;
          const existing = providers.findIndex(row => row.resource_id === p.record.resource_id);
          assert.ok(existing >= 0, "This scenario updates an existing provider without replacing its Key");
          assert.equal(p.expected_sequence, providers[existing].update_sequence);
          assert.equal(p.record.value.credential_ref, providers[existing].value.credential_ref);
          providers[existing] = clone(p.record);
          const result = { reference: identity(p.record), update_sequence: p.record.update_sequence, deleted: false };
          accepted.set(p.idempotency_key, { op, request: clone(p), result: clone(result) });
          return route.fulfill({ json: envelope(op, p, result) });
        }
        assert.equal(p.record.type_id, "workflow.model-profile", "Key action must not require extra provider resource save");
        report.profile_dispatches++;
        if (unknownProfile) { unknownProfile = false; return route.abort("failed"); }
        report.profile_saves++;
        const existing = profiles.findIndex(row => row.resource_id === p.record.resource_id);
        assert.equal(p.expected_sequence, existing < 0 ? 0 : profiles[existing].update_sequence);
        if (existing < 0) profiles.push(clone(p.record)); else profiles[existing] = clone(p.record);
        const result = { reference: identity(p.record), update_sequence: p.record.update_sequence, deleted: false };
        accepted.set(p.idempotency_key, { op, request: clone(p), result: clone(result) });
        if (loseProfileResponse) { loseProfileResponse = false; return route.abort("failed"); }
        return route.fulfill({ json: envelope(op, p, result) });
      }
      throw new Error("Unexpected command " + op);
    } catch (error) {
      report.unexpected.push(error.message);
      await route.abort();
    }
  });
  const page = await context.newPage();
  page.on("pageerror", error => report.page_errors.push(error.message));
  page.on("dialog", dialog => dialog.accept());
  await page.goto(base + "/");
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  const providerPanel = page.locator(".current-provider-panel"), panel = page.locator(".model-profiles-panel");
  await providerPanel.getByRole("button", { name: "新增供应商" }).click();
  const providerForm = providerPanel.locator("form").first();
  assert.equal(await providerForm.getByLabel("后端凭据引用", { exact: true }).count(), 0);
  assert.equal(await providerPanel.getByText(/env:(DEEPSEEK|GEMINI)_API_KEY/).count(), 0);
  await providerForm.getByLabel("供应商协议", { exact: true }).selectOption("gemini");
  assert.equal(await providerForm.getByLabel("服务地址", { exact: true }).inputValue(),
    "https://generativelanguage.googleapis.com/v1beta");
  assert.equal(await providerForm.getByLabel("后端凭据引用", { exact: true }).count(), 0);
  await providerForm.getByLabel("供应商协议", { exact: true }).selectOption("chat");
  await screenshot(page, "single-private-key-route");
  report.scenarios.push("single-private-key-route");
  const keyInput = providerForm.getByLabel("API Key", { exact: true });
  assert.equal(await keyInput.getAttribute("type"), "password");
  assert.equal(await keyInput.getAttribute("autocomplete"), "off");
  assert.equal(await providerForm.getAttribute("autocomplete"), "off");
  assert.equal(await providerForm.getByRole("button", { name: "保存 Key", exact: true }).count(), 0);
  assert.equal(await providerForm.getByRole("button", { name: "保存当前资源", exact: true }).count(), 0);
  assert.equal(await providerForm.getByRole("button", { name: "保存供应商", exact: true }).count(), 1);
  await keyInput.fill("dummy-browser-only-key");
  assert.equal(await providerForm.getByLabel("名称", { exact: true }).isEnabled(), true);
  assert.equal(await providerForm.getByLabel("服务地址", { exact: true }).isEnabled(), true);
  assert.equal(await providerForm.getByLabel("供应商协议", { exact: true }).isEnabled(), true);
  await providerForm.getByLabel("供应商协议", { exact: true }).selectOption("gemini");
  await providerForm.getByLabel("供应商协议", { exact: true }).selectOption("chat");
  await providerForm.getByLabel("名称", { exact: true }).fill("Browser provider");
  await providerForm.getByLabel("服务地址", { exact: true }).fill("https://offline.invalid/v1");
  assert.equal(await keyInput.inputValue(), "dummy-browser-only-key");
  await waitFor(() => drafts.calls.some(call => call.kind === "save"
    && JSON.stringify(call.parameters.value).includes("Browser provider")), "Non-secret provider draft is persisted");
  assert.equal(providers.length, 0);
  assert.equal(report.key_saves, 0);
  assert.equal(report.provider_config_saves, 0);
  assert.ok(!JSON.stringify(drafts.calls).includes("dummy-browser-only-key"));
  await screenshot(page, "editable-key-draft");
  report.scenarios.push("draft-only-until-save", "key-does-not-lock-or-reset-fields", "no-new-password-hint");
  await page.reload();
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  await providerForm.getByLabel("名称", { exact: true }).waitFor();
  assert.equal(await providerForm.getByLabel("名称", { exact: true }).inputValue(), "Browser provider");
  assert.equal(await providerForm.getByLabel("服务地址", { exact: true }).inputValue(), "https://offline.invalid/v1");
  assert.equal(await keyInput.inputValue(), "");
  assert.equal(providers.length, 0);
  await keyInput.fill("dummy-browser-only-key");
  report.scenarios.push("provider-draft-cold-reload-key-memory-only");
  await providerPanel.getByRole("button", { name: "保存供应商", exact: true }).click();
  await waitFor(() => providers.length === 1, "Provider Key must create effective provider");
  await providerPanel.getByText("供应商配置与 Key 已保存", { exact: true }).first().waitFor();
  assert.equal(await providerPanel.getByLabel("API Key", { exact: true }).inputValue(), "");
  assert.ok(providers[0].value.credential_ref.startsWith("local:"));
  assert.equal(providers[0].value.base_url, "https://offline.invalid/v1");
  report.scenarios.push("single-action-whole-provider");
  const reference = providers[0].value.credential_ref;
  await keyInput.fill("dummy-replacement-key");
  await providerForm.getByLabel("名称", { exact: true }).fill("Updated provider");
  await providerForm.getByLabel("服务地址", { exact: true }).fill("https://changed.invalid/v1");
  holdProviderResponse = true;
  await providerPanel.getByRole("button", { name: "保存供应商", exact: true }).click();
  await waitFor(() => releaseProviderResponse, "Provider save response is held for submission-lock checks");
  assert.equal(await providerForm.getByLabel("名称", { exact: true }).isEnabled(), false);
  assert.equal(await keyInput.isEnabled(), false);
  assert.equal(await providerPanel.getByRole("button", { name: "新增供应商" }).isEnabled(), false);
  releaseProviderResponse();
  await waitFor(() => providers[0].update_sequence === 2, "Complete provider update");
  await keyInput.waitFor();
  await page.waitForFunction(() => document.querySelector('input[aria-label="API Key"]').value === ""
    && !document.querySelector(".current-provider-panel fieldset").disabled);
  assert.equal(providers[0].value.name, "Updated provider");
  assert.equal(providers[0].value.base_url, "https://changed.invalid/v1");
  assert.equal(providers[0].value.credential_ref, reference);
  assert.equal(credentials.get(reference).generation, 2);
  report.scenarios.push("existing-config-and-key-together", "only-inflight-fields-locked");
  await providerForm.getByLabel("名称", { exact: true }).fill("Configuration only");
  await providerForm.getByLabel("服务地址", { exact: true }).fill("https://config-only.invalid/v1");
  await providerPanel.getByRole("button", { name: "保存供应商", exact: true }).click();
  await waitFor(() => providers[0].update_sequence === 3, "Configuration-only update");
  await providerPanel.getByText("供应商已保存", { exact: true }).waitFor();
  assert.equal(providers[0].value.base_url, "https://config-only.invalid/v1");
  assert.equal(credentials.get(reference).generation, 2);
  assert.equal(report.key_saves, 2);
  report.scenarios.push("blank-key-preserves-saved-key");
  await keyInput.fill("dummy-lost-response-key");
  await providerForm.getByLabel("服务地址", { exact: true }).fill("https://unknown.invalid/v1");
  loseProviderResponse = true;
  await keyInput.press("Enter");
  await providerPanel.getByRole("button", { name: "核实原 Key 请求", exact: true }).waitFor();
  const keySaves = report.key_saves;
  const draftText = JSON.stringify(drafts.calls);
  assert.ok(!draftText.includes("dummy-replacement-key") && !draftText.includes("dummy-lost-response-key"));
  await page.reload();
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  await providerPanel.getByRole("button", { name: "核实原 Key 请求", exact: true }).waitFor();
  assert.equal(await providerForm.getByLabel("服务地址", { exact: true }).isEnabled(), false);
  await providerPanel.getByRole("button", { name: "核实原 Key 请求", exact: true }).click();
  await providerPanel.getByRole("button", { name: "核实原 Key 请求", exact: true }).waitFor({ state: "hidden" });
  assert.equal(report.key_saves, keySaves);
  assert.equal(providers[0].value.base_url, "https://unknown.invalid/v1");
  assert.equal(await providerForm.getByLabel("服务地址", { exact: true }).inputValue(), "https://unknown.invalid/v1");
  report.scenarios.push("enter-saves-whole-provider", "provider-cold-reconcile-no-replay");
  await keyInput.fill("dummy-partial-save-key");
  await providerForm.getByLabel("名称", { exact: true }).fill("Failed provider draft");
  await providerForm.getByLabel("服务地址", { exact: true }).fill("https://failed-save.invalid/v1");
  partialProviderSave = true;
  await providerPanel.getByRole("button", { name: "保存供应商", exact: true }).click();
  const partialFailure = "Key 已保存，但供应商配置尚未写入，保存未完成；请核实原请求。";
  await providerPanel.getByText(partialFailure, { exact: true }).waitFor();
  assert.equal(await providerPanel.getByRole("button", { name: "完成原供应商保存", exact: true }).count(), 0);
  assert.equal(providers[0].value.base_url, "https://unknown.invalid/v1");
  const savedGeneration = credentials.get(reference).generation, savedKeyCommands = report.key_saves;
  const savedProviderCommands = report.provider_config_saves, savedProvider = clone(providers[0]);
  assert.equal(await keyInput.inputValue(), "");
  assert.equal(await providerForm.getByLabel("名称", { exact: true }).isEnabled(), false);
  assert.ok(!JSON.stringify(drafts.calls).includes("dummy-partial-save-key"));
  await page.reload();
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  await providerPanel.getByRole("button", { name: "核实原 Key 请求", exact: true }).waitFor();
  assert.equal(await providerForm.getByLabel("名称", { exact: true }).inputValue(), "Failed provider draft");
  assert.equal(await providerForm.getByLabel("服务地址", { exact: true }).inputValue(), "https://failed-save.invalid/v1");
  await providerPanel.getByRole("button", { name: "核实原 Key 请求", exact: true }).click();
  await providerPanel.getByText(partialFailure, { exact: true }).waitFor();
  assert.equal(await providerPanel.getByRole("button", { name: "完成原供应商保存", exact: true }).count(), 0);
  assert.equal(await providerPanel.getByRole("button", { name: "核实原 Key 请求", exact: true }).isVisible(), true);
  assert.equal(report.key_saves, savedKeyCommands);
  assert.equal(report.provider_config_saves, savedProviderCommands);
  assert.equal(credentials.get(reference).generation, savedGeneration);
  assert.equal(await providerForm.getByLabel("名称", { exact: true }).isEnabled(), false);
  assert.deepEqual(providers[0], savedProvider);
  report.scenarios.push("partial-save-truthful-failure", "partial-cold-reconcile-original-no-mutation");
  await screenshot(page, "partial-save-original-reconciliation");
  const originalPartial = [...accepted.values()].find(row => row.op === "provider.credential.save"
    && row.request.provider_seed.name === "Failed provider draft");
  await providerPanel.locator(".provider-credential-fields")
    .getByRole("button", { name: "归档并解除锁定", exact: true }).click();
  await providerPanel.getByRole("button", { name: "核实原 Key 请求", exact: true }).waitFor({ state: "hidden" });
  const archived = drafts.calls.find(call => call.kind === "save"
    && call.parameters.address.key.startsWith("archive:provider-key:"));
  assert.ok(archived, "The original partial request must be archived before unlocking");
  assert.deepEqual(archived.parameters.value.request.request, originalPartial.request);
  assert.equal(report.key_saves, savedKeyCommands);
  assert.equal(report.provider_config_saves, savedProviderCommands);
  assert.equal(credentials.get(reference).generation, savedGeneration);
  assert.deepEqual(providers[0], savedProvider);
  assert.equal(await providerForm.getByLabel("名称", { exact: true }).inputValue(), "Failed provider draft");
  report.scenarios.push("partial-original-evidence-archive-no-mutation");
  await panel.getByRole("button", { name: "新增模型配置" }).click();
  await panel.getByLabel("配置名称", { exact: true }).fill("Browser profile");
  await panel.getByRole("button", { name: "获取上游模型列表" }).click();
  await panel.getByLabel("上游模型列表", { exact: true }).selectOption("discovered-chat");
  const number = label => panel.locator("label").filter({ hasText: label }).locator("input");
  assert.equal(await number("上下文窗口 tokens").inputValue(), "0");
  assert.equal(await number("摘要冷输入上限 tokens").inputValue(), "0");
  await number("上下文窗口 tokens").fill("32000");
  await number("摘要冷输入上限 tokens").fill("12000");
  report.scenarios.push("manual-capacity");
  await panel.getByRole("button", { name: "保存模型配置", exact: true }).click();
  await waitFor(() => profiles.length === 1, "Profile saved");
  await panel.locator("form").waitFor({ state: "hidden" });
  await panel.getByRole("button").filter({ hasText: "Browser profile" }).waitFor();
  await page.reload();
  await page.getByRole("button", { name: "供应商", exact: true }).click();
  await panel.getByRole("button").filter({ hasText: "Browser profile" }).click();
  assert.equal(await number("上下文窗口 tokens").inputValue(), "32000");
  assert.equal(await panel.getByLabel("模型名称", { exact: true }).inputValue(), "discovered-chat");
  report.scenarios.push("profile-roundtrip");
  await panel.getByLabel("配置名称", { exact: true }).fill("Verified profile");
  loseProfileResponse = true;
  await panel.getByRole("button", { name: "保存模型配置", exact: true }).click();
  await panel.getByRole("button", { name: "核实原请求", exact: true }).waitFor();
  await panel.getByRole("button", { name: "核实原请求", exact: true }).click();
  await panel.locator("form").waitFor({ state: "hidden" });
  await panel.getByRole("button").filter({ hasText: "Verified profile" }).waitFor();
  assert.equal(report.profile_saves, 2);
  report.scenarios.push("receipt-only-recovery");
  await panel.getByRole("button").filter({ hasText: "Verified profile" }).click();
  await panel.getByLabel("配置名称", { exact: true }).fill("Unknown edited profile");
  unknownProfile = true;
  await panel.getByRole("button", { name: "保存模型配置", exact: true }).click();
  await panel.getByRole("button", { name: "核实原请求", exact: true }).waitFor();
  const dispatches = report.profile_dispatches;
  archiveFailure = true;
  await panel.getByRole("button", { name: "归档并解除锁定", exact: true }).click();
  await panel.getByText(/internal_error/).waitFor();
  await waitFor(() => drafts.calls.some(call => call.kind === "read" && call.address.key.startsWith("archive:resource-request")),
    "Failed archive is reconciled without clearing original");
  assert.ok(drafts.value("models:profiles:pending:v1", null).request);
  assert.equal(report.profile_dispatches, dispatches);
  report.scenarios.push("archive-failure-retains");
  archiveFailure = false;
  await panel.getByRole("button", { name: "归档并解除锁定", exact: true }).click();
  await panel.getByRole("button", { name: "归档并解除锁定", exact: true }).waitFor({ state: "hidden" });
  assert.equal(drafts.value("models:profiles:pending:v1", null).request, null);
  assert.equal(report.profile_dispatches, dispatches);
  report.scenarios.push("archive-unlocks");
  archiveFailure = true;
  await panel.getByRole("button", { name: "重新读取生效模型配置" }).click();
  await panel.getByText(/internal_error/).waitFor();
  assert.equal(await panel.getByLabel("配置名称", { exact: true }).inputValue(), "Unknown edited profile");
  report.scenarios.push("reload-form-archive-failure");
  archiveFailure = false;
  await panel.getByRole("button", { name: "重新读取生效模型配置" }).click();
  await panel.getByText("旧表单已归档，已读取生效模型配置", { exact: true }).waitFor();
  assert.equal(await panel.getByLabel("配置名称", { exact: true }).inputValue(), "Verified profile");
  report.scenarios.push("reload-form-explicit");
  await screenshot(page, "provider-profile-desktop");
  await page.setViewportSize({ width: 1024, height: 900 });
  await screenshot(page, "provider-profile-narrow");
  await page.setViewportSize({ width: 390, height: 844 });
  await screenshot(page, "provider-profile-mobile");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "工作流", exact: true }).click();
  await page.getByRole("button", { name: "创建串行 Agent 示例", exact: true }).click();
  const entry = () => Object.values(drafts.value("workbench:v1", null).graph.entries).find(row =>
    row.document.nodes.some(node => node.component_id === "models.source"));
  await waitFor(() => entry(), "Current batch demo persisted");
  const model = entry().document.nodes.find(node => node.component_id === "models.source");
  await page.locator(`.vue-flow__node[data-id="${model.node_binding_id}"] header`).click();
  await page.getByRole("button", { name: "配置", exact: true }).click();
  const fields = page.locator(".model-source-fields");
  await fields.getByLabel("全局模型配置", { exact: true }).selectOption({ label: "Verified profile · discovered-chat" });
  assert.equal(await fields.getByLabel("API Key").count(), 0);
  assert.equal(await fields.getByText("上下文窗口 tokens", { exact: true }).count(), 0);
  assert.equal(await fields.getByRole("button", { name: "获取上游模型列表" }).count(), 0);
  await fields.getByRole("button", { name: "应用模型引用", exact: true }).click();
  await waitFor(() => entry().document.nodes.find(node => node.node_binding_id === model.node_binding_id)
    .config.reference?.resource_id === profiles[0].resource_id, "Reference applied to workflow");
  assert.deepEqual(Object.keys(entry().document.nodes.find(node => node.node_binding_id === model.node_binding_id).config), ["reference"]);
  report.scenarios.push("reference-only-node", "desktop-and-narrow-layout");
  assert.deepEqual(report.page_errors, []);
  assert.deepEqual(report.unexpected, []);
  fs.writeFileSync(path.join(evidence, "browser-report.json"), JSON.stringify(report, null, 2));
})().catch(error => {
  console.error(error);
  console.error(JSON.stringify({ page_errors: report.page_errors, unexpected: report.unexpected }));
  process.exitCode = 1;
}).finally(async () => { if (browser) await browser.close(); });
