"""Local drafts never become executable facts or consumer management access."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_drafts import MAX_DRAFT_BYTES
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.storage import SqliteStore, STORAGE_VERSION

from test_graph_service import create, service, text_graph


def address(scope="management", kind="form", workflow=None, session=None, key="editor:v1"):
    return {"scope": scope, "kind": kind, "key": key,
            "workflow_definition_id": workflow, "workflow_session_id": session}


def save(app, target, value, expected=0, key=None, version=1):
    request = {"address": target, "format_version": version, "value": value,
               "expected_sequence": expected, "idempotency_key": key or str(uuid4())}
    operation = "consumer.draft.save" if target["scope"] == "consumer" else "draft.save"
    return app.command(operation, request), request


def client(workflow, session=None, pending=None):
    return {"schema_version": 1, "kind": "workflow.chat-client", "workflow_definition_id": workflow,
            "workflow_session_id": session, "pending": pending}


def consumer_fixture(service):
    document = text_graph(service.registry)
    session = create(service, document)
    return document["workflow_definition_id"], session["workflow_session_id"]


def test_draft_roundtrip_invalid_editor_text_and_frozen_receipt(service):
    app = GraphApplication(service)
    target = address()
    value = {"raw": "{unclosed", "number": "-", "text": "key is a normal word",
             "parameters": {"key": "ordinary data", "credential_ref": "env:GEMINI_API_KEY"}}
    original = deepcopy(value)
    accepted, request = save(app, target, value, key="draft-first")
    record = accepted["result"]
    assert value == original and record["value"] == original
    assert record["sequence"] == 1 and not record["deleted"]
    assert accepted["receipt"]["accepted"] == {
        "draft_address": target, "draft_sequence": 1, "draft_format_version": 1, "deleted": False}
    assert accepted["receipt"]["target"] == {}
    assert app.query("draft.read", {"address": target}) == record
    save(app, target, {"text": "newer"}, expected=1)
    assert app.command("draft.save", request) == accepted
    receipt = read_graph_application_receipt(service.database, "draft.save", request)
    assert receipt["outcome"] == "matched" and receipt["result"] == record
    assert receipt["receipt"] == accepted["receipt"]
    assert read_graph_application_receipt(service.database, "draft.save", request, scope="consumer")[
        "reason_code"] == "application_scope_denied"


def test_cas_and_idempotency_conflict_leave_newer_record(service):
    app = GraphApplication(service)
    target = address()
    first, request = save(app, target, {"raw": "first"}, key="fixed-key")
    newer, _ = save(app, target, {"raw": "second"}, expected=1)
    with pytest.raises(ContractValidationError) as stale:
        save(app, target, {"raw": "stale"}, expected=1)
    assert stale.value.reason_code == "stale_draft_sequence"
    with pytest.raises(ContractValidationError) as conflict:
        app.command("draft.save", {**request, "value": "different"})
    assert conflict.value.reason_code == "idempotency_conflict"
    assert app.query("draft.read", {"address": target}) == newer["result"]
    assert first["result"]["sequence"] == 1


def test_null_is_cas_tombstone_not_sequence_reset(service):
    app = GraphApplication(service)
    target = address()
    assert app.query("draft.read", {"address": target}) is None
    saved, _ = save(app, target, "draft")
    deleted, request = save(app, target, None, expected=1)
    assert deleted["result"]["deleted"] and deleted["result"]["sequence"] == 2
    assert app.query("draft.read", {"address": target}) == deleted["result"]
    assert app.query("draft.list")["records"] == [deleted["result"]]
    restored, _ = save(app, target, "restored", expected=2)
    assert restored["result"]["sequence"] == 3 and not restored["result"]["deleted"]
    assert read_graph_application_receipt(service.database, "draft.save", request)["result"] == deleted["result"]


@pytest.mark.parametrize("mutation", [
    lambda value: {**value, "extra": "no"},
    lambda value: {**value, "scope": "consumer"},
    lambda value: {**value, "kind": "chat-input"},
    lambda value: {**value, "kind": []},
    lambda value: {**value, "key": ""},
    lambda value: {**value, "key": " padded "},
    lambda value: {**value, "key": "x" * 513},
    lambda value: {**value, "key": "line\nbreak"},
    lambda value: {**value, "workflow_session_id": str(uuid4())},
    lambda value: {**value, "workflow_definition_id": "not-a-uuid"},
])
def test_address_rejects_unknown_fields_and_invalid_owners(service, mutation):
    app = GraphApplication(service)
    with pytest.raises(ContractValidationError) as failure:
        save(app, mutation(address()), "raw")
    assert failure.value.reason_code == "invalid_draft_address"
    assert app.query("draft.list")["records"] == []


@pytest.mark.parametrize("field", ["api_key", "api-key", "apikey", "authorization", "private_key",
                                  "password", "access_token", "credential_value"])
def test_structured_secrets_do_not_enter_draft_or_receipt(service, field):
    app = GraphApplication(service)
    with pytest.raises(ContractValidationError) as failure:
        save(app, address(), {"form": {field: "secret-test-value"}})
    assert failure.value.reason_code == "draft_secret_disallowed"
    assert app.query("draft.list")["records"] == []


@pytest.mark.parametrize("value", [float("nan"), {1: "not JSON"}, {"nested": b"raw"}, object()])
def test_nonjson_drafts_are_rejected(service, value):
    with pytest.raises(ContractValidationError) as failure:
        save(GraphApplication(service), address(), value)
    assert failure.value.reason_code == "invalid_request"


def test_size_and_sequence_format_validation(service):
    app = GraphApplication(service)
    with pytest.raises(ContractValidationError) as capacity:
        save(app, address(), "x" * MAX_DRAFT_BYTES)
    assert capacity.value.reason_code == "draft_capacity_exceeded"
    for expected in (-1, True, 2**53):
        with pytest.raises(ContractValidationError):
            save(app, address(), "text", expected=expected)
    for version in (0, True, 2**53):
        with pytest.raises(ContractValidationError):
            save(app, address(), "text", version=version)


def test_consumer_scope_is_separate_and_does_not_mutate_execution(service, monkeypatch):
    workflow, session = consumer_fixture(service)
    app = GraphApplication(service)
    consumer = app.for_consumer()
    target = address("consumer", "chat-input", workflow, session)
    before = service.get_session(session)
    saved, request = save(consumer, target, {"message": "unsubmitted", "number": "-"})
    assert consumer.query("consumer.draft.read", {"address": target}) == saved["result"]
    assert app.query("draft.list")["records"] == []
    assert consumer.query("consumer.draft.list", {"workflow_definition_id": workflow})["records"] == [saved["result"]]
    assert service.get_session(session) == before
    with pytest.raises(ContractValidationError) as denied:
        consumer.command("draft.save", request)
    assert denied.value.reason_code == "application_scope_denied"
    with pytest.raises(ContractValidationError) as address_denied:
        consumer.query("consumer.draft.read", {"address": address()})
    assert address_denied.value.reason_code == "invalid_draft_address"
    with pytest.raises(ContractValidationError):
        app.query("draft.read", {"address": target})
    monkeypatch.setattr(service, "_store", lambda: pytest.fail("Receipt read constructed a mutable store"))
    receipt = read_graph_application_receipt(service.database, "consumer.draft.save", request, scope="consumer")
    assert receipt["outcome"] == "matched" and receipt["result"] == saved["result"]
    assert receipt["receipt"]["operation_scope"] == "consumer"


def test_consumer_create_intent_without_session_is_durable_only(service):
    workflow, session = consumer_fixture(service)
    consumer = GraphApplication(service, scope="consumer")
    command = {"action": "create", "workflow_session_id": None, "path": "/api/graph/consumer/sessions",
               "body": {"workflow_definition_id": workflow, "definition_revision": 1,
                        "idempotency_key": str(uuid4())}}
    target = address("consumer", "chat-client", workflow)
    before = service.list_sessions(workflow)
    result, request = save(consumer, target, client(workflow, session, command))
    assert service.list_sessions(workflow) == before
    assert result["result"]["value"]["pending"] == command
    assert read_graph_application_receipt(service.database, "consumer.draft.save", request, scope="consumer")[
        "outcome"] == "matched"


def test_consumer_start_intent_uses_exact_session_and_never_runs(service):
    workflow, session = consumer_fixture(service)
    consumer = GraphApplication(service, scope="consumer")
    command = {"action": "start", "workflow_session_id": session,
               "path": f"/api/graph/sessions/{session}/consumer/runs",
               "body": {"expected_revision": 1, "idempotency_key": str(uuid4()), "inputs": {"text": "hello"}}}
    target = address("consumer", "chat-client", workflow, session)
    accepted, _ = save(consumer, target, client(workflow, session, command))
    assert not service.get_session(session)["chains"]
    assert consumer.query("consumer.draft.read", {"address": target}) == accepted["result"]
    invalid = deepcopy(command)
    invalid["path"] = f"/api/graph/sessions/{session}/runs"
    with pytest.raises(ContractValidationError) as failure:
        save(consumer, target, client(workflow, session, invalid), expected=1)
    assert failure.value.reason_code == "invalid_consumer_draft"


def test_consumer_cannot_store_management_or_wrong_workflow_commands(service):
    workflow, session = consumer_fixture(service)
    consumer = GraphApplication(service, scope="consumer")
    target = address("consumer", "chat-client", workflow)
    invalids = [
        {"action": "save", "workflow_session_id": session, "path": "/api/graph/definitions",
         "body": {"expected_revision": 1, "idempotency_key": str(uuid4())}},
        {"action": "create", "workflow_session_id": None, "path": "/api/graph/consumer/sessions",
         "body": {"workflow_definition_id": str(uuid4()), "definition_revision": 1, "idempotency_key": str(uuid4())}},
        {"action": "start", "workflow_session_id": str(uuid4()),
         "path": f"/api/graph/sessions/{session}/consumer/runs",
         "body": {"expected_revision": 1, "idempotency_key": str(uuid4()), "inputs": {}}},
    ]
    for command in invalids:
        with pytest.raises(ContractValidationError) as failure:
            save(consumer, target, client(workflow, session, command))
        assert failure.value.reason_code == "invalid_consumer_draft"
    assert consumer.query("consumer.draft.list", {"workflow_definition_id": workflow})["records"] == []


def test_consumer_owner_checks_workflow_session_and_selected_client_session(service):
    workflow, session = consumer_fixture(service)
    other_workflow, other_session = consumer_fixture(service)
    consumer = GraphApplication(service, scope="consumer")
    for target, value in [
        (address("consumer", "chat-input", workflow, other_session), "not yours"),
        (address("consumer", "chat-client", workflow), client(workflow, other_session)),
    ]:
        with pytest.raises(ContractValidationError) as mismatch:
            save(consumer, target, value)
        assert mismatch.value.reason_code == "draft_owner_mismatch"
    with pytest.raises(ContractValidationError):
        consumer.query("consumer.draft.list", {"kind": "chat-input"})
    with pytest.raises(ContractValidationError):
        consumer.query("consumer.draft.list", {"workflow_definition_id": workflow, "kind": "workbench"})
    save(consumer, address("consumer", "chat-input", workflow, session), "correct")
    assert consumer.query("consumer.draft.list", {"workflow_definition_id": other_workflow})["records"] == []


def test_consumer_list_rejects_damaged_row_coordinates_instead_of_leaking_management(service):
    workflow, session = consumer_fixture(service)
    consumer = GraphApplication(service, scope="consumer")
    target = address("consumer", "chat-input", workflow, session)
    saved, _ = save(consumer, target, "consumer text")
    damaged = deepcopy(saved["result"])
    damaged["address"] = address("management", "form")
    damaged["value"] = {"private": "must not leak"}
    with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
        connection.execute("UPDATE local_editor_drafts SET payload=?", (canonical_bytes(damaged).decode(),))
    with pytest.raises(ContractValidationError) as failure:
        consumer.query("consumer.draft.list", {"workflow_definition_id": workflow})
    assert failure.value.reason_code == "storage_contract_violation"
    with pytest.raises(ContractValidationError):
        consumer.query("consumer.draft.read", {"address": target})


def test_draft_transaction_rolls_back_record_and_application_origin(service, monkeypatch):
    app = GraphApplication(service)
    target = address()
    before, _ = save(app, target, "original")
    with closing(sqlite3.connect(service.database)) as connection:
        receipts = connection.execute("SELECT * FROM idempotency ORDER BY operation,key").fetchall()
    def fail(point):
        if point == "before_commit":
            raise RuntimeError("disk test failure")
    monkeypatch.setattr(service, "_fault_injector", fail)
    with pytest.raises(RuntimeError):
        save(app, target, "must roll back", expected=1)
    assert app.query("draft.read", {"address": target}) == before["result"]
    with closing(sqlite3.connect(service.database)) as connection:
        assert connection.execute("SELECT * FROM idempotency ORDER BY operation,key").fetchall() == receipts


def test_corrupt_draft_or_receipt_is_not_adopted_or_overwritten(service):
    app = GraphApplication(service)
    target = address()
    saved, request = save(app, target, "original", key="corrupt-receipt")
    with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
        tampered = {**saved["result"], "sequence": 9}
        connection.execute(
            "UPDATE idempotency SET result_payload=? WHERE operation='graph.draft.save' AND key=?",
            (canonical_bytes([{"graph_result": tampered}]).decode(), request["idempotency_key"]))
    assert read_graph_application_receipt(service.database, "draft.save", request)["reason_code"] == "receipt_invalid"
    with pytest.raises(ContractValidationError):
        app.command("draft.save", request)
    with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
        connection.execute("UPDATE local_editor_drafts SET payload=?", ('{"invalid":"retained"}',))
    with pytest.raises(ContractValidationError) as failure:
        app.query("draft.read", {"address": target})
    assert failure.value.reason_code == "storage_contract_violation"
    with pytest.raises(ContractValidationError):
        save(app, target, "replacement", expected=1)
    with closing(sqlite3.connect(service.database)) as connection:
        assert connection.execute("SELECT payload FROM local_editor_drafts").fetchone()[0] == '{"invalid":"retained"}'


def test_v15_to_v16_migration_is_additive_without_retirement(tmp_path, monkeypatch):
    path = tmp_path / "existing-v15.sqlite"
    with closing(SqliteStore(path)):
        pass
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("DROP TABLE local_editor_drafts")
        connection.execute("INSERT INTO records VALUES(?,?,?,?,?)",
                           ("unknown.extension", "original", '{"retained":"extension bytes"}', 1, "time"))
        connection.execute("PRAGMA user_version=15")
        before = connection.execute("SELECT * FROM records").fetchall()
    monkeypatch.setattr("phase1_agent.storage_retirement.remove_retired_test_data",
                        lambda connection: pytest.fail("v15 data entered retired cleanup"))
    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == STORAGE_VERSION == 16
        assert [tuple(row) for row in store._connection.execute("SELECT * FROM records")] == before
        assert store._connection.execute("SELECT count(*) FROM local_editor_drafts").fetchone()[0] == 0


def test_draft_migration_rolls_back_ddl_and_version_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "migration-failure.sqlite"
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("PRAGMA user_version=15")
    from phase1_agent import graph_drafts
    initialize = graph_drafts.initialize_draft_tables
    def fail(connection):
        initialize(connection)
        raise RuntimeError("migration failure")
    monkeypatch.setattr(graph_drafts, "initialize_draft_tables", fail)
    with pytest.raises(ContractValidationError):
        SqliteStore(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 15
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_named_http_dispatch_uses_application_receipt(service):
    app = GraphApplication(service)
    request = {"address": address(), "format_version": 1, "value": "unapplied",
               "expected_sequence": 0, "idempotency_key": "http-draft"}
    status, accepted = dispatch_graph(app, "POST", "/api/graph/commands",
                                     {"operation": "draft.save", "parameters": request})
    assert status == 200 and accepted["result"]["sequence"] == 1
    status, read = dispatch_graph(app, "POST", "/api/graph/queries",
                                {"operation": "draft.read", "parameters": {"address": address()}})
    assert status == 200 and read == accepted["result"]
