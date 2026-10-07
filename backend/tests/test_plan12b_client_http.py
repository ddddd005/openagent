"""Run the shipped JavaScript consumer against a temporary real HTTP host."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_graph_application import public_document
from test_graph_server import host_server, request


def test_shipped_consumer_replays_lost_named_receipt_without_running_twice(tmp_path):
    node_binary = shutil.which("node")
    if node_binary is None:
        pytest.skip("Node.js is required for the shipped JavaScript consumer integration")
    core_path = Path(__file__).resolve().parents[1] / "src" / "phase1_agent" / "static" / "graph-chat-core.js"
    with host_server(tmp_path) as (host, port):
        document = public_document(host.graph_service)
        status, _ = request(port, "POST", "/api/graph/commands", {
            "operation": "definition.save", "parameters": {
                "document": document, "expected_revision": 0, "idempotency_key": "http-consumer-definition",
            },
        })
        assert status == 201
        script = r"""
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const { randomUUID } = require("node:crypto");
const assert = require("node:assert/strict");
const scope = {};
runInNewContext(readFileSync(process.argv[1], "utf8"), scope);
const { GraphChatClient, httpFailure } = scope.GraphChat;
const base = process.argv[2], workflowId = process.argv[3];
const values = new Map(), calls = [], starts = [];
const storage = { getItem: key => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value) };
let loseStartReceipt = true;
async function transport(path, options = {}) {
  const body = options.body ? JSON.parse(options.body) : null;
  const call = { path, method: options.method ?? "GET", body, rawBody: options.body };
  calls.push(call);
  const response = await fetch(base + path, { ...options,
    headers: { "Content-Type": "application/json", Origin: base }, cache: "no-store" });
  const value = await response.json();
  if (!response.ok) throw httpFailure(value, response.status);
  call.response = value;
  if (path === "/api/graph/consumer/commands" && body?.operation === "consumer.run.start") {
    starts.push(call);
    if (loseStartReceipt) {
      loseStartReceipt = false;
      throw new Error("simulated lost accepted response");
    }
  }
  return value;
}
(async () => {
  const options = { workflowId, storage, request: transport, keyFactory: randomUUID };
  const client = new GraphChatClient(options);
  await client.discover();
  const definition = await client.readDefinition();
  await client.command("create", { definition_revision: definition.definition_revision });
  const sessionId = client.sessionId;
  await assert.rejects(client.command("start", { inputs: {} }), /lost accepted response/);
  assert.ok(client.pending);
  const original = JSON.parse(JSON.stringify(client.pending));
  const originalKey = original.body.idempotency_key;
  assert.equal(starts.length, 1);
  assert.equal(starts[0].body.parameters.idempotency_key, originalKey);
  const mutationCount = calls.filter(call => call.path === "/api/graph/consumer/commands").length;
  const recovered = new GraphChatClient(options);
  assert.equal(recovered.sessionId, sessionId);
  assert.deepEqual(JSON.parse(JSON.stringify(recovered.pending)), original);
  await recovered.discover();
  await recovered.refresh();
  assert.deepEqual(JSON.parse(JSON.stringify(recovered.pending)), original);
  // A different intent must only reconcile the frozen unknown start.
  const reconciled = await recovered.command("create", { definition_revision: 999 });
  assert.equal(reconciled.receipt.idempotency_key, originalKey);
  assert.equal(reconciled.receipt.workflow_session_id, sessionId);
  assert.deepEqual(reconciled.receipt, starts[0].response.result.receipt);
  assert.equal(recovered.pending, null);
  assert.equal(starts.length, 1, "receipt recovery resent the start mutation");
  assert.equal(calls.filter(call => call.path === "/api/graph/consumer/commands").length, mutationCount);
  const reads = calls.filter(call => call.path === "/api/graph/consumer/receipts/read");
  assert.equal(reads.length, 1);
  assert.equal(reads[0].response.kind, "workflow.application-receipt-read");
  assert.equal(reads[0].response.outcome, "matched");
  assert.equal(reads[0].method, "POST");
  assert.equal(reads[0].rawBody, starts[0].rawBody, "receipt recovery changed the original request envelope");
  assert.deepEqual(reads[0].body.parameters, {
    session_id: sessionId, inputs: original.body.inputs,
    expected_revision: original.body.expected_revision, idempotency_key: originalKey });
  const deadline = Date.now() + 10000;
  while (recovered.consumer.status !== "succeeded") {
    assert.ok(Date.now() < deadline, "temporary graph did not complete");
    await new Promise(resolve => setTimeout(resolve, 20));
    await recovered.refresh();
  }
  assert.equal(recovered.consumer.history.length, 1);
  const output = recovered.consumer.outputs.find(item => item.availability === "produced");
  assert.equal(output.payload.text, "pear pear");
  const history = await recovered.readHistory(output);
  assert.equal(history.outputs.length, 1);
  await recovered.listRegistrations({ limit: 1 });
  assert.ok(calls.every(call => ["/api/graph/consumer/application",
    "/api/graph/consumer/commands", "/api/graph/consumer/queries",
    "/api/graph/consumer/receipts/read"].includes(call.path)));
  console.log(JSON.stringify({ sessionId, calls: calls.length, starts: starts.length }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node_binary, "-e", script, str(core_path), f"http://127.0.0.1:{port}",
             document["workflow_definition_id"]],
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads(result.stdout)
        assert evidence["starts"] == 1
        final = host.graph_service.get_session(evidence["sessionId"])
        assert final["status"] == "succeeded" and len(final["chains"]) == 1
        assert len(host.graph_service.list_graph_candidates(evidence["sessionId"])["candidates"]) == 1
        assert not hasattr(host, "_legacy") and not hasattr(host.graph_service, "_native_runtime")
