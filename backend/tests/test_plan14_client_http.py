"""Actual consumer script calls only its declared event path over isolated HTTP."""

import json
from contextlib import closing
from pathlib import Path
import shutil
import subprocess

import pytest

from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.host_sdk import ObjectBinding
from test_graph_server import host_server, request
from test_graph_service import edge, node
from test_plan13a_client_http import display_document


def event_document(service):
    document = display_document(service)
    source = node(service.registry, "tools.current-input", 3901, input_name="edit")
    conversion = node(service.registry, "tools.text-to-json", 3902)
    assignment = node(service.registry, "tools.variable-assign", 3903, object_key="variable/value")
    dormant_agent = node(service.registry, "agents.execute", 3904)
    document["nodes"].extend([source, conversion, assignment, dormant_agent])
    document["edges"].extend([
        edge(source, conversion, 3901),
        edge(conversion, assignment, 3902, target_port="value"),
    ])
    document["object_bindings"].append(ObjectBinding(
        "variable/value", "workflow.variable", 1, "shared",
        readers=(assignment["node_binding_id"],), writers=(assignment["node_binding_id"],),
        default_value={"registered": True, "name": "value", "value_type": "string",
                       "assigned": True, "value": "original"},
    ).to_dict())
    document["event_bindings"] = [
        {"event_id": "variable.set", "schema_version": 1, "display_name": "Update variable",
         "audience": "consumer", "target_node_ids": [assignment["node_binding_id"]],
         "payload_schema": {"type": "object", "properties": {"edit": {"type": "string"}},
                            "required": ["edit"], "additionalProperties": False}},
        {"event_id": "variable.private", "schema_version": 1, "display_name": "Private action",
         "audience": "management", "target_node_ids": [assignment["node_binding_id"]],
         "payload_schema": {"type": "object", "properties": {"edit": {"type": "string"}},
                            "required": ["edit"], "additionalProperties": False}},
    ]
    return document


def test_http_event_scope_schema_and_exact_target_rejection(tmp_path):
    with host_server(tmp_path) as (host, port):
        service = host.graph_service
        document = event_document(service)
        service.save_definition(document, expected_revision=0, idempotency_key="event-scope-save")
        initial = service.create_session(document["workflow_definition_id"], 1, idempotency_key="event-scope-create")
        other = service.create_session(document["workflow_definition_id"], 1, idempotency_key="event-scope-other")
        sid = initial["workflow_session_id"]
        parameters = {
            "session_id": sid, "workflow_definition_id": document["workflow_definition_id"],
            "definition_revision": 1, "event_id": "variable.set", "event_schema_version": 1,
            "payload": {"edit": '"accepted"'}, "expected_revision": initial["revision"],
            "idempotency_key": "event-scope-submit",
        }
        for changes, expected_code in [
            ({"event_id": "variable.private"}, "graph_event_not_public"),
            ({"definition_revision": 2}, "consumer_definition_mismatch"),
            ({"payload": {"edit": 42}}, "graph_event_payload_invalid"),
            ({"target_node_ids": [document["nodes"][0]["node_binding_id"]]}, "invalid_request"),
        ]:
            status, failure = request(port, "POST", "/api/graph/consumer/commands", {
                "operation": "consumer.event.submit", "parameters": {**parameters, **changes},
            })
            assert status in (400, 403, 409), failure
            assert failure["error"]["reason_code"] == expected_code
        assert service.get_session(sid)["chains"] == []
        status, accepted = request(port, "POST", "/api/graph/consumer/commands", {
            "operation": "consumer.event.submit", "parameters": parameters,
        })
        assert status == 202, accepted
        chain_id = accepted["result"]["receipt"]["chain_run_id"]
        service.wait(chain_id)
        status, event = request(port, "POST", "/api/graph/consumer/queries", {
            "operation": "consumer.event.read", "parameters": {"session_id": sid, "chain_id": chain_id},
        })
        assert status == 200 and event["status"] == "succeeded"
        assert event["execution_kind"] == "event"
        assert not {"inputs", "outputs", "objects", "private_states", "manifests"} & event.keys()
        assert "accepted" not in json.dumps(event)
        status, denied = request(port, "POST", "/api/graph/consumer/queries", {
            "operation": "consumer.event.read",
            "parameters": {"session_id": other["workflow_session_id"], "chain_id": chain_id},
        })
        assert status == 404 and denied["error"]["reason_code"] == "not_found"
        status, replay = request(port, "POST", "/api/graph/consumer/commands", {
            "operation": "consumer.event.submit", "parameters": parameters,
        })
        assert status == 202 and replay["receipt"] == accepted["receipt"]
        assert service.get_session(sid)["objects"]["variable/value"]["revision"] == 2
        assert service.get_session(other["workflow_session_id"])["objects"]["variable/value"]["revision"] == 1
        assert service.list_graph_candidates(sid)["candidates"] == []


