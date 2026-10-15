"""Operation planning and native dispatch work without any frontend node."""

from contextlib import closing
from copy import deepcopy
from threading import Event, Thread
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.content_contracts import text_content
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.workflow_operations import WorkflowOperations, read_native_receipt

from test_graph_service import document, edge, node, text_graph


@pytest.fixture
def prepared(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "workflow-operations.sqlite")) as service:
        graph = text_graph(service.registry)
        graph["schema_version"] = 3
        for entry in graph["nodes"]:
            entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        saved = service.update_definition(
            graph, workflow_definition_id=graph["workflow_definition_id"],
            expected_edit_sequence=0, idempotency_key="save")
        assert all(entry["component_id"] != "frontend.node" for entry in saved["document"]["nodes"])
        yield service, WorkflowOperations(service), {
            "workflow_definition_id": saved["workflow_definition_id"],
            "definition_revision": saved["definition_revision"],
        }


def _wait(service, result):
    if result["start_result"] is not None:
        service.wait(result["start_result"]["active_chain_run_id"])


def _lock_is_free(service):
    acquired = Event()

    def take_lock():
        with service._lock:
            acquired.set()

    worker = Thread(target=take_lock)
    worker.start()
    try:
        assert acquired.wait(2), "Operation dispatch kept the service lock"
    finally:
        worker.join(timeout=2)


def test_plan_accepts_trusted_adapter_identity_and_replay_never_reauthorizes(prepared, monkeypatch):
    service, operations, identity = prepared
    calls = []
    request = {**identity, "node_binding_id": str(uuid4()), "session_id": None,
               "expected_session_revision": None}
    parameters = {
        **identity, "idempotency_key": "adapter-start",
        "receipt_operation": "test.adapter.start", "receipt_request": request,
        "create_key": "original-create", "dispatch_key": "original-start",
        "verify": lambda repo: calls.append(True),
    }
    plan, accepted = operations.plan_start(**parameters)
    assert accepted and calls == [True]
    assert plan == {
        "create_request": identity, "create_key": "original-create",
        "dispatch_key": "original-start", "dispatch_request": None,
    }
    result = operations.execute_start(plan, accepted)
    assert result["outcome"] == "succeeded"
    _wait(service, result)
    monkeypatch.setattr(service, "_document", lambda *args: pytest.fail("Replay read current graph"))
    parameters["verify"] = lambda repo: pytest.fail("Replay reauthorized current binding")
    replay, accepted = operations.plan_start(**parameters)
    assert replay == plan and not accepted
    assert operations.execute_start(replay, accepted) == result
    assert read_native_receipt(
        service, "test.adapter.start", "adapter-start", request) == plan


def test_create_original_receipt_supplies_start_basis_and_dispatch_releases_lock(prepared, monkeypatch):
    service, operations, identity = prepared
    native_create, native_start = service.create_session, service.start
    calls = []

    def create(*args, **kwargs):
        _lock_is_free(service)
        result = native_create(*args, **kwargs)
        calls.append(("create", result["workflow_session_id"]))
        return {**result, "workflow_session_id": str(uuid4()), "revision": 9999}

    def start(session_id, **kwargs):
        _lock_is_free(service)
        calls.append(("start", session_id))
        assert kwargs["expected_revision"] == 1
        return native_start(session_id, **kwargs)

    monkeypatch.setattr(service, "create_session", create)
    monkeypatch.setattr(service, "start", start)
    result = operations.start(**identity, idempotency_key="create-basis")
    assert result["outcome"] == "succeeded"
    assert calls[0][1] == calls[1][1] == result["create_result"]["workflow_session_id"]
    _wait(service, result)


@pytest.mark.parametrize("phase", ("create", "start"))
def test_rejected_native_phase_reports_actual_partial_result_and_never_retries(
    prepared, monkeypatch, phase,
):
    service, operations, identity = prepared
    calls = []

    def reject(*args, **kwargs):
        calls.append(True)
        raise DiagnosticError("test_operation_rejected", "Controlled rejection", status=409)

    monkeypatch.setattr(service, "create_session" if phase == "create" else "start", reject)
    parameters = {**identity, "idempotency_key": "partial"}
    first = operations.start(**parameters)
    assert first["outcome"] == "failed" and first["failure_phase"] == phase
    assert first["error"]["reason_code"] == "test_operation_rejected"
    assert (first["create_result"] is None) == (phase == "create")
    assert first["start_result"] is None
    replay = operations.start(**parameters)
    assert replay["outcome"] == "unknown" and replay["failure_phase"] == phase
    assert replay["create_result"] == first["create_result"]
    assert calls == [True]


