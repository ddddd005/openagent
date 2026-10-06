"""Offline HTTP integration for the persisted workflow-version chain."""

import http.client
import json
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from phase1_agent.contract_graph import validate_bundle
from phase1_agent.server import create_server
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="http-versions-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder) / "workflow.sqlite"


@contextmanager
def running_service(database, factory):
    with closing(WorkflowService(database, mode="offline", model_factory=factory)) as service:
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


def read_bundle(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def rows(bundle, kind, session_id):
    return [
        row for row in bundle.get(kind, [])
        if row["workflow_session_id"] == session_id
    ]


def head(bundle, session_id):
    refs = rows(bundle, "workflow_ref", session_id)
    assert len(refs) == 1
    return refs[0]


def seed(bundle, session_id):
    matches = [
        row for row in rows(bundle, "workflow_commit", session_id)
        if row["source"] == {"kind": "session_seed", "workflow_session_id": session_id}
    ]
    assert len(matches) == 1
    return matches[0]


def test_http_create_and_submit_write_seed_candidate_and_selected_head(database):
    with running_service(database, OfflineAdapter) as (service, port):
        status, created = request(port, "POST", "/api/sessions", {})
        assert status == 201
        sid = created["workflow_session_id"]
        initial = read_bundle(database)
        root = seed(initial, sid)
        assert root["parent_commit_id"] is None
        assert head(initial, sid)["head_commit_id"] == root["commit_id"]
        assert head(initial, sid)["revision"] == 1
        assert len(rows(initial, "state_snapshot", sid)) == 1
        assert next(row for row in initial["workflow_operation"]
                    if row["operation_id"] == root["operation_id"])["kind"] == "initialize_session"

        status, submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
            "text": "A durable reply", "idempotency_key": "submit",
        })
        assert status == 202
        service.wait_for_idle(sid)
        status, view = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and view["error"] is None
        assert [row["role"] for row in view["messages"]] == ["user", "assistant"]

        bundle = read_bundle(database)
        candidates = rows(bundle, "workflow_candidate", sid)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate["chain_run_id"] == submitted["chain_run_id"]
        assert candidate["base_commit_id"] == root["commit_id"]
        result = next(row for row in bundle["workflow_commit"]
                      if row["commit_id"] == candidate["result_commit_id"])
        assert result["parent_commit_id"] == root["commit_id"]
        assert result["source"] == {
            "kind": "candidate", "candidate_id": candidate["candidate_id"],
        }
        assert head(bundle, sid)["head_commit_id"] == result["commit_id"]
        assert head(bundle, sid)["revision"] == 2
        state = next(row for row in bundle["state_snapshot"]
                     if row["state_snapshot_id"] == result["state_snapshot_id"])
        assert [row["role"] for row in state["visible_message_refs"]] == ["user", "assistant"]
        checkpoint = next(row for row in bundle["workflow_checkpoint"]
                          if row["checkpoint_id"] == candidate["checkpoint_id"])
        assert state["selection_refs"] == checkpoint["selection_refs"]
        assert candidate["output_id"] == checkpoint["output_id"]
    with running_service(
        database, lambda stage: pytest.fail(f"reopen redispatched {stage}"),
    ) as (_, port):
        assert request(port, "GET", f"/api/sessions/{sid}")[1]["messages"] == view["messages"]
        assert head(read_bundle(database), sid)["head_commit_id"] == result["commit_id"]


def test_http_failed_b_preserves_seed_head_without_complete_candidate(database):
    class FailedAdapter:
        def generate(self, messages, tools):
            request = httpx.Request("POST", "https://offline.invalid/chat/completions")
            raise httpx.HTTPStatusError(
                "offline B failure", request=request,
                response=httpx.Response(400, request=request),
            )

    def factory(stage):
        return FailedAdapter() if stage == "B" else OfflineAdapter(stage)

    with running_service(database, factory) as (service, port):
        sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
        root_ref = head(read_bundle(database), sid)
        assert request(port, "POST", f"/api/sessions/{sid}/inputs", {
            "text": "B will fail", "idempotency_key": "submit",
        })[0] == 202
        service.wait_for_idle(sid)
        status, view = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and view["error"]["code"] == "model_error"
        assert [row["role"] for row in view["messages"]] == ["user"]
        failed = read_bundle(database)
        assert head(failed, sid) == root_ref
        assert rows(failed, "workflow_candidate", sid) == []
        assert rows(failed, "workflow_commit", sid) == [seed(failed, sid)]
    with running_service(
        database, lambda stage: pytest.fail(f"reopen redispatched {stage}"),
    ) as (_, port):
        assert request(port, "GET", f"/api/sessions/{sid}")[1]["error"]["code"] == "RECOVERY_UNAVAILABLE"
        assert head(read_bundle(database), sid) == root_ref


