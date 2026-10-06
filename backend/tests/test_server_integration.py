"""Offline HTTP -> WorkflowService -> SQLite control-route integration tests."""

import http.client
import json
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent import workflow as workflow_module
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.server import create_server
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import B_BINDING, OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="workflow-http-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder) / "workflow.sqlite"


@pytest.fixture
def execution_counts(monkeypatch):
    model_calls = []
    tool_calls = []
    original_register = workflow_module.register_callable

    def counting_register(name, description, schema, implementation):
        if name == "inspect_text":
            original_tool = implementation

            def counted_tool(text):
                tool_calls.append(text)
                return original_tool(text)

            implementation = counted_tool
        return original_register(name, description, schema, implementation)

    class CountingAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            model_calls.append(self.stage)
            return super().generate(messages, tools)

    monkeypatch.setattr(workflow_module, "register_callable", counting_register)
    return CountingAdapter, model_calls, tool_calls


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
    assert set(body) == {"error"}


def create_completed_source(port, service, text="The first request"):
    status, created = request(port, "POST", "/api/sessions", {})
    assert status == 201
    sid = created["workflow_session_id"]
    status, submitted = request(
        port, "POST", f"/api/sessions/{sid}/inputs",
        {"text": text, "idempotency_key": "submit-once"},
    )
    assert status == 202
    service.wait_for_idle(sid)
    status, view = request(port, "GET", f"/api/sessions/{sid}")
    assert status == 200 and view["error"] is None
    assert [message["role"] for message in view["messages"]] == ["user", "assistant"]
    assert view["chains"] == [{"chain_run_id": submitted["chain_run_id"], "status": "succeeded"}]
    return sid, submitted, view


def test_archive_retry_real_http_replays_without_model_or_tool_redispatch(
    database, execution_counts, monkeypatch,
):
    factory, model_calls, tool_calls = execution_counts
    original_archive = SqliteStore.archive_success

    def fail_b_archive(store, records, key):
        run = next(row for kind, row in records if kind == "run_record")
        if run["node_binding_id"] == B_BINDING:
            raise RuntimeError("simulated archive failure")
        return original_archive(store, records, key)

    with running_service(database, factory) as (service, port):
        status, created = request(port, "POST", "/api/sessions", {})
        assert status == 201
        sid = created["workflow_session_id"]
        with monkeypatch.context() as patch:
            patch.setattr(SqliteStore, "archive_success", fail_b_archive)
            assert request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "Archive this result", "idempotency_key": "submit",
            })[0] == 202
            service.wait_for_idle(sid)
        status, view = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and view["available_actions"] == ["retry_archive"]
        assert view["error"]["code"] == "STORAGE_OR_COORDINATION_FAILED"
        assert [row["role"] for row in view["messages"]] == ["user"]
        assert (model_calls, len(tool_calls)) == (["A", "A", "B", "B"], 2)
        a_run, b_run = view["nodes"][0]["run_id"], view["nodes"][1]["run_id"]
        path = f"/api/sessions/{sid}/runs/{b_run}/retry-archive"
        payload = {"idempotency_key": "archive-retry", "expected_session_revision": view["revision"]}
        assert_conflict(port, path, {**payload, "expected_session_revision": view["revision"] - 1})
        assert_conflict(port, f"/api/sessions/{sid}/runs/{a_run}/retry-archive", payload)
        assert_conflict(port, f"/api/sessions/{sid}/runs/{b_run}/retry-publish", payload)

        status, receipt = request(port, "POST", path, payload)
        assert status == 200 and receipt == {
            "workflow_session_id": sid, "run_id": b_run,
            "chain_run_id": view["chains"][0]["chain_run_id"], "status": "succeeded",
        }
        assert request(port, "POST", path, payload) == (200, receipt)
        assert_conflict(port, f"/api/sessions/{sid}/runs/{a_run}/retry-archive", payload)
        assert_conflict(port, path, {
            **payload, "expected_session_revision": view["revision"] + 1,
        })
        assert_conflict(port, path, {**payload, "idempotency_key": "new-attempt"})
        assert (model_calls, len(tool_calls)) == (["A", "A", "B", "B"], 2)
        assert [row["role"] for row in request(port, "GET", f"/api/sessions/{sid}")[1]["messages"]] == [
            "user", "assistant",
        ]
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"reopen redispatched {stage}"),
    )) as reopened:
        assert [row["role"] for row in reopened.get_session(sid)["messages"]] == ["user", "assistant"]
    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
        assert len(bundle["turn"]) == 2
        assert len(bundle["output_delivery"]) == 1
        assert len(bundle["chain_run"]) == 1


