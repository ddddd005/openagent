"""Loopback HTTP contract for whole-chain reroll and candidate preservation."""

import http.client
import json
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.server import create_server
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


@contextmanager
def running_server(service):
    with create_server(service, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


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


def reroll_path(session_id, chain_id):
    return f"/api/sessions/{session_id}/chains/{chain_id}/reroll"


class RecordingService:
    def __init__(self):
        self.calls = []

    def reroll(
        self, session_id, chain_id, *, idempotency_key,
        expected_session_revision, expected_ref_revision, expected_head_commit_id,
    ):
        self.calls.append((
            session_id, chain_id, idempotency_key, expected_session_revision,
            expected_ref_revision, expected_head_commit_id,
        ))
        return {
            "workflow_session_id": session_id,
            "source_chain_run_id": chain_id,
            "chain_run_id": "replacement",
            "status": "prepared",
        }


def test_reroll_route_validates_exact_cas_request_before_dispatch():
    service = RecordingService()
    payload = {
        "idempotency_key": "reroll-once",
        "expected_session_revision": 3,
        "expected_ref_revision": 2,
        "expected_head_commit_id": "head_1",
    }
    path = reroll_path("session_1", "chain_1")
    with running_server(service) as port:
        for bad in (
            *({key: value for key, value in payload.items() if key != field}
              for field in payload),
            {**payload, "extra": 1},
            {**payload, "idempotency_key": ""},
            {**payload, "idempotency_key": "x" * 129},
            {**payload, "expected_session_revision": True},
            {**payload, "expected_session_revision": 0},
            {**payload, "expected_ref_revision": "2"},
            {**payload, "expected_ref_revision": 0},
            {**payload, "expected_head_commit_id": ""},
            {**payload, "expected_head_commit_id": "bad.id"},
        ):
            assert request(port, "POST", path, bad)[0] == 400
        assert request(port, "POST", path, payload, origin=False)[0] == 403
        assert request(port, "GET", path)[0] == 404
        for bad_path in (
            path + "/extra", path + "?revision=1",
            reroll_path("bad.id", "chain_1"),
            reroll_path("session_1", "bad.id"),
        ):
            assert request(port, "POST", bad_path, payload)[0] == 404
        assert service.calls == []

        status, receipt = request(port, "POST", path, payload)
        assert status == 202
        assert receipt["chain_run_id"] == "replacement"
        assert service.calls == [
            ("session_1", "chain_1", "reroll-once", 3, 2, "head_1"),
        ]


def test_http_reroll_auto_selects_and_keeps_original_candidate():
    with TemporaryDirectory(prefix="http-reroll-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=OfflineAdapter)) as service:
            with running_server(service) as port:
                sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
                status, submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                    "text": "Keep both replies", "idempotency_key": "submit",
                })
                assert status == 202
                service.wait_for_idle(sid)
                with closing(SqliteStore(database)) as store:
                    original = store.read_bundle()
                first = next(row for row in original["workflow_candidate"]
                             if row["workflow_session_id"] == sid)
                old_message = next(row for row in original["visible_message"]
                                   if row["source"] == {
                                       "kind": "workflow_output",
                                       "output_id": first["output_id"],
                                   })
                session = next(row for row in original["workflow_session"]
                               if row["workflow_session_id"] == sid)
                head = next(row for row in original["workflow_ref"]
                            if row["workflow_session_id"] == sid)
                payload = {
                    "idempotency_key": "reroll-once",
                    "expected_session_revision": session["revision"],
                    "expected_ref_revision": head["revision"],
                    "expected_head_commit_id": head["head_commit_id"],
                }
                path = reroll_path(sid, submitted["chain_run_id"])
                status, accepted = request(port, "POST", path, payload)
                assert status == 202
                assert accepted["source_chain_run_id"] == submitted["chain_run_id"]
                assert accepted["chain_run_id"] != submitted["chain_run_id"]
                service.wait_for_idle(sid)

                with closing(SqliteStore(database)) as store:
                    completed = store.read_bundle()
                candidates = [row for row in completed["workflow_candidate"]
                              if row["workflow_session_id"] == sid]
                assert len(candidates) == 2 and first in candidates
                assert old_message in completed["visible_message"]
                newer = next(row for row in candidates
                             if row["chain_run_id"] == accepted["chain_run_id"])
                current = next(row for row in completed["workflow_ref"]
                               if row["workflow_session_id"] == sid)
                assert current["head_commit_id"] == newer["result_commit_id"]
                status, view = request(port, "GET", f"/api/sessions/{sid}")
                assert status == 200
                assert [row["role"] for row in view["messages"]] == ["user", "assistant"]
                assert view["messages"][-1]["chain_run_id"] == accepted["chain_run_id"]

                assert request(port, "POST", path, payload) == (202, accepted)
                assert request(port, "POST", path, {
                    **payload, "idempotency_key": "stale-request",
                })[0] == 409
                with closing(SqliteStore(database)) as store:
                    assert store.read_bundle() == completed

                select = {
                    "idempotency_key": "select-original",
                    "expected_session_revision": view["revision"],
                    "expected_ref_revision": current["revision"],
                    "expected_head_commit_id": current["head_commit_id"],
                }
                status, selected = request(
                    port, "POST", f"/api/sessions/{sid}/candidates/{first['candidate_id']}/select",
                    select,
                )
                assert status == 200 and selected["head_commit_id"] == first["result_commit_id"]
                assert request(port, "GET", f"/api/sessions/{sid}")[1]["messages"][-1][
                    "chain_run_id"
                ] == first["chain_run_id"]


