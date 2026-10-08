"""Real shipped tavern page, native offline graph, fork and original receipts."""

from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from phase1_agent.graph_service import GraphWorkflowService
from test_context_native_integration import NativeTransport, native_graph, node_output
from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from test_tavern_chat_integration import add_chat
from test_tavern_fork_http import consumer_server


FIRST_TEXT = (
    "First parent round.\n\n**Native Markdown** and `{{keep_literal}}`.\n\n"
    "<script>window.tavern_attack=1</script>"
    "<img src='https://forbidden.test/pixel' onerror='window.tavern_attack=2'>"
    "<a href='javascript:alert(1)' onclick='window.tavern_attack=3'>Unsafe link</a>"
    "<style>body{display:none}</style>\n\n"
    "[Normal link](https://example.test)"
)
SECOND_TEXT = "Second parent round must not enter the first checkpoint child."
CHILD_TEXT = "Continue only inside the actual first-checkpoint child."


def browser_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for joint browser acceptance")
    probe = subprocess.run(
        [node, "-e", "console.log(require('playwright').chromium.executablePath())"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if probe.returncode:
        pytest.skip("Set NODE_PATH to an installed Playwright package")
    executable = os.environ.get("TAVERN_CHROMIUM_EXECUTABLE", probe.stdout.strip())
    if not Path(executable).is_file():
        pytest.skip("An installed Chromium or TAVERN_CHROMIUM_EXECUTABLE is required")
    return node


def test_real_tavern_native_rounds_fork_reload_and_lost_response(tmp_path, monkeypatch):
    node = browser_runtime()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-joint-browser")
    transport = NativeTransport()
    parent_snapshots = []
    evidence_path = Path(os.environ.get("TAVERN_JOINT_BROWSER_EVIDENCE", str(tmp_path / "browser")))
    evidence_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "joint-browser.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, native = native_graph(service, policy=False, once=False)
        current = next(row for row in document["nodes"] if row["component_id"] == "tools.current-input")
        chat = add_chat(
            service, document, current, native["a"]["agent"], after=native["a"]["merge"])
        initial = create(service, document)
        parent_id = initial["workflow_session_id"]
        fork = service.fork_graph_candidate

        def recording_fork(sid, **parameters):
            before = deepcopy(service.get_session(sid))
            result = fork(sid, **parameters)
            assert service.get_session(sid) == before
            parent_snapshots.append(before)
            return result

        monkeypatch.setattr(service, "fork_graph_candidate", recording_fork)
        with consumer_server(service) as port:
            process = subprocess.run(
                [node, "-e", BROWSER_SCRIPT, f"http://127.0.0.1:{port}",
                 document["workflow_definition_id"], parent_id, str(evidence_path),
                 json.dumps({"first": FIRST_TEXT, "second": SECOND_TEXT, "child": CHILD_TEXT})],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=120, check=False,
            )
        assert process.returncode == 0, process.stdout + process.stderr
        observed = json.loads(process.stdout)
        assert observed["parent_id"] == parent_id
        assert observed["fork_posts"] == 2
        assert observed["receipt_posts"] == 1
        assert observed["first_child"] != observed["reconciled_child"]
        assert len(parent_snapshots) == 2
        assert parent_snapshots[0] == parent_snapshots[1]
        parent = service.get_session(parent_id)
        assert parent == parent_snapshots[0]
        assert len(parent["history_refs"]) == 2
        assert len(parent["objects"]["tavern-chat"]["value"]["entries"]) == 4
        assert parent["objects"]["context-a"]["revision"] == 3
        first_chain = parent["history_refs"][0]
        assert observed["first_chain"] == first_chain
        first_checkpoint = next(row for row in service.list_graph_candidates(parent_id)["candidates"]
                                if row["chain_run_id"] == first_chain)
        assert first_checkpoint["candidate_id"] == observed["first_candidate"]
        assert len(service.list_sessions(document["workflow_definition_id"])) == 3

        first_child = service.get_session(observed["first_child"])
        assert first_child["source"]["workflow_session_id"] == parent_id
        assert first_child["source"]["candidate_commit_id"] == first_checkpoint["candidate_id"]
        assert first_child["history_refs"][0] == first_chain
        assert len(first_child["history_refs"]) == 2
        assert parent["history_refs"][1] not in first_child["history_refs"]
        assert len(first_child["objects"]["tavern-chat"]["value"]["entries"]) == 4
        assert first_child["objects"]["context-a"]["revision"] == 2
        child_context = node_output(first_child, native["a"]["merge"])
        child_texts = [block.get("text", "") for message in child_context["messages"]
                       for block in message["blocks"]]
        assert child_texts == [FIRST_TEXT, "Native accepted answer", CHILD_TEXT, "Native accepted answer"]
        assert SECOND_TEXT not in str(child_context)

        reconciled = service.get_session(observed["reconciled_child"])
        assert reconciled["source"]["candidate_commit_id"] == first_checkpoint["candidate_id"]
        assert reconciled["history_refs"] == [first_chain]
        assert len(reconciled["objects"]["tavern-chat"]["value"]["entries"]) == 2
        assert reconciled["objects"]["context-a"]["revision"] == 1
        assert len(transport.calls) == 3
        second_contents = [message.get("content", "") for message in transport.calls[1]["messages"]]
        continued_contents = [message.get("content", "") for message in transport.calls[2]["messages"]]
        assert SECOND_TEXT in second_contents
        assert SECOND_TEXT not in continued_contents
        assert FIRST_TEXT in continued_contents
        assert CHILD_TEXT in continued_contents
        assert not transport.summaries
        saved_parent = deepcopy(parent)
        saved_child = deepcopy(first_child)
        saved_reconciled = deepcopy(reconciled)

    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as reopened:
        assert reopened.get_session(parent_id) == saved_parent
        assert reopened.get_session(observed["first_child"]) == saved_child
        assert reopened.get_session(observed["reconciled_child"]) == saved_reconciled
        assert len(transport.calls) == 3, "fork and cold reopen must never call the model"
    observed.update(
        transport="NativeTransport (offline fixture)", native_model_calls=len(transport.calls),
        parent_revision=saved_parent["revision"],
        parent_chat_revision=saved_parent["objects"]["tavern-chat"]["revision"],
        parent_context_revision=saved_parent["objects"]["context-a"]["revision"],
        parent_unchanged=True, cold_database_reopen=True,
    )
    (evidence_path / "joint-evidence.json").write_text(
        json.dumps(observed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


BROWSER_SCRIPT = r"""
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path");
const { chromium } = require("playwright");
const [base, workflow, parent, evidence, encoded] = process.argv.slice(1), texts = JSON.parse(encoded);
const requests = [], errors = [], missing = [], externals = [], artifactReplies = [], candidateReplies = [], consumerReplies = [];
let loseFork = false, lostAcceptance = null, firstChild = null;
const expectedApi = new Set(["/api/graph/consumer/application", "/api/graph/consumer/queries",
  "/api/graph/consumer/commands", "/api/graph/consumer/receipts/read"]);
function operation(request) {
  try { return request.method() === "POST" ? request.postDataJSON() : null; } catch { return null; }
}
function journalKey() { return "workflow.tavern:workflow-chat:v1:" + workflow; }
(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url()), body = operation(request);
      if (url.origin !== base) { externals.push(request.url()); return route.abort("blockedbyclient"); }
      requests.push({ pathname: url.pathname, method: request.method(), body });
      if (loseFork && body?.operation === "consumer.candidate.fork") {
        loseFork = false;
        const actual = await route.fetch();
        assert.equal(actual.status(), 201);
        lostAcceptance = await actual.json();
        assert.equal(lostAcceptance.result.receipt.fork_source.workflow_session_id, parent);
        assert.equal(lostAcceptance.result.receipt.status, "succeeded");
        return route.abort("failed");
      }
      return route.continue();
    });
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    page.on("response", async response => {
      const request = response.request(), url = new URL(response.url()), body = operation(request);
      if (response.status() >= 400) missing.push({ path: url.pathname, status: response.status() });
      if (body?.operation === "output.artifact.read") artifactReplies.push(await response.json());
      if (body?.operation === "consumer.candidate.list") candidateReplies.push(await response.json());
      if (body?.operation === "consumer.read") consumerReplies.push(await response.json());
    });
    const response = await page.goto(`${base}/tavern/?graph_workflow=${workflow}&graph_session=${parent}`);
    assert.equal(response.status(), 200);
    const csp = response.headers()["content-security-policy"];
    assert.ok(csp.includes("default-src 'none'") && csp.includes("script-src 'self'"));
    assert.ok(csp.includes("img-src 'self'") && !csp.includes("unsafe-inline") && !csp.includes("unsafe-eval"));
    await page.waitForFunction(() => !document.querySelector("#send_textarea").disabled);
    await page.locator("#send_textarea").fill(texts.first);
    await page.getByRole("button", { name: "发送消息", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#transcript-status").textContent === "2 条展示记录"
      && document.querySelectorAll(".mes_create_branch:not([hidden])").length === 2
      && !document.querySelector("#send_textarea").disabled);
    assert.equal(await page.locator("#error").textContent(), "");
    await page.locator("#send_textarea").fill(texts.second);
    await page.getByRole("button", { name: "发送消息", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#transcript-status").textContent === "4 条展示记录"
      && document.querySelectorAll(".mes_create_branch:not([hidden])").length === 4
      && !document.querySelector("#send_textarea").disabled);
    assert.equal(await page.locator("#error").textContent(), "");
    const candidatePacket = candidateReplies.filter(row => row.workflow_session_id === parent && row.candidates.length === 2).at(-1);
    assert.ok(candidatePacket, JSON.stringify(candidateReplies));
    const currentParent = consumerReplies.filter(row => row.workflow_session_id === parent && row.history.length === 2).at(-1);
    const display = currentParent.outputs.find(output => output.data_type === "TAVERN_CHAT_DISPLAY");
    const firstArtifact = artifactReplies.filter(row => row.reference.output_id === display.payload.entries[0].source_ref.output_id).at(-1);
    const lastArtifact = artifactReplies.filter(row => row.reference.output_id === display.payload.entries[2].source_ref.output_id).at(-1);
    const firstCandidate = candidatePacket.candidates.find(row => row.chain_run_id === firstArtifact.producer.chain_run_id
      && row.source_session_id === firstArtifact.producer.workflow_session_id);
    const lastCandidate = candidatePacket.candidates.find(row => row.chain_run_id === lastArtifact.producer.chain_run_id
      && row.source_session_id === lastArtifact.producer.workflow_session_id);
    assert.ok(firstCandidate && lastCandidate);
    assert.notEqual(firstCandidate.chain_run_id, lastCandidate.chain_run_id);
    assert.equal(display.chain_run_id, lastCandidate.chain_run_id);
    const firstEntries = display.payload.entries.slice(0, 2);
    for (const entry of firstEntries) {
      const artifact = artifactReplies.filter(row => row.reference.output_id === entry.source_ref.output_id).at(-1);
      assert.equal(artifact.producer.chain_run_id, firstCandidate.chain_run_id);
      assert.equal(artifact.producer.workflow_session_id, parent);
      const button = page.locator(`.mes[data-entry-id="${entry.entry_id}"] .mes_create_branch`);
      assert.equal(await button.getAttribute("title"), "从第 1 个完成回合检查点分叉");
    }
    for (const entry of display.payload.entries.slice(2)) {
      const artifact = artifactReplies.filter(row => row.reference.output_id === entry.source_ref.output_id).at(-1);
      assert.equal(artifact.producer.chain_run_id, lastCandidate.chain_run_id);
      assert.equal(await page.locator(`.mes[data-entry-id="${entry.entry_id}"] .mes_create_branch`).getAttribute("title"),
        "从第 2 个完成回合检查点分叉");
    }
    assert.equal(await page.locator(".mes_text strong").first().textContent(), "Native Markdown");
    assert.equal(await page.locator(".mes_text code").first().textContent(), "{{keep_literal}}");
    assert.equal(await page.evaluate(() => window.tavern_attack), undefined);
    assert.equal(await page.locator(".mes_text script,.mes_text style,.mes_text img,.mes_text iframe").count(), 0);
    assert.equal(await page.locator(".mes_text [onclick],.mes_text [onerror],.mes_text a[href^='javascript:']").count(), 0);
    assert.equal(await page.locator(".avatar img").evaluateAll(images => images.every(image =>
      image.complete && image.naturalWidth > 0 && new URL(image.src).origin === location.origin)), true);
    assert.equal(await page.locator(".mes_copy svg").count(), 4);
    assert.equal(await page.locator(".mes_create_branch svg").count(), 4);
    assert.equal(await page.locator(".mes_edit,.swipe_right,.mes_hide,.mes_continue").count(), 0);
    await page.reload();
    await page.waitForFunction(() => document.querySelector("#transcript-status").textContent === "4 条展示记录"
      && document.querySelectorAll(".mes_create_branch:not([hidden])").length === 4);
    assert.equal(await page.locator("#chat .mes").count(), 4, "cold page reload must not duplicate facts");
    assert.equal(await page.locator("#error").textContent(), "");
    async function geometry() {
      return page.evaluate(() => {
        const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect();
          return { top: r.top, bottom: r.bottom, left: r.left, right: r.right }; };
        return { width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth,
          header: rect(".tavern-header"), chat: rect("#chat"), composer: rect("#form_sheld") };
      });
    }
    function fits(value) {
      assert.ok(value.scrollWidth <= value.width, JSON.stringify(value));
      assert.ok(value.header.bottom <= value.chat.top, JSON.stringify(value));
      assert.ok(value.chat.bottom <= value.composer.top + 1, JSON.stringify(value));
      assert.ok(value.composer.bottom <= value.height + 1, JSON.stringify(value));
      assert.ok(value.chat.bottom > value.chat.top + 100, JSON.stringify(value));
    }
    fits(await geometry());
    await page.screenshot({ path: path.join(evidence, "tavern-joint-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    fits(await geometry());
    await page.screenshot({ path: path.join(evidence, "tavern-joint-mobile.png"), fullPage: true });
    const confirmations = [];
    page.on("dialog", async dialog => { confirmations.push(dialog.message()); await dialog.accept(); });
    await page.getByRole("button", { name: "从第 1 个完成回合检查点分叉", exact: true }).first().click();
    await page.waitForFunction(parent => new URL(location.href).searchParams.get("graph_session") !== parent
      && document.querySelector("#transcript-status").textContent === "2 条展示记录"
      && !document.querySelector("#send_textarea").disabled, parent);
    firstChild = new URL(page.url()).searchParams.get("graph_session");
    assert.notEqual(firstChild, parent);
    assert.match(confirmations[0], /#1, #2/);
    assert.match(confirmations[0], /完整回合/);
    assert.match(confirmations[0], /父会话不变/);
    assert.ok(confirmations[0].includes(firstCandidate.candidate_id));
    assert.equal(await page.locator("#error").textContent(), "");
    assert.ok(!(await page.locator("#chat").textContent()).includes(texts.second));
    await page.locator("#send_textarea").fill(texts.child);
    await page.getByRole("button", { name: "发送消息", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#transcript-status").textContent === "4 条展示记录"
      && !document.querySelector("#send_textarea").disabled
      && document.querySelectorAll(".mes_create_branch:not([hidden])").length === 4);
    assert.ok((await page.locator("#chat").textContent()).includes(texts.child));
    assert.ok(!(await page.locator("#chat").textContent()).includes(texts.second));
    assert.equal(await page.locator("#error").textContent(), "");
    await page.reload();
    await page.waitForFunction(() => document.querySelector("#transcript-status").textContent === "4 条展示记录"
      && !document.querySelector("#send_textarea").disabled);
    assert.equal(new URL(page.url()).searchParams.get("graph_session"), firstChild);
    await page.locator("#sessions").selectOption(parent);
    await page.waitForFunction(() => document.querySelector("#transcript-status").textContent === "4 条展示记录"
      && document.querySelectorAll(".mes_create_branch:not([hidden])").length === 4);
    assert.ok((await page.locator("#chat").textContent()).includes(texts.second));
    assert.ok(!(await page.locator("#chat").textContent()).includes(texts.child));
    const parentJournal = await page.evaluate(key => localStorage.getItem(key), journalKey());
    assert.equal(JSON.parse(parentJournal).workflow_session_id, parent);
    loseFork = true;
    await page.getByRole("button", { name: "从第 1 个完成回合检查点分叉", exact: true }).last().click();
    await page.waitForFunction(() => !document.querySelector("#pending").hidden
      && document.querySelector("#error").textContent.includes("未知"));
    assert.ok(lostAcceptance, "route.fetch must get real native acceptance before simulating disconnect");
    const realLostChild = lostAcceptance.result.receipt.workflow_session_id;
    assert.notEqual(realLostChild, firstChild);
    assert.notEqual(realLostChild, parent);
    const pending = await page.evaluate(key => localStorage.getItem(key), journalKey());
    const frozen = JSON.parse(pending).pending;
    assert.equal(frozen.workflow_session_id, parent);
    assert.equal(frozen.candidate.candidate_id, firstCandidate.candidate_id);
    assert.equal(frozen.candidate.chain_run_id, firstCandidate.chain_run_id);
    assert.equal(frozen.body.idempotency_key, lostAcceptance.result.receipt.idempotency_key);
    assert.equal(await page.locator("#sessions").isDisabled(), true);
    assert.equal(await page.locator("#new-session").isDisabled(), true);
    assert.equal(await page.locator("#send_but").isDisabled(), true);
    assert.equal(await page.locator(".mes_create_branch:visible").count(), 0);
    const countForks = () => requests.filter(row => row.body?.operation === "consumer.candidate.fork"
      && row.pathname === "/api/graph/consumer/commands").length;
    const countReceipts = () => requests.filter(row => row.pathname === "/api/graph/consumer/receipts/read").length;
    assert.equal(countForks(), 2);
    assert.equal(countReceipts(), 0);
    await page.goto(`${base}/tavern/?graph_workflow=${workflow}&graph_session=${firstChild}`);
    await page.waitForFunction(() => !document.querySelector("#pending").hidden
      && document.querySelector("#transcript-status").textContent === "4 条展示记录");
    assert.equal(new URL(page.url()).searchParams.get("graph_session"), parent);
    assert.equal(await page.evaluate(key => localStorage.getItem(key), journalKey()), pending);
    assert.equal(countForks(), 2);
    assert.equal(countReceipts(), 0, "reopen must not reconcile or mutate automatically");
    await page.getByRole("button", { name: "核实原请求", exact: true }).click();
    await page.waitForFunction(child => new URL(location.href).searchParams.get("graph_session") === child
      && document.querySelector("#pending").hidden && document.querySelector("#transcript-status").textContent === "2 条展示记录"
      && !document.querySelector("#send_textarea").disabled, realLostChild);
    assert.equal(countForks(), 2);
    assert.equal(countReceipts(), 1);
    const reconciliation = requests.filter(row => row.pathname === "/api/graph/consumer/receipts/read")[0].body;
    const original = requests.filter(row => row.pathname === "/api/graph/consumer/commands"
      && row.body?.operation === "consumer.candidate.fork").at(-1).body;
    assert.deepEqual(reconciliation, original);
    assert.equal(JSON.parse(await page.evaluate(key => localStorage.getItem(key), journalKey())).pending, null);
    assert.equal(await page.locator("#error").textContent(), "");
    assert.ok(!(await page.locator("#chat").textContent()).includes(texts.second));
    assert.ok(!(await page.locator("#chat").textContent()).includes(texts.child));
    assert.deepEqual(errors, []);
    assert.deepEqual(missing, []);
    assert.deepEqual(externals, []);
    assert.ok(requests.filter(row => row.pathname.startsWith("/api/")).every(row => expectedApi.has(row.pathname)),
      JSON.stringify(requests));
    assert.ok(requests.every(row => !row.pathname.startsWith("/api/chats") && !row.pathname.startsWith("/api/characters")));
    assert.equal(requests.filter(row => row.body?.operation === "consumer.run.start").length, 3);
    const evidenceValue = { parent_id: parent, first_child: firstChild, reconciled_child: realLostChild,
      first_chain: firstCandidate.chain_run_id, first_candidate: firstCandidate.candidate_id,
      fork_posts: countForks(), receipt_posts: countReceipts(), model_start_posts: 3,
      desktop: "tavern-joint-desktop.png", mobile: "tavern-joint-mobile.png", requests: requests.length,
      csp, external_requests: externals, missing_assets: missing, page_errors: errors };
    console.log(JSON.stringify(evidenceValue));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
