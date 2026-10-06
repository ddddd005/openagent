"""HTTP fork candidate choice and child-local inherited selection."""

import http.client
import json
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

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


def branch_payload(message_id, *, key, revision, candidate_id=None):
    result = {
        "visible_message_id": message_id,
        "idempotency_key": key,
        "expected_source_revision": revision,
    }
    if candidate_id is not None:
        result["candidate_id"] = candidate_id
    return result


def candidate_path(session_id, candidate_id):
    return f"/api/sessions/{session_id}/candidates/{candidate_id}/select"


class RecordingService:
    def __init__(self):
        self.calls = []

    def create_branch(
        self, session_id, message_id, *, idempotency_key, expected_source_revision,
        candidate_id=None,
    ):
        self.calls.append((
            "branch", session_id, message_id, idempotency_key,
            expected_source_revision, candidate_id,
        ))
        return {"workflow_session_id": "child", "role": "assistant"}

    def create_and_switch_branch(
        self, session_id, message_id, *, idempotency_key, expected_source_revision,
        expected_selection_revision, candidate_id=None,
    ):
        self.calls.append((
            "switch", session_id, message_id, idempotency_key,
            expected_source_revision, expected_selection_revision, candidate_id,
        ))
        return {"workflow_session_id": "child", "role": "assistant"}

    def select_candidate(
        self, session_id, candidate_id, *, idempotency_key,
        expected_session_revision, expected_ref_revision, expected_head_commit_id,
    ):
        self.calls.append((
            "inherited", session_id, candidate_id, idempotency_key,
            expected_session_revision, expected_ref_revision, expected_head_commit_id,
        ))
        return {
            "workflow_session_id": session_id, "candidate_id": candidate_id,
            "head_commit_id": "child_head", "ref_revision": 2,
            "session_revision": 2, "status": "succeeded",
        }


def test_branch_optional_candidate_and_shared_selection_route_validate_exact_json():
    service = RecordingService()
    branch = branch_payload("message_1", key="fork", revision=2, candidate_id="candidate_1")
    inherited = {
        "idempotency_key": "select",
        "expected_session_revision": 1,
        "expected_ref_revision": 1,
        "expected_head_commit_id": "head_1",
    }
    with running_server(service) as port:
        for path, payload in (
            ("/api/sessions/source_1/branches", branch),
            ("/api/sessions/source_1/branches/switch",
             {**branch, "expected_selection_revision": 1}),
        ):
            assert request(port, "POST", path, payload)[0] == 201
        assert request(port, "POST", "/api/sessions/source_1/branches", {
            **branch, "idempotency_key": "legacy", "candidate_id": None,
        })[0] == 400
        for value in ("", "bad.id", 1, True):
            assert request(port, "POST", "/api/sessions/source_1/branches", {
                **branch, "candidate_id": value,
            })[0] == 400
        assert request(port, "POST", "/api/sessions/source_1/branches", {
            **branch, "unexpected": "ignored",
        })[0] == 400
        assert request(port, "POST", "/api/sessions/source_1/branches", branch_payload(
            "message_1", key="legacy", revision=2,
        ))[0] == 201
        path = candidate_path("child_1", "candidate_1")
        for bad in (
            *({key: value for key, value in inherited.items() if key != field}
              for field in inherited),
            {**inherited, "extra": 1},
            {**inherited, "expected_session_revision": True},
            {**inherited, "expected_ref_revision": 0},
            {**inherited, "expected_head_commit_id": "bad.id"},
        ):
            assert request(port, "POST", path, bad)[0] == 400
        assert request(port, "GET", path)[0] == 404
        for bad_path in (path + "/extra", path + "?x=1",
                         candidate_path("bad.id", "candidate_1"),
                         candidate_path("child_1", "bad.id"),
                         "/api/sessions/child_1/inherited-candidates/candidate_1/select"):
            assert request(port, "POST", bad_path, inherited)[0] == 404
        assert request(port, "POST", path, inherited)[0] == 200
        assert service.calls == [
            ("branch", "source_1", "message_1", "fork", 2, "candidate_1"),
            ("switch", "source_1", "message_1", "fork", 2, 1, "candidate_1"),
            ("branch", "source_1", "message_1", "legacy", 2, None),
            ("inherited", "child_1", "candidate_1", "select", 1, 1, "head_1"),
        ]


