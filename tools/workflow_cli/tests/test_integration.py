import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from test_graph_server import host_server


CLI = Path(__file__).resolve().parents[1] / "oa.py"


def invoke(port, *args, expected=0):
    result = subprocess.run(
        [sys.executable, str(CLI), "--base-url", f"http://127.0.0.1:{port}", *map(str, args)],
        text=True, capture_output=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == expected, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_cli_real_backend_batch_round_trip(tmp_path):
    workflow_id, temp_id, edge_id, kept_edge_id = (str(uuid4()) for _ in range(4))
    binding = {"object_key": "independent", "type_id": "workflow.chat-context",
               "schema_version": 1, "scope": "shared", "owner_node_id": None,
               "registrar_node_id": None, "readers": ["@text"], "writers": []}
    create = {"schema_version": 1, "workflow": {"name": "CLI integration", "id": workflow_id},
              "operations": [
                  {"operation": "node.add", "as": "text", "component_id": "tools.text",
                   "component_version": "1", "config": {"text": "first"}},
                  {"operation": "node.configure", "node_binding_id": "@text", "changes": {"text": "updated"}},
                  {"operation": "node.add", "as": "out", "component_id": "tools.output",
                   "component_version": "1"},
                  {"operation": "node.add", "node_binding_id": temp_id, "component_id": "tools.text",
                   "component_version": "1"},
                  {"operation": "edge.add", "edge_id": kept_edge_id, "source_node_id": "@text",
                   "source_port_id": "output", "target_node_id": "@out",
                   "target_port_id": "input", "order": 0},
                  {"operation": "edge.add", "edge_id": edge_id, "source_node_id": temp_id,
                   "source_port_id": "output", "target_node_id": "@out",
                   "target_port_id": "input", "order": 1},
                  {"operation": "binding.put", "binding": binding},
              ]}
    with host_server(tmp_path) as (_, port):
        path = write(tmp_path / "create.json", create)
        request = tmp_path / "create-request.json"
        prepared = invoke(port, "workflow", "edit", "--batch", path, "--request-out", request)
        assert prepared["status"] == "prepared"
        assert prepared["preview"]["read_only"] is True
        saved = invoke(port, "workflow", "apply", "--request", request)
        assert saved["status"] == "saved"
        before = invoke(port, "workflow", "read", workflow_id, "--full")
        assert len(before["nodes"]) == 3 and len(before["edges"]) == 2
        assert before["object_bindings"][0]["object_key"] == "independent"
        assert before["object_bindings"][0]["readers"] == [prepared["aliases"]["text"]]
        kept = next(e for e in before["edges"] if e["edge_id"] == kept_edge_id)
        assert (kept["source_node_id"], kept["source_port_id"], kept["target_node_id"],
                kept["target_port_id"]) == (prepared["aliases"]["text"], "output",
                                             prepared["aliases"]["out"], "input")
        assert next(n for n in before["nodes"] if n["node_binding_id"] == prepared["aliases"]["text"])["config"]["text"] == "updated"
        receipt = invoke(port, "receipt", "--request", request)
        assert receipt["outcome"] == "matched"
        delete = {"schema_version": 1, "workflow": {"id": workflow_id, "revision": before["revision"],
                  "edit_sequence": before["edit_sequence"]}, "operations": [
                      {"operation": "edge.remove", "edge_id": edge_id},
                      {"operation": "node.remove", "node_binding_id": temp_id}]}
        path = write(tmp_path / "delete.json", delete)
        result = invoke(port, "workflow", "edit", "--batch", path, "--request-out",
                        tmp_path / "delete-request.json", "--apply")
        assert result["apply"]["status"] == "saved"
        after = invoke(port, "workflow", "read", workflow_id, "--full")
        assert after["nodes"] == [n for n in before["nodes"] if n["node_binding_id"] != temp_id]
        assert after["edges"] == [kept]
        assert after["object_bindings"] == before["object_bindings"]


def test_two_agent_branches_preserve_context_and_ports(tmp_path):
    workflow_id = str(uuid4())
    with host_server(tmp_path) as (_, port):
        empty = {"schema_version": 1, "workflow": {"id": workflow_id, "name": "Two branches"},
                 "operations": []}
        empty_file = write(tmp_path / "empty.json", empty)
        empty_result = invoke(port, "workflow", "edit", "--batch", empty_file, "--request-out",
                              tmp_path / "empty-request.json", "--apply")
        assert empty_result["apply"]["status"] == "saved"
        base = invoke(port, "workflow", "read", workflow_id, "--full")
        assert base["nodes"] == [] and base["edges"] == []
        operations = []
        for branch in ("a", "b"):
            context_key, chat_key = "context-" + branch, "chat-" + branch
            for name, component, version, config in [
                ("input", "tools.editable-input", "1", {}),
                ("frontend", "frontend.node", "2",
                 {"input_node_id": f"@{branch}-input", "object_key": chat_key, "role": "assistant"}),
                ("agent", "agents.execute", "10", {}),
                ("output", "context.output", "5", {"object_key": context_key}),
                ("assembly", "context.assembly", "5", {}),
                ("merge", "context.merge", "6", {"object_key": context_key}),
            ]:
                operations.append({"operation": "node.add", "as": f"{branch}-{name}",
                                   "component_id": component, "component_version": version, "config": config})
            for source, source_port, target, target_port in [
                ("input", "output", "assembly", "current_input"),
                ("output", "output", "assembly", "view"),
                ("assembly", "output", "agent", "prompt"),
                ("agent", "receipts", "merge", "receipts"),
                ("output", "output", "merge", "view"),
                ("agent", "result", "frontend", "content"),
            ]:
                operations.append({"operation": "edge.add", "source_node_id": f"@{branch}-{source}",
                                   "source_port_id": source_port, "target_node_id": f"@{branch}-{target}",
                                   "target_port_id": target_port, "order": 0})
            for binding in [
                {"object_key": chat_key, "type_id": "workflow.chat-context", "schema_version": 1,
                 "scope": "shared", "owner_node_id": None, "registrar_node_id": f"@{branch}-frontend",
                 "readers": [f"@{branch}-frontend"], "writers": [f"@{branch}-frontend"]},
                {"object_key": context_key, "type_id": "workflow.effective-context", "schema_version": 4,
                 "scope": "shared", "owner_node_id": None, "registrar_node_id": f"@{branch}-agent",
                 "readers": [f"@{branch}-output", f"@{branch}-merge"], "writers": [f"@{branch}-merge"]},
            ]:
                operations.append({"operation": "binding.put", "binding": binding})
        batch = {"schema_version": 1, "workflow": {"id": workflow_id, "revision": base["revision"],
                 "edit_sequence": base["edit_sequence"]}, "operations": operations}
        result = invoke(port, "workflow", "edit", "--batch", write(tmp_path / "branches.json", batch),
                        "--request-out", tmp_path / "branches-request.json", "--apply")
        assert result["apply"]["status"] == "saved"
        doc = invoke(port, "workflow", "read", workflow_id, "--full")
        aliases = result["aliases"]
        for branch in ("a", "b"):
            edges = {(e["source_node_id"], e["source_port_id"], e["target_node_id"], e["target_port_id"])
                     for e in doc["edges"]}
            assert (aliases[f"{branch}-agent"], "receipts", aliases[f"{branch}-merge"], "receipts") in edges
            assert (aliases[f"{branch}-agent"], "result", aliases[f"{branch}-frontend"], "content") in edges
            context = next(b for b in doc["object_bindings"] if b["object_key"] == "context-" + branch)
            assert context["registrar_node_id"] == aliases[f"{branch}-agent"]
            assert context["readers"] == [aliases[f"{branch}-output"], aliases[f"{branch}-merge"]]
            assert context["writers"] == [aliases[f"{branch}-merge"]]
        assert invoke(port, "receipt", "--request", tmp_path / "branches-request.json")["outcome"] == "matched"


def test_preview_does_not_reserve_cas(tmp_path):
    workflow_id = str(uuid4())
    with host_server(tmp_path) as (_, port):
        empty = {"schema_version": 1, "workflow": {"id": workflow_id, "name": "CAS"},
                 "operations": []}
        invoke(port, "workflow", "edit", "--batch", write(tmp_path / "empty.json", empty),
               "--request-out", tmp_path / "empty-request.json", "--apply")
        base = invoke(port, "workflow", "read", workflow_id, "--full")
        for name in ("first", "second"):
            batch = {"schema_version": 1, "workflow": {"id": workflow_id, "revision": base["revision"],
                     "edit_sequence": base["edit_sequence"]}, "operations": [
                         {"operation": "node.add", "component_id": "tools.text",
                          "component_version": "1", "config": {"text": name}}]}
            prepared = invoke(port, "workflow", "edit", "--batch", write(tmp_path / f"{name}.json", batch),
                              "--request-out", tmp_path / f"{name}-request.json")
            assert prepared["preview"]["read_only"] is True
        assert invoke(port, "workflow", "apply", "--request", tmp_path / "first-request.json")["status"] == "saved"
        saved = invoke(port, "workflow", "read", workflow_id, "--full")
        rejected = invoke(port, "workflow", "apply", "--request", tmp_path / "second-request.json", expected=1)
        assert rejected["error"]["http_status"] == 409
        assert rejected["error"]["response"]["error"]["reason_code"] == "stale_edit_sequence"
        assert invoke(port, "workflow", "read", workflow_id, "--full") == saved
