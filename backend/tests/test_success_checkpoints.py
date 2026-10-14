"""Successful round completion persists immutable collected checkpoints."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_collection import collect_checkpoint_state
from phase1_agent.checkpoint_repository import CheckpointRepository
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore

from test_context_native_integration import NativeTransport, native_graph
from test_graph_events import event_document, event_service, submit
from test_graph_service import (
    create, document, edge, gate_graph, node, run as graph_run, service, text_graph,
)
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import run as native_run


def checkpoint_history(service, session):
    store = service._store()
    try:
        connection = store._connection
        connection.execute("BEGIN")
        try:
            repository = CheckpointRepository(
                connection,
                workflow_definition_id=session["workflow_definition_id"],
                workflow_session_id=session["workflow_session_id"],
                fixed_definition_revision=session["definition_revision"],
            )
            latest = repository.latest()
            if latest is None:
                return {"latest": None, "path": [], "state": None, "collected": None}
            path = repository.prefix(latest["checkpoint_id"])
            return {
                "latest": latest,
                "path": path,
                "state": repository.materialize(latest["checkpoint_id"]),
                "collected": collect_checkpoint_state(
                    GraphRecordStore(store),
                    session["workflow_definition_id"],
                    session["definition_revision"],
                    session["workflow_session_id"],
                    types=service.registry.data_types, registry=service.registry,
                ),
            }
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
    finally:
        store.close()


def test_creation_is_empty_then_native_rounds_append_full_and_diff_checkpoints(tmp_path):
    transport = NativeTransport()
    with closing(GraphWorkflowService(
            tmp_path / "native-success-checkpoints.sqlite",
            public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, _ = native_graph(service, policy=False, once=False)
        initial = create(service, doc)
        assert checkpoint_history(service, initial)["latest"] is None

        first = native_run(service, initial, "First accepted input.")
        assert first["status"] == "succeeded", first["chains"]
        first_history = checkpoint_history(service, first)
        assert len(first_history["path"]) == 1
        root = first_history["path"][0]
        assert root["parent_checkpoint_id"] is None
        assert root["state_storage"]["mode"] == "full"
        assert first_history["state"] == first_history["collected"]
        assert first_history["state"]["agent_contexts"]["context-a"] == {
            "value": first["objects"]["context-a"]["value"],
        }
        frozen_root = deepcopy(root)
        frozen_root_state = deepcopy(first_history["state"])

        second = native_run(service, first, "Second accepted input.")
        assert second["status"] == "succeeded", second["chains"]
        second_history = checkpoint_history(service, second)
        assert len(second_history["path"]) == 2
        assert second_history["path"][0] == frozen_root
        assert second_history["path"][1]["parent_checkpoint_id"] == root["checkpoint_id"]
        diff = second_history["path"][1]["state_storage"]
        assert diff["mode"] == "diff"
        assert diff["base_checkpoint_id"] == root["checkpoint_id"]
        assert "context-a" in diff["upsert"]["agent_contexts"]
        assert second_history["state"] == second_history["collected"]
        assert second_history["state"]["agent_contexts"]["context-a"]["value"] == (
            second["objects"]["context-a"]["value"])
        assert second_history["state"] != frozen_root_state
        assert transport.calls


def test_schema_two_without_agent_saves_empty_and_declared_configuration(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "declared-checkpoint.sqlite")) as service:
        empty_doc = text_graph(service.registry)
        empty_session = create(service, empty_doc)
        assert checkpoint_history(service, empty_session)["latest"] is None
        empty_done = graph_run(service, empty_session)
        assert empty_done["status"] == "succeeded", empty_done["chains"]
        empty_state = checkpoint_history(service, empty_done)["state"]
        assert empty_doc["schema_version"] == 2
        assert empty_state == {
            "configuration": {
                "workflow_definition_id": empty_doc["workflow_definition_id"],
                "fixed_definition_revision": empty_doc["revision"],
                "nodes": {},
            },
            "agent_contexts": {},
        }

        service.registry.register(NodeDefinition(
            "test.checkpoint-declared", "1", "Declared checkpoint value", "Test",
            {"retained": "keep me", "private": "omit me"},
            {
                "type": "object",
                "properties": {"retained": {"type": "string"}, "private": {"type": "string"}},
                "required": ["retained", "private"],
                "additionalProperties": False,
            },
            inputs=(NodePort("input", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
            checkpoint_save={"config_paths": [["retained"]]},
        ), lambda config, inputs, context: {"output": inputs["input"]})
        source = node(service.registry, "tools.text", 6101, text="declared value")
        declared = node(service.registry, "test.checkpoint-declared", 6102,
                        retained="keep me", private="omit me")
        output = node(service.registry, "tools.output", 6103)
        output["public_outputs"] = ["output"]
        declared_doc = document(
            [source, declared, output],
            [edge(source, declared, 6101), edge(declared, output, 6102)],
        )
        declared_doc["package_lock"] = list(service.registry.execution_package_lock)
        initial = create(service, declared_doc)
        assert checkpoint_history(service, initial)["latest"] is None
        final = graph_run(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        state = checkpoint_history(service, final)["state"]
        assert state["agent_contexts"] == {}
        assert state["configuration"]["nodes"][declared["node_binding_id"]] == {
            "component_id": "test.checkpoint-declared",
            "component_version": "1",
            "config": {"retained": "keep me"},
        }


def test_failed_paused_and_event_completions_do_not_append_checkpoints(
        tmp_path, event_service, service):
    failed_transport = NativeTransport(bad_summary=True)
    with closing(GraphWorkflowService(
            tmp_path / "failed-checkpoint.sqlite",
            public_model_factory=failed_transport.factory)) as failed_service:
        ModelDatabaseFixture.write(failed_service, 1)
        failed_doc, _ = native_graph(failed_service)
        failed_initial = create(failed_service, failed_doc)
        failed = native_run(failed_service, failed_initial, "Fail before acceptance.")
        assert failed["status"] == "failed", failed["chains"]
        assert checkpoint_history(failed_service, failed)["latest"] is None

    sid, chain_id, release = gate_graph(service)
    try:
        current = service.get_session(sid)
        service.control(
            sid, action="pause", expected_revision=current["revision"],
            idempotency_key="success-checkpoint-pause",
        )
        release.set()
        service.wait(chain_id)
        paused = service.get_session(sid)
        assert paused["status"] == "paused"
        assert checkpoint_history(service, paused)["latest"] is None
    finally:
        release.set()
        service.wait(chain_id)

    event_instance, _ = event_service
    event_doc = event_document(event_instance)
    event_initial = create(event_instance, event_doc)
    accepted = submit(event_instance, event_initial, event_doc, idempotency_key="checkpoint-event")
    event_instance.wait(accepted["active_chain_run_id"])
    event_done = event_instance.get_session(event_initial["workflow_session_id"])
    assert event_instance.read_event(
        event_initial["workflow_session_id"], chain_id=accepted["active_chain_run_id"])["status"] == "succeeded"
    assert checkpoint_history(event_instance, event_done)["latest"] is None


def test_checkpoint_write_fault_rolls_back_and_replays_exact_success_receipts(
        tmp_path, monkeypatch):
    injected = []

    def fail_once(point):
        if point == "after_checkpoint_write" and not injected:
            injected.append(point)
            raise OSError("injected success checkpoint write failure")

    transport = NativeTransport()
    with closing(GraphWorkflowService(
            tmp_path / "checkpoint-retry.sqlite",
            public_model_factory=transport.factory,
            fault_injector=fail_once)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, _ = native_graph(service, policy=False, once=False)
        initial = create(service, doc)
        finish_attempts = []
        original_change = service._change

        def capture_finish(operation, key, request, callback):
            if operation == "graph.run.finish":
                finish_attempts.append((key, deepcopy(request)))
            return original_change(operation, key, request, callback)

        monkeypatch.setattr(service, "_change", capture_finish)
        start_key = str(uuid4())
        started = service.start(
            initial["workflow_session_id"],
            expected_revision=initial["revision"],
            idempotency_key=start_key,
            inputs={"text": "Accepted input for retry."},
        )
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        pending = service.get_session(initial["workflow_session_id"])
        assert pending["status"] == "archive_failed", pending["chains"]
        assert pending["chains"][-1]["diagnostic"]["reason_code"] == "checkpoint_commit_pending"
        assert pending["head_commit_id"] == initial["head_commit_id"]
        assert pending["head_revision"] == initial["head_revision"]
        assert checkpoint_history(service, pending)["latest"] is None
        assert len(transport.calls) == 1

        retry_key = str(uuid4())
        retrying = service.control(
            initial["workflow_session_id"], action="retry_archive",
            expected_revision=pending["revision"], idempotency_key=retry_key,
        )
        service.wait(chain_id)
        final = service.get_session(initial["workflow_session_id"])
        assert final["status"] == "succeeded" and final["active_chain_run_id"] is None
        assert final["head_commit_id"] != initial["head_commit_id"]
        assert final["head_revision"] == initial["head_revision"] + 1
        assert injected == ["after_checkpoint_write"]
        assert len(transport.calls) == 1
        history = checkpoint_history(service, final)
        assert len(history["path"]) == 1
        assert history["path"][0]["state_storage"]["mode"] == "full"
        assert history["state"] == history["collected"]

        assert len(finish_attempts) == 2
        assert finish_attempts[0] == finish_attempts[1]
        finish_key, finish_request = finish_attempts[-1]
        assert original_change(
            "graph.run.finish", finish_key, finish_request,
            lambda repo: pytest.fail("finish receipt replay invoked its callback"),
        ) == {"status": "succeeded"}
        assert service.start(
            initial["workflow_session_id"], expected_revision=initial["revision"],
            idempotency_key=start_key,
            inputs={"text": "Accepted input for retry."},
        ) == started
        assert service.control(
            initial["workflow_session_id"], action="retry_archive",
            expected_revision=pending["revision"], idempotency_key=retry_key,
        ) == retrying
        assert len(checkpoint_history(service, final)["path"]) == 1
        assert len(transport.calls) == 1
