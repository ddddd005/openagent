"""Real HTTP selection, CAS, replay, and restart recovery."""

import http.client
import json
import threading
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from phase1_agent.server import create_server
from phase1_agent.workflow import OfflineAdapter, WorkflowService


def request(port, method, path, payload=None):
    headers = {"Host": f"127.0.0.1:{port}"}
    body = None
    if method == "POST":
        headers.update({
            "Origin": f"http://127.0.0.1:{port}",
            "Content-Type": "application/json",
        })
        body = json.dumps(payload).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_http_switch_uses_independent_revision_and_recovers_on_restart():
    with TemporaryDirectory(prefix="http-active-session-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=OfflineAdapter)) as service:
            with create_server(service, port=0) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    port = server.server_address[1]
                    first = request(port, "POST", "/api/sessions", {})[1]
                    second = request(port, "POST", "/api/sessions", {})[1]
                    source = first["workflow_session_id"]
                    target = second["workflow_session_id"]
                    assert request(port, "GET", "/api/active-session") == (
                        200, {"active_workflow_session_id": source, "revision": 1},
                    )
                    switch = {"workflow_session_id": target,
                              "expected_selection_revision": 1,
                              "idempotency_key": "select-child"}
                    result = (200, {"active_workflow_session_id": target, "revision": 2})
                    assert request(port, "POST", "/api/active-session", switch) == result
                    assert request(port, "POST", "/api/active-session", switch) == result
                    assert request(port, "POST", "/api/active-session", {
                        **switch, "workflow_session_id": source,
                    })[0] == 409
                    assert request(port, "POST", "/api/active-session", {
                        **switch, "idempotency_key": "stale",
                        "workflow_session_id": source,
                    })[0] == 409
                    assert request(port, "GET", f"/api/sessions/{source}")[1][
                        "revision"
                    ] == first["revision"]
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    assert not thread.is_alive()
        with closing(WorkflowService(database, model_factory=OfflineAdapter)) as reopened:
            assert reopened.get_active_session() == result[1]
