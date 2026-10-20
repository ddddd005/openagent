"""Stage 2 acceptance gaps for checkpoint fork writes and receipts."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.checkpoint_repository import CheckpointRepository
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeRegistry
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.prompt_lifecycle import lifecycle_prompt_content, make_lifecycle_prompt_item
from phase1_agent.session_objects import SessionObjectStore
from test_context_native_integration import NativeTransport, native_graph
from test_graph_application_receipt_custody import raw_database
from test_graph_service import (
    create, document, edge, node, run as graph_run, service, text_graph,
)
from test_models_service_integration import ModelDatabaseFixture
from test_success_checkpoints import checkpoint_history
from test_workflow_configuration import declaration as prompt_declaration, member as prompt_member
from workflow_test_support import run as native_run


OPERATION = "checkpoint.fork"
NATIVE_OPERATION = "graph.checkpoint.fork"


def _fork(service, source, checkpoint_id, key=None):
    return service.fork_checkpoint(
        source["workflow_session_id"],
        checkpoint_id=checkpoint_id,
        expected_session_revision=source["revision"],
        expected_data_revision=source["data_revision"],
        expected_head_revision=source["head_revision"],
        idempotency_key=key or str(uuid4()),
    )


def _app_request(source, checkpoint_id, key):
    return {
        "session_id": source["workflow_session_id"],
        "checkpoint_id": checkpoint_id,
        "expected_session_revision": source["revision"],
        "expected_data_revision": source["data_revision"],
        "expected_head_revision": source["head_revision"],
        "idempotency_key": key,
    }


def _adjustable_prompt_graph(service, execute, initial_text):
    prompt_entry = service.registry.get("prompts.item", "3")
    assert prompt_entry is not None
    assert prompt_entry.executor is execute
    assert prompt_entry.definition.adjustable_configuration == prompt_declaration()
    prompt = {
        "node_binding_id": str(uuid4()),
        "component_id": "prompts.item",
        "component_version": "3",
        "title": "Current prompt",
        "position": {"x": 10, "y": 20},
        "configuration_policy": {"schema_version": 1, "prompt_mode": "adjustable"},
        "config": prompt_member(initial_text),
    }
    assembly = node(service.registry, "prompts.assembly", 8102)
    text = node(service.registry, "tools.prompt-to-text", 8103)
    output = node(service.registry, "tools.output", 8104, mode="text")
    output["public_outputs"] = ["output"]
    graph = document(
        [prompt, assembly, text, output],
        [edge(prompt, assembly, 8101), edge(assembly, text, 8102), edge(text, output, 8103)],
    )
    graph["schema_version"] = 3
    graph["package_lock"] = deepcopy(list(service.registry.execution_package_lock))
    for item in graph["nodes"]:
        item["configuration_policy"] = {
            "schema_version": 1, "prompt_mode": "adjustable"
            if item["node_binding_id"] == prompt["node_binding_id"] else "fixed",
        }
    return graph, prompt, output


def _test_prompt_executor(config, inputs, context):
    item = make_lifecycle_prompt_item(
        context.node_binding_id, config["id"], config["text"], config["presentation"],
        lifecycle=config["lifecycle"], compaction=config["compaction"],
        source={"kind": "prompt_item", "node_id": context.node_binding_id, "member_id": config["id"]},
        metadata=config["metadata"],
    )
    return {"output": lifecycle_prompt_content([item])}


def _adjustable_prompt_service(database):
    production = create_package_registry().registry
    registry = NodeRegistry(production.data_types.detached())
    registry.package_lock = deepcopy(production.package_lock)
    registry.execution_package_lock = deepcopy(production.execution_package_lock)
    for component_id, version in (
        ("prompts.item", "3"), ("prompts.assembly", "2"),
        ("tools.prompt-to-text", "1"), ("tools.output", "1"),
    ):
        registered = production.get(component_id, version)
        assert registered is not None
        registry.register(
            registered.definition,
            _test_prompt_executor if component_id == "prompts.item" else registered.executor,
            config_validator=registered.config_validator,
        )
    return GraphWorkflowService(database, registry=registry)


def _update_prompt_text(service, graph, expected_edit_sequence, text, key):
    changed = deepcopy(graph)
    changed["nodes"][0]["config"]["text"] = text
    return service.update_definition(
        changed,
        workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=expected_edit_sequence,
        idempotency_key=key,
    )


def _inject_after_graph_record(monkeypatch, kind, source_session_id):
    original = GraphRecordStore.put
    fired = []

    def write_then_fail(repo, record_kind, value):
        result = original(repo, record_kind, value)
        if record_kind == kind and value.get("workflow_session_id") != source_session_id:
            fired.append(record_kind)
            raise RuntimeError(f"injected after {record_kind}")
        return result

    monkeypatch.setattr(GraphRecordStore, "put", write_then_fail)
    return fired


def test_forking_the_full_root_copies_no_later_checkpoints(service):
    source = create(service, text_graph(service.registry))
    for _ in range(3):
        source = graph_run(service, source)
        assert source["status"] == "succeeded", source["chains"]
    history = checkpoint_history(service, source)
    assert len(history["path"]) == 3
    root = history["path"][0]
    assert root["state_storage"]["mode"] == "full"

    result = _fork(service, source, root["checkpoint_id"])
    target = service.get_session(result["target_session_id"])
    target_history = checkpoint_history(service, target)

    assert len(target_history["path"]) == 1
    copied = target_history["path"][0]
    assert copied["checkpoint_id"] == result["target_checkpoint_id"]
    assert copied["checkpoint_id"] != root["checkpoint_id"]
    assert copied["parent_checkpoint_id"] is None
    assert copied["state_storage"] == root["state_storage"]
    assert target_history["state"] == history["path"][0]["state_storage"]["state"]
    assert target_history["state"] == target_history["collected"]


def test_fork_continuation_uses_current_adjustable_config_after_checkpoint(tmp_path):
    with closing(_adjustable_prompt_service(tmp_path / "adjustable-fork.sqlite")) as service:
        graph, prompt, output = _adjustable_prompt_graph(service, _test_prompt_executor, "before checkpoint")

        saved = service.update_definition(
            graph,
            workflow_definition_id=graph["workflow_definition_id"],
            expected_edit_sequence=0,
            idempotency_key="step26-adjustable-initial-save",
        )
        source = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"],
            idempotency_key="step26-adjustable-source",
        )
        source = graph_run(service, source)
        assert source["status"] == "succeeded", source["chains"]
        source_history = checkpoint_history(service, source)
        assert source_history["latest"] is not None

        updated = _update_prompt_text(
            service, saved["document"], saved["edit_sequence"],
            "latest adjustable value", "step26-adjustable-after-checkpoint",
        )
        assert updated["outcome"] == "saved"
        assert updated["definition_revision"] == source["definition_revision"]
        assert service.get_definition(saved["workflow_definition_id"])["nodes"][0]["config"]["text"] == (
            "latest adjustable value")
        with closing(sqlite3.connect(service.database)) as connection:
            configuration_before_fork = tuple(connection.execute(
                "SELECT * FROM workflow_current_configuration WHERE workflow_definition_id=?",
                (saved["workflow_definition_id"],),
            ))

        result = _fork(service, source, source_history["latest"]["checkpoint_id"])
        target = service.get_session(result["target_session_id"])
        assert target["workflow_definition_id"] == source["workflow_definition_id"]
        assert target["definition_revision"] == source["definition_revision"]
        target = graph_run(service, target)
        assert target["status"] == "succeeded", target["chains"]
        detail = service.get_run(target["workflow_session_id"], target["chains"][-1]["chain_run_id"])
        prompt_run = next(item for item in detail["node_runs"]
                          if item["node_binding_id"] == prompt["node_binding_id"])
        assert prompt_run["config"]["text"] == "latest adjustable value"
        output_record = next(item for item in detail["outputs"]
                             if item["node_binding_id"] == output["node_binding_id"])
        assert output_record["payload"]["text"] == "latest adjustable value"
        with closing(sqlite3.connect(service.database)) as connection:
            assert tuple(connection.execute(
                "SELECT * FROM workflow_current_configuration WHERE workflow_definition_id=?",
                (saved["workflow_definition_id"],),
            )) == configuration_before_fork


def test_edit_after_run_preparation_does_not_change_that_run_config(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    with closing(_adjustable_prompt_service(tmp_path / "prepared-config.sqlite")) as service:
        graph, prompt, output = _adjustable_prompt_graph(service, _test_prompt_executor, "fixed A")
        saved = service.update_definition(
            graph,
            workflow_definition_id=graph["workflow_definition_id"],
            expected_edit_sequence=0,
            idempotency_key="step26-prepared-config-initial-save",
        )
        source = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"],
            idempotency_key="step26-prepared-config-source",
        )
        adjustable_b = _update_prompt_text(
            service, saved["document"], saved["edit_sequence"],
            "prepared B", "step26-prepared-config-before-run",
        )
        assert adjustable_b["outcome"] == "saved"
        assert adjustable_b["definition_revision"] == saved["definition_revision"]

        original_execute = service._execute

        def wait_before_execute(session_id, chain_id):
            entered.set()
            assert release.wait(10), "prepared execution gate timed out"
            return original_execute(session_id, chain_id)

        monkeypatch.setattr(service, "_execute", wait_before_execute)
        started = service.start(
            source["workflow_session_id"],
            expected_revision=source["revision"],
            idempotency_key="step26-prepared-config-run",
        )
        chain_id = started["active_chain_run_id"]
        try:
            assert entered.wait(5)
            prepared = service.get_run(source["workflow_session_id"], chain_id)
            prepared_prompt = next(item for item in prepared["node_runs"]
                                   if item["node_binding_id"] == prompt["node_binding_id"])
            assert prepared_prompt["config"]["text"] == "prepared B"
            updated = _update_prompt_text(
                service, adjustable_b["document"], adjustable_b["edit_sequence"],
                "current C", "step26-prepared-config-after-run-start",
            )
            assert updated["outcome"] == "saved"
            assert updated["definition_revision"] == saved["definition_revision"]
        finally:
            release.set()
            service.wait(chain_id)

        completed = service.get_session(source["workflow_session_id"])
        detail = service.get_run(completed["workflow_session_id"], chain_id)
        completed_prompt = next(item for item in detail["node_runs"]
                                if item["node_binding_id"] == prompt["node_binding_id"])
        output_record = next(item for item in detail["outputs"]
                             if item["node_binding_id"] == output["node_binding_id"])
        assert completed_prompt["config"]["text"] == "prepared B"
        assert output_record["payload"]["text"] == "prepared B"


def test_fork_rolls_back_after_each_completed_write_stage(tmp_path, monkeypatch):
    transport = NativeTransport()
    with closing(GraphWorkflowService(
            tmp_path / "fork-stage-rollback.sqlite",
            public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        graph, _ = native_graph(service, policy=False, once=False)
        source = create(service, graph)
        source = native_run(service, source, "Create one accepted context checkpoint.")
        assert source["status"] == "succeeded", source["chains"]
        history = checkpoint_history(service, source)
        checkpoint_id = history["latest"]["checkpoint_id"]
        source_id = source["workflow_session_id"]

        def inject_graph_record(kind):
            def install(patch):
                fired = _inject_after_graph_record(patch, kind, source_id)
                return fired
            return install

        def inject_checkpoint(patch):
            original = CheckpointRepository.insert
            fired = []

            def insert_then_fail(repository, checkpoint):
                result = original(repository, checkpoint)
                if repository.owner["workflow_session_id"] != source_id:
                    fired.append("workflow_checkpoints")
                    raise RuntimeError("injected after workflow_checkpoints")
                return result

            patch.setattr(CheckpointRepository, "insert", insert_then_fail)
            return fired

        def inject_object_initialization(patch):
            original = SessionObjectStore.initialize
            fired = []

            def initialize_then_fail(objects, session_id, document, **kwargs):
                result = original(objects, session_id, document, **kwargs)
                if session_id != source_id:
                    fired.append("session_object_initialize")
                    raise RuntimeError("injected after session object initialization")
                return result

            patch.setattr(SessionObjectStore, "initialize", initialize_then_fail)
            return fired

        def inject_agent_write(patch):
            original = SessionObjectStore._initialize_checkpoint_values
            fired = []

            def initialize_then_fail(objects, session_id, *args, **kwargs):
                result = original(objects, session_id, *args, **kwargs)
                if session_id != source_id:
                    fired.append("checkpoint_value_initialization")
                    raise RuntimeError("injected after checkpoint value initialization")
                return result

            patch.setattr(SessionObjectStore, "_initialize_checkpoint_values", initialize_then_fail)
            return fired

        def inject_manifest(patch):
            original = service._save_manifest
            fired = []

            def save_then_fail(repo, kind, identity, session, *, history=None, status=None,
                               registry=None):
                result = original(repo, kind, identity, session, history=history,
                                  status=status, registry=registry)
                if session["workflow_session_id"] != source_id:
                    fired.append("graph_state_manifests")
                    raise RuntimeError("injected after graph_state_manifests")
                return result

            patch.setattr(service, "_save_manifest", save_then_fail)
            return fired

        def inject_native_receipt(_patch):
            with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
                connection.execute(
                    "CREATE TRIGGER step26_fail_native_fork_receipt "
                    "AFTER INSERT ON idempotency "
                    f"WHEN NEW.operation='{NATIVE_OPERATION}' "
                    "BEGIN SELECT RAISE(ABORT,'injected native fork receipt'); END")
            return ["trigger-installed"]

        def drop_native_receipt_trigger():
            with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
                connection.execute("DROP TRIGGER IF EXISTS step26_fail_native_fork_receipt")

        def inject_application_identity(patch):
            import phase1_agent.graph_store as graph_store_module

            original = graph_store_module.persist_application_command_identity
            fired = []

            def persist_then_fail(*args, **kwargs):
                result = original(*args, **kwargs)
                fired.append("application_identity")
                raise RuntimeError("injected after application identity")

            patch.setattr(graph_store_module, "persist_application_command_identity",
                          persist_then_fail)
            return fired

        stages = [
            ("target session", inject_graph_record("workflow_session"), RuntimeError),
            ("target private state", inject_graph_record("node_session"), RuntimeError),
            ("checkpoint prefix", inject_checkpoint, RuntimeError),
            ("initialized objects", inject_object_initialization, RuntimeError),
            ("checkpoint context initialization", inject_agent_write, RuntimeError),
            ("snapshot", inject_graph_record("state_snapshot"), RuntimeError),
            ("commit", inject_graph_record("workflow_commit"), RuntimeError),
            ("workflow ref", inject_graph_record("workflow_ref"), RuntimeError),
            ("manifest", inject_manifest, RuntimeError),
            ("native command receipt", inject_native_receipt, sqlite3.IntegrityError),
            ("application identity receipt", inject_application_identity, RuntimeError),
        ]
        for index, (name, install, error_type) in enumerate(stages):
            request = _app_request(source, checkpoint_id, f"step26-stage-{index}")
            before = raw_database(service.database)
            if name == "native command receipt":
                fired = install(None)
                try:
                    with pytest.raises(error_type, match="injected native fork receipt"):
                        GraphApplication(service).command(OPERATION, request)
                finally:
                    drop_native_receipt_trigger()
            else:
                with monkeypatch.context() as patch:
                    fired = install(patch)
                    with pytest.raises(error_type, match="injected after"):
                        GraphApplication(service).command(OPERATION, request)
            assert fired
            assert raw_database(service.database) == before, name
            assert len(service.list_sessions(source["workflow_definition_id"])) == 1, name
            assert len(transport.calls) == 1


def test_unknown_checkpoint_fork_request_receipt_is_unresolved_and_read_only(service):
    source = create(service, text_graph(service.registry))
    request = _app_request(source, str(uuid4()), "never-accepted-checkpoint-fork")
    before = raw_database(service.database)

    result = read_graph_application_receipt(service.database, OPERATION, request)

    assert result["outcome"] == "unresolved"
    assert result["reason_code"] == "application_identity_missing"
    assert raw_database(service.database) == before
