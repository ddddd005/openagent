"""Actual persistence stages retain completed Agent work without redriving it."""

from copy import deepcopy
import sqlite3

import pytest

from phase1_agent.contract_errors import DiagnosticError
from phase1_agent.result_acceptance import retained_acceptance_allowed
from phase1_agent.runtime_fact_store import RuntimeFactStore
from test_result_acceptance_isolation_q16 import (
    _start, _Counters, _sqlite_read, _write_observation,
)
from phase1_agent.graph_service import GraphWorkflowService
from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import AgentTransportFixture, graph


def _setup(tmp_path, name):
    fixture = AgentTransportFixture(batch=True)
    service = GraphWorkflowService(
        tmp_path / (name + ".sqlite"), public_model_factory=fixture.factory)
    ModelDatabaseFixture.write(service, 1)
    document = graph(service)
    agent_node_id = next(row["node_binding_id"] for row in document["nodes"]
                         if row["component_id"] == "agents.execute")
    return service, fixture, create(service, document), agent_node_id


def _work_counts(service, counters, fixture):
    with sqlite3.connect(service.database) as connection:
        tools = connection.execute(
            "SELECT COUNT(*) FROM workflow_runtime_facts "
            "WHERE json_extract(payload,'$.payload.kind')='tool_dispatch'").fetchone()[0]
    return {**counters.of(fixture), "tool_dispatches": tools}


@pytest.mark.parametrize("point", [
    "after_runtime_fact_write", "before_runtime_fact_commit", "after_runtime_fact_commit",
])
def test_coded_terminal_fact_persistence_rollback_retains_exact_result(tmp_path, monkeypatch, point):
    service, fixture, initial, agent_node_id = _setup(tmp_path, point)
    counters = _Counters(monkeypatch)
    original = service._accept_executor_fact
    envelopes, injected = [], []

    def fact(envelope):
        if envelope["payload"].get("kind") != "agent_result":
            return original(envelope)
        envelopes.append(deepcopy(envelope))
        def fail(current):
            if current == point and not injected:
                injected.append(True)
                raise DiagnosticError("storage_contract_violation", "synthetic persistence failure", status=500)
        service._fault_injector = fail
        try:
            return original(envelope)
        finally:
            service._fault_injector = None

    monkeypatch.setattr(service, "_accept_executor_fact", fact)
    try:
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        pending = service.get_session(sid)
        assert pending["status"] == "archive_failed"
        assert pending["available_actions"] == ["retry_acceptance", "close"]
        assert pending["chains"][-1]["diagnostic"]["failure_stage"] == "fact_acceptance"
        before = _sqlite_read(service)
        assert before["terminal_agent_result_rows"] == (1 if point == "after_runtime_fact_commit" else 0)
        counts = _work_counts(service, counters, fixture)
        assert counts["tool_dispatches"] == 3
        resumed = service.control(
            sid, action="retry_acceptance", expected_revision=pending["revision"],
            idempotency_key="retry-" + point)
        service.wait(resumed["active_chain_run_id"])
        assert service.get_session(sid)["status"] == "succeeded"
        after = _sqlite_read(service)
        assert after["terminal_agent_result_rows"] == 1
        assert _work_counts(service, counters, fixture) == counts
        assert envelopes[0] == envelopes[1]
        _write_observation(tmp_path, point, {
            "counts_before_retry": counts, "counts_after_retry": _work_counts(service, counters, fixture),
            "before": before, "after": after, "envelopes_equal": envelopes[0] == envelopes[1]})
    finally:
        service.close()


@pytest.mark.parametrize("kind", ["node_commit", "round_checkpoint"])
def test_coded_settlement_failure_reuses_completed_agent_work(tmp_path, monkeypatch, kind):
    service, fixture, initial, agent_node_id = _setup(tmp_path, kind)
    counters = _Counters(monkeypatch)
    original_event = service._node_event
    failed = []
    if kind == "node_commit":
        def event(sid, chain_id, value):
            if value.get("event") != "succeeded" or value.get("node_binding_id") != agent_node_id:
                return original_event(sid, chain_id, value)
            def fail(point):
                if point == "before_commit" and not failed:
                    failed.append(True)
                    raise DiagnosticError("storage_contract_violation", "synthetic node commit failure", status=500)
            service._fault_injector = fail
            try:
                return original_event(sid, chain_id, value)
            finally:
                service._fault_injector = None
        monkeypatch.setattr(service, "_node_event", event)
    else:
        def fail(point):
            if point == "after_checkpoint_write" and not failed:
                failed.append(True)
                raise DiagnosticError("storage_contract_violation", "synthetic checkpoint failure", status=500)
        service._fault_injector = fail
    try:
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        pending = service.get_session(sid)
        assert failed
        assert pending["status"] == "archive_failed"
        expected_action = "retry_acceptance" if kind == "node_commit" else "retry_archive"
        assert pending["available_actions"] == [expected_action, "close"]
        assert pending["chains"][-1]["diagnostic"]["failure_stage"] == (
            "node_acceptance" if kind == "node_commit" else "round_acceptance")
        before = _sqlite_read(service)
        assert before["terminal_agent_result_rows"] == 1
        counts = _work_counts(service, counters, fixture)
        assert counts["tool_dispatches"] == 3
        service._fault_injector = None
        resumed = service.control(
            sid, action=expected_action, expected_revision=pending["revision"],
            idempotency_key="retry-" + kind)
        service.wait(resumed["active_chain_run_id"])
        assert service.get_session(sid)["status"] == "succeeded"
        assert _work_counts(service, counters, fixture) == counts
        after = _sqlite_read(service)
        assert after["runtime_fact_ids"] == before["runtime_fact_ids"]
        _write_observation(tmp_path, kind, {
            "counts_before_retry": counts, "counts_after_retry": _work_counts(service, counters, fixture),
            "before": before, "after": after})
    finally:
        service.close()


