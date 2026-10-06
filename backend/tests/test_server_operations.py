"""Real loopback HTTP for the v1 workflow-operation envelope and legacy routes."""

import copy
import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.server import create_server
from phase1_agent.workflow import OfflineAdapter, WorkflowService
from test_workflow_operations import durable, envelope
from test_workflow_components import FinalModel, IndependentKernel, selected


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="http-operation-", dir=Path(__file__).parent) as folder:
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


def request(port, method, path, payload=None, *, origin=True, headers=None, raw=None):
    request_headers = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    body = None
    if method == "POST":
        request_headers["Content-Type"] = "application/json"
        if origin:
            request_headers["Origin"] = f"http://127.0.0.1:{port}"
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_http_envelope_submit_replay_and_legacy_body_remain_compatible(database):
    with closing(WorkflowService(database)) as service:
        with running_server(service) as port:
            sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
            operation = envelope(service, sid, "submit", payload={"text": "Public command"})
            status, receipt = request(port, "POST", "/api/operations", operation)
            assert status == 202 and receipt["operation_id"] == operation["operation_id"]
            service.wait_for_idle(sid)
            before = durable(database)
            assert request(port, "POST", "/api/operations", operation) == (status, receipt)
            assert durable(database) == before
            status, submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "Unchanged legacy body", "idempotency_key": "legacy",
            })
            assert status == 202
            assert "result" not in submitted and "operation_id" not in submitted
            service.wait_for_idle(sid)
            invalid = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "legacy", "idempotency_key": "other", "expected_revision": 1,
            })
            assert invalid[0] == 400 and "reason_code" not in invalid[1]["error"]


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r.update(payload={"text": "private-user-text", "chain_run_id": str(uuid4())}),
     "invalid_request"),
    (lambda r: r.update(schema_version=2), "invalid_request"),
    (lambda r: r["expected_revisions"][0].update(revision=99), "stale_revision"),
    (lambda r: r.update(operation_id="PRIVATE-INVALID-ID"), "invalid_request"),
])
def test_http_refusals_are_classified_redacted_and_effect_free(database, mutation, reason):
    with closing(WorkflowService(database)) as service:
        with running_server(service) as port:
            sid = service.create_session()["workflow_session_id"]
            operation = envelope(service, sid, "submit", payload={"text": "private-user-text"})
            mutation(operation)
            before = durable(database)
            status, body = request(port, "POST", "/api/operations", operation)
            assert status == (409 if reason == "stale_revision" else 400)
            assert body["error"]["reason_code"] == reason
            assert "private-user-text" not in str(body)
            assert "PRIVATE" not in str(body)
            assert durable(database) == before


def test_http_concurrent_replays_create_one_chain_and_no_double_dispatch(database):
    calls = []
    with closing(WorkflowService(
        database, model_factory=lambda stage: calls.append(stage) or OfflineAdapter(stage),
    )) as service:
        with running_server(service) as port:
            sid = service.create_session()["workflow_session_id"]
            operation = envelope(service, sid, "submit", payload={"text": "one command"})
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(request, port, "POST", "/api/operations", operation)
                    for _ in range(4)
                ]
                responses = [future.result(timeout=15) for future in futures]
            assert all(result == responses[0] for result in responses)
            assert responses[0][0] == 202
            service.wait_for_idle(sid)
            assert calls == ["A", "B"]
            assert len(durable(database)["chain_run"]) == 1
            changed = {**operation, "operation_id": str(uuid4())}
            status, body = request(port, "POST", "/api/operations", changed)
            assert status == 409 and body["error"]["reason_code"] == "idempotency_conflict"


def test_committed_command_response_failure_does_not_redispatch_on_http_retry(database, monkeypatch):
    calls = []
    with closing(WorkflowService(
        database, model_factory=lambda stage: calls.append(stage) or OfflineAdapter(stage),
    )) as service:
        with running_server(service) as port:
            sid = service.create_session()["workflow_session_id"]
            operation = envelope(service, sid, "submit", payload={"text": "response lost"})
            dispatch = service.dispatch_operation
            lost = False

            def lose_first_response(value):
                nonlocal lost
                result = dispatch(value)
                if not lost:
                    lost = True
                    raise RuntimeError("PRIVATE RESPONSE FAILURE")
                return result

            monkeypatch.setattr(service, "dispatch_operation", lose_first_response)
            status, body = request(port, "POST", "/api/operations", operation)
            assert status == 500 and "PRIVATE" not in str(body)
            status, receipt = request(port, "POST", "/api/operations", operation)
            assert status == 202 and receipt["operation_id"] == operation["operation_id"]
            service.wait_for_idle(sid)
            assert calls == ["A", "B"]
            assert len(durable(database)["chain_run"]) == 1


