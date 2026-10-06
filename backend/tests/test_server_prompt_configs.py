"""Real loopback HTTP for the versioned, non-executing prompt catalog."""

import http.client
import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.server import create_server
from phase1_agent.workflow import WorkflowService
from test_workflow_prompt_configs import config, durable, group, item, uid


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="prompt-http-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


@contextmanager
def running_server(service):
    with create_server(service, port=0) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def request(port, method, path, payload=None, *, origin=True, raw=None):
    headers = {"Host": f"127.0.0.1:{port}"}
    body = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{port}"
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def mutation(record, expected=0, key="write"):
    return {"record": record, "expected_revision": expected, "idempotency_key": key}


def test_http_crud_resolve_and_retry_survive_hidden_definitions_and_reopen(database):
    with closing(WorkflowService(database)) as service:
        before = durable(database)
        with running_server(service) as port:
            payload = mutation(item())
            status, saved = request(port, "POST", "/api/prompt-configs/item", payload)
            assert status == 201
            assert request(port, "POST", "/api/prompt-configs/item", payload) == (201, saved)
            assert request(port, "GET", "/api/prompt-configs/item") == (200, [saved])
            for kind, record in (("group", group()), ("config", config())):
                assert request(port, "POST", f"/api/prompt-configs/{kind}",
                               mutation(record, key=kind))[0] == 201
            path = f"/api/prompt-configs/config/{uid(3)}/revisions/1/resolve"
            status, resolved = request(port, "POST", path, {})
            assert status == 200 and len(resolved["items"]) == 2
            deleted = request(port, "POST", f"/api/prompt-configs/item/{uid(1)}/delete", {
                "expected_revision": 1, "idempotency_key": "hide",
            })
            assert deleted[0] == 200 and not deleted[1]["head"]["selectable"]
            assert request(port, "GET", "/api/prompt-configs/item") == (200, [])
            assert request(port, "GET", f"/api/prompt-configs/item/{uid(1)}/revisions/1") == (200, item())
            assert request(port, "POST", path, {}) == (200, resolved)
            assert request(port, "POST", "/api/prompt-configs/item", payload) == (201, saved)
        assert durable(database) == before
    with closing(WorkflowService(database)) as service:
        with running_server(service) as port:
            assert request(port, "POST", path, {}) == (200, resolved)


@pytest.mark.parametrize("change", [
    lambda value: value.update(expected_revision=True),
    lambda value: value.update(extra="Private extra text"),
    lambda value: value["record"].update(schema_version=2),
    lambda value: value["record"].update(role="tool"),
    lambda value: value["record"].update(item_id="PRIVATE-ID"),
    lambda value: value["record"].update(order=True),
])
def test_http_invalid_config_is_redacted_bad_request_with_no_effect(database, change):
    with closing(WorkflowService(database)) as service:
        with running_server(service) as port:
            before = durable(database)
            payload = mutation(item())
            change(payload)
            status, body = request(port, "POST", "/api/prompt-configs/item", payload)
            assert status == 400
            assert "PRIVATE" not in str(body) and "Private" not in str(body)
            assert request(port, "GET", "/api/prompt-configs/item") == (200, [])
            assert durable(database) == before


def test_http_stale_key_conflicts_missing_refs_and_strict_peer_paths(database):
    calls = []
    with closing(WorkflowService(database, model_factory=lambda stage: calls.append(stage))) as service:
        with running_server(service) as port:
            payload = mutation(item())
            assert request(port, "POST", "/api/prompt-configs/item", payload)[0] == 201
            changed = deepcopy(payload)
            changed["record"]["text"] = "PRIVATE CONFLICT"
            status, error = request(port, "POST", "/api/prompt-configs/item", changed)
            assert status == 409 and error["error"]["reason_code"] == "idempotency_conflict"
            assert "PRIVATE" not in str(error)
            stale = mutation(item(2), expected=0, key="stale")
            assert request(port, "POST", "/api/prompt-configs/item", stale)[0] == 409
            assert request(port, "GET", f"/api/prompt-configs/config/{uid(3)}/revisions/1")[0] == 404
            assert request(port, "POST", "/api/prompt-configs/group", mutation(group(), key="group"))[0] == 201
            assert request(port, "POST", "/api/prompt-configs/item", payload, origin=False)[0] == 403
            assert request(port, "POST", "/api/prompt-configs/item?revision=1", payload)[0] == 404
            for raw in (b"[]", b'{"record":{},"record":{}}', b'{"expected_revision":NaN}'):
                assert request(port, "POST", "/api/prompt-configs/item", raw=raw)[0] == 400
            assert calls == []


def test_http_storage_corruption_is_an_explicit_redacted_server_fault(database):
    with closing(WorkflowService(database)) as service:
        with running_server(service) as port:
            assert request(port, "POST", "/api/prompt-configs/item", mutation(item()))[0] == 201
            with closing(sqlite3.connect(database)) as raw:
                raw.execute(
                    "UPDATE prompt_revisions SET payload = ? WHERE definition_id = ?",
                    ('{"PRIVATE CORRUPT BODY":true}', uid(1)),
                )
                raw.commit()
            for path in (
                "/api/prompt-configs/item",
                f"/api/prompt-configs/item/{uid(1)}",
                f"/api/prompt-configs/item/{uid(1)}/revisions/1",
            ):
                status, body = request(port, "GET", path)
                assert status == 500
                assert body["error"]["code"] == "internal_error"
                assert body["error"]["reason_code"] == "storage_contract_violation"
                assert "PRIVATE" not in str(body)


def test_http_transaction_failure_can_retry_same_key_without_partial_catalog(database):
    failed = False

    def fault(point):
        nonlocal failed
        if point == "prompt_after_revision_write" and not failed:
            failed = True
            raise RuntimeError("PRIVATE STORAGE FAILURE")

    with closing(WorkflowService(database, fault_injector=fault)) as service:
        with running_server(service) as port:
            payload = mutation(item())
            status, body = request(port, "POST", "/api/prompt-configs/item", payload)
            assert status == 500 and "PRIVATE" not in str(body)
            assert request(port, "GET", "/api/prompt-configs/item") == (200, [])
            assert request(port, "POST", "/api/prompt-configs/item", payload)[0] == 201
            assert len(request(port, "GET", "/api/prompt-configs/item")[1]) == 1
