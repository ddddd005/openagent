"""Real workbench resource panels and exact tavern handoff on temporary state."""

from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from test_agent_integration import run
from test_context_native_integration import NativeTransport, native_graph, versioned_node
from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from test_tavern_chat_integration import add_chat
from test_tavern_global_lorebook import record as lorebook_record, identity as lorebook_identity
from test_tavern_prompt_presets import record as prompt_record, member as prompt_member


def browser_runtime():
    bundle = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node"
    node = os.environ.get("TAVERN_BROWSER_NODE") or str(bundle / "bin" / "node.exe")
    if not Path(node).is_file():
        node = shutil.which("node")
    packages = Path(os.environ.get("TAVERN_BROWSER_NODE_MODULES", str(bundle / "node_modules")))
    if not node or not (packages / "playwright").is_dir():
        pytest.skip("Real workbench browser acceptance requires an installed Playwright runtime")
    executable = os.environ.get("TAVERN_CHROMIUM_EXECUTABLE")
    if executable is None:
        probe = subprocess.run(
            [node, "-e", "console.log(require('playwright').chromium.executablePath())"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            env={**os.environ, "NODE_PATH": str(packages)}, check=False,
        )
        executable = probe.stdout.strip() if probe.returncode == 0 else ""
    if not Path(executable).is_file():
        pytest.skip("An installed Chromium or TAVERN_CHROMIUM_EXECUTABLE is required")
    return node, packages


@pytest.mark.parametrize("width", [1440, 1024], ids=["desktop", "narrow-desktop"])
def test_real_workbench_resource_management_and_tavern_launch(tmp_path, monkeypatch, width):
    node, packages = browser_runtime()
    configured_port = os.environ.get("TAVERN_WORKBENCH_PORT")
    if not configured_port:
        pytest.skip("Set TAVERN_WORKBENCH_PORT to the isolated build-time VITE_CHAT_UI_URL port")
    port = int(configured_port)
    assert 1024 <= port <= 65535 and port != 8765, "Do not use the existing product API port"
    workspace = Path(__file__).resolve().parents[2]
    dist = workspace / "frontend" / "dist"
    assert (dist / "index.html").is_file(), "Build the current frontend before browser acceptance"
    assert '"/workbench/assets/' in (dist / "index.html").read_text(encoding="utf-8"), (
        "Build with npm run build -- --base=/workbench/ before same-origin browser acceptance")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-tavern-workbench")
    transport = NativeTransport()
    evidence = tmp_path / f"workbench-{width}"
    evidence.mkdir()
    database = evidence / "workbench.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        book = lorebook_record(text="WORKBENCH_SEEDED_BOOK", resource_id=str(uuid4()))
        prompt = prompt_record(prompt_member(9800, "WORKBENCH_SEEDED_PROMPT"))
        app = GraphApplication(service)
        for saved in (book, prompt):
            app.command("resource.save", {
                "record": saved, "expected_sequence": 0, "idempotency_key": str(uuid4()),
            })
        doc, native = native_graph(service, policy=False, once=False)
        current = next(row for row in doc["nodes"] if row["component_id"] == "tools.current-input")
        add_chat(service, doc, current, native["a"]["agent"], after=native["a"]["merge"])
        references = []
        for index in range(2):
            reference = versioned_node(service, "lorebook.global-reference", "1", 7900 + index)
            reference["title"] = f"Shared Lorebook {index + 1}"
            reference["config"] = {"reference": lorebook_identity(book)}
            references.append(reference)
        prompt_reference = versioned_node(service, "prompts.global-reference", "2", 7902)
        prompt_reference["title"] = "Explicit Prompt Group"
        prompt_reference["config"] = {"reference": {
            field: prompt[field] for field in ("envelope_version", "scope", "type_id", "resource_id")}}
        doc["nodes"] += [*references, prompt_reference]
        for index, row in enumerate(doc["nodes"]):
            row["position"] = {"x": (index % 3) * 440, "y": (index // 3) * 350}
        first = run(service, create(service, doc), "Browser acceptance original input.")
        assert first["status"] == "succeeded", first["chains"]
        before = deepcopy(service.get_session(first["workflow_session_id"]))
        assert len(transport.calls) == 1
        workflow_id = doc["workflow_definition_id"]
        cache = {
            "schemaVersion": 7, "kind": "graph-workbench", "revision": 1,
            "savedAt": "2026-10-08T00:00:00.000Z", "activeWorkflowId": workflow_id,
            "selectedWorkflowId": workflow_id,
            "catalog": [{"id": workflow_id, "title": doc["name"], "description": "Browser acceptance",
                         "nodeCount": len(doc["nodes"]), "state": "saved"}],
            "graph": {"schema_version": 1, "entries": {workflow_id: {
                "document": doc, "saved_document": doc, "saved_revision": 1,
                "session_id": first["workflow_session_id"], "pending": None,
            }}},
        }
        seed = {"cache": cache, "lorebook_nodes": [row["node_binding_id"] for row in references],
                "prompt_node": prompt_reference["node_binding_id"], "original_session": first["workflow_session_id"],
                "width": width, "evidence": str(evidence)}
        seed_path = evidence / "seed.json"
        seed_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
        environment = {**os.environ, "NODE_PATH": str(packages)}
        from phase1_agent.server import create_server
        from threading import Thread
        server = create_server(service, port=port, graph_service=service, workbench_dist=dist)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = subprocess.run([
                node, str(Path(__file__).with_name("tavern_workbench_browser.cjs")),
                f"http://127.0.0.1:{server.server_address[1]}", str(seed_path),
            ], capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=environment, timeout=160, check=False)
            (evidence / "runner.stdout.txt").write_text(result.stdout, encoding="utf-8")
            (evidence / "runner.stderr.txt").write_text(result.stderr, encoding="utf-8")
            assert result.returncode == 0, result.stderr + result.stdout
        finally:
            server.shutdown()
            worker.join(5)
            server.server_close()
            assert not worker.is_alive()
        observed = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
        assert observed["page_errors"] == [] and observed["asset_errors"] == []
        assert observed["console_errors"] == []
        assert observed["overflow"] == [] and observed["overlaps"] == []
        assert observed["native_calls"] is False
        assert service.get_session(first["workflow_session_id"]) == before
        assert len(transport.calls) == 1
        saved_books = service.list_global_resources(type_id="workflow.tavern.lorebook")
        actual_book = next(row for row in saved_books if row["resource_id"] == observed["book_id"])
        assert actual_book["update_sequence"] == 2
        assert actual_book["value"]["entries"][0]["id"] == observed["book_entry_id"]
        assert actual_book["value"]["entries"][0]["text"] == "WORKBENCH_BOOK_EDITED"
        groups = service.list_global_resources(type_id="workflow.prompt-resource")
        original = next(row for row in groups if row["resource_id"] == observed["prompt_original"])
        copied = next(row for row in groups if row["resource_id"] == observed["prompt_copy"])
        assert original["value"]["members"][0]["text"] == "WORKBENCH_GROUP_FIRST"
        assert [row["text"] for row in copied["value"]["members"]] == [
            "WORKBENCH_GROUP_SECOND", "WORKBENCH_GROUP_FIRST"]
        assert {row["id"] for row in original["value"]["members"]}.isdisjoint(
            row["id"] for row in copied["value"]["members"])
        explicit = service.get_definition(observed["reference_workflow"])
        assert [next(row for row in explicit["nodes"] if row["node_binding_id"] == target)["config"]["reference"]["resource_id"]
                for target in seed["lorebook_nodes"]] == [observed["book_id"]] * 2
        assert next(row for row in explicit["nodes"] if row["node_binding_id"] == seed["prompt_node"])[
            "config"]["reference"]["resource_id"] == observed["prompt_copy"]
