"""Fresh HTTP defaults use current packages without legacy registry bootstrap."""

from contextlib import closing, contextmanager
import http.client
import json
from threading import Thread
from uuid import uuid4

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.server import create_server
from phase1_agent.workflow_host import WorkflowHost


@contextmanager
def serve(database):
    host = WorkflowHost(database, mode="offline")
    server = create_server(host, port=0, mode="offline")
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield host, server.server_address[1]
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()
        host.close()


def request(port, path, operation, parameters):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", path, json.dumps({
            "operation": operation, "parameters": parameters,
        }), {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def command(port, operation, **parameters):
    return request(port, "/api/graph/commands", operation, parameters)


def query(port, operation, **parameters):
    status, value = request(port, "/api/graph/queries", operation, parameters)
    assert status == 200
    return value


def test_fresh_http_current_graph_runs_and_reopens_without_compat(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Current HTTP defaults must not construct the compatibility registry")

    monkeypatch.setattr("phase1_agent.graph_nodes.create_default_registry", forbidden)
    database = tmp_path / "fresh-http.sqlite"
    source, output = str(uuid4()), str(uuid4())
    document = {
        "schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Fresh current HTTP graph", "object_bindings": [], "package_lock": [],
        "nodes": [
            {"node_binding_id": source, "component_id": "tools.text", "component_version": "1",
             "title": "Text", "position": {"x": 0, "y": 0}, "config": {"text": "current HTTP"}},
            {"node_binding_id": output, "component_id": "tools.output", "component_version": "1",
             "title": "Output", "position": {"x": 250, "y": 0}, "config": {"mode": "text"}},
        ],
        "edges": [
            {"edge_id": str(uuid4()), "source_node_id": source, "source_port_id": "output",
             "target_node_id": output, "target_port_id": "input", "order": 0},
        ],
    }
    with serve(database) as (host, port):
        assert host._graph is None and host._legacy is None
        platform = query(port, "platform")
        assert "workflow.compat" not in {row["package_id"] for row in platform["package_lock"]}
        assert platform["package_diagnostics"] == []
        catalog = query(port, "catalog.node-types", protocol_version=2)
        components = {row["component_id"] for row in catalog["node_types"]}
        assert {"tools.text", "tools.output", "agents.execute"} <= components
        assert "workflow.agent" not in components and "workflow.text" not in components
        document["package_lock"] = platform["execution_package_lock"]
        status, saved = command(port, "definition.save", document=document,
                                expected_revision=0, idempotency_key="save-current")
        assert status == 201
        assert saved["receipt"]["operation"] == "definition.save"
        status, created = command(port, "session.create",
                                  workflow_definition_id=document["workflow_definition_id"],
                                  definition_revision=1, idempotency_key="session-current")
        assert status == 201
        session = created["result"]
        status, started = command(port, "run.start", session_id=session["workflow_session_id"],
                                  expected_revision=session["revision"], idempotency_key="run-current")
        assert status == 202
        chain_id = started["result"]["active_chain_run_id"]
        host.graph_service.wait(chain_id)
        completed = query(port, "session.read", session_id=session["workflow_session_id"])
        assert completed["status"] == "succeeded"
        assert completed["nodes"][-1]["outputs"]["output"]["text"] == "current HTTP"
        history = query(port, "run.read", session_id=session["workflow_session_id"], chain_id=chain_id)
        frozen = next(row for row in history["outputs"] if row["node_binding_id"] == output)
        assert frozen["payload"]["text"] == "current HTTP"
        assert host._legacy is None and host.graph_service._native_runtime is None

    with serve(database) as (host, port):
        assert query(port, "platform")["package_diagnostics"] == []
        assert query(port, "session.read", session_id=session["workflow_session_id"])["status"] == "succeeded"
        assert query(port, "run.read", session_id=session["workflow_session_id"], chain_id=chain_id) == history
        with closing(host.graph_service._store()) as store:
            selected = json.loads(store._connection.execute(
                "SELECT payload FROM graph_project_packages WHERE configuration_id='current-execution'",
            ).fetchone()["payload"])
            assert store._connection.execute(
                "SELECT payload FROM graph_project_packages WHERE configuration_id='project'",
            ).fetchone() is None
        assert selected == DEFAULT_PACKAGES
        assert host._legacy is None and host.graph_service._native_runtime is None