def test_shipped_client_event_unknown_replay_and_following_round_checkpoint(tmp_path):
    node_binary = shutil.which("node")
    if node_binary is None:
        pytest.skip("Node.js is required for shipped consumer integration")
    core = Path(__file__).resolve().parents[1] / "src" / "phase1_agent" / "static" / "graph-chat-core.js"
    with host_server(tmp_path) as (host, port):
        document = event_document(host.graph_service)
        status, saved = request(port, "POST", "/api/graph/commands", {
            "operation": "definition.save", "parameters": {
                "document": document, "expected_revision": 0, "idempotency_key": "event-http-save",
            },
        })
        assert status == 201, saved
        script = r"""
const { readFileSync } = require("node:fs"), { runInNewContext } = require("node:vm");
const { randomUUID } = require("node:crypto"), assert = require("node:assert/strict");
const scope = {}, data = new Map(), calls = [];
runInNewContext(readFileSync(process.argv[1], "utf8"), scope);
const base = process.argv[2], workflowId = process.argv[3];
let discardEventResponse = false, lastEventRequest = null;
async function transport(path, options = {}) {
  const packet = options.body ? JSON.parse(options.body) : null;
  const call = { path, method: options.method ?? "GET", operation: packet?.operation,
    packet, rawBody: options.body };
  calls.push(call);
  const response = await fetch(base + path, { ...options, headers: {
    "Content-Type": "application/json", Origin: base }, cache: "no-store" });
  const value = await response.json();
  if (!response.ok) throw scope.GraphChat.httpFailure(value, response.status);
  call.response = value;
  if (path === "/api/graph/consumer/commands" && packet?.operation === "consumer.event.submit") {
    lastEventRequest = packet;
    if (discardEventResponse) { discardEventResponse = false; throw new Error("response lost after acceptance"); }
  }
  return value;
}
const options = { workflowId, request: transport, keyFactory: randomUUID, storage: {
  getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) } };
async function finished(client) {
  const deadline = Date.now() + 10000;
  while (!client.consumer.can_submit) {
    assert.ok(Date.now() < deadline, "workflow failed to settle");
    assert.ok(!["failed", "archive_failed", "recovery_unavailable"].includes(client.consumer.status),
      JSON.stringify(client.consumer));
    await new Promise(resolve => setTimeout(resolve, 20)); await client.refresh();
  }
}
(async () => {
  const client = new scope.GraphChat.GraphChatClient(options);
  await client.discover(); const definition = await client.readDefinition();
  await client.command("create", { definition_revision: definition.definition_revision });
  await client.command("start", { inputs: { text: "first" } }); await finished(client);
  const firstOutput = client.consumer.outputs.find(item => item.data_type === "FRONTEND_DISPLAY");
  assert.equal(firstOutput.payload.entries.length, 2);
  const bindingPacket = await client.readEventBindings();
  assert.equal(bindingPacket.kind, "workflow.event-bindings");
  assert.equal(bindingPacket.bindings.length, 1);
  const binding = bindingPacket.bindings[0];
  assert.equal(binding.event_id, "variable.set");
  await assert.rejects(client.query("consumer.event.submit", {}));
  await assert.rejects(client.query("event.bindings", { session_id: client.sessionId,
    workflow_definition_id: workflowId, definition_revision: 1 }));
  discardEventResponse = true;
  await assert.rejects(client.submitEvent(binding, { edit: '"updated"' }));
  assert.ok(client.pending, "unknown accepted event lost the original request");
  const original = JSON.parse(JSON.stringify(client.pending));
  const eventKey = lastEventRequest.parameters.idempotency_key;
  const submittedEvent = calls.find(item => item.path === "/api/graph/consumer/commands"
    && item.operation === "consumer.event.submit");
  assert.equal(calls.filter(item => item.path === "/api/graph/consumer/commands"
    && item.operation === "consumer.event.submit").length, 1);
  const mutationCount = calls.filter(item => item.path === "/api/graph/consumer/commands").length;
  const recovered = new scope.GraphChat.GraphChatClient(options);
  await recovered.discover();
  assert.deepEqual(JSON.parse(JSON.stringify(recovered.pending)), original);
  const reconciled = await recovered.command();
  assert.equal(reconciled.receipt.idempotency_key, eventKey);
  assert.equal(reconciled.receipt.workflow_session_id, recovered.sessionId);
  assert.deepEqual(reconciled.receipt, submittedEvent.response.result.receipt);
  assert.equal(recovered.pending, null);
  assert.equal(calls.filter(item => item.path === "/api/graph/consumer/commands").length, mutationCount);
  const eventPacket = calls.filter(item => item.path === "/api/graph/consumer/commands"
    && item.operation === "consumer.event.submit");
  assert.equal(eventPacket.length, 1, "receipt recovery resent the event mutation");
  const receiptReads = calls.filter(item => item.path === "/api/graph/consumer/receipts/read");
  assert.equal(receiptReads.length, 1);
  assert.equal(receiptReads[0].response.kind, "workflow.application-receipt-read");
  assert.equal(receiptReads[0].response.outcome, "matched");
  assert.equal(receiptReads[0].method, "POST");
  assert.equal(receiptReads[0].operation, "consumer.event.submit");
  assert.equal(receiptReads[0].rawBody, submittedEvent.rawBody, "receipt recovery changed the original event envelope");
  assert.deepEqual(receiptReads[0].packet.parameters, {
    session_id: original.workflow_session_id, ...original.body });
  assert.equal(lastEventRequest.parameters.idempotency_key, eventKey);
  await recovered.refresh(); await finished(recovered);
  const unchanged = recovered.consumer.outputs.find(item => item.data_type === "FRONTEND_DISPLAY");
  assert.equal(unchanged.payload.entries.length, 2, "event implicitly executed the round display chain");
  await recovered.command("start", { inputs: { text: "second" } }); await finished(recovered);
  assert.equal(recovered.consumer.outputs.find(item => item.data_type === "FRONTEND_DISPLAY").payload.entries.length, 4);
  assert.ok(calls.every(item => ["/api/graph/consumer/application", "/api/graph/consumer/queries",
    "/api/graph/consumer/commands", "/api/graph/consumer/receipts/read"].includes(item.path)));
  console.log(JSON.stringify({ sessionId: recovered.sessionId, eventKey }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node_binary, "-e", script, str(core), f"http://127.0.0.1:{port}", document["workflow_definition_id"]],
            capture_output=True, text=True, encoding="utf-8", timeout=45, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads(result.stdout)
        service = host.graph_service
        view = service.get_session(evidence["sessionId"])
        events = [chain for chain in view["chains"] if chain.get("execution_kind") == "event"]
        assert len(view["chains"]) == 3 and len(events) == 1
        event = events[0]
        assert event["event"]["idempotency_key"] == evidence["eventKey"]
        assert event["status"] == "succeeded"
        assert event["ordered_nodes"] == [item["node_binding_id"] for item in document["nodes"][-4:-1]]
        assert view["objects"]["variable/value"]["value"]["value"] == "updated"
        assert view["objects"]["variable/value"]["revision"] == 2
        candidates = service.list_graph_candidates(view["workflow_session_id"])["candidates"]
        assert len(candidates) == 2
        with closing(service._store()) as store:
            repo = GraphRecordStore(store)
            candidate = next(item for item in candidates if item["selected"])
            commit = repo.get("workflow_commit", commit_id=candidate["candidate_id"])
            manifest = service._manifests(repo).get("state_snapshot", commit["state_snapshot_id"])
            objects = service._objects(repo).at_manifest(manifest["objects"])
            assert objects["variable/value"]["value"]["value"] == "updated"
            assert not any(row["source"].get("chain_run_id") == event["chain_run_id"]
                           for row in repo.rows("workflow_commit"))
        assert not hasattr(host, "_legacy") and not hasattr(service, "_native_runtime")
