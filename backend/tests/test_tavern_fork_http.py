"""Short-lived real HTTP consumer forks and cold receipt-only reconciliation."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
from importlib.resources import files
import json
import shutil
import subprocess
from threading import Thread
from uuid import uuid4

import pytest

from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.server import create_server
from test_agent_integration import run
from test_graph_application_receipt_custody import raw_database
from test_graph_receipt_http import post, serve_receipts
from test_graph_service import create
from test_graph_server import request
from test_server import call
from test_tavern_chat_integration import chat_graph
from test_tavern_fork_integration import candidates, fork_request


@contextmanager
def consumer_server(service):
    server = create_server(service, port=0, graph_service=service)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()
        assert not worker.is_alive()


def envelope(operation, parameters):
    return {"operation": operation, "parameters": parameters}


def test_real_http_fork_catalog_repeated_key_and_consumer_scopes(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "http-fork.sqlite")) as service:
        doc, chat = chat_graph(service)
        final = run(service, create(service, doc), "One completed HTTP round.")
        sid = final["workflow_session_id"]
        before = deepcopy(service.get_session(sid))
        parameters = {
            "session_id": sid, "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1,
        }
        candidate = candidates(service, final)["candidates"][0]
        original = fork_request(final, candidate["candidate_id"])
        body = envelope("consumer.candidate.fork", original)
        with consumer_server(service) as port:
            status, discovery = request(port, "GET", "/api/graph/consumer/application")
            assert status == 200
            assert "consumer.candidate.fork" in {row["name"] for row in discovery["commands"]}
            assert "consumer.candidate.list" in {row["name"] for row in discovery["queries"]}
            assert not {"candidate.select", "candidate.fork", "object.write"} & {
                row["name"] for row in discovery["commands"]}
            assert not {"candidate.list", "object.list", "object.artifact.read"} & {
                row["name"] for row in discovery["queries"]}
            assert request(port, "POST", "/api/graph/consumer/queries",
                           envelope("consumer.candidate.list", parameters)) == (200, candidates(service, final))
            assert request(port, "POST", "/api/graph/consumer/commands", body, origin=False)[0] == 403
            with ThreadPoolExecutor(max_workers=3) as pool:
                responses = list(pool.map(lambda _: request(
                    port, "POST", "/api/graph/consumer/commands", body), range(3)))
            assert {status for status, _ in responses} == {201}
            results = [response["result"] for _, response in responses]
            assert len({value["receipt"]["workflow_session_id"] for value in results}) == 1
            child_sid = results[0]["receipt"]["workflow_session_id"]
            assert service.get_session(sid) == before
            assert len(service.list_sessions(doc["workflow_definition_id"])) == 2
            for operation in ("candidate.select", "candidate.fork", "object.write"):
                status, denied = request(port, "POST", "/api/graph/consumer/commands", envelope(operation, {}))
                assert status == 403 and denied["error"]["reason_code"] == "application_scope_denied"
            assert request(port, "POST", f"/api/graph/sessions/{sid}/consumer/candidates/select", {})[0] == 404
            assert request(port, "POST", f"/api/graph/sessions/{sid}/consumer/candidates/fork", {})[0] == 404
            child = service.get_consumer(child_sid)
            public = child["outputs"][0]
            source_ref = public["payload"]["entries"][0]["source_ref"]
            status, artifact = request(port, "POST", "/api/graph/consumer/queries", envelope("output.artifact.read", {
                **parameters, "session_id": child_sid, "node_id": chat["presentation"]["node_binding_id"],
                "port_id": "display", "run_id": public["run_id"], "reference": source_ref,
            }))
            assert status == 200 and artifact["value"]["text"] == "One completed HTTP round."
            assert artifact["producer"]["workflow_session_id"] == sid
            changed = run(service, final, "Parent now changed.")
            stale = {**original, "idempotency_key": str(uuid4())}
            status, rejected = request(port, "POST", "/api/graph/consumer/commands",
                                       envelope("consumer.candidate.fork", stale))
            assert status == 409 and rejected["error"]["reason_code"] == "stale_revision"
            assert service.get_session(sid) == changed
            assert request(port, "POST", "/api/graph/consumer/commands", body)[1]["result"]["receipt"] == results[0]["receipt"]
            assert len(service.list_sessions(doc["workflow_definition_id"])) == 2


def test_lost_fork_result_resolves_only_frozen_receipt_on_cold_http_host(tmp_path, monkeypatch):
    database = tmp_path / "cold-http-fork.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        doc, _ = chat_graph(service)
        final = run(service, create(service, doc), "Receipt-only fork boundary.")
        candidate = candidates(service, final)["candidates"][0]
        original = fork_request(final, candidate["candidate_id"])
        accepted = GraphApplication(service).for_consumer().command("consumer.candidate.fork", original)
        expected = deepcopy(accepted["result"]["receipt"])
        child = service.get_session(expected["workflow_session_id"])
        continued = run(service, child, "Child after acceptance.")
        assert continued["status"] == "succeeded"
        assert len(service.list_sessions(doc["workflow_definition_id"])) == 2
    before = raw_database(database)
    with serve_receipts(database, monkeypatch) as (host, port):
        body = envelope("consumer.candidate.fork", original)
        status, resolved = post(port, "/api/graph/consumer/receipts/read", body)
        assert status == 200 and resolved["outcome"] == "matched", resolved
        assert resolved["result"] == {"receipt": expected}
        assert resolved["receipt"] == accepted["receipt"]
        assert "consumer" not in resolved["result"]
        assert post(port, "/api/graph/consumer/queries", envelope("receipt.read", body)) == (200, resolved)
        changed = deepcopy(original)
        changed["candidate_id"] = str(uuid4())
        assert post(port, "/api/graph/consumer/receipts/read",
                    envelope("consumer.candidate.fork", changed))[1]["outcome"] == "unresolved"
        assert post(port, "/api/graph/consumer/receipts/read",
                    envelope("candidate.fork", original))[1]["outcome"] == "unresolved"
        assert post(port, "/api/graph/consumer/receipts/read", body) == (200, resolved)
        assert host._graph is None
        status, _, raw = call(port, "GET", f"/tavern/?graph_workflow={doc['workflow_definition_id']}")
        assert status == 200 and b"/tavern/entry.js" in raw
    assert raw_database(database) == before


def test_actual_graph_chat_client_reads_forks_and_reconciles_over_real_http(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Actual shared client contract test requires Node.js")
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { randomUUID } = require("node:crypto");
const [corePath, base, workflow, parent, candidateId] = process.argv.slice(1);
vm.runInThisContext(fs.readFileSync(corePath, "utf8"), { filename: corePath });
const storageData = new Map(), requests = [];
const storage = { getItem: key => storageData.get(key) ?? null, setItem: (key, value) => storageData.set(key, value) };
let loseNextFork = false;
async function request(path, options = {}) {
  const body = options.body ? JSON.parse(options.body) : null;
  requests.push({ path, body });
  const response = await fetch(base + path, { ...options, headers: {
    "Content-Type": "application/json", Origin: base,
  } });
  const value = await response.json();
  if (!response.ok) throw GraphChat.httpFailure(value, response.status);
  if (loseNextFork && body?.operation === "consumer.candidate.fork") {
    loseNextFork = false;
    throw new Error("Simulated lost accepted fork response");
  }
  return value;
}
function client() {
  return new GraphChat.GraphChatClient({ workflowId: workflow, sessionId: parent, storage, request, keyFactory: randomUUID });
}
(async () => {
  const first = client();
  await first.discover(); await first.refresh();
  const projected = await first.readCandidates();
  const candidate = projected.candidates.find(row => row.candidate_id === candidateId);
  assert.ok(candidate);
  const accepted = await first.forkCandidate(candidate);
  assert.equal(accepted.receipt.status, "succeeded");
  assert.equal(first.sessionId, accepted.receipt.workflow_session_id);
  assert.equal(first.pending, null);
  assert.equal(first.consumer.history.length, 1);
  assert.equal(first.consumer.history[0].chain_run_id, candidate.chain_run_id);
  const output = first.consumer.outputs.find(row => row.data_type === "TAVERN_CHAT_DISPLAY");
  assert.ok(output);
  const texts = [];
  for (const entry of output.payload.entries) {
    const artifact = await first.readDisplayEntry(output, entry);
    texts.push(GraphChat.displayEntryText(artifact));
    assert.equal(artifact.producer.chain_run_id, candidate.chain_run_id);
    assert.equal(artifact.producer.workflow_session_id, parent);
  }
  assert.deepEqual(texts, ["First real client round.", "Reply: First real client round."]);
  await first.selectSession(parent);
  const available = await first.readCandidates();
  loseNextFork = true;
  await assert.rejects(first.forkCandidate(available.candidates.find(row => row.candidate_id === candidateId)), /lost/);
  const pending = GraphChat.clone(first.pending);
  assert.equal(first.sessionId, parent);
  const commandCount = requests.filter(row => row.path === "/api/graph/consumer/commands"
    && row.body?.operation === "consumer.candidate.fork").length;
  assert.equal(commandCount, 2);
  const reopened = client();
  assert.deepEqual(reopened.pending, pending);
  const matched = await reopened.command("fork");
  assert.equal(matched.receipt.idempotency_key, pending.body.idempotency_key);
  assert.equal(matched.receipt.fork_source.chain_run_id, candidate.chain_run_id);
  assert.notEqual(reopened.sessionId, parent);
  assert.notEqual(reopened.sessionId, accepted.receipt.workflow_session_id);
  assert.equal(reopened.pending, null);
  assert.equal(reopened.consumer, null);
  assert.equal(requests.filter(row => row.path === "/api/graph/consumer/commands"
    && row.body?.operation === "consumer.candidate.fork").length, commandCount);
  assert.equal(requests.filter(row => row.path === "/api/graph/consumer/receipts/read").length, 1);
  await reopened.refresh();
  assert.equal(reopened.consumer.history.length, 1);
  console.log(JSON.stringify({ first_child: accepted.receipt.workflow_session_id, reconciled_child: reopened.sessionId,
    command_count: commandCount, receipt_count: 1, candidate_chain: candidate.chain_run_id }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    core = files("phase1_agent").joinpath("static", "graph-chat-core.js")
    with closing(GraphWorkflowService(tmp_path / "actual-client.sqlite")) as service:
        doc, _ = chat_graph(service)
        first = run(service, create(service, doc), "First real client round.")
        first_candidate = candidates(service, first)["candidates"][0]
        final = run(service, first, "Second excluded parent round.")
        before = deepcopy(service.get_session(final["workflow_session_id"]))
        with consumer_server(service) as port:
            result = subprocess.run([
                node, "-e", script, str(core), f"http://127.0.0.1:{port}",
                doc["workflow_definition_id"], final["workflow_session_id"], first_candidate["candidate_id"],
            ], capture_output=True, text=True, timeout=40, check=False)
        assert result.returncode == 0, result.stderr + result.stdout
        observed = json.loads(result.stdout)
        assert observed["command_count"] == 2 and observed["receipt_count"] == 1
        assert observed["candidate_chain"] == first["selected_chain_run_id"]
        assert service.get_session(final["workflow_session_id"]) == before
        assert len(service.list_sessions(doc["workflow_definition_id"])) == 3
        for child_sid in (observed["first_child"], observed["reconciled_child"]):
            child = service.get_session(child_sid)
            assert child["history_refs"] == [first["selected_chain_run_id"]]
            assert len(child["objects"]["tavern-chat"]["value"]["entries"]) == 2
