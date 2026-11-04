"""S1-01b: durable in-transaction arbitration of one unknown configuration request.

A user-initiated termination is arbitrated in one SQLite write transaction: a
committed business receipt returns the formal result (no re-execution), while a
request without receipt gets a durable abandonment fact.  Every later execution
of that exact native request is refused by the existing atomic command entry.
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import text_graph


UPDATE = "definition.update"
UPDATE_OWNER = "workflow-configuration"
COPY = "definition.copy"
COPY_OWNER = "workflow-configuration-copy"


def _editable(service, graph):
    graph["schema_version"] = 3
    graph.setdefault("package_lock", list(service.registry.execution_package_lock))
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    return graph


@pytest.fixture
def service(tmp_path):
    instance = GraphWorkflowService(
        tmp_path / "abandon.sqlite", registry=create_package_registry().registry.detached())
    try:
        yield instance
    finally:
        instance.close()


def _app(service):
    return GraphApplication(service)


def _graph(service):
    return _editable(service, text_graph(service.registry))


def _update_request(graph, key, sequence=0):
    return {"workflow_definition_id": graph["workflow_definition_id"],
            "document": deepcopy(graph), "expected_edit_sequence": sequence,
            "idempotency_key": key}


def _write_evidence(app, operation, identity, owner_kind, request):
    return app.command("request-evidence.write", {
        "operation": operation, "idempotency_key": request["idempotency_key"],
        "owner_kind": owner_kind, "owner_id": identity,
        "request": deepcopy(request)})["result"]


def _abandon(app, identity, operation, target_key, target_digest, key):
    return app.command("request.abandon", {
        "workflow_definition_id": identity, "target_operation": operation,
        "target_idempotency_key": target_key, "target_request_digest": target_digest,
        "idempotency_key": key})


def _resolutions(app, owner_kind, identity):
    return [row["resolution"] for row in app.query(
        "request-evidence.read", {"owner_kind": owner_kind, "owner_id": identity})]


def test_uncommitted_request_is_abandoned_and_late_request_is_refused(service):
    app = _app(service)
    graph = _graph(service)
    identity = graph["workflow_definition_id"]
    request = _update_request(graph, "business-key")
    evidence = _write_evidence(app, UPDATE, identity, UPDATE_OWNER, request)

    arbitration = _abandon(app, identity, UPDATE, "business-key",
                           evidence["request_digest"], "abandon-key")["result"]
    assert (arbitration["outcome"], arbitration["evidence_resolution"]) == ("abandoned", "archived")
    assert arbitration["result"] is None
    assert arbitration["workflow_definition_id"] == identity
    assert _resolutions(app, UPDATE_OWNER, identity) == ["archived"]

    with pytest.raises(ContractValidationError) as late:
        app.command(UPDATE, request)
    assert (late.value.reason_code, late.value.status_code) == ("request_abandoned", 409)
    with closing(service._store()) as store:
        assert store.read_receipt("graph.definition.update", "business-key") is None


def test_committed_request_returns_the_formal_result_without_reexecution(service):
    app = _app(service)
    graph = _graph(service)
    identity = graph["workflow_definition_id"]
    request = _update_request(graph, "business-key")
    evidence = _write_evidence(app, UPDATE, identity, UPDATE_OWNER, request)
    accepted = app.command(UPDATE, request)
    assert accepted["result"]["outcome"] == "saved"

    arbitration = _abandon(app, identity, UPDATE, "business-key",
                           evidence["request_digest"], "abandon-key")["result"]
    assert (arbitration["outcome"], arbitration["evidence_resolution"]) == ("committed", "verified")
    assert arbitration["result"] == accepted["result"]
    assert _resolutions(app, UPDATE_OWNER, identity) == ["verified"]

    with closing(service._store()) as store:
        receipts = store._connection.execute(
            "SELECT COUNT(*) FROM idempotency WHERE operation='graph.definition.update'"
        ).fetchone()[0]
    assert receipts == 1
    # Replaying the arbitration command returns the same formal result.
    replay = _abandon(app, identity, UPDATE, "business-key",
                      evidence["request_digest"], "abandon-key")
    assert replay["result"] == arbitration


def test_wrong_target_digest_is_refused_without_archiving(service):
    app = _app(service)
    graph = _graph(service)
    identity = graph["workflow_definition_id"]
    request = _update_request(graph, "business-key")
    _write_evidence(app, UPDATE, identity, UPDATE_OWNER, request)

    with pytest.raises(ContractValidationError) as mismatch:
        _abandon(app, identity, UPDATE, "business-key", "0" * 64, "abandon-key")
    assert (mismatch.value.reason_code, mismatch.value.status_code) == (
        "request_evidence_conflict", 409)
    assert _resolutions(app, UPDATE_OWNER, identity) == ["pending"]
    # The original request was not terminated and still executes normally.
    assert app.command(UPDATE, request)["result"]["outcome"] == "saved"


def test_missing_evidence_cannot_be_arbitrated(service):
    app = _app(service)
    graph = _graph(service)
    identity = graph["workflow_definition_id"]
    with pytest.raises(ContractValidationError) as unavailable:
        _abandon(app, identity, UPDATE, "unknown-key", "a" * 64, "abandon-key")
    assert (unavailable.value.reason_code, unavailable.value.status_code) == (
        "request_arbitration_unavailable", 409)


def test_arbitration_key_replay_conflicts_on_a_different_request(service):
    app = _app(service)
    graph = _graph(service)
    identity = graph["workflow_definition_id"]
    request = _update_request(graph, "business-key")
    evidence = _write_evidence(app, UPDATE, identity, UPDATE_OWNER, request)
    first = _abandon(app, identity, UPDATE, "business-key",
                     evidence["request_digest"], "abandon-key")
    assert first["result"]["outcome"] == "abandoned"
    assert _abandon(app, identity, UPDATE, "business-key",
                    evidence["request_digest"], "abandon-key") == first
    with pytest.raises(ContractValidationError) as conflict:
        _abandon(app, str(uuid4()), UPDATE, "business-key",
                 evidence["request_digest"], "abandon-key")
    assert conflict.value.reason_code == "idempotency_conflict"


def test_copy_request_can_be_arbitrated_before_it_runs(service):
    app = _app(service)
    source, target = str(uuid4()), str(uuid4())
    request = {"source_workflow_definition_id": source,
               "expected_source_definition_revision": 1,
               "expected_source_edit_sequence": 1,
               "target_workflow_definition_id": target,
               "target_name": "Copied workflow",
               "idempotency_key": "copy-key"}
    evidence = _write_evidence(app, COPY, source, COPY_OWNER, request)
    arbitration = _abandon(app, source, COPY, "copy-key",
                           evidence["request_digest"], "abandon-copy")["result"]
    assert (arbitration["outcome"], arbitration["evidence_resolution"]) == ("abandoned", "archived")
    with pytest.raises(ContractValidationError) as late:
        app.command(COPY, request)
    assert late.value.reason_code == "request_abandoned"
    assert _resolutions(app, COPY_OWNER, source) == ["archived"]


def test_arbitration_stays_available_after_package_publication_failure(service, monkeypatch):
    app = _app(service)
    graph = _graph(service)
    identity = graph["workflow_definition_id"]
    request = _update_request(graph, "business-key")
    evidence = _write_evidence(app, UPDATE, identity, UPDATE_OWNER, request)

    # Recovery operations are exempt from the package-publication gate so an
    # unknown request can still be arbitrated; ordinary saves stay blocked.
    monkeypatch.setattr(service, "_publication_failure", {
        "selection_committed": True, "publication_completed": False, "enabled_packages": {}})
    with pytest.raises(ContractValidationError) as blocked:
        app.command(UPDATE, request)
    assert (blocked.value.reason_code, blocked.value.status_code) == (
        "package_publication_failed", 503)

    arbitration = _abandon(app, identity, UPDATE, "business-key",
                           evidence["request_digest"], "abandon-key")["result"]
    assert (arbitration["outcome"], arbitration["evidence_resolution"]) == ("abandoned", "archived")
    assert _resolutions(app, UPDATE_OWNER, identity) == ["archived"]
    # With publication healthy again the abandoned request stays terminated.
    monkeypatch.setattr(service, "_publication_failure", None)
    with pytest.raises(ContractValidationError) as late:
        app.command(UPDATE, request)
    assert late.value.reason_code == "request_abandoned"
