"""Targeted N05 in-process interrupt/resume tests for the real workflow service."""

import copy
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="workflow-interrupt-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def stored(database, kind, session_id):
    with closing(SqliteStore(database)) as store:
        if kind == "turn":
            run_ids = {
                row["run_id"] for row in store.list_records("run_record")
                if row["workflow_session_id"] == session_id
            }
            return [row for row in store.list_records("turn") if row["run_id"] in run_ids]
        return [
            row for row in store.list_records(kind)
            if row["workflow_session_id"] == session_id
        ]


def run_record(database, run_id):
    with closing(SqliteStore(database)) as store:
        return store.get_record("run_record", {"run_id": run_id})


def accepted_interrupt(service, session_id, run_id, session_revision, run_revision, release, key):
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(
            service.interrupt, session_id, run_id, idempotency_key=key,
            expected_session_revision=session_revision, expected_run_revision=run_revision,
        )
        try:
            return response.result(timeout=5)
        finally:
            if not response.done():
                release.set()


class StatelessOfflineAdapter:
    """Model fixture derives its next call from accepted messages, not adapter memory."""

    def __init__(self, stage, calls, *, block_first=None):
        self.stage = stage
        self.calls = calls
        self.block_first = block_first

    def generate(self, messages, tools):
        self.calls.append(self.stage)
        if self.block_first is not None and self.stage == "A":
            started, release = self.block_first
            self.block_first = None
            started.set()
            assert release.wait(20)
        user = next(message for message in reversed(messages) if message["role"] == "user")
        text = loads_strict(user["blocks"][0]["text"])["text"]
        has_inspected = any(message["role"] == "tool" for message in messages)
        if not has_inspected:
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "inspect_text", dumps_pretty({"text": text})),
            ))
        if self.stage == "A":
            answer = "[Offline draft]\n\n" + text.strip()
        else:
            answer = "[Offline revision]\n\n" + text.strip().removeprefix("[Offline draft]\n\n")
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall(str(uuid4()), "final_answer", dumps_pretty({"answer": {"text": answer}})),
        ))


def test_interrupt_prepared_queued_run_never_dispatches_model(database):
    blockers_ready, release_workers = Event(), Event()
    count_lock = Lock()
    blockers_started = 0
    model_calls = []

    def block_worker():
        nonlocal blockers_started
        with count_lock:
            blockers_started += 1
            if blockers_started == 4:
                blockers_ready.set()
        assert release_workers.wait(20)

    def factory(stage):
        model_calls.append(stage)
        return StatelessOfflineAdapter(stage, [])

    with closing(WorkflowService(database, model_factory=factory)) as service:
        blockers = [service._executor.submit(block_worker) for _ in range(4)]
        try:
            assert blockers_ready.wait(10)
            sid = service.create_session()["workflow_session_id"]
            submission = service.submit(sid, "Do not dispatch a queued run", "queued")
            prepared = service.get_session(sid)
            run_id = prepared["nodes"][0]["run_id"]
            assert prepared["nodes"][0]["status"] == "prepared"
            assert model_calls == []
            run_revision = run_record(database, run_id)["revision"]
            receipt = service.interrupt(
                sid, run_id, idempotency_key="interrupt-queued",
                expected_session_revision=prepared["revision"],
                expected_run_revision=run_revision,
            )
            assert receipt["status"] == "paused"
            assert service.interrupt(
                sid, run_id, idempotency_key="interrupt-queued",
                expected_session_revision=prepared["revision"],
                expected_run_revision=run_revision,
            ) == receipt
        finally:
            release_workers.set()
        for blocker in blockers:
            blocker.result(timeout=10)
        future = service._futures[sid]
        try:
            future.result(timeout=10)
        except CancelledError:
            pass
        paused = service.get_session(sid)
        assert paused["nodes"][0]["run_id"] == run_id
        assert paused["nodes"][0]["status"] == "paused"
        assert paused["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "paused",
        }]
        assert model_calls == []
        assert stored(database, "turn", sid) == []
        assert stored(database, "workflow_candidate", sid) == []


