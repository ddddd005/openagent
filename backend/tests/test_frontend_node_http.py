"""Exact frontend-node HTTP addresses and real postcommit SSE delivery."""

from contextlib import closing, contextmanager
from copy import deepcopy
import http.client
import json
from pathlib import Path
from queue import Empty, Queue
import shutil
import socket
import subprocess
from threading import Event, Thread, current_thread
import time

import pytest

from phase1_agent.contract_errors import DiagnosticError
from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.frontend_node_operations import BoundFrontendNodeOperations
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.server import create_server
from phase1_agent.workflow_host import WorkflowHost

from test_frontend_node_runtime import _create, _graph, _save
from test_graph_service import node
from test_graph_server import request


def owner(graph, frontend, session=None):
    return {
        "workflow_definition_id": graph["workflow_definition_id"], "definition_revision": graph["revision"],
        "node_binding_id": frontend["node_binding_id"],
        "session_id": session["workflow_session_id"] if session else None,
    }


def path(binding, action):
    coordinates = "/".join(str(binding[field]) for field in (
        "workflow_definition_id", "definition_revision", "node_binding_id",
    )) + "/" + (binding["session_id"] or "none")
    if action == "page":
        return "/frontend-node/" + coordinates + "/"
    return "/api/graph/frontend-nodes/" + coordinates + "/" + action


def command(port, binding, operation, parameters=None):
    return request(port, "POST", path(binding, "channel"), {
        "owner": deepcopy(binding), "operation": operation, "parameters": deepcopy(parameters or {}),
    })


