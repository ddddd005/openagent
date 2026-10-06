"""Explicit loopback closeout and manual continuation of lost executions."""

import asyncio
import copy
import http.client
import json
import threading
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.server import create_server
from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


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


def request(port, method, path, payload=None, *, origin=True):
    headers = {"Host": f"127.0.0.1:{port}"}
    body = None
    if method == "POST":
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{port}"
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def recovery_path(session_id, chain_id, action):
    return f"/api/sessions/{session_id}/chains/{chain_id}/{action}"


def control_payload(view, key):
    return {
        "idempotency_key": key,
        "expected_session_revision": view["revision"],
        "expected_ref_revision": view["ref_revision"],
        "expected_head_commit_id": view["head_commit_id"],
    }


class RecordingService:
    def __init__(self):
        self.calls = []

    def close_execution(self, session_id, chain_id, **arguments):
        self.calls.append(("close_execution", session_id, chain_id, arguments))
        return {"chain_run_id": chain_id, "status": "closed"}

    def continue_workflow(self, session_id, chain_id, **arguments):
        self.calls.append(("continue_workflow", session_id, chain_id, arguments))
        return {"chain_run_id": "new-chain", "source_chain_run_id": chain_id, "status": "prepared"}


@pytest.mark.parametrize("action,status", [
    ("close_execution", 200), ("continue_workflow", 202),
])
def test_recovery_route_strict_four_field_cas_validation(action, status):
    service = RecordingService()
    payload = {
        "idempotency_key": "manual-once",
        "expected_session_revision": 3,
        "expected_ref_revision": 2,
        "expected_head_commit_id": "head_1",
    }
    path = recovery_path("session_1", "chain_1", action)
    with running_server(service) as port:
        for invalid in (
            *({key: value for key, value in payload.items() if key != field}
              for field in payload),
            {**payload, "unexpected": True},
            {**payload, "idempotency_key": ""},
            {**payload, "idempotency_key": "x" * 129},
            {**payload, "expected_session_revision": True},
            {**payload, "expected_session_revision": 0},
            {**payload, "expected_ref_revision": False},
            {**payload, "expected_ref_revision": "2"},
            {**payload, "expected_head_commit_id": ""},
            {**payload, "expected_head_commit_id": "bad.id"},
        ):
            assert request(port, "POST", path, invalid)[0] == 400
        assert request(port, "POST", path, payload, origin=False)[0] == 403
        assert request(port, "GET", path)[0] == 404
        for invalid_path in (
            path + "/extra", path + "?revision=1",
            recovery_path("bad.id", "chain_1", action),
            recovery_path("session_1", "bad.id", action),
        ):
            assert request(port, "POST", invalid_path, payload)[0] == 404
        assert service.calls == []
        assert request(port, "POST", path, payload)[0] == status
        assert service.calls == [(action, "session_1", "chain_1", payload)]


