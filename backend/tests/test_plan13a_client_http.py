"""Shipped public consumer resolves multi-round display references over real HTTP."""

import json
from contextlib import closing
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.graph_service import GraphWorkflowService
from test_graph_server import host_server, request
from test_graph_service import create, edge, node
from graph_test_plugin import run_current_graph as run


def display_document(service):
    """Two explicit writes with a fresh read between them, without an Agent."""
    source = node(service.registry, "tools.current-input", 1901)
    processed = node(service.registry, "tools.regex", 1902, pattern="^", replacement="答：")
    first_read = node(service.registry, "frontend.state.output", 1903)
    first_append = node(service.registry, "frontend.state.append", 1904, role="user")
    second_read = node(service.registry, "frontend.state.output", 1905)
    second_append = node(service.registry, "frontend.state.append", 1906)
    presentation = node(service.registry, "frontend.presentation", 1907)
    presentation["public_outputs"] = ["display"]
    return {
        "schema_version": 2,
        "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Explicit public display integration",
        "nodes": [source, processed, first_read, first_append, second_read, second_append, presentation],
        "edges": [
            edge(source, processed, 1901),
            edge(first_read, first_append, 1902, source_port="view", target_port="view"),
            edge(source, first_append, 1903, target_port="content"),
            edge(second_read, second_append, 1904, source_port="view", target_port="view"),
            edge(processed, second_append, 1905, target_port="content"),
            edge(second_append, presentation, 1906, source_port="view", target_port="view"),
        ],
        "control_edges": [{
            "edge_id": str(uuid4()), "source_node_id": first_append["node_binding_id"],
            "target_node_id": second_read["node_binding_id"],
        }],
        "execution_roots": [presentation["node_binding_id"]],
        "object_bindings": [ObjectBinding(
            "frontend", "workflow.frontend-state", 1, "shared",
            readers=tuple(item["node_binding_id"] for item in
                          [first_read, first_append, second_read, second_append]),
            writers=tuple(item["node_binding_id"] for item in [first_append, second_append]),
        ).to_dict()],
        "package_lock": list(service.registry.execution_package_lock),
    }


def test_shipped_consumer_reads_two_rounds_and_reopens_reference_only_display(tmp_path, monkeypatch):
    node_binary = shutil.which("node")
    if node_binary is None:
        pytest.skip("Node.js is required for the shipped JavaScript consumer integration")
    core_path = Path(__file__).resolve().parents[1] / "src" / "phase1_agent" / "static" / "graph-chat-core.js"
    with host_server(tmp_path) as (host, port):
        document = display_document(host.graph_service)
        status, saved = request(port, "POST", "/api/graph/commands", {
            "operation": "definition.save", "parameters": {
                "document": document, "expected_revision": 0, "idempotency_key": "display-http-save",
            },
        })
        assert status == 201, saved
        script = r"""
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const { randomUUID } = require("node:crypto");
const assert = require("node:assert/strict");
const scope = {};
runInNewContext(readFileSync(process.argv[1], "utf8"), scope);
const { GraphChatClient, httpFailure } = scope.GraphChat;
const base = process.argv[2], workflowId = process.argv[3];
const values = new Map(), calls = [];
const storage = { getItem: key => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value) };
async function transport(path, options = {}) {
  calls.push({ path, body: options.body ? JSON.parse(options.body) : null });
  const response = await fetch(base + path, { ...options,
    headers: { "Content-Type": "application/json", Origin: base }, cache: "no-store" });
  const value = await response.json();
  if (!response.ok) throw httpFailure(value, response.status);
  return value;
}
async function finish(client) {
  const deadline = Date.now() + 10000;
  while (client.consumer.status !== "succeeded") {
    assert.ok(Date.now() < deadline, "temporary display graph did not complete");
    assert.ok(!["failed", "archive_failed", "closed"].includes(client.consumer.status),
      JSON.stringify(client.consumer.diagnostics));
    await new Promise(resolve => setTimeout(resolve, 20));
    await client.refresh();
  }
  return client.consumer.outputs.find(item => item.data_type === "FRONTEND_DISPLAY");
}
async function textEntries(client, output) {
  const texts = [];
  for (const entry of output.payload.entries) {
    const result = await client.readDisplayEntry(output, entry);
    assert.equal(result.data_type, "TEXT");
    assert.equal(result.data_schema_version, 2);
    assert.equal(result.value.kind, "workflow.text");
    assert.deepEqual(Object.keys(result.producer).sort(),
      ["workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"].sort());
    assert.ok(!("config" in result) && !("input_refs" in result));
    texts.push(result.value.text);
  }
  return texts;
}
(async () => {
  const options = { workflowId, storage, request: transport, keyFactory: randomUUID };
  const client = new GraphChatClient(options);
  await client.discover();
  const definition = await client.readDefinition();
  await client.command("create", { definition_revision: definition.definition_revision });
  await client.command("start", { inputs: { text: "第一问" } });
  const first = await finish(client);
  assert.equal(first.payload.entries.length, 2);
  assert.equal(JSON.stringify(first.payload.entries.map(item => item.role)),
    JSON.stringify(["user", "assistant"]));
  assert.equal(JSON.stringify(await textEntries(client, first)), JSON.stringify(["第一问", "答：第一问"]));
  await client.command("start", { inputs: { text: "第二问" } });
  const second = await finish(client);
  assert.equal(second.payload.entries.length, 4);
  assert.equal(JSON.stringify(second.payload.entries.slice(0, 2)), JSON.stringify(first.payload.entries));
  const expected = ["第一问", "答：第一问", "第二问", "答：第二问"];
  assert.equal(JSON.stringify(await textEntries(client, second)), JSON.stringify(expected));
  assert.equal(new Set(second.payload.entries.map(item => item.entry_id)).size, 4);
  const recovered = new GraphChatClient(options);
  assert.equal(recovered.sessionId, client.sessionId);
  await recovered.discover();
  await recovered.refresh();
  const reopened = recovered.consumer.outputs.find(item => item.data_type === "FRONTEND_DISPLAY");
  assert.equal(JSON.stringify(await textEntries(recovered, reopened)), JSON.stringify(expected));
  const history = await recovered.readHistory(reopened);
  assert.equal(history.outputs.length, 2);
  const previous = history.outputs.find(item => item.run_id === first.run_id);
  assert.ok(previous);
  assert.equal(JSON.stringify(await textEntries(recovered, previous)), JSON.stringify(["第一问", "答：第一问"]));
  assert.ok(calls.every(call => ["/api/graph/consumer/application",
    "/api/graph/consumer/commands", "/api/graph/consumer/queries"].includes(call.path)));
  assert.equal(calls.filter(call => call.body?.operation === "consumer.run.start").length, 2);
  assert.ok(calls.some(call => call.body?.operation === "output.artifact.read"));
  console.log(JSON.stringify({ sessionId: recovered.sessionId }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node_binary, "-e", script, str(core_path), f"http://127.0.0.1:{port}",
             document["workflow_definition_id"]],
            capture_output=True, text=True, encoding="utf-8", timeout=40, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads(result.stdout)
        final = host.graph_service.get_session(evidence["sessionId"])
        assert final["status"] == "succeeded" and len(final["chains"]) == 2
        assert len(host.graph_service.list_graph_candidates(evidence["sessionId"])["candidates"]) == 2
        retained = final["objects"]["frontend"]["value"]["entries"]
        assert len(retained) == 4
        assert all(set(entry) == {"entry_id", "role", "source_ref"} for entry in retained)
        assert not hasattr(host, "_legacy") and not hasattr(host.graph_service, "_native_runtime")


def test_actual_workbench_wiring_generates_a_runnable_shared_display(tmp_path):
    """Catch disagreements between the TS graph editor and Python compiler."""
    node_binary = shutil.which("node")
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    typescript = frontend / "node_modules" / "typescript"
    if node_binary is None or not typescript.is_dir():
        pytest.skip("Node.js and the installed frontend TypeScript dependency are required")
    with closing(GraphWorkflowService(tmp_path / "workbench-wiring.sqlite")) as service:
        document = display_document(service)
        document["nodes"] = document["nodes"][:2]
        document["edges"] = document["edges"][:1]
        document.update(object_bindings=[], control_edges=[], execution_roots=[])
        script = r"""
