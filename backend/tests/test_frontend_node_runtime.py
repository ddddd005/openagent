"""Whole frontend nodes append graph messages through real object settlement."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_contracts import FRONTEND_STATE_TYPE
from phase1_agent.graph_contracts import GraphCompiler, NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding

from test_graph_service import document, edge, node, run
from test_success_checkpoints import checkpoint_history


def _graph(service, *, count=1, bound=False, readers=True, writers=True):
    messages = [
        node(service.registry, "tools.editable-input", 19001 + index, text="message " + str(index))
        for index in range(count)
    ]
    frontends = [
        node(service.registry, "frontend.node", 19011 + index, object_key="chat-" + str(index),
             input_node_id=messages[index]["node_binding_id"] if bound else "")
        for index in range(count)
    ]
    graph = document([*messages, *frontends], [
        edge(message, frontend, 19001 + index, target_port="content")
        for index, (message, frontend) in enumerate(zip(messages, frontends))
    ])
    graph.update(
        schema_version=3, package_lock=list(service.registry.execution_package_lock),
        object_bindings=[
            ObjectBinding(
                entry["config"]["object_key"], FRONTEND_STATE_TYPE, 1, "shared",
                readers=(entry["node_binding_id"],) if readers else (),
                writers=(entry["node_binding_id"],) if writers else (),
                registrar_node_id=entry["node_binding_id"],
            ).to_dict()
            for entry in frontends
        ],
    )
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    return graph, messages, frontends


def _save(service, graph):
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()),
    )
    assert saved["outcome"] == "saved"
    return saved["document"]


def _create(service, graph):
    return service.create_session(
        graph["workflow_definition_id"], graph["revision"], idempotency_key=str(uuid4()),
    )


def _output(session, entry, port):
    return next(item["outputs"][port] for item in session["nodes"]
                if item["node_binding_id"] == entry["node_binding_id"])


def test_frontend_node_contract_is_working_output_with_explicit_registration_and_access(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-contract.sqlite")) as service:
        entry = service.registry.get("frontend.node", "1")
        descriptor = entry.definition.to_dict()
        assert descriptor["display_name"] == "前端节点"
        assert descriptor["default_config"] == {
            "object_key": "frontend", "role": "assistant", "input_node_id": "",
        }
        assert descriptor["execution_class"] == "working" and descriptor["is_output"]
        assert descriptor["input_storage"] == "references"
        assert descriptor["inputs"] == [{
            "port_id": "content", "data_type": "TEXT", "data_schema_version": 2,
            "required": True, "multiple": True,
        }]
        assert [port["port_id"] for port in descriptor["outputs"]] == ["proof", "commit"]
        assert [port["data_type"] for port in descriptor["outputs"]] == [
            "FRONTEND_NODE_APPEND", "FRONTEND_COMMIT",
        ]
        assert descriptor["provided_session_item_types"] == [
            {"type_id": FRONTEND_STATE_TYPE, "schema_version": 1},
        ]
        assert descriptor["object_accesses"] == [{
            "config_field": "object_key", "multiple": False, "access": "read_write",
            "type_id": FRONTEND_STATE_TYPE, "schema_version": 1,
        }]
        assert descriptor["config_node_references"][0]["target_types"] == [
            {"component_id": "tools.editable-input", "component_version": "1"},
        ]
        assert entry.settlement_builder is not None
        for invalid in (None, "not-a-node", 4):
            with pytest.raises(ContractValidationError):
                service.registry.validate_config(entry, {
                    **entry.definition.default_config, "input_node_id": invalid,
                })


@pytest.mark.parametrize("target", ["malformed", None, 4, "missing", "wrong_type", "wrong_version"])
def test_optional_input_reference_still_rejects_invalid_or_inexact_targets(tmp_path, target):
    with closing(GraphWorkflowService(tmp_path / "frontend-reference-invalid.sqlite")) as service:
        graph, messages, frontends = _graph(service)
        if target == "missing":
            value = str(uuid4())
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


def test_optional_reference_true_allows_only_empty_or_exact_editable_input(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-reference-valid.sqlite")) as service:
        graph, messages, frontends = _graph(service)
        declaration = service.registry.get("frontend.node", "1").definition.config_node_references[0]
        assert declaration["allow_empty"] is True
        empty = GraphCompiler(service.registry).compile(graph)
        frontends[0]["config"]["input_node_id"] = messages[0]["node_binding_id"]
        bound = GraphCompiler(service.registry).compile(graph)
        assert empty.ordered_node_ids == bound.ordered_node_ids
        assert empty.target_node_ids == bound.target_node_ids


@pytest.mark.parametrize("allow_empty", [None, False])
def test_old_reference_without_explicit_empty_permission_still_rejects_empty(tmp_path, allow_empty):
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
        current = service.registry.get("frontend.node", "1").definition
        reference = {**current.config_node_references[0], "allow_empty": allow_empty}
        candidate = replace(current, component_id="test.invalid-empty-reference",
                            config_node_references=(reference,))
        with pytest.raises(ContractValidationError) as caught:
            service.registry.register(candidate, lambda *args: {})
        assert caught.value.reason_code == "graph_invalid_type"


@pytest.mark.parametrize("bound", [False, True])
def test_graph_without_browser_appends_new_messages_and_returns_actual_receipt(tmp_path, bound):
    with closing(GraphWorkflowService(tmp_path / "frontend-append.sqlite")) as service:
        graph, messages, frontends = _graph(service, bound=bound)
        graph = _save(service, graph)
        assert frontends[0]["node_binding_id"] in GraphCompiler(service.registry).compile(
            graph).target_node_ids
        first = run(service, _create(service, graph))
        assert first["status"] == "succeeded", first["chains"]
        key = frontends[0]["config"]["object_key"]
        initial_entries = deepcopy(first["objects"][key]["value"]["entries"])
        assert len(initial_entries) == 1 and initial_entries[0]["role"] == "assistant"
        proof = _output(first, frontends[0], "proof")
        commit = _output(first, frontends[0], "commit")
        assert proof["entries"] == initial_entries
        assert commit["receipt"] == {
            "object_key": key, "revision": first["objects"][key]["revision"],
            "revision_id": first["objects"][key]["revision_id"], "deleted": False,
        }
        assert first["objects"][key]["value"]["view_ref"] == commit["view_ref"]
        artifact = service.read_object_artifact(
            first["workflow_session_id"], object_key=key, node_id=frontends[0]["node_binding_id"],
            reference=initial_entries[0]["source_ref"],
        )
        assert artifact["value"] == text_content(messages[0]["config"]["text"])
        second = run(service, first)
        assert second["status"] == "succeeded", second["chains"]
        entries = second["objects"][key]["value"]["entries"]
        assert len(entries) == 2 and entries[:1] == initial_entries
        assert entries[1]["entry_id"] != entries[0]["entry_id"]
        assert first["objects"][key]["value"]["entries"] == initial_entries
        assert checkpoint_history(service, second)["state"]["agent_contexts"] == {}


def test_multiple_message_bindings_keep_graph_order_and_empty_text(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-order.sqlite")) as service:
        graph, messages, frontends = _graph(service)
        extra = node(service.registry, "tools.text", 19002, text="")
        extra["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        graph["nodes"].insert(0, extra)
        graph["edges"][0]["order"] = 4
        graph["edges"].append(edge(extra, frontends[0], 19002, target_port="content", order=2))
        final = run(service, _create(service, _save(service, graph)))
        assert final["status"] == "succeeded", final["chains"]
        entries = final["objects"]["chat-0"]["value"]["entries"]
        texts = [
            service.read_object_artifact(
                final["workflow_session_id"], object_key="chat-0",
                node_id=frontends[0]["node_binding_id"], reference=entry["source_ref"],
            )["value"]["text"]
            for entry in entries
        ]
        assert texts == ["", messages[0]["config"]["text"]]


def test_two_nodes_and_sessions_keep_registered_items_separate(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-isolation.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2)
        graph = _save(service, graph)
        source, other = _create(service, graph), _create(service, graph)
        before_other = deepcopy(other["objects"])
        first = run(service, source)
        assert first["status"] == "succeeded", first["chains"]
        assert service.get_session(other["workflow_session_id"])["objects"] == before_other
        for index, entry in enumerate(frontends):
            key = entry["config"]["object_key"]
            assert len(first["objects"][key]["value"]["entries"]) == 1
            assert _output(first, entry, "proof")["owner"] == {
                "workflow_session_id": first["workflow_session_id"], "object_key": key,
            }
        assert first["objects"]["chat-0"]["value"] != first["objects"]["chat-1"]["value"]
        independent = run(service, other)
        assert independent["status"] == "succeeded", independent["chains"]
        assert independent["objects"]["chat-0"]["revision_id"] != first["objects"]["chat-0"]["revision_id"]


@pytest.mark.parametrize(("readers", "writers"), [(False, True), (True, False)])
def test_registration_does_not_grant_missing_acl(tmp_path, readers, writers):
    with closing(GraphWorkflowService(tmp_path / "frontend-acl.sqlite")) as service:
        graph, _, _ = _graph(service, readers=readers, writers=writers)
        with pytest.raises(ContractValidationError) as caught:
            GraphCompiler(service.registry).compile(_save(service, graph))
        assert caught.value.reason_code == "session_object_access_denied"


def test_frontend_node_cannot_append_to_another_registrars_item(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-registrar.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2)
        frontends[0]["config"]["object_key"] = "chat-1"
        graph["object_bindings"][1]["readers"].append(frontends[0]["node_binding_id"])
        graph["object_bindings"][1]["writers"].append(frontends[0]["node_binding_id"])
        initial = _create(service, _save(service, graph))
        final = run(service, initial)
        assert final["status"] == "failed", final["chains"]
        assert final["objects"] == initial["objects"]
        assert "frontend_node_registration_mismatch" in str(final["chains"])


def test_rollback_after_object_apply_publishes_no_messages_or_proof(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "frontend-rollback.sqlite")) as service:
        graph, _, frontends = _graph(service)
        initial = _create(service, _save(service, graph))
        original = service._save_effects

        def fail_after_apply(repo, sid, document, event):
            result = original(repo, sid, document, event)
            if event["node_binding_id"] == frontends[0]["node_binding_id"]:
                raise OSError("frontend append settlement interrupted")
            return result

        monkeypatch.setattr(service, "_save_effects", fail_after_apply)
        final = run(service, initial)
        assert final["status"] == "archive_failed", final["chains"]
        assert final["objects"] == initial["objects"]
        assert checkpoint_history(service, final)["latest"] is None
        with closing(service._store()) as store:
            assert store._connection.execute(
                "SELECT count(*) FROM records WHERE record_type='workflow_output' "
                "AND json_extract(payload,'$.node_binding_id')=?",
                (frontends[0]["node_binding_id"],),
            ).fetchone()[0] == 0
            assert store._connection.execute(
                "SELECT count(*) FROM session_object_receipts WHERE session_id=?",
                (initial["workflow_session_id"],),
            ).fetchone()[0] == 0


def test_accepted_frontend_append_does_not_finish_uncompleted_downstream(tmp_path):
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "frontend downstream gate timed out"
        return {"output": text_content("done")}

    with closing(GraphWorkflowService(tmp_path / "frontend-downstream.sqlite")) as service:
        service.registry.register(NodeDefinition(
            "test.frontend-downstream", "1", "Downstream", "Test", {}, {"type": "object"},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),), is_output=True,
        ), gate)
        graph, _, frontends = _graph(service)
        downstream = node(service.registry, "test.frontend-downstream", 19030)
        downstream["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        graph["nodes"].append(downstream)
        graph["control_edges"] = [{
            "edge_id": str(uuid4()), "source_node_id": frontends[0]["node_binding_id"],
            "target_node_id": downstream["node_binding_id"],
        }]
        initial = _create(service, _save(service, graph))
        started = service.start(
            initial["workflow_session_id"], expected_revision=initial["revision"],
            idempotency_key=str(uuid4()),
        )
        try:
            assert entered.wait(5)
            active = service.get_session(initial["workflow_session_id"])
            assert active["status"] == "running"
            assert len(active["objects"]["chat-0"]["value"]["entries"]) == 1
            assert checkpoint_history(service, active)["latest"] is None
        finally:
            release.set()
            service.wait(started["active_chain_run_id"])
        final = service.get_session(initial["workflow_session_id"])
        assert final["status"] == "succeeded", final["chains"]
        assert checkpoint_history(service, final)["latest"] is not None