@pytest.mark.parametrize("stage", ["A", "B"])
def test_http_lost_execution_requires_manual_close_and_manual_new_execution(stage):
    entered, release = threading.Event(), threading.Event()
    calls, resumed_requests = [], []
    blocked = False

    class PausedAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            nonlocal blocked
            calls.append(self.stage)
            if self.stage == stage and not blocked:
                blocked = True
                entered.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    class ContinuingAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            resumed_requests.append((self.stage, copy.deepcopy(messages)))
            return super().generate(messages, tools)

    with TemporaryDirectory(prefix="http-recover-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=PausedAdapter)) as service:
            with running_server(service) as port:
                sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
                try:
                    submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                        "text": "Continue from trustworthy facts", "idempotency_key": "submit",
                    })[1]
                    assert entered.wait(10)
                    running = request(port, "GET", f"/api/sessions/{sid}")[1]
                    index = 0 if stage == "A" else 1
                    old_run = running["nodes"][index]
                    assert request(
                        port, "POST", f"/api/sessions/{sid}/runs/{old_run['run_id']}/interrupt",
                        {
                            "idempotency_key": "pause",
                            "expected_session_revision": running["revision"],
                            "expected_run_revision": old_run["revision"],
                        },
                    )[0] == 202
                finally:
                    release.set()
                service.wait_for_idle(sid)
                paused = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert paused["chains"][-1]["status"] == "paused"
                with closing(SqliteStore(database)) as store:
                    before = store.read_bundle()
                    old_facts = store.read_execution_facts(old_run["run_id"])
        with closing(WorkflowService(database, model_factory=ContinuingAdapter)) as service:
            with running_server(service) as port:
                recovered = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert resumed_requests == []
                assert recovered["can_close_execution"]
                assert not recovered["can_continue_workflow"]
                assert recovered["recovery_chain_run_id"] == submitted["chain_run_id"]
                close_path = recovery_path(sid, submitted["chain_run_id"], "close_execution")
                close_payload = control_payload(recovered, "close")
                for field, value in (
                    ("expected_session_revision", recovered["revision"] + 1),
                    ("expected_ref_revision", recovered["ref_revision"] + 1),
                    ("expected_head_commit_id", str(uuid4())),
                ):
                    assert request(port, "POST", close_path, {
                        **close_payload, field: value, "idempotency_key": "conflict-" + field,
                    })[0] == 409
                assert resumed_requests == []
                status, closed_receipt = request(port, "POST", close_path, close_payload)
                assert status == 200
                assert closed_receipt["status"] == "closed"
                assert request(port, "POST", close_path, close_payload) == (200, closed_receipt)
                closed = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert resumed_requests == []
                assert closed["can_continue_workflow"]
                assert not closed["can_close_execution"]
                assert closed["can_reroll"]
                assert closed["head_commit_id"] == recovered["head_commit_id"]
                assert closed["ref_revision"] == recovered["ref_revision"]
                assert request(port, "POST", close_path, {
                    **close_payload, "idempotency_key": "stale-close",
                })[0] == 409
                with closing(SqliteStore(database)) as store:
                    assert store.read_execution_facts(old_run["run_id"]) == old_facts
                    closed_bundle = store.read_bundle()
                    assert all(row in closed_bundle.get("turn", []) for row in before.get("turn", []))
                    assert all(row in closed_bundle.get("workflow_candidate", [])
                               for row in before.get("workflow_candidate", []))
                continue_path = recovery_path(sid, submitted["chain_run_id"], "continue_workflow")
                continue_payload = control_payload(closed, "continue")
                assert request(port, "POST", continue_path, {
                    **continue_payload, "expected_ref_revision": closed["ref_revision"] + 1,
                    "idempotency_key": "conflict-before-continue",
                })[0] == 409
                assert resumed_requests == []
                status, continued = request(port, "POST", continue_path, continue_payload)
                assert status == 202
                assert continued["chain_run_id"] != submitted["chain_run_id"]
                assert continued["run_id"] != old_run["run_id"]
                assert request(port, "POST", continue_path, continue_payload) == (202, continued)
                assert request(port, "POST", continue_path, {
                    **continue_payload, "expected_ref_revision": closed["ref_revision"] + 1,
                })[0] == 409
                service.wait_for_idle(sid)
                completed = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert completed["error"] is None
                assert completed["can_submit"]
                assert [message["role"] for message in completed["messages"]] == ["user", "assistant"]
                assert completed["messages"][0]["visible_message_id"] == submitted["visible_message_id"]
                assert completed["messages"][-1]["chain_run_id"] == continued["chain_run_id"]
                assert resumed_requests
                assert resumed_requests[0][0] == stage
                if stage == "B":
                    assert all(node == "B" for node, _ in resumed_requests)
                request_text = json.dumps(resumed_requests[0][1], ensure_ascii=False)
                assert old_run["run_id"] in request_text
                assert "execution_facts" not in json.dumps(completed)
                with closing(SqliteStore(database)) as store:
                    final_bundle = store.read_bundle()
                    assert store.read_execution_facts(old_run["run_id"]) == old_facts
                    assert all(row in final_bundle["turn"] for row in before.get("turn", []))
                assert request(port, "POST", continue_path, {
                    **continue_payload, "idempotency_key": "stale-continue",
                })[0] == 409
                with closing(SqliteStore(database)) as store:
                    assert store.read_bundle() == final_bundle
        with closing(WorkflowService(
            database, model_factory=lambda node: pytest.fail(f"reopened dispatch {node}"),
        )) as reopened:
            with running_server(reopened) as port:
                assert request(port, "GET", f"/api/sessions/{sid}")[1]["messages"] == completed["messages"]
                assert request(port, "POST", continue_path, continue_payload) == (202, continued)


