"""Public workflow commands preserve native boundaries and durable identities."""

import asyncio
import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import dumps_pretty
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.storage import ACTIVE_SESSION_SELECTION_ID, SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService
from phase1_agent.workflow_operations import RECEIPT_OPERATION


@pytest.fixture
def tmp_path():
    with TemporaryDirectory(prefix="operation-dispatch-", dir=Path(__file__).parent) as folder:
        yield Path(folder)


def durable(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def envelope(service, sid, kind, target=None, *, key=None, payload=None):
    view = service.get_session(sid)
    with closing(SqliteStore(service.database)) as store:
        head = service._workflow_ref(store, sid)
    targets = {
        "submit": "workflow_session", "switch_session": "workflow_session",
        "initialize_session": "workflow_session", "continue_pending_input": "node_input",
        "create_branch": "visible_message", "create_and_switch_branch": "visible_message",
        "select_candidate": "candidate", "reroll": "chain_run",
        "close_execution": "chain_run", "continue_workflow": "chain_run",
    }
    target_kind = targets.get(kind, "run_record")
    value = {
        "schema_version": 1, "operation_id": str(uuid4()), "kind": kind,
        "scope": {"kind": "workflow_session", "id": sid},
        "target": {"kind": target_kind, "id": target or sid},
        "idempotency_key": key or str(uuid4()), "expected_revisions": [],
        "payload": payload or {},
    }
    if kind not in ("initialize_session", "switch_session", "select_candidate"):
        value["expected_revisions"].append({
            "kind": "workflow_session", "id": sid, "revision": view["revision"],
        })
    if kind in ("interrupt", "resume", "extend_budget"):
        run = next((row for row in view["nodes"] if row["run_id"] == target), {"revision": 1})
        value["expected_revisions"].append({
            "kind": "run_record", "id": target or sid, "revision": run["revision"],
        })
    if kind in (
        "reroll", "close_execution", "continue_workflow", "select_candidate",
        "create_branch", "create_and_switch_branch",
    ):
        value["expected_revisions"].append({
            "kind": "workflow_ref", "id": head["workflow_ref_id"],
            "revision": head["revision"],
        })
        value["expected_head_commit_id"] = head["head_commit_id"]
    if kind in ("switch_session", "create_and_switch_branch"):
        selection = service.get_active_session()
        value["expected_revisions"].append({
            "kind": "session_selection", "id": ACTIVE_SESSION_SELECTION_ID,
            "revision": selection["revision"],
        })
        if kind == "switch_session":
            value["scope"] = {"kind": "session_selection", "id": ACTIVE_SESSION_SELECTION_ID}
    return value


def test_submit_receipt_owns_real_chain_identity_and_replays_after_restart(tmp_path):
    database = tmp_path / "operations.sqlite"
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        request = envelope(service, sid, "submit", payload={"text": "Freeze the first input"})
        receipt = service.dispatch_operation(request)
        assert receipt["operation_id"] == request["operation_id"]
        service.wait_for_idle(sid)
        assert calls == ["A", "B"]
        before = durable(database)
        assert service.dispatch_operation(request) == receipt
        assert durable(database) == before
        chain = next(row for row in before["chain_run"]
                     if row["chain_run_id"] == receipt["result"]["chain_run_id"])
        assert chain["operation_id"] == request["operation_id"]
        with closing(SqliteStore(database)) as store:
            saved = store.read_receipt(
                RECEIPT_OPERATION, f"workflow_session:{sid}:{request['idempotency_key']}",
            )
            assert saved[-1]["dispatch_request"]["payload"] == request["payload"]
    with closing(WorkflowService(database, model_factory=lambda stage: pytest.fail("redispatch"))) as service:
        assert service.dispatch_operation(request) == receipt
        assert durable(database) == before


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(operation_id=str(uuid4())),
    lambda r: r.update(idempotency_key="different-key"),
    lambda r: r["payload"].update(text="different text"),
    lambda r: r["expected_revisions"][0].update(revision=99),
])
def test_public_operation_rejects_identity_rebinding_without_effects(tmp_path, mutation):
    database = tmp_path / "identity.sqlite"
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        request = envelope(service, sid, "submit", key="once", payload={"text": "original"})
        service.dispatch_operation(request)
        service.wait_for_idle(sid)
        changed = copy.deepcopy(request)
        mutation(changed)
        before = durable(database)
        with pytest.raises(ContractValidationError):
            service.dispatch_operation(changed)
        assert durable(database) == before


