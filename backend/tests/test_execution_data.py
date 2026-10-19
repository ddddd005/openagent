"""Execution-domain ports stay limited and are the real selected-path entry."""

from contextlib import closing
from dataclasses import fields
import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.content_contracts import text_content
from phase1_agent.execution_data import (
    ConfigurationReadSession,
    ExecutionChatData,
    ExecutionRecords,
    ExecutionSettlement,
    ExecutionState,
    ExecutionTypeEvidence,
    RunData,
)
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_records import graph_error, graph_record
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.session_objects import SessionObjectStore

from test_graph_service import (
    create, document, edge, node, run as graph_run, service, text_graph,
)


def _check(service, store, statement, parameters=()):
    return store._connection.execute(statement, parameters).fetchone()


def test_run_data_exposes_only_named_sessions_and_requires_the_open_transaction(
        service):
    basis = service._capture_run_basis()
    with closing(service._store()) as store:
        with pytest.raises(ContractValidationError) as failure:
            service._data_management.execution_transaction(
                GraphRecordStore(store), basis=basis)
        assert failure.value.reason_code == "storage_contract_violation"

        store._connection.execute("BEGIN")
        try:
            data = service._data_management.execution_transaction(
                GraphRecordStore(store), basis=basis)
            assert {item.name for item in fields(RunData)} == {
                "records", "state", "settlement", "type_evidence",
                "configuration", "chat"}
            for name in ("store", "connection", "repo", "atomic", "get", "put", "rows"):
                assert not hasattr(data, name)
                assert not hasattr(data.records, name)
            for port, expected in (
                (data.records, ExecutionRecords),
                (data.state, ExecutionState),
                (data.settlement, ExecutionSettlement),
                (data.type_evidence, ExecutionTypeEvidence),
                (data.configuration, ConfigurationReadSession),
                (data.chat, ExecutionChatData),
            ):
                assert isinstance(port, expected)
        finally:
            store._connection.execute("ROLLBACK")


