"""Loopback-only HTTP facade for the current graph application."""

from __future__ import annotations

import argparse
import json
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .contract_errors import ContractValidationError
from .contract_json import loads_strict


MAX_BODY_BYTES = 64 * 1024
MAX_GRAPH_BODY_BYTES = 4 * 1024 * 1024
REJECT_BODY_TIMEOUT = 0.25
REJECT_DRAIN_BYTES = 2 * MAX_BODY_BYTES
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/chat-entry.js": ("chat-entry.js", "text/javascript; charset=utf-8"),
    "/static/graph-chat-core.js": ("graph-chat-core.js", "text/javascript; charset=utf-8"),
    "/static/graph-chat.js": ("graph-chat.js", "text/javascript; charset=utf-8"),
    "/static/frontend-package-host.js": ("frontend-package-host.js", "text/javascript; charset=utf-8"),
    "/static/frontend-package.js": ("frontend-package.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
_USER_UI_QUERY_FIELDS = frozenset({"graph_workflow", "graph_session"})
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_WORKBENCH_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def _workbench_files(directory: str | Path | None) -> dict[str, tuple[bytes, str]]:
    if directory is None:
        return {}
    root = Path(directory).resolve(strict=True)
    if not root.is_dir() or not (root / "index.html").is_file():
        raise ValueError("Workbench build needs an index.html file")
    files = {}
    for path in [root / "index.html", *sorted((root / "assets").rglob("*"))]:
        if not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Workbench build cannot contain linked files")
        relative = path.relative_to(root).as_posix()
        if relative == "index.html":
            content_type = "text/html; charset=utf-8"
        elif path.suffix in _WORKBENCH_CONTENT_TYPES:
            content_type = _WORKBENCH_CONTENT_TYPES[path.suffix]
        else:
            continue
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in (".", "..")
               for part in relative.split("/")):
            raise ValueError("Workbench build contains an invalid asset name")
        files["/workbench/" if relative == "index.html" else "/workbench/" + relative] = (
            path.read_bytes(), content_type,
        )
    return files


