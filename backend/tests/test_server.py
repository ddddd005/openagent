"""Local HTTP boundary tests with a fake workflow service."""

import http.client
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.server import MAX_BODY_BYTES, REJECT_BODY_TIMEOUT, create_server


# Candidate HTTP contract for the five existing WorkflowService control methods.
CONTROL_CASES = (
    (
        "retry_archive", "/api/sessions/s1/runs/r1/retry-archive",
        {"idempotency_key": "once", "expected_session_revision": 2}, 200,
        ("retry_archive", "s1", "r1", "once", 2),
    ),
    (
        "retry_publish", "/api/sessions/s1/runs/r1/retry-publish",
        {"idempotency_key": "once", "expected_session_revision": 2}, 200,
        ("retry_publish", "s1", "r1", "once", 2),
    ),
    (
        "create_branch", "/api/sessions/s1/branches",
        {"visible_message_id": "m1", "idempotency_key": "once", "expected_source_revision": 2},
        201, ("create_branch", "s1", "m1", "once", 2),
    ),
    (
        "create_and_switch_branch", "/api/sessions/s1/branches/switch",
        {"visible_message_id": "m1", "idempotency_key": "once", "expected_source_revision": 2,
         "expected_selection_revision": 1},
        201, ("create_and_switch_branch", "s1", "m1", "once", 2),
    ),
    (
        "continue_pending_input", "/api/sessions/s1/pending-inputs/i1/continue",
        {"idempotency_key": "once", "expected_session_revision": 2}, 202,
        ("continue_pending_input", "s1", "i1", "once", 2),
    ),
)


class FakeService:
    def __init__(self):
        self.calls = []
        self.rejected = False
        self.control_rejected = set()
        self.control_receipts = {}
        self.view = {
            "workflow_session_id": "s1", "revision": 1, "mode": "offline",
            "can_submit": True, "messages": [], "nodes": [], "chains": [],
            "error": None, "available_actions": [],
        }

    def create_session(self):
        self.calls.append(("create",))
        return self.view

    def list_sessions(self):
        self.calls.append(("list",))
        return [{"workflow_session_id": "s1"}]

    def get_session(self, session_id):
        self.calls.append(("get", session_id))
        return self.view

    def get_active_session(self):
        self.calls.append(("get_active",))
        return {"active_workflow_session_id": "s1", "revision": 1}

    def switch_session(self, session_id, *, idempotency_key, expected_selection_revision):
        self.calls.append(("switch_session", session_id, idempotency_key, expected_selection_revision))
        return {"active_workflow_session_id": session_id, "revision": expected_selection_revision + 1}

    def submit(self, session_id, text, idempotency_key):
        self.calls.append(("submit", session_id, text, idempotency_key))
        if self.rejected:
            raise ContractValidationError(f"secret: {text}")
        return {
            "workflow_session_id": session_id, "chain_run_id": "c1",
            "visible_message_id": "m1", "input_id": "i1", "status": "running",
        }

    def _control(self, action, session_id, target_id, idempotency_key, revision):
        self.calls.append((action, session_id, target_id, idempotency_key, revision))
        if action in self.control_rejected:
            raise ContractValidationError("secret workflow state")
        key = (action, session_id, idempotency_key)
        if key in self.control_receipts:
            original_target, receipt = self.control_receipts[key]
            if target_id != original_target:
                raise ContractValidationError("secret idempotency conflict")
            return receipt
        if action.startswith("retry_"):
            receipt = {
                "workflow_session_id": session_id, "run_id": target_id,
                "chain_run_id": "c1", "status": "succeeded",
            }
        elif action == "continue_pending_input":
            receipt = {
                "workflow_session_id": session_id, "chain_run_id": "c2",
                "visible_message_id": "m1", "input_id": target_id, "status": "prepared",
            }
        else:
            child = "s2"
            receipt = {
                "workflow_session_id": child, "source_workflow_session_id": session_id,
                "visible_message_id": target_id, "fork_anchor_id": "f1",
                "role": "user", "pending_input_id": "i2",
                "active_workflow_session_id": (
                    child if action == "create_and_switch_branch" else session_id
                ),
                "status": "pending",
            }
        self.control_receipts[key] = (target_id, receipt)
        return receipt

    def retry_archive(self, session_id, run_id, *, idempotency_key, expected_session_revision):
        return self._control(
            "retry_archive", session_id, run_id, idempotency_key, expected_session_revision,
        )

    def retry_publish(self, session_id, run_id, *, idempotency_key, expected_session_revision):
        return self._control(
            "retry_publish", session_id, run_id, idempotency_key, expected_session_revision,
        )

    def create_branch(
        self, source_workflow_session_id, visible_message_id, *,
        idempotency_key, expected_source_revision,
    ):
        return self._control(
            "create_branch", source_workflow_session_id, visible_message_id,
            idempotency_key, expected_source_revision,
        )

    def create_and_switch_branch(
        self, source_workflow_session_id, visible_message_id, *,
        idempotency_key, expected_source_revision, expected_selection_revision,
    ):
        return self._control(
            "create_and_switch_branch", source_workflow_session_id, visible_message_id,
            idempotency_key, expected_source_revision,
        )

    def continue_pending_input(
        self, session_id, input_id, *, idempotency_key, expected_session_revision,
    ):
        return self._control(
            "continue_pending_input", session_id, input_id,
            idempotency_key, expected_session_revision,
        )


