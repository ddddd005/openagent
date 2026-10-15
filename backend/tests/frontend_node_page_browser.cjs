const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const output = process.argv[2];
const root = path.resolve(__dirname, "../src/phase1_agent");
const origin = "http://127.0.0.1:5199";
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const owner = { workflow_definition_id: id(1), definition_revision: 1, node_binding_id: id(2), session_id: null };
const copy = value => JSON.parse(JSON.stringify(value));
const ownerPath = value => [value.workflow_definition_id, value.definition_revision, value.node_binding_id, value.session_id ?? "none"].join("/");
const urls = value => { const part = ownerPath(value); return {
  page_url: origin + "/frontend-node/" + part + "/", channel_url: origin + "/api/graph/frontend-nodes/" + part + "/channel",
  events_url: origin + "/api/graph/frontend-nodes/" + part + "/events",
}; };
const report = { errors: [], unexpected: [], operations: [], scenarios: [], layouts: [] };
const workflows = new Map([[id(1), { text: "正式输入", edit_sequence: 1, policy: "adjustable" }]]);
const sessions = new Map(), receipts = new Map(), pending = new Map();
let losePatch = false, createdCount = 0, startCount = 0, browser;
function packet(binding) {
  const session = binding.session_id === null ? null : sessions.get(binding.session_id);
  return { schema_version: 1, kind: "workflow.frontend-node-state", owner: copy(binding),
    input: copy(workflows.get(binding.workflow_definition_id)),
    run: session ? { revision: session.revision, status: session.status,
      can_start: ["idle", "succeeded", "closed"].includes(session.status), can_pause: session.status === "running",
      can_resume: session.status === "paused", can_close: session.status === "paused",
      can_fork: ["idle", "succeeded", "closed"].includes(session.status),
      ...(session.snapshot.schema_version === 2 ? { data_revision: 4, head_revision: 2,
        object_revision: session.snapshot.basis.revision } : {}) } : null,
    snapshot: session ? copy(session.snapshot) : null,
    pending: [...pending.values()].filter(record => JSON.stringify(record.request.owner) === JSON.stringify(binding)),
  };
}
function phase(binding, revision, status) {
  return { workflow_definition_id: binding.workflow_definition_id, definition_revision: binding.definition_revision,
    workflow_session_id: binding.session_id, revision, status, active_chain_run_id: status === "idle" ? null : id(40) };
}
function result(request, body) {
  return { schema_version: 1, kind: "workflow.frontend-node-result", owner: copy(request.owner),
    operation: request.operation, idempotency_key: request.parameters.idempotency_key, outcome: body.outcome, result: body };
}
function evidence(request) {
  return { schema_version: 1, kind: "workflow.request-evidence", operation: "frontend-node.patch-input",
    idempotency_key: request.parameters.idempotency_key, owner_kind: "frontend-node", owner_id: "exact-node-input",
    request: { owner: copy(request.owner), parameters: copy(request.parameters) },
    request_digest: "original-digest", resolution: "pending" };
}
function initialSnapshot(binding, message) {
  return { schema_version: 1, kind: "workflow.frontend-node-snapshot", owner: {
    workflow_definition_id: binding.workflow_definition_id, definition_revision: binding.definition_revision,
    node_binding_id: binding.node_binding_id, workflow_session_id: binding.session_id, object_key: "chat",
  }, basis: { revision: 2, revision_id: id(60) }, entries: [{
    entry_id: id(61), role: "user", source_ref: { scope: "artifact", output_id: id(62) },
    content: { schema_version: 2, kind: "workflow.text", text: message },
  }], source: { workflow_session_id: binding.session_id, node_binding_id: binding.node_binding_id,
    chain_run_id: id(40), node_run_id: id(63) } };
}
function logicalSnapshot(binding) {
  const value = initialSnapshot(binding, "");
  delete value.entries; delete value.source; value.schema_version = 2;
  value.chat = { rounds: [0, 1].map(index => ({
    round_id: id(100 + index), base_checkpoint_id: index ? id(90) : null, submitted_input: "原始输入 " + index,
    candidates: [0, 1].map(candidate => ({
      candidate_id: id(110 + index * 2 + candidate), result_checkpoint_id: id(120 + index * 2 + candidate),
      messages: [{ message_id: id(130 + index * 2 + candidate), role: "assistant",
        text: "完整候选回复 " + index + "-" + candidate }],
    })), selected_candidate_id: id(111 + index * 2),
  })) };
  return value;
}
function parseBinding(pathname, suffix) {
  const parts = pathname.slice("/api/graph/frontend-nodes/".length, -suffix.length).split("/");
  assert.equal(parts.length, 4);
  return { workflow_definition_id: parts[0], definition_revision: Number(parts[1]),
    node_binding_id: parts[2], session_id: parts[3] === "none" ? null : parts[3] };
}
async function screenshot(page, name) {
  const layout = await page.evaluate(() => ({
    width: innerWidth, documentWidth: document.documentElement.scrollWidth,
    overflow: [...document.querySelectorAll("button,textarea,.chat-round-controls select,.node-header,.composer-footer,.decision")].filter(element => {
      if (element.closest("[hidden]")) return false;
      const box = element.getBoundingClientRect();
      return box.width && (box.left < -1 || box.right > innerWidth + 1 || element.scrollWidth > element.clientWidth + 2);
    }).map(element => element.id || element.className),
    avatars: [...document.querySelectorAll("img")].every(image => image.complete && image.naturalWidth > 0),
  }));
  assert.ok(layout.documentWidth <= layout.width + 1, name + " horizontal overflow");
  assert.deepEqual(layout.overflow, [], name + " text/control overflow");
  assert.ok(layout.avatars, "Existing visual assets must render");
  report.layouts.push({ name, ...layout });
  await page.screenshot({ path: path.join(output, name + ".png"), fullPage: true });
}
(async () => {
  browser = await chromium.launch({ headless: true, ...(process.env.TAVERN_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addInitScript(() => {
    window.__nodeStreams = [];
    window.EventSource = class {
      constructor(url) {
        this.url = url; this.closed = false; this.handlers = {};
        window.__nodeStreams.push(this); queueMicrotask(() => this.onopen?.());
      }
      addEventListener(name, handler) { this.handlers[name] = handler; }
      close() { this.closed = true; }
      emit(name, value) { this.handlers[name]?.({ data: JSON.stringify(value) }); }
      disconnect() { this.onerror?.(); }
    };
  });
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    try {
      assert.equal(url.origin, origin, "No external network is allowed");
      if (!url.pathname.startsWith("/api/")) {
        let file;
        if (url.pathname.startsWith("/frontend-node/")) file = path.join(root, "static/frontend-node.html");
        else if (url.pathname.startsWith("/static/frontend-node")) file = path.join(root, "static", path.basename(url.pathname));
        else if (url.pathname === "/tavern/vendor/lucide/lucide.min.js") file = path.join(root, "tavern/frontend/vendor/lucide/lucide.min.js");
        else if (["/tavern/assets/assistant.png", "/tavern/assets/user-default.png"].includes(url.pathname))
          file = path.join(root, "tavern/frontend/assets", path.basename(url.pathname));
        else throw new Error("Unexpected static path " + url.pathname);
        return await route.fulfill({ path: file, contentType: file.endsWith(".js") ? "text/javascript"
          : file.endsWith(".css") ? "text/css" : file.endsWith(".png") ? "image/png" : "text/html" });
      }
      assert.ok(url.pathname.startsWith("/api/graph/frontend-nodes/"), "No generic graph API");
      if (url.pathname.endsWith("/entry")) {
        const binding = parseBinding(url.pathname, "/entry");
        return await route.fulfill({ json: { schema_version: 1, kind: "workflow.frontend-node-entry",
          owner: binding, ...urls(binding) } });
      }
      assert.ok(url.pathname.endsWith("/channel")); assert.equal(request.method(), "POST");
      const binding = parseBinding(url.pathname, "/channel"), body = request.postDataJSON();
      assert.deepEqual(body.owner, binding); report.operations.push(copy(body));
      if (body.operation === "state") return await route.fulfill({ json: packet(binding) });
      if (body.operation === "checkpoints") return await route.fulfill({ json: {
        schema_version: 1, kind: "workflow.frontend-node-checkpoints", owner: binding, result: {
          items: [{ checkpoint_id: id(90), parent_checkpoint_id: null },
            { checkpoint_id: id(91), parent_checkpoint_id: id(90) }],
          basis: { revision: sessions.get(binding.session_id).revision, data_revision: 0, head_revision: 1 },
        },
      } });
      if (body.operation === "checkpoint") return await route.fulfill({ json: {
        schema_version: 1, kind: "workflow.frontend-node-checkpoint", owner: binding, result: {
          schema_version: 3, checkpoint_id: body.parameters.checkpoint_id,
          workflow_definition_id: binding.workflow_definition_id, workflow_session_id: binding.session_id,
          fixed_definition_revision: binding.definition_revision,
          parent_checkpoint_id: body.parameters.checkpoint_id === id(90) ? null : id(90),
        },
      } });
      if (["receipt", "accept_receipt"].includes(body.operation)) {
        const original = body.parameters.original_request;
        assert.deepEqual(original.owner, binding);
        const saved = receipts.get(original.parameters.idempotency_key);
        assert.ok(saved, "Only an actual recorded original can reconcile");
        if (body.operation === "accept_receipt") pending.delete(original.parameters.idempotency_key);
        return await route.fulfill({ json: saved });
      }
      let value;
      if (body.operation === "patch_input") {
        const current = workflows.get(binding.workflow_definition_id);
        assert.equal(body.parameters.expected_edit_sequence, current.edit_sequence);
        if (current.policy === "fixed" && binding.session_id !== null) {
          if (!body.parameters.confirm_derivation) value = result(body, {
            outcome: "confirmation_required", edit_sequence: current.edit_sequence,
            definition_revision: binding.definition_revision, text: body.parameters.text, next_owner: null,
          });
          else {
            const next = { ...binding, workflow_definition_id: id(9), session_id: null };
            workflows.set(id(9), { text: body.parameters.text, edit_sequence: 1, policy: "fixed" });
            value = result(body, { outcome: "derived", edit_sequence: 1, definition_revision: 1,
              text: body.parameters.text, next_owner: next });
          }
        } else {
          current.text = body.parameters.text; current.edit_sequence++;
          const next = current.policy === "fixed" && binding.session_id === null
            ? { ...binding, definition_revision: binding.definition_revision + 1 } : null;
          value = result(body, { outcome: "saved", edit_sequence: current.edit_sequence,
            definition_revision: next?.definition_revision ?? binding.definition_revision,
            text: current.text, next_owner: next });
        }
        receipts.set(body.parameters.idempotency_key, copy(value));
        if (losePatch) {
          losePatch = false; pending.set(body.parameters.idempotency_key, evidence(body));
          return await route.fulfill({ status: 409, json: { error: { code: "conflict",
            reason_code: "frontend_node_result_mismatch", message: "保存已发生，但结果投影未完成",
            diagnostics: [{ code: "frontend_node_result_mismatch" }],
          } } });
        }
      } else if (body.operation === "start") {
        startCount++;
        const actual = binding.session_id === null ? { ...binding, session_id: id(3) } : binding;
        if (binding.session_id === null) createdCount++;
        sessions.set(actual.session_id, { revision: 3, status: "succeeded",
          snapshot: initialSnapshot(actual, workflows.get(binding.workflow_definition_id).text) });
        value = result(body, { outcome: "succeeded",
          create_result: binding.session_id === null ? phase(actual, 1, "idle") : null,
          start_result: phase(actual, 2, "prepared"), failure_phase: null, error: null,
          next_owner: binding.session_id === null ? actual : null });
      } else if (body.operation === "pause") {
        sessions.get(binding.session_id).revision++;
        sessions.get(binding.session_id).status = "pausing";
        value = result(body, { outcome: "succeeded", control_result: phase(binding,
          sessions.get(binding.session_id).revision, "running"), failure_phase: null, error: null, next_owner: null });
      } else if (["resume", "close"].includes(body.operation)) {
        const session = sessions.get(binding.session_id);
        assert.equal(body.parameters.expected_session_revision, session.revision);
        assert.equal(session.status, "paused");
        session.revision++; session.status = body.operation === "resume" ? "running" : "closed";
        value = result(body, { outcome: "succeeded",
          control_result: phase(binding, session.revision, session.status),
          failure_phase: null, error: null, next_owner: null });
      } else if (["reroll", "select_candidate", "fork_floor"].includes(body.operation)) {
        const session = sessions.get(binding.session_id), snapshot = session.snapshot;
        assert.equal(snapshot.schema_version, 2);
        assert.equal(body.parameters.expected_session_revision, session.revision);
        assert.equal(body.parameters.expected_data_revision, 4);
        assert.equal(body.parameters.expected_head_revision, 2);
        assert.equal(body.parameters.expected_object_revision, snapshot.basis.revision);
        assert.equal(Object.hasOwn(body.parameters, "object_key"), false);
        const index = snapshot.chat.rounds.findIndex(round => round.round_id === body.parameters.round_id);
        assert.ok(index >= 0); const round = snapshot.chat.rounds[index];
        if (body.operation === "fork_floor") {
          const next = { ...binding, session_id: id(15) }, target = copy(snapshot);
          target.owner.workflow_session_id = id(15);
          target.basis = { revision: 1, revision_id: id(150) };
          target.chat.rounds = target.chat.rounds.slice(0, index + 1);
          sessions.set(id(15), { revision: 1, status: "succeeded", snapshot: target });
          value = result(body, { outcome: "succeeded", fork_result: {
            target_session_id: id(15), target_checkpoint_id: id(151),
            target_commit_id: id(152), target_workflow_ref_id: id(153),
          }, failure_phase: null, error: null, next_owner: next });
        } else {
          assert.equal(index, snapshot.chat.rounds.length - 1);
          if (body.operation === "select_candidate") {
            assert.ok(round.candidates.some(candidate => candidate.candidate_id === body.parameters.candidate_id));
            round.selected_candidate_id = body.parameters.candidate_id;
          } else {
            assert.equal(round.submitted_input, "原始输入 1");
            round.candidates.push({ candidate_id: id(140), result_checkpoint_id: id(141),
              messages: [{ message_id: id(142), role: "assistant", text: "重新生成的完整正文" }] });
            round.selected_candidate_id = id(140);
          }
          session.revision++; snapshot.basis.revision++; snapshot.basis.revision_id = id(160 + session.revision);
          value = result(body, { outcome: "succeeded", control_result: phase(binding, session.revision, session.status),
            failure_phase: null, error: null, next_owner: null });
        }
      } else if (body.operation === "fork_checkpoint") {
        assert.equal(body.parameters.checkpoint_id, id(90));
        assert.equal(body.parameters.expected_session_revision, sessions.get(binding.session_id).revision);
        assert.equal(body.parameters.expected_data_revision, 0);
        assert.equal(body.parameters.expected_head_revision, 1);
        const next = { ...binding, session_id: id(5) };
        const snapshot = initialSnapshot(next, "");
        snapshot.basis = { revision: 1, revision_id: id(95) };
        snapshot.entries = []; snapshot.source = null;
        sessions.set(id(5), { revision: 1, status: "idle", snapshot });
        value = result(body, { outcome: "succeeded", fork_result: {
          target_session_id: id(5), target_checkpoint_id: id(92),
          target_commit_id: id(93), target_workflow_ref_id: id(94),
        }, failure_phase: null, error: null, next_owner: next });
      } else throw new Error("Unexpected operation " + body.operation);
      receipts.set(body.parameters.idempotency_key, copy(value));
      return await route.fulfill({ json: value });
    } catch (error) {
      report.unexpected.push(error.message);
      return await route.fulfill({ status: 500, json: { error: { code: "test_failure", message: error.message } } });
    }
  });
  const page = await context.newPage();
  page.on("pageerror", error => report.errors.push(error.message));
  await page.goto(urls(owner).page_url);
  await assert.doesNotReject(page.locator("#message").waitFor({ state: "visible" }));
  await page.waitForFunction(() => !document.getElementById("message").disabled);
  assert.equal(startCount, 0); assert.equal(createdCount, 0);
  assert.equal(await page.locator("#message").inputValue(), "正式输入");
  report.scenarios.push("opening-is-read-only");

  losePatch = true;
  await page.locator("#message").fill("真实保存后的未知输入");
  await page.locator("#pending button").waitFor();
  assert.equal(startCount, 0);
  assert.equal(await page.locator("#send").isDisabled(), true);
  const mutations = report.operations.filter(row => row.operation === "patch_input").length;
  await page.locator("#pending button").click();
  await page.locator("#pending").waitFor({ state: "hidden" });
  assert.equal(report.operations.filter(row => row.operation === "patch_input").length, mutations);
  assert.deepEqual(report.operations.slice(-2).map(row => row.operation), ["receipt", "accept_receipt"]);
  report.scenarios.push("unknown-only-receipt-and-accept");

  await page.locator("#send").click();
  await page.waitForURL(urls({ ...owner, session_id: id(3) }).page_url);
  await page.locator(".message pre").filter({ hasText: "真实保存后的未知输入" }).waitFor();
  assert.equal(startCount, 1); assert.equal(createdCount, 1);
  report.scenarios.push("first-start-rebinds-without-starting-twice");
  await screenshot(page, "node-page-desktop");
  await page.setViewportSize({ width: 390, height: 844 }); await screenshot(page, "node-page-mobile");
  await page.setViewportSize({ width: 320, height: 760 }); await screenshot(page, "node-page-small");

  const bound = { ...owner, session_id: id(3) };
  sessions.get(id(3)).status = "running";
  await page.evaluate(binding => window.__nodeStreams.at(-1).emit("state_changed", {
    schema_version: 1, kind: "workflow.frontend-node-state-changed", owner: binding,
  }), bound);
  await page.waitForFunction(() => !document.getElementById("pause").disabled);
  await page.locator("#pause").click();
  await page.locator("#run-status").filter({ hasText: "正在请求暂停" }).waitFor();
  sessions.get(id(3)).status = "paused";
  await page.evaluate(binding => window.__nodeStreams.at(-1).emit("state_changed", {
    schema_version: 1, kind: "workflow.frontend-node-state-changed", owner: binding,
  }), bound);
  await page.locator("#run-status").filter({ hasText: "已暂停" }).waitFor();
  report.scenarios.push("pause-acceptance-is-not-safety-point");
  await page.locator("#resume").click();
  await page.locator("#run-status").filter({ hasText: "运行中" }).waitFor();
  assert.equal(report.operations.filter(row => row.operation === "resume").length, 1);
  report.scenarios.push("resume-submits-through-bound-node");
  sessions.get(id(3)).status = "paused";
  await page.evaluate(binding => window.__nodeStreams.at(-1).emit("state_changed", {
    schema_version: 1, kind: "workflow.frontend-node-state-changed", owner: binding,
  }), bound);
  await page.waitForFunction(() => !document.getElementById("close-run").disabled);
  await page.locator("#close-run").click();
  await page.locator("#run-status").filter({ hasText: "已结束" }).waitFor();
  report.scenarios.push("close-round-submits-through-bound-node");

  const stateReads = report.operations.filter(row => row.operation === "state").length;
  const snap = sessions.get(id(3)).snapshot;
  snap.basis = { revision: 3, revision_id: id(70) };
  snap.entries.push({ entry_id: id(71), role: "assistant", source_ref: { scope: "artifact", output_id: id(72) },
    content: { schema_version: 2, kind: "workflow.text", text: "后端再次读取后的展示。" } });
  await page.evaluate(snapshot => {
    const stream = window.__nodeStreams.at(-1); stream.emit("snapshot", snapshot); stream.emit("snapshot", snapshot);
  }, copy(snap));
  await page.locator(".message pre").filter({ hasText: "后端再次读取后的展示。" }).waitFor();
  assert.equal(report.operations.filter(row => row.operation === "state").length, stateReads + 1);
  report.scenarios.push("snapshot-push-deduplicated-read-only");

  await page.evaluate(() => window.__nodeStreams.at(-1).disconnect());
  await page.locator("#connection-status").filter({ hasText: "连接断开" }).waitFor();
  assert.ok(await page.evaluate(() => window.__nodeStreams.at(-1).closed));
  const streamCount = await page.evaluate(() => window.__nodeStreams.length);
  await page.locator("#refresh").click();
  await page.waitForFunction(count => window.__nodeStreams.length === count + 1, streamCount);
  report.scenarios.push("disconnect-requires-explicit-reconnect");
  const sourceBefore = copy(sessions.get(id(3)));
  await page.locator("#checkpoints-open").click();
  await page.locator("#checkpoint-select option[value='" + id(91) + "']").waitFor({ state: "attached" });
  await page.locator("#checkpoint-select").selectOption(id(90));
  await page.waitForFunction(() => document.getElementById("checkpoint-detail").textContent.includes("首个检查点"));
  await page.waitForFunction(() => !document.getElementById("checkpoint-fork").disabled);
  await page.setViewportSize({ width: 1440, height: 1000 }); await screenshot(page, "node-checkpoints-desktop");
  await page.setViewportSize({ width: 390, height: 844 }); await screenshot(page, "node-checkpoints-mobile");
  await page.setViewportSize({ width: 320, height: 760 }); await screenshot(page, "node-checkpoints-small");
  await page.locator("#checkpoint-fork").click();
  await page.locator("#binding-next").waitFor({ state: "visible" });
  assert.equal(page.url(), urls(bound).page_url);
  assert.deepEqual(sessions.get(id(3)), sourceBefore);
  assert.equal(startCount, 1);
  await page.locator("#continue-binding").click();
  await page.waitForURL(urls({ ...bound, session_id: id(5) }).page_url);
  assert.equal(startCount, 1);
  report.scenarios.push("checkpoint-list-read-fork-explicit-binding");

  const fixedBinding = { ...owner, workflow_definition_id: id(8), session_id: id(4) };
  workflows.set(id(8), { text: "固定正式输入", edit_sequence: 1, policy: "fixed" });
  sessions.set(id(4), { revision: 3, status: "succeeded", snapshot: initialSnapshot(fixedBinding, "固定正式输入") });
  await page.goto(urls(fixedBinding).page_url);
  await page.waitForFunction(() => document.getElementById("input-policy").textContent.startsWith("固定输入"));
  await page.locator("#message").fill("固定修改的原正文");
  await page.locator("#confirmation").waitFor({ state: "visible" });
  await page.locator("#confirm").click();
  await page.locator("#binding-next").waitFor({ state: "visible" });
  assert.equal(startCount, 1);
  await page.locator("#continue-binding").click();
  await page.waitForURL(urls({ ...owner, workflow_definition_id: id(9) }).page_url);
  assert.equal(startCount, 1);
  assert.equal(await page.locator("#message").inputValue(), "固定修改的原正文");
  report.scenarios.push("fixed-derivation-explicit-binding-without-start");
  await screenshot(page, "node-page-derived-mobile");
  for (const [button, workflow] of [["save", id(10)], ["send", id(11)]]) {
    const fixedWithoutSession = { ...owner, workflow_definition_id: workflow };
    workflows.set(workflow, { text: "无会话固定输入", edit_sequence: 1, policy: "fixed" });
    await page.goto(urls(fixedWithoutSession).page_url);
    await page.waitForFunction(() => !document.getElementById("message").disabled);
    await page.evaluate(button => {
      const input = document.getElementById("message");
      input.value = "已经正式保存到修订 2";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      document.getElementById(button).click();
    }, button);
    await page.locator("#notice").filter({ hasText: "输入已保存，请接续新绑定；本轮尚未启动。" }).waitFor();
    assert.equal(await page.locator("#error").isHidden(), true);
    assert.equal(await page.locator("#input-status").textContent(), "输入已正式保存");
    assert.equal(await page.locator("#binding-next").isVisible(), true);
    assert.equal(page.url(), urls(fixedWithoutSession).page_url);
    assert.equal(startCount, 1);
    assert.equal(report.operations.filter(row => row.owner.workflow_definition_id === workflow
      && row.operation === "patch_input").length, 1);
    await page.locator("#continue-binding").click();
    await page.waitForURL(urls({ ...fixedWithoutSession, definition_revision: 2 }).page_url);
    assert.equal(startCount, 1);
  }
  report.scenarios.push("fixed-revision-save-and-send-report-saved-before-rebinding");
  const chatBinding = { ...owner, workflow_definition_id: id(12), session_id: id(13) };
  workflows.set(id(12), { text: "输入框当前草稿", edit_sequence: 1, policy: "adjustable" });
  sessions.set(id(13), { revision: 8, status: "succeeded", snapshot: logicalSnapshot(chatBinding) });
  await page.goto(urls(chatBinding).page_url);
  await page.locator(".chat-round").nth(1).waitFor();
  assert.equal(await page.locator('[aria-label="第 1 轮候选"]').isDisabled(), true);
  assert.equal(await page.locator('[aria-label="重 roll 第 1 轮"]').isDisabled(), true);
  assert.equal(await page.locator('[aria-label="第 2 轮候选"]').isEnabled(), true);
  assert.equal(await page.locator(".message pre").filter({ hasText: "原始输入 1" }).count(), 1);
  report.scenarios.push("logical-chat-inline-display-and-locked-earlier-floor");
  await page.setViewportSize({ width: 1440, height: 1000 }); await screenshot(page, "node-chat-desktop");
  await page.setViewportSize({ width: 390, height: 844 }); await screenshot(page, "node-chat-mobile");
  await page.setViewportSize({ width: 320, height: 760 }); await screenshot(page, "node-chat-small");
  await page.locator('[aria-label="第 2 轮候选"]').selectOption(id(112));
  await page.locator(".message pre").filter({ hasText: "完整候选回复 1-0" }).waitFor();
  await page.locator('[aria-label="重 roll 第 2 轮"]').click();
  await page.locator(".message pre").filter({ hasText: "重新生成的完整正文" }).waitFor();
  assert.equal(await page.locator('[aria-label="第 2 轮候选"] option').count(), 3);
  assert.equal(await page.locator(".message pre").filter({ hasText: "原始输入 1" }).count(), 1);
  assert.equal(report.operations.filter(row => row.operation === "reroll").length, 1);
  assert.equal(report.operations.filter(row => row.operation === "select_candidate").length, 1);
  report.scenarios.push("candidate-select-and-reroll-only-through-bound-node");
  const chatSource = copy(sessions.get(id(13)));
  await page.locator('[aria-label="从第 1 轮分叉"]').click();
  await page.locator("#binding-next").waitFor({ state: "visible" });
  assert.deepEqual(sessions.get(id(13)), chatSource);
  assert.equal(page.url(), urls(chatBinding).page_url);
  await page.locator("#continue-binding").click();
  await page.waitForURL(urls({ ...chatBinding, session_id: id(15) }).page_url);
  assert.equal(await page.locator(".chat-round").count(), 1);
  assert.equal(await page.locator('[aria-label="第 1 轮候选"] option').count(), 2);
  assert.equal(await page.locator('[aria-label="第 1 轮候选"]').isEnabled(), true);
  await page.locator('[aria-label="第 1 轮候选"]').selectOption(id(110));
  await page.locator(".message pre").filter({ hasText: "完整候选回复 0-0" }).waitFor();
  assert.deepEqual(sessions.get(id(13)), chatSource);
  report.scenarios.push("floor-fork-keeps-candidates-and-unlocks-independent-target");
  const beforeClose = report.operations.length;
  await page.goto("about:blank");
  assert.equal(report.operations.length, beforeClose);
  assert.deepEqual(report.errors, []); assert.deepEqual(report.unexpected, []);
  fs.writeFileSync(path.join(output, "page-report.json"), JSON.stringify(report, null, 2));
  await browser.close();
})().catch(async error => {
  report.errors.push(error.stack ?? String(error));
  fs.writeFileSync(path.join(output, "page-report.json"), JSON.stringify(report, null, 2));
  if (browser) await browser.close();
  console.error(error); process.exitCode = 1;
});
