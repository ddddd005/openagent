from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.checkpoint_repository import CheckpointRepository
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import GraphDiagnosticError, NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from test_graph_service import document, edge, node


def _save(service, value):
    value = deepcopy(value)
    value["schema_version"] = 3
    for item in value["nodes"]:
        item["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    return service.update_definition(
        value, workflow_definition_id=value["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))


def _points(service, session):
    with closing(service._store()) as store:
        store._connection.execute("BEGIN")
        try:
            repository = CheckpointRepository(
                store._connection, workflow_definition_id=session["workflow_definition_id"],
                workflow_session_id=session["workflow_session_id"],
                fixed_definition_revision=session["definition_revision"],
            )
            latest = repository.latest()
            points = repository.prefix(latest["checkpoint_id"]) if latest is not None else []
            return points, [repository.materialize(point["checkpoint_id"]) for point in points]
        finally:
            store._connection.execute("ROLLBACK")


def test_formal_service_selects_connected_writer_and_ignores_non_working_and_isolated_nodes(tmp_path, monkeypatch):
    registry = create_package_registry().registry.detached()
    calls = []
    registry.register(NodeDefinition(
        "test.terminal-writer", "1", "Writer", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
    ), lambda config, inputs, context: calls.append(inputs["input"]["text"]) or {})
    registry.register(NodeDefinition(
        "test.manual", "1", "Manual", "Test", {}, {"type": "object"},
        execution_class="non_working",
    ), lambda config, inputs, context: pytest.fail("non-working node executed"))
    original_maybe = GraphRecordStore.maybe

    def no_old_declarations(repo, kind, **identity):
        assert kind != "node_definition", "old declaration authority was read"
        return original_maybe(repo, kind, **identity)

    monkeypatch.setattr(GraphRecordStore, "maybe", no_old_declarations)
    with closing(GraphWorkflowService(tmp_path / "selection.sqlite", registry=registry)) as service:
        source = node(service.registry, "tools.text", 1, text="accepted")
        writer = node(service.registry, "test.terminal-writer", 2)
        output = node(service.registry, "tools.output", 3)
        manual_a = node(service.registry, "test.manual", 4)
        manual_b = node(service.registry, "test.manual", 5)
        isolated = node(service.registry, "tools.text", 6)
        value = document([writer, source, manual_a, manual_b, isolated, output], [
            edge(source, writer, 1), edge(source, output, 2),
        ])
        value["control_edges"] = [{
            "edge_id": str(uuid4()), "source_node_id": manual_a["node_binding_id"],
            "target_node_id": manual_b["node_binding_id"],
        }]
        saved = _save(service, value)
        session = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"], idempotency_key=str(uuid4()))
        assert session["readonly"] is False
        started = service.start(session["workflow_session_id"], expected_revision=session["revision"],
                                idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        result = service.get_run(session["workflow_session_id"], started["active_chain_run_id"])
        assert result["chain"]["status"] == "succeeded", result["chain"]["diagnostic"]
        assert result["chain"]["ordered_nodes"] == [
            source["node_binding_id"], writer["node_binding_id"], output["node_binding_id"],
        ]
        assert calls == ["accepted"]
        with closing(service._store()) as store:
            assert GraphRecordStore(store).rows("node_definition") == []


def test_paused_run_and_success_checkpoint_keep_accepted_declarations_then_new_run_uses_update(tmp_path):
    registry = create_package_registry().registry.detached()
    entered, release = Event(), Event()

    def execute(config, inputs, context):
        entered.set()
        assert release.wait(10), "declaration snapshot test timed out"
        return {"output": text_content(config["retained"])}

    definition = NodeDefinition(
        "test.declaration-source", "1", "Source", "Test",
        {"retained": "original", "other": "new-projection"},
        {"type": "object", "properties": {"retained": {"type": "string"}, "other": {"type": "string"}},
         "required": ["retained", "other"], "additionalProperties": False},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        checkpoint_save={"config_paths": [["retained"]]},
    )
    registry.register(definition, execute)
    with closing(GraphWorkflowService(tmp_path / "frozen.sqlite", registry=registry)) as service:
        source = node(service.registry, definition.component_id, 1)
        output = node(service.registry, "tools.output", 2)
        saved = _save(service, document([source, output], [edge(source, output, 1)]))
        session = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"], idempotency_key=str(uuid4()))
        started = service.start(session["workflow_session_id"], expected_revision=session["revision"],
                                idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        try:
            assert entered.wait(5)
            running = service.get_session(session["workflow_session_id"])
            service.control(running["workflow_session_id"], action="pause",
                            expected_revision=running["revision"], idempotency_key=str(uuid4()))
            updated = replace(definition, checkpoint_save={"config_paths": [["other"]]},
                              execution_class="non_working")
            service.registry.update_declarations(updated)
        finally:
            release.set()
        service.wait(chain_id)
        paused = service.get_session(session["workflow_session_id"])
        assert paused["status"] == "paused"
        service.control(paused["workflow_session_id"], action="resume", expected_revision=paused["revision"],
                        idempotency_key=str(uuid4()))
        service.wait(chain_id)
        completed = service.get_session(session["workflow_session_id"])
        assert completed["status"] == "succeeded"
        points, states = _points(service, completed)
        assert states[0]["configuration"]["nodes"][source["node_binding_id"]]["config"] == {"retained": "original"}

        with pytest.raises(GraphDiagnosticError) as caught:
            service.start(completed["workflow_session_id"], expected_revision=completed["revision"],
                          idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "graph_non_working_dependency"
        service.registry.update_declarations(replace(updated, execution_class="working"))
        second = service.start(completed["workflow_session_id"], expected_revision=completed["revision"],
                               idempotency_key=str(uuid4()))
        service.wait(second["active_chain_run_id"])
        completed = service.get_session(session["workflow_session_id"])
        assert completed["status"] == "succeeded"
        new_points, new_states = _points(service, completed)
        assert new_points[0] == points[0]
        assert new_states[0] == states[0]
        assert new_states[1]["configuration"]["nodes"][source["node_binding_id"]]["config"] == {
            "other": "new-projection",
        }
        fork = service.fork_checkpoint(
            completed["workflow_session_id"], checkpoint_id=points[0]["checkpoint_id"],
            expected_session_revision=completed["revision"], expected_data_revision=completed["data_revision"],
            expected_head_revision=completed["head_revision"], idempotency_key=str(uuid4()))
        target = service.get_session(fork["target_session_id"])
        _, target_states = _points(service, target)
        assert target_states == [states[0]]


def test_current_definition_missing_does_not_fall_back_to_saved_descriptor(tmp_path):
    registry = create_package_registry().registry.detached()
    with closing(GraphWorkflowService(tmp_path / "missing.sqlite", registry=registry)) as service:
        source = node(service.registry, "tools.text", 1)
        output = node(service.registry, "tools.output", 2)
        saved = _save(service, document([source, output], [edge(source, output, 1)]))
        session = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"], idempotency_key=str(uuid4()))
        service.registry._nodes.pop(("tools.text", "1"))
        with pytest.raises(ContractValidationError) as caught:
            service.get_definition(saved["workflow_definition_id"])
        assert caught.value.reason_code == "graph_missing_node_type"
        assert service.get_definition(saved["workflow_definition_id"], saved["definition_revision"])["nodes"]
        with closing(service._store()) as store:
            repo = GraphRecordStore(store)
            assert repo.rows("chain_run", workflow_session_id=session["workflow_session_id"]) == []
            assert repo.rows("node_definition") == []


def test_isolated_incomplete_node_does_not_block_success_checkpoint(tmp_path):
    registry = create_package_registry().registry.detached()
    registry.register(NodeDefinition(
        "test.incomplete", "1", "Incomplete", "Test", {},
        {"type": "object", "properties": {"required": {"type": "string"}},
         "required": ["required"], "additionalProperties": False},
        checkpoint_save={"config_paths": [["required"]]},
    ), lambda config, inputs, context: pytest.fail("isolated incomplete node executed"))
    with closing(GraphWorkflowService(tmp_path / "incomplete.sqlite", registry=registry)) as service:
        source = node(service.registry, "tools.text", 1, text="complete")
        output = node(service.registry, "tools.output", 2)
        incomplete = node(service.registry, "test.incomplete", 3)
        saved = _save(service, document([source, incomplete, output], [edge(source, output, 1)]))
        session = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"], idempotency_key=str(uuid4()))
        started = service.start(session["workflow_session_id"], expected_revision=session["revision"],
                                idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        current = service.get_session(session["workflow_session_id"])
        assert current["status"] == "succeeded"
        result = service.get_run(current["workflow_session_id"], started["active_chain_run_id"])
        assert incomplete["node_binding_id"] not in result["chain"]["ordered_nodes"]
        points, states = _points(service, current)
        assert len(points) == len(states) == 1
        assert incomplete["node_binding_id"] not in states[0]["configuration"]["nodes"]


def test_explicit_event_executes_only_its_non_working_target_and_keeps_payload_authorization_and_receipt(tmp_path):
    registry = create_package_registry().registry.detached()
    calls = []
    registry.register(NodeDefinition(
        "test.explicit-manual", "1", "Manual", "Test", {}, {"type": "object"},
        execution_class="non_working", capabilities=("external:read",),
    ), lambda config, inputs, context: calls.append(context.external_input("text")) or {})
    registry.register(NodeDefinition(
        "test.unrelated-manual", "1", "Unrelated", "Test", {}, {"type": "object"},
        execution_class="non_working",
    ), lambda config, inputs, context: pytest.fail("event expanded beyond its declared target"))
    with closing(GraphWorkflowService(tmp_path / "event.sqlite", registry=registry)) as service:
        target = node(service.registry, "test.explicit-manual", 1)
        unrelated = node(service.registry, "test.unrelated-manual", 2)
        value = document([target, unrelated], [])
        value["execution_roots"] = []
        value["event_bindings"] = [{
            "event_id": "test.manual", "schema_version": 1, "display_name": "Manual",
            "audience": "management", "target_node_ids": [target["node_binding_id"]],
            "payload_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                               "required": ["text"], "additionalProperties": False},
        }]
        saved = _save(service, value)
        session = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"], idempotency_key=str(uuid4()))
        request = {
            "workflow_definition_id": saved["workflow_definition_id"],
            "definition_revision": saved["definition_revision"],
            "event_id": "test.manual", "event_schema_version": 1,
            "payload": {"text": "explicit"}, "expected_revision": session["revision"],
            "idempotency_key": str(uuid4()),
        }
        with pytest.raises(ContractValidationError) as caught:
            service.submit_consumer_event(session["workflow_session_id"], **request)
        assert caught.value.reason_code == "graph_event_not_public"
        with pytest.raises(GraphDiagnosticError) as caught:
            service.submit_event(session["workflow_session_id"], **{**request, "payload": {"text": 1}})
        assert caught.value.reason_code == "graph_event_payload_invalid"
        started = service.submit_event(session["workflow_session_id"], **request)
        service.wait(started["active_chain_run_id"])
        result = service.get_run(session["workflow_session_id"], started["active_chain_run_id"])
        assert result["chain"]["status"] == "succeeded", result["chain"]["diagnostic"]
        assert result["chain"]["ordered_nodes"] == [target["node_binding_id"]]
        assert result["chain"]["execution_kind"] == "event"
        assert service.submit_event(session["workflow_session_id"], **request) == started
        assert calls == ["explicit"]
        current = service.get_session(session["workflow_session_id"])
        assert _points(service, current) == ([], [])