def test_interrupt_before_blocked_model_response_fences_late_result_and_reopen_refuses_resume(
    database,
):
    started, release = Event(), Event()
    model_calls = []
    first = True

    def factory(stage):
        nonlocal first
        block = (started, release) if stage == "A" and first else None
        if block is not None:
            first = False
        return StatelessOfflineAdapter(stage, model_calls, block_first=block)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submission = service.submit(sid, "Do not accept a late model response", "submit")
            assert started.wait(10)
            before = service.get_session(sid)
            assert before["available_actions"] == ["interrupt"]
            run_id = before["nodes"][0]["run_id"]
            run_revision = run_record(database, run_id)["revision"]
            receipt = accepted_interrupt(
                service, sid, run_id, before["revision"], run_revision, release, "interrupt-once",
            )
            assert receipt["status"] == "pausing"
        finally:
            release.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["nodes"][0]["status"] == "paused"
        assert paused["available_actions"] == ["resume", "reroll"]
        assert paused["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "paused",
        }]
        assert [row["role"] for row in paused["messages"]] == ["user"]
        assert stored(database, "turn", sid) == []
        assert stored(database, "workflow_candidate", sid) == []
        assert model_calls == ["A"]

        assert service.interrupt(
            sid, run_id, idempotency_key="interrupt-once",
            expected_session_revision=before["revision"],
            expected_run_revision=run_revision,
        ) == receipt
        with pytest.raises(ContractValidationError):
            service.interrupt(
                sid, run_id, idempotency_key="interrupt-once",
                expected_session_revision=before["revision"],
                expected_run_revision=run_revision + 1,
            )
        with pytest.raises(ContractValidationError):
            service.interrupt(
                sid, run_id, idempotency_key="stale-interrupt",
                expected_session_revision=before["revision"],
                expected_run_revision=run_revision,
            )
        with pytest.raises(ContractValidationError):
            service.submit(sid, "A separate input must wait", "another-submit")
        with pytest.raises(ContractValidationError):
            service.create_branch(
                sid, submission["visible_message_id"], idempotency_key="busy-fork",
                expected_source_revision=paused["revision"],
            )

    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"reopen dispatched {stage}"),
    )) as reopened:
        unavailable = reopened.get_session(sid)
        assert unavailable["available_actions"] == ["close_execution"]
        assert unavailable["nodes"][0]["status"] == "recovery_unavailable"
        assert unavailable["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "recovery_unavailable",
        }]
        with pytest.raises(ContractValidationError):
            reopened.resume(
                sid, run_id, idempotency_key="resume-after-restart",
                expected_session_revision=unavailable["revision"],
                expected_run_revision=run_record(database, run_id)["revision"],
            )
        assert model_calls == ["A"]


