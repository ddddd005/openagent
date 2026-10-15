"""Frontend request custody is exact, node-owned and never re-dispatches unknowns."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event, Thread
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.frontend_node_evidence import (
    FRONTEND_NODE_EVIDENCE_OPERATIONS, FrontendNodeEvidenceAdapter,
    frontend_node_evidence_owner, validate_frontend_node_evidence_request,
)
from phase1_agent.frontend_node_operations import FrontendNodeOperations
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_records import graph_error
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.request_evidence import RequestEvidenceService
from phase1_agent.storage import SqliteStore
from test_frontend_node_operations import prepared


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-evidence.sqlite")) as instance:
        yield instance


def patch_parameters(*, key="input-original", text="  original\r\n"):
    return {"text": text, "expected_edit_sequence": 1, "idempotency_key": key}


def original(bound, parameters):
    return {"owner": bound._identity(), "parameters": deepcopy(parameters)}


def snapshot(service):
    with closing(service._store()) as store:
        return {
            table: tuple(tuple(row) for row in store._connection.execute("SELECT * FROM " + table + " ORDER BY rowid"))
            for table in ("records", "workflow_current_configuration", "pending_commands", "idempotency")
        }


def records(service, bound, operation):
    private = FRONTEND_NODE_EVIDENCE_OPERATIONS[operation]
    owner_kind, owner_id = frontend_node_evidence_owner(private, {"owner": bound._identity()})
    return RequestEvidenceService(service.database).read_evidence(owner_kind, owner_id)


def test_private_evidence_is_not_a_generic_application_command(service):
    bound, _, _, _ = prepared(service)
    request = original(bound, patch_parameters())
    private = FRONTEND_NODE_EVIDENCE_OPERATIONS["patch_input"]
    assert private not in {entry.name for entry in COMMANDS}
    with pytest.raises(ContractValidationError):
        GraphApplication(service).command(private, request)
    assert read_graph_application_receipt(service.database, private, request)["reason_code"] == "unsupported_operation"
    kind, owner_id = frontend_node_evidence_owner(private, request)
    evidence = RequestEvidenceService(service.database)
    for owner_kind, owner in (("workflow-management", owner_id), (kind, "arbitrary-owner")):
        with pytest.raises(ContractValidationError) as caught:
            evidence.write_evidence(
                operation=private, idempotency_key="input-original",
                owner_kind=owner_kind, owner_id=owner, request=request)
        assert caught.value.reason_code == "request_evidence_conflict"
    assert evidence.read_evidence(kind, owner_id) == []


def test_execute_persists_original_then_verifies_actual_configuration_receipt(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    parameters = patch_parameters()
    caller_original = deepcopy(parameters)
    method = type(bound).patch_input

    def inspect(self, *args, **kwargs):
        durable = records(service, self, "patch_input")
        assert len(durable) == 1 and durable[0]["resolution"] == "pending"
        assert durable[0]["request"] == original(self, caller_original)
        parameters["text"] = "late caller edit"
        return method(self, *args, **kwargs)

    monkeypatch.setattr(type(bound), "patch_input", inspect)
    result = adapter.execute(bound, "patch_input", parameters)
    assert result["document"]["nodes"][0]["config"]["text"] == caller_original["text"]
    saved = records(service, bound, "patch_input")[0]
    assert saved["resolution"] == "verified"
    assert saved["request"] == original(bound, caller_original)
    assert adapter.pending(bound) == []
    before = snapshot(service)
    monkeypatch.setattr(type(bound), "patch_input", lambda *args, **kwargs: pytest.fail("re-dispatched"))
    assert adapter.execute(bound, "patch_input", caller_original) == result
    assert adapter.read_receipt(bound, "patch_input", saved["request"]) == result
    assert snapshot(service) == before


@pytest.mark.parametrize("outcome", ["confirmation_required", "derived"])
def test_real_configuration_confirmation_receipts_are_verified_without_hidden_dispatch(service, outcome):
    bound, graph, messages, _ = prepared(service)
    fixed = deepcopy(graph)
    fixed["nodes"][0]["configuration_policy"]["prompt_mode"] = "fixed"
    saved = service.update_definition(
        fixed, workflow_definition_id=fixed["workflow_definition_id"],
        expected_edit_sequence=1, idempotency_key="make-fixed")
    bound = FrontendNodeOperations(service).bind(**{
        **bound._identity(), "definition_revision": saved["definition_revision"]})
    service.create_session(bound.workflow_definition_id, bound.definition_revision, idempotency_key="effective-session")
    parameters = patch_parameters()
    parameters["expected_edit_sequence"] = saved["edit_sequence"]
    if outcome == "derived":
        parameters["confirm_derivation"] = True
    result = FrontendNodeEvidenceAdapter(service.database).execute(bound, "patch_input", parameters)
    assert result["outcome"] == outcome
    assert records(service, bound, "patch_input")[0]["resolution"] == "verified"


def test_unknown_after_actual_save_only_reads_original_then_explicitly_accepts_evidence(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    parameters = patch_parameters()
    method = type(bound).patch_input
    accepted = []

    def lose_result(self, **kwargs):
        accepted.append(method(self, **kwargs))
        raise OSError("response lost")

    monkeypatch.setattr(type(bound), "patch_input", lose_result)
    with pytest.raises(OSError, match="response lost"):
        adapter.execute(bound, "patch_input", parameters)
    pending = adapter.pending(bound)
    assert len(pending) == 1 and pending[0]["request"] == original(bound, parameters)
    before = snapshot(service)
    monkeypatch.setattr(type(bound), "patch_input", lambda *args, **kwargs: pytest.fail("unknown re-dispatched"))
    assert adapter.execute(bound, "patch_input", parameters) == accepted[0]
    assert adapter.read_receipt(bound, "patch_input", pending[0]["request"]) == accepted[0]
    assert snapshot(service) == before
    resolved = adapter.accept_receipt(bound, "patch_input", pending[0]["request"])
    assert resolved == accepted[0]
    assert records(service, bound, "patch_input")[0]["resolution"] == "verified"
    assert records(service, bound, "patch_input")[0]["request"] == pending[0]["request"]
    assert adapter.pending(bound) == []
    before_again = snapshot(service)
    assert adapter.accept_receipt(bound, "patch_input", pending[0]["request"]) == resolved
    assert snapshot(service) == before_again


def test_unknown_without_native_receipt_is_never_retried_or_accepted(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    parameters = patch_parameters()
    calls = []

    def interrupted(self, **kwargs):
        calls.append(kwargs)
        raise OSError("unknown dispatch")

    monkeypatch.setattr(type(bound), "patch_input", interrupted)
    with pytest.raises(OSError):
        adapter.execute(bound, "patch_input", parameters)
    before = snapshot(service)
    request = adapter.pending(bound)[0]["request"]
    assert adapter.execute(bound, "patch_input", parameters) is None
    assert adapter.read_receipt(bound, "patch_input", request) is None
    with pytest.raises(ContractValidationError) as caught:
        adapter.accept_receipt(bound, "patch_input", request)
    assert caught.value.reason_code == "frontend_node_receipt_unresolved"
    assert len(calls) == 1 and snapshot(service) == before


def test_unknown_input_does_not_block_pause_evidence_slot(service, monkeypatch):
    bound, _, _, _ = prepared(service, session=True)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    monkeypatch.setattr(type(bound), "patch_input", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("lost")))
    with pytest.raises(OSError):
        adapter.execute(bound, "patch_input", patch_parameters())
    pause_calls = []

    def unknown_pause(self, **kwargs):
        pause_calls.append(kwargs)
        return {"operation": "pause", "outcome": "unknown", "control_result": None,
                "failure_phase": "pause", "error": None}

    monkeypatch.setattr(type(bound), "pause", unknown_pause)
    result = adapter.execute(bound, "pause", {"expected_session_revision": 1, "idempotency_key": "pause-own-slot"})
    assert result["outcome"] == "unknown" and len(pause_calls) == 1
    assert {record["operation"] for record in adapter.pending(bound)} == {
        "frontend-node.patch-input", "frontend-node.pause"}


def test_known_rejection_archives_original_but_receipt_read_does_not_invent_failure(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    parameters = patch_parameters()
    calls = []

    def rejected(self, **kwargs):
        calls.append(kwargs)
        raise DiagnosticError("test_rejected", "Definite rejection")

    monkeypatch.setattr(type(bound), "patch_input", rejected)
    with pytest.raises(ContractValidationError, match="Definite rejection"):
        adapter.execute(bound, "patch_input", parameters)
    saved = records(service, bound, "patch_input")[0]
    assert saved["resolution"] == "archived" and saved["request"] == original(bound, parameters)
    before = snapshot(service)
    assert adapter.execute(bound, "patch_input", parameters) is None
    assert adapter.read_receipt(bound, "patch_input", saved["request"]) is None
    assert len(calls) == 1 and snapshot(service) == before


def test_actual_graph_error_diagnostic_archives_a_known_4xx_rejection(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    error = graph_error("stale_edit_sequence", "Workflow configuration changed", 409)
    assert error.diagnostics == [{
        "reason_code": "stale_edit_sequence", "message": "Workflow configuration changed",
    }]

    def rejected(self, **kwargs):
        raise error

    monkeypatch.setattr(type(bound), "patch_input", rejected)
    with pytest.raises(ContractValidationError) as caught:
        adapter.execute(bound, "patch_input", patch_parameters())
    assert caught.value is error
    assert adapter.pending(bound) == []
    assert records(service, bound, "patch_input")[0]["resolution"] == "archived"


@pytest.mark.parametrize("error", [
    ContractValidationError("unstructured validation error"),
    DiagnosticError("frontend_node_dispatch_unknown", "Response is unknown"),
    DiagnosticError("response_unavailable", "Response is unavailable", status=503),
    DiagnosticError("idempotency_conflict", "Original identity conflicts", status=409),
])
def test_unstructured_or_unknown_contract_error_preserves_pending(service, monkeypatch, error):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)

    def fail(self, **kwargs):
        raise error

    monkeypatch.setattr(type(bound), "patch_input", fail)
    with pytest.raises(ContractValidationError) as caught:
        adapter.execute(bound, "patch_input", patch_parameters())
    assert caught.value is error
    assert len(adapter.pending(bound)) == 1
    assert records(service, bound, "patch_input")[0]["resolution"] == "pending"


def test_start_created_session_then_rejected_start_archives_request_without_persisting_failure(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    parameters = {"expected_session_revision": None, "idempotency_key": "partial-start"}
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: (_ for _ in ()).throw(
        DiagnosticError("test_start_rejected", "Start rejected")))
    result = adapter.execute(bound, "start", parameters)
    assert result["outcome"] == "failed" and result["create_result"] is not None
    record = records(service, bound, "start")[0]
    assert record["resolution"] == "archived"
    reopened = FrontendNodeEvidenceAdapter(service.database)
    read = reopened.read_receipt(bound, "start", record["request"])
    assert read["outcome"] == "unknown" and read["create_result"] == result["create_result"]
    before = snapshot(service)
    assert reopened.execute(bound, "start", parameters) == read
    assert snapshot(service) == before


@pytest.mark.parametrize("overrides", [
    {"text": "different"}, {"expected_edit_sequence": 2}, {"confirm_derivation": False},
])
def test_same_key_different_original_evidence_is_rejected_before_dispatch(service, overrides, monkeypatch):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    parameters = patch_parameters()
    adapter.execute(bound, "patch_input", parameters)
    before = snapshot(service)
    monkeypatch.setattr(type(bound), "patch_input", lambda *args, **kwargs: pytest.fail("conflict dispatched"))
    with pytest.raises(ContractValidationError) as caught:
        adapter.execute(bound, "patch_input", {**parameters, **overrides})
    assert caught.value.reason_code == "request_evidence_conflict"
    assert snapshot(service) == before


def test_wrong_bound_owner_and_browser_supplied_owner_are_rejected(service):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    parameters = patch_parameters()
    adapter.execute(bound, "patch_input", parameters)
    request = original(bound, parameters)
    before = snapshot(service)
    foreign = replace(bound, node_binding_id=str(uuid4()))
    with pytest.raises(ContractValidationError):
        adapter.read_receipt(foreign, "patch_input", request)
    with pytest.raises(ContractValidationError):
        adapter.execute(bound, "patch_input", {**parameters, "owner_kind": "workflow-management"})
    assert snapshot(service) == before


def test_evidence_write_failure_cannot_dispatch_business(service):
    bound, _, _, _ = prepared(service)
    before = snapshot(service)

    def fault(point):
        if point == "after_request_evidence_write":
            raise OSError("evidence write failed")

    adapter = FrontendNodeEvidenceAdapter(
        service.database, store_factory=lambda: SqliteStore(service.database, fault_injector=fault))
    with pytest.raises(OSError, match="evidence write failed"):
        adapter.execute(bound, "patch_input", patch_parameters())
    assert snapshot(service) == before


def test_acceptance_metadata_failure_keeps_pending_without_replaying_business(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    method = type(bound).patch_input

    def lose_result(self, **kwargs):
        method(self, **kwargs)
        raise OSError("result lost")

    monkeypatch.setattr(type(bound), "patch_input", lose_result)
    with pytest.raises(OSError):
        adapter.execute(bound, "patch_input", patch_parameters())
    request = adapter.pending(bound)[0]["request"]
    result = adapter.read_receipt(bound, "patch_input", request)
    before = snapshot(service)
    monkeypatch.setattr(type(bound), "patch_input", lambda *args, **kwargs: pytest.fail("business replayed"))
    armed = True

    def fault(point):
        if armed and point == "before_commit":
            raise OSError("acceptance metadata failed")

    failing = FrontendNodeEvidenceAdapter(
        service.database, store_factory=lambda: SqliteStore(service.database, fault_injector=fault))
    with pytest.raises(OSError, match="acceptance metadata failed"):
        failing.accept_receipt(bound, "patch_input", request)
    assert snapshot(service) == before
    assert len(adapter.pending(bound)) == 1
    armed = False
    assert failing.accept_receipt(bound, "patch_input", request) == result
    assert adapter.pending(bound) == []
    assert snapshot(service)["records"] == before["records"]
    assert snapshot(service)["workflow_current_configuration"] == before["workflow_current_configuration"]


def test_concurrent_same_start_request_only_observes_original_plan(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    adapter = FrontendNodeEvidenceAdapter(service.database)
    parameters = {"expected_session_revision": None, "idempotency_key": "concurrent-start"}
    entered, release = Event(), Event()
    original_create = service.create_session
    calls, results, errors = [], [], []

    def blocked_create(*args, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(10), "Concurrent start creation gate timed out"
        return original_create(*args, **kwargs)

    def invoke():
        try:
            results.append(adapter.execute(bound, "start", parameters))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(service, "create_session", blocked_create)
    first = Thread(target=invoke)
    first.start()
    try:
        assert entered.wait(5), "First frontend start did not reach session creation"
        observed = adapter.execute(bound, "start", parameters)
        assert observed["outcome"] == "unknown" and observed["create_result"] is None
        assert len(calls) == 1
    finally:
        release.set()
        first.join(timeout=10)
    assert not first.is_alive() and errors == [] and len(results) == 1
    assert results[0]["outcome"] == "succeeded"
    service.wait(results[0]["start_result"]["active_chain_run_id"])
    assert records(service, bound, "start")[0]["resolution"] == "verified"
    before = snapshot(service)
    assert adapter.execute(bound, "start", parameters) == results[0]
    assert snapshot(service) == before and len(calls) == 1


@pytest.mark.parametrize(("operation", "mutate"), [
    ("patch_input", lambda value: value["parameters"].update(text=None)),
    ("patch_input", lambda value: value["parameters"].update(text=False)),
    ("patch_input", lambda value: value["parameters"].update(text="x" * 1_000_001)),
    ("patch_input", lambda value: value["parameters"].update(expected_edit_sequence=True)),
    ("patch_input", lambda value: value["parameters"].update(expected_edit_sequence=-1)),
    ("patch_input", lambda value: value["parameters"].update(confirm_derivation=None)),
    ("patch_input", lambda value: value["parameters"].update(confirm_derivation=1)),
    ("patch_input", lambda value: value["parameters"].update(target_node_id=str(uuid4()))),
    ("patch_input", lambda value: value["owner"].update(definition_revision=True)),
    ("patch_input", lambda value: value["owner"].update(session_id="not-a-session")),
    ("patch_input", lambda value: value.update(extra=True)),
    ("start", lambda value: value["parameters"].update(expected_session_revision=1)),
    ("start", lambda value: value["parameters"].update(inputs={"text": "external"})),
    ("start", lambda value: value["parameters"].pop("expected_session_revision")),
    ("pause", lambda value: value["owner"].update(session_id=None)),
    ("pause", lambda value: value["parameters"].update(expected_session_revision=True)),
    ("pause", lambda value: value["parameters"].update(action="close")),
])
def test_private_validator_rejects_wrong_fields_formats_and_foreign_control(operation, mutate):
    owner = {"workflow_definition_id": str(uuid4()), "definition_revision": 1,
             "node_binding_id": str(uuid4()), "session_id": str(uuid4()) if operation == "pause" else None}
    parameters = patch_parameters() if operation == "patch_input" else {
        "expected_session_revision": 1 if operation == "pause" else None, "idempotency_key": "strict"}
    request = {"owner": owner, "parameters": parameters}
    key = parameters["idempotency_key"]
    mutate(request)
    with pytest.raises(ContractValidationError):
        validate_frontend_node_evidence_request(FRONTEND_NODE_EVIDENCE_OPERATIONS[operation], key, request)
