"""Retired graph projections and writes reject while exact frozen receipts remain readable."""

from copy import deepcopy
import http.client
import json
from types import SimpleNamespace
import sqlite3
from contextlib import closing
from threading import Thread
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import RETIRED_RECEIPT_COMMANDS
from phase1_agent.graph_application_identity import (
    APPLICATION_IDENTITY_OPERATION, application_command_digest, application_command_identity,
    application_origin_key,
)
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.server import create_server
from phase1_agent.storage import SqliteStore
from test_graph_application_receipt_custody import raw_database
from test_graph_service import create, run, service, text_graph


def _insert_historical_data_receipt(path, parameters, result):
    identity = application_command_identity("session.data.update", "management", parameters)
    native = identity["native_receipt"]
    digest = "workflow-op-v1:" + identity["request_sha256"]
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute(
            "INSERT INTO idempotency "
            "(operation,key,digest,result_refs,result_payload,request_digest) VALUES(?,?,?,?,?,?)",
            (APPLICATION_IDENTITY_OPERATION, application_origin_key(identity), digest, "[]",
             canonical_bytes([{"application_identity": identity}]).decode(), digest),
        )
        connection.execute(
            "INSERT INTO idempotency "
            "(operation,key,digest,result_refs,result_payload,request_digest) VALUES(?,?,?,?,?,?)",
            (native["operation"], native["key"], native["request_digest"], "[]",
             canonical_bytes([{"graph_result": result}]).decode(), native["request_digest"]),
        )


def _assert_not_found(callback):
    with pytest.raises(ContractValidationError) as error:
        callback()
    assert error.value.reason_code == "not_found"
    assert error.value.status_code == 404


def test_retired_candidate_read_and_session_data_write_are_unreachable_but_receipt_is_cold(service, monkeypatch):
    application = GraphApplication(service)
    document = text_graph(service.registry)
    session = create(service, document)
    completed = run(service, session)
    session_id = completed["workflow_session_id"]
    chain_id = completed["selected_chain_run_id"]

    assert completed["status"] == "succeeded"
    assert chain_id in completed["history_refs"]
    assert application.query("run.read", {"session_id": session_id, "chain_id": chain_id})[
        "chain"]["status"] == "succeeded"
    assert application.query("output.list", {"session_id": session_id})[
        "outputs"][0]["payload"]["text"] == "pear pear"
    assert application.query("object.list", {"session_id": session_id}) == service.get_session_objects(session_id)

    commands = {row["name"] for row in application.describe()["commands"]}
    queries = {row["name"] for row in application.describe()["queries"]}
    assert "session.data.update" not in commands
    assert "candidate.list" not in queries
    assert not hasattr(service, "update_data")
    assert not hasattr(service, "list_graph_candidates")
    retired = {item.name: item for item in RETIRED_RECEIPT_COMMANDS}
    assert retired["session.data.update"].method == "update_data"
    assert retired["session.data.update"].required == frozenset({
        "expected_revision", "expected_data_revision", "idempotency_key",
    })
    assert retired["session.data.update"].optional == frozenset({"variables", "shared"})

    parameters = {
        "session_id": session_id,
        "expected_revision": completed["revision"],
        "expected_data_revision": completed["data_revision"],
        "idempotency_key": "frozen-session-data-update",
    }
    stored_result = deepcopy(completed)
    stored_result["revision"] += 1
    stored_result["data_revision"] += 1
    _insert_historical_data_receipt(service.database, parameters, stored_result)

    cold_application = GraphApplication(SimpleNamespace(database=service.database))
    _assert_not_found(lambda: cold_application.command("session.data.update", parameters))
    _assert_not_found(lambda: cold_application.query("candidate.list", {"session_id": session_id}))
    _assert_not_found(lambda: dispatch_graph(
        cold_application, "GET", f"/api/graph/sessions/{session_id}/candidates"))
    _assert_not_found(lambda: dispatch_graph(
        cold_application, "POST", f"/api/graph/sessions/{session_id}/data", {}))
    _assert_not_found(lambda: dispatch_graph(cold_application, "POST", "/api/graph/commands", {
        "operation": "session.data.update", "parameters": parameters,
    }))
    _assert_not_found(lambda: dispatch_graph(cold_application, "POST", "/api/graph/queries", {
        "operation": "candidate.list", "parameters": {"session_id": session_id},
    }))

    receipt_request = {"operation": "session.data.update", "parameters": parameters}
    before = raw_database(service.database)
    monkeypatch.setattr(GraphWorkflowService, "__init__", lambda *args, **kwargs: pytest.fail(
        "Cold receipt verification must not initialize a graph service"))
    monkeypatch.setattr(SqliteStore, "__init__", lambda *args, **kwargs: pytest.fail(
        "Cold receipt verification must not construct a storage coordinator"))
    matched = cold_application.query("receipt.read", receipt_request)
    assert matched["outcome"] == "matched"
    assert matched["receipt"]["operation"] == "session.data.update"
    assert matched["receipt"]["request_sha256"] == application_command_digest(
        "session.data.update", parameters)
    assert matched["result"] == stored_result
    assert dispatch_graph(cold_application, "POST", "/api/graph/queries", {
        "operation": "receipt.read", "parameters": receipt_request,
    }) == (200, matched)

    forged_owner = {**parameters, "session_id": str(uuid4())}
    forged = cold_application.query("receipt.read", {
        "operation": "session.data.update", "parameters": forged_owner,
    })
    assert forged["outcome"] == "unresolved"
    assert forged["reason_code"] == "request_mismatch"
    changed_body = {**parameters, "shared": {"unregistered": "value"}}
    changed = cold_application.query("receipt.read", {
        "operation": "session.data.update", "parameters": changed_body,
    })
    assert changed["outcome"] == "unresolved"
    assert changed["reason_code"] == "request_mismatch"
    consumer = cold_application.for_consumer().query("receipt.read", {
        "operation": "session.data.update", "parameters": parameters,
    })
    assert consumer["outcome"] == "unresolved"
    assert consumer["reason_code"] == "application_scope_denied"
    assert raw_database(service.database) == before


def test_retired_session_routes_return_404_before_constructing_services(tmp_path, monkeypatch):
    database = tmp_path / "missing" / "product.sqlite"
    session_id = str(uuid4())

    def forbidden_constructor(*args, **kwargs):
        pytest.fail("Retired session routes must not construct graph services or storage")

    monkeypatch.setattr(GraphWorkflowService, "__init__", forbidden_constructor)
    monkeypatch.setattr(GraphManagementService, "__init__", forbidden_constructor)
    monkeypatch.setattr(SqliteStore, "__init__", forbidden_constructor)

    server = create_server(SimpleNamespace(database=database), port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        for method, path in (
            ("POST", f"/api/graph/sessions/{session_id}/data"),
            ("GET", f"/api/graph/sessions/{session_id}/candidates"),
            ("POST", f"/api/graph/sessions/{session_id}/candidates/select"),
            ("POST", f"/api/graph/sessions/{session_id}/candidates/fork"),
        ):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
            }
            body = json.dumps({}) if method == "POST" else None
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                payload = json.loads(response.read())
                assert response.status == 404
                assert payload["error"]["code"] == "not_found"
            finally:
                connection.close()
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()

    assert not database.parent.exists()