def test_publish_retry_real_http_replays_without_model_or_tool_redispatch(
    database, execution_counts, monkeypatch,
):
    factory, model_calls, tool_calls = execution_counts

    def fail_publish(service, sid, chain_id, upstream):
        raise RuntimeError("simulated publish failure")

    with running_service(database, factory) as (service, port):
        status, created = request(port, "POST", "/api/sessions", {})
        assert status == 201
        sid = created["workflow_session_id"]
        with monkeypatch.context() as patch:
            patch.setattr(WorkflowService, "_publish", fail_publish)
            assert request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "Publish this result", "idempotency_key": "submit",
            })[0] == 202
            service.wait_for_idle(sid)
        status, view = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and view["available_actions"] == ["retry_publish"]
        assert view["error"]["code"] == "STORAGE_OR_COORDINATION_FAILED"
        assert [row["role"] for row in view["messages"]] == ["user"]
        assert (model_calls, len(tool_calls)) == (["A", "A", "B", "B"], 2)
        a_run, b_run = view["nodes"][0]["run_id"], view["nodes"][1]["run_id"]
        path = f"/api/sessions/{sid}/runs/{b_run}/retry-publish"
        payload = {"idempotency_key": "publish-retry", "expected_session_revision": view["revision"]}
        assert_conflict(port, path, {**payload, "expected_session_revision": view["revision"] - 1})
        assert_conflict(port, f"/api/sessions/{sid}/runs/{a_run}/retry-publish", payload)
        assert_conflict(port, f"/api/sessions/{sid}/runs/{b_run}/retry-archive", payload)

        status, receipt = request(port, "POST", path, payload)
        assert status == 200 and receipt["status"] == "succeeded"
        assert receipt["run_id"] == b_run
        assert request(port, "POST", path, payload) == (200, receipt)
        assert_conflict(port, f"/api/sessions/{sid}/runs/{a_run}/retry-publish", payload)
        assert_conflict(port, path, {
            **payload, "expected_session_revision": view["revision"] + 1,
        })
        assert_conflict(port, path, {**payload, "idempotency_key": "new-attempt"})
        assert (model_calls, len(tool_calls)) == (["A", "A", "B", "B"], 2)
        assert [row["role"] for row in request(port, "GET", f"/api/sessions/{sid}")[1]["messages"]] == [
            "user", "assistant",
        ]
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"reopen redispatched {stage}"),
    )) as reopened:
        assert [row["role"] for row in reopened.get_session(sid)["messages"]] == ["user", "assistant"]
    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
        assert len(bundle["turn"]) == 2
        assert len(bundle["output_delivery"]) == 1
        assert len(bundle["chain_run"]) == 1


