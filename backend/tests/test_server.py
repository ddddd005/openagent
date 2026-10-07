"""Current HTTP safety boundaries independent of models or durable databases."""

import http.client
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from phase1_agent.graph_records import graph_error
from phase1_agent.server import MAX_BODY_BYTES, MAX_GRAPH_BODY_BYTES, REJECT_BODY_TIMEOUT, create_server


class FakeService:
    def __init__(self):
        self.calls = []
        self.rejected = None

    def dispatch(self, method, path, data):
        if path == "/api/graph/node-types" and method == "GET":
            self.calls.append(("catalog",))
            return 200, {"schema_version": 2, "node_types": []}
        if path == "/api/graph/commands" and method == "POST":
            if type(data) is not dict or set(data) != {"operation", "parameters"}:
                raise graph_error("invalid_request", "Application request fields differ")
            if self.rejected is not None:
                raise self.rejected
            self.calls.append(("command", data))
            return 200, {"result": "accepted"}
        raise graph_error("not_found", "Graph route not found", 404)


@contextmanager
def running_server(service=None):
    service = service or FakeService()
    with patch("phase1_agent.graph_http.dispatch_graph",
               side_effect=lambda target, method, path, data: target.dispatch(method, path, data)):
        server = create_server(service, port=0, graph_service=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield service, server.server_address[1]
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def call(port, method, path, body=None, headers=None, *, include_origin=True):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    request_headers = {
        "Host": f"127.0.0.1:{port}",
        **({"Origin": f"http://127.0.0.1:{port}"} if method == "POST" and include_origin else {}),
        **(headers or {}),
    }
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def command(port, value, **kwargs):
    return call(port, "POST", "/api/graph/commands", json.dumps(value).encode("utf-8"),
                {"Content-Type": "application/json"}, **kwargs)


def test_current_routes_and_static_allowlist_have_security_headers():
    with running_server() as (service, port):
        for path, marker in (
            ("/", b"<!doctype html>"), ("/static/index.html", b"<!doctype html>"),
            ("/static/chat-entry.js", b"/static/graph-chat.js"),
            ("/static/graph-chat-core.js", b"GraphChatClient"),
            ("/static/graph-chat.js", b"GraphChatClient"),
            ("/static/frontend-package-host.js", b"ConsumerFrontendHost"),
            ("/static/frontend-package.js", b"WorkflowFrontendPackage"),
            ("/static/style.css", b".graph-actions"),
        ):
            status, headers, body = call(port, "GET", path)
            assert status == 200 and marker in body
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert headers["Cache-Control"] == "no-store"
            assert headers["Referrer-Policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert json.loads(call(port, "GET", "/api/health")[2]) == {"status": "ok"}
        assert service.calls == []
        assert call(port, "GET", "/api/graph/node-types")[0] == 200
        assert command(port, {"operation": "example.current", "parameters": {}})[0] == 200
        assert service.calls == [("catalog",), ("command", {
            "operation": "example.current", "parameters": {},
        })]


@pytest.mark.parametrize("path", [
    "/static/app.js", "/static/../contract_errors.py", "/static/secret",
    "/api/sessions", "/api/sessions/s1", "/api/sessions/s1/inputs",
    "/api/active-session", "/api/operations", "/api/prompt-configs/config",
    "/api/model-configurations/provider", "/api/exposure-configurations",
    "/api/global-content", "/api/session-data/definitions", "/api/legacy/queries",
])
def test_retired_paths_have_no_read_or_write_fallback(path):
    with running_server() as (service, port):
        assert call(port, "GET", path)[0] == 404
        assert call(port, "POST", path, b"{}", {"Content-Type": "application/json"})[0] == 404
        assert service.calls == []


def test_host_origin_and_strict_json_are_checked_before_current_dispatch():
    with running_server() as (service, port):
        assert call(port, "GET", "/", headers={"Host": "evil.example"})[0] == 403
        assert call(port, "GET", "/", headers={"Origin": "http://evil.example"})[0] == 403
        for origin in ("http://evil.example", ""):
            assert call(port, "POST", "/api/graph/commands", b"{}", {
                "Content-Type": "application/json", "Origin": origin,
            })[0] == 403
        for body, content_type, status in (
            (b"{}", "text/plain", 415),
            (b'{"x":1,"x":2}', "application/json", 400),
            (b'{"x":1}', "application/json", 400),
            (b"[]", "application/json", 400),
            (b"\xff", "application/json", 400),
            (b"", "application/json", 400),
        ):
            assert call(port, "POST", "/api/graph/commands", body,
                        {"Content-Type": content_type})[0] == status
        assert service.calls == []


def test_missing_origin_and_oversized_content_length_are_rejected():
    with running_server() as (service, port):
        assert call(port, "POST", "/api/graph/commands", b"{}",
                    {"Content-Type": "application/json"}, include_origin=False)[0] == 403
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.putrequest("POST", "/api/graph/commands")
            connection.putheader("Origin", f"http://127.0.0.1:{port}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "9" * 100)
            connection.endheaders()
            assert connection.getresponse().status == 413
        finally:
            connection.close()
        assert service.calls == []


def test_rejected_peer_consumes_a_bounded_delayed_body_before_closing():
    with running_server() as (service, port):
        body = b'{"operation":"example.current","parameters":{}}'
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            connection.sendall((
                "POST /api/graph/commands HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii"))
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


def test_rejected_peer_body_deadline_is_total_not_reset_by_each_byte():
    with running_server() as (service, port):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            connection.sendall((
                "POST /api/graph/commands HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                "Content-Length: 32\r\n\r\n"
            ).encode("ascii"))
            stopped, sent = threading.Event(), []

            def drip():
                for _ in range(32):
                    if stopped.wait(REJECT_BODY_TIMEOUT / 3):
                        return
                    connection.sendall(b"x")
                    sent.append(1)

            with ThreadPoolExecutor(max_workers=1) as pool:
                sender = pool.submit(drip)
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


def test_oversized_rejection_half_closes_before_bounded_delayed_body():
    with running_server() as (service, port):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            connection.sendall((
                "POST /api/graph/commands HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {MAX_GRAPH_BODY_BYTES + 1}\r\n\r\n"
            ).encode("ascii"))
            response = http.client.HTTPResponse(connection)
            response.begin()
            assert response.status == 413
            assert response.getheader("Connection") == "close"
            assert json.loads(response.read())["error"]["code"] == "payload_too_large"
            assert connection.recv(1) == b""
            connection.sendall(b"x" * (MAX_BODY_BYTES + 1))
            connection.shutdown(socket.SHUT_WR)
        assert service.calls == []


def test_concurrent_peer_and_size_rejections_return_http_errors():
    with running_server() as (service, port):
        def reject(index):
            if index % 2:
                status, _, body = call(port, "POST", "/api/graph/commands", b"{}",
                    {"Content-Type": "application/json"}, include_origin=False)
                assert status == 403 and json.loads(body)["error"]["code"] == "forbidden"
            else:
                status, _, body = call(port, "POST", "/api/graph/commands", b"x" * MAX_BODY_BYTES,
                    {"Content-Type": "application/json", "Content-Length": str(MAX_GRAPH_BODY_BYTES + 1)})
                assert status == 413 and json.loads(body)["error"]["code"] == "payload_too_large"

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(reject, range(32)))
        assert service.calls == []


def test_current_error_status_and_internal_redaction_are_preserved():
    with running_server() as (service, port):
        service.rejected = graph_error("stale_revision", "Graph revision changed", 409)
        status, _, body = command(port, {"operation": "example.current", "parameters": {}})
        assert status == 409 and json.loads(body)["error"]["reason_code"] == "stale_revision"
        service.rejected = RuntimeError("DEEPSEEK_API_KEY=private")
        status, _, body = command(port, {"operation": "example.current", "parameters": {}})
        assert status == 500 and b"private" not in body and b"DEEPSEEK_API_KEY" not in body
        assert json.loads(body)["error"]["reason_code"] == "storage_error"
        assert service.calls == []