def test_interrupt_during_b_tool_settles_then_resumes_same_run_without_repeating_tools(
    database, monkeypatch,
):
    tool_started, release_tool = Event(), Event()
    model_calls = []
    tool_calls = []
    original_tools = WorkflowService._tools

    def tracked_tools():
        inspect, final = original_tools()
        original_inspect = inspect._implementation

        def counted_inspect(text):
            stage = "B" if text.startswith("[Offline draft]\n\n") else "A"
            tool_calls.append(stage)
            if stage == "B":
                tool_started.set()
                assert release_tool.wait(20)
            return original_inspect(text)

        return replace(inspect, _implementation=counted_inspect), final

    monkeypatch.setattr(WorkflowService, "_tools", staticmethod(tracked_tools))

    def factory(stage):
        return StatelessOfflineAdapter(stage, model_calls)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submission = service.submit(sid, "Count each completed tool once", "submit")
            assert tool_started.wait(10)
            before = service.get_session(sid)
            a_run = before["nodes"][0]["run_id"]
            b_run = before["nodes"][1]["run_id"]
            assert before["nodes"][0]["status"] == "succeeded"
            assert before["nodes"][1]["status"] == "running"
            run_revision = run_record(database, b_run)["revision"]
            interrupted = accepted_interrupt(
                service, sid, b_run, before["revision"], run_revision,
                release_tool, "interrupt-b",
            )
            assert interrupted["status"] == "pausing"
        finally:
            release_tool.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["nodes"][0]["run_id"] == a_run
        assert paused["nodes"][0]["status"] == "succeeded"
        assert paused["nodes"][1]["run_id"] == b_run
        assert paused["nodes"][1]["status"] == "paused"
        assert paused["available_actions"] == ["resume", "reroll"]
        assert paused["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "paused",
        }]
        assert tool_calls == ["A", "B"]
        assert len(stored(database, "turn", sid)) == 1
        assert stored(database, "workflow_candidate", sid) == []
        with pytest.raises(ContractValidationError):
            service.resume(
                sid, b_run, idempotency_key="stale-resume",
                expected_session_revision=before["revision"],
                expected_run_revision=run_revision,
            )

        resume_revision = paused["revision"]
        paused_run_revision = run_record(database, b_run)["revision"]
        resumed = service.resume(
            sid, b_run, idempotency_key="resume-b",
            expected_session_revision=resume_revision,
            expected_run_revision=paused_run_revision,
        )
        assert resumed["status"] == "running"
        assert service.resume(
            sid, b_run, idempotency_key="resume-b",
            expected_session_revision=resume_revision,
            expected_run_revision=paused_run_revision,
        ) == resumed
        with pytest.raises(ContractValidationError):
            service.resume(
                sid, b_run, idempotency_key="second-resume",
                expected_session_revision=resume_revision,
                expected_run_revision=paused_run_revision,
            )
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert [row["role"] for row in completed["messages"]] == ["user", "assistant"]
        assert completed["nodes"][0]["run_id"] == a_run
        assert completed["nodes"][1]["run_id"] == b_run
        assert completed["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "succeeded",
        }]
        assert len(stored(database, "run_record", sid)) == 2
        assert len(stored(database, "workflow_candidate", sid)) == 1
        assert tool_calls == ["A", "B"]
        assert model_calls == ["A", "A", "B", "B"]
        assert service.resume(
            sid, b_run, idempotency_key="resume-b",
            expected_session_revision=resume_revision,
            expected_run_revision=paused_run_revision,
        ) == resumed
        with pytest.raises(ContractValidationError):
            service.resume(
                sid, b_run, idempotency_key="resume-b",
                expected_session_revision=resume_revision,
                expected_run_revision=paused_run_revision + 1,
            )


def test_accepted_final_race_rejects_interrupt_before_archive(database, monkeypatch):
    final_ready, allow_archive = Event(), Event()
    model_calls = []
    original_archive = WorkflowService._archive_accepted_node

    def block_b_archive(service, package):
        if package["stage"] == "B":
            final_ready.set()
            assert allow_archive.wait(20)
        return original_archive(service, package)

    monkeypatch.setattr(WorkflowService, "_archive_accepted_node", block_b_archive)

    def factory(stage):
        return StatelessOfflineAdapter(stage, model_calls)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submission = service.submit(sid, "Final already accepted", "submit")
            assert final_ready.wait(10)
            view = service.get_session(sid)
            b_run = view["nodes"][1]["run_id"]
            assert run_record(database, b_run)["status"] == "final_ready"
            assert stored(database, "workflow_candidate", sid) == []
            with pytest.raises(ContractValidationError):
                service.interrupt(
                    sid, b_run, idempotency_key="too-late",
                    expected_session_revision=view["revision"],
                    expected_run_revision=run_record(database, b_run)["revision"],
                )
            assert [row["role"] for row in service.get_session(sid)["messages"]] == ["user"]
        finally:
            allow_archive.set()
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert completed["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "succeeded",
        }]
        assert [row["role"] for row in completed["messages"]] == ["user", "assistant"]
        assert model_calls == ["A", "A", "B", "B"]
        assert len(stored(database, "workflow_candidate", sid)) == 1


