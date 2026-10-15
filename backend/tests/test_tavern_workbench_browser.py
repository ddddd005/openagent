"""Current resource panels and ordinary workflows on isolated temporary state."""

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
from phase1_agent.content_contracts import default_presentation
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.prompt_package import PROMPT_RESOURCE_TYPE
from test_tavern_global_lorebook import record as lorebook_record, identity as lorebook_identity


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
def test_real_workbench_resources_and_ordinary_creation(tmp_path, width):
    node, packages = browser_runtime()
    port = int(os.environ.get("TAVERN_WORKBENCH_PORT", "0"))
    assert port == 0 or 1024 <= port <= 65535 and port != 8765, "Do not use the product API port"
    workspace = Path(__file__).resolve().parents[2]
    dist = Path(os.environ.get("TAVERN_WORKBENCH_DIST", str(workspace / "frontend" / "dist")))
    assert (dist / "index.html").is_file(), "Build the current frontend into an isolated directory"
    assert '"/workbench/assets/' in (dist / "index.html").read_text(encoding="utf-8")
    model_calls = []

    def forbid_model(*args, **kwargs):
        model_calls.append(True)
        raise AssertionError("Resource checks must not initialize model services")

    evidence = tmp_path / f"workbench-{width}"
    evidence.mkdir()
    with closing(GraphWorkflowService(evidence / "workbench.sqlite", public_model_factory=forbid_model)) as service:
        book = lorebook_record(text="WORKBENCH_SEEDED_BOOK", resource_id=str(uuid4()))
        prompt = {
            **ResourceIdentity("workspace", PROMPT_RESOURCE_TYPE, str(uuid4())).to_dict(),
            "data_schema_version": 2, "update_sequence": 1,
            "value": {"enabled": True, "members": [{
                "id": str(uuid4()), "text": "WORKBENCH_SEEDED_PROMPT", "metadata": {},
                "presentation": default_presentation(), "lifecycle": "per_request", "compaction": "never",
            }]},
        }
        app = GraphApplication(service)
        for saved in (book, prompt):
            app.command("resource.save", {
                "record": saved, "expected_sequence": 0, "idempotency_key": str(uuid4()),
            })

        def configured_node(component, version, title, config):
            definition = service.registry.get(component, version).definition
            return {"node_binding_id": str(uuid4()), "component_id": component,
                    "component_version": version, "title": title, "position": {"x": 0, "y": 0},
                    "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
                    "config": {**deepcopy(definition.default_config), **config}}

        references = [
            configured_node("lorebook.global-reference", "1", f"Shared Lorebook {index + 1}",
                            {"reference": lorebook_identity(book)})
            for index in range(2)
        ]
        prompt_reference = configured_node("prompts.global-reference", "2", "Explicit Prompt Group", {"reference": {
            field: prompt[field] for field in ("envelope_version", "scope", "type_id", "resource_id")}})
        doc = {"schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
               "name": "Ordinary resource references", "nodes": [*references, prompt_reference], "edges": [],
               "object_bindings": [], "execution_roots": [], "control_edges": [],
               "package_lock": list(service.registry.package_lock)}
        for index, row in enumerate(doc["nodes"]):
            row["position"] = {"x": index * 440, "y": 0}
        saved = service.update_definition(doc, workflow_definition_id=doc["workflow_definition_id"],
                                          expected_edit_sequence=0, idempotency_key=str(uuid4()))
        assert saved["outcome"] == "saved"
        doc = saved["document"]
        workflow_id = doc["workflow_definition_id"]
        seed = {"workflow_id": workflow_id, "workflow_name": doc["name"],
                "lorebook_nodes": [row["node_binding_id"] for row in references],
                "prompt_node": prompt_reference["node_binding_id"], "original_prompt_id": prompt["resource_id"],
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
        assert model_calls == []
        assert service.list_sessions(workflow_id) == []
        assert service.list_sessions(observed["new_workflow"]) == []
        ordinary = service.get_definition(observed["new_workflow"])
        assert ordinary["nodes"] == [] and ordinary["edges"] == [] and ordinary["object_bindings"] == []
        saved_books = service.list_global_resources(type_id="workflow.tavern.lorebook")
        actual_book = next(row for row in saved_books if row["resource_id"] == observed["book_id"])
        assert actual_book["update_sequence"] == 2
        assert actual_book["value"]["entries"][0]["id"] == observed["book_entry_id"]
        assert actual_book["value"]["entries"][0]["text"] == "WORKBENCH_BOOK_EDITED"
        groups = service.list_global_resources(type_id="workflow.prompt-resource")
        original = next(row for row in groups if row["resource_id"] == observed["prompt_original"])
        copied = next(row for row in groups if row["resource_id"] == observed["prompt_copy"])
        assert [row["text"] for row in original["value"]["members"]] == [
            "WORKBENCH_GROUP_FIRST", "WORKBENCH_GROUP_SECOND"]
        assert [row["text"] for row in copied["value"]["members"]] == [
            "WORKBENCH_GROUP_SECOND", "WORKBENCH_GROUP_FIRST"]
        assert {row["id"] for row in original["value"]["members"]}.isdisjoint(
            row["id"] for row in copied["value"]["members"])
        explicit = service.get_definition(observed["reference_workflow"])
        assert [next(row for row in explicit["nodes"] if row["node_binding_id"] == target)["config"]["reference"]["resource_id"]
                for target in seed["lorebook_nodes"]] == [observed["book_id"]] * 2
        assert next(row for row in explicit["nodes"] if row["node_binding_id"] == seed["prompt_node"])[
            "config"]["reference"]["resource_id"] == observed["prompt_copy"]
