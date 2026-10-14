"""Named application custody for the sole checkpoint-fork command."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, content_digest, loads_strict
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_identity import (
    APPLICATION_IDENTITY_OPERATION, application_command_identity, application_origin_key,
    native_command_request,
)
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_receipts import read_graph_application_receipt
from test_graph_application_receipt_custody import raw_database
from test_graph_service import create, run, service, text_graph


OPERATION = "checkpoint.fork"
NATIVE_OPERATION = "graph.checkpoint.fork"
RESULT_FIELDS = {
    "target_session_id", "target_checkpoint_id", "target_commit_id", "target_workflow_ref_id",
}


def _completed_checkpoint(service):
    document = text_graph(service.registry)
    created = create(service, document)
    completed = run(service, created)
    with closing(sqlite3.connect(service.database)) as connection:
        row = connection.execute(
            "SELECT checkpoint_id FROM workflow_checkpoints "
            "WHERE workflow_session_id=? ORDER BY rowid DESC LIMIT 1",
            (completed["workflow_session_id"],),
        ).fetchone()
    assert row is not None
    return document, completed, row[0]


def _request(session, checkpoint_id, key=None):
    return {
        "session_id": session["workflow_session_id"],
        "checkpoint_id": checkpoint_id,
        "expected_session_revision": session["revision"],
        "expected_data_revision": session["data_revision"],
        "expected_head_revision": session["head_revision"],
        "idempotency_key": key or str(uuid4()),
    }


def _assert_fork_result(packet, request):
    result = packet["result"]
    assert type(result) is dict and set(result) == RESULT_FIELDS
    assert all(type(result[field]) is str for field in RESULT_FIELDS)
    assert result["target_session_id"] != request["session_id"]
    receipt = packet["receipt"]
    assert receipt["operation"] == OPERATION
    assert receipt["operation_scope"] == "management"
    assert receipt["idempotency_key"] == request["idempotency_key"]
    assert receipt["target"] == {"session_id": request["session_id"]}
    assert receipt["accepted"] == result
    return result


def test_checkpoint_fork_is_management_only_and_uses_existing_named_routes(service):
    document = text_graph(service.registry)
    session = create(service, document)
    checkpoint_id = str(uuid4())
    application = GraphApplication(service)
    consumer = application.for_consumer()
    spec = next(row for row in application.describe()["commands"] if row["name"] == OPERATION)
    assert spec == {
        "name": OPERATION, "kind": "command", "scope": "management",
        "required_fields": ["checkpoint_id", "expected_data_revision", "expected_head_revision",
                            "expected_session_revision", "idempotency_key", "session_id"],
        "optional_fields": [], "idempotency": "service_receipt",
        "idempotency_key_format": "opaque", "http_status": 201,
    }
    assert OPERATION not in {row["name"] for row in consumer.describe()["commands"]}

    request = _request(session, checkpoint_id)
    before = raw_database(service.database)
    with pytest.raises(ContractValidationError) as denied:
        consumer.command(OPERATION, request)
    assert denied.value.reason_code == "application_scope_denied"
    with pytest.raises(ContractValidationError) as missing:
        application.command(OPERATION, {key: value for key, value in request.items()
                                        if key != "checkpoint_id"})
    assert missing.value.reason_code == "invalid_request"
    for invalid in (
        {**request, "checkpoint_id": "not-a-uuid"},
        {**request, "expected_session_revision": 0},
        {**request, "expected_data_revision": -1},
    ):
        with pytest.raises(ContractValidationError) as invalid_field:
            application.command(OPERATION, invalid)
        assert invalid_field.value.reason_code == "invalid_request"
    with pytest.raises(ContractValidationError) as no_alias:
        dispatch_graph(service, "POST",
                       f"/api/graph/sessions/{session['workflow_session_id']}/checkpoint/fork", {})
    assert no_alias.value.reason_code == "not_found"
    with pytest.raises(ContractValidationError) as consumer_http:
        dispatch_graph(service, "POST", "/api/graph/consumer/commands", {
            "operation": OPERATION, "parameters": request,
        })
    assert consumer_http.value.reason_code == "application_scope_denied"
    assert raw_database(service.database) == before
    assert document["workflow_definition_id"] == session["workflow_definition_id"]


def test_checkpoint_fork_native_identity_maps_only_the_source_session_and_exact_body():
    session_id, checkpoint_id, key = str(uuid4()), str(uuid4()), "checkpoint-fork-native-key"
    request = {
        "session_id": session_id, "checkpoint_id": checkpoint_id,
        "expected_session_revision": 8, "expected_data_revision": 3,
        "expected_head_revision": 4, "idempotency_key": key,
    }
    native_request = {
        "session": session_id, "checkpoint_id": checkpoint_id,
        "expected_session_revision": 8, "expected_data_revision": 3,
        "expected_head_revision": 4,
    }
    native = native_command_request(OPERATION, request)
    assert native == {
        "table": "idempotency", "operation": NATIVE_OPERATION, "key": key,
        "request_digest": "workflow-op-v1:" + content_digest(native_request).rsplit(":", 1)[1],
    }
    identity = application_command_identity(OPERATION, "management", request)
    assert identity["target"] == {"session_id": session_id}
    assert identity["native_receipt"] == native


def test_named_checkpoint_fork_preserves_native_identity_and_replays_one_result(service):
    document, session, checkpoint_id = _completed_checkpoint(service)
    request = _request(session, checkpoint_id, "checkpoint-fork-exact-key")
    expected_native_request = {
        "session": request["session_id"],
        "checkpoint_id": request["checkpoint_id"],
        "expected_session_revision": request["expected_session_revision"],
        "expected_data_revision": request["expected_data_revision"],
        "expected_head_revision": request["expected_head_revision"],
    }
    native = native_command_request(OPERATION, request)
    assert native == {
        "table": "idempotency", "operation": NATIVE_OPERATION, "key": request["idempotency_key"],
        "request_digest": "workflow-op-v1:" + content_digest(expected_native_request).rsplit(":", 1)[1],
    }
    identity = application_command_identity(OPERATION, "management", request)
    assert identity["target"] == {"session_id": request["session_id"]}
    assert identity["native_receipt"] == native

    before_sessions = service.list_sessions(document["workflow_definition_id"])
    status, first = dispatch_graph(service, "POST", "/api/graph/commands", {
        "operation": OPERATION, "parameters": request,
    })
    assert status == 201
    result = _assert_fork_result(first, request)
    status, replay = dispatch_graph(service, "POST", "/api/graph/commands", {
        "operation": OPERATION, "parameters": deepcopy(request),
    })
    assert status == 201 and replay == first
    after_sessions = service.list_sessions(document["workflow_definition_id"])
    assert len(after_sessions) == len(before_sessions) + 1
    assert sum(row["workflow_session_id"] == result["target_session_id"] for row in after_sessions) == 1

    origin_key = application_origin_key(identity)
    with closing(sqlite3.connect(service.database)) as connection:
        native_rows = connection.execute(
            "SELECT digest,request_digest,result_payload FROM idempotency WHERE operation=? AND key=?",
            (NATIVE_OPERATION, request["idempotency_key"]),
        ).fetchall()
        origin_rows = connection.execute(
            "SELECT digest,request_digest,result_payload FROM idempotency WHERE operation=? AND key=?",
            (APPLICATION_IDENTITY_OPERATION, origin_key),
        ).fetchall()
    assert len(native_rows) == len(origin_rows) == 1
    assert native_rows[0][0] == native_rows[0][1] == native["request_digest"]
    assert origin_rows[0][0] == origin_rows[0][1] == "workflow-op-v1:" + identity["request_sha256"]
    stored_identity = loads_strict(origin_rows[0][2])[0]["application_identity"]
    assert stored_identity == identity


def test_checkpoint_fork_receipt_reads_exact_four_ids_without_current_records(service):
    _, session, checkpoint_id = _completed_checkpoint(service)
    request = _request(session, checkpoint_id, "checkpoint-fork-receipt-key")
    status, packet = dispatch_graph(service, "POST", "/api/graph/commands", {
        "operation": OPERATION, "parameters": request,
    })
    assert status == 201
    result = _assert_fork_result(packet, request)
    before = raw_database(service.database)

    # The original native receipt and application origin are sufficient; current
    # sessions, commits, refs, checkpoints, and their parent closure are not read.
    with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
        connection.execute("DELETE FROM workflow_checkpoints")
        connection.execute("DELETE FROM records")
    without_business_records = raw_database(service.database)
    assert not without_business_records[2]["records"][1]
    assert not without_business_records[2]["workflow_checkpoints"][1]

    matched = read_graph_application_receipt(service.database, OPERATION, request)
    assert matched["outcome"] == "matched"
    assert matched["result"] == result
    assert matched["receipt"]["target"] == {"session_id": request["session_id"]}
    assert matched["receipt"]["accepted"] == result
    status, named = dispatch_graph(service, "POST", "/api/graph/queries", {
        "operation": "receipt.read",
        "parameters": {"operation": OPERATION, "parameters": request},
    })
    assert status == 200 and named == matched
    consumer = read_graph_application_receipt(service.database, OPERATION, request, scope="consumer")
    assert consumer["outcome"] == "unresolved"
    assert consumer["reason_code"] == "application_scope_denied"

    changed_body = {**request, "expected_head_revision": request["expected_head_revision"] + 1}
    assert read_graph_application_receipt(service.database, OPERATION, changed_body)["reason_code"] == "request_mismatch"
    changed_owner = {**request, "session_id": str(uuid4())}
    assert read_graph_application_receipt(service.database, OPERATION, changed_owner)["reason_code"] == "request_mismatch"
    assert raw_database(service.database) == without_business_records
    assert before != without_business_records


def test_checkpoint_fork_receipt_rejects_extra_or_unrelated_result_ids(service):
    _, session, checkpoint_id = _completed_checkpoint(service)
    request = _request(session, checkpoint_id, "checkpoint-fork-strict-result")
    packet = GraphApplication(service).command(OPERATION, request)
    _assert_fork_result(packet, request)
    native = application_command_identity(OPERATION, "management", request)["native_receipt"]

    with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
        row = connection.execute(
            "SELECT result_payload FROM idempotency WHERE operation=? AND key=?",
            (native["operation"], native["key"]),
        ).fetchone()
        original = loads_strict(row[0])
        for defect in ("extra", "invalid_uuid", "same_owner"):
            payload = deepcopy(original)
            result = payload[0]["graph_result"]
            if defect == "extra":
                result["source_session_id"] = request["session_id"]
            elif defect == "invalid_uuid":
                result["target_checkpoint_id"] = "not-a-uuid"
            else:
                result["target_session_id"] = request["session_id"]
            connection.execute(
                "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
                (canonical_bytes(payload).decode("utf-8"), native["operation"], native["key"]),
            )
            receipt = read_graph_application_receipt(service.database, OPERATION, request)
            assert receipt["outcome"] == "unresolved"
            assert receipt["reason_code"] == "receipt_invalid"
        connection.execute(
            "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
            (canonical_bytes(original).decode("utf-8"), native["operation"], native["key"]),
        )


def test_checkpoint_fork_failure_before_commit_leaves_no_target_or_receipts(service):
    document, session, checkpoint_id = _completed_checkpoint(service)
    request = _request(session, checkpoint_id, "checkpoint-fork-rollback-key")
    before = raw_database(service.database)

    def fail_before_commit(point):
        if point == "before_commit":
            raise RuntimeError("controlled checkpoint-fork rollback")

    service._fault_injector = fail_before_commit
    try:
        with pytest.raises(RuntimeError, match="controlled checkpoint-fork rollback"):
            dispatch_graph(service, "POST", "/api/graph/commands", {
                "operation": OPERATION, "parameters": request,
            })
    finally:
        service._fault_injector = None

    assert raw_database(service.database) == before
    unresolved = read_graph_application_receipt(service.database, OPERATION, request)
    assert unresolved["outcome"] == "unresolved"
    assert unresolved["reason_code"] == "application_identity_missing"
    assert len(service.list_sessions(document["workflow_definition_id"])) == 1