@pytest.mark.parametrize("kind", [
    "submit", "continue_pending_input", "retry_archive", "retry_publish", "interrupt",
    "resume", "extend_budget", "reroll", "close_execution", "continue_workflow",
    "select_candidate", "create_branch", "create_and_switch_branch", "switch_session",
])
def test_unknown_payload_fields_are_rejected_before_any_dispatch(tmp_path, kind):
    with closing(WorkflowService(tmp_path / "payload.sqlite")) as service:
        sid = service.create_session()["workflow_session_id"]
        request = envelope(service, sid, kind)
        request["payload"] = {"chain_run_id": str(uuid4())}
        before = durable(service.database)
        with pytest.raises(ContractValidationError) as error:
            service.dispatch_operation(request)
        assert error.value.status_code == 400
        assert durable(service.database) == before


def test_stale_revision_unsupported_and_legacy_collision_do_not_write(tmp_path):
    with closing(WorkflowService(tmp_path / "stale.sqlite")) as service:
        sid = service.create_session()["workflow_session_id"]
        stale = envelope(service, sid, "submit", payload={"text": "never run"})
        stale["expected_revisions"][0]["revision"] += 1
        initial = durable(service.database)
        with pytest.raises(ContractValidationError, match="revision"):
            service.dispatch_operation(stale)
        with pytest.raises(ContractValidationError, match="initialization"):
            service.dispatch_operation(envelope(service, sid, "initialize_session"))
        assert durable(service.database) == initial
        service.submit(sid, "legacy", "legacy-key")
        service.wait_for_idle(sid)
        before = durable(service.database)
        with pytest.raises(ContractValidationError, match="legacy"):
            service.dispatch_operation(envelope(
                service, sid, "submit", key="legacy-key", payload={"text": "legacy"},
            ))
        assert durable(service.database) == before