@contextmanager
def running_server():
    service = FakeService()
    server = create_server(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield service, server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def call(port, method, path, body=None, headers=None, *, include_origin=True):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    request_headers = {
        "Host": f"127.0.0.1:{port}",
        **({"Origin": f"http://127.0.0.1:{port}"} if method == "POST" and include_origin else {}),
        **(headers or {}),
    }
    try:
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data
    finally:
        conn.close()


def control_call(port, path, payload, headers=None):
    return call(
        port, "POST", path, json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json", **(headers or {})},
    )


def test_routes_and_static_allowlist():
    with running_server() as (service, port):
        for path, marker in (
            ("/", b"<!doctype html>"),
            ("/static/index.html", b"<!doctype html>"),
            ("/static/app.js", b"textContent"),
            ("/static/chat-entry.js", b"/static/graph-chat.js"),
            ("/static/graph-chat-core.js", b"GraphChatClient"),
            ("/static/graph-chat.js", b"GraphChatClient"),
            ("/static/frontend-package-host.js", b"ConsumerFrontendHost"),
            ("/static/frontend-package.js", b"WorkflowFrontendPackage"),
            ("/static/style.css", b".workflow"),
        ):
            status, headers, data = call(port, "GET", path)
            assert status == 200 and marker in data
            assert headers["X-Content-Type-Options"] == "nosniff"
        assert json.loads(call(port, "GET", "/api/health")[2]) == {"status": "ok", "mode": "offline"}
        assert json.loads(call(port, "GET", "/api/sessions")[2]) == [{"workflow_session_id": "s1"}]
        assert json.loads(call(port, "GET", "/api/sessions/s1")[2]) == service.view
        assert json.loads(call(port, "GET", "/api/active-session")[2]) == {
            "active_workflow_session_id": "s1", "revision": 1,
        }
        assert json.loads(control_call(port, "/api/active-session", {
            "workflow_session_id": "s1", "expected_selection_revision": 1,
            "idempotency_key": "switch",
        })[2]) == {"active_workflow_session_id": "s1", "revision": 2}
        assert call(port, "POST", "/api/sessions", b"{}", {"Content-Type": "application/json"})[0] == 201
        body = json.dumps({"text": "你好", "idempotency_key": "once"}).encode()
        status, _, data = call(port, "POST", "/api/sessions/s1/inputs", body, {"Content-Type": "application/json"})
        assert status == 202 and json.loads(data)["status"] == "running"
        assert service.calls[-1] == ("submit", "s1", "你好", "once")
        for path in ("/static/../contract_errors.py", "/static/secret", "/api/archive", "/api/sessions/s1/../../etc"):
            assert call(port, "GET", path)[0] == 404


def test_rejects_host_origin_and_invalid_json_before_service():
    with running_server() as (service, port):
        assert call(port, "GET", "/", headers={"Host": "evil.example"})[0] == 403
        assert call(port, "GET", "/", headers={"Origin": "http://evil.example"})[0] == 403
        body = b"{}"
        assert call(port, "POST", "/api/sessions", body, {
            "Content-Type": "application/json", "Origin": "http://evil.example"
        })[0] == 403
        assert call(port, "POST", "/api/sessions", body, {
            "Content-Type": "application/json", "Origin": ""
        })[0] == 403
        for body, content_type, expected in (
            (b"{}", "text/plain", 415),
            (b'{"x":1,"x":2}', "application/json", 400),
            (b'{"x":1}', "application/json", 400),
            (b"[]", "application/json", 400),
            (b"\xff", "application/json", 400),
            (b"x" * 65537, "application/json", 413),
        ):
            assert call(port, "POST", "/api/sessions", body, {"Content-Type": content_type})[0] == expected
        assert service.calls == []


def test_missing_origin_and_oversized_length():
    with running_server() as (service, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("POST", "/api/sessions", body=b"{}", headers={
            "Host": f"127.0.0.1:{port}", "Content-Type": "application/json"
        })
        assert conn.getresponse().status == 403
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.putrequest("POST", "/api/sessions")
        conn.putheader("Origin", f"http://127.0.0.1:{port}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "9" * 100)
        conn.endheaders()
        assert conn.getresponse().status == 413
        conn.close()
        assert service.calls == []


def test_rejected_peer_consumes_a_bounded_delayed_body_before_closing_connection():
    with running_server() as (service, port):
        body = b'{"idempotency_key":"once","expected_session_revision":2}'
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            headers = (
                "POST /api/sessions/s1/pending-inputs/i1/continue HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            )
            connection.sendall(headers.encode("ascii"))
            connection.settimeout(0.05)
            with pytest.raises(TimeoutError):
                connection.recv(1)
            connection.settimeout(3)
            connection.sendall(body)
            response = http.client.HTTPResponse(connection)
            response.begin()
            assert response.status == 403
            assert json.loads(response.read())["error"]["code"] == "forbidden"
        assert service.calls == []


def test_rejected_peer_does_not_wait_indefinitely_for_missing_body():
    with running_server() as (service, port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.putrequest("POST", "/api/sessions")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "2")
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 403
            assert json.loads(response.read())["error"]["code"] == "forbidden"
        finally:
            connection.close()
        assert service.calls == []


def test_rejected_peer_body_deadline_is_total_not_reset_by_each_byte():
    with running_server() as (service, port):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            headers = (
                "POST /api/sessions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 32\r\n\r\n"
            )
            connection.sendall(headers.encode("ascii"))
            stopped = threading.Event()
            sent = []

            def drip_body():
                for _ in range(32):
                    if stopped.wait(REJECT_BODY_TIMEOUT / 3):
                        return
                    connection.sendall(b"x")
                    sent.append(1)

            with ThreadPoolExecutor(max_workers=1) as pool:
                sender = pool.submit(drip_body)
                started = time.monotonic()
                try:
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    assert response.status == 403
                    assert json.loads(response.read())["error"]["code"] == "forbidden"
                    assert time.monotonic() - started < REJECT_BODY_TIMEOUT + 0.75
                    assert 1 <= len(sent) < 32
                finally:
                    stopped.set()
                sender.result(timeout=3)
        assert service.calls == []


def test_oversized_rejection_half_closes_response_before_draining_delayed_body():
    with running_server() as (service, port):
        body = b"x" * (MAX_BODY_BYTES + 1)
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            headers = (
                "POST /api/sessions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Origin: http://127.0.0.1:{port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            )
            connection.sendall(headers.encode("ascii"))
            response = http.client.HTTPResponse(connection)
            response.begin()
            assert response.status == 413
            assert response.getheader("Connection") == "close"
            assert json.loads(response.read())["error"]["code"] == "payload_too_large"
            assert connection.recv(1) == b""
            connection.sendall(body)
            connection.shutdown(socket.SHUT_WR)
        assert service.calls == []


def test_concurrent_peer_rejections_return_http_errors_without_connection_abort():
    with running_server() as (service, port):
        def reject(_):
            status, _, body = call(
                port, "POST", "/api/sessions/s1/pending-inputs/i1/continue",
                b'{"idempotency_key":"once","expected_session_revision":2}',
                {"Content-Type": "application/json"}, include_origin=False,
            )
            assert status == 403
            assert json.loads(body)["error"]["code"] == "forbidden"

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(reject, range(250)))
        assert service.calls == []


def test_concurrent_oversized_rejections_return_http_errors_without_connection_abort():
    with running_server() as (service, port):
        def reject(_):
            status, _, body = call(
                port, "POST", "/api/sessions/s1/runs/r1/retry-archive",
                b"x" * (MAX_BODY_BYTES + 1), {"Content-Type": "application/json"},
            )
            assert status == 413
            assert json.loads(body)["error"]["code"] == "payload_too_large"

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(reject, range(250)))
        assert service.calls == []


def test_submit_validation_and_service_error_are_redacted():
    with running_server() as (service, port):
        url = "/api/sessions/s1/inputs"
        for obj in ({"text": " ", "idempotency_key": "k"}, {"text": "x"}, {"text": "x", "idempotency_key": "k", "extra": 1}):
            status, _, _ = call(port, "POST", url, json.dumps(obj).encode(), {"Content-Type": "application/json"})
            assert status == 400
        assert service.calls == []
        service.rejected = True
        secret = "DEEPSEEK_API_KEY=private"
        body = json.dumps({"text": secret, "idempotency_key": "k"}).encode()
        status, _, response = call(port, "POST", url, body, {"Content-Type": "application/json"})
        assert status == 409
        assert secret.encode() not in response and b"secret" not in response


def test_explicit_contract_status():
    with running_server() as (service, port):
        def reject(_):
            exc = ContractValidationError("sensitive")
            exc.status_code = 404
            raise exc
        service.get_session = reject
        status, _, body = call(port, "GET", "/api/sessions/s1")
        assert status == 404 and b"sensitive" not in body


def test_mode_and_loopback_binding():
    service = FakeService()
    with create_server(service, port=0, mode="deepseek") as server:
        assert server.server_address[0] == "127.0.0.1"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert json.loads(call(server.server_address[1], "GET", "/api/health")[2])["mode"] == "deepseek"
        finally:
            server.shutdown()
            thread.join(timeout=3)


def test_active_session_route_rejects_stale_or_malformed_cas_before_service():
    with running_server() as (service, port):
        path = "/api/active-session"
        valid = {"workflow_session_id": "s1", "expected_selection_revision": 1,
                 "idempotency_key": "once"}
        for bad in (
            {key: value for key, value in valid.items() if key != "expected_selection_revision"},
            {**valid, "expected_selection_revision": 0},
            {**valid, "expected_selection_revision": True},
            {**valid, "workflow_session_id": "bad.id"},
            {**valid, "extra": 1},
        ):
            assert control_call(port, path, bad)[0] == 400
        assert service.calls == []
        assert control_call(port, path, valid)[0] == 200
        assert service.calls == [("switch_session", "s1", "once", 1)]


@pytest.mark.parametrize("action,path,payload,expected_status,expected_call", CONTROL_CASES)
def test_control_success_and_idempotent_replay(
    action, path, payload, expected_status, expected_call,
):
    with running_server() as (service, port):
        status, _, body = control_call(port, path, payload)
        assert status == expected_status, (action, status, body)
        receipt = json.loads(body)
        assert receipt == service.control_receipts[(action, "s1", "once")][1]
        assert service.calls == [expected_call]

        replay_payload = {**payload, next(
            field for field in payload if field.startswith("expected_")
        ): 99}
        status, _, body = control_call(port, path, replay_payload)
        assert status == expected_status, (action, status, body)
        assert json.loads(body) == receipt
        assert service.calls == [expected_call, (*expected_call[:-1], 99)]
        assert len(service.control_receipts) == 1


@pytest.mark.parametrize("action,path,payload,expected_status,expected_call", CONTROL_CASES)
def test_control_same_key_cannot_target_another_resource(
    action, path, payload, expected_status, expected_call,
):
    with running_server() as (service, port):
        assert control_call(port, path, payload)[0] == expected_status
        if action.startswith("create_"):
            conflicting_path = path
            conflicting_payload = {**payload, "visible_message_id": "m2"}
        elif action == "continue_pending_input":
            conflicting_path = path.replace("/i1/", "/i2/")
            conflicting_payload = payload
        else:
            conflicting_path = path.replace("/r1/", "/r2/")
            conflicting_payload = payload
        status, _, body = control_call(port, conflicting_path, conflicting_payload)
        assert status == 409, (action, status, body)
        assert json.loads(body)["error"]["code"] == "conflict"
        assert b"secret" not in body
        conflicting_target = "m2" if action.startswith("create_") else (
            "i2" if action == "continue_pending_input" else "r2"
        )
        assert service.calls == [
            expected_call, (*expected_call[:2], conflicting_target, *expected_call[3:]),
        ]
        assert len(service.control_receipts) == 1


@pytest.mark.parametrize("action,path,payload,expected_status,expected_call", CONTROL_CASES)
def test_control_state_conflict_is_redacted(
    action, path, payload, expected_status, expected_call,
):
    with running_server() as (service, port):
        service.control_rejected.add(action)
        status, _, body = control_call(port, path, payload)
        assert status == 409, (action, status, body)
        assert json.loads(body)["error"]["code"] == "conflict"
        assert b"secret" not in body
        assert service.calls == [expected_call]


@pytest.mark.parametrize("action,path,payload,expected_status,expected_call", CONTROL_CASES)
def test_control_rejects_invalid_json_fields_before_service(
    action, path, payload, expected_status, expected_call,
):
    revision_field = (
        "expected_source_revision" if action.startswith("create_")
        else "expected_session_revision"
    )
    bad_payloads = [
        {key: value for key, value in payload.items() if key != "idempotency_key"},
        {key: value for key, value in payload.items() if key != revision_field},
        {**payload, "idempotency_key": ""},
        {**payload, "idempotency_key": "x" * 129},
        {**payload, "idempotency_key": 42},
        {**payload, revision_field: True},
        {**payload, revision_field: 0},
        {**payload, revision_field: "2"},
        {**payload, "extra": "ignored?"},
    ]
    if action.startswith("create_"):
        bad_payloads.extend((
            {key: value for key, value in payload.items() if key != "visible_message_id"},
            {**payload, "visible_message_id": ""},
            {**payload, "visible_message_id": 42},
            {**payload, "visible_message_id": "bad.id"},
        ))
    with running_server() as (service, port):
        for invalid in bad_payloads:
            status, _, body = control_call(port, path, invalid)
            assert status == 400, (action, invalid, status, body)
        assert service.calls == []


@pytest.mark.parametrize("action,path,payload,expected_status,expected_call", CONTROL_CASES)
def test_control_rejects_malformed_json_and_media_type_before_service(
    action, path, payload, expected_status, expected_call,
):
    with running_server() as (service, port):
        for body, content_type, expected in (
            (b'{"idempotency_key":"x","idempotency_key":"y"}', "application/json", 400),
            (b"[]", "application/json", 400),
            (b"{", "application/json", 400),
            (b"{}", "text/plain", 415),
            (b"x" * 65537, "application/json", 413),
        ):
            status, _, response = call(
                port, "POST", path, body, {"Content-Type": content_type},
            )
            assert status == expected, (action, status, response)
        assert service.calls == []


@pytest.mark.parametrize("action,path,payload,expected_status,expected_call", CONTROL_CASES)
def test_control_method_path_and_peer_validation(
    action, path, payload, expected_status, expected_call,
):
    with running_server() as (service, port):
        assert call(port, "GET", path)[0] == 404
        invalid_paths = [
            path + "/extra", path + "?revision=2",
            path.replace("/s1/", "/bad.id/"),
            path.replace("/s1/", "/s1/../s1/"),
        ]
        if "/r1/" in path or "/i1/" in path:
            invalid_paths.append(path.replace("/r1/", "/bad.id/").replace("/i1/", "/bad.id/"))
        for invalid_path in invalid_paths:
            assert control_call(port, invalid_path, payload)[0] == 404, (action, invalid_path)
        assert control_call(port, path, payload, {"Host": "evil.example"})[0] == 403
        assert control_call(port, path, payload, {"Origin": "http://evil.example"})[0] == 403
        assert control_call(port, path, payload, {"Origin": ""})[0] == 403
        assert control_call(
            port, path, payload, {"Host": f"localhost:{port}"}
        )[0] == 403
        assert call(
            port, "POST", path, json.dumps(payload).encode(),
            {"Content-Type": "application/json"}, include_origin=False,
        )[0] == 403
        assert service.calls == []

        status, _, body = control_call(
            port, path, payload,
            {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"},
        )
        assert status == expected_status, (action, status, body)
        assert service.calls == [expected_call]