def test_http_fork_freezes_both_candidates_and_child_selection_is_local():
    with TemporaryDirectory(prefix="http-inherited-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=OfflineAdapter)) as service:
            with running_server(service) as port:
                source = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
                submitted = request(port, "POST", f"/api/sessions/{source}/inputs", {
                    "text": "Keep both results at fork", "idempotency_key": "submit",
                })[1]
                service.wait_for_idle(source)
                before = request(port, "GET", f"/api/sessions/{source}")[1]
                original = before["messages"][-1]["reply_candidates"][0]
                reroll = request(
                    port, "POST", f"/api/sessions/{source}/chains/{submitted['chain_run_id']}/reroll",
                    {
                        "idempotency_key": "reroll",
                        "expected_session_revision": before["revision"],
                        "expected_ref_revision": before["ref_revision"],
                        "expected_head_commit_id": before["head_commit_id"],
                    },
                )
                assert reroll[0] == 202
                service.wait_for_idle(source)
                current = request(port, "GET", f"/api/sessions/{source}")[1]
                assert len(current["messages"][-1]["reply_candidates"]) == 2
                alternative = current["messages"][-1]["reply_candidates"][-1]
                assert [row["selected"] for row in current["messages"][-1][
                    "reply_candidates"
                ]] == [False, True]
                assistant_id = current["messages"][-1]["visible_message_id"]
                fork = branch_payload(
                    assistant_id, key="from-original", revision=current["revision"],
                    candidate_id=original["candidate_id"],
                )
                branch_path = f"/api/sessions/{source}/branches"
                status, created = request(port, "POST", branch_path, fork)
                assert status == 201 and created["role"] == "assistant"
                child_id = created["workflow_session_id"]
                assert request(port, "POST", branch_path, fork) == (201, created)
                assert request(port, "POST", branch_path, {
                    **fork, "candidate_id": alternative["candidate_id"],
                })[0] == 409
                assert request(port, "POST", branch_path, {
                    **fork, "idempotency_key": "stale",
                })[0] == 409
                assert request(port, "POST", branch_path, {
                    **fork, "idempotency_key": "not-an-assistant",
                    "visible_message_id": current["messages"][0]["visible_message_id"],
                })[0] == 409
                source_after = request(port, "GET", f"/api/sessions/{source}")[1]
                assert source_after["head_commit_id"] == current["head_commit_id"]
                assert source_after["messages"] == current["messages"]

                child = request(port, "GET", f"/api/sessions/{child_id}")[1]
                floors = child["messages"][-1]["reply_candidates"]
                assert [row["candidate_id"] for row in floors] == [
                    original["candidate_id"], alternative["candidate_id"],
                ]
                assert [row["selected"] for row in floors] == [True, False]
                assert child["messages"][-1]["chain_run_id"] == original["chain_run_id"]
                child_head = child["head_commit_id"]
                with closing(SqliteStore(database)) as store:
                    archived = store.read_bundle()
                source_ref = next(row for row in archived["workflow_ref"]
                                  if row["workflow_session_id"] == source)
                child_ref = next(row for row in archived["workflow_ref"]
                                 if row["workflow_session_id"] == child_id)
                assert child_ref["head_commit_id"] == child_head
                assert child_ref["workflow_ref_id"] != source_ref["workflow_ref_id"]

                select = {
                    "idempotency_key": "choose-in-child",
                    "expected_session_revision": child["revision"],
                    "expected_ref_revision": child["ref_revision"],
                    "expected_head_commit_id": child_head,
                }
                path = candidate_path(child_id, alternative["candidate_id"])
                status, selected = request(port, "POST", path, select)
                assert status == 200 and selected["status"] == "succeeded"
                assert selected["head_commit_id"] != child_head
                assert request(port, "POST", path, select) == (200, selected)
                assert request(port, "POST", path, {
                    **select, "idempotency_key": "stale",
                })[0] == 409
                selected_child = request(port, "GET", f"/api/sessions/{child_id}")[1]
                assert selected_child["head_commit_id"] == selected["head_commit_id"]
                assert selected_child["messages"][-1]["chain_run_id"] == alternative[
                    "chain_run_id"
                ]
                assert [row["selected"] for row in selected_child["messages"][-1][
                    "reply_candidates"
                ]] == [False, True]
                assert request(port, "GET", f"/api/sessions/{source}")[1][
                    "messages"
                ] == current["messages"]
                with closing(SqliteStore(database)) as store:
                    after = store.read_bundle()
                assert next(row for row in after["workflow_ref"]
                            if row["workflow_session_id"] == source) == source_ref
                assert [row for row in after["workflow_candidate"]
                        if row["workflow_session_id"] == source] == [
                    row for row in archived["workflow_candidate"]
                    if row["workflow_session_id"] == source
                ]

        with closing(WorkflowService(database, model_factory=OfflineAdapter)) as reopened:
            with running_server(reopened) as port:
                assert request(port, "POST", branch_path, fork) == (201, created)
                assert request(port, "POST", path, select) == (200, selected)
                restored = request(port, "GET", f"/api/sessions/{child_id}")[1]
                assert restored["head_commit_id"] == selected["head_commit_id"]
                assert [row["selected"] for row in restored["messages"][-1][
                    "reply_candidates"
                ]] == [False, True]


def test_historical_floor_requires_fork_before_choosing_or_rerolling():
    with TemporaryDirectory(prefix="http-history-fork-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=OfflineAdapter)) as service:
            with running_server(service) as port:
                source = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
                first = request(port, "POST", f"/api/sessions/{source}/inputs", {
                    "text": "First floor", "idempotency_key": "first",
                })[1]
                service.wait_for_idle(source)
                before = request(port, "GET", f"/api/sessions/{source}")[1]
                original = before["messages"][-1]["reply_candidates"][0]
                assert request(
                    port, "POST",
                    f"/api/sessions/{source}/chains/{first['chain_run_id']}/reroll",
                    {
                        "idempotency_key": "first-reroll",
                        "expected_session_revision": before["revision"],
                        "expected_ref_revision": before["ref_revision"],
                        "expected_head_commit_id": before["head_commit_id"],
                    },
                )[0] == 202
                service.wait_for_idle(source)
                first_floor = request(port, "GET", f"/api/sessions/{source}")[1]
                assert len(first_floor["messages"][-1]["reply_candidates"]) == 2
                first_assistant_id = first_floor["messages"][-1]["visible_message_id"]
                first_selected_chain = first_floor["messages"][-1]["chain_run_id"]
                assert request(port, "POST", f"/api/sessions/{source}/inputs", {
                    "text": "Second floor", "idempotency_key": "second",
                })[0] == 202
                service.wait_for_idle(source)
                current = request(port, "GET", f"/api/sessions/{source}")[1]
                assert [row["role"] for row in current["messages"]] == [
                    "user", "assistant", "user", "assistant",
                ]
                assert len(current["messages"][1]["reply_candidates"]) == 2
                assert request(
                    port, "POST", candidate_path(source, original["candidate_id"]), {
                        "idempotency_key": "historical-direct-select",
                        "expected_session_revision": current["revision"],
                        "expected_ref_revision": current["ref_revision"],
                        "expected_head_commit_id": current["head_commit_id"],
                    },
                )[0] == 409
                assert request(
                    port, "POST", f"/api/sessions/{source}/chains/{first_selected_chain}/reroll", {
                        "idempotency_key": "historical-direct-reroll",
                        "expected_session_revision": current["revision"],
                        "expected_ref_revision": current["ref_revision"],
                        "expected_head_commit_id": current["head_commit_id"],
                    },
                )[0] == 409
                path = f"/api/sessions/{source}/branches"
                default_fork = branch_payload(
                    first_assistant_id, key="historical-default",
                    revision=current["revision"],
                )
                status, default_created = request(port, "POST", path, default_fork)
                assert status == 201
                default_child = request(
                    port, "GET",
                    f"/api/sessions/{default_created['workflow_session_id']}",
                )[1]
                assert [row["role"] for row in default_child["messages"]] == [
                    "user", "assistant",
                ]
                assert default_child["messages"][-1]["chain_run_id"] == first_selected_chain
                assert [row["selected"] for row in default_child["messages"][-1][
                    "reply_candidates"
                ]] == [False, True]
                assert request(port, "POST", path, default_fork) == (201, default_created)
                refreshed = request(port, "GET", f"/api/sessions/{source}")[1]
                fork = branch_payload(
                    first_assistant_id, key="historical-fork",
                    revision=refreshed["revision"], candidate_id=original["candidate_id"],
                )
                status, created = request(port, "POST", path, fork)
                assert status == 201 and created["role"] == "assistant"
                child_id = created["workflow_session_id"]
                assert request(port, "POST", path, fork) == (201, created)
                child = request(port, "GET", f"/api/sessions/{child_id}")[1]
                assert [row["role"] for row in child["messages"]] == ["user", "assistant"]
                assert child["messages"][-1]["chain_run_id"] == original["chain_run_id"]
                assert [row["candidate_id"] for row in child["messages"][-1][
                    "reply_candidates"
                ]] == [row["candidate_id"] for row in current["messages"][1][
                    "reply_candidates"
                ]]
                assert [row["selected"] for row in child["messages"][-1][
                    "reply_candidates"
                ]] == [True, False]
                assert request(port, "GET", f"/api/sessions/{source}")[1][
                    "messages"
                ] == current["messages"]
                with closing(SqliteStore(database)) as store:
                    bundle = store.read_bundle()
                child_seed = next(
                    row for row in bundle["workflow_commit"]
                    if row["workflow_session_id"] == child_id
                    and row["source"]["kind"] == "session_seed"
                )
                child_head = next(
                    row for row in bundle["workflow_ref"]
                    if row["workflow_session_id"] == child_id
                )
                assert child_seed["parent_commit_id"] == next(
                    row["result_commit_id"] for row in bundle["workflow_candidate"]
                    if row["candidate_id"] == original["candidate_id"]
                )
                assert child_head["head_commit_id"] == child["head_commit_id"] == child_seed[
                    "commit_id"
                ]