def test_http_paused_latest_user_reroll_starts_new_chain_without_resuming_old_run():
    started, release = threading.Event(), threading.Event()
    first = True

    class BlockingA(OfflineAdapter):
        def generate(self, messages, tools):
            nonlocal first
            if self.stage == "A" and first:
                first = False
                started.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with TemporaryDirectory(prefix="http-paused-reroll-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=BlockingA)) as service:
            with running_server(service) as port:
                sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
                try:
                    submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                        "text": "Still the same user input", "idempotency_key": "submit",
                    })[1]
                    assert started.wait(10)
                    running = request(port, "GET", f"/api/sessions/{sid}")[1]
                    old_run = running["nodes"][0]
                    stopped = request(
                        port, "POST", f"/api/sessions/{sid}/runs/{old_run['run_id']}/interrupt", {
                            "idempotency_key": "interrupt",
                            "expected_session_revision": running["revision"],
                            "expected_run_revision": old_run["revision"],
                        },
                    )
                    assert stopped[0] == 202
                finally:
                    release.set()
                service.wait_for_idle(sid)
                paused = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert paused["can_reroll"] and paused["messages"][-1]["role"] == "user"
                path = reroll_path(sid, submitted["chain_run_id"])
                payload = {
                    "idempotency_key": "replacement",
                    "expected_session_revision": paused["revision"],
                    "expected_ref_revision": paused["ref_revision"],
                    "expected_head_commit_id": paused["head_commit_id"],
                }
                status, accepted = request(port, "POST", path, payload)
                assert status == 202
                assert accepted["run_id"] != old_run["run_id"]
                assert request(port, "POST", path, payload) == (202, accepted)
                service.wait_for_idle(sid)
                completed = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert completed["messages"][0]["visible_message_id"] == submitted["visible_message_id"]
                assert completed["messages"][-1]["chain_run_id"] == accepted["chain_run_id"]
                assert completed["can_submit"]
                with closing(SqliteStore(database)) as store:
                    old = store.get_record("run_record", {"run_id": old_run["run_id"]})
                    assert old["status"] == "superseded"
                    assert old["superseded_by_run_id"] == accepted["run_id"]


