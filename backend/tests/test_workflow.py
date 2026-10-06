"""End-to-end fixed workflow tests; no credentials or network model requests."""

import copy
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import httpx
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def test_successful_user_submission_runs_two_agents_and_independent_output_node(database):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        session = service.create_session()
        sid = session["workflow_session_id"]
        receipt = service.submit(sid, "A small writing request.", "one")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"] is None, view
        assert view["can_submit"]
        assert [row["role"] for row in view["messages"]] == ["user", "assistant"]
        assert all(node["status"] == "succeeded" for node in view["nodes"])
        assert calls == ["A", "B"]
        assert view["messages"][0]["visible_message_id"] == receipt["visible_message_id"]
        assert "A small writing request." in view["messages"][1]["payload"]["text"]
    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
    assert len(bundle["turn"]) == 2
    assert len(bundle["node_run"]) == 1
    assert len(bundle["input_snapshot"]) == 2
    assert len(bundle["workflow_output"]) == 3
    assert len(bundle["output_delivery"]) == 1
    assert all(row["status"] == "succeeded" for row in bundle["run_record"])
    second = next(row for row in bundle["input_snapshot"] if row["node_binding_id"] == B_BINDING)
    assert second["s0"][-1]["source"]["kind"] == "upstream_node"
    assert all("tool_result" == turn["messages"][-1]["blocks"][0]["kind"] for turn in bundle["turn"])


