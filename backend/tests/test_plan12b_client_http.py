"""Run the shipped JavaScript consumer against a temporary real HTTP host."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_graph_application import public_document
from test_graph_server import host_server, request


def test_shipped_consumer_replays_lost_named_receipt_without_running_twice(tmp_path, monkeypatch):
    node_binary = shutil.which("node")
    if node_binary is None:
        pytest.skip("Node.js is required for the shipped JavaScript consumer integration")
    core_path = Path(__file__).resolve().parents[1] / "src" / "phase1_agent" / "static" / "graph-chat-core.js"
    with host_server(tmp_path, monkeypatch) as (host, port):
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
  calls.push(path);
  const response = await fetch(base + path, { ...options,
    headers: { "Content-Type": "application/json", Origin: base }, cache: "no-store" });
  const value = await response.json();
  if (!response.ok) throw httpFailure(value, response.status);
  const body = options.body ? JSON.parse(options.body) : null;
  if (body?.operation === "consumer.run.start") {
    starts.push(body);
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
  const originalKey = client.pending.body.idempotency_key;
  const recovered = new GraphChatClient(options);
  assert.equal(recovered.sessionId, sessionId);
  await recovered.discover();
  await recovered.refresh();
  // A different new intent must still replay the frozen unknown start.
  await recovered.command("create", { definition_revision: 999 });
  assert.equal(recovered.pending, null);
  assert.equal(starts.length, 2);
  assert.equal(starts[0].parameters.idempotency_key, originalKey);
  assert.equal(JSON.stringify(starts[0]), JSON.stringify(starts[1]));
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
  assert.ok(calls.every(path => ["/api/graph/consumer/application",
    "/api/graph/consumer/commands", "/api/graph/consumer/queries"].includes(path)));
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
        final = host.graph_service.get_session(evidence["sessionId"])
        assert final["status"] == "succeeded" and len(final["chains"]) == 1
        assert len(host.graph_service.list_graph_candidates(evidence["sessionId"])["candidates"]) == 1
        assert host._legacy is None and host.graph_service._native_runtime is None