def test_http_branch_seed_and_pending_first_start_move_only_child_head(database):
    started, release = threading.Event(), threading.Event()
    pause_child = False

    class BlockingAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            if pause_child and self.stage == "A":
                started.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with running_service(database, BlockingAdapter) as (service, port):
        sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
        assert request(port, "POST", f"/api/sessions/{sid}/inputs", {
            "text": "Fork this request", "idempotency_key": "submit",
        })[0] == 202
        service.wait_for_idle(sid)
        source = request(port, "GET", f"/api/sessions/{sid}")[1]
        user_id, assistant_id = [row["visible_message_id"] for row in source["messages"]]
        source_bundle = read_bundle(database)
        source_root = seed(source_bundle, sid)
        source_head = head(source_bundle, sid)

        status, assistant_branch = request(port, "POST", f"/api/sessions/{sid}/branches", {
            "visible_message_id": assistant_id, "idempotency_key": "assistant",
            "expected_source_revision": source["revision"],
        })
        assert status == 201
        assistant_child = assistant_branch["workflow_session_id"]
        assistant_seed = seed(read_bundle(database), assistant_child)
        assert assistant_seed["parent_commit_id"] == source_head["head_commit_id"]
        assert head(read_bundle(database), assistant_child)["head_commit_id"] == assistant_seed["commit_id"]

        revision = request(port, "GET", f"/api/sessions/{sid}")[1]["revision"]
        status, user_branch = request(port, "POST", f"/api/sessions/{sid}/branches/switch", {
            "visible_message_id": user_id, "idempotency_key": "user",
            "expected_source_revision": revision,
            "expected_selection_revision": request(port, "GET", "/api/active-session")[1]["revision"],
        })
        assert status == 201
        child = user_branch["workflow_session_id"]
        pending_id = user_branch["pending_input_id"]
        child_bundle = read_bundle(database)
        child_seed = seed(child_bundle, child)
        assert child_seed["parent_commit_id"] == source_root["commit_id"]
        child_head = head(child_bundle, child)
        child_state = next(row for row in child_bundle["state_snapshot"]
                           if row["state_snapshot_id"] == child_seed["state_snapshot_id"])
        assert child_state["pending_input_id"] == pending_id
        assert head(child_bundle, sid) == source_head

        try:
            pause_child = True
            child_revision = request(port, "GET", f"/api/sessions/{child}")[1]["revision"]
            status, continued = request(
                port, "POST", f"/api/sessions/{child}/pending-inputs/{pending_id}/continue",
                {"idempotency_key": "first-start", "expected_session_revision": child_revision},
            )
            assert status == 202 and started.wait(10)
            running = read_bundle(database)
            assert head(running, child) == child_head
            assert rows(running, "workflow_candidate", child) == []
        finally:
            release.set()
        service.wait_for_idle(child)
        status, completed = request(port, "GET", f"/api/sessions/{child}")
        assert status == 200 and completed["error"] is None
        assert [row["role"] for row in completed["messages"]] == ["user", "assistant"]
        bundle = read_bundle(database)
        candidates = rows(bundle, "workflow_candidate", child)
        assert len(candidates) == 1
        assert candidates[0]["chain_run_id"] == continued["chain_run_id"]
        assert candidates[0]["base_commit_id"] == child_seed["commit_id"]
        assert head(bundle, child)["head_commit_id"] == candidates[0]["result_commit_id"]
        assert head(bundle, child)["revision"] == child_head["revision"] + 1
        assert head(bundle, sid) == source_head
