"""Versioned logical-chat nodes use workflow-owned metadata and real stored snapshots."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace

import pytest

from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_contracts import FRONTEND_COMMIT_TYPE
from phase1_agent.frontend_node_contracts import FRONTEND_NODE_APPEND_TYPE
from phase1_agent.graph_contracts import GraphCompiler, NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.session_objects import SessionObjectStore
from frontend_node_test_support import create as _create, graph as _graph
from frontend_node_test_support import output as _output, owner as _owner, save as _save
from test_graph_service import document, edge, node, run
from test_success_checkpoints import checkpoint_history


def test_default_registry_exposes_only_current_logical_chat_contract(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "v2-declarations.sqlite")) as service:
        assert service.registry.get("frontend.node", "1") is None
        current = service.registry.get("frontend.node", "2").definition.to_dict()
        assert current["provided_session_item_types"] == [{"type_id": CHAT_CONTEXT_TYPE, "schema_version": 1}]
        assert current["outputs"][0]["data_schema_version"] == 2
        assert current["outputs"][1]["data_type"] == FRONTEND_COMMIT_TYPE
        assert service.registry.data_types.get(
            FRONTEND_NODE_APPEND_TYPE, 2, scope="content") is not None
        for retired in (
                ("frontend.state.output", "1"), ("frontend.state.append", "1"),
                ("frontend.presentation", "1")):
            assert service.registry.get(*retired) is None


@pytest.mark.parametrize("target", ["malformed", None, 4, "missing", "wrong_type", "wrong_version"])
def test_optional_input_reference_rejects_invalid_or_inexact_targets(tmp_path, target):
    with closing(GraphWorkflowService(tmp_path / "frontend-reference-invalid.sqlite")) as service:
        graph, messages, frontends = _graph(service)
        if target == "missing":
            value = "00000000-0000-4000-8000-000000009999"
        elif target in ("wrong_type", "wrong_version"):
            value = messages[0]["node_binding_id"]
            if target == "wrong_type":
                messages[0]["component_id"] = "tools.text"
                messages[0]["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
            else:
                original = service.registry.get("tools.editable-input", "1")
                alternate = replace(original.definition, component_version="2", adjustable_configuration=None)
                service.registry.register(alternate, original.executor)
                messages[0]["component_version"] = "2"
                messages[0]["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        else:
            value = target
        frontends[0]["config"]["input_node_id"] = value
        with pytest.raises(ContractValidationError) as caught:
            GraphCompiler(service.registry).compile(graph)
        expected = (
            "graph_missing_config_reference" if target == "missing"
            else "graph_config_reference_type_mismatch" if target in ("wrong_type", "wrong_version")
            else "graph_invalid_config"
        )
        assert caught.value.reason_code == expected


def test_optional_input_reference_allows_only_empty_or_exact_editable_input(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-reference-valid.sqlite")) as service:
        graph, messages, frontends = _graph(service)
        declaration = service.registry.get("frontend.node", "2").definition.config_node_references[0]
        assert declaration["allow_empty"] is True
        empty = GraphCompiler(service.registry).compile(graph)
        frontends[0]["config"]["input_node_id"] = messages[0]["node_binding_id"]
        bound = GraphCompiler(service.registry).compile(graph)
        assert empty.ordered_node_ids == bound.ordered_node_ids
        assert empty.target_node_ids == bound.target_node_ids


@pytest.mark.parametrize("allow_empty", [None, False])
def test_required_reference_declarations_still_reject_empty(tmp_path, allow_empty):
    with closing(GraphWorkflowService(tmp_path / "frontend-reference-required.sqlite")) as service:
        reference = {
            "config_field": "input_node_id", "multiple": False,
            "target_types": [{"component_id": "tools.editable-input", "component_version": "1"}],
        }
        if allow_empty is not None:
            reference["allow_empty"] = allow_empty
        service.registry.register(NodeDefinition(
            "test.frontend-required-reference", "1", "Required binding", "Test",
            {"input_node_id": ""},
            {"type": "object", "properties": {"input_node_id": {"type": "string"}},
             "required": ["input_node_id"], "additionalProperties": False},
            inputs=(NodePort("content", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),), is_output=True,
            config_node_references=(reference,),
        ), lambda config, inputs, context: {"output": inputs["content"]})
        source = node(service.registry, "tools.text", 19201, text="literal")
        target = node(service.registry, "test.frontend-required-reference", 19202)
        graph = document([source, target], [edge(source, target, 19201, target_port="content")])
        with pytest.raises(ContractValidationError):
            GraphCompiler(service.registry).compile(graph)


@pytest.mark.parametrize("allow_empty", [None, 1, "", "true"])
def test_optional_reference_declaration_requires_a_real_boolean(tmp_path, allow_empty):
    with closing(GraphWorkflowService(tmp_path / "frontend-reference-declaration.sqlite")) as service:
        current = service.registry.get("frontend.node", "2").definition
        reference = {**current.config_node_references[0], "allow_empty": allow_empty}
        candidate = replace(current, component_id="test.invalid-empty-reference",
                            config_node_references=(reference,))
        with pytest.raises(ContractValidationError) as caught:
            service.registry.register(candidate, lambda *args: {})
        assert caught.value.reason_code == "graph_invalid_type"


@pytest.mark.parametrize("bound", [False, True])
def test_graph_runs_without_frontend_and_saves_inline_chat_with_real_success_point(tmp_path, bound):
    with closing(GraphWorkflowService(tmp_path / "v2-run.sqlite")) as service:
        graph, messages, frontends = _graph(service, bound=bound)
        graph = _save(service, graph)
        initial = _create(service, graph)
        final = run(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        key = frontends[0]["config"]["object_key"]
        value = final["objects"][key]["value"]
        assert "source_ref" not in str(value) and "view_ref" not in str(value)
        assert len(value["rounds"]) == 1
        floor = value["rounds"][0]
        assert floor["submitted_input"] == (messages[0]["config"]["text"] if bound else None)
        assert floor["base_checkpoint_id"] is None
        candidate = floor["candidates"][0]
        assert candidate["messages"][0]["text"] == messages[0]["config"]["text"]
        assert candidate["result_checkpoint_id"] is not None
        assert floor["selected_candidate_id"] == candidate["candidate_id"]
        proof = _output(final, frontends[0], "proof")
        assert proof["schema_version"] == 2 and proof["round"]["round_id"] == floor["round_id"]
        assert proof["round"]["candidate_id"] == candidate["candidate_id"]
        assert _output(final, frontends[0], "commit")["receipt"]["object_key"] == key
        history = checkpoint_history(service, final)
        assert history["state"]["chat_contexts"][key]["value"] == value
        assert candidate["result_checkpoint_id"] == history["latest"]["checkpoint_id"]
        saved = deepcopy(value)
        second = run(service, final)
        assert second["status"] == "succeeded", second["chains"]
        assert second["objects"][key]["value"]["rounds"][0] == saved["rounds"][0]
        assert len(second["objects"][key]["value"]["rounds"]) == 2


def test_logical_snapshot_reads_stored_data_without_artifact_resolution_and_is_detached(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "v2-snapshot.sqlite")) as service:
        graph, _, frontends = _graph(service)
        graph = _save(service, graph)
        initial = _create(service, graph)
        final = run(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        owner = _owner(graph, frontends[0], final)
        monkeypatch.setattr(SessionObjectStore, "_resolve_write_artifact",
                            lambda *args, **kwargs: pytest.fail("Logical chat snapshots cannot require artifacts"))
        snapshot = service.read_frontend_node_snapshot(*owner)
        assert set(snapshot) == {"schema_version", "kind", "owner", "basis", "chat"}
        assert snapshot["schema_version"] == 2
        assert snapshot["chat"] == final["objects"]["chat-0"]["value"]
        assert snapshot["chat"]["rounds"][0]["candidates"][0]["result_checkpoint_id"] is not None
        snapshot["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] = "local"
        assert service.get_cached_frontend_node_snapshot(*owner)["chat"] == final["objects"]["chat-0"]["value"]


def test_two_logical_nodes_and_sessions_do_not_share_values_or_snapshots(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "v2-isolation.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2)
        graph = _save(service, graph)
        first, other = _create(service, graph), _create(service, graph)
        final = run(service, first)
        assert final["status"] == "succeeded", final["chains"]
        assert service.get_session(other["workflow_session_id"])["objects"] == other["objects"]
        for index, frontend in enumerate(frontends):
            snapshot = service.read_frontend_node_snapshot(*_owner(graph, frontend, final))
            assert snapshot["owner"]["object_key"] == "chat-" + str(index)
            assert snapshot["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] == "message " + str(index)
            empty = service.read_frontend_node_snapshot(*_owner(graph, frontend, other))
            assert empty["chat"] == {"rounds": []}