def test_unknown_native_failure_never_exposes_exception_or_repeats_dispatch(prepared, monkeypatch):
    service, operations, identity = prepared
    calls = []

    def unknown(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("private detail")

    monkeypatch.setattr(service, "create_session", unknown)
    parameters = {**identity, "idempotency_key": "unknown"}
    first = operations.start(**parameters)
    assert first["outcome"] == "unknown"
    assert first["error"] == {"reason_code": "workflow_operation_dispatch_unknown"}
    assert operations.start(**parameters)["outcome"] == "unknown"
    assert calls == [True]


def test_same_key_concurrent_start_observes_recorded_plan_without_dispatch(prepared, monkeypatch):
    service, operations, identity = prepared
    entered, release = Event(), Event()
    native_create = service.create_session
    results, errors, calls = [], [], []

    def blocked(*args, **kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(10)
        return native_create(*args, **kwargs)

    def run():
        try:
            results.append(operations.start(**identity, idempotency_key="concurrent"))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(service, "create_session", blocked)
    worker = Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        observed = operations.start(**identity, idempotency_key="concurrent")
        assert observed == {
            "operation": "start", "outcome": "unknown", "create_result": None,
            "start_result": None, "failure_phase": "create", "error": None,
        }
        assert calls == [True]
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive() and errors == []
    assert results[0]["outcome"] == "succeeded" and calls == [True]
    _wait(service, results[0])


def test_session_identity_and_original_request_cannot_change(prepared):
    service, operations, identity = prepared
    session = service.create_session(**identity, idempotency_key="create")
    with pytest.raises(ContractValidationError) as denied:
        operations.start(
            **{**identity, "workflow_definition_id": str(uuid4())},
            session_id=session["workflow_session_id"], expected_session_revision=1,
            idempotency_key="other-workflow")
    assert denied.value.reason_code in ("not_found", "workflow_operation_session_mismatch")
    parameters = {
        **identity, "session_id": session["workflow_session_id"],
        "expected_session_revision": 1, "idempotency_key": "existing",
    }
    result = operations.start(**parameters)
    assert result["create_result"] is None and result["outcome"] == "succeeded"
    _wait(service, result)
    with pytest.raises(ContractValidationError) as conflict:
        operations.start(**{**parameters, "expected_session_revision": 2})
    assert conflict.value.reason_code == "idempotency_conflict"


def test_native_receipt_rejects_different_original_request_without_dispatch(prepared):
    service, operations, identity = prepared
    plan, _ = operations.plan_start(**identity, idempotency_key="receipt")
    request = {**identity, "session_id": None, "expected_session_revision": None}
    assert read_native_receipt(
        service, "graph.workflow-operation.start", "receipt", request) == plan
    with pytest.raises(ContractValidationError) as conflict:
        read_native_receipt(
            service, "graph.workflow-operation.start", "receipt", {**request, "session_id": str(uuid4())})
    assert conflict.value.reason_code == "idempotency_conflict"


def test_trusted_verification_failure_does_not_record_plan(prepared):
    service, operations, identity = prepared
    before = deepcopy(service.list_sessions(identity["workflow_definition_id"]))

    def reject(repo):
        raise DiagnosticError("adapter_binding_denied", "Controlled denial", status=403)

    with pytest.raises(ContractValidationError):
        operations.plan_start(
            **identity, idempotency_key="denied", verify=reject)
    request = {**identity, "session_id": None, "expected_session_revision": None}
    assert read_native_receipt(service, "graph.workflow-operation.start", "denied", request) is None
    assert service.list_sessions(identity["workflow_definition_id"]) == before


@pytest.mark.parametrize("existing", (False, True))
def test_current_input_values_are_frozen_and_forwarded_without_frontend_node(
    prepared, existing, monkeypatch,
):
    service, operations, _ = prepared
    source = node(service.registry, "tools.current-input", 21, input_name="message")
    output = node(service.registry, "tools.output", 22)
    graph = document([source, output], [edge(source, output, 21)])
    graph["schema_version"] = 3
    for entry in graph["nodes"]:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key="current-save")
    parameters = {
        "workflow_definition_id": saved["workflow_definition_id"],
        "definition_revision": saved["definition_revision"],
        "inputs": {"message": text_content("frozen input")}, "idempotency_key": "current-start",
    }
    if existing:
        session = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"], idempotency_key="current-create")
        parameters.update(
            session_id=session["workflow_session_id"], expected_session_revision=session["revision"])
    plan, accepted = operations.plan_start(**parameters)
    parameters["inputs"]["message"]["text"] = "caller changed later"
    result = operations.execute_start(plan, accepted)
    assert result["outcome"] == "succeeded", result
    _wait(service, result)
    session_id = result["start_result"]["workflow_session_id"]
    final = service.get_session(session_id)
    assert final["status"] == "succeeded"
    assert [entry["payload"]["text"] for entry in final["outputs"]] == ["frozen input", "frozen input"]
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: pytest.fail("Receipt read dispatched"))
    assert operations.read_start_result(plan) == result
    with pytest.raises(ContractValidationError) as conflict:
        operations.plan_start(**parameters)
    assert conflict.value.reason_code == "idempotency_conflict"
