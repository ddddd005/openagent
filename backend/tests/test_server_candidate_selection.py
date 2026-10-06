"""HTTP boundary for explicit selection of an archived, unpublished candidate."""

import http.client
import json
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_graph import validate_bundle
from phase1_agent.server import create_server
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="http-candidate-", dir=Path(__file__).parent) as folder:
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


def durable(database):
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


def operations(bundle, session_id):
    return [
        row for row in bundle["workflow_operation"]
        if row["scope"] == {"kind": "workflow_session", "id": session_id}
    ]


def assert_conflict(port, path, payload):
    status, body = request(port, "POST", path, payload)
    assert status == 409, (status, body)
    assert body == {
        "error": {
            "code": "conflict",
            "message": "请求与当前会话状态冲突",
        },
    }


def test_http_select_archived_candidate_replay_and_no_redispatch(database, monkeypatch):
    with running_service(database, OfflineAdapter) as (service, port):
        source = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
        other = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
        initial = durable(database)
        source_head = head(initial, source)
        other_head = head(initial, other)

        original_save = SqliteStore.save_bundle
        failures = []

        def fail_automatic_selection(store, records, key, **kwargs):
            if kwargs.get("operation") == "workflow.select_candidate" and not failures:
                failures.append(key)
                raise RuntimeError("private candidate selection fault")
            return original_save(store, records, key, **kwargs)

        with monkeypatch.context() as failure:
            failure.setattr(SqliteStore, "save_bundle", fail_automatic_selection)
            status, _ = request(port, "POST", f"/api/sessions/{source}/inputs", {
                "text": "Archive without publishing", "idempotency_key": "submit",
            })
            assert status == 202
            service.wait_for_idle(source)
        assert len(failures) == 1

        archived = durable(database)
        candidates = rows(archived, "workflow_candidate", source)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert head(archived, source) == source_head
        assert head(archived, other) == other_head
        assert any(
            row["commit_id"] == candidate["result_commit_id"]
            for row in rows(archived, "workflow_commit", source)
        )
        pending = [
            row for row in archived["output_delivery"]
            if row["target"] == {"kind": "ui", "workflow_session_id": source}
            and row["status"] == "pending"
        ]
        assert len(pending) == 1 and pending[0]["output_id"] == candidate["output_id"]
        assert [row["role"] for row in service.get_session(source)["messages"]] == ["user"]

    path = f"/api/sessions/{source}/candidates/{candidate['candidate_id']}/select"
    payload = {
        "idempotency_key": "select-once",
        "expected_session_revision": next(
            row["revision"] for row in rows(archived, "workflow_session", source)
        ),
        "expected_ref_revision": source_head["revision"],
        "expected_head_commit_id": source_head["head_commit_id"],
    }
    with running_service(
        database, lambda stage: pytest.fail(f"candidate selection redispatched {stage}"),
    ) as (service, port):
        original_select = service.select_candidate
        calls = []

        def observed_select(*args, **kwargs):
            calls.append((args, kwargs))
            return original_select(*args, **kwargs)

        service.select_candidate = observed_select
        bad_payloads = [
            {key: value for key, value in payload.items() if key != field}
            for field in payload
        ] + [
            {**payload, "extra": 1},
            {**payload, "idempotency_key": ""},
            {**payload, "idempotency_key": "x" * 129},
            {**payload, "idempotency_key": True},
            {**payload, "expected_session_revision": True},
            {**payload, "expected_session_revision": 0},
            {**payload, "expected_ref_revision": False},
            {**payload, "expected_ref_revision": 0},
            {**payload, "expected_ref_revision": "1"},
            {**payload, "expected_head_commit_id": None},
            {**payload, "expected_head_commit_id": ""},
            {**payload, "expected_head_commit_id": "bad.id"},
        ]
        for invalid in bad_payloads:
            status, body = request(port, "POST", path, invalid)
            assert status == 400, (invalid, status, body)
        assert calls == []
        assert request(port, "GET", path)[0] == 404
        assert request(port, "POST", path + "/extra", payload)[0] == 404
        assert request(port, "POST", path + "?revision=1", payload)[0] == 404
        assert request(
            port, "POST", path.replace(candidate["candidate_id"], "bad.id"), payload,
        )[0] == 404
        assert calls == []
        assert durable(database) == archived

        other_payload = {
            **payload,
            "expected_session_revision": next(
                row["revision"] for row in rows(archived, "workflow_session", other)
            ),
            "expected_ref_revision": other_head["revision"],
            "expected_head_commit_id": other_head["head_commit_id"],
        }
        assert_conflict(port, path.replace(source, other), other_payload)
        assert_conflict(port, path, {
            **payload, "expected_session_revision": payload["expected_session_revision"] + 1,
        })
        assert_conflict(port, path, {
            **payload, "expected_ref_revision": payload["expected_ref_revision"] + 1,
        })
        assert_conflict(port, path, {
            **payload, "expected_head_commit_id": other_head["head_commit_id"],
        })
        assert durable(database) == archived

        status, receipt = request(port, "POST", path, payload)
        assert status == 200 and receipt == {
            "workflow_session_id": source,
            "candidate_id": candidate["candidate_id"],
            "head_commit_id": candidate["result_commit_id"],
            "ref_revision": source_head["revision"] + 1,
            "session_revision": payload["expected_session_revision"] + 1,
            "status": "succeeded",
        }
        assert request(port, "POST", path, payload) == (200, receipt)
        assert_conflict(port, path, {
            **payload, "expected_session_revision": receipt["session_revision"],
        })
        selected = durable(database)
        assert head(selected, source)["head_commit_id"] == candidate["result_commit_id"]
        assert head(selected, other) == other_head
        assert rows(selected, "workflow_candidate", source) == candidates
        assert [row["role"] for row in service.get_session(source)["messages"]] == [
            "user", "assistant",
        ]
        for kind in ("chain_run", "run_record", "node_run", "workflow_commit"):
            assert rows(selected, kind, source) == rows(archived, kind, source)
        assert len(operations(selected, source)) == len(operations(archived, source)) + 1

    with running_service(
        database, lambda stage: pytest.fail(f"replay redispatched {stage}"),
    ) as (_, port):
        assert request(port, "POST", path, payload) == (200, receipt)
        assert durable(database) == selected
