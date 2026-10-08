"""Current graph execution uses public resources and a local model substitute."""

import os
from pathlib import Path
import subprocess
import sys


_BLOCKED = (
    "phase1_agent.graph_agent_host",
    "phase1_agent.graph_agent_runtime",
    "phase1_agent.graph_agent_nodes",
    "phase1_agent.graph_agent_contracts",
    "phase1_agent.graph_nodes",
    "phase1_agent.graph_prompt",
    "phase1_agent.graph_prompt_nodes",
    "phase1_agent.graph_archives",
    "phase1_agent.graph_archive_http",
    "phase1_agent.workbench_resources",
    "phase1_agent.workflow",
    "phase1_agent.prepared_context",
    "phase1_agent.prompt_preparation",
    "phase1_agent.variable_preparation",
    "phase1_agent.kernel",
)


def _run_current_scenario(database):
    from contextlib import closing
    from copy import deepcopy
    import json
    from uuid import uuid4

    from phase1_agent.content_contracts import default_presentation
    from phase1_agent.contracts import ModelResponse, ModelToolCall
    from phase1_agent.graph_service import GraphWorkflowService
    from phase1_agent.host_sdk import ObjectBinding, ResourceIdentity
    from phase1_agent.model_host_service import MODEL_SERVICE_REF
    from phase1_agent.prompt_package import PROMPT_RESOURCE_TYPE

    calls = []

    def factory(*, provider, parameters, api_key):
        assert api_key == "offline-isolation-credential"

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                calls.append(deepcopy(messages))
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    f"public.final.{len(calls)}", "final_answer",
                    json.dumps({"answer": {"text": f"answer-{len(calls)}"}}),
                ),))

            def close(self):
                pass

        return Transport()

    with closing(GraphWorkflowService(database, public_model_factory=factory)) as service:
        assert service._service_options[MODEL_SERVICE_REF]["transport_factory"] is factory
        assert not any(row["package_id"] == "workflow.compat" for row in service.registry.package_lock)
        nodes, edges = [], []

        def node(component, version="1", **config):
            entry = service.registry.get(component, version)
            value = {
                "node_binding_id": str(uuid4()), "component_id": component,
                "component_version": version, "title": component,
                "position": {"x": len(nodes) * 100, "y": 0},
                "config": {**deepcopy(entry.definition.default_config), **config},
            }
            nodes.append(value)
            return value

        def connect(source, target, *, source_port="output", target_port="input"):
            edges.append({
                "edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
                "source_port_id": source_port, "target_node_id": target["node_binding_id"],
                "target_port_id": target_port, "order": 0,
            })

        model = node("models.source", "2", capacity={
            "context_window_tokens": 100000, "output_reserve_tokens": 1024,
            "summary_max_tokens": 128, "max_cold_input_tokens": 100000})
        provider = {
            **model["config"]["reference"], "data_schema_version": 1, "update_sequence": 1,
            "value": {
                "name": "Local substitute", "protocol": "chat", "base_url": "https://isolation.invalid",
                "credential_ref": "env:DEEPSEEK_API_KEY", "enabled": True,
            },
        }
        service.save_global_resource(provider, expected_sequence=0, idempotency_key=str(uuid4()))
        reference = ResourceIdentity("workspace", PROMPT_RESOURCE_TYPE, str(uuid4())).to_dict()
        service.save_global_resource({
            **reference, "data_schema_version": 2, "update_sequence": 1,
            "value": {"enabled": True, "members": [{
                "id": str(uuid4()), "text": "Retained current prompt",
                "presentation": default_presentation(), "metadata": {},
                "lifecycle": "per_request", "compaction": "never",
            }]},
        }, expected_sequence=0, idempotency_key=str(uuid4()))
        prompt = node("prompts.global-reference", "2", reference=reference)
        resolved = node("prompts.global-resolve", "2")
        current = node("tools.current-input")
        execute = node("agents.execute", "4")
        read = node("context.output", "4", object_key="context", agent_node_id=execute["node_binding_id"])
        assembly = node("context.assembly", "4")
        merge = node("context.merge", "4", object_key="context", agent_node_id=execute["node_binding_id"])
        output = node("tools.output")
        output["public_outputs"] = ["output"]
        connect(prompt, resolved)
        connect(resolved, assembly, target_port="materials")
        connect(current, assembly, target_port="current_input")
        connect(read, assembly, target_port="view")
        connect(model, execute, target_port="model")
        connect(assembly, execute, target_port="prompt")
        connect(read, merge, target_port="view")
        connect(execute, merge, source_port="context", target_port="context")
        connect(execute, output, source_port="result")
        document = {
            "schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": "Current runtime isolation", "nodes": nodes, "edges": edges,
            "package_lock": list(service.registry.package_lock),
            "execution_roots": [merge["node_binding_id"]],
            "control_edges": [{
                "edge_id": str(uuid4()), "source_node_id": merge["node_binding_id"],
                "target_node_id": output["node_binding_id"],
            }],
            "object_bindings": [ObjectBinding(
                "context", "workflow.effective-context", 4, "shared",
                readers=(read["node_binding_id"], merge["node_binding_id"]),
                writers=(merge["node_binding_id"],),
            ).to_dict()],
        }
        saved = service.save_definition(document, expected_revision=0, idempotency_key=str(uuid4()))
        initial = service.create_session(saved["workflow_definition_id"], 1, idempotency_key=str(uuid4()))

        def run(view, question):
            started = service.start(
                view["workflow_session_id"], expected_revision=view["revision"],
                idempotency_key=str(uuid4()), inputs={"text": question},
            )
            service.wait(started["active_chain_run_id"])
            final = service.get_session(view["workflow_session_id"])
            assert final["status"] == "succeeded", final["chains"]
            assert not hasattr(service, "_native_runtime")
            assert not service._resource_frames and not service._service_runs
            return final

        first = run(initial, "first question")
        candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
        second = run(first, "second question")
        assert len(calls) == 2
        assert calls[1][0]["content"] == "Retained current prompt"
        assert any(message.get("content") == "first question" for message in calls[1])
        assert len(second["objects"]["context"]["value"]["accepted_delta_ids"]) == 2
        fork = service.fork_graph_candidate(
            second["workflow_session_id"], candidate_id=candidate["candidate_id"],
            expected_revision=second["revision"], expected_data_revision=second["data_revision"],
            expected_head_revision=second["head_revision"], idempotency_key=str(uuid4()),
        )
        branch = run(fork, "branch question")
        assert len(calls) == 3
        assert not any(message.get("content") == "second question" for message in calls[2])
        assert len(branch["objects"]["context"]["value"]["accepted_delta_ids"]) == 2
        sid, chain_id = second["workflow_session_id"], second["selected_chain_run_id"]
        history = service.get_run(sid, chain_id)
        assert "offline-isolation-credential" not in json.dumps(history)
        assert service.get_session(sid)["objects"] == second["objects"]
    with closing(GraphWorkflowService(database, public_model_factory=factory)) as service:
        assert service.get_run(sid, chain_id) == history
        assert service.get_session(sid)["objects"] == second["objects"]
        assert not hasattr(service, "_native_runtime")
        assert len(calls) == 3
    assert not any(name == blocked or name.startswith(blocked + ".")
                   for name in sys.modules for blocked in _BLOCKED)


def test_current_agent_multiround_fork_and_reopen_without_legacy_execution_imports(tmp_path):
    program = """
import importlib.abc
import sys
from test_current_runtime_isolation import _BLOCKED, _run_current_scenario

class NoLegacyExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == blocked or fullname.startswith(blocked + ".") for blocked in _BLOCKED):
            raise AssertionError("Current graph imported legacy execution: " + fullname)

sys.meta_path.insert(0, NoLegacyExecution())
_run_current_scenario(sys.argv[1])
print("current Agent multiround, fork and history reopen isolated")
"""
    root = Path(__file__).resolve().parents[1]
    environment = dict(
        os.environ, PYTHONDONTWRITEBYTECODE="1", DEEPSEEK_API_KEY="offline-isolation-credential",
    )
    environment["PYTHONPATH"] = os.pathsep.join([
        str(root / "src"), str(root / "tests"), environment.get("PYTHONPATH", ""),
    ])
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "isolated-current.sqlite")],
        text=True, capture_output=True, env=environment, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "current Agent multiround, fork and history reopen isolated"