def test_same_storage_code_without_transaction_is_not_persistence_authority(tmp_path):
    connection = sqlite3.connect(tmp_path / "no-transaction.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        from types import SimpleNamespace
        store = RuntimeFactStore(SimpleNamespace(_connection=connection))
        with pytest.raises(Exception) as raised:
            store.accept(owner={}, stream_id="executor:test", fact={})
        error = raised.value
        assert error.reason_code == "storage_contract_violation"
        assert not retained_acceptance_allowed(
            error, stage="fact_acceptance", definite_result=True)
        assert not hasattr(error, "_result_persistence_stage")
    finally:
        connection.close()


def test_retry_declaration_rejection_preserves_exact_retained_result(tmp_path, monkeypatch):
    from test_workflow_run_order import _reject_background
    from phase1_agent.contract_errors import ContractValidationError
    from test_graph_application_receipt_custody import raw_database
    service, fixture, initial, agent_node_id = _setup(tmp_path, "retry-declaration")
    original = service._accept_executor_fact
    failed = []

    def fact(envelope):
        if envelope["payload"].get("kind") == "agent_result" and not failed:
            failed.append(True)
            raise OSError("synthetic terminal fact storage failure")
        return original(envelope)

    monkeypatch.setattr(service, "_accept_executor_fact", fact)
    try:
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        pending = service.get_session(sid)
        assert pending["status"] == "archive_failed"
        records = raw_database(service.database)
        candidate = service._acceptance_candidates[chain_id]
        basis = service._execution_registries[chain_id]
        counts = len(fixture.calls)
        _reject_background(monkeypatch)
        with pytest.raises(ContractValidationError, match="declaration and bindings differ"):
            service.control(sid, action="retry_acceptance", expected_revision=pending["revision"],
                            idempotency_key="retry-bad-declaration")
        assert service.get_session(sid) == pending
        assert raw_database(service.database) == records
        assert service._acceptance_candidates[chain_id] is candidate
        assert service._execution_registries[chain_id] is basis
        assert chain_id not in service._resuming
        assert len(fixture.calls) == counts
    finally:
        service.close()


@pytest.mark.parametrize("projection", ["project_generated_messages", "validate_agent_receipts"])
def test_repeated_projection_failure_retains_stage_and_completed_work(tmp_path, monkeypatch, projection):
    import phase1_agent.agent_executor as agent_module
    service, fixture, initial, agent_node_id = _setup(tmp_path, "repeated-" + projection)
    counters = _Counters(monkeypatch)
    original = getattr(agent_module, projection)
    attempts = []

    def fail_twice(*args, **kwargs):
        attempts.append(True)
        if len(attempts) <= 2:
            raise ZeroDivisionError("synthetic repeated projection failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_module, projection, fail_twice)
    try:
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        pending = service.get_session(sid)
        before = _sqlite_read(service)
        counts = _work_counts(service, counters, fixture)
        assert counts["tool_dispatches"] == 3
        assert before["terminal_agent_result_rows"] == 1
        observations = []
        for attempt in range(2):
            diagnostic = pending["chains"][-1]["diagnostic"]
            assert pending["status"] == "archive_failed"
            assert pending["available_actions"] == ["retry_acceptance", "close"]
            assert diagnostic["failure_stage"] == "result_projection"
            assert diagnostic["responsibility"] == "projection"
            assert diagnostic["definite_result"] is True
            assert _work_counts(service, counters, fixture) == counts
            current = _sqlite_read(service)
            assert current["runtime_fact_ids"] == before["runtime_fact_ids"]
            assert current["terminal_agent_result_rows"] == 1
            observations.append({"diagnostic": diagnostic, "counts": counts, "sqlite": current})
            resumed = service.control(
                sid, action="retry_acceptance", expected_revision=pending["revision"],
                idempotency_key=f"retry-projection-{attempt}")
            service.wait(resumed["active_chain_run_id"])
            pending = service.get_session(sid)
        assert pending["status"] == "succeeded"
        assert _work_counts(service, counters, fixture) == counts
        assert _sqlite_read(service)["runtime_fact_ids"] == before["runtime_fact_ids"]
        assert len(attempts) == 3
        _write_observation(tmp_path, "repeated-projection", {
            "projection": projection, "failures": observations,
            "final_counts": _work_counts(service, counters, fixture), "final_sqlite": _sqlite_read(service)})
    finally:
        service.close()
