"""The isolated tavern shell consumes public facts without ST network/storage."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "src" / "phase1_agent" / "tavern" / "frontend"
CORE = ROOT / "src" / "phase1_agent" / "static" / "graph-chat-core.js"


def node_run(script, *arguments, timeout=30):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for isolated tavern client checks")
    result = subprocess.run(
        [node, "-e", script, str(SHELL), str(CORE), *map(str, arguments)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


NODE_SETUP = r"""
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const assert = require("node:assert/strict"), { randomUUID } = require("node:crypto");
const shell = process.argv[1], core = process.argv[2];
vm.runInThisContext(fs.readFileSync(core, "utf8"), { filename: core });
vm.runInThisContext(fs.readFileSync(path.join(shell, "adapter.js"), "utf8"));
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const workflow = id(1), session = id(2), node = id(4), run = id(5), chain = id(6);
const copy = value => JSON.parse(JSON.stringify(value));
const input = { name: "message", data_type: "TEXT", required: true, node_ids: [node] };
const bare = { schema_version: 1, kind: "workflow.consumer", workflow_definition_id: workflow,
  definition_revision: 1, workflow_session_id: session, session_revision: 1, status: "idle",
  can_submit: true, available_actions: [], inputs: [input], nodes: [], outputs: [], history: [], diagnostics: [] };
const raw = new Map(), storage = { getItem: key => raw.get(key) ?? null, setItem: (key, value) => raw.set(key, value) };
const response = value => ({ ok: true, status: 200, json: async () => copy(value) });
const entry = (number, role) => ({ entry_id: id(number), role,
  source_ref: { scope: "artifact", output_id: id(number + 100) } });
const entries = [entry(10, "user"), entry(11, "assistant"), entry(12, "user"), entry(13, "assistant")];
const source = (whichRun = run, whichChain = chain) => ({ workflow_definition_id: workflow,
  definition_revision: 1, workflow_session_id: session, node_binding_id: node, port_id: "output",
  run_id: whichRun, chain_run_id: whichChain });
const display = (rows, whichRun = run, whichChain = chain) => ({
  node_binding_id: node, port_id: "output", data_type: "TAVERN_CHAT_DISPLAY", data_schema_version: 1,
  label: "聊天展示", is_output: true, status: "succeeded", availability: "produced", reason_code: null,
  run_id: whichRun, chain_run_id: whichChain, source: source(whichRun, whichChain),
  payload: { schema_version: 1, kind: "workflow.tavern-chat-display", entries: rows } });
const firstOutput = display(entries.slice(0, 2));
const finalOutput = display(entries, id(7), id(8));
const historyRows = [{ workflow_definition_id: workflow, definition_revision: 1,
  workflow_session_id: session, chain_run_id: chain, status: "succeeded", revision: 2, inherited: false },
  { workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: session,
    chain_run_id: id(8), status: "succeeded", revision: 2, inherited: false }];
const view = { ...bare, session_revision: 4, status: "succeeded", outputs: [finalOutput], history: historyRows,
  nodes: [{ node_binding_id: node, label: "展示", status: "succeeded", run_id: id(7), revision: 1,
    diagnostic: null, budget: null }] };
function publicArtifact(output, reference) {
  const index = entries.findIndex(row => row.source_ref.output_id === reference.output_id);
  const text = ["第一轮", "**答复**一", "第二轮", "答复二"][index];
  return { schema_version: 1, kind: "workflow.public-artifact", workflow_definition_id: workflow,
    definition_revision: 1, workflow_session_id: session, session_revision: 4, node_id: node,
    port_id: "output", run_id: output.run_id, reference, data_type: "TEXT", data_schema_version: 2,
    value: { schema_version: 2, kind: "workflow.text", text },
    producer: { workflow_session_id: session, chain_run_id: index < 2 ? chain : id(8),
      node_binding_id: node, node_run_id: index < 2 ? run : id(7) } };
}
"""


def test_tavern_input_boundary_and_isolated_transport():
    node_run(NODE_SETUP + r"""