def test_branch_routes_and_pending_first_start_real_http(
    database, execution_counts,
):
    factory, model_calls, tool_calls = execution_counts
    with running_service(database, factory) as (service, port):
        source, submission, source_view = create_completed_source(port, service)
        user_id, assistant_id = [row["visible_message_id"] for row in source_view["messages"]]
        assert (model_calls, len(tool_calls)) == (["A", "A", "B", "B"], 2)
        branch_path = f"/api/sessions/{source}/branches"
        switch_path = branch_path + "/switch"
        branch = {
            "visible_message_id": assistant_id, "idempotency_key": "assistant-branch",
            "expected_source_revision": source_view["revision"],
        }
        status, assistant_receipt = request(port, "POST", branch_path, branch)
        assert status == 201 and assistant_receipt["role"] == "assistant"
        assert assistant_receipt["active_workflow_session_id"] == source
        assert assistant_receipt["pending_input_id"] is None
        assert request(port, "POST", branch_path, branch) == (201, assistant_receipt)
        assert_conflict(port, branch_path, {**branch, "visible_message_id": user_id})
        assert_conflict(port, branch_path, {
            **branch, "expected_source_revision": source_view["revision"] + 1,
        })
        assert_conflict(port, branch_path, {**branch, "idempotency_key": "stale"})
        cross_kind = {
            **branch, "expected_source_revision": source_view["revision"] + 1,
            "expected_selection_revision": request(port, "GET", "/api/active-session")[1]["revision"],
        }
        status, switched_assistant = request(port, "POST", switch_path, cross_kind)
        assert status == 201 and switched_assistant["role"] == "assistant"
        assert switched_assistant["workflow_session_id"] != assistant_receipt["workflow_session_id"]
        assert switched_assistant["active_workflow_session_id"] == switched_assistant["workflow_session_id"]
        assert request(port, "POST", switch_path, cross_kind) == (201, switched_assistant)
        status, assistant_child = request(
            port, "GET", f"/api/sessions/{assistant_receipt['workflow_session_id']}",
        )
        assert status == 200 and [row["role"] for row in assistant_child["messages"]] == [
            "user", "assistant",
        ]
        assert assistant_child["chains"] == []

        source_revision = request(port, "GET", f"/api/sessions/{source}")[1]["revision"]
        switch = {
            "visible_message_id": user_id, "idempotency_key": "user-switch",
            "expected_source_revision": source_revision,
            "expected_selection_revision": request(port, "GET", "/api/active-session")[1]["revision"],
        }
        status, user_receipt = request(port, "POST", switch_path, switch)
        assert status == 201 and user_receipt["role"] == "user"
        child = user_receipt["workflow_session_id"]
        assert user_receipt["active_workflow_session_id"] == child
        assert child != assistant_receipt["workflow_session_id"]
        assert request(port, "POST", switch_path, switch) == (201, user_receipt)
        assert_conflict(port, switch_path, {**switch, "visible_message_id": assistant_id})
        assert_conflict(port, switch_path, {
            **switch, "expected_source_revision": source_revision + 1,
        })
        assert_conflict(port, switch_path, {**switch, "idempotency_key": "stale"})
        assert (model_calls, len(tool_calls)) == (["A", "A", "B", "B"], 2)

        status, pending = request(port, "GET", f"/api/sessions/{child}")
        assert status == 200 and pending["revision"] == 1
        assert [row["visible_message_id"] for row in pending["messages"]] == [user_id]
        assert pending["chains"] == [] and not pending["can_submit"]
        input_id = user_receipt["pending_input_id"]
        assert pending["pending_input_id"] == input_id
        assert pending["available_actions"] == ["continue_pending_input"]
        pending_path = f"/api/sessions/{child}/pending-inputs/{input_id}/continue"
        start = {"idempotency_key": "first-start", "expected_session_revision": pending["revision"]}
        assert_conflict(port, pending_path, {**start, "expected_session_revision": pending["revision"] + 1})
        assert_conflict(
            port, f"/api/sessions/{child}/pending-inputs/not-the-input/continue", start,
        )
        status, continued = request(port, "POST", pending_path, start)
        assert status == 202 and continued["status"] == "prepared"
        assert continued["visible_message_id"] == user_id
        assert continued["input_id"] == input_id
        assert continued["chain_run_id"] != submission["chain_run_id"]
        service.wait_for_idle(child)
        assert request(port, "POST", pending_path, start) == (202, continued)
        assert_conflict(port, pending_path, {
            **start, "expected_session_revision": pending["revision"] + 1,
        })
        assert_conflict(
            port, f"/api/sessions/{child}/pending-inputs/another-input/continue", start,
        )
        assert_conflict(port, pending_path, {**start, "idempotency_key": "new-start"})
        assert (model_calls, len(tool_calls)) == (
            ["A", "A", "B", "B", "A", "A", "B", "B"], 4,
        )
        status, completed = request(port, "GET", f"/api/sessions/{child}")
        assert status == 200 and completed["error"] is None
        assert completed["pending_input_id"] is None
        assert "continue_pending_input" not in completed["available_actions"]
        assert [row["role"] for row in completed["messages"]] == ["user", "assistant"]
        assert completed["chains"] == [{
            "chain_run_id": continued["chain_run_id"], "status": "succeeded",
        }]
    with running_service(
        database, lambda stage: pytest.fail(f"reopen redispatched {stage}"),
    ) as (_, port):
        assert request(port, "POST", branch_path, branch) == (201, assistant_receipt)
        assert request(port, "POST", switch_path, switch) == (201, user_receipt)
        assert request(port, "POST", pending_path, start) == (202, continued)
        assert [row["role"] for row in request(
            port, "GET", f"/api/sessions/{child}",
        )[1]["messages"]] == ["user", "assistant"]
        assert [row["role"] for row in request(
            port, "GET", f"/api/sessions/{source}",
        )[1]["messages"]] == ["user", "assistant"]
    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
        assert len(bundle["fork_anchor"]) == 3
        assert len(bundle["chain_run"]) == 2
        assert len(bundle["turn"]) == 4
        assert len(bundle["output_delivery"]) == 2