def test_steer_is_explicitly_unsupported_without_dispatch_records_or_events(tmp_path):
    calls = []
    with closing(WorkflowService(
        tmp_path / "unsupported-steer.sqlite",
        model_factory=lambda stage: calls.append(stage) or OfflineAdapter(stage),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        request = envelope(service, sid, "steer", payload={"text": "do not dispatch"})
        before = durable(service.database)
        with pytest.raises(ContractValidationError, match="not supported") as error:
            service.dispatch_operation(request)
        assert error.value.status_code == 400
        assert error.value.reason_code == "unsupported"
        assert durable(service.database) == before
        assert calls == []
        assert not service._event_sources
        assert not service._active_runs
        assert not service._futures


def test_atomic_fault_leaves_no_public_receipt_or_dispatch_and_is_retryable(tmp_path):
    database = tmp_path / "atomic.sqlite"
    calls = []
    fail = False

    def inject(point):
        if fail and point == "before_commit":
            raise RuntimeError("PRIVATE STORAGE FAULT")

    with closing(WorkflowService(
        database, model_factory=lambda stage: calls.append(stage) or OfflineAdapter(stage),
        fault_injector=inject,
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        request = envelope(service, sid, "submit", payload={"text": "atomic"})
        before = durable(database)
        fail = True
        with pytest.raises(RuntimeError):
            service.dispatch_operation(request)
        assert durable(database) == before
        with closing(SqliteStore(database)) as store:
            key = f"workflow_session:{sid}:{request['idempotency_key']}"
            assert store.read_receipt(RECEIPT_OPERATION, key) is None
        assert calls == []
        fail = False
        receipt = service.dispatch_operation(request)
        service.wait_for_idle(sid)
        assert service.dispatch_operation(request) == receipt
        assert calls == ["A", "B"]


@pytest.mark.parametrize("failure_point", ["after_first_write", "before_commit"])
def test_public_prepared_submit_atomically_binds_variables_operation_and_replay(
    tmp_path, failure_point,
):
    from test_workflow_variable_preparation import selected_components, variable_counts

    database = tmp_path / "prepared-operation.sqlite"
    calls = []
    failing = False

    def inject(point):
        if failing and point == failure_point:
            raise RuntimeError("PREPARED OPERATION STORAGE FAULT")

    with closing(WorkflowService(
        database, components=selected_components(),
        model_factory=lambda stage: calls.append(stage) or OfflineAdapter(stage),
        fault_injector=inject,
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        request = envelope(service, sid, "submit", payload={"text": "atomic variables"})
        receipt_key = f"workflow_session:{sid}:{request['idempotency_key']}"
        before = durable(database)
        before_variables = variable_counts(database)
        failing = True
        with pytest.raises(RuntimeError, match="PREPARED OPERATION"):
            service.dispatch_operation(request)
        assert durable(database) == before
        assert variable_counts(database) == before_variables
        with closing(SqliteStore(database)) as store:
            assert store.read_receipt(RECEIPT_OPERATION, receipt_key) is None
        assert calls == []

        failing = False
        receipt = service.dispatch_operation(request)
        service.wait_for_idle(sid)
        assert calls == ["A", "B"]
        saved = durable(database)
        chain = next(row for row in saved["chain_run"]
                     if row["chain_run_id"] == receipt["result"]["chain_run_id"])
        assert chain["status"] == "succeeded"
        assert chain["operation_id"] == request["operation_id"]
        assert all("variable_preparation" in row["config"]["payload"]
                   for row in saved["input_snapshot"])
        saved_variables = variable_counts(database)
        assert service.dispatch_operation(request) == receipt
        assert durable(database) == saved
        assert variable_counts(database) == saved_variables
        assert calls == ["A", "B"]

    with closing(WorkflowService(
        database, components=selected_components(),
        model_factory=lambda stage: pytest.fail("prepared operation redispatched"),
    )) as service:
        assert service.dispatch_operation(request) == receipt
        assert durable(database) == saved
        assert variable_counts(database) == saved_variables


def test_reply_lifecycle_reroll_select_fork_and_pending_continuation(tmp_path):
    database = tmp_path / "lifecycle.sqlite"
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "Keep every candidate", "initial")
        service.wait_for_idle(sid)
        initial = service.get_session(sid)
        original_candidate = initial["messages"][-1]["reply_candidates"][0]["candidate_id"]
        reroll = envelope(service, sid, "reroll", submitted["chain_run_id"])
        receipt = service.dispatch_operation(reroll)
        service.wait_for_idle(sid)
        assert service.dispatch_operation(reroll) == receipt
        view = service.get_session(sid)
        assert len(view["messages"][-1]["reply_candidates"]) == 2
        selected = envelope(service, sid, "select_candidate", original_candidate)
        selection = service.dispatch_operation(selected)
        assert selection["result"]["candidate_id"] == original_candidate
        assert service.dispatch_operation(selected) == selection
        assistant_id = service.get_session(sid)["messages"][-1]["visible_message_id"]
        branch = envelope(
            service, sid, "create_and_switch_branch", assistant_id,
            payload={"candidate_id": original_candidate},
        )
        forked = service.dispatch_operation(branch)
        child = forked["result"]["workflow_session_id"]
        assert service.get_active_session()["active_workflow_session_id"] == child
        assert service.dispatch_operation(branch) == forked
        operations = durable(database)["workflow_operation"]
        public = next(row for row in operations if row["operation_id"] == branch["operation_id"])
        subordinate = next(
            row for row in operations if row["kind"] == "create_branch"
            and row["payload"].get("child_workflow_session_id") == child
        )
        assert public["operation_id"] != subordinate["operation_id"]
        assert len(service.get_session(child)["messages"][-1]["reply_candidates"]) == 2
        user_branch = envelope(
            service, sid, "create_branch", submitted["visible_message_id"],
        )
        user_fork = service.dispatch_operation(user_branch)
        pending = user_fork["result"]
        request = envelope(
            service, pending["workflow_session_id"], "continue_pending_input",
            pending["pending_input_id"],
        )
        started = service.dispatch_operation(request)
        service.wait_for_idle(pending["workflow_session_id"])
        assert service.dispatch_operation(request) == started
        switch = envelope(service, sid, "switch_session")
        switched = service.dispatch_operation(switch)
        assert switched["result"]["active_workflow_session_id"] == sid
        assert service.dispatch_operation(switch) == switched


def test_foreign_ref_and_head_for_branch_fail_without_forking(tmp_path):
    with closing(WorkflowService(tmp_path / "foreign.sqlite")) as service:
        sid = service.create_session()["workflow_session_id"]
        other = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "stable", "input")
        service.wait_for_idle(sid)
        request = envelope(service, sid, "create_branch", submitted["visible_message_id"])
        other_ref = next(
            row for row in durable(service.database)["workflow_ref"]
            if row["workflow_session_id"] == other
        )
        before = durable(service.database)
        for changed in (
            {**request, "expected_head_commit_id": str(uuid4())},
            {key: value for key, value in request.items() if key != "expected_head_commit_id"},
            {**request, "expected_revisions": [
                {**row, "id": other_ref["workflow_ref_id"]}
                if row["kind"] == "workflow_ref" else row
                for row in request["expected_revisions"]
            ]},
        ):
            with pytest.raises(ContractValidationError):
                service.dispatch_operation(changed)
            assert durable(service.database) == before


def test_extend_and_resume_have_one_public_identity_and_do_not_duplicate_budget(tmp_path):
    calls = []
    recovering = False

    class Model:
        def generate(self, messages, tools):
            calls.append(messages)
            if recovering:
                return ModelResponse("tool_calls", tool_calls=(
                    ModelToolCall(str(uuid4()), "final_answer",
                                  dumps_pretty({"answer": {"text": "finished"}})),
                ))
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "inspect_text", dumps_pretty({"text": "once"})),
            ))

    with closing(WorkflowService(tmp_path / "budget.sqlite", model_factory=lambda stage: Model())) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Keep limits separate", "submit")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        run_id = failed["nodes"][0]["run_id"]
        request = envelope(service, sid, "extend_budget", run_id, payload={
            "additional_model_requests": 1, "additional_model_attempts": 0,
        })
        extended = service.dispatch_operation(request)
        assert extended["result"]["budget"] == {"max_model_requests": 9, "max_model_attempts": 32}
        assert service.dispatch_operation(request) == extended
        assert len(calls) == 8
        recovering = True
        resume = envelope(service, sid, "resume", run_id)
        receipt = service.dispatch_operation(resume)
        service.wait_for_idle(sid)
        assert service.dispatch_operation(resume) == receipt
        assert len(calls) == 10
        assert service.get_session(sid)["nodes"][0]["budget"]["max_model_requests"] == 9


def test_interrupt_then_resume_keeps_same_run(tmp_path):
    started, release = Event(), Event()

    class Blocked(OfflineAdapter):
        def generate(self, messages, tools):
            if self.stage == "A" and self.requests == 0:
                started.set()
                assert release.wait(5)
            return super().generate(messages, tools)

    with closing(WorkflowService(tmp_path / "pause.sqlite", model_factory=Blocked)) as service:
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "pause safely", "input")
        assert started.wait(5)
        run_id = service.get_session(sid)["nodes"][0]["run_id"]
        pause = envelope(service, sid, "interrupt", run_id)
        receipt = service.dispatch_operation(pause)
        release.set()
        service.wait_for_idle(sid)
        assert service.dispatch_operation(pause) == receipt
        request = envelope(service, sid, "resume", run_id)
        resumed = service.dispatch_operation(request)
        service.wait_for_idle(sid)
        assert service.dispatch_operation(request) == resumed
        view = service.get_session(sid)
        assert view["nodes"][0]["run_id"] == run_id
        assert view["chains"][0]["chain_run_id"] == submitted["chain_run_id"]