def test_two_workflow_turns_reopen_with_separate_node_histories(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        for index in range(2):
            service.submit(sid, f"Request {index}", str(index))
            service.wait_for_idle(sid)
            assert service.get_session(sid)["error"] is None
    with closing(WorkflowService(database)) as reopened:
        view = reopened.get_session(sid)
        assert view["can_submit"]
        assert len(view["messages"]) == 4
        assert len(reopened.list_sessions()) == 1
    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
    runs = {run["run_id"]: run for run in bundle["run_record"]}
    for binding in (A_BINDING, B_BINDING):
        turns = [turn for turn in bundle["turn"] if runs[turn["run_id"]]["node_binding_id"] == binding]
        assert turns[0]["parent_turn_id"] is None
        assert turns[1]["parent_turn_id"] == turns[0]["turn_id"]
    assert len(bundle["workflow_checkpoint"]) == 5


def test_submission_replay_is_durable_without_reexecution(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        original = service.submit(sid, "Same text", "same-key")
        assert service.submit(sid, "Same text", "same-key") == original
        service.wait_for_idle(sid)
        with pytest.raises(ContractValidationError, match="different input"):
            service.submit(sid, "Changed text", "same-key")
    with closing(WorkflowService(database, model_factory=lambda _: pytest.fail("must not redispatch"))) as service:
        assert service.submit(sid, "Same text", "same-key") == original
        assert len(service.get_session(sid)["messages"]) == 2


def test_failed_start_transaction_has_zero_model_and_tool_dispatch(database):
    armed = False
    calls = []

    def failure(point):
        if armed and point == "before_commit":
            raise RuntimeError("injected")

    with closing(WorkflowService(database, fault_injector=failure,
                                 model_factory=lambda stage: calls.append(stage))) as service:
        sid = service.create_session()["workflow_session_id"]
        armed = True
        with pytest.raises(RuntimeError, match="injected"):
            service.submit(sid, "Must not execute", "fault")
        armed = False
        assert calls == []
        view = service.get_session(sid)
        assert view["messages"] == []
        assert view["can_submit"]
    with closing(SqliteStore(database)) as store:
        assert store.list_records("run_record") == []
        assert store.list_records("input_snapshot") == []


def test_scheduler_failure_is_durable_and_idempotent_without_dispatch(database):
    calls = []

    def reject_dispatch(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("executor unavailable")

    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"must not create {stage} model"),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        service._executor.submit = reject_dispatch

        receipt = service.submit(sid, "Must not dispatch", "queue-failure")
        view = service.get_session(sid)
        assert receipt["status"] == "prepared"
        assert view["error"] == {
            "code": "DISPATCH_FAILED",
            "message": "The workflow could not be scheduled after durable acceptance. No model or tool was dispatched.",
        }
        assert not view["can_submit"]
        assert [row["role"] for row in view["messages"]] == ["user"]
        assert view["chains"][0]["status"] == "failed"
        assert calls

        replay = service.submit(sid, "Must not dispatch", "queue-failure")
        assert replay == receipt
        assert len(calls) == 1

        with closing(SqliteStore(database)) as store:
            bundle = validate_bundle(store.read_bundle())
            run = bundle["run_record"][0]
            chain = bundle["chain_run"][0]
            user_ref = next(row for row in bundle["visible_message_ref"] if row["role"] == "user")
            assert run["status"] == "failed"
            assert chain["status"] == "failed"
            assert user_ref["boundary"]["input_status"] == "failed"
            assert store.read_receipt("workflow.submit", sid + ":queue-failure") is not None


def test_accepted_final_archive_failure_keeps_result_without_redispatch(database, monkeypatch):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    original_archive = SqliteStore.archive_success

    def fail_b_archive(store, records, key):
        run = next(row for kind, row in records if kind == "run_record")
        if run["node_binding_id"] == B_BINDING:
            raise RuntimeError("archive unavailable")
        return original_archive(store, records, key)

    monkeypatch.setattr(SqliteStore, "archive_success", fail_b_archive)
    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        receipt = service.submit(sid, "Archive the accepted final", "archive-failure")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert receipt["status"] == "prepared"
        assert view["error"]["code"] == "STORAGE_OR_COORDINATION_FAILED"
        assert [row["role"] for row in view["messages"]] == ["user"]
        assert view["available_actions"] == ["retry_archive"]
        assert calls == ["A", "B"]
        assert len(service._active_results) == 1
        active_run_id = next(iter(service._active_results))

        with pytest.raises(ContractValidationError, match="unarchived result"):
            service.create_branch(
                sid, receipt["visible_message_id"], idempotency_key="blocked-final",
                expected_source_revision=view["revision"],
            )

        assert service.submit(sid, "Archive the accepted final", "archive-failure") == receipt
        assert calls == ["A", "B"]
        assert active_run_id in service._active_results

        with closing(SqliteStore(database)) as store:
            bundle = validate_bundle(store.read_bundle())
            assert len(bundle["turn"]) == 1
            assert bundle["run_record"][-1]["status"] == "final_ready"
            assert not bundle.get("visible_message", [])[1:]

        monkeypatch.setattr(SqliteStore, "archive_success", original_archive)
        retry = service.retry_archive(
            sid, active_run_id, idempotency_key="archive-retry",
            expected_session_revision=view["revision"],
        )
        assert retry["status"] == "succeeded"
        assert calls == ["A", "B"]
        assert service.get_session(sid)["error"] is None
        assert [row["role"] for row in service.get_session(sid)["messages"]] == ["user", "assistant"]
        assert service.retry_archive(
            sid, active_run_id, idempotency_key="archive-retry",
            expected_session_revision=view["revision"],
        ) == retry
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            service.retry_archive(
                sid, active_run_id, idempotency_key="archive-retry",
                expected_session_revision=view["revision"] + 1,
            )


def test_publish_failure_keeps_accepted_result_without_redispatch_or_assistant(database, monkeypatch):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    original_publish = WorkflowService._publish

    def fail_publish(service, sid, chain_id, upstream):
        raise RuntimeError("publish unavailable")

    monkeypatch.setattr(WorkflowService, "_publish", fail_publish)
    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        receipt = service.submit(sid, "Publish the accepted final", "publish-failure")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert receipt["status"] == "prepared"
        assert view["error"]["code"] == "STORAGE_OR_COORDINATION_FAILED"
        assert [row["role"] for row in view["messages"]] == ["user"]
        assert view["available_actions"] == ["retry_publish"]
        assert calls == ["A", "B"]
        assert len(service._active_results) == 1
        active_run_id = next(iter(service._active_results))

        assert service.submit(sid, "Publish the accepted final", "publish-failure") == receipt
        assert calls == ["A", "B"]
        assert active_run_id in service._active_results

        with closing(SqliteStore(database)) as store:
            bundle = validate_bundle(store.read_bundle())
            assert len(bundle["turn"]) == 2
            assert len(bundle["visible_message"]) == 1
            assert bundle["run_record"][-1]["status"] == "succeeded"
            assert bundle["chain_run"][0]["status"] == "running"

        monkeypatch.setattr(WorkflowService, "_publish", original_publish)
        retry = service.retry_publish(
            sid, active_run_id, idempotency_key="publish-retry",
            expected_session_revision=view["revision"],
        )
        assert retry["status"] == "succeeded"
        assert calls == ["A", "B"]
        assert service.get_session(sid)["error"] is None
        assert [row["role"] for row in service.get_session(sid)["messages"]] == ["user", "assistant"]
        assert service.retry_publish(
            sid, active_run_id, idempotency_key="publish-retry",
            expected_session_revision=view["revision"],
        ) == retry
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            service.retry_publish(
                sid, active_run_id, idempotency_key="publish-retry",
                expected_session_revision=view["revision"] + 1,
            )


@pytest.mark.parametrize("failed_stage,successful_turns", [("A", 0), ("B", 1)])
def test_model_failure_keeps_ui_user_but_never_creates_failed_turn(database, failed_stage, successful_turns):
    calls = []

    class FailedAdapter:
        def generate(self, messages, tools):
            request = httpx.Request("POST", "https://offline.invalid/chat/completions")
            raise httpx.HTTPStatusError(
                "private provider details", request=request,
                response=httpx.Response(400, request=request),
            )

    def factory(stage):
        calls.append(stage)
        return FailedAdapter() if stage == failed_stage else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Try a request", "failure")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"]["code"] == "model_error", view
        assert "private provider" not in str(view)
        assert [row["role"] for row in view["messages"]] == ["user"]
        assert not view["can_submit"]
        assert calls == (["A"] if failed_stage == "A" else ["A", "B"])
        with pytest.raises(ContractValidationError, match="unfinished"):
            service.submit(sid, "Unrelated next input", "new")
    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
        assert len(bundle.get("turn", [])) == successful_turns
    with closing(WorkflowService(database)) as reopened:
        assert reopened.get_session(sid)["error"]["code"] == "RECOVERY_UNAVAILABLE"
        assert not reopened.get_session(sid)["can_submit"]


def test_busy_session_rejects_second_submission_but_other_session_is_independent(database):
    started, release = Event(), Event()

    class BlockingAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            started.set()
            assert release.wait(10)
            return super().generate(messages, tools)

    with closing(WorkflowService(database, model_factory=BlockingAdapter)) as service:
        sid = service.create_session()["workflow_session_id"]
        other = service.create_session()["workflow_session_id"]
        try:
            service.submit(sid, "First", "one")
            assert started.wait(10)
            with pytest.raises(ContractValidationError, match="unfinished"):
                service.submit(sid, "Second", "two")
            assert service.get_session(other)["can_submit"]
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None


def test_second_live_owner_cannot_mark_running_database_recovered(database):
    with closing(WorkflowService(database)):
        with pytest.raises(ContractValidationError, match="live workflow owner"):
            WorkflowService(database)


def test_user_projection_excludes_private_snapshots_tools_and_full_turns(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "<script>bad()</script>", "escape")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"] is None
        assert "input_snapshot" not in view and "turn" not in view and "tools" not in view
        copy_of_view = copy.deepcopy(view)
        copy_of_view["messages"][0]["payload"]["text"] = "mutated"
        assert service.get_session(sid)["messages"][0]["payload"]["text"] == "<script>bad()</script>"


def test_assistant_branch_is_a_durable_control_operation_without_execution(database):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Make a branch from the reply", "source")
        service.wait_for_idle(source)
        source_view = service.get_session(source)
        assistant_id = source_view["messages"][1]["visible_message_id"]

        receipt = service.create_branch(
            source, assistant_id, idempotency_key="assistant-branch",
            expected_source_revision=source_view["revision"],
        )
        assert receipt["role"] == "assistant"
        assert receipt["active_workflow_session_id"] == source
        assert calls == ["A", "B"]
        child = receipt["workflow_session_id"]
        child_view = service.get_session(child)
        assert [message["role"] for message in child_view["messages"]] == ["user", "assistant"]
        assert child_view["can_submit"]
        assert all(node["status"] == "idle" for node in child_view["nodes"])
        assert service.create_branch(
            source, assistant_id, idempotency_key="assistant-branch",
            expected_source_revision=source_view["revision"],
        ) == receipt

    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
    anchors = bundle["fork_anchor"]
    assert len(anchors) == 1 and anchors[0]["role"] == "assistant"
    assert len(bundle.get("chain_run", [])) == 1
    assert not [row for row in bundle.get("chain_run", []) if row["workflow_session_id"] == child]


def test_user_branch_owns_independent_pending_input_and_can_continue_once(database):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Keep this input for the branch", "source")
        service.wait_for_idle(source)
        source_view = service.get_session(source)
        user_id = source_view["messages"][0]["visible_message_id"]

        receipt = service.create_and_switch_branch(
            source, user_id, idempotency_key="user-branch",
            expected_source_revision=source_view["revision"],
        )
        child = receipt["workflow_session_id"]
        assert receipt["role"] == "user"
        assert receipt["active_workflow_session_id"] == child
        child_view = service.get_session(child)
        assert child_view["messages"] == [{
            "visible_message_id": user_id, "role": "user", "payload": {"text": "Keep this input for the branch"},
            "sequence": 1, "chain_run_id": None,
        }]
        assert not child_view["can_submit"]
        pending_id = receipt["pending_input_id"]
        assert child_view["pending_input_id"] == pending_id
        assert child_view["available_actions"] == ["continue_pending_input"]
        continued = service.continue_pending_input(
            child, pending_id, idempotency_key="continue",
            expected_session_revision=child_view["revision"],
        )
        assert continued["visible_message_id"] == user_id
        service.wait_for_idle(child)
        completed = service.get_session(child)
        assert completed["error"] is None
        assert completed["can_submit"]
        assert completed["pending_input_id"] is None
        assert "continue_pending_input" not in completed["available_actions"]
        assert [message["role"] for message in completed["messages"]] == ["user", "assistant"]
        assert service.continue_pending_input(
            child, pending_id, idempotency_key="continue",
            expected_session_revision=child_view["revision"],
        ) == continued
        assert calls == ["A", "B", "A", "B"]

    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
    child_sessions = [row for row in bundle["workflow_session"] if row["source"]["kind"] == "fork"]
    assert len(child_sessions) == 1
    child = child_sessions[0]["workflow_session_id"]
    child_runs = [row for row in bundle["run_record"] if row["workflow_session_id"] == child]
    assert len(child_runs) == 2
    assert bundle["fork_anchor"][0]["pending_input"]["input_id"] == child_runs[0]["input_id"]


def test_same_user_can_create_multiple_independent_branches(database):
    with closing(WorkflowService(database)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Branch this input more than once", "source")
        service.wait_for_idle(source)
        user_id = service.get_session(source)["messages"][0]["visible_message_id"]

        first = service.create_branch(
            source, user_id, idempotency_key="first",
            expected_source_revision=service.get_session(source)["revision"],
        )
        second = service.create_branch(
            source, user_id, idempotency_key="second",
            expected_source_revision=service.get_session(source)["revision"],
        )
        assert first["workflow_session_id"] != second["workflow_session_id"]
        assert first["fork_anchor_id"] != second["fork_anchor_id"]
        assert first["pending_input_id"] != second["pending_input_id"]

    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
    assert len(bundle["fork_anchor"]) == 2
    assert len([row for row in bundle["workflow_session"] if row["source"]["kind"] == "fork"]) == 2


def test_branch_replay_requires_same_target_revision_and_operation_kind(database):
    with closing(WorkflowService(database)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Branch request", "source")
        service.wait_for_idle(source)
        view = service.get_session(source)
        user_id, assistant_id = (message["visible_message_id"] for message in view["messages"])
        revision = view["revision"]

        first = service.create_branch(
            source, user_id, idempotency_key="shared", expected_source_revision=revision,
        )
        assert service.create_branch(
            source, user_id, idempotency_key="shared", expected_source_revision=revision,
        ) == first
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            service.create_branch(
                source, assistant_id, idempotency_key="shared",
                expected_source_revision=revision,
            )
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            service.create_branch(
                source, user_id, idempotency_key="shared",
                expected_source_revision=revision + 1,
            )

        switched = service.create_and_switch_branch(
            source, assistant_id, idempotency_key="shared",
            expected_source_revision=service.get_session(source)["revision"],
        )
        assert switched["workflow_session_id"] != first["workflow_session_id"]
        assert switched["active_workflow_session_id"] == switched["workflow_session_id"]
        assert service.create_and_switch_branch(
            source, assistant_id, idempotency_key="shared",
            expected_source_revision=revision + 1,
        ) == switched

    with closing(WorkflowService(database)) as reopened:
        assert reopened.create_branch(
            source, user_id, idempotency_key="shared", expected_source_revision=revision,
        ) == first
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            reopened.create_branch(
                source, assistant_id, idempotency_key="shared",
                expected_source_revision=revision,
            )


def test_legacy_branch_receipt_without_request_identity_refuses_ambiguous_replay(database):
    with closing(WorkflowService(database)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Legacy fork receipt", "source")
        service.wait_for_idle(source)
        view = service.get_session(source)
        target = view["messages"][0]["visible_message_id"]
        revision = view["revision"]
        service.create_branch(
            source, target, idempotency_key="legacy", expected_source_revision=revision,
        )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE idempotency SET request_digest = NULL "
            "WHERE operation = 'workflow.fork' AND key = ?",
            (source + ":legacy",),
        )
        connection.commit()
    with closing(WorkflowService(database)) as reopened:
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            reopened.create_branch(
                source, target, idempotency_key="legacy",
                expected_source_revision=revision,
            )


def test_pending_start_replay_requires_same_input_and_expected_revision(database):
    with closing(WorkflowService(database)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Pending request", "source")
        service.wait_for_idle(source)
        source_view = service.get_session(source)
        branch = service.create_branch(
            source, source_view["messages"][0]["visible_message_id"],
            idempotency_key="branch", expected_source_revision=source_view["revision"],
        )
        child = branch["workflow_session_id"]
        pending = branch["pending_input_id"]
        revision = service.get_session(child)["revision"]
        first = service.continue_pending_input(
            child, pending, idempotency_key="start", expected_session_revision=revision,
        )
        service.wait_for_idle(child)
        assert service.continue_pending_input(
            child, pending, idempotency_key="start", expected_session_revision=revision,
        ) == first
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            service.continue_pending_input(
                child, source, idempotency_key="start", expected_session_revision=revision,
            )
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            service.continue_pending_input(
                child, pending, idempotency_key="start",
                expected_session_revision=revision + 1,
            )


def test_legacy_pending_receipt_replays_only_provable_original_request(database):
    with closing(WorkflowService(database)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Legacy pending start", "source")
        service.wait_for_idle(source)
        view = service.get_session(source)
        branch = service.create_branch(
            source, view["messages"][0]["visible_message_id"],
            idempotency_key="branch", expected_source_revision=view["revision"],
        )
        child, pending = branch["workflow_session_id"], branch["pending_input_id"]
        revision = service.get_session(child)["revision"]
        first = service.continue_pending_input(
            child, pending, idempotency_key="legacy", expected_session_revision=revision,
        )
        service.wait_for_idle(child)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE idempotency SET request_digest = NULL "
            "WHERE operation = 'workflow.pending' AND key = ?",
            (child + ":legacy",),
        )
        connection.commit()
    with closing(WorkflowService(database)) as reopened:
        assert reopened.continue_pending_input(
            child, pending, idempotency_key="legacy", expected_session_revision=revision,
        ) == first
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            reopened.continue_pending_input(
                child, source, idempotency_key="legacy", expected_session_revision=revision,
            )
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            reopened.continue_pending_input(
                child, pending, idempotency_key="legacy", expected_session_revision=revision + 1,
            )


def test_fork_rejects_revision_conflicts_active_runs_and_failed_boundaries(database):
    started, release = Event(), Event()

    class BlockingAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            started.set()
            assert release.wait(10)
            return super().generate(messages, tools)

    with closing(WorkflowService(database, model_factory=BlockingAdapter)) as service:
        source = service.create_session()["workflow_session_id"]
        receipt = service.submit(source, "Busy source", "busy")
        assert started.wait(10)
        view = service.get_session(source)
        with pytest.raises(ContractValidationError, match="active execution"):
            service.create_branch(
                source, receipt["visible_message_id"], idempotency_key="busy-branch",
                expected_source_revision=view["revision"],
            )
        with pytest.raises(ContractValidationError, match="revision conflict"):
            service.create_branch(
                source, receipt["visible_message_id"], idempotency_key="stale",
                expected_source_revision=view["revision"] - 1,
            )
        release.set()
        service.wait_for_idle(source)

    class FailedAdapter:
        def generate(self, messages, tools):
            request = httpx.Request("POST", "https://offline.invalid/chat/completions")
            raise httpx.HTTPStatusError(
                "provider failure", request=request,
                response=httpx.Response(400, request=request),
            )

    with closing(WorkflowService(database, model_factory=lambda _: FailedAdapter())) as service:
        failed = service.create_session()["workflow_session_id"]
        receipt = service.submit(failed, "Failed source", "failed")
        service.wait_for_idle(failed)
        view = service.get_session(failed)
        with pytest.raises(ContractValidationError, match="completed user"):
            service.create_branch(
                failed, receipt["visible_message_id"], idempotency_key="failed-branch",
                expected_source_revision=view["revision"],
            )


def test_fork_transaction_rolls_back_source_revision_and_child_records(database):
    armed = False

    def failure(point):
        if armed and point == "before_commit":
            raise RuntimeError("fork commit failed")

    with closing(WorkflowService(database, fault_injector=failure)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Transactional branch", "source")
        service.wait_for_idle(source)
        view = service.get_session(source)
        user_id = view["messages"][0]["visible_message_id"]
        armed = True
        with pytest.raises(RuntimeError, match="fork commit failed"):
            service.create_branch(
                source, user_id, idempotency_key="rollback",
                expected_source_revision=view["revision"],
            )
        armed = False
        assert service.get_session(source)["revision"] == view["revision"]
        assert len(service.list_sessions()) == 1

    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
    assert bundle.get("fork_anchor", []) == []
    assert len(bundle["workflow_session"]) == 1