def test_http_branch_receipt_is_created_and_ref_ownership_is_checked(database):
    with closing(WorkflowService(database)) as service:
        with running_server(service) as port:
            sid = service.create_session()["workflow_session_id"]
            other = service.create_session()["workflow_session_id"]
            submitted = service.submit(sid, "branch exact boundary", "input")
            service.wait_for_idle(sid)
            operation = envelope(service, sid, "create_branch", submitted["visible_message_id"])
            foreign = envelope(service, other, "create_branch", submitted["visible_message_id"])
            wrong = copy.deepcopy(operation)
            wrong["expected_revisions"] = [
                next(row for row in foreign["expected_revisions"] if row["kind"] == "workflow_ref")
                if row["kind"] == "workflow_ref" else row
                for row in wrong["expected_revisions"]
            ]
            before = durable(database)
            status, body = request(port, "POST", "/api/operations", wrong)
            assert status == 409 and body["error"]["reason_code"] == "ownership_mismatch"
            assert durable(database) == before
            status, receipt = request(port, "POST", "/api/operations", operation)
            assert status == 201 and receipt["status"] == "completed"
            assert request(port, "POST", "/api/operations", operation) == (status, receipt)
            assert len(service.list_sessions()) == 3


def test_http_operation_peer_path_strict_json_and_unsupported_initialization(database):
    with closing(WorkflowService(database)) as service:
        with running_server(service) as port:
            sid = service.create_session()["workflow_session_id"]
            operation = envelope(service, sid, "submit", payload={"text": "never run"})
            before = durable(database)
            assert request(port, "POST", "/api/operations", operation, origin=False)[0] == 403
            assert request(port, "POST", "/api/operations", operation,
                           headers={"Host": "evil.example"})[0] == 403
            assert request(port, "GET", "/api/operations")[0] == 404
            assert request(port, "POST", "/api/operations?revision=1", operation)[0] == 404
            for raw in (b"[]", b'{"kind":"submit","kind":"resume"}', b'{"payload":NaN}'):
                status, body = request(port, "POST", "/api/operations", raw=raw)
                assert status == 400 and body["error"]["reason_code"] == "invalid_request"
            status, body = request(
                port, "POST", "/api/operations", envelope(service, sid, "initialize_session"),
            )
            assert status == 400 and body["error"]["reason_code"] == "unsupported"
            assert durable(database) == before


def test_custom_kernel_http_control_rejects_unsupported_without_changing_state(database):
    entered, release = threading.Event(), threading.Event()
    calls = []
    components = selected(kernel=IndependentKernel(entered=entered, release=release))
    with closing(WorkflowService(
        database, components=components, model_factory=lambda stage: FinalModel(stage, calls),
    )) as service:
        with running_server(service) as port:
            sid = service.create_session()["workflow_session_id"]
            service.submit(sid, "custom control", "input")
            assert entered.wait(5)
            try:
                view = service.get_session(sid)
                run = view["nodes"][0]
                operation = envelope(service, sid, "interrupt", run["run_id"])
                before = durable(database)
                status, body = request(port, "POST", "/api/operations", operation)
                assert status == 409 and body["error"]["reason_code"] == "unsupported"
                assert durable(database) == before
                status, body = request(
                    port, "POST", f"/api/sessions/{sid}/runs/{run['run_id']}/interrupt", {
                        "idempotency_key": "legacy-unsupported",
                        "expected_session_revision": view["revision"],
                        "expected_run_revision": run["revision"],
                    },
                )
                assert status == 409 and "reason_code" not in body["error"]
                assert durable(database) == before
                assert calls == []
            finally:
                release.set()
            service.wait_for_idle(sid)
            assert service.get_session(sid)["error"] is None
