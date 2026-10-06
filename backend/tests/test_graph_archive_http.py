"""Existing private archive routes bypass both lazy execution coordinators."""

from contextlib import contextmanager
import http.client
import json
from threading import Thread
from types import SimpleNamespace

import pytest

from phase1_agent.server import create_server
from phase1_agent.workflow_host import WorkflowHost
from test_graph_agent_contracts import accepted_package, archive_bundle, uid
from test_graph_application_receipt_custody import forbidden, prohibit_coordinators, raw_database
from test_graph_archives import legacy_bundle, native_archive, seed_database


@contextmanager
def serve_archives(path, monkeypatch, *, graph_database=None):
    host = WorkflowHost(path, mode="offline")
    prohibit_coordinators(monkeypatch)
    monkeypatch.setattr(WorkflowHost, "graph_service", property(forbidden))
    monkeypatch.setattr(WorkflowHost, "__getattr__", forbidden)
    graph = SimpleNamespace(database=graph_database) if graph_database is not None else None
    server = create_server(host, port=0, mode="offline", graph_service=graph)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield host, server.server_address[1]
        assert host._graph is None and host._legacy is None
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()
        host.close()
        assert not worker.is_alive()


def request(port, method, path, body=None, *, raw=False):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body if raw or body is None else json.dumps(body), {
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}",
        })
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def archive_request(named, archive_id, *, session_id=None):
    sid = session_id or uid(4)
    if named:
        return "POST", "/api/graph/queries", {
            "operation": "archive.read", "parameters": {"session_id": sid, "archive_id": archive_id},
        }
    return "GET", f"/api/graph/sessions/{sid}/archives/{archive_id}", None


@pytest.mark.parametrize("named", [False, True], ids=["route", "named-query"])
@pytest.mark.parametrize("family", ["native", "prepared2"])
def test_existing_routes_return_exact_archives_without_initializing_hosts(
    tmp_path, monkeypatch, named, family,
):
    path = tmp_path / "http.sqlite"
    if family == "native":
        bundle = archive_bundle(accepted_package())
        expected = native_archive(bundle)
    else:
        from phase1_agent.legacy_archive import read_closed_legacy_archive
        from test_legacy_archive_contracts import Records
        bundle, records = legacy_bundle(family)
        expected = read_closed_legacy_archive(Records(records), records["turn"]["turn_id"])
    seed_database(path, bundle)
    before = raw_database(path)
    with serve_archives(path, monkeypatch) as (_, port):
        args = archive_request(named, expected["turn"]["turn_id"])
        assert request(port, *args) == (200, expected)
        assert request(port, *args) == (200, expected)
    assert raw_database(path) == before


@pytest.mark.parametrize("defect,status,reason", [
    ("consumer", 403, "application_scope_denied"),
    ("foreign-turn", 403, "graph_history_scope_mismatch"),
    ("foreign-session", 404, "not_found"),
    ("extra-envelope", 400, "invalid_request"),
    ("extra-parameter", 400, "invalid_request"),
    ("bad-parameters", 400, "invalid_request"),
    ("bad-json", 400, None),
    ("bad-route-id", 404, "not_found"),
    ("wrong-method", 404, "not_found"),
])
def test_rejected_archive_queries_never_dispatch_or_write(
    tmp_path, monkeypatch, defect, status, reason,
):
    path = tmp_path / "http-refusal.sqlite"
    bundle = archive_bundle(accepted_package())
    seed_database(path, bundle)
    before = raw_database(path)
    method, route, body = archive_request(True, native_archive(bundle)["turn"]["turn_id"])
    if defect == "consumer":
        route = "/api/graph/consumer/queries"
    elif defect == "foreign-turn":
        body["parameters"]["archive_id"] = uid(999)
    elif defect == "foreign-session":
        body["parameters"]["session_id"] = uid(999)
    elif defect == "extra-envelope":
        body["scope"] = "management"
    elif defect == "extra-parameter":
        body["parameters"]["scope"] = "management"
    elif defect == "bad-parameters":
        body["parameters"] = []
    elif defect == "bad-json":
        body = '{"operation":"archive.read","parameters":'
    else:
        route = f"/api/graph/sessions/{uid(4)}/archives/" + (
            "bad" if defect == "bad-route-id" else native_archive(bundle)["turn"]["turn_id"]
        )
        method = "GET" if defect == "bad-route-id" else "POST"
        body = None if method == "GET" else {}
    with serve_archives(path, monkeypatch) as (_, port):
        actual, result = request(port, method, route, body, raw=defect == "bad-json")
        assert actual == status
        assert set(result) == {"error"}
        if reason is not None:
            assert result["error"]["reason_code"] == reason
    assert raw_database(path) == before


def test_missing_database_stays_missing(tmp_path, monkeypatch):
    path = tmp_path / "missing.sqlite"
    with serve_archives(path, monkeypatch) as (_, port):
        status, result = request(port, *archive_request(False, uid(999)))
        assert status == 404
        assert result["error"]["reason_code"] == "not_found"
    assert not path.exists()


def test_explicit_graph_database_takes_precedence_without_touching_host_database(tmp_path, monkeypatch):
    graph_path = tmp_path / "graph.sqlite"
    host_path = tmp_path / "host-missing.sqlite"
    bundle = archive_bundle(accepted_package())
    seed_database(graph_path, bundle)
    before = raw_database(graph_path)
    expected = native_archive(bundle)
    with serve_archives(host_path, monkeypatch, graph_database=graph_path) as (_, port):
        assert request(port, *archive_request(False, expected["turn"]["turn_id"])) == (200, expected)
    assert raw_database(graph_path) == before
    assert not host_path.exists()


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/graph/platform", None),
    ("POST", "/api/graph/queries", {"operation": "session.read", "parameters": {}}),
    ("POST", "/api/graph/consumer/queries", {
        "operation": "archive.read", "parameters": {"session_id": uid(4), "archive_id": uid(999)},
    }),
])
def test_unrelated_or_denied_query_never_resolves_database(method, path, body):
    from phase1_agent.contract_errors import ContractValidationError
    from phase1_agent.graph_archive_http import dispatch_archive_read

    if path == "/api/graph/consumer/queries":
        with pytest.raises(ContractValidationError) as rejected:
            dispatch_archive_read(forbidden, method, path, body)
        assert rejected.value.reason_code == "application_scope_denied"
    else:
        assert dispatch_archive_read(forbidden, method, path, body) is None
