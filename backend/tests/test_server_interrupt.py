"""Real loopback HTTP coverage for N05 interrupt and same-run resume."""

import http.client
import json
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.server import create_server
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="http-interrupt-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder) / "workflow.sqlite"


@contextmanager
def running_service(database, model_factory):
    with closing(WorkflowService(database, mode="offline", model_factory=model_factory)) as service:
        with create_server(service, port=0, mode="offline") as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield service, server.server_address[1]
            finally:
                server.shutdown()
                thread.join(timeout=5)
                assert not thread.is_alive()


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


def assert_conflict(port, path, payload):
    status, body = request(port, "POST", path, payload)
    assert status == 409, body
    assert body["error"]["code"] == "conflict"


class StatelessOfflineAdapter:
    def __init__(self, stage, calls, first_block=None):
        self.stage = stage
        self.calls = calls
        self.first_block = first_block

    def generate(self, messages, tools):
        self.calls.append(self.stage)
        if self.first_block is not None:
            started, release = self.first_block
            self.first_block = None
            started.set()
            assert release.wait(20)
        user = next(message for message in reversed(messages) if message["role"] == "user")
        text = loads_strict(user["blocks"][0]["text"])["text"]
        if not any(message["role"] == "tool" for message in messages):
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "inspect_text", dumps_pretty({"text": text})),
            ))
        answer = (
            "[Offline draft]\n\n" + text.strip()
            if self.stage == "A"
            else "[Offline revision]\n\n" + text.strip().removeprefix("[Offline draft]\n\n")
        )
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall(str(uuid4()), "final_answer", dumps_pretty({"answer": {"text": answer}})),
        ))


def blocking_factory(started, release, calls):
    first = True

    def factory(stage):
        nonlocal first
        block = (started, release) if first and stage == "A" else None
        if block is not None:
            first = False
        return StatelessOfflineAdapter(stage, calls, block)

    return factory


def test_http_interrupt_resume_strict_cas_replay_and_same_run(database):
    started, release = threading.Event(), threading.Event()
    model_calls = []
    with running_service(
        database, blocking_factory(started, release, model_calls),
    ) as (service, port):
        sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
        try:
            status, submission = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "Keep this chain identity", "idempotency_key": "submit",
            })
            assert status == 202 and started.wait(10)
            status, running = request(port, "GET", f"/api/sessions/{sid}")
            assert status == 200
            run_id = running["nodes"][0]["run_id"]
            interrupt_path = f"/api/sessions/{sid}/runs/{run_id}/interrupt"
            interrupt = {
                "idempotency_key": "stop-once",
                "expected_session_revision": running["revision"],
                "expected_run_revision": running["nodes"][0]["revision"],
            }
            for bad in (
                {key: value for key, value in interrupt.items() if key != "expected_run_revision"},
                {**interrupt, "extra": True},
                {**interrupt, "idempotency_key": ""},
                {**interrupt, "idempotency_key": "x" * 129},
                {**interrupt, "expected_session_revision": True},
                {**interrupt, "expected_run_revision": 0},
                {**interrupt, "expected_run_revision": "2"},
            ):
                assert request(port, "POST", interrupt_path, bad)[0] == 400
            assert request(port, "GET", interrupt_path)[0] == 404
            assert request(port, "GET", f"/api/sessions/{sid}")[1]["nodes"][0]["revision"] == \
                interrupt["expected_run_revision"]
            status, stopped = request(port, "POST", interrupt_path, interrupt)
            assert status == 202 and stopped["status"] == "pausing"
        finally:
            release.set()
        service.wait_for_idle(sid)
        status, paused = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and paused["nodes"][0]["status"] == "paused"
        assert paused["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "paused",
        }]
        assert [row["role"] for row in paused["messages"]] == ["user"]
        assert request(port, "POST", interrupt_path, interrupt) == (202, stopped)
        assert_conflict(port, interrupt_path, {
            **interrupt, "expected_run_revision": interrupt["expected_run_revision"] + 1,
        })
        assert_conflict(port, interrupt_path, {**interrupt, "idempotency_key": "stale-stop"})

        resume_path = f"/api/sessions/{sid}/runs/{run_id}/resume"
        resume = {
            "idempotency_key": "continue-once",
            "expected_session_revision": paused["revision"],
            "expected_run_revision": paused["nodes"][0]["revision"],
        }
        for bad in (
            {key: value for key, value in resume.items() if key != "expected_session_revision"},
            {**resume, "extra": 1},
            {**resume, "expected_run_revision": False},
        ):
            assert request(port, "POST", resume_path, bad)[0] == 400
        assert_conflict(port, resume_path, {
            **resume, "expected_session_revision": resume["expected_session_revision"] - 1,
        })
        status, continued = request(port, "POST", resume_path, resume)
        assert status == 202 and continued["status"] == "running"
        assert request(port, "POST", resume_path, resume) == (202, continued)
        assert_conflict(port, resume_path, {**resume, "idempotency_key": "second-resume"})
        service.wait_for_idle(sid)
        status, completed = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and completed["error"] is None
        assert [row["role"] for row in completed["messages"]] == ["user", "assistant"]
        assert completed["nodes"][0]["run_id"] == run_id
        assert completed["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "succeeded",
        }]
        assert model_calls == ["A", "A", "A", "B", "B"]
        with closing(SqliteStore(database)) as store:
            assert len(store.list_records("workflow_candidate")) == 1
            assert len(store.list_records("run_record")) == 2


def test_http_resume_after_reopen_cannot_restore_paused_run(database):
    started, release = threading.Event(), threading.Event()
    calls = []
    with running_service(database, blocking_factory(started, release, calls)) as (service, port):
        sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
        try:
            status, submission = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "Pause before restart", "idempotency_key": "submit",
            })
            assert status == 202 and started.wait(10)
            running = request(port, "GET", f"/api/sessions/{sid}")[1]
            run_id = running["nodes"][0]["run_id"]
            interrupt = {
                "idempotency_key": "stop",
                "expected_session_revision": running["revision"],
                "expected_run_revision": running["nodes"][0]["revision"],
            }
            assert request(
                port, "POST", f"/api/sessions/{sid}/runs/{run_id}/interrupt", interrupt,
            )[0] == 202
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert request(port, "GET", f"/api/sessions/{sid}")[1]["nodes"][0]["status"] == "paused"
    with running_service(
        database, lambda stage: pytest.fail(f"restart redispatched {stage}"),
    ) as (_, port):
        status, recovered = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and recovered["error"]["code"] == "RECOVERY_UNAVAILABLE"
        assert recovered["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "recovery_unavailable",
        }]
        assert_conflict(
            port, f"/api/sessions/{sid}/runs/{run_id}/resume", {
                "idempotency_key": "not-resumable",
                "expected_session_revision": recovered["revision"],
                "expected_run_revision": recovered["nodes"][0]["revision"],
            },
        )
        assert calls == ["A"]