def test_normal_round_uses_records_settlement_evidence_configuration_and_chat(
        service, monkeypatch):
    calls = {}

    def track(name, method):
        def wrapper(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return method(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(ExecutionRecords, "put_node_run",
                        track("records.put_node_run", ExecutionRecords.put_node_run))
    monkeypatch.setattr(ExecutionRecords, "put_chain",
                        track("records.put_chain", ExecutionRecords.put_chain))
    monkeypatch.setattr(ExecutionSettlement, "save_manifest",
                        track("settlement.save_manifest", ExecutionSettlement.save_manifest))
    monkeypatch.setattr(ExecutionTypeEvidence, "check_registry",
                        track("type_evidence.check_registry", ExecutionTypeEvidence.check_registry))
    monkeypatch.setattr(ConfigurationReadSession, "fixed_document",
                        track("configuration.fixed_document", ConfigurationReadSession.fixed_document))
    monkeypatch.setattr(ExecutionChatData, "invocations",
                        track("chat.invocations", ExecutionChatData.invocations))
    monkeypatch.setattr(ExecutionState, "objects",
                        track("state.objects", ExecutionState.objects))

    doc = text_graph(service.registry)
    session = create(service, doc)
    final = graph_run(service, session)
    assert final["status"] == "succeeded", final["chains"]
    for name in ("records.put_node_run", "records.put_chain",
                 "settlement.save_manifest", "type_evidence.check_registry",
                 "configuration.fixed_document", "chat.invocations",
                 "state.objects"):
        assert calls.get(name, 0) > 0, name


def test_independent_reread_observes_persisted_chain_outputs_and_checkpoint(service):
    doc = text_graph(service.registry)
    session = create(service, doc)
    final = graph_run(service, session)
    chain_id = final["selected_chain_run_id"]

    with service._data_management.read_execution(registry=service.registry) as data:
        stored_session = data.records.session(session["workflow_session_id"])
        chain = data.records.chain(chain_id)
        runs = data.records.node_runs(chain_id)
        outputs = data.records.outputs_for_chain(chain_id)
        head = data.records.head(session["workflow_session_id"])
        commit = data.records.commit(head["head_commit_id"])
        checkpoint = data.settlement.latest_checkpoint(stored_session)
        start_manifest = data.settlement.manifest("chain_start", chain_id)
        end_manifest = data.settlement.manifest("chain_end", chain_id)
        private_states = data.records.private_states(session["workflow_session_id"])
        objects = data.state.objects(session["workflow_session_id"])
        data_revision = data.state.variables(session["workflow_session_id"])["revision"]

    assert chain["status"] == "succeeded"
    assert all(item["status"] == "succeeded" for item in runs)
    assert outputs and {item["port_id"] for item in outputs} == {"output"}
    assert head["head_commit_id"] == final["head_commit_id"]
    assert commit["source"]["kind"] == "completed_execution"
    assert commit["source"]["chain_run_id"] == chain_id
    assert start_manifest["status"] == "prepared"
    assert end_manifest["status"] == "succeeded"
    assert checkpoint is not None
    assert private_states == final["private_states"]
    assert objects == final["objects"]
    assert data_revision == final["data_revision"]


def test_scheduling_and_begin_reads_do_not_bypass_execution_ports(service, monkeypatch):
    direct = []
    original = GraphRecordStore.get

    def tracked(self, kind, **identity):
        if kind == "chain_run":
            caller = inspect.currentframe().f_back.f_code.co_name
            if caller in ("_start_run", "_execute"):
                direct.append(caller)
        return original(self, kind, **identity)

    monkeypatch.setattr(GraphRecordStore, "get", tracked)
    doc = text_graph(service.registry)
    session = create(service, doc)
    final = graph_run(service, session)
    assert final["status"] == "succeeded", final["chains"]
    assert direct == []


def test_control_close_uses_execution_ports_and_persists_closed_execution(service, monkeypatch):
    calls = {}

    def track(name, method):
        def wrapper(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return method(*args, **kwargs)
        return wrapper

    for owner, name in (
        (ExecutionRecords, "put_chain"),
        (ExecutionRecords, "put_node_run"),
        (ExecutionRecords, "put_session"),
        (ExecutionRecords, "session"),
        (ExecutionSettlement, "commit"),
        (ExecutionSettlement, "save_manifest"),
        (ExecutionState, "variables"),
    ):
        monkeypatch.setattr(owner, name, track(
            f"{owner.__name__}.{name}", getattr(owner, name)))

    original_checkpoint = service._append_success_checkpoint
    failed = []

    def fail_checkpoint(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise OSError("injected checkpoint failure")
        return original_checkpoint(*args, **kwargs)

    monkeypatch.setattr(service, "_append_success_checkpoint", fail_checkpoint)
    doc = text_graph(service.registry)
    session = create(service, doc)
    started = service.start(
        session["workflow_session_id"], expected_revision=session["revision"],
        idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    pending = service.get_session(session["workflow_session_id"])
    assert pending["status"] == "archive_failed"
    assert failed == [True]
    calls.clear()

    closed = service.control(
        pending["workflow_session_id"], action="close",
        expected_revision=pending["revision"], idempotency_key=str(uuid4()))
    assert closed["status"] == "closed", closed["chains"]
    for name in (
        "ExecutionRecords.put_chain", "ExecutionRecords.put_session",
        "ExecutionRecords.session", "ExecutionSettlement.commit",
        "ExecutionSettlement.save_manifest", "ExecutionState.variables",
    ):
        assert calls.get(name, 0) > 0, name

    sid = session["workflow_session_id"]
    chain_id = pending["active_chain_run_id"]
    with service._data_management.read_execution(registry=service.registry) as data:
        stored = data.records.session(sid)
        chain = data.records.chain(chain_id)
        head = data.records.head(sid)
        commit = data.records.commit(head["head_commit_id"])
        manifest = data.settlement.manifest("chain_end", chain_id)
        checkpoint = data.settlement.latest_checkpoint(stored)
        outputs = data.records.outputs_for_chain(chain_id)
    assert stored["active_chain_run_id"] is None
    assert chain["status"] == "closed"
    assert commit["source"] == {"kind": "closed_execution", "chain_run_id": chain_id}
    assert manifest["status"] == "closed"
    assert checkpoint is None
    assert outputs and all(row["payload"] for row in outputs)


def test_retry_archive_uses_execution_ports(service, monkeypatch):
    calls = {}

    def track(name, method):
        def wrapper(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return method(*args, **kwargs)
        return wrapper

    for owner, name in (
        (ExecutionRecords, "put_chain"),
        (ExecutionRecords, "put_session"),
        (ExecutionSettlement, "commit"),
    ):
        monkeypatch.setattr(owner, name, track(
            f"{owner.__name__}.{name}", getattr(owner, name)))

    original_checkpoint = service._append_success_checkpoint
    failed = []

    def fail_checkpoint(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise OSError("injected checkpoint failure")
        return original_checkpoint(*args, **kwargs)

    monkeypatch.setattr(service, "_append_success_checkpoint", fail_checkpoint)
    doc = text_graph(service.registry)
    session = create(service, doc)
    started = service.start(
        session["workflow_session_id"], expected_revision=session["revision"],
        idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    pending = service.get_session(session["workflow_session_id"])
    assert pending["status"] == "archive_failed"
    calls.clear()
    retried = service.control(
        pending["workflow_session_id"], action="retry_archive",
        expected_revision=pending["revision"], idempotency_key=str(uuid4()))
    service.wait(retried["active_chain_run_id"])
    final = service.get_session(session["workflow_session_id"])
    assert final["status"] == "succeeded", final["chains"]
    for name in ("ExecutionRecords.put_chain", "ExecutionRecords.put_session",
                 "ExecutionSettlement.commit"):
        assert calls.get(name, 0) > 0, name


def test_failed_retry_capture_uses_the_execution_data_basis(service):
    def fail(config, inputs, context):
        raise ValueError("registered failed retry probe")

    service.registry.register(
        NodeDefinition(
            "test.failed-retry-probe", "1", "Failed retry probe", "Test", {},
            {"type": "object"},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), fail,
        failed_retry_validator=lambda config, evidence: {
            "classification": "retryable", "service_ref": None, "service_evidence": {},
        })
    source = node(service.registry, "test.failed-retry-probe", 1)
    output = node(service.registry, "tools.output", 2)
    doc = document([source, output], [edge(source, output, 1)])
    doc["package_lock"] = list(service.registry.execution_package_lock)
    initial = create(service, doc)
    started = service.start(
        initial["workflow_session_id"], expected_revision=initial["revision"],
        idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    chain_id = started["active_chain_run_id"]
    with service._data_management.read_execution(registry=service.registry) as data:
        chain = data.records.chain(chain_id)
        service._resource_frames[chain_id] = {source["node_binding_id"]: []}
        service._runtime_hosts[chain_id] = SimpleNamespace(
            contains=lambda owner: True,
            snapshot=lambda owner: {"status": "failed", "disposed": True,
                                    "disposal_error": None},
            release=lambda owner: None,
            active_handle_count=0,
        )
        result = service._capture_failed_retry(data, chain)
    assert result["allowed"] is True
    candidate = service._failed_retry_candidates[chain_id]
    assert candidate["basis"]["state"] == service.get_session(
        initial["workflow_session_id"])["data"]
    assert candidate["basis"]["definition"] == [
        initial["workflow_definition_id"], initial["definition_revision"]]


def test_private_state_boolean_replacement_is_not_silently_skipped(service):
    def toggle(config, inputs, context):
        assert context.private_read() == 1
        context.private_write(True)
        return {"output": text_content("toggled")}

    service.registry.register(NodeDefinition(
        "test.private-toggle", "1", "Private toggle", "Test", {},
        {"type": "object"},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        capabilities=("private:read", "private:write"),
        private_state_schema={"type": ["integer", "boolean"]},
        private_state_default=1,
    ), toggle)
    source = node(service.registry, "test.private-toggle", 1)
    output = node(service.registry, "tools.output", 2)
    doc = document([source, output], [edge(source, output, 1)])
    doc["package_lock"] = list(service.registry.execution_package_lock)
    initial = create(service, doc)
    final = graph_run(service, initial)
    assert final["status"] == "succeeded", final["chains"]
    assert final["private_states"][source["node_binding_id"]] is True

    with service._data_management.read_execution(registry=service.registry) as data:
        sid = initial["workflow_session_id"]
        stored_private = data.records.private_states(sid)[source["node_binding_id"]]
        head = data.records.head(sid)
        commit = data.records.commit(head["head_commit_id"])
        start_manifest = data.settlement.manifest("chain_start", final["selected_chain_run_id"])
        end_manifest = data.settlement.manifest("chain_end", final["selected_chain_run_id"])
        objects = data.state.objects(sid)
    assert stored_private is True
    assert commit["source"]["kind"] == "completed_execution"
    assert commit["source"]["chain_run_id"] == final["selected_chain_run_id"]
    assert start_manifest["status"] == "prepared"
    assert end_manifest["status"] == "succeeded"
    assert objects == {}


def test_final_settlement_fault_keeps_previously_accepted_node_outputs(tmp_path):
    from phase1_agent.graph_service import GraphWorkflowService
    from test_graph_service import create_package_registry, register_content_probe

    injected = []

    def fail_once(point):
        if point == "after_checkpoint_write" and not injected:
            injected.append(point)
            raise OSError("injected success checkpoint write failure")

    registry = create_package_registry().registry.detached()
    register_content_probe(registry)
    with closing(GraphWorkflowService(
            tmp_path / "accepted-prefix.sqlite", registry=registry,
            fault_injector=fail_once)) as service:
        doc = text_graph(service.registry)
        session = create(service, doc)
        started = service.start(
            session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        pending = service.get_session(session["workflow_session_id"])
        assert pending["status"] == "archive_failed"
        assert injected == ["after_checkpoint_write"]
        assert pending["head_commit_id"] == session["head_commit_id"]
        stored = service.get_run(session["workflow_session_id"], chain_id)
        assert stored["chain"]["diagnostic"]["reason_code"] == "checkpoint_commit_pending"
        assert stored["outputs"] and len(stored["outputs"]) == 3
        assert all(item["status"] == "succeeded" for item in stored["node_runs"])


def test_type_evidence_is_written_by_the_execution_adapter_and_rolls_back(
        service, monkeypatch):
    doc = text_graph(service.registry)
    session = create(service, doc)
    definitions = service.registry.data_types.definitions()
    assert len(definitions) >= 2
    first = definitions[0]
    with closing(service._store()) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        store._connection.execute(
            "DELETE FROM registered_type_contracts WHERE scope=? AND type_id=? AND schema_version=?",
            (first.scope, first.type_id, first.schema_version))
        store._connection.execute("COMMIT")
    started = service.start(
        session["workflow_session_id"], expected_revision=session["revision"],
        idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    with closing(service._store()) as store:
        assert _check(
            service, store,
            "SELECT 1 FROM registered_type_contracts WHERE scope=? AND type_id=? AND schema_version=?",
            (first.scope, first.type_id, first.schema_version)) is not None

    second = definitions[1]
    with closing(service._store()) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        store._connection.execute(
            "DELETE FROM registered_type_contracts WHERE scope=? AND type_id=? AND schema_version=?",
            (second.scope, second.type_id, second.schema_version))
        store._connection.execute("COMMIT")

    def reject(document, plan=None, *, registry):
        raise graph_error("test_rejected", "Rejected after type evidence", 409)

    monkeypatch.setattr(service, "_check_implementations", reject)
    with pytest.raises(ContractValidationError) as failure:
        service.start(
            session["workflow_session_id"], expected_revision=service.get_session(
                session["workflow_session_id"])["revision"],
            idempotency_key=str(uuid4()))
    assert failure.value.reason_code == "test_rejected"
    with closing(service._store()) as store:
        assert _check(
            service, store,
            "SELECT 1 FROM registered_type_contracts WHERE scope=? AND type_id=? AND schema_version=?",
            (second.scope, second.type_id, second.schema_version)) is None


def test_port_write_still_passes_through_bundle_validation(service):
    doc = text_graph(service.registry)
    session = create(service, doc)
    sid = session["workflow_session_id"]
    basis = service._capture_run_basis()
    run_id = str(uuid4())

    def change(repo):
        data = service._data_management.execution_transaction(repo, basis=basis)
        document = data.configuration.fixed_document(doc["workflow_definition_id"], 1)
        node = document["nodes"][0]
        data.records.put_node_run(graph_record(
            "node_run", profile="node", run_id=run_id, workflow_session_id=sid,
            node_binding_id=node["node_binding_id"], chain_run_id=str(uuid4()),
            status="prepared", revision=1, input_refs={}, input_values={},
            output_refs={}, reads=[], effects=[], config=node["config"],
            diagnostic=None, input_storage="values"))
        return run_id

    with pytest.raises(ContractValidationError) as failure:
        service._change("test.bundle", str(uuid4()), {}, change)
    assert failure.value.reason_code == "storage_contract_violation"
    with closing(service._store()) as store:
        assert _check(service, store,
                      "SELECT 1 FROM records WHERE record_type='node_run' AND json_extract(payload,'$.run_id')=?",
                      (run_id,)) is None


def test_object_type_check_reuses_the_same_type_evidence_adapter():
    calls = []

    class Evidence:
        def check(self, types, type_id, schema_version, scope, **options):
            calls.append((type_id, schema_version, scope, options))
            return {"digest": "test"}

    binding = SimpleNamespace(type_id="TEXT", schema_version=2)
    objects = SessionObjectStore(
        SimpleNamespace(_connection=None), None, type_evidence=Evidence())
    objects._check_type(binding, legacy=True)
    assert calls == [("TEXT", 2, "session", {
        "allow_legacy": True, "allow_missing": False})]
