"""Graph HTTP integration and lazy host initialization on temporary databases."""

import http.client
import json
from contextlib import contextmanager
from threading import Thread

from phase1_agent.server import MAX_GRAPH_BODY_BYTES, create_server
from phase1_agent.workflow_host import WorkflowHost
from test_graph_service import text_graph


@contextmanager
def host_server(tmp_path):
    database = tmp_path / "http.sqlite"
    host = WorkflowHost(database)
    server = create_server(host, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, server.server_address[1]
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
        host.close()


def request(port, method, path, payload=None, *, origin=True, extra_headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = f"http://127.0.0.1:{port}"
    headers.update(extra_headers or {})
    body = json.dumps(payload) if payload is not None else None
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_graph_routes_run_without_agent_bootstrap(tmp_path):
    with host_server(tmp_path) as (host, port):
        assert request(port, "GET", "/api/health")[0] == 200
        assert host._graph is None and not hasattr(host, "_legacy")
        status, catalog = request(port, "GET", "/api/graph/node-types/v2")
        assert status == 200
        assert {row["component_id"] for row in catalog["node_types"]} >= {
            "tools.text", "tools.regex", "tools.output",
        }
        doc = text_graph(host.graph_service.registry)
        assert request(port, "POST", "/api/graph/definitions", {
            "document": doc, "expected_revision": 0, "idempotency_key": "save"}) == (201, doc)
        definition_path = "/api/graph/definitions/" + doc["workflow_definition_id"]
        assert request(port, "GET", definition_path + "/revisions/1") == (200, doc)
        status, session = request(port, "POST", "/api/graph/sessions", {
            "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1, "idempotency_key": "session"})
        assert status == 201
        session_path = "/api/graph/sessions/" + session["workflow_session_id"]
        status, started = request(port, "POST", session_path + "/runs", {
            "expected_revision": session["revision"], "idempotency_key": "run", "inputs": {}})
        assert status == 202
        host.graph_service.wait(started["active_chain_run_id"])
        status, final = request(port, "GET", session_path)
        assert status == 200 and final["status"] == "succeeded"
        status, history = request(port, "GET", session_path + "/runs/" + started["active_chain_run_id"])
        assert status == 200 and len(history["node_runs"]) == 3
        assert len(request(port, "GET", definition_path + "/sessions")[1]) == 1
        assert not hasattr(host, "_legacy")
        assert not hasattr(host.graph_service, "_native_runtime")


def test_current_platform_and_empty_definition_reject_retired_migration(tmp_path):
    with host_server(tmp_path) as (host, port):
        status, platform = request(port, "GET", "/api/graph/platform")
        assert status == 200 and platform["package_diagnostics"] == []
        assert "workflow.compat" not in {row["package_id"] for row in platform["package_lock"]}
        assert request(port, "POST", "/api/graph/resources/list", {}) == (200, [])
        assert request(port, "GET", "/api/model-configurations/provider")[0] == 404
        assert request(port, "GET", "/api/global-content")[0] == 404
        assert not hasattr(host, "_legacy") and not hasattr(host.graph_service, "_native_runtime")
        doc = text_graph(host.graph_service.registry)
        doc["nodes"] = []
        doc["edges"] = []
        body = {"document": doc, "source_session_id": None, "expected_source_revision": None,
                "mappings": [], "idempotency_key": "empty-migration"}
        assert request(port, "POST", "/api/graph/migrations", body)[0] == 404
        assert request(port, "POST", "/api/graph/definitions", {
            "document": doc, "expected_revision": 0, "idempotency_key": "empty-definition",
        }) == (201, doc)
        status, session = request(port, "POST", "/api/graph/sessions", {
            "workflow_definition_id": doc["workflow_definition_id"],
            "definition_revision": 1, "idempotency_key": "empty-session",
        })
        assert status == 201
        sid = session["workflow_session_id"]
        status, error = request(port, "POST", f"/api/graph/sessions/{sid}/runs", {"expected_revision": 1, "idempotency_key": "no-output"})
        assert status == 400 and error["error"]["reason_code"] == "graph_no_outputs"
        assert host.graph_service.get_session(sid)["chains"] == []
        assert not hasattr(host, "_legacy")


def test_structured_diagnostics_strict_fields_and_origins(tmp_path):
    with host_server(tmp_path) as (host, port):
        status, error = request(port, "POST", "/api/graph/definitions", {}, origin=False)
        assert status == 403 and "diagnostics" in error["error"]
        doc = text_graph(host.graph_service.registry)
        doc["edges"] = []
        assert request(port, "POST", "/api/graph/definitions", {
            "document": doc, "expected_revision": 0, "idempotency_key": "save"})[0] == 201
        _, session = request(port, "POST", "/api/graph/sessions", {
            "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1, "idempotency_key": "session"})
        path = "/api/graph/sessions/" + session["workflow_session_id"] + "/runs"
        status, error = request(port, "POST", path, {"expected_revision": 1, "idempotency_key": "run"})
        assert status == 400 and error["error"]["reason_code"] == "graph_required_input_missing"
        assert error["error"]["diagnostics"][0]["node_id"] == doc["nodes"][-1]["node_binding_id"]
        assert error["error"]["diagnostics"][0]["port_id"] == "input"
        status, error = request(port, "POST", path, {"expected_revision": True, "idempotency_key": "bad"})
        assert status == 400 and error["error"]["reason_code"] == "invalid_request"
        assert request(port, "POST", path, {"expected_revision": 1, "idempotency_key": "bad", "rogue": 1})[0] == 400
        assert host.graph_service.get_session(session["workflow_session_id"])["chains"] == []


def test_current_graph_body_limit_and_retired_routes(tmp_path):
    with host_server(tmp_path) as (host, port):
        doc = text_graph(host.graph_service.registry)
        doc["nodes"][0]["config"]["text"] = "x" * 70_000
        assert request(port, "POST", "/api/graph/definitions", {
            "document": doc, "expected_revision": 0, "idempotency_key": "large"})[0] == 201
        assert request(port, "POST", "/api/sessions", {"text": "x" * 70_000})[0] == 404
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST", "/api/graph/definitions", body=None, headers={
                "Content-Type": "application/json", "Content-Length": str(MAX_GRAPH_BODY_BYTES + 1),
                "Origin": f"http://127.0.0.1:{port}"})
            response = connection.getresponse()
            assert response.status == 413
            assert json.loads(response.read())["error"]["reason_code"] == "invalid_request"
        finally:
            connection.close()