def test_branch_routes_reject_active_execution_over_real_http(database):
    started, release = threading.Event(), threading.Event()

    class BlockingAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            started.set()
            assert release.wait(10)
            return super().generate(messages, tools)

    with running_service(database, BlockingAdapter) as (service, port):
        status, created = request(port, "POST", "/api/sessions", {})
        assert status == 201
        sid = created["workflow_session_id"]
        try:
            status, submission = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "Source is still running", "idempotency_key": "submit",
            })
            assert status == 202 and started.wait(5)
            revision = request(port, "GET", f"/api/sessions/{sid}")[1]["revision"]
            for path in (f"/api/sessions/{sid}/branches", f"/api/sessions/{sid}/branches/switch"):
                expected_selection = (
                    {"expected_selection_revision": request(port, "GET", "/api/active-session")[1]["revision"]}
                    if path.endswith("/switch") else {}
                )
                assert_conflict(port, path, {
                    "visible_message_id": submission["visible_message_id"],
                    "idempotency_key": path.rsplit("/", 1)[-1],
                    "expected_source_revision": revision,
                    **expected_selection,
                })
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert request(port, "GET", f"/api/sessions/{sid}")[1]["error"] is None


def test_reopen_reads_failed_archive_but_cannot_resume_active_result_over_http(
    database, execution_counts, monkeypatch,
):
    factory, model_calls, tool_calls = execution_counts
    original_archive = SqliteStore.archive_success

    def fail_b_archive(store, records, key):
        run = next(row for kind, row in records if kind == "run_record")
        if run["node_binding_id"] == B_BINDING:
            raise RuntimeError("simulated archive failure")
        return original_archive(store, records, key)

    with running_service(database, factory) as (service, port):
        sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
        with monkeypatch.context() as patch:
            patch.setattr(SqliteStore, "archive_success", fail_b_archive)
            assert request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "Keep the accepted result", "idempotency_key": "submit",
            })[0] == 202
            service.wait_for_idle(sid)
        view = request(port, "GET", f"/api/sessions/{sid}")[1]
        run_id = view["nodes"][1]["run_id"]
        assert view["available_actions"] == ["retry_archive"]
        assert (model_calls, len(tool_calls)) == (["A", "A", "B", "B"], 2)
    with running_service(
        database, lambda stage: pytest.fail(f"reopen redispatched {stage}"),
    ) as (_, port):
        status, recovered = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200
        assert recovered["error"]["code"] == "RECOVERY_UNAVAILABLE"
        assert recovered["available_actions"] == ["close_execution"]
        assert [row["role"] for row in recovered["messages"]] == ["user"]
        assert recovered["nodes"][1]["status"] == "recovery_unavailable"
        assert_conflict(
            port, f"/api/sessions/{sid}/runs/{run_id}/retry-archive",
            {"idempotency_key": "after-restart", "expected_session_revision": recovered["revision"]},
        )
        status, closed = request(
            port, "POST", f"/api/sessions/{sid}/chains/{recovered['recovery_chain_run_id']}/close_execution",
            {
                "idempotency_key": "close-unavailable-result",
                "expected_session_revision": recovered["revision"],
                "expected_ref_revision": recovered["ref_revision"],
                "expected_head_commit_id": recovered["head_commit_id"],
            },
        )
        assert status == 200 and closed["status"] == "closed"
        current = request(port, "GET", f"/api/sessions/{sid}")[1]
        assert not current["can_continue_workflow"]
        assert current["available_actions"] == ["reroll"]
        assert_conflict(
            port, f"/api/sessions/{sid}/chains/{closed['chain_run_id']}/continue_workflow", {
                "idempotency_key": "not-model-recoverable",
                "expected_session_revision": current["revision"],
                "expected_ref_revision": current["ref_revision"],
                "expected_head_commit_id": current["head_commit_id"],
            },
        )
        assert (model_calls, len(tool_calls)) == (["A", "A", "B", "B"], 2)