(async () => {
  assert.deepEqual(TavernAdapter.inputsFor(bare, "a {{macro}}\n"), { message: "a {{macro}}\n" });
  assert.throws(() => TavernAdapter.inputsFor(bare, " "), /不能为空/);
  assert.throws(() => TavernAdapter.inputsFor(bare, "x".repeat(1000001)), /范围/);
  for (const inputs of [[], [input, { ...input, name: "other" }], [{ ...input, data_type: "PROMPT" }]]) {
    assert.ok(TavernAdapter.textInput({ ...bare, inputs }).diagnostic);
    assert.throws(() => TavernAdapter.inputsFor({ ...bare, inputs }, "x"), /唯一/);
  }
  const idle = { ...finalOutput, availability: "unproduced", payload: null };
  delete idle.data_schema_version;
  assert.equal(TavernAdapter.displayPorts({ outputs: [idle] }).length, 1);
  assert.equal(TavernAdapter.displayPorts({ outputs: [{ ...idle, data_type: "FRONTEND_DISPLAY" }] }).length, 0);
  const called = [], transport = TavernAdapter.createRequest(async (url, options) => {
    called.push([url, options]); return response({ okay: true });
  });
  for (const url of ["/api/chats/save", "/api/characters/all", "/api/graph/commands",
    "https://example.test/api/graph/consumer/queries"]) await assert.rejects(transport(url, { method: "POST" }), /未授权/);
  await assert.rejects(transport("/api/graph/consumer/application", { method: "POST" }), /未授权/);
  await transport("/api/graph/consumer/queries", { method: "POST", body: "{}" });
  assert.equal(called.length, 1);
  assert.equal(called[0][1].credentials, "same-origin");
  assert.equal(called[0][1].cache, "no-store");
  const wrapped = TavernAdapter.storageForTavern(storage);
  wrapped.setItem("workflow-chat:v1:" + workflow, "isolated");
  assert.equal(raw.get("workflow.tavern:workflow-chat:v1:" + workflow), "isolated");
  assert.equal(storage.getItem("workflow-chat:v1:" + workflow), null);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_tavern_public_transcript_deduplicates_and_preserves_artifact_producer():
    node_run(NODE_SETUP + r"""
(async () => {
  const calls = [], fetcher = async (url, options) => {
    const envelope = JSON.parse(options.body); calls.push(envelope);
    if (envelope.operation === "output.history") return response({
      schema_version: 1, kind: "workflow.public-history", workflow_definition_id: workflow,
      definition_revision: 1, workflow_session_id: session, session_revision: 4, node_id: node,
      port_id: "output", outputs: [firstOutput, finalOutput] });
    assert.equal(envelope.operation, "output.artifact.read");
    const output = envelope.parameters.run_id === run ? firstOutput : finalOutput;
    return response(publicArtifact(output, envelope.parameters.reference));
  };
  const client = TavernAdapter.createClient({ workflowId: workflow, sessionId: session,
    storage, fetcher, keyFactory: randomUUID });
  client.accept(view);
  const cache = new Map(), rows = await TavernAdapter.transcript(client, cache);
  assert.equal(rows.length, 4);
  assert.equal(rows[0].text, "第一轮");
  assert.equal(rows[1].text, "**答复**一");
  assert.equal(rows[0].producer.chain_run_id, chain);
  assert.equal(rows[3].producer.chain_run_id, id(8));
  assert.notEqual(rows[0].producer.chain_run_id, finalOutput.source.chain_run_id);
  assert.equal(calls.filter(row => row.operation === "output.artifact.read").length, 4);
  await TavernAdapter.transcript(client, cache);
  assert.equal(calls.filter(row => row.operation === "output.artifact.read").length, 4);
  assert.equal(TavernAdapter.collectEntries([firstOutput, finalOutput, finalOutput]).length, 4);
  assert.throws(() => TavernAdapter.collectEntries([firstOutput, display([
    { ...entries[0], role: "assistant" }])]), /身份冲突/);
  assert.throws(() => TavernAdapter.collectEntries([display([
    { ...entries[0], unwanted: true }])]), /无效/);
  assert.throws(() => TavernAdapter.collectEntries([display([
    { ...entries[0], source_ref: { scope: "artifact", output_id: id(999), extra: true } }])]), /无效/);
  client.consumer = { ...client.consumer, outputs: [finalOutput, { ...finalOutput, port_id: "second" }] };
  await assert.rejects(TavernAdapter.transcript(client), /唯一/);
  let finish; client.consumer = copy(view);
  client.readHistory = () => new Promise(resolve => { finish = resolve; });
  const delayed = TavernAdapter.transcript(client);
  client.consumer = { ...client.consumer, workflow_session_id: id(3) };
  finish({ outputs: [firstOutput] });
  assert.equal(await delayed, null);
  assert.ok(calls.every(row => ["output.history", "output.artifact.read"].includes(row.operation)));
  assert.ok([...raw.values()].every(value => !value.includes("source_ref") && !value.includes("entries")));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_tavern_unknown_result_survives_reopen_without_reposting():
    node_run(NODE_SETUP + r"""
(async () => {
  const calls = []; let saved = null, resolved = false;
  const fetcher = async (url, options) => {
    const body = JSON.parse(options.body); calls.push({ url, body });
    if (url === "/api/graph/consumer/commands") { saved = copy(body); throw new Error("disconnected"); }
    assert.equal(url, "/api/graph/consumer/receipts/read");
    assert.deepEqual(body, saved);
    if (!resolved) return response({ schema_version: 1, kind: "workflow.application-receipt-read",
      outcome: "unresolved", reason_code: "request_outcome_unknown" });
    const receipt = { workflow_definition_id: workflow, definition_revision: 1,
      workflow_session_id: session, session_revision: 2, chain_run_id: chain, status: "running",
      idempotency_key: body.parameters.idempotency_key, operation: "start" };
    return response({ schema_version: 1, kind: "workflow.application-receipt-read",
      outcome: "matched", reason_code: "receipt_matched",
      receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: body.operation,
        operation_scope: "consumer", idempotency_key: body.parameters.idempotency_key,
        request_sha256: "a".repeat(64), authority: "service_receipt", target: { session_id: session },
        accepted: receipt }, result: { receipt } });
  };
  raw.set("workflow-chat:v1:" + workflow, "generic journal must not be consumed");
  const options = { workflowId: workflow, sessionId: session, storage, fetcher, keyFactory: randomUUID };
  const original = TavernAdapter.createClient(options); original.accept(bare);
  await assert.rejects(original.command("start", { inputs: { message: "immutable original" } }), /未知/);
  const pending = copy(original.pending), reopened = TavernAdapter.createClient({ ...options, sessionId: id(3) });
  assert.equal(reopened.sessionId, session);
  assert.deepEqual(reopened.pending, pending);
  await assert.rejects(reopened.selectSession(id(3)), /核实/);
  await assert.rejects(reopened.command("start", { inputs: { message: "changed draft" } }), /保留原请求/);
  assert.deepEqual(reopened.pending, pending);
  assert.equal(calls.filter(row => row.url === "/api/graph/consumer/commands").length, 1);
  resolved = true; await reopened.command("start", { inputs: { message: "ignored" } });
  assert.equal(reopened.pending, null);
  assert.equal(reopened.sessionId, session);
  assert.equal(calls.filter(row => row.url === "/api/graph/consumer/commands").length, 1);
  assert.ok(calls.slice(1).every(row => row.url === "/api/graph/consumer/receipts/read"));
  assert.ok(calls.every(row => row.body.parameters.inputs?.message === "immutable original"));
  assert.equal(raw.get("workflow-chat:v1:" + workflow), "generic journal must not be consumed");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


FORK_SETUP = NODE_SETUP + r"""
const candidate = { candidate_id: id(20), chain_run_id: chain, source_session_id: session,
  source_workflow_definition_id: workflow, source_definition_revision: 1 };
const secondCandidate = { ...candidate, candidate_id: id(21), chain_run_id: id(8) };
const parent = { ...copy(view), definition_revision: 2 };
const packet = { schema_version: 1, kind: "workflow.consumer-candidates",
  workflow_definition_id: workflow, definition_revision: 2, workflow_session_id: session,
  session_revision: 4, data_revision: 0, head_revision: 3, status: "succeeded",
  can_fork: true, candidates: [candidate, secondCandidate] };
const child = { ...copy(view), workflow_session_id: id(3), session_revision: 1,
  definition_revision: 1, status: "succeeded", outputs: [copy(firstOutput)],
  history: [{ ...historyRows[0], inherited: true }] };
function forkAccepted(envelope, whichCandidate = candidate) {
  return { workflow_definition_id: workflow, definition_revision: whichCandidate.source_definition_revision,
    workflow_session_id: id(3), session_revision: 1, status: "succeeded", chain_run_id: null,
    operation: "fork", idempotency_key: envelope.parameters.idempotency_key,
    fork_source: { workflow_session_id: session, candidate_id: whichCandidate.candidate_id,
      chain_run_id: whichCandidate.chain_run_id, workflow_definition_id: workflow,
      definition_revision: whichCandidate.source_definition_revision } };
}
function applicationReceipt(envelope, accepted) {
  return { schema_version: 1, kind: "workflow.application-receipt",
    operation: envelope.operation, operation_scope: "consumer",
    idempotency_key: envelope.parameters.idempotency_key, request_sha256: "a".repeat(64),
    authority: "service_receipt", target: { session_id: session }, accepted };
}
function forkEnvelope(envelope) {
  const accepted = forkAccepted(envelope);
  return { schema_version: 1, kind: "workflow.application-command",
    receipt: applicationReceipt(envelope, accepted),
    result: { schema_version: 1, kind: "workflow.consumer.receipt", receipt: accepted, consumer: copy(child) } };
}
function forkRead(envelope) {
  const accepted = forkAccepted(envelope);
  return { schema_version: 1, kind: "workflow.application-receipt-read",
    outcome: "matched", reason_code: "receipt_matched",
    receipt: applicationReceipt(envelope, accepted), result: { receipt: accepted } };
}
"""


def test_tavern_candidate_boundary_and_actual_round_identity():
    node_run(FORK_SETUP + r"""
(async () => {
  GraphChat.validateCandidates(packet, parent);
  for (const changed of [
    { ...packet, session_revision: 3 }, { ...packet, definition_revision: 1 },
    { ...packet, head_revision: 0 }, { ...packet, data_revision: -1 },
    { ...packet, candidates: [{ ...candidate, source_session_id: id(30) }] },
    { ...packet, candidates: [{ ...candidate, source_workflow_definition_id: id(31) }] },
    { ...packet, candidates: [{ ...candidate, can_select: true }] },
    { ...packet, candidates: [candidate, { ...candidate, candidate_id: id(22) }] },
  ]) assert.throws(() => GraphChat.validateCandidates(changed, parent), /无效|完成回合/);
  assert.throws(() => GraphChat.validateCandidates({ ...packet, status: "running" },
    { ...parent, status: "running" }), /活动会话/);
  assert.throws(() => GraphChat.validateCandidates(packet,
    { ...parent, history: [{ ...historyRows[0], status: "failed" }, historyRows[1]] }), /完成回合/);
  const messages = entries.map((entry, index) => ({ ...entry, text: "real", producer: {
    workflow_session_id: session, chain_run_id: index < 2 ? chain : id(8), node_binding_id: node,
    node_run_id: index < 2 ? run : id(7) } }));
  const targets = TavernAdapter.roundTargets(messages, packet);
  assert.equal(targets.size, 4);
  assert.equal(targets.get(entries[0].entry_id).candidate.candidate_id, candidate.candidate_id);
  assert.deepEqual(targets.get(entries[0].entry_id), targets.get(entries[1].entry_id));
  assert.deepEqual(targets.get(entries[0].entry_id).records, [1, 2]);
  assert.equal(targets.get(entries[3].entry_id).candidate.candidate_id, secondCandidate.candidate_id);
  assert.equal(TavernAdapter.roundTargets(messages, { ...packet, can_fork: false }).size, 0);
  assert.equal(TavernAdapter.roundTargets([{ ...messages[0], producer: {
    ...messages[0].producer, workflow_session_id: id(30) } }], packet).size, 0);
  assert.equal(TavernAdapter.roundTargets([{ ...messages[0], producer: {
    ...messages[0].producer, chain_run_id: id(30) } }], packet).size, 0);
  assert.match(TavernAdapter.forkConfirmation(targets.get(entries[0].entry_id)), /#1, #2/);
  assert.match(TavernAdapter.forkConfirmation(targets.get(entries[0].entry_id)), /完整回合/);
  assert.match(TavernAdapter.forkConfirmation(targets.get(entries[0].entry_id)), /父会话不变/);
  assert.ok(!TavernAdapter.forkConfirmation(targets.get(entries[0].entry_id)).includes("最近"));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_tavern_fork_adopts_actual_child_old_definition_and_keeps_parent():
    node_run(FORK_SETUP + r"""
(async () => {
  const before = copy(parent), calls = [], fetcher = async (url, options) => {
    const body = JSON.parse(options.body); calls.push({ url, body });
    if (url.endsWith("/queries")) {
      assert.deepEqual(body, { operation: "consumer.candidate.list", parameters: {
        session_id: session, workflow_definition_id: workflow, definition_revision: 2 } });
      return response(packet);
    }
    assert.equal(body.operation, "consumer.candidate.fork");
    assert.deepEqual(Object.keys(body.parameters).sort(),
      ["candidate_id", "expected_revision", "expected_data_revision", "expected_head_revision", "idempotency_key", "session_id"].sort());
    assert.equal(body.parameters.candidate_id, candidate.candidate_id);
    assert.equal(body.parameters.expected_revision, 4);
    assert.equal(body.parameters.expected_data_revision, 0);
    assert.equal(body.parameters.expected_head_revision, 3);
    return response(forkEnvelope(body));
  };
  const client = TavernAdapter.createClient({ workflowId: workflow, sessionId: session,
    storage, fetcher, keyFactory: randomUUID });
  client.accept(parent);
  await assert.rejects(client.forkCandidate(candidate), /动作当前不可用/);
  await client.readCandidates();
  await assert.rejects(client.forkCandidate({ ...candidate, source_session_id: id(99) }), /完成检查点/);
  await client.forkCandidate(candidate);
  assert.equal(client.sessionId, id(3));
  assert.equal(client.consumer.definition_revision, 1, "fork must not adopt the parent's newer definition");
  assert.equal(client.consumer.history.length, 1);
  assert.equal(client.consumer.outputs[0].payload.entries.length, 2);
  assert.equal(client.pending, null);
  assert.deepEqual(parent, before);
  assert.equal(calls.filter(row => row.url.endsWith("/commands")).length, 1);
  const reopened = TavernAdapter.createClient({ workflowId: workflow, storage, fetcher, keyFactory: randomUUID });
  assert.equal(reopened.sessionId, id(3));
  assert.equal(reopened.pending, null);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_tavern_fork_unknown_reopen_receipt_only_and_strict_source():
    node_run(FORK_SETUP + r"""
(async () => {
  const calls = []; let original = null, mode = "unknown";
  const fetcher = async (url, options) => {
    const body = JSON.parse(options.body); calls.push({ url, body });
    if (url.endsWith("/queries")) return response(packet);
    if (url.endsWith("/commands")) { original = copy(body); throw new Error("lost"); }
    assert.equal(url, "/api/graph/consumer/receipts/read"); assert.deepEqual(body, original);
    if (mode === "unknown") return response({ schema_version: 1, kind: "workflow.application-receipt-read",
      outcome: "unresolved", reason_code: "request_outcome_unknown" });
    const result = forkRead(body);
    if (mode === "wrong_source") {
      result.receipt.accepted.fork_source.candidate_id = id(99);
      result.result.receipt.fork_source.candidate_id = id(99);
    }
    return response(result);
  };
  const options = { workflowId: workflow, sessionId: session, storage, fetcher, keyFactory: randomUUID };
  const client = TavernAdapter.createClient(options); client.accept(parent); await client.readCandidates();
  await assert.rejects(client.forkCandidate(candidate), /未知/);
  const pending = copy(client.pending);
  assert.deepEqual(pending.candidate, candidate);
  assert.equal(pending.workflow_session_id, session);
  const reopened = TavernAdapter.createClient({ ...options, sessionId: id(3) });
  assert.deepEqual(reopened.pending, pending);
  assert.equal(reopened.sessionId, session, "unknown fork retains parent, not guessed child");
  await assert.rejects(reopened.selectSession(id(3)), /核实/);
  await assert.rejects(reopened.forkCandidate(candidate), /待核实/);
  await assert.rejects(reopened.command("fork"), /保留原请求/);
  mode = "wrong_source"; await assert.rejects(reopened.command("fork"), /检查点不匹配/);
  assert.deepEqual(reopened.pending, pending);
  mode = "matched"; await reopened.command("fork");
  assert.equal(reopened.sessionId, id(3));
  assert.equal(reopened.pending, null);
  assert.equal(reopened.consumer, null, "read-only receipt must not pretend to be fresh consumer state");
  assert.equal(calls.filter(row => row.url.endsWith("/commands")).length, 1);
  assert.ok(calls.filter(row => row.url.endsWith("/receipts/read")).every(row => JSON.stringify(row.body) === JSON.stringify(original)));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_tavern_fork_late_responses_stale_catalog_and_storage_failure():
    node_run(FORK_SETUP + r"""
(async () => {
  let finish, requests = [];
  const fetcher = async (url, options) => {
    const body = JSON.parse(options.body); requests.push({ url, body });
    if (url.endsWith("/queries")) return response(packet);
    return new Promise(resolve => { finish = () => resolve(response(forkEnvelope(body))); });
  };
  const client = TavernAdapter.createClient({ workflowId: workflow, sessionId: session, storage, fetcher, keyFactory: randomUUID });
  client.accept(parent); await client.readCandidates();
  const delayed = client.forkCandidate(candidate);
  client.generation++; finish();
  await assert.rejects(delayed, /迟到回执/);
  assert.equal(client.sessionId, session);
  assert.ok(client.pending); assert.equal(client.pending.workflow_session_id, session);
  const fresh = TavernAdapter.createClient({ workflowId: workflow, sessionId: session,
    storage: { getItem: () => null, setItem: () => {} }, fetcher: async () => new Promise(resolve => {
      finish = () => resolve(response(packet)); }), keyFactory: randomUUID });
  fresh.accept(parent);
  const catalog = fresh.readCandidates();
  fresh.accept({ ...parent, session_revision: 5 });
  finish(); assert.equal(await catalog, null);
  assert.equal(fresh.forkCandidates, null);
  let writes = 0, original = null, posts = 0;
  const failedStorage = { getItem: key => raw.get(key) ?? null, setItem: (key, value) => {
    writes++; if (writes === 2) throw new Error("journal full");
    raw.set(key, value);
  }};
  raw.clear();
  const options = { workflowId: workflow, sessionId: session, storage: failedStorage,
    keyFactory: randomUUID, fetcher: async (url, options) => {
      const body = JSON.parse(options.body);
      if (url.endsWith("/queries")) return response(packet);
      if (url.endsWith("/commands")) { posts++; original = copy(body); return response(forkEnvelope(body)); }
      assert.equal(url, "/api/graph/consumer/receipts/read"); assert.deepEqual(body, original);
      return response(forkRead(body));
    } };
  const blocked = TavernAdapter.createClient(options);
  blocked.accept(parent); await blocked.readCandidates();
  await assert.rejects(blocked.forkCandidate(candidate), /journal full/);
  assert.equal(blocked.sessionId, session);
  assert.equal(blocked.consumer.workflow_session_id, session);
  assert.equal(blocked.pending.workflow_session_id, session);
  const recovered = TavernAdapter.createClient({ ...options, sessionId: id(3) });
  assert.equal(recovered.sessionId, session);
  await recovered.command("fork");
  assert.equal(recovered.sessionId, id(3));
  assert.equal(posts, 1);
  const savingFailedFirst = TavernAdapter.createClient({ ...options,
    storage: { getItem: () => null, setItem: () => { throw new Error("cannot journal"); } } });
  savingFailedFirst.accept(parent); await savingFailedFirst.readCandidates();
  await assert.rejects(savingFailedFirst.forkCandidate(candidate), /cannot journal/);
  assert.equal(savingFailedFirst.pending, null);
  assert.equal(posts, 1, "unpersisted request must not POST");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_tavern_copy_manifest_matches_delivered_files():
    manifest = json.loads((SHELL / "vendor" / "sillytavern" / "COPY-MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["upstream"]["commit"] == "06bde939fb1e9c4c8d8641d810f0a916b5bce127"
    assert manifest["upstream"]["license"] == "AGPL-3.0"
    root = SHELL.resolve()
    for item in manifest["copies"]:
        target = (root / item["destination"]).resolve()
        assert target.is_relative_to(root)
        assert sha256(target.read_bytes()).hexdigest() == item["destination_sha256"]
        assert len(item["source_sha256"]) == 64
        assert "modifications" in item
    assert {row["name"]: row["version"] for row in manifest["dependencies"]} == {
        "showdown": "2.1.0", "dompurify": "3.4.16", "lucide": "0.577.0",
    }
    html = (SHELL / "index.html").read_text(encoding="utf-8")
    assert "/static/graph-chat-core.js" in html
    assert "/tavern/legal.html" in html
    assert "script.js" not in html
    assert "mes_create_branch" in html
    assert "mes_create_branch icon-button" in html
    assert "mes_edit" not in html


def browser_run(tmp_path, mode="shell"):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    probe = subprocess.run(
        [node, "-e", "console.log(require('playwright').chromium.executablePath())"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if probe.returncode:
        pytest.skip("Set NODE_PATH to an installed Playwright package for offline browser acceptance")
    executable = os.environ.get("TAVERN_CHROMIUM_EXECUTABLE", probe.stdout.strip())
    if not Path(executable).is_file():
        pytest.skip("An installed Chromium or TAVERN_CHROMIUM_EXECUTABLE is required")
    destination = os.environ.get("TAVERN_BROWSER_EVIDENCE", str(tmp_path))
    Path(destination).mkdir(parents=True, exist_ok=True)
    node_run(NODE_SETUP + BROWSER_SCRIPT, destination, mode, timeout=90)


def test_tavern_offline_browser_shell_and_markdown(tmp_path):
    browser_run(tmp_path)


def test_tavern_offline_browser_completed_round_fork(tmp_path):
    browser_run(tmp_path, "fork")


BROWSER_SCRIPT = r"""
const { chromium } = require("playwright");
const destination = process.argv[3], calls = [], violations = [], browserErrors = [];
const forkMode = process.argv[4] === "fork";
const definition = { schema_version: 1, kind: "workflow.consumer-definition",
  workflow_definition_id: workflow, definition_revision: 1, name: "两轮离线聊天" };
const queries = ["consumer.definition", "consumer.sessions", "consumer.read", "output.history", "output.artifact.read"];
const commands = ["consumer.session.create", "consumer.run.start", "consumer.run.control"];
if (forkMode) { queries.push("consumer.candidate.list"); commands.push("consumer.candidate.fork"); }
const operation = (name, kind) => ({ name, kind, scope: "consumer", required_fields: [],
  optional_fields: [], idempotency: kind === "command" ? "service_receipt" : "none",
  idempotency_key_format: kind === "command" ? "uuid4" : null, http_status: 200 });
const application = { schema_version: 1, kind: "workflow.application", scope: "consumer",
  boundary: { caller: "trusted_local", remote_plugin_authentication: false },
  commands: commands.map(name => operation(name, "command")), queries: queries.map(name => operation(name, "query")) };
let activeView = copy(view), posted = 0, activeSession = session;
const firstView = copy(view);
let forkStage = "success", acceptedFork = null, forkCreates = 0;
const firstCandidate = { candidate_id: id(20), chain_run_id: chain, source_session_id: session,
  source_workflow_definition_id: workflow, source_definition_revision: 1 };
const lastCandidate = { ...firstCandidate, candidate_id: id(21), chain_run_id: id(8) };
function forkChild(whichSession) {
  return { ...copy(firstView), workflow_session_id: whichSession, session_revision: 1,
    outputs: [copy(firstOutput)], history: [{ ...historyRows[0], inherited: true }] };
}
function forkProjection() {
  return { schema_version: 1, kind: "workflow.consumer-candidates", workflow_definition_id: workflow,
    definition_revision: activeView.definition_revision, workflow_session_id: activeSession,
    session_revision: activeView.session_revision, data_revision: 0,
    head_revision: activeSession === session ? 3 : 1, status: activeView.status, can_fork: true,
    candidates: activeSession === session ? [firstCandidate, lastCandidate] : [firstCandidate] };
}
function emptyView(whichSession) { return { ...bare, workflow_session_id: whichSession, status: "idle",
  nodes: [{ node_binding_id: node, label: "展示", status: "idle", run_id: null,
    revision: null, diagnostic: null, budget: null }],
  outputs: [{ ...finalOutput, data_schema_version: undefined, availability: "unproduced", status: "idle",
    reason_code: null, payload: null, source: null, run_id: null, chain_run_id: null }], history: [] }; }
function directories() { return [session, id(3), ...(forkMode && forkCreates > 1 ? [id(9)] : [])].map(whichSession => ({ workflow_definition_id: workflow,
  definition_revision: 1, workflow_session_id: whichSession, session_revision: whichSession === session ? 4 : 1,
  status: whichSession === session || forkMode ? "succeeded" : "idle" })); }
function receiptEnvelope(body, newView, operationName) {
  const receipt = { workflow_definition_id: workflow, definition_revision: 1,
    workflow_session_id: newView.workflow_session_id, session_revision: newView.session_revision,
    chain_run_id: operationName === "create" ? null : chain, status: newView.status,
    idempotency_key: body.parameters.idempotency_key, operation: operationName };
  return { schema_version: 1, kind: "workflow.application-command",
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: body.operation,
      operation_scope: "consumer", idempotency_key: receipt.idempotency_key, request_sha256: "a".repeat(64),
      authority: "service_receipt", target: operationName === "create" ? {} : { session_id: newView.workflow_session_id },
      accepted: receipt }, result: { schema_version: 1, kind: "workflow.consumer.receipt", receipt, consumer: newView } };
}
function forkBrowserReceipt(body, whichSession) {
  const accepted = { workflow_definition_id: workflow, definition_revision: 1, workflow_session_id: whichSession,
    session_revision: 1, status: "succeeded", chain_run_id: null, operation: "fork",
    idempotency_key: body.parameters.idempotency_key, fork_source: { workflow_session_id: session,
      candidate_id: firstCandidate.candidate_id, chain_run_id: chain, workflow_definition_id: workflow,
      definition_revision: 1 } };
  const application = { schema_version: 1, kind: "workflow.application-receipt",
    operation: body.operation, operation_scope: "consumer", idempotency_key: accepted.idempotency_key,
    request_sha256: "a".repeat(64), authority: "service_receipt", target: { session_id: session }, accepted };
  return { accepted, application };
}
(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.TAVERN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.TAVERN_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
      if (url.origin !== "https://tavern.test") { violations.push(request.url()); return route.abort(); }
      if (pathname.startsWith("/api/")) {
        const body = request.method() === "POST" ? request.postDataJSON() : null;
        calls.push({ pathname, body });
        let value;
        if (pathname === "/api/graph/consumer/application") value = application;
        else if (pathname === "/api/graph/consumer/queries") {
          const { operation, parameters } = body;
          if (operation === "consumer.definition") value = definition;
          else if (operation === "consumer.sessions") value = directories();
          else if (operation === "consumer.read") {
            activeSession = parameters.session_id;
            activeView = parameters.session_id === session ? copy(firstView)
              : forkMode ? forkChild(parameters.session_id) : emptyView(id(3));
            value = activeView;
          } else if (operation === "output.history") value = {
            schema_version: 1, kind: "workflow.public-history", workflow_definition_id: workflow,
            definition_revision: 1, workflow_session_id: activeSession, session_revision: activeView.session_revision,
            node_id: node, port_id: "output", outputs: activeSession === session ? [firstOutput, finalOutput]
              : forkMode ? [firstOutput] : [] };
          else if (operation === "consumer.candidate.list") value = forkProjection();
          else if (operation === "output.artifact.read") {
            value = publicArtifact(parameters.run_id === run ? firstOutput : finalOutput, parameters.reference);
            value.workflow_session_id = activeSession; value.session_revision = activeView.session_revision;
            if (parameters.reference.output_id === entries[1].source_ref.output_id)
              value.value.text = "**加粗**\n\n`{{literal}}`\n\n<script>window.pwned=1</script><img src='https://evil.test/pixel' onerror='window.pwned=2'><a href='javascript:alert(1)' onclick='window.pwned=3'>恶意链接</a><style>body{display:none}</style>\n\n[正常链接](https://example.test)";
          } else throw new Error("Unauthorized mock query: " + operation);
        } else if (pathname === "/api/graph/consumer/commands") {
          posted++;
          if (body.operation === "consumer.session.create") value = receiptEnvelope(body, emptyView(id(3)), "create");
          else if (body.operation === "consumer.candidate.fork") {
            assert.equal(body.parameters.session_id, session);
            assert.equal(body.parameters.candidate_id, firstCandidate.candidate_id);
            assert.equal(body.parameters.expected_revision, 4);
            assert.equal(body.parameters.expected_data_revision, 0);
            assert.equal(body.parameters.expected_head_revision, 3);
            forkCreates++; acceptedFork = copy(body);
            const receipt = forkBrowserReceipt(body, forkCreates === 1 ? id(3) : id(9));
            if (forkStage === "unknown") return route.abort("failed");
            value = { schema_version: 1, kind: "workflow.application-command", receipt: receipt.application,
              result: { schema_version: 1, kind: "workflow.consumer.receipt", receipt: receipt.accepted,
                consumer: forkChild(id(3)) } };
          }
          else if (body.operation === "consumer.run.start") {
            assert.equal(body.parameters.inputs.message, "浏览器原始消息 {{macro}}");
            if (forkMode) assert.equal(body.parameters.session_id, id(9), "continuation must use actual matched child");
            return route.abort("failed");
          } else throw new Error("Unauthorized mock command");
        } else if (pathname === "/api/graph/consumer/receipts/read") {
          if (forkMode) assert.deepEqual(body, acceptedFork);
          if (forkMode && forkStage === "matched") {
            const receipt = forkBrowserReceipt(body, id(9));
            value = { schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
              reason_code: "receipt_matched", receipt: receipt.application, result: { receipt: receipt.accepted } };
          } else value = { schema_version: 1, kind: "workflow.application-receipt-read",
            outcome: "unresolved", reason_code: "request_outcome_unknown" };
        }
        else { violations.push(pathname); return route.abort(); }
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
      }
      const filename = pathname === "/static/graph-chat-core.js" ? core
        : pathname === "/tavern/" ? path.join(shell, "index.html")
          : pathname.startsWith("/tavern/") ? path.join(shell, pathname.slice("/tavern/".length)) : null;
      if (!filename || !fs.existsSync(filename)) { violations.push(pathname); return route.fulfill({ status: 404, body: "" }); }
      const ext = path.extname(filename), contentType = { ".html": "text/html", ".js": "text/javascript",
        ".css": "text/css", ".png": "image/png", ".json": "application/json" }[ext] ?? "text/plain";
      return route.fulfill({ status: 200, contentType, body: fs.readFileSync(filename),
        headers: pathname.endsWith("/") ? { "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'" } : {} });
    });
    const page = await context.newPage();
    page.on("pageerror", error => browserErrors.push(error.message));
    await page.goto(`https://tavern.test/tavern/?graph_workflow=${workflow}&graph_session=${session}`);
    await page.waitForFunction(() => document.querySelectorAll("#chat .mes").length === 4);
    await page.waitForFunction(() => document.querySelector("#transcript-status").textContent === "4 条展示记录");
    assert.equal(await page.locator("#error").textContent(), "");
    assert.equal(await page.locator("#chat .mes").count(), 4);
    assert.equal(await page.locator(".mes_text strong").first().textContent(), "加粗");
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    assert.equal(await page.locator(".mes_text img,.mes_text script,.mes_text style,.mes_text iframe").count(), 0);
    assert.equal(await page.locator(".mes_text [onclick],.mes_text [onerror],.mes_text [style]").count(), 0);
    assert.equal(await page.locator(".mes_text a[href^='javascript:']").count(), 0);
    assert.equal(await page.locator(".mes_copy svg").count(), 4);
    assert.equal(await page.locator("#new-session svg").count(), 1);
    assert.equal(await page.locator(".mes_edit,.swipe_right").count(), 0);
    await page.waitForFunction(expected => document.querySelectorAll(".mes_create_branch:not([hidden])").length === expected, forkMode ? 4 : 0);
    assert.equal(await page.locator(".mes_create_branch:visible").count(), forkMode ? 4 : 0);
    assert.equal(await page.locator(".avatar img").evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0)), true);
    await page.screenshot({ path: path.join(destination, forkMode ? "tavern-fork-desktop.png" : "tavern-desktop.png"), fullPage: true });
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await page.waitForTimeout(200);
    assert.equal(await page.locator("#chat .mes").count(), 4);
    await page.reload(); await page.waitForFunction(() => document.querySelectorAll("#chat .mes").length === 4);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(destination, forkMode ? "tavern-fork-mobile.png" : "tavern-mobile.png"), fullPage: true });
    const geometry = await page.evaluate(() => {
      const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right }; };
      return { width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth,
        header: rect(".tavern-header"), chat: rect("#chat"), composer: rect("#form_sheld") };
    });
    assert.ok(geometry.scrollWidth <= geometry.width, JSON.stringify(geometry));
    assert.ok(geometry.header.bottom <= geometry.chat.top, JSON.stringify(geometry));
    assert.ok(geometry.chat.bottom <= geometry.composer.top + 1, JSON.stringify(geometry));
    assert.ok(geometry.composer.bottom <= geometry.height + 1, JSON.stringify(geometry));
    if (forkMode) {
      const confirmations = [], before = JSON.stringify(firstView);
      page.on("dialog", async dialog => {
        confirmations.push(dialog.message()); await dialog.accept();
      });
      await page.getByRole("button", { name: "从第 1 个完成回合检查点分叉", exact: true }).first().click();
      await page.waitForFunction(() => new URL(location.href).searchParams.get("graph_session") === "00000000-0000-4000-8000-000000000003");
      await page.waitForFunction(() => document.querySelector("#transcript-status").textContent === "2 条展示记录");
      assert.equal(await page.locator("#chat .mes").count(), 2);
      assert.equal(await page.locator("#send_textarea").isEnabled(), true);
      assert.match(confirmations[0], /#1, #2/);
      assert.match(confirmations[0], /完整回合/);
      assert.match(confirmations[0], /父会话不变/);
      await page.locator("#sessions").selectOption(session);
      await page.waitForFunction(() => document.querySelectorAll("#chat .mes").length === 4
        && document.querySelectorAll(".mes_create_branch:not([hidden])").length === 4);
      assert.equal(JSON.stringify(firstView), before);
      forkStage = "unknown";
      await page.getByRole("button", { name: "从第 1 个完成回合检查点分叉", exact: true }).last().click();
      await page.waitForFunction(() => !document.querySelector("#pending").hidden);
      assert.equal(await page.locator("#sessions").isDisabled(), true);
      assert.equal(await page.locator("#new-session").isDisabled(), true);
      assert.equal(await page.locator("#send_but").isDisabled(), true);
      assert.equal(await page.locator(".mes_create_branch:visible").count(), 0);
      const pending = await page.evaluate(() => localStorage.getItem("workflow.tavern:workflow-chat:v1:" + new URL(location.href).searchParams.get("graph_workflow")));
      assert.equal(JSON.parse(pending).workflow_session_id, session);
      assert.equal(JSON.parse(pending).pending.candidate.candidate_id, firstCandidate.candidate_id);
      await page.goto(`https://tavern.test/tavern/?graph_workflow=${workflow}&graph_session=${id(3)}`);
      await page.waitForFunction(() => !document.querySelector("#pending").hidden
        && document.querySelectorAll("#chat .mes").length === 4);
      assert.equal(new URL(page.url()).searchParams.get("graph_session"), session);
      await page.getByRole("button", { name: "核实原请求", exact: true }).click();
      await page.waitForFunction(() => document.querySelector("#error").textContent.includes("保留原请求"));
      assert.equal(await page.evaluate(() => localStorage.getItem("workflow.tavern:workflow-chat:v1:" + new URL(location.href).searchParams.get("graph_workflow"))), pending);
      assert.equal(forkCreates, 2);
      forkStage = "matched";
      await page.getByRole("button", { name: "核实原请求", exact: true }).click();
      await page.waitForFunction(() => new URL(location.href).searchParams.get("graph_session") === "00000000-0000-4000-8000-000000000009"
        && document.querySelector("#pending").hidden && document.querySelector("#transcript-status").textContent === "2 条展示记录");
      assert.equal(forkCreates, 2, "matched receipt must not recreate the child");
      assert.equal(JSON.stringify(firstView), before);
      assert.equal(await page.locator("#error").textContent(), "");
      await page.locator("#send_textarea").fill("浏览器原始消息 {{macro}}");
      await page.getByRole("button", { name: "发送消息", exact: true }).click();
      await page.waitForFunction(() => !document.querySelector("#pending").hidden);
      assert.equal(posted, 3);
      assert.equal(await page.locator("#chat .mes").count(), 2, "unknown continuation is not a fact");
    } else {
    await page.getByRole("button", { name: "新建会话", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("#error").hidden
      || new URL(location.href).searchParams.get("graph_session") === "00000000-0000-4000-8000-000000000003");
    assert.equal(await page.locator("#error").textContent(), "", JSON.stringify(calls));
    await page.waitForFunction(() => new URL(location.href).searchParams.get("graph_session") === "00000000-0000-4000-8000-000000000003");
    await page.waitForFunction(() => !document.querySelector("#send_textarea").disabled);
    assert.equal(await page.locator("#chat .mes").count(), 0);
    await page.locator("#sessions").selectOption(session);
    await page.waitForFunction(() => document.querySelectorAll("#chat .mes").length === 4);
    await page.locator("#send_textarea").fill("浏览器原始消息 {{macro}}");
    await page.getByRole("button", { name: "发送消息", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("#pending").hidden);
    const pendingBefore = await page.evaluate(() => localStorage.getItem("workflow.tavern:workflow-chat:v1:" + new URL(location.href).searchParams.get("graph_workflow")));
    assert.equal(await page.locator("#sessions").isDisabled(), true);
    assert.equal(await page.locator("#new-session").isDisabled(), true);
    await page.reload(); await page.waitForFunction(() => !document.querySelector("#pending").hidden);
    assert.equal(await page.locator("#send_textarea").inputValue(), "浏览器原始消息 {{macro}}");
    await page.getByRole("button", { name: "核实原请求", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#error").textContent.includes("保留原请求"));
    assert.equal(await page.evaluate(() => localStorage.getItem("workflow.tavern:workflow-chat:v1:" + new URL(location.href).searchParams.get("graph_workflow"))), pendingBefore);
    assert.equal(posted, 2, "create once and start once; reopen/reconcile must not repost");
    assert.equal(await page.locator("#chat .mes").count(), 4, "unknown message must not become a fact");
    }
    assert.deepEqual(violations, []);
    assert.deepEqual(browserErrors, []);
    assert.ok(calls.every(call => ["/api/graph/consumer/application", "/api/graph/consumer/queries",
      "/api/graph/consumer/commands", "/api/graph/consumer/receipts/read"].includes(call.pathname)));
    console.log(JSON.stringify({ desktop: "tavern-desktop.png", mobile: "tavern-mobile.png", calls: calls.length }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