def test_http_paused_b_reroll_preserves_old_a_and_new_b_consumes_new_a():
    entered, release = threading.Event(), threading.Event()
    calls, b_inputs = [], []
    a_count = 0
    blocked = False

    class OneRequestAdapter:
        def __init__(self, stage):
            self.stage = stage

        def generate(self, messages, tools):
            nonlocal a_count, blocked
            calls.append(self.stage)
            if self.stage == "A":
                a_count += 1
                text = f"A-result-{a_count}"
            else:
                user = next(message for message in reversed(messages) if message["role"] == "user")
                text = loads_strict(user["blocks"][0]["text"])["text"]
                b_inputs.append(text)
                if not blocked:
                    blocked = True
                    entered.set()
                    assert release.wait(20)
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "final_answer", dumps_pretty({"answer": {"text": text}})),
            ))

    with TemporaryDirectory(prefix="http-b-reroll-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=OneRequestAdapter)) as service:
            with running_server(service) as port:
                sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
                try:
                    submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                        "text": "Only one user input", "idempotency_key": "submit",
                    })[1]
                    assert entered.wait(10)
                    running = request(port, "GET", f"/api/sessions/{sid}")[1]
                    old_b_id = running["nodes"][1]["run_id"]
                    assert request(
                        port, "POST", f"/api/sessions/{sid}/runs/{old_b_id}/interrupt", {
                            "idempotency_key": "stop-b",
                            "expected_session_revision": running["revision"],
                            "expected_run_revision": running["nodes"][1]["revision"],
                        },
                    )[0] == 202
                finally:
                    release.set()
                service.wait_for_idle(sid)
                paused = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert paused["can_reroll"]
                assert paused["available_actions"] == ["resume", "reroll"]
                with closing(SqliteStore(database)) as store:
                    original = store.read_bundle()
                    old_a = store.get_record("run_record", {"run_id": running["nodes"][0]["run_id"]})
                    old_a_turn = store.get_record("turn", {"turn_id": old_a["result_turn_id"]})
                    old_b_facts = store.read_execution_facts(old_b_id)
                path = reroll_path(sid, submitted["chain_run_id"])
                payload = {
                    "idempotency_key": "restart-from-a",
                    "expected_session_revision": paused["revision"],
                    "expected_ref_revision": paused["ref_revision"],
                    "expected_head_commit_id": paused["head_commit_id"],
                }
                status, accepted = request(port, "POST", path, payload)
                assert status == 202
                assert accepted["chain_run_id"] != submitted["chain_run_id"]
                assert accepted["run_id"] not in {old_a["run_id"], old_b_id}
                assert request(port, "POST", path, payload) == (202, accepted)
                service.wait_for_idle(sid)
                completed = request(port, "GET", f"/api/sessions/{sid}")[1]
                assert completed["error"] is None
                assert [message["role"] for message in completed["messages"]] == ["user", "assistant"]
                assert completed["messages"][0]["visible_message_id"] == submitted["visible_message_id"]
                assert completed["messages"][-1]["payload"]["text"] == "A-result-2"
                assert calls == ["A", "B", "A", "B"]
                assert b_inputs == ["A-result-1", "A-result-2"]
                with closing(SqliteStore(database)) as store:
                    bundle = store.read_bundle()
                    assert store.get_record("run_record", {"run_id": old_a["run_id"]}) == old_a
                    assert store.get_record("turn", {"turn_id": old_a["result_turn_id"]}) == old_a_turn
                    assert store.read_execution_facts(old_b_id) == old_b_facts
                    assert all(candidate in bundle["candidate_group"]
                               for candidate in original["candidate_group"])
                    old_b = store.get_record("run_record", {"run_id": old_b_id})
                    assert old_b["status"] in {"closed", "superseded"}
                    assert old_b["superseded_by_run_id"] != accepted["run_id"]
                    new_chain = store.get_record("chain_run", {"chain_run_id": accepted["chain_run_id"]})
                    assert len(new_chain["node_run_ids"]) == 3
                    assert len([message for message in bundle["visible_message_ref"]
                                if message["workflow_session_id"] == sid and message["role"] == "user"]) == 1