def _valid_user_ui_query(query: str) -> bool:
    if not query or len(query) > 1024:
        return False
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          errors="strict", max_num_fields=len(_USER_UI_QUERY_FIELDS))
    except (ValueError, UnicodeError):
        return False
    values = dict(pairs)
    return (len(values) == len(pairs) and values.keys() <= _USER_UI_QUERY_FIELDS
            and _UUID4.fullmatch(values.get("graph_workflow", "")) is not None
            and ("graph_session" not in values or _UUID4.fullmatch(values["graph_session"]) is not None))


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class _RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def create_server(service: Any, port: int = 8765, *, graph_service=None,
                  workbench_dist: str | Path | None = None) -> ThreadingHTTPServer:
    """Bind only to IPv4 loopback; pages and receipt reads do not initialize a runtime."""
    workbench_files = _workbench_files(workbench_dist)
    graph_services = [graph_service]
    graph_owned = [False]
    graph_lock = RLock()

    def get_graph_service():
        from .graph_service import GraphWorkflowService
        from .workflow_host import WorkflowHost

        with graph_lock:
            if graph_services[0] is None:
                if isinstance(service, WorkflowHost):
                    graph_services[0] = service.graph_service
                elif isinstance(service, GraphWorkflowService):
                    graph_services[0] = service
                else:
                    graph_services[0] = GraphWorkflowService(service.database)
                    graph_owned[0] = True
            return graph_services[0]

    class GraphHTTPServer(LocalHTTPServer):
        def server_close(self):
            super().server_close()
            if graph_owned[0] and graph_services[0] is not None:
                graph_services[0].close()
                graph_owned[0] = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _send(self, status: int, body: bytes, content_type: str, *, workbench: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if status >= 400:
                self.send_header("Connection", "close")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
                + ("; style-src-attr 'unsafe-inline'; img-src 'self' data:; font-src 'self'"
                   if workbench else ""),
            )
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str,
                   reason_code: str | None = None, diagnostics: list | None = None) -> None:
            self._rejection_deadline = time.monotonic() + REJECT_BODY_TIMEOUT
            self._discard_rejected_body()
            error = {"code": code, "message": message}
            if reason_code is not None:
                error["reason_code"] = reason_code
            if diagnostics is not None:
                error["diagnostics"] = diagnostics
            self._json(status, {"error": error})
            self._finish_rejection()

        def _discard_rejected_body(self) -> None:
            # Closing with unread body bytes can reset TCP before rejection arrives.
            if self.headers.get("Transfer-Encoding") is not None:
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or len(lengths[0]) > 20
                    or re.fullmatch(r"[0-9]+", lengths[0]) is None):
                return
            size = int(lengths[0])
            remaining = size - getattr(self, "_body_bytes_read", 0)
            if remaining <= 0 or size > MAX_BODY_BYTES:
                return
            timeout = self.connection.gettimeout()
            deadline = self._rejection_deadline
            try:
                while remaining:
                    wait = deadline - time.monotonic()
                    if wait <= 0:
                        break
                    self.connection.settimeout(wait)
                    data = self.rfile.read1(min(remaining, 8192))
                    if not data:
                        break
                    remaining -= len(data)
                    self._body_bytes_read = getattr(self, "_body_bytes_read", 0) + len(data)
            except (TimeoutError, OSError):
                pass
            finally:
                self.connection.settimeout(timeout)

        def _finish_rejection(self) -> None:
            lengths = self.headers.get_all("Content-Length", [])
            known_size = None
            if (len(lengths) == 1 and len(lengths[0]) <= 20
                    and re.fullmatch(r"[0-9]+", lengths[0]) is not None
                    and self.headers.get("Transfer-Encoding") is None):
                known_size = int(lengths[0])
            read = getattr(self, "_body_bytes_read", 0)
            if known_size is not None and read >= known_size:
                return
            if not lengths and self.headers.get("Transfer-Encoding") is None:
                return
            self.close_connection = True
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                return
            remaining = REJECT_DRAIN_BYTES - read
            if known_size is not None:
                remaining = min(remaining, known_size - read)
            timeout = self.connection.gettimeout()
            try:
                while remaining > 0:
                    wait = self._rejection_deadline - time.monotonic()
                    if wait <= 0:
                        break
                    self.connection.settimeout(wait)
                    data = self.rfile.read1(min(remaining, 8192))
                    if not data:
                        break
                    remaining -= len(data)
            except (TimeoutError, OSError):
                pass
            finally:
                self.connection.settimeout(timeout)

        def _validate_peer(self, require_origin: bool) -> None:
            host_values = self.headers.get_all("Host", [])
            port_number = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port_number}", f"localhost:{port_number}"}
            if len(host_values) != 1 or host_values[0] not in allowed:
                raise _RequestError(403, "forbidden", "仅允许本机访问")
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1 or (require_origin and not origins):
                raise _RequestError(403, "forbidden", "来源不受信任")
            if origins and origins[0] != f"http://{host_values[0]}":
                raise _RequestError(403, "forbidden", "来源不受信任")

        def _path(self) -> str:
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.fragment:
                raise _RequestError(404, "not_found", "未找到接口")
            if target.path != self.path and not (
                self.command == "GET" and target.path in ("/", "/static/index.html")
                and _valid_user_ui_query(target.query)
            ):
                raise _RequestError(404, "not_found", "未找到接口")
            return target.path

        def _read_json(self, max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
            if self.headers.get("Transfer-Encoding") is not None:
                raise _RequestError(400, "bad_request", "不支持分块请求")
            content_types = self.headers.get_all("Content-Type", [])
            if len(content_types) != 1 or content_types[0].lower() not in (
                "application/json", "application/json; charset=utf-8",
            ):
                raise _RequestError(415, "unsupported_media_type", "需要 application/json")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
                raise _RequestError(411, "length_required", "需要明确请求长度")
            if len(lengths[0]) > 20:
                raise _RequestError(413, "payload_too_large", "请求内容过大")
            size = int(lengths[0])
            if size > max_bytes:
                raise _RequestError(413, "payload_too_large", "请求内容过大")
            if size == 0:
                raise _RequestError(400, "bad_request", "请求体不能为空")
            raw = self.rfile.read(size)
            self._body_bytes_read = len(raw)
            try:
                data = loads_strict(raw.decode("utf-8"))
            except (UnicodeError, ContractValidationError):
                raise _RequestError(400, "bad_request", "JSON 格式无效") from None
            if type(data) is not dict:
                raise _RequestError(400, "bad_request", "需要 JSON 对象")
            return data

        def _dispatch(self, method: str) -> None:
            graph_request = False
            try:
                self._validate_peer(require_origin=method == "POST")
                path = self._path()
                graph_request = path.startswith("/api/graph/")
                if graph_request:
                    data = self._read_json(MAX_GRAPH_BODY_BYTES) if method == "POST" else None
                    receipt_scope = {
                        "/api/graph/receipts/read": "management",
                        "/api/graph/consumer/receipts/read": "consumer",
                    }.get(path)
                    receipt_request = data
                    if method == "POST" and path in ("/api/graph/queries", "/api/graph/consumer/queries") \
                            and type(data) is dict and data.get("operation") == "receipt.read":
                        from .graph_records import require
                        require(set(data) == {"operation", "parameters"},
                                "invalid_request", "Application request fields differ")
                        receipt_scope = "consumer" if "/consumer/" in path else "management"
                        receipt_request = data["parameters"]
                    if receipt_scope is not None:
                        from .graph_records import require
                        from .graph_receipts import read_graph_application_receipt
                        require(method == "POST", "not_found", "Receipt route not found", 404)
                        require(type(receipt_request) is dict
                                and set(receipt_request) == {"operation", "parameters"},
                                "invalid_request", "Receipt request fields differ")
                        database = graph_services[0].database if graph_services[0] is not None else service.database
                        result = read_graph_application_receipt(
                            database, receipt_request["operation"], receipt_request["parameters"],
                            scope=receipt_scope,
                        )
                        self._json(200, result)
                        return
                    from .graph_http import dispatch_graph
                    status, result = dispatch_graph(get_graph_service(), method, path, data)
                    self._json(status, result)
                    return
                if method == "GET":
                    if path in workbench_files:
                        body, content_type = workbench_files[path]
                        self._send(200, body, content_type, workbench=True)
                        return
                    if path in _STATIC_FILES:
                        name, content_type = _STATIC_FILES[path]
                        body = resources.files("phase1_agent").joinpath("static", name).read_bytes()
                        self._send(200, body, content_type)
                        return
                    if path == "/api/health":
                        self._json(200, {"status": "ok"})
                        return
                raise _RequestError(404, "not_found", "未找到接口")
            except _RequestError as exc:
                self._error(exc.status, exc.code, exc.message,
                            "invalid_request" if graph_request or self.path.startswith("/api/graph/") else None,
                            diagnostics=[] if graph_request or self.path.startswith("/api/graph/") else None)
            except ContractValidationError as exc:
                status = getattr(exc, "status_code", 400)
                if status not in (400, 403, 404, 409, 500, 503):
                    status = 400
                code = ("internal_error" if status >= 500 else "conflict" if status == 409
                        else "not_found" if status == 404 else "forbidden" if status == 403 else "bad_request")
                self._error(status, code, "Graph request failed" if status >= 500 else str(exc),
                            getattr(exc, "reason_code", "invalid_request"),
                            diagnostics=getattr(exc, "diagnostics", []))
            except Exception:
                self._error(500, "internal_error", "服务暂不可用",
                            "storage_error" if graph_request else None,
                            diagnostics=[] if graph_request else None)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return GraphHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="本机工作流页面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", required=True)
    parser.add_argument("--workbench-dist", type=Path,
                        help="Serve a production build at /workbench/ on the same loopback origin")
    args = parser.parse_args(argv)
    from .workflow_host import WorkflowHost

    service = WorkflowHost(database_path=args.database)
    try:
        try:
            server = create_server(service, port=args.port, workbench_dist=args.workbench_dist)
        except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
            parser.error(f"Invalid workbench build: {exc}")
        with server:
            print(f"http://127.0.0.1:{server.server_address[1]}/", flush=True)
            if args.workbench_dist is not None:
                print(f"http://127.0.0.1:{server.server_address[1]}/workbench/", flush=True)
            server.serve_forever()
    finally:
        service.close()


if __name__ == "__main__":
    main()
