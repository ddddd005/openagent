"""Actual shipped consumer package, platform API and stored graph in one host."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.builtin_packages import builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackageLoader
from test_graph_server import host_server, request
from test_plan13a_client_http import display_document


def test_actual_workbench_manifest_matches_registered_package():
    node_binary = shutil.which("node")
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    typescript = frontend / "node_modules" / "typescript"
    if node_binary is None or not typescript.is_dir():
        pytest.skip("Node.js and the installed TypeScript dependency are required")
    loaded = CapabilityPackageLoader(builtin_capability_packages()).load({"workflow.frontend": "1.0.0"})
    script = r"""
const ts = require(process.argv[1]), fs = require("node:fs"), assert = require("node:assert/strict");
const exports = {};
const source = ts.transpileModule(fs.readFileSync(process.argv[2], "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
new Function("require", "exports", source)(require, exports);
const sort = items => items.sort((a, b) => a.extension_id.localeCompare(b.extension_id));
assert.deepEqual(sort(exports.workflowFrontendExtensions), sort(JSON.parse(process.argv[3])));
"""
    result = subprocess.run(
        [node_binary, "-e", script, str(typescript),
         str(frontend / "src" / "plugins" / "workflowFrontendManifest.ts"),
         json.dumps(list(loaded.frontend_extensions))],
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_consumer_package_mount_disable_headless_run_and_reenable(tmp_path, monkeypatch):
    node_binary = shutil.which("node")
    if node_binary is None:
        pytest.skip("Node.js is required for the shipped consumer package integration")
    with host_server(tmp_path, monkeypatch) as (host, port):
        document = display_document(host.graph_service)
        status, saved = request(port, "POST", "/api/graph/commands", {
            "operation": "definition.save", "parameters": {
                "document": document, "expected_revision": 0, "idempotency_key": "b13-save",
            },
        })
        assert status == 201, saved
        script = r"""
const { runInNewContext } = require("node:vm");
const { randomUUID } = require("node:crypto");
const assert = require("node:assert/strict");
const base = process.argv[1], workflowId = process.argv[2], enabled = JSON.parse(process.argv[3]);
const privateNode = process.argv[4], scope = {}, calls = [], data = new Map();
class Element {
  constructor() { this.children = []; this.ownerDocument = { createElement: () => new Element() }; this.textContent = ""; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
}
async function transport(path, options = {}) {
  calls.push({ path, operation: options.body ? JSON.parse(options.body).operation : null });
  const response = await fetch(base + path, { ...options, headers: {
    "Content-Type": "application/json", Origin: base }, cache: "no-store" });
  const value = await response.json();
  if (!response.ok) throw scope.GraphChat.httpFailure(value, response.status);
  return value;
}
async function configure(selection) {
  const response = await fetch(base + "/api/graph/commands", { method: "POST",
    headers: { "Content-Type": "application/json", Origin: base }, body: JSON.stringify({
      operation: "packages.configure", parameters: { enabled_packages: selection } }) });
  const value = await response.json();
  assert.ok(response.ok, JSON.stringify(value));
}
async function finish(client) {
  const deadline = Date.now() + 10000;
  while (client.consumer.status !== "succeeded") {
    assert.ok(Date.now() < deadline && !["failed", "closed", "archive_failed"].includes(client.consumer.status),
      JSON.stringify(client.consumer));
    await new Promise(resolve => setTimeout(resolve, 20)); await client.refresh();
  }
  return client.consumer.outputs.find(row => row.data_type === "FRONTEND_DISPLAY");
}
async function mounted(host, output) {
  const container = new Element(), handle = host.attach(output, container);
  assert.equal(await handle.ready, "mounted");
  return { container, handle };
}
async function body(container, index) {
  const row = container.children[index + 1];
  await row.children[2].onclick();
  return row.children[3].textContent;
}
(async () => {
  for (const name of ["graph-chat-core.js", "frontend-package-host.js", "frontend-package.js"]) {
    const response = await fetch(base + "/static/" + name);
    assert.equal(response.status, 200); assert.ok(response.headers.get("content-type").includes("javascript"));
    runInNewContext(await response.text(), scope);
  }
  const options = { workflowId, request: transport, keyFactory: randomUUID, storage: {
    getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) } };
  const client = new scope.GraphChat.GraphChatClient(options);
  await client.discover(); const definition = await client.readDefinition();
  assert.ok(client.application.queries.some(row => row.name === "frontend.extensions.read"));
  await client.command("create", { definition_revision: definition.definition_revision });
  await client.command("start", { inputs: { text: "first" } });
  const first = await finish(client), packet = await client.readFrontendExtensions(first);
  assert.equal(packet.frontend_extensions.length, 1);
  assert.equal(packet.frontend_extensions[0].extension_id, "workflow.frontend.public-output");
  assert.deepEqual(JSON.parse(JSON.stringify(packet.frontend_extensions[0])), JSON.parse(JSON.stringify(scope.WorkflowFrontendPackage.declaration)),
    "shipped JS package and actual registered Python contract differ");
  const host = new scope.WorkflowFrontendHost.ConsumerFrontendHost(client);
  scope.WorkflowFrontendPackage.install(host);
  const firstMount = await mounted(host, first);
  assert.equal(await body(firstMount.container, 0), "first");
  assert.equal(await body(firstMount.container, 1), "答：first");
  await assert.rejects(client.query("frontend.extensions.read", {
    session_id: client.sessionId, workflow_definition_id: workflowId, definition_revision: 1,
    node_id: privateNode, port_id: "view" }), /not publicly declared/);
  const withoutUI = { ...enabled }; delete withoutUI["workflow.frontend"];
  await configure(withoutUI); await client.refresh();
  const disabledOutput = client.consumer.outputs.find(row => row.data_type === "FRONTEND_DISPLAY");
  const disabled = host.attach(disabledOutput, new Element());
  assert.equal(await disabled.ready, "unregistered"); assert.equal(firstMount.container.children.length, 0);
  const existing = await client.readDisplayEntry(disabledOutput, disabledOutput.payload.entries[0]);
  assert.equal(existing.value.text, "first");
  await client.command("start", { inputs: { text: "second" } });
  const second = await finish(client); assert.equal(second.payload.entries.length, 4);
  assert.equal(JSON.stringify(second.payload.entries.slice(0, 2)), JSON.stringify(first.payload.entries));
  const recovered = new scope.GraphChat.GraphChatClient(options);
  await recovered.discover(); await recovered.refresh();
  assert.equal(recovered.sessionId, client.sessionId);
  await configure(enabled); await recovered.refresh();
  const recoveredHost = new scope.WorkflowFrontendHost.ConsumerFrontendHost(recovered);
  scope.WorkflowFrontendPackage.install(recoveredHost);
  const reopenedOutput = recovered.consumer.outputs.find(row => row.data_type === "FRONTEND_DISPLAY");
  const reopened = await mounted(recoveredHost, reopenedOutput);
  assert.equal(await body(reopened.container, 3), "答：second");
  const history = await recovered.readHistory(reopenedOutput);
  const original = history.outputs.find(row => row.run_id === first.run_id);
  const originalMount = await mounted(recoveredHost, original);
  assert.equal(await body(originalMount.container, 1), "答：first");
  assert.ok(calls.every(call => ["/api/graph/consumer/application", "/api/graph/consumer/queries",
    "/api/graph/consumer/commands"].includes(call.path)), "renderer escalated to a management API");
  host.dispose(); recoveredHost.dispose();
  assert.ok([...data.values()].every(value => !value.includes("source_ref") && !value.includes("messages")));
  console.log(JSON.stringify({ sessionId: client.sessionId }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node_binary, "-e", script, f"http://127.0.0.1:{port}",
             document["workflow_definition_id"], json.dumps(DEFAULT_PACKAGES),
             document["nodes"][2]["node_binding_id"]],
            capture_output=True, text=True, encoding="utf-8", timeout=45, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads(result.stdout)
        session = host.graph_service.get_session(evidence["sessionId"])
        assert session["status"] == "succeeded"
        assert len(session["objects"]["frontend"]["value"]["entries"]) == 4
        assert len(host.graph_service.list_graph_candidates(evidence["sessionId"])["candidates"]) == 2
        assert host._legacy is None and host.graph_service._native_runtime is None