def test_http_tool_interruption_is_evidence_not_automatic_tool_replay(monkeypatch):
    executed, continuation_requests = [], []
    original_tools = WorkflowService._tools

    def interrupted(text):
        executed.append(text)
        raise asyncio.CancelledError("PRIVATE-TOOL-DIAGNOSTIC")

    def tools():
        inspect, final = original_tools()
        return replace(inspect, _implementation=interrupted), final

    monkeypatch.setattr(WorkflowService, "_tools", staticmethod(tools))

    class InterruptedAdapter:
        def __init__(self, stage):
            self.stage = stage

        def generate(self, messages, tools):
            return ModelResponse("tool_calls", tool_calls=tuple(
                ModelToolCall(str(uuid4()), "inspect_text", dumps_pretty({"text": text}))
                for text in ("uncertain-effect", "never-executed")
            ))

    class EvidenceAdapter:
        def __init__(self, stage):
            self.stage = stage

        def generate(self, messages, tools):
            continuation_requests.append((self.stage, copy.deepcopy(messages)))
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "final_answer",
                              dumps_pretty({"answer": {"text": "Handled interruption"}})),
            ))

    with TemporaryDirectory(prefix="http-tool-recovery-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=InterruptedAdapter)) as service:
            with running_server(service) as port:
                sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
                submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                    "text": "Keep uncertain tool evidence", "idempotency_key": "submit",
                })[1]
                service.wait_for_idle(sid)
                failed = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert failed["error"]["code"] == "execution_interrupted"
                assert failed["available_actions"] == ["close_execution"]
                assert executed == ["uncertain-effect"]
                old_run_id = failed["nodes"][0]["run_id"]
                with closing(SqliteStore(database)) as store:
                    old_facts = store.read_execution_facts(old_run_id)
        with closing(WorkflowService(database, model_factory=EvidenceAdapter)) as reopened:
            with running_server(reopened) as port:
                recovered = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert continuation_requests == []
                closed = request(
                    port, "POST", recovery_path(sid, submitted["chain_run_id"], "close_execution"),
                    control_payload(recovered, "close"),
                )
                assert closed[0] == 200
                current = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert current["can_continue_workflow"]
                assert continuation_requests == []
                continued = request(
                    port, "POST", recovery_path(sid, submitted["chain_run_id"], "continue_workflow"),
                    control_payload(current, "continue"),
                )
                assert continued[0] == 202
                reopened.wait_for_idle(sid)
                finished = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert finished["error"] is None
                assert executed == ["uncertain-effect"]
                assert [stage for stage, _ in continuation_requests] == ["A", "B"]
                observation = next(message for message in continuation_requests[0][1]
                                   if message["source"]["kind"] == "runtime_execution_observation")
                evidence = loads_strict(observation["blocks"][0]["text"].split("\n", 1)[1])
                observations = evidence["runs"][0]["tool_observations"]
                assert [row["known_outcome"] for row in observations] == [
                    "outcome_unknown", "never_started",
                ]
                assert observations[0]["tool_execution_id"] is not None
                assert observations[1]["tool_execution_id"] is None
                assert observations[0]["saved_result"] is None
                assert not any(message["role"] == "tool" for message in continuation_requests[0][1])
                assert "PRIVATE-TOOL-DIAGNOSTIC" not in str(finished) + str(observation)
                with closing(SqliteStore(database)) as store:
                    assert store.read_execution_facts(old_run_id) == old_facts


def test_http_program_failure_is_explicit_and_cannot_be_delegated_to_model():
    class BrokenAdapter:
        def __init__(self, stage):
            self.stage = stage

        def generate(self, messages, tools):
            raise ValueError("PRIVATE-CONTRACT-DIAGNOSTIC")

    with TemporaryDirectory(prefix="http-contract-recovery-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=BrokenAdapter)) as service:
            with running_server(service) as port:
                sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
                submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                    "text": "Surface program failure", "idempotency_key": "submit",
                })[1]
                service.wait_for_idle(sid)
                failed = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert failed["error"]["code"] == "adapter_contract_error"
                assert "PRIVATE-CONTRACT-DIAGNOSTIC" not in str(failed)
                status, closed = request(
                    port, "POST", recovery_path(sid, submitted["chain_run_id"], "close_execution"),
                    control_payload(failed, "close"),
                )
                assert status == 200 and closed["status"] == "closed"
                current = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert not current["can_continue_workflow"]
                assert current["can_reroll"]
                assert current["available_actions"] == ["reroll"]
                status, refusal = request(
                    port, "POST", recovery_path(sid, submitted["chain_run_id"], "continue_workflow"),
                    control_payload(current, "do-not-model-recover"),
                )
                assert status == 409
                assert refusal["error"]["code"] == "conflict"
                assert "PRIVATE-CONTRACT-DIAGNOSTIC" not in str(refusal)
        with closing(WorkflowService(
            database, model_factory=lambda node: pytest.fail(f"dispatch after close {node}"),
        )) as reopened:
            with running_server(reopened) as port:
                assert request(port, "GET", f"/api/sessions/{sid}")[1]["available_actions"] == ["reroll"]
