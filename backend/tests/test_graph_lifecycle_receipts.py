"""Actual lifecycle acceptance remains readable after its target is deleted."""

from contextlib import closing
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphWorkflowService

from test_frontend_node_runtime import _create, _graph, _save
from test_graph_application_receipt_custody import raw_database
from test_graph_receipt_http import post, serve_receipts


OPERATIONS = ("definition.archive", "definition.delete", "session.archive", "session.delete")


def _accepted_then_deleted(database, operation):
    def forbidden(*args, **kwargs):
        pytest.fail("Lifecycle receipt test cannot call models or provider discovery")

    with closing(GraphWorkflowService(
        database, public_model_factory=forbidden, provider_discovery_client_factory=forbidden,
    )) as service:
        graph, _, _ = _graph(service, bound=True)
        graph = _save(service, graph)
        session = _create(service, graph)
        kind, action = operation.split(".")
        parameters = {
            "identity" if kind == "definition" else "session_id":
                graph["workflow_definition_id"] if kind == "definition" else session["workflow_session_id"],
            "expected_revision": 1, "idempotency_key": str(uuid4()),
        }
        accepted = GraphApplication(service).command(operation, parameters)
        if action == "archive":
            GraphApplication(service).command(kind + ".delete", {
                **parameters, "idempotency_key": str(uuid4()),
            })
        return parameters, accepted


@pytest.mark.parametrize("operation", OPERATIONS)
def test_actual_lifecycle_receipt_cold_reads_deleted_target_without_dispatch(
        tmp_path, monkeypatch, operation):
    database = tmp_path / "lifecycle.sqlite"
    parameters, accepted = _accepted_then_deleted(database, operation)
    before = raw_database(database)
    records_columns, records_rows = before[2]["records"]
    kind = operation.split(".")[0]
    record_type = "workflow_session" if kind == "session" else "workflow_definition_revision"
    type_index = records_columns.index("record_type")
    assert not any(row[type_index] == record_type for row in records_rows)
    expected = read_graph_application_receipt(database, operation, parameters)
    assert expected["outcome"] == "matched", expected
    assert expected["receipt"] == accepted["receipt"] and expected["result"] == accepted["result"]
    with serve_receipts(database, monkeypatch) as (host, port):
        original = {"operation": operation, "parameters": parameters}
        assert post(port, "/api/graph/receipts/read", original) == (200, expected)
        assert post(port, "/api/graph/queries", {
            "operation": "receipt.read", "parameters": original,
        }) == (200, expected)
        for field, value in (
            ("expected_revision", parameters["expected_revision"] + 1),
            ("session_id" if kind == "session" else "identity", str(uuid4())),
        ):
            status, rejected = post(port, "/api/graph/receipts/read", {
                "operation": operation, "parameters": {**parameters, field: value},
            })
            assert status == 200 and rejected["outcome"] == "unresolved"
            assert rejected["reason_code"] == "request_mismatch"
        assert post(port, "/api/graph/consumer/receipts/read", original)[1] == {
            "schema_version": 1, "kind": "workflow.application-receipt-read",
            "outcome": "unresolved", "reason_code": "application_scope_denied",
        }
        assert host._graph is None
    assert raw_database(database) == before


@pytest.mark.parametrize("operation", OPERATIONS)
def test_lifecycle_reader_rejects_malformed_flags_targets_revision_and_envelope(tmp_path, operation):
    database = tmp_path / "lifecycle-invalid.sqlite"
    parameters, accepted = _accepted_then_deleted(database, operation)
    target = "workflow_session_id" if operation.startswith("session.") else "workflow_definition_id"
    variants = [
        {"schema_version": True}, {"schema_version": 2}, {"kind": "workflow.session"},
        {"revision": True}, {"revision": parameters["expected_revision"] + 1},
        {"archived": 1}, {"deleted": 1}, {"archived": True, "deleted": True},
        {"archived": False, "deleted": False}, {target: str(uuid4())},
        {"workflow_definition_id": "not-an-identity"}, {"extra": "not part of the contract"},
    ]
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        row = connection.execute(
            "SELECT result_payload FROM idempotency WHERE operation=? AND key=?",
            ("graph." + operation, parameters["idempotency_key"]),
        ).fetchone()
        original = row[0]
        for patch in variants:
            payload = loads_strict(original)
            payload[0]["graph_result"].update(patch)
            connection.execute(
                "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
                (canonical_bytes(payload).decode("utf-8"), "graph." + operation, parameters["idempotency_key"]),
            )
            snapshot = raw_database(database)
            result = read_graph_application_receipt(database, operation, parameters)
            assert result["outcome"] == "unresolved" and result["reason_code"] == "receipt_invalid", patch
            assert raw_database(database) == snapshot
        payload = loads_strict(original)
        del payload[0]["graph_result"]["archived"]
        connection.execute(
            "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
            (canonical_bytes(payload).decode("utf-8"), "graph." + operation, parameters["idempotency_key"]),
        )
        assert read_graph_application_receipt(database, operation, parameters)["outcome"] == "unresolved"
        connection.execute(
            "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
            (original, "graph." + operation, parameters["idempotency_key"]),
        )
    assert read_graph_application_receipt(database, operation, parameters)["result"] == accepted["result"]