const { readFileSync } = require("node:fs");
const ts = require(process.argv[1]);
const { webcrypto } = require("node:crypto");
globalThis.crypto = webcrypto;
function load(path, dependencies = {}) {
  const source = ts.transpileModule(readFileSync(path, "utf8"), { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const exports = {};
  new Function("require", "exports", source)(name => dependencies[name] ?? require(name), exports);
  return exports;
}
const domain = load(process.argv[2]);
const { wireFrontendDisplay } = load(process.argv[3], { "./workflowGraph": domain });
const { document, catalog, packageLock } = JSON.parse(readFileSync(0, "utf8"));
wireFrontendDisplay(document, catalog, packageLock, {
  sourceNodeId: document.nodes[0].node_binding_id, sourcePortId: "output", objectKey: "frontend", role: "user" });
wireFrontendDisplay(document, catalog, packageLock, {
  sourceNodeId: document.nodes[1].node_binding_id, sourcePortId: "output", objectKey: "frontend", role: "assistant" });
console.log(JSON.stringify(document));
"""
        result = subprocess.run(
            [node_binary, "-e", script, str(typescript), str(frontend / "src/domain/workflowGraph.ts"),
             str(frontend / "src/domain/frontendWiring.ts")],
            input=json.dumps({"document": document,
                              "catalog": service.node_types(protocol_version=2)["node_types"],
                              "packageLock": list(service.registry.package_lock)}, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        generated = json.loads(result.stdout)
        assert len([item for item in generated["nodes"]
                    if item["component_id"] == "frontend.presentation"]) == 1
        final = run(service, create(service, generated), inputs={"text": "工作台生成的图"})
        assert final["status"] == "succeeded", final["chains"]
        assert len(final["objects"]["frontend"]["value"]["entries"]) == 2
        consumer = service.get_consumer(final["workflow_session_id"])
        display = next(item for item in consumer["outputs"] if item["data_type"] == "FRONTEND_DISPLAY")
        entries = display["payload"]["entries"]
        assert [item["role"] for item in entries] == ["user", "assistant"]
        bodies = [service.read_public_artifact(
            final["workflow_session_id"], workflow_definition_id=generated["workflow_definition_id"],
            definition_revision=1, node_id=display["node_binding_id"], port_id="display",
            run_id=display["run_id"], reference=entry["source_ref"],
        )["value"]["text"] for entry in entries]
        assert bodies == ["工作台生成的图", "答：工作台生成的图"]