def test_close_and_manual_new_execution_use_public_id_without_duplicate_user(tmp_path):
    broken = True

    class Interrupted:
        def generate(self, messages, tools):
            raise asyncio.CancelledError("PRIVATE HOST FAILURE")

    with closing(WorkflowService(
        tmp_path / "close.sqlite",
        model_factory=lambda stage: Interrupted() if broken else OfflineAdapter(stage),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        old = service.submit(sid, "continue from facts", "input")
        service.wait_for_idle(sid)
        request = envelope(service, sid, "close_execution", old["chain_run_id"])
        closed = service.dispatch_operation(request)
        assert service.dispatch_operation(request) == closed
        broken = False
        continuation = envelope(service, sid, "continue_workflow", old["chain_run_id"])
        fresh = service.dispatch_operation(continuation)
        service.wait_for_idle(sid)
        assert service.dispatch_operation(continuation) == fresh
        assert fresh["result"]["chain_run_id"] != old["chain_run_id"]
        assert [row["role"] for row in service.get_session(sid)["messages"]] == ["user", "assistant"]


@pytest.mark.parametrize("kind", ["retry_archive", "retry_publish"])
def test_retry_acceptance_is_durable_before_unconfirmed_retry_and_never_replayed(
    tmp_path, monkeypatch, kind,
):
    database = tmp_path / "retry.sqlite"
    original_save = SqliteStore.save_bundle
    original_archive = SqliteStore.archive_success
    failed_once = False

    def fail_boundary(store, *args, **kwargs):
        nonlocal failed_once
        native = kwargs.get("operation")
        should_fail = (
            kind == "retry_publish" and native == "workflow.select_candidate"
            or kind == "retry_archive" and native is None
        )
        if should_fail and not failed_once:
            failed_once = True
            raise RuntimeError("PRIVATE FIRST BOUNDARY FAILURE")
        return (
            original_archive(store, *args, **kwargs) if native is None
            else original_save(store, *args, **kwargs)
        )

    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        with monkeypatch.context() as patch:
            patch.setattr(SqliteStore, "save_bundle", fail_boundary)
            if kind == "retry_archive":
                patch.setattr(SqliteStore, "archive_success", fail_boundary)
            submitted = service.submit(sid, "retry only accepted work", "input")
            service.wait_for_idle(sid)
        assert failed_once
        view = service.get_session(sid)
        run_id = view["nodes"][0 if kind == "retry_archive" else 1]["run_id"]
        request = envelope(service, sid, kind, run_id)
        calls = []

        def failed_retry(*args, **kwargs):
            calls.append(True)
            raise RuntimeError("PRIVATE RETRY FAILURE")

        with monkeypatch.context() as patch:
            patch.setattr(
                service, "_archive_accepted_node" if kind == "retry_archive" else "_publish",
                failed_retry,
            )
            with pytest.raises(RuntimeError, match="RETRY"):
                service.dispatch_operation(request)
        accepted = service.dispatch_operation(request)
        assert accepted["status"] == "accepted"
        assert accepted["result"]["completion"] == "unconfirmed"
        assert calls == [True]
        assert service.dispatch_operation(request) == accepted
        assert submitted["chain_run_id"] == accepted["result"]["chain_run_id"]
    with closing(WorkflowService(database, model_factory=lambda stage: pytest.fail("retry replayed"))) as service:
        assert service.dispatch_operation(request) == accepted