def test_interrupt_wins_before_final_acceptance_and_resume_does_not_requery_final(
    database, monkeypatch,
):
    final_waiting, release_final = Event(), Event()
    model_calls = []
    original_boundary = WorkflowService._run_boundary
    blocked_once = False

    def hold_before_final(service, run_id, generation, kind):
        nonlocal blocked_once
        if kind == "before_final" and not blocked_once:
            blocked_once = True
            final_waiting.set()
            assert release_final.wait(20)
        return original_boundary(service, run_id, generation, kind)

    monkeypatch.setattr(WorkflowService, "_run_boundary", hold_before_final)

    with closing(WorkflowService(
        database, model_factory=lambda stage: StatelessOfflineAdapter(stage, model_calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submission = service.submit(sid, "Pause just before final acceptance", "submit")
            assert final_waiting.wait(10)
            before = service.get_session(sid)
            run_id = before["nodes"][0]["run_id"]
            assert before["nodes"][0]["status"] == "running"
            assert model_calls == ["A", "A"]
            receipt = accepted_interrupt(
                service, sid, run_id, before["revision"],
                run_record(database, run_id)["revision"], release_final, "interrupt-final",
            )
            assert receipt["status"] == "pausing"
        finally:
            release_final.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["nodes"][0]["run_id"] == run_id
        assert paused["nodes"][0]["status"] == "paused"
        assert paused["available_actions"] == ["resume"]
        assert stored(database, "turn", sid) == []
        assert stored(database, "workflow_candidate", sid) == []
        checkpoint = service._run_checkpoints[run_id]
        assert len(checkpoint.pending_tools) == 1
        assert checkpoint.pending_tools[0].name == "final_answer"
        assert len(checkpoint.messages) == 3

        resumed = service.resume(
            sid, run_id, idempotency_key="resume-final",
            expected_session_revision=paused["revision"],
            expected_run_revision=run_record(database, run_id)["revision"],
        )
        assert resumed["status"] == "running"
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert completed["nodes"][0]["run_id"] == run_id
        assert completed["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "succeeded",
        }]
        assert model_calls == ["A", "A", "B", "B"]
        assert len(stored(database, "workflow_candidate", sid)) == 1


def test_finalizing_gate_rejects_interrupt_before_final_ready_is_persisted(
    database, monkeypatch,
):
    settling_final, release_final = Event(), Event()
    model_calls = []
    original_tool_event = WorkflowService._run_tool_event
    blocked_once = False

    def hold_final_settle(service, run_id, generation, kind, call_id, execution_id, outcome):
        nonlocal blocked_once
        if kind == "settle" and run_id in service._finalizing_runs and not blocked_once:
            blocked_once = True
            settling_final.set()
            assert release_final.wait(20)
        return original_tool_event(
            service, run_id, generation, kind, call_id, execution_id, outcome,
        )

    monkeypatch.setattr(WorkflowService, "_run_tool_event", hold_final_settle)
    with closing(WorkflowService(
        database, model_factory=lambda stage: StatelessOfflineAdapter(stage, model_calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submission = service.submit(sid, "Final acceptance owns this race", "submit")
            assert settling_final.wait(10)
            view = service.get_session(sid)
            run_id = view["nodes"][0]["run_id"]
            assert view["nodes"][0]["status"] == "running"
            assert view["available_actions"] == []
            with pytest.raises(ContractValidationError, match="interruptible"):
                service.interrupt(
                    sid, run_id, idempotency_key="too-late",
                    expected_session_revision=view["revision"],
                    expected_run_revision=run_record(database, run_id)["revision"],
                )
            assert stored(database, "turn", sid) == []
        finally:
            release_final.set()
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert completed["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "succeeded",
        }]
        assert model_calls == ["A", "A", "B", "B"]
        assert len(stored(database, "workflow_candidate", sid)) == 1


def test_exhausted_budget_with_pending_tool_refuses_resume_without_new_dispatch(
    database, monkeypatch,
):
    tool_waiting, release_tool = Event(), Event()
    model_calls, tool_calls = [], []
    original_register = WorkflowService._register_components
    original_boundary = WorkflowService._run_boundary
    original_tools = WorkflowService._tools
    blocked_once = False

    def one_request_for_a(service):
        original_register(service)
        context, kernel, adapter, config = service._resolved["A"]
        bounded = copy.deepcopy(config)
        bounded["payload"]["kernel"]["max_model_requests"] = 1
        service._resolved["A"] = (context, kernel, adapter, bounded)

    def hold_first_tool(service, run_id, generation, kind):
        nonlocal blocked_once
        if kind == "before_tool" and not blocked_once:
            blocked_once = True
            tool_waiting.set()
            assert release_tool.wait(20)
        return original_boundary(service, run_id, generation, kind)

    def tracked_tools():
        inspect, final = original_tools()
        return replace(
            inspect,
            _implementation=lambda text: tool_calls.append(text) or {"characters": len(text), "lines": 1},
        ), final

    monkeypatch.setattr(WorkflowService, "_register_components", one_request_for_a)
    monkeypatch.setattr(WorkflowService, "_run_boundary", hold_first_tool)
    monkeypatch.setattr(WorkflowService, "_tools", staticmethod(tracked_tools))

    with closing(WorkflowService(
        database, model_factory=lambda stage: StatelessOfflineAdapter(stage, model_calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            service.submit(sid, "Pending tool has no remaining model budget", "submit")
            assert tool_waiting.wait(10)
            before = service.get_session(sid)
            run_id = before["nodes"][0]["run_id"]
            assert accepted_interrupt(
                service, sid, run_id, before["revision"],
                run_record(database, run_id)["revision"], release_tool, "interrupt-pending",
            )["status"] == "pausing"
        finally:
            release_tool.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["nodes"][0]["status"] == "paused"
        assert paused["available_actions"] == ["extend_budget"]
        checkpoint = service._run_checkpoints[run_id]
        assert (checkpoint.model_requests, checkpoint.attempts) == (1, 1)
        assert [tool.name for tool in checkpoint.pending_tools] == ["inspect_text"]
        assert model_calls == ["A"]
        assert tool_calls == []
        with pytest.raises(ContractValidationError, match="budget"):
            service.resume(
                sid, run_id, idempotency_key="resume-without-budget",
                expected_session_revision=paused["revision"],
                expected_run_revision=run_record(database, run_id)["revision"],
            )
        assert service.get_session(sid)["nodes"][0]["status"] == "paused"
        assert service.get_session(sid)["revision"] == paused["revision"]
        assert model_calls == ["A"]
        assert tool_calls == []


def test_exhausted_budget_allows_pending_final_without_new_model_request(
    database, monkeypatch,
):
    final_waiting, release_final = Event(), Event()
    model_calls = []
    original_register = WorkflowService._register_components
    original_boundary = WorkflowService._run_boundary
    blocked_once = False

    def one_request_for_a(service):
        original_register(service)
        context, kernel, adapter, config = service._resolved["A"]
        bounded = copy.deepcopy(config)
        bounded["payload"]["kernel"]["max_model_requests"] = 1
        service._resolved["A"] = (context, kernel, adapter, bounded)

    def hold_before_final(service, run_id, generation, kind):
        nonlocal blocked_once
        if kind == "before_final" and not blocked_once:
            blocked_once = True
            final_waiting.set()
            assert release_final.wait(20)
        return original_boundary(service, run_id, generation, kind)

    class ImmediateFinalAdapter:
        def generate(self, messages, tools):
            model_calls.append("A")
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(
                    str(uuid4()), "final_answer",
                    dumps_pretty({"answer": {"text": "[Offline draft]\n\nOne request"}}),
                ),
            ))

    def factory(stage):
        return ImmediateFinalAdapter() if stage == "A" else StatelessOfflineAdapter(stage, model_calls)

    monkeypatch.setattr(WorkflowService, "_register_components", one_request_for_a)
    monkeypatch.setattr(WorkflowService, "_run_boundary", hold_before_final)
    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submission = service.submit(sid, "One request", "submit")
            assert final_waiting.wait(10)
            before = service.get_session(sid)
            run_id = before["nodes"][0]["run_id"]
            assert accepted_interrupt(
                service, sid, run_id, before["revision"],
                run_record(database, run_id)["revision"], release_final, "interrupt-final",
            )["status"] == "pausing"
        finally:
            release_final.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["available_actions"] == ["resume"]
        checkpoint = service._run_checkpoints[run_id]
        assert (checkpoint.model_requests, checkpoint.attempts) == (1, 1)
        assert [tool.name for tool in checkpoint.pending_tools] == ["final_answer"]

        assert service.resume(
            sid, run_id, idempotency_key="resume-final",
            expected_session_revision=paused["revision"],
            expected_run_revision=run_record(database, run_id)["revision"],
        )["status"] == "running"
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert completed["nodes"][0]["run_id"] == run_id
        assert completed["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "succeeded",
        }]
        assert model_calls == ["A", "B", "B"]
        assert len(stored(database, "workflow_candidate", sid)) == 1


def test_resume_transaction_failure_keeps_paused_run_and_zero_new_dispatch(database):
    started, release = Event(), Event()
    model_calls = []
    fault_armed = False
    first = True

    def failure(point):
        if fault_armed and point == "before_commit":
            raise RuntimeError("injected resume commit failure")

    def factory(stage):
        nonlocal first
        block = (started, release) if stage == "A" and first else None
        if block is not None:
            first = False
        return StatelessOfflineAdapter(stage, model_calls, block_first=block)

    with closing(WorkflowService(
        database, model_factory=factory, fault_injector=failure,
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submission = service.submit(sid, "Keep pause after rollback", "submit")
            assert started.wait(10)
            before = service.get_session(sid)
            run_id = before["nodes"][0]["run_id"]
            assert accepted_interrupt(
                service, sid, run_id, before["revision"],
                run_record(database, run_id)["revision"], release, "interrupt",
            )["status"] == "pausing"
        finally:
            release.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["nodes"][0]["status"] == "paused"
        paused_revision = run_record(database, run_id)["revision"]
        assert model_calls == ["A"]

        fault_armed = True
        try:
            with pytest.raises(RuntimeError, match="injected resume commit failure"):
                service.resume(
                    sid, run_id, idempotency_key="resume-after-fault",
                    expected_session_revision=paused["revision"],
                    expected_run_revision=paused_revision,
                )
        finally:
            fault_armed = False
        assert service.get_session(sid)["nodes"][0]["status"] == "paused"
        assert run_record(database, run_id)["revision"] == paused_revision
        assert service.get_session(sid)["revision"] == paused["revision"]
        assert model_calls == ["A"]
        assert stored(database, "workflow_candidate", sid) == []
        with closing(SqliteStore(database)) as store:
            assert store.read_receipt("workflow.resume", sid + ":resume-after-fault") is None

        assert service.resume(
            sid, run_id, idempotency_key="resume-after-fault",
            expected_session_revision=paused["revision"],
            expected_run_revision=paused_revision,
        )["status"] == "running"
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert completed["nodes"][0]["run_id"] == run_id
        assert completed["chains"] == [{
            "chain_run_id": submission["chain_run_id"], "status": "succeeded",
        }]
