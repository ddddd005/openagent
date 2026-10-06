"""Loopback-only HTTP facade for the workflow service."""

from __future__ import annotations

import argparse
import json
import re
import socket
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from threading import RLock
from urllib.parse import parse_qsl, urlsplit

from .contract_errors import ContractValidationError
from .contract_json import loads_strict
from .prompt_selection import validate_prompt_selection
from .workflow_operations import operation_http_status
from .workflow_context_view import validate_context_request
from .model_configuration import validate_configuration
from .model_selection import validate_model_selection
from .prompt_errors import PromptProcessingError


MAX_BODY_BYTES = 64 * 1024
MAX_GRAPH_BODY_BYTES = 4 * 1024 * 1024
REJECT_BODY_TIMEOUT = 0.25
REJECT_DRAIN_BYTES = 2 * MAX_BODY_BYTES
_ID = r"([A-Za-z0-9_-]{1,128})"
_SESSION_PATH = re.compile(rf"/api/sessions/{_ID}")
_INPUT_PATH = re.compile(rf"/api/sessions/{_ID}/inputs")
_NODE_CONTEXT_PATH = re.compile(rf"/api/sessions/{_ID}/nodes/{_ID}/context/(read|preview)")
_SESSION_VARIABLE_PATH = re.compile(rf"/api/sessions/{_ID}/variables/(read|write)")
_RUN_ACTION_PATH = re.compile(rf"/api/sessions/{_ID}/runs/{_ID}/(retry-archive|retry-publish)")
_RUN_CONTROL_PATH = re.compile(rf"/api/sessions/{_ID}/runs/{_ID}/(interrupt|resume)")
_RUN_BUDGET_PATH = re.compile(rf"/api/sessions/{_ID}/runs/{_ID}/extend_budget")
_CHAIN_REROLL_PATH = re.compile(rf"/api/sessions/{_ID}/chains/{_ID}/reroll")
_CHAIN_RECOVERY_PATH = re.compile(
    rf"/api/sessions/{_ID}/chains/{_ID}/(close_execution|continue_workflow)"
)
_BRANCH_PATH = re.compile(rf"/api/sessions/{_ID}/branches(?:/(switch))?")
_PENDING_INPUT_PATH = re.compile(rf"/api/sessions/{_ID}/pending-inputs/{_ID}/continue")
_CANDIDATE_SELECT_PATH = re.compile(rf"/api/sessions/{_ID}/candidates/{_ID}/select")
_PROMPT_COLLECTION_PATH = re.compile(r"/api/prompt-configs/(item|group|config)")
_PROMPT_HEAD_PATH = re.compile(rf"/api/prompt-configs/(item|group|config)/{_ID}")
_PROMPT_REVISION_PATH = re.compile(
    rf"/api/prompt-configs/(item|group|config)/{_ID}/revisions/([1-9][0-9]{{0,9}})"
)
_PROMPT_DELETE_PATH = re.compile(rf"/api/prompt-configs/(item|group|config)/{_ID}/delete")
_PROMPT_RESOLVE_PATH = re.compile(
    rf"/api/prompt-configs/config/{_ID}/revisions/([1-9][0-9]{{0,9}})/resolve"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MODEL_COLLECTION_PATH = re.compile(r"/api/model-configurations/(provider|model)")
_EXPOSURE_HEAD_PATH = re.compile(rf"/api/exposure-configurations/{_ID}")
_EXPOSURE_REVISION_PATH = re.compile(rf"/api/exposure-configurations/{_ID}/revisions/([1-9][0-9]{{0,15}})")
_EXPOSURE_READ_PATH = re.compile(rf"/api/sessions/{_ID}/exposures/{_ID}/revisions/([1-9][0-9]{{0,15}})")
_MODEL_HEAD_PATH = re.compile(rf"/api/model-configurations/(provider|model)/{_ID}")
_MODEL_REVISION_PATH = re.compile(
    rf"/api/model-configurations/(provider|model)/{_ID}/revisions/([1-9][0-9]{{0,15}})"
)
_MODEL_DIAGNOSE_PATH = re.compile(
    rf"/api/model-configurations/model/{_ID}/revisions/([1-9][0-9]{{0,15}})/diagnostics"
)
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/chat-entry.js": ("chat-entry.js", "text/javascript; charset=utf-8"),
    "/static/graph-chat-core.js": ("graph-chat-core.js", "text/javascript; charset=utf-8"),
    "/static/graph-chat.js": ("graph-chat.js", "text/javascript; charset=utf-8"),
    "/static/frontend-package-host.js": ("frontend-package-host.js", "text/javascript; charset=utf-8"),
    "/static/frontend-package.js": ("frontend-package.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
_USER_UI_QUERY_FIELDS = frozenset({
    "session", "prompt_a", "prompt_a_revision", "prompt_b", "prompt_b_revision",
    "model_config", "model_revision", "exposure_config", "exposure_revision",
    "graph_workflow", "graph_session",
})
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_PROCESSING_CODES = frozenset({
    "invalid_preparation_program", "macro_input_limit", "macro_output_limit", "macro_substitution_limit",
    "missing_macro_variable", "preparation_evidence_limit", "prompt_capacity_exceeded",
    "prompt_pipeline_limit", "prompt_selector_missing", "prompt_variables_missing",
    "variable_not_registered", "variable_type_conflict", "variable_type_mismatch", "variable_value_limit",
    "regex_input_limit", "regex_invalid_flags", "regex_invalid_input", "regex_invalid_limits",
    "regex_invalid_pattern", "regex_invalid_replacement", "regex_invalid_rule", "regex_match_limit",
    "regex_output_limit", "regex_rule_limit", "regex_timeout", "regex_worker_failed",
})


def _valid_user_ui_query(query: str) -> bool:
    if not query or len(query) > 1024:
        return False
    try:
        pairs = parse_qsl(
            query, keep_blank_values=True, strict_parsing=True, errors="strict",
            max_num_fields=len(_USER_UI_QUERY_FIELDS),
        )
    except (ValueError, UnicodeError):
        return False
    values = dict(pairs)
    if len(values) != len(pairs) or not values.keys() <= _USER_UI_QUERY_FIELDS:
        return False
    if "graph_workflow" in values or "graph_session" in values:
        return (values.keys() <= {"graph_workflow", "graph_session"}
                and _UUID4.fullmatch(values.get("graph_workflow", "")) is not None
                and ("graph_session" not in values or _UUID4.fullmatch(values["graph_session"]) is not None))
    if _UUID4.fullmatch(values.get("session", "")) is None:
        return False
    for config, revision in (
        ("prompt_a", "prompt_a_revision"), ("prompt_b", "prompt_b_revision"),
        ("model_config", "model_revision"),
        ("exposure_config", "exposure_revision"),
    ):
        if (config in values) != (revision in values):
            return False
        if config in values and (
            _UUID4.fullmatch(values[config]) is None
            or re.fullmatch(r"[1-9][0-9]{0,15}", values[revision]) is None
            or int(values[revision]) > 2**53 - 1
        ):
            return False
    return True


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class _RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def create_server(service: Any, port: int = 8765, mode: str = "offline", *, graph_service=None) -> ThreadingHTTPServer:
    """Bind only to IPv4 loopback; the service owns workflow execution."""

    graph_services = [graph_service]
    graph_owned = [False]
    graph_lock = RLock()

    def get_graph_service():
        from .workflow_host import WorkflowHost
        with graph_lock:
            if graph_services[0] is None:
                if isinstance(service, WorkflowHost):
                    graph_services[0] = service.graph_service
                else:
                    from .graph_service import GraphWorkflowService
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
            # Request paths and exception text may contain user data.
            pass

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
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
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _error(
            self, status: int, code: str, message: str, reason_code: str | None = None,
            diagnostic: dict | None = None,
            diagnostics: list | None = None,
        ) -> None:
            self._rejection_deadline = time.monotonic() + REJECT_BODY_TIMEOUT
            self._discard_rejected_body()
            error = {"code": code, "message": message}
            if reason_code is not None:
                error["reason_code"] = reason_code
            if diagnostic is not None:
                error["diagnostic"] = diagnostic
            if diagnostics is not None:
                error["diagnostics"] = diagnostics
            self._json(status, {"error": error})
            self._finish_rejection()

        def _discard_rejected_body(self) -> None:
            # Closing with unread body bytes can reset TCP before the rejection arrives.
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
                "application/json",
                "application/json; charset=utf-8",
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

        def _read_control_json(
            self, revision_field: str, identifier_field: str | None = None,
            extra_revision_field: str | None = None,
            optional_identifier_field: str | None = None,
        ) -> dict[str, Any]:
            data = self._read_json()
            expected = {"idempotency_key", revision_field}
            if identifier_field is not None:
                expected.add(identifier_field)
            if extra_revision_field is not None:
                expected.add(extra_revision_field)
            allowed = expected | ({optional_identifier_field} if optional_identifier_field else set())
            key = data.get("idempotency_key")
            revision = data.get(revision_field)
            extra_revision = data.get(extra_revision_field) if extra_revision_field is not None else None
            if (
                not expected <= set(data) <= allowed
                or type(key) is not str
                or not 1 <= len(key) <= 128
                or type(revision) is not int
                or revision < 1
                or (
                    extra_revision_field is not None
                    and (type(extra_revision) is not int or extra_revision < 1)
                )
                or (
                    identifier_field is not None
                    and (
                        type(data[identifier_field]) is not str
                        or _IDENTIFIER.fullmatch(data[identifier_field]) is None
                    )
                )
                or (
                    optional_identifier_field in data
                    and (
                        type(data[optional_identifier_field]) is not str
                        or _IDENTIFIER.fullmatch(data[optional_identifier_field]) is None
                    )
                )
            ):
                raise _RequestError(400, "bad_request", "提交字段无效")
            return data

        def _read_budget_json(self) -> dict[str, Any]:
            data = self._read_json()
            expected = {
                "idempotency_key", "expected_session_revision", "expected_run_revision",
                "additional_model_requests", "additional_model_attempts",
            }
            if (
                set(data) != expected
                or type(data["idempotency_key"]) is not str
                or not 1 <= len(data["idempotency_key"]) <= 128
                or any(
                    type(data[field]) is not int or data[field] < 1
                    for field in ("expected_session_revision", "expected_run_revision")
                )
                or type(data["additional_model_requests"]) is not int
                or not 0 <= data["additional_model_requests"] <= 64
                or type(data["additional_model_attempts"]) is not int
                or not 0 <= data["additional_model_attempts"] <= 256
                or not (data["additional_model_requests"] or data["additional_model_attempts"])
            ):
                raise _RequestError(400, "bad_request", "提交字段无效")
            return data

        def _read_prompt_mutation(self, *, write: bool) -> dict[str, Any]:
            data = self._read_json()
            expected = {"expected_revision", "idempotency_key"}
            if write:
                expected.add("record")
            if (
                set(data) != expected
                or type(data["expected_revision"]) is not int
                or data["expected_revision"] < (0 if write else 1)
                or type(data["idempotency_key"]) is not str
                or not 1 <= len(data["idempotency_key"]) <= 128
                or not data["idempotency_key"].strip()
                or (write and type(data["record"]) is not dict)
            ):
                raise _RequestError(400, "bad_request", "提示词配置字段无效")
            return data

        def _dispatch(self, method: str) -> None:
            try:
                self._validate_peer(require_origin=method == "POST")
                path = self._path()
                if path.startswith("/api/graph/"):
                    from .graph_http import dispatch_graph
                    data = self._read_json(MAX_GRAPH_BODY_BYTES) if method == "POST" else None
                    status, result = dispatch_graph(get_graph_service(), method, path, data)
                    self._json(status, result)
                    return
                context_request = _NODE_CONTEXT_PATH.fullmatch(path) is not None
                resource_request = path.startswith(("/api/global-content", "/api/session-data")) or bool(
                    re.fullmatch(rf"/api/sessions/{_ID}/(?:copy|data/(?:read|write|revision)|outputs/(?:read|public))", path)
                )
                if method == "GET":
                    if path == "/api/global-content":
                        self._json(200, service.list_global_content())
                        return
                    if path == "/api/session-data/definitions":
                        self._json(200, service.list_session_data_definitions())
                        return
                    match = re.fullmatch(rf"/api/sessions/{_ID}/outputs/public", path)
                    if match:
                        self._json(200, service.list_public_node_outputs(match.group(1)))
                        return
                    match = _EXPOSURE_READ_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.read_exposures(match.group(1), match.group(2), int(match.group(3))))
                        return
                    match = _EXPOSURE_REVISION_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.get_exposure_configuration(match.group(1), int(match.group(2))))
                        return
                    match = _EXPOSURE_HEAD_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.get_exposure_configuration(match.group(1)))
                        return
                    if path in _STATIC_FILES:
                        name, content_type = _STATIC_FILES[path]
                        data = resources.files("phase1_agent").joinpath("static", name).read_bytes()
                        self._send(200, data, content_type)
                        return
                    if path == "/api/health":
                        self._json(200, {"status": "ok", "mode": mode})
                        return
                    if path == "/api/sessions":
                        self._json(200, service.list_sessions())
                        return
                    if path == "/api/active-session":
                        self._json(200, service.get_active_session())
                        return
                    if path == "/api/model-configurations/provider":
                        self._json(200, service.list_model_providers())
                        return
                    match = _MODEL_DIAGNOSE_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.diagnose_model_configuration(
                            match.group(1), int(match.group(2)),
                        ))
                        return
                    match = _MODEL_REVISION_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.get_model_configuration(
                            match.group(1), match.group(2), int(match.group(3)),
                        ))
                        return
                    match = _MODEL_HEAD_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.get_model_configuration(match.group(1), match.group(2)))
                        return
                    match = _PROMPT_COLLECTION_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.list_prompt_configs(match.group(1)))
                        return
                    match = _PROMPT_REVISION_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.get_prompt_config_revision(
                            match.group(1), match.group(2), int(match.group(3)),
                        ))
                        return
                    match = _PROMPT_HEAD_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.get_prompt_config_head(
                            match.group(1), match.group(2),
                        ))
                        return
                    match = _SESSION_PATH.fullmatch(path)
                    if match:
                        self._json(200, service.get_session(match.group(1)))
                        return
                elif method == "POST":
                    if path == "/api/global-content":
                        self._json(201, service.save_global_content(**self._read_prompt_mutation(write=True)))
                        return
                    if path == "/api/global-content/resolve":
                        data = self._read_json()
                        if set(data) != {"resource_ids"}:
                            raise _RequestError(400, "bad_request", "内容引用字段无效")
                        self._json(200, service.resolve_global_content(data["resource_ids"]))
                        return
                    match = re.fullmatch(rf"/api/global-content/{_ID}/delete", path)
                    if match:
                        data = self._read_json()
                        if set(data) != {"expected_revision"}:
                            raise _RequestError(400, "bad_request", "内容删除字段无效")
                        self._json(200, service.delete_global_content(match.group(1), **data))
                        return
                    match = re.fullmatch(rf"/api/sessions/{_ID}/copy", path)
                    if match:
                        data = self._read_json()
                        if not {"expected_source_revision", "expected_data_revision", "idempotency_key"} <= set(data) <= {
                            "expected_source_revision", "expected_data_revision", "idempotency_key", "target_workflow_id",
                        }:
                            raise _RequestError(400, "bad_request", "会话复制字段无效")
                        self._json(201, service.copy_workbench_session(match.group(1), **data))
                        return
                    match = re.fullmatch(rf"/api/sessions/{_ID}/data/(read|write|revision)", path)
                    if match:
                        data = self._read_json()
                        if match.group(2) == "revision":
                            if data:
                                raise _RequestError(400, "bad_request", "版本读取只接受空对象")
                            with service._lock, closing(service._store()) as store:
                                service._session(store, match.group(1))
                                state = service._program_current_state(store, match.group(1))
                                self._json(200, {"revision": state["revision"]})
                            return
                        expected = {"definition_id", "revision", "public"} if match.group(2) == "read" else {
                            "definition_id", "revision", "value", "expected_revision",
                            "expected_session_revision", "idempotency_key",
                        }
                        if set(data) != expected:
                            raise _RequestError(400, "bad_request", "会话数据字段无效")
                        operation = service.read_session_data if match.group(2) == "read" else service.write_session_data
                        self._json(200, operation(match.group(1), **data))
                        return
                    match = re.fullmatch(rf"/api/sessions/{_ID}/outputs/read", path)
                    if match:
                        data = self._read_json()
                        if not {"node_id"} <= set(data) <= {
                            "node_id", "run_id", "public", "exposure_configuration",
                        }:
                            raise _RequestError(400, "bad_request", "输出读取字段无效")
                        self._json(200, service.read_node_outputs(match.group(1), **data))
                        return
                    if path == "/api/exposure-configurations":
                        data = self._read_prompt_mutation(write=True)
                        self._json(201, service.save_exposure_configuration(**data))
                        return
                    match = _MODEL_COLLECTION_PATH.fullmatch(path)
                    if match:
                        data = self._read_prompt_mutation(write=True)
                        data["record"] = validate_configuration(match.group(1), data["record"])
                        self._json(201, service.save_model_configuration(match.group(1), **data))
                        return
                    match = _NODE_CONTEXT_PATH.fullmatch(path)
                    if match:
                        preview = match.group(3) == "preview"
                        data = validate_context_request(self._read_json(), preview=preview)
                        operation = service.preview_node_context if preview else service.read_node_context
                        self._json(200, operation(match.group(1), match.group(2), **data))
                        return
                    match = _SESSION_VARIABLE_PATH.fullmatch(path)
                    if match:
                        context_request = True
                        data = self._read_json()
                        operation = (service.read_session_variables if match.group(2) == "read"
                                     else service.write_session_variable)
                        self._json(200, operation(match.group(1), **data))
                        return
                    match = _PROMPT_COLLECTION_PATH.fullmatch(path)
                    if match:
                        data = self._read_prompt_mutation(write=True)
                        self._json(201, service.save_prompt_config(match.group(1), **data))
                        return
                    match = _PROMPT_DELETE_PATH.fullmatch(path)
                    if match:
                        data = self._read_prompt_mutation(write=False)
                        self._json(200, service.delete_prompt_config(
                            match.group(1), match.group(2), **data,
                        ))
                        return
                    match = _PROMPT_RESOLVE_PATH.fullmatch(path)
                    if match:
                        if self._read_json():
                            raise _RequestError(400, "bad_request", "配置解析只接受空对象")
                        self._json(200, service.resolve_prompt_config(
                            match.group(1), int(match.group(2)),
                        ))
                        return
                    if path == "/api/operations":
                        data = self._read_json()
                        result = service.dispatch_operation(data)
                        self._json(operation_http_status(result["kind"]), result)
                        return
                    if path == "/api/sessions":
                        data = self._read_json()
                        if set(data) not in (set(), {"workflow_id"}):
                            raise _RequestError(400, "bad_request", "新建会话字段无效")
                        self._json(201, service.create_session(**data))
                        return
                    if path == "/api/active-session":
                        data = self._read_control_json(
                            "expected_selection_revision", "workflow_session_id",
                        )
                        self._json(200, service.switch_session(
                            data["workflow_session_id"],
                            idempotency_key=data["idempotency_key"],
                            expected_selection_revision=data["expected_selection_revision"],
                        ))
                        return
                    match = _INPUT_PATH.fullmatch(path)
                    if match:
                        data = self._read_json()
                        prompt_selection_supplied = "prompt_selection" in data
                        model_selection_supplied = "model_selection" in data
                        if (
                            not {"text", "idempotency_key"} <= set(data)
                            or not set(data) <= {"text", "idempotency_key", "prompt_selection", "model_selection"}
                            or type(data["text"]) is not str
                            or not data["text"].strip()
                            or type(data["idempotency_key"]) is not str
                            or not data["idempotency_key"]
                        ):
                            raise _RequestError(400, "bad_request", "提交字段无效")
                        selection = {}
                        if prompt_selection_supplied:
                            selection["prompt_selection"] = validate_prompt_selection(data["prompt_selection"])
                        if model_selection_supplied:
                            selection["model_selection"] = validate_model_selection(data["model_selection"])
                        self._json(
                            202, service.submit(
                                match.group(1), data["text"], data["idempotency_key"], **selection,
                            )
                        )
                        return
                    match = _RUN_CONTROL_PATH.fullmatch(path)
                    if match:
                        data = self._read_control_json(
                            "expected_session_revision",
                            extra_revision_field="expected_run_revision",
                        )
                        operation = service.interrupt if match.group(3) == "interrupt" else service.resume
                        self._json(202, operation(
                            match.group(1), match.group(2),
                            idempotency_key=data["idempotency_key"],
                            expected_session_revision=data["expected_session_revision"],
                            expected_run_revision=data["expected_run_revision"],
                        ))
                        return
                    match = _RUN_BUDGET_PATH.fullmatch(path)
                    if match:
                        data = self._read_budget_json()
                        self._json(200, service.extend_budget(
                            match.group(1), match.group(2), **data,
                        ))
                        return
                    match = _RUN_ACTION_PATH.fullmatch(path)
                    if match:
                        data = self._read_control_json("expected_session_revision")
                        operation = (
                            service.retry_archive if match.group(3) == "retry-archive"
                            else service.retry_publish
                        )
                        self._json(200, operation(
                            match.group(1), match.group(2),
                            idempotency_key=data["idempotency_key"],
                            expected_session_revision=data["expected_session_revision"],
                        ))
                        return
                    match = _CHAIN_REROLL_PATH.fullmatch(path)
                    if match:
                        data = self._read_control_json(
                            "expected_session_revision",
                            "expected_head_commit_id",
                            extra_revision_field="expected_ref_revision",
                        )
                        self._json(202, service.reroll(
                            match.group(1), match.group(2),
                            idempotency_key=data["idempotency_key"],
                            expected_session_revision=data["expected_session_revision"],
                            expected_ref_revision=data["expected_ref_revision"],
                            expected_head_commit_id=data["expected_head_commit_id"],
                        ))
                        return
                    match = _CHAIN_RECOVERY_PATH.fullmatch(path)
                    if match:
                        data = self._read_control_json(
                            "expected_session_revision",
                            "expected_head_commit_id",
                            extra_revision_field="expected_ref_revision",
                        )
                        close_execution = match.group(3) == "close_execution"
                        operation = (
                            service.close_execution if close_execution
                            else service.continue_workflow
                        )
                        self._json(200 if close_execution else 202, operation(
                            match.group(1), match.group(2),
                            idempotency_key=data["idempotency_key"],
                            expected_session_revision=data["expected_session_revision"],
                            expected_ref_revision=data["expected_ref_revision"],
                            expected_head_commit_id=data["expected_head_commit_id"],
                        ))
                        return
                    match = _BRANCH_PATH.fullmatch(path)
                    if match:
                        data = self._read_control_json(
                            "expected_source_revision", "visible_message_id",
                            extra_revision_field=(
                                "expected_selection_revision" if match.group(2) else None
                            ),
                            optional_identifier_field="candidate_id",
                        )
                        operation = (
                            service.create_and_switch_branch if match.group(2)
                            else service.create_branch
                        )
                        candidate = (
                            {"candidate_id": data["candidate_id"]}
                            if "candidate_id" in data else {}
                        )
                        if match.group(2):
                            candidate["expected_selection_revision"] = data[
                                "expected_selection_revision"
                            ]
                        self._json(201, operation(
                            match.group(1), data["visible_message_id"],
                            idempotency_key=data["idempotency_key"],
                            expected_source_revision=data["expected_source_revision"],
                            **candidate,
                        ))
                        return
                    match = _PENDING_INPUT_PATH.fullmatch(path)
                    if match:
                        data = self._read_control_json("expected_session_revision")
                        self._json(202, service.continue_pending_input(
                            match.group(1), match.group(2),
                            idempotency_key=data["idempotency_key"],
                            expected_session_revision=data["expected_session_revision"],
                        ))
                        return
                    match = _CANDIDATE_SELECT_PATH.fullmatch(path)
                    if match:
                        data = self._read_control_json(
                            "expected_session_revision",
                            "expected_head_commit_id",
                            extra_revision_field="expected_ref_revision",
                        )
                        self._json(200, service.select_candidate(
                            match.group(1), match.group(2),
                            idempotency_key=data["idempotency_key"],
                            expected_session_revision=data["expected_session_revision"],
                            expected_ref_revision=data["expected_ref_revision"],
                            expected_head_commit_id=data["expected_head_commit_id"],
                        ))
                        return
                raise _RequestError(404, "not_found", "未找到接口")
            except _RequestError as exc:
                if locals().get("path", self.path).startswith("/api/graph/"):
                    self._error(exc.status, exc.code, exc.message, "invalid_request", diagnostics=[])
                    return
                self._error(
                    exc.status, exc.code, exc.message,
                    "invalid_request" if (
                        locals().get("path") == "/api/operations"
                        or locals().get("path", "").startswith("/api/prompt-configs/")
                        or locals().get("path", "").startswith("/api/model-configurations/")
                        or "exposures" in locals().get("path", "")
                        or locals().get("path", "").startswith("/api/exposure-configurations")
                        or locals().get("prompt_selection_supplied", False)
                        or locals().get("model_selection_supplied", False)
                        or locals().get("context_request", False)
                    ) else None,
                )
            except ContractValidationError as exc:
                if locals().get("path", "").startswith("/api/graph/"):
                    status = getattr(exc, "status_code", 400)
                    if status not in (400, 403, 404, 409, 500):
                        status = 400
                    code = "internal_error" if status == 500 else "conflict" if status == 409 else "not_found" if status == 404 else "bad_request"
                    self._error(status, code, "Graph request failed" if status == 500 else str(exc),
                                getattr(exc, "reason_code", "invalid_request"),
                                diagnostics=getattr(exc, "diagnostics", []))
                    return
                status = getattr(exc, "status_code", None)
                catalog_request = path.startswith(("/api/prompt-configs/", "/api/model-configurations/",
                                                    "/api/exposure-configurations")) or _EXPOSURE_READ_PATH.fullmatch(path) is not None
                model_request = bool(locals().get("model_selection_supplied", False)) or getattr(exc, "reason_code", "") in {
                    "model_selection_required", "credential_changed", "credential_unavailable",
                    "provider_unavailable", "offline_model_unsupported",
                }
                selection_request = bool(locals().get("prompt_selection_supplied", False)) or model_request
                context_request = bool(locals().get("context_request", False))
                if catalog_request or selection_request or context_request:
                    status = status if status in (400, 404, 409, 500) else 400
                elif status not in (400, 404, 409):
                    status = (
                        409 if method == "POST" and path != "/api/sessions"
                        else 400
                    )
                code = (
                    "internal_error" if status == 500 else "conflict" if status == 409
                    else "not_found" if status == 404 else "bad_request"
                )
                reason = None
                diagnostic = None
                if catalog_request or selection_request:
                    reason = getattr(exc, "reason_code", "invalid_request")
                    if reason not in {
                        "invalid_request", "not_found", "stale_revision", "idempotency_conflict", "ownership_mismatch",
                        "storage_error", "storage_contract_violation",
                        "model_dependency_missing", "provider_missing", "provider_unavailable",
                        "credential_reference_missing", "credential_unavailable", "credential_changed",
                        "offline_model_unsupported", "model_factory_unsupported",
                        "model_parameters_unsupported", "model_selection_required",
                        "global_content_missing", "global_content_disabled",
                        "session_data_unregistered", "session_data_type_mismatch",
                    }:
                        reason = "invalid_request"
                if context_request:
                    reason = getattr(exc, "reason_code", "invalid_request")
                    if reason not in {
                        "invalid_request", "not_found", "stale_revision", "ownership_mismatch",
                        "idempotency_conflict",
                        "storage_error", "storage_contract_violation", "context_limit", "unsupported",
                    }:
                        reason = "invalid_request"
                if resource_request:
                    status = getattr(exc, "status_code", 400)
                    if status not in (400, 403, 404, 409, 500):
                        status = 400
                    reason = getattr(exc, "reason_code", "invalid_request")
                if path == "/api/operations":
                    reason = getattr(exc, "reason_code", "invalid_state")
                    if reason not in {
                        "invalid_request", "unsupported", "stale_revision", "ownership_mismatch",
                        "idempotency_conflict", "invalid_state", "budget_blocked",
                    }:
                        reason = "invalid_state"
                processing = exc.diagnostic() if isinstance(exc, PromptProcessingError) else getattr(
                    exc, "processing_diagnostic", None,
                )
                processing_code = (processing or {}).get("code", getattr(exc, "reason_code", None))
                if processing_code in _PROCESSING_CODES:
                    reason = processing_code
                    status, code = 400, "bad_request"
                    if processing is not None:
                        diagnostic = {"code": processing_code}
                        for field in ("node_id", "item_instance_id", "group_instance_id"):
                            value = processing.get(field)
                            diagnostic[field] = value if (
                                type(value) is str and len(value) <= 128
                                and re.fullmatch(r"[A-Za-z0-9:_-]+", value) is not None
                            ) else None
                        offset = processing.get("offset")
                        diagnostic["offset"] = offset if type(offset) is int and offset >= 0 else None
                self._error(
                    status, code,
                    "服务暂不可用" if status == 500 else
                    "请求与当前会话状态冲突" if status == 409 else "请求无效",
                    reason,
                    diagnostic,
                )
            except Exception:
                self._error(
                    500, "internal_error", "服务暂不可用",
                    "storage_error" if locals().get("context_request", False) or locals().get("path", "").startswith("/api/graph/") else None,
                    diagnostics=[] if locals().get("path", "").startswith("/api/graph/") else None,
                )

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return GraphHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="本机工作流页面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", required=True)
    parser.add_argument("--mode", choices=("offline", "deepseek"), default="offline")
    args = parser.parse_args(argv)
    from .workflow_host import WorkflowHost

    service = WorkflowHost(database_path=args.database, mode=args.mode)
    try:
        with create_server(service, port=args.port, mode=args.mode) as server:
            print(f"http://127.0.0.1:{server.server_address[1]}/", flush=True)
            server.serve_forever()
    finally:
        service.close()


if __name__ == "__main__":
    main()
