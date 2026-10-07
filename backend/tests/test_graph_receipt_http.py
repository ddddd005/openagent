"""Receipt HTTP routes leave the lazy coordinator and all saved rows untouched."""

from contextlib import closing, contextmanager
import http.client
import json
import sqlite3
from threading import Thread

import pytest

from phase1_agent.server import create_server
from phase1_agent.workflow_host import WorkflowHost
from test_graph_application_receipt_custody import (
    assert_matched, assert_unresolved, forbidden, prohibit_coordinators, raw_database,
    seed_created, uid,
)


@contextmanager
def serve_receipts(path, monkeypatch):
    host = WorkflowHost(path)
    prohibit_coordinators(monkeypatch)
    monkeypatch.setattr(WorkflowHost, "graph_service", property(forbidden))
    assert not hasattr(WorkflowHost, "__getattr__")
    server = create_server(host, port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        assert host._graph is None and not hasattr(host, "_legacy")
        yield host, server.server_address[1]
        assert host._graph is None and not hasattr(host, "_legacy")
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()
        host.close()
        assert not worker.is_alive()


def post(port, path, value, *, raw=False):
    body = value if raw else json.dumps(value)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", path, body, {
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}",
        })
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize("consumer,named", [
    (False, False), (False, True), (True, False), (True, True),
], ids=["management-route", "management-query", "consumer-route", "consumer-query"])
def test_receipt_routes_match_real_origins_without_initializing_hosts(
    tmp_path, monkeypatch, consumer, named,
):
    database = tmp_path / "http-matched.sqlite"
    operation, parameters, origin = seed_created(database, consumer=consumer)
    scope_path = "/consumer" if consumer else ""
    body = {"operation": operation, "parameters": parameters}
    path = "/api/graph" + scope_path + ("/queries" if named else "/receipts/read")
    if named:
        body = {"operation": "receipt.read", "parameters": body}
    before = raw_database(database)
    with serve_receipts(database, monkeypatch) as (_, port):
        status, result = post(port, path, body)
        assert status == 200
        assert_matched(result, origin, consumer=consumer)
        status, repeated = post(port, path, body)
        assert status == 200 and repeated == result
    assert raw_database(database) == before


def test_http_direct_native_receipt_is_unresolved_without_a_new_origin(tmp_path, monkeypatch):
    database = tmp_path / "http-native-only.sqlite"
    operation, parameters, _ = seed_created(database, direct=True)
    before = raw_database(database)
    with serve_receipts(database, monkeypatch) as (_, port):
        status, result = post(port, "/api/graph/receipts/read", {
            "operation": operation, "parameters": parameters,
        })
        assert status == 200
        assert_unresolved(result)
    assert raw_database(database) == before


@pytest.mark.parametrize("defect", ["request", "consumer-scope", "extra-envelope", "bad-json"])
def test_http_failed_resolution_never_dispatches_or_writes(tmp_path, monkeypatch, defect):
    database = tmp_path / "http-refusal.sqlite"
    operation, parameters, _ = seed_created(database)
    body = {"operation": operation, "parameters": parameters}
    path = "/api/graph/receipts/read"
    if defect == "request":
        parameters["definition_revision"] = 2
    elif defect == "consumer-scope":
        path = "/api/graph/consumer/receipts/read"
    elif defect == "extra-envelope":
        body["scope"] = "management"
    else:
        body = '{"operation":"session.create","parameters":'
    before = raw_database(database)
    with serve_receipts(database, monkeypatch) as (host, port):
        status, result = post(port, path, body, raw=defect == "bad-json")
        if status == 200:
            assert_unresolved(result)
        else:
            assert 400 <= status < 500
            assert "receipt" not in result and "result" not in result
        assert host._graph is None and not hasattr(host, "_legacy")
    assert raw_database(database) == before


def test_http_missing_database_remains_missing_and_unresolved(tmp_path, monkeypatch):
    database = tmp_path / "http-missing.sqlite"
    with serve_receipts(database, monkeypatch) as (_, port):
        status, result = post(port, "/api/graph/receipts/read", {
            "operation": "session.create",
            "parameters": {
                "workflow_definition_id": uid(1), "definition_revision": 1,
                "idempotency_key": "never-created",
            },
        })
        assert status == 200
        assert_unresolved(result)
    assert not database.exists()


def test_http_old_schema_is_not_initialized_or_migrated(tmp_path, monkeypatch):
    database = tmp_path / "http-schema-one.sqlite"
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        connection.execute(
            "CREATE TABLE idempotency(operation TEXT,key TEXT,digest TEXT,result_refs TEXT,"
            "result_payload TEXT,PRIMARY KEY(operation,key))",
        )
        connection.execute("PRAGMA user_version=1")
    before = raw_database(database)
    with serve_receipts(database, monkeypatch) as (_, port):
        status, result = post(port, "/api/graph/receipts/read", {
            "operation": "session.create",
            "parameters": {
                "workflow_definition_id": uid(1), "definition_revision": 1,
                "idempotency_key": "old-schema",
            },
        })
        assert status == 200
        assert_unresolved(result)
    assert raw_database(database) == before