@contextmanager
def serving(service):
    server = create_server(service, port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server, server.server_address[1]
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()


class Stream:
    def __init__(self, port, binding):
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.connection.request("GET", path(binding, "events"), headers={
            "Origin": f"http://127.0.0.1:{port}",
        })
        self.socket = self.connection.sock
        self.response = self.connection.getresponse()
        assert self.response.status == 200
        assert self.response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        self.events, self.buffer = Queue(), []

        def receive():
            name, data = None, None
            try:
                for line in self.response:
                    line = line.decode("utf-8").rstrip("\r\n")
                    if line.startswith("event: "):
                        name = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                    elif not line and name is not None and data is not None:
                        self.events.put((name, data))
                        name, data = None, None
            except (OSError, ValueError):
                pass

        self.reader = Thread(target=receive, daemon=True)
        self.reader.start()

    def next(self, name, timeout=5):
        for index, item in enumerate(self.buffer):
            if item[0] == name:
                return self.buffer.pop(index)[1]
        deadline = time.monotonic() + timeout
        while True:
            item = self.events.get(timeout=max(0.001, deadline - time.monotonic()))
            if item[0] == name:
                return item[1]
            self.buffer.append(item)

    def no_events(self, timeout=0.2):
        assert not self.buffer
        with pytest.raises(Empty):
            self.events.get(timeout=timeout)

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.reader.join(3)
        self.response.close()
        self.connection.close()


def test_entry_page_and_no_session_stream_bind_without_execution_or_session_creation(tmp_path, monkeypatch):
    with closing(WorkflowHost(tmp_path / "entry-lazy.sqlite")) as host:
        service = host.graph_management_service
        graph, _, frontends = _graph(service, bound=True)
        graph = _save(service, graph)
        binding = owner(graph, frontends[0])

        def forbidden(_):
            pytest.fail("Entry, page and read-only channel cannot construct execution")

        monkeypatch.setattr(WorkflowHost, "graph_service", property(forbidden))
        with serving(host) as (server, port):
            status, entry = request(port, "GET", path(binding, "entry"))
            assert status == 200
            assert entry == {
                "schema_version": 1, "kind": "workflow.frontend-node-entry", "owner": binding,
                "page_url": f"http://127.0.0.1:{port}" + path(binding, "page"),
                "channel_url": f"http://127.0.0.1:{port}" + path(binding, "channel"),
                "events_url": f"http://127.0.0.1:{port}" + path(binding, "events"),
            }
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", path(binding, "page"))
                response = connection.getresponse()
                assert response.status == 200
                assert b"<!doctype html>" in response.read().lower()
                assert "img-src 'self'" in response.getheader("Content-Security-Policy")
            finally:
                connection.close()
            stream = Stream(port, binding)
            try:
                assert command(port, binding, "state")[1]["snapshot"] is None
                assert command(port, binding, "patch_input", {
                    "text": "via HTTP", "expected_edit_sequence": 1, "idempotency_key": "lazy-patch",
                })[1]["outcome"] == "saved"
                for operation in ("receipt", "accept_receipt"):
                    assert command(port, binding, operation, {
                        "operation": "patch_input", "original_request": {
                            "owner": binding, "parameters": {
                                "text": "via HTTP", "expected_edit_sequence": 1, "idempotency_key": "lazy-patch",
                            },
                        },
                    })[1]["outcome"] == "saved"
                assert command(port, binding, "receipt", {
                    "operation": "start", "original_request": {
                        "owner": binding,
                        "parameters": {"expected_session_revision": None, "idempotency_key": "no-start"},
                    },
                })[1]["outcome"] == "unknown"
                assert host._graph is None
                assert service.list_sessions(graph["workflow_definition_id"]) == []
                assert server.frontend_node_connections.connection_count == 1
            finally:
                stream.close()


@pytest.mark.parametrize("action", ["entry", "page", "events"])
def test_entry_addresses_require_the_exact_saved_node(tmp_path, action):
    with closing(GraphWorkflowService(tmp_path / "entry-owner.sqlite")) as service:
        graph, messages, frontends = _graph(service)
        graph = _save(service, graph)
        binding = owner(graph, frontends[0])
        wrong = {**binding, "node_binding_id": messages[0]["node_binding_id"]}
        with serving(service) as (_, port):
            assert request(port, "GET", path(wrong, action))[0] == 409
            assert request(port, "GET", path(binding, action) + "?object_key=other")[0] == 404
            assert service.list_sessions(graph["workflow_definition_id"]) == []


def test_route_body_mismatch_and_host_origin_reject_before_dispatch(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "entry-security.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2)
        graph = _save(service, graph)
        first, second = (owner(graph, item) for item in frontends)
        with serving(service) as (_, port):
            status, result = request(port, "POST", path(first, "channel"), {
                "owner": second, "operation": "start",
                "parameters": {"expected_session_revision": None, "idempotency_key": "spoof"},
            })
            assert status == 409 and result["error"]["reason_code"] == "frontend_node_owner_mismatch"
            assert request(port, "GET", path(first, "entry"), extra_headers={"Host": "evil.example"})[0] == 403
            assert request(port, "GET", path(first, "events"), extra_headers={"Origin": "http://evil.example"})[0] == 403
            assert request(port, "POST", path(first, "channel"), {
                "owner": first, "operation": "state", "parameters": {},
            }, origin=False)[0] == 403
            assert service.list_sessions(graph["workflow_definition_id"]) == []


def test_real_start_and_postcommit_snapshots_stay_separate_for_two_nodes_and_sessions(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "http-stream-isolation.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2, bound=True)
        graph = _save(service, graph)
        first, other = _create(service, graph), _create(service, graph)
        bindings = [owner(graph, item, first) for item in frontends]
        other_binding = owner(graph, frontends[0], other)
        with serving(service) as (server, port):
            streams = [Stream(port, binding) for binding in [*bindings, other_binding]]
            try:
                for stream in streams:
                    assert stream.next("snapshot")["entries"] == []
                status, started = command(port, bindings[0], "start", {
                    "expected_session_revision": first["revision"], "idempotency_key": "http-real-start",
                })
                assert status == 200 and started["outcome"] == "succeeded"
                service.wait(started["result"]["start_result"]["active_chain_run_id"])
                for index, stream in enumerate(streams[:2]):
                    snapshot = stream.next("snapshot")
                    assert snapshot["owner"]["node_binding_id"] == frontends[index]["node_binding_id"]
                    assert snapshot["owner"]["workflow_session_id"] == first["workflow_session_id"]
                    assert snapshot["entries"][0]["content"]["text"] == "message " + str(index)
                    event = stream.next("state_changed")
                    assert event == {
                        "schema_version": 1, "kind": "workflow.frontend-node-state-changed", "owner": bindings[index],
                    }
                    state = command(port, bindings[index], "state")[1]
                    assert state["run"]["status"] == "succeeded" and state["run"]["can_start"]
                    assert state["snapshot"]["basis"] == snapshot["basis"]
                    stream.no_events()
                streams[2].no_events()
                assert service.get_session(other["workflow_session_id"])["objects"] == other["objects"]
                assert server.frontend_node_connections.connection_count == 3
            finally:
                for stream in streams:
                    stream.close()


def test_rollback_pushes_only_real_failed_state_and_no_uncommitted_snapshot(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "http-stream-rollback.sqlite")) as service:
        graph, _, frontends = _graph(service)
        graph = _save(service, graph)
        initial = _create(service, graph)
        binding = owner(graph, frontends[0], initial)
        original = service._save_effects

        def fail_after_cas(repo, sid, document, event):
            receipts = original(repo, sid, document, event)
            if event["node_binding_id"] == frontends[0]["node_binding_id"]:
                raise OSError("rollback stream write")
            return receipts

        monkeypatch.setattr(service, "_save_effects", fail_after_cas)
        with serving(service) as (_, port):
            stream = Stream(port, binding)
            try:
                assert stream.next("snapshot")["entries"] == []
                started = command(port, binding, "start", {
                    "expected_session_revision": initial["revision"], "idempotency_key": "rollback-start",
                })[1]
                service.wait(started["result"]["start_result"]["active_chain_run_id"])
                assert stream.next("state_changed")["owner"] == binding
                final = command(port, binding, "state")[1]
                assert final["run"]["status"] == "archive_failed"
                assert final["snapshot"]["entries"] == []
                stream.no_events()
                assert service.get_session(initial["workflow_session_id"])["objects"] == initial["objects"]
            finally:
                stream.close()


def test_server_close_wakes_and_ends_actual_stream_handler_without_runtime_control(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "stream-close.sqlite")) as service:
        graph, _, frontends = _graph(service)
        graph = _save(service, graph)
        binding = owner(graph, frontends[0])
        server = create_server(service, port=0)
        handlers, entered = [], Event()
        original = server.RequestHandlerClass._frontend_node_events

        def capture(handler, address):
            handlers.append(current_thread())
            entered.set()
            return original(handler, address)

        monkeypatch.setattr(server.RequestHandlerClass, "_frontend_node_events", capture)
        monkeypatch.setattr(service, "control", lambda *args, **kwargs: pytest.fail("disconnect controlled runtime"))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        stream = Stream(server.server_address[1], binding)
        try:
            assert entered.wait(2)
            assert server.frontend_node_connections.connection_count == 1
            server.shutdown()
            worker.join(3)
            server.server_close()
            for handler in handlers:
                handler.join(3)
                assert not handler.is_alive()
            assert server.frontend_node_connections.connection_count == 0
            assert service.list_sessions(graph["workflow_definition_id"]) == []
        finally:
            stream.close()
            server.server_close()


def test_disconnect_removes_connection_but_does_not_pause_or_stop_running_session(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "disconnect gate timed out"
        return {}

    with closing(GraphWorkflowService(tmp_path / "stream-disconnect.sqlite")) as service:
        service.registry.register(NodeDefinition(
            "test.disconnect-gate", "1", "Gate", "Test", {}, {"type": "object"}, is_output=True,
        ), gate)
        graph, _, frontends = _graph(service)
        waiting = node(service.registry, "test.disconnect-gate", 19801)
        waiting["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        graph["nodes"].append(waiting)
        graph["control_edges"] = [{
            "edge_id": "fb49fd8b-75f9-4e4b-9cfd-85324c4fe5b6",
            "source_node_id": frontends[0]["node_binding_id"], "target_node_id": waiting["node_binding_id"],
        }]
        graph = _save(service, graph)
        initial = _create(service, graph)
        binding = owner(graph, frontends[0], initial)
        monkeypatch.setattr(service, "control", lambda *args, **kwargs: pytest.fail("disconnect controlled runtime"))
        with serving(service) as (server, port):
            stream = Stream(port, binding)
            stream.next("snapshot")
            started = command(port, binding, "start", {
                "expected_session_revision": initial["revision"], "idempotency_key": "disconnect-start",
            })[1]
            try:
                assert entered.wait(5)
                stream.next("snapshot")
                stream.close()
                deadline = time.monotonic() + 4
                while server.frontend_node_connections.connection_count and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert server.frontend_node_connections.connection_count == 0
                active = service.get_session(initial["workflow_session_id"])
                assert active["status"] == "running"
                assert started["result"]["start_result"]["active_chain_run_id"] not in service._pauses
            finally:
                release.set()
                service.wait(started["result"]["start_result"]["active_chain_run_id"])
                stream.close()
            assert service.get_session(initial["workflow_session_id"])["status"] == "succeeded"


def test_projection_error_after_real_save_is_a_409_with_original_receipt_and_no_resubmit(tmp_path, monkeypatch):
    with closing(WorkflowHost(tmp_path / "projection-409.sqlite")) as host:
        service = host.graph_management_service
        graph, _, frontends = _graph(service, bound=True)
        graph = _save(service, graph)
        binding = owner(graph, frontends[0])
        parameters = {"text": "saved before projection", "expected_edit_sequence": 1, "idempotency_key": "projection-key"}
        original_projection = FrontendNodeChannel._patch_projection
        original_patch = BoundFrontendNodeOperations.patch_input
        calls = []

        def actual_patch(bound, *args, **kwargs):
            calls.append(kwargs["idempotency_key"])
            return original_patch(bound, *args, **kwargs)

        def fail_projection(channel, raw):
            raise DiagnosticError("frontend_node_result_mismatch", "Projection rejected after actual save", status=409)

        monkeypatch.setattr(BoundFrontendNodeOperations, "patch_input", actual_patch)
        with serving(host) as (_, port):
            monkeypatch.setattr(FrontendNodeChannel, "_patch_projection", fail_projection)
            status, error = command(port, binding, "patch_input", parameters)
            assert status == 409 and error["error"]["reason_code"] == "frontend_node_result_mismatch"
            assert error["error"]["diagnostics"][0]["code"] == "frontend_node_result_mismatch"
            assert service.get_definition(graph["workflow_definition_id"])["nodes"][0]["config"]["text"] == parameters["text"]
            monkeypatch.setattr(FrontendNodeChannel, "_patch_projection", original_projection)
            for operation in ("receipt", "accept_receipt"):
                status, actual = command(port, binding, operation, {
                    "operation": "patch_input", "original_request": {"owner": binding, "parameters": parameters},
                })
                assert status == 200 and actual["outcome"] == "saved"
            assert calls == ["projection-key"]
            assert host._graph is None


def test_internal_http_error_is_sanitized_and_real_original_request_can_be_verified(tmp_path, monkeypatch):
    with closing(WorkflowHost(tmp_path / "projection-500.sqlite")) as host:
        service = host.graph_management_service
        graph, _, frontends = _graph(service, bound=True)
        graph = _save(service, graph)
        binding = owner(graph, frontends[0])
        parameters = {"text": "saved before error", "expected_edit_sequence": 1, "idempotency_key": "internal-key"}
        original = FrontendNodeChannel._patch_projection

        def fail(channel, raw):
            raise RuntimeError("do not expose this secret diagnostic")

        with serving(host) as (_, port):
            monkeypatch.setattr(FrontendNodeChannel, "_patch_projection", fail)
            status, error = command(port, binding, "patch_input", parameters)
            assert status == 500 and error["error"]["reason_code"] == "internal_error"
            assert "secret diagnostic" not in json.dumps(error)
            monkeypatch.setattr(FrontendNodeChannel, "_patch_projection", original)
            assert command(port, binding, "receipt", {
                "operation": "patch_input", "original_request": {"owner": binding, "parameters": parameters},
            })[1]["outcome"] == "saved"


def test_real_pause_and_finish_each_notify_owner_for_formal_state_refresh(tmp_path):
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "HTTP SSE pause gate timed out"
        return {}

    with closing(GraphWorkflowService(tmp_path / "http-stream-pause.sqlite")) as service:
        for name, executor in (("gate", gate), ("tail", lambda *args: {})):
            service.registry.register(NodeDefinition(
                "test.http-" + name, "1", name, "Test", {}, {"type": "object"}, is_output=True,
            ), executor)
        graph, _, frontends = _graph(service)
        gate_node = node(service.registry, "test.http-gate", 19701)
        tail = node(service.registry, "test.http-tail", 19702)
        for entry in (gate_node, tail):
            entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        graph["nodes"].extend([gate_node, tail])
        graph["control_edges"] = [
            {"edge_id": "cb5f1d03-7a17-49f6-84da-cb168804395a",
             "source_node_id": frontends[0]["node_binding_id"], "target_node_id": gate_node["node_binding_id"]},
            {"edge_id": "146a02bf-4ed5-4f8d-8f25-926c89f71f04",
             "source_node_id": gate_node["node_binding_id"], "target_node_id": tail["node_binding_id"]},
        ]
        graph = _save(service, graph)
        initial = _create(service, graph)
        binding = owner(graph, frontends[0], initial)
        with serving(service) as (_, port):
            stream = Stream(port, binding)
            try:
                stream.next("snapshot")
                started = command(port, binding, "start", {
                    "expected_session_revision": initial["revision"], "idempotency_key": "sse-pause-start",
                })[1]
                assert entered.wait(5)
                assert len(stream.next("snapshot")["entries"]) == 1
                running = command(port, binding, "state")[1]
                assert running["run"]["status"] == "running"
                assert command(port, binding, "pause", {
                    "expected_session_revision": running["run"]["revision"], "idempotency_key": "sse-pause",
                })[1]["outcome"] == "succeeded"
                assert stream.next("state_changed")["owner"] == binding
                assert command(port, binding, "state")[1]["run"]["status"] == "pausing"
                release.set()
                service.wait(started["result"]["start_result"]["active_chain_run_id"])
                assert stream.next("state_changed")["owner"] == binding
                assert command(port, binding, "state")[1]["run"]["status"] == "paused"
            finally:
                release.set()
                stream.close()


@pytest.mark.parametrize("mode", ["projection", "lost"])
def test_real_javascript_host_fetch_preserves_unknown_and_only_verifies_original_request(
        tmp_path, monkeypatch, mode):
    node_runtime = shutil.which("node")
    if node_runtime is None:
        pytest.skip("Node.js is not installed")
    with closing(GraphWorkflowService(tmp_path / "actual-http-client.sqlite")) as service:
        graph, _, frontends = _graph(service, bound=True)
        graph = _save(service, graph)
        binding = owner(graph, frontends[0])
        original_projection = FrontendNodeChannel._patch_projection
        original_patch = BoundFrontendNodeOperations.patch_input
        calls, projection_failures = [], []

        def counted(bound, *args, **kwargs):
            calls.append(kwargs["idempotency_key"])
            return original_patch(bound, *args, **kwargs)

        def projection(channel, raw):
            if mode == "projection" and not projection_failures:
                projection_failures.append(True)
                raise DiagnosticError(
                    "frontend_node_result_mismatch", "Actual saved result could not be projected", status=409)
            return original_projection(channel, raw)

        monkeypatch.setattr(BoundFrontendNodeOperations, "patch_input", counted)
        monkeypatch.setattr(FrontendNodeChannel, "_patch_projection", projection)
        with serving(service) as (_, port):
            process = subprocess.run(
                [node_runtime, str(Path(__file__).with_name("frontend_node_http_bridge.cjs")),
                 f"http://127.0.0.1:{port}", json.dumps(binding), mode],
                capture_output=True, text=True, encoding="utf-8", timeout=25,
            )
            assert process.returncode == 0, process.stderr
            result = json.loads(process.stdout)
            assert result["done"] is True
            assert result["trace"].count("patch_input") == 1
            assert result["trace"].count("receipt") == 1
            assert result["trace"].count("accept_receipt") == 1
            assert result["trace"].count("start") == 1
            assert calls == ["http-client-1"]
            sessions = service.list_sessions(graph["workflow_definition_id"])
            assert len(sessions) == 1 and sessions[0]["status"] == "succeeded"
            assert len(sessions[0]["objects"]["chat-0"]["value"]["entries"]) == 1
            assert result["owner"]["session_id"] == sessions[0]["workflow_session_id"]
            assert service.get_definition(graph["workflow_definition_id"])["edit_sequence"] == 2
