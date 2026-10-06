"""Execution ownership and immutable event versions across observation lifetimes."""

from __future__ import annotations

from concurrent.futures import Future
from contextlib import closing
from threading import Event

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.event_registry import UnsupportedEventDeclaration
from phase1_agent.runtime import KernelPauseRequested, SnapshotKernel
from phase1_agent.storage import SqliteStore
from phase1_agent.tools import final_answer_tool, register_callable
from phase1_agent.workflow import A_BINDING, B_BINDING, OfflineAdapter, WorkflowService

from test_workflow_events import (
    KERNEL, REF, ReportingKernel, complete, first_run_id,
    observe_all, records, reporting_components, uid,
)
from test_workflow_model_recovery import HistoryAdapter, control_args, set_budget


def test_parent_turn_and_reroll_source_turn_are_distinct_actual_references(tmp_path):
    service = WorkflowService(tmp_path / "source-turn.sqlite")
    try:
        session_id, first = complete(service, "first")
        second = service.submit(session_id, "second", uid())
        service.wait_for_idle(session_id)
        saved = records(service)
        first_a = next(run for run in saved["run_record"]
                       if run["chain_run_id"] == first["chain_run_id"] and run["node_binding_id"] == A_BINDING)
        second_a = next(run for run in saved["run_record"]
                        if run["chain_run_id"] == second["chain_run_id"] and run["node_binding_id"] == A_BINDING)
        _, events = observe_all(service, session_id, second_a["run_id"], monitor=True)
        started = next(event["payload"] for event in events if event["event_type"] == "run_started")
        assert started["parent_turn_id"] == first_a["result_turn_id"]
        assert started["source_turn_id"] is None
        assert started["source_run_id"] is None
        view = service.get_session(session_id)
        replacement = service.reroll(
            session_id, second["chain_run_id"], idempotency_key=uid(),
            expected_session_revision=view["revision"], expected_ref_revision=view["ref_revision"],
            expected_head_commit_id=view["head_commit_id"],
        )
        service.wait_for_idle(session_id)
        assert service.get_session(session_id)["error"] is None
        _, rerolled = observe_all(service, session_id, replacement["run_id"], monitor=True)
        started = next(event["payload"] for event in rerolled if event["event_type"] == "run_started")
        assert started["parent_turn_id"] == first_a["result_turn_id"]
        assert started["source_turn_id"] == second_a["result_turn_id"]
        assert started["source_run_id"] == second_a["run_id"]
        assert started["source_chain_run_id"] == second["chain_run_id"]
        assert all(turn in records(service)["turn"] for turn in saved["turn"])
    finally:
        service.close()


def test_prepared_pause_first_resume_emits_one_real_start_before_dispatch(tmp_path, monkeypatch):
    calls = []
    service = WorkflowService(
        tmp_path / "prepared-pause.sqlite",
        model_factory=lambda stage: calls.append(stage) or OfflineAdapter(stage),
    )
    original_submit = service._executor.submit
    scheduled = Future()
    monkeypatch.setattr(service._executor, "submit", lambda *args, **kwargs: scheduled)
    try:
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "start only after manual resume", uid())
        run_id = first_run_id(service, submission)
        view = service.get_session(session_id)
        node = next(node for node in view["nodes"] if node["run_id"] == run_id)
        paused = service.interrupt(
            session_id, run_id, idempotency_key=uid(),
            expected_session_revision=view["revision"], expected_run_revision=node["revision"],
        )
        assert paused["status"] == "paused" and calls == []
        scheduled.set_result(False)
        _, events = observe_all(service, session_id, run_id, monitor=True)
        assert "run_started" not in {event["event_type"] for event in events}
        assert "command_result" not in {event["event_type"] for event in events}
        operation = next(event for event in events if event["event_type"] == "workflow_operation_result")
        assert operation["payload"]["operation_kind"] == "interrupt"
        state = next(event for event in events if event["event_type"] == "run_state")
        assert operation["sequence"] < state["sequence"]
        monkeypatch.setattr(service._executor, "submit", original_submit)
        view = service.get_session(session_id)
        node = next(node for node in view["nodes"] if node["run_id"] == run_id)
        service.resume(
            session_id, run_id, idempotency_key=uid(),
            expected_session_revision=view["revision"], expected_run_revision=node["revision"],
        )
        service.wait_for_idle(session_id)
        assert service.get_session(session_id)["error"] is None
        _, events = observe_all(service, session_id, run_id, monitor=True)
        starts = [event for event in events if event["event_type"] == "run_started"]
        assert len(starts) == 1
        assert starts[0]["sequence"] < next(event["sequence"] for event in events if event["event_type"] == "request_attempt")
        assert calls == ["A", "B"]
    finally:
        service.close()


class BlockingAdapter(OfflineAdapter):
    def __init__(self, stage, entered, release):
        super().__init__(stage)
        self.entered, self.release = entered, release

    def generate(self, messages, tools):
        if self.stage == "A" and self.requests == 0:
            self.entered.set()
            assert self.release.wait(15)
        return super().generate(messages, tools)


def test_same_run_resume_keeps_frozen_schema_and_new_b_uses_frozen_plan(tmp_path):
    entered, release = Event(), Event()
    kernel = ReportingKernel()
    components = reporting_components(kernel)
    producer = components.registry.resolve("kernel", KERNEL, "1", config=components.kernels["A"].config).descriptor
    service = WorkflowService(
        tmp_path / "versions.sqlite", components=components,
        model_factory=lambda stage: BlockingAdapter(stage, entered, release),
    )
    try:
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "fixed event schema", uid())
        run_id = first_run_id(service, submission)
        assert entered.wait(15)
        with closing(SqliteStore(service.database)) as store:
            original_run = store.get_record("run_record", {"run_id": run_id})
            original_snapshot = store.get_record("input_snapshot", {"snapshot_id": original_run["snapshot_id"]})
        view = service.get_session(session_id)
        node = next(node for node in view["nodes"] if node["run_id"] == run_id)
        service.interrupt(
            session_id, run_id, idempotency_key=uid(),
            expected_session_revision=view["revision"], expected_run_revision=node["revision"],
        )
        release.set()
        service.wait_for_idle(session_id)
        assert service.get_session(session_id)["nodes"][0]["status"] == "paused"
        components.event_registry.register(
            "chapter.check.progress", 2, producer=producer,
            payload_schema_ref={"schema_id": REF["schema_id"], "version": 2},
            payload_schema={
                "type": "object", "properties": {"chapter": {"type": "string"}},
                "required": ["chapter"], "additionalProperties": False,
            },
        )
        view = service.get_session(session_id)
        node = next(node for node in view["nodes"] if node["run_id"] == run_id)
        service.resume(
            session_id, run_id, idempotency_key=uid(),
            expected_session_revision=view["revision"], expected_run_revision=node["revision"],
        )
        service.wait_for_idle(session_id)
        assert service.get_session(session_id)["error"] is None
        saved = records(service)
        a = next(run for run in saved["run_record"] if run["run_id"] == run_id)
        b = next(run for run in saved["run_record"] if run["node_binding_id"] == B_BINDING)
        frozen_a = next(snapshot for snapshot in saved["input_snapshot"] if snapshot["snapshot_id"] == a["snapshot_id"])
        frozen_b = next(snapshot for snapshot in saved["input_snapshot"] if snapshot["snapshot_id"] == b["snapshot_id"])
        assert canonical_bytes(frozen_a) == canonical_bytes(original_snapshot)
        assert frozen_b["config"]["payload"]["run_events"]["behaviors"][0]["event_version"] == 1
        for run in (a, b):
            _, events = observe_all(service, session_id, run["run_id"], monitor=True)
            custom = [event for event in events if event["event_type"] == "chapter.check.progress"]
            assert custom and all(event["payload_schema_ref"] == REF for event in custom)
        _, a_events = observe_all(service, session_id, a["run_id"], monitor=True)
        assert sum(event["event_type"] == "run_started" for event in a_events) == 1
    finally:
        release.set()
        service.close()


def test_budget_change_is_typed_and_duplicate_extension_dispatches_nothing(tmp_path):
    calls = []
    service = WorkflowService(
        tmp_path / "budget-events.sqlite",
        model_factory=lambda stage: HistoryAdapter(stage, calls),
    )
    try:
        set_budget(service, "A", 1, 4)
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "manual budget decision", uid())
        service.wait_for_idle(session_id)
        run_id = first_run_id(service, submission)
        assert service.get_session(session_id)["available_actions"] == ["extend_budget", "close_execution"]
        arguments = {
            **control_args(service, session_id, run_id, uid()),
            "additional_model_requests": 1,
        }
        result = service.extend_budget(session_id, run_id, **arguments)
        _, events = observe_all(service, session_id, run_id, monitor=True)
        changed = [event for event in events if event["event_type"] == "budget_changed"]
        assert len(changed) == 1
        assert changed[0]["payload"] == {
            "budget_kind": "model_requests", "additional": 1, "limit": 2,
            "used": 1, "reason_code": "manual_extension",
        }
        operation = next(event for event in events if event["event_type"] == "workflow_operation_result")
        assert operation["payload"]["operation_kind"] == "extend_budget"
        assert "command_id" not in operation["payload"]
        assert len(calls) == 1
        assert service.extend_budget(session_id, run_id, **arguments) == result
        _, replayed = observe_all(service, session_id, run_id, monitor=True)
        assert replayed == events
        assert len(calls) == 1
        service.resume(session_id, run_id, **control_args(service, session_id, run_id, uid()))
        service.wait_for_idle(session_id)
        assert service.get_session(session_id)["error"] is None
        _, completed = observe_all(service, session_id, run_id, monitor=True)
        assert sum(event["event_type"] == "run_started" for event in completed) == 1
        assert sum(event["event_type"] == "budget_changed" for event in completed) == 1
    finally:
        service.close()


def test_unavailable_exact_version_does_not_use_new_version_for_new_b(tmp_path):
    entered, release = Event(), Event()
    kernel = ReportingKernel()
    components = reporting_components(kernel)
    calls = []

    def factory(stage):
        calls.append(stage)
        return BlockingAdapter(stage, entered, release)

    service = WorkflowService(tmp_path / "missing-version.sqlite", components=components, model_factory=factory)
    try:
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "old declared version", uid())
        run_id = first_run_id(service, submission)
        assert entered.wait(15)
        service.interrupt(session_id, run_id, **control_args(service, session_id, run_id, uid()))
        release.set()
        service.wait_for_idle(session_id)
        components.event_registry.unregister("chapter.check.progress", 1)
        service.resume(session_id, run_id, **control_args(service, session_id, run_id, uid()))
        service.wait_for_idle(session_id)
        failed = service._execution_failures[session_id]
        assert isinstance(failed, UnsupportedEventDeclaration)
        assert failed.reason_code == "unsupported"
        assert calls == ["A", "A"]
        saved = records(service)
        a = next(run for run in saved["run_record"] if run["run_id"] == run_id)
        b = next(run for run in saved["run_record"] if run["node_binding_id"] == B_BINDING)
        assert a["status"] == "succeeded"
        assert len(saved["turn"]) == 1
        _, events = observe_all(service, session_id, b["run_id"], monitor=True)
        assert not {"run_started", "request_attempt", "final_ready", "workflow_published"} & {
            event["event_type"] for event in events
        }
    finally:
        release.set()
        service.close()


class RetainedFaultKernel(SnapshotKernel):
    def __init__(self, *, failure=None, preview=False):
        super().__init__()
        self.callbacks = None
        self.generation = None
        self.failure = failure if failure is not None else RuntimeError("private-secret-program-error")
        self.preview = preview

    def run(self, *args, on_event, **kwargs):
        self.callbacks = {key: value for key, value in kwargs.items() if key.startswith("on_")}
        self.callbacks["on_event"] = on_event
        self.generation = on_event("chapter.check.progress", REF, {"chapter": 1})["generation"]
        if self.preview:
            on_event.preview("unaccepted preview")
        raise self.failure


def assert_failed_callbacks_revoked(service, kernel, run_id):
    assert service._active_runs[run_id].snapshot().generation != kernel.generation
    source = service._event_sources[run_id]
    before = source.sequence
    saved = records(service)
    assert kernel.callbacks["on_event"]("chapter.check.progress", REF, {"chapter": 2}) is None
    assert kernel.callbacks["on_event"].preview("late") is None
    assert kernel.callbacks["on_boundary"]("before_request") is False
    with pytest.raises(KernelPauseRequested):
        kernel.callbacks["on_tool_event"]("queue", uid(), None, None)
    with pytest.raises(KernelPauseRequested):
        kernel.callbacks["on_progress"]({"model_requests": 1, "attempts": 1, "accepted_messages": 0})
    with pytest.raises(ContractValidationError, match="Stale"):
        kernel.callbacks["on_fact"]({"kind": "model_request", "payload": {}})
    assert source.sequence == before
    assert records(service) == saved
    assert saved.get("turn", []) == []
    assert saved["run_record"][0]["status"] == "running"


@pytest.mark.parametrize("host_failure", [False, True])
def test_diagnostic_base_exception_cannot_bypass_failed_callback_revocation(
    tmp_path, monkeypatch, host_failure,
):
    primary = KeyboardInterrupt("original host signal") if host_failure else RuntimeError("original program fault")
    secondary = SystemExit("diagnostic interrupted")
    kernel = RetainedFaultKernel(failure=primary)
    service = WorkflowService(tmp_path / "diagnostic-interrupted.sqlite", components=reporting_components(kernel))

    def failed_diagnosis(*args):
        raise secondary

    monkeypatch.setattr(service, "_persist_execution_failure", failed_diagnosis)
    try:
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "retain the primary error", uid())
        expected = primary if host_failure else secondary
        with pytest.raises(type(expected)) as raised:
            service.wait_for_idle(session_id)
        assert raised.value is expected
        assert service._execution_failures[session_id] is primary
        assert service.get_session(session_id)["error"]["code"] == (
            "HOST_INTERRUPTED" if host_failure else "STORAGE_OR_COORDINATION_FAILED"
        )
        assert_failed_callbacks_revoked(service, kernel, first_run_id(service, submission))
    finally:
        service.close()


def test_preview_projection_failure_happens_after_callback_generation_revocation(tmp_path, monkeypatch):
    primary = RuntimeError("original program fault")
    secondary = ContractValidationError("preview projection failed")
    kernel = RetainedFaultKernel(failure=primary, preview=True)
    service = WorkflowService(tmp_path / "preview-clear-fault.sqlite", components=reporting_components(kernel))
    original_publish = service._publish_event

    def failed_projection(run_id, event_type, payload, **kwargs):
        if event_type == "preview_cleared":
            raise secondary
        return original_publish(run_id, event_type, payload, **kwargs)

    monkeypatch.setattr(service, "_publish_event", failed_projection)
    try:
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "reject the old preview owner", uid())
        with pytest.raises(ContractValidationError) as raised:
            service.wait_for_idle(session_id)
        assert raised.value is secondary
        assert service._execution_failures[session_id] is primary
        assert service.get_session(session_id)["error"]["code"] == "STORAGE_OR_COORDINATION_FAILED"
        run_id = first_run_id(service, submission)
        assert run_id not in service._event_previews
        assert_failed_callbacks_revoked(service, kernel, run_id)
    finally:
        service.close()


def test_program_failure_revokes_old_reports_without_fabricating_failed_history(tmp_path):
    kernel = RetainedFaultKernel()
    service = WorkflowService(tmp_path / "revoke.sqlite", components=reporting_components(kernel))
    try:
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "retain known facts", uid())
        service.wait_for_idle(session_id)
        run_id = first_run_id(service, submission)
        generation = service._active_runs[run_id].snapshot().generation
        assert generation != kernel.generation
        source = service._event_sources[run_id]
        before = source.sequence
        assert kernel.callbacks["on_event"]("chapter.check.progress", REF, {"chapter": 2}) is None
        assert kernel.callbacks["on_event"].preview("late") is None
        assert not kernel.callbacks["on_boundary"]("before_request")
        assert source.sequence == before
        assert records(service).get("turn", []) == []
        assert records(service)["run_record"][0]["status"] == "running"
    finally:
        service.close()


def test_old_callbacks_after_close_cannot_touch_new_owner_records_or_rebuild_sources(tmp_path, monkeypatch):
    path = tmp_path / "old-owner.sqlite"
    kernel = RetainedFaultKernel()
    old = WorkflowService(path, components=reporting_components(kernel))
    session_id = old.create_session()["workflow_session_id"]
    submission = old.submit(session_id, "old owner", uid())
    old.wait_for_idle(session_id)
    run_id = first_run_id(old, submission)
    old.close()
    assert not old._active_runs and not old._event_sources
    current = WorkflowService(path, components=reporting_components(ReportingKernel()))
    try:
        before = records(current)

        def forbidden_store():
            raise AssertionError("closed callback must not open storage")

        monkeypatch.setattr(old, "_store", forbidden_store)
        assert kernel.callbacks["on_event"]("chapter.check.progress", REF, {"chapter": 2}) is None
        assert kernel.callbacks["on_event"].preview("late") is None
        assert kernel.callbacks["on_boundary"]("before_request") is False
        with pytest.raises(KernelPauseRequested):
            kernel.callbacks["on_tool_event"]("queue", uid(), None, None)
        with pytest.raises(KernelPauseRequested):
            kernel.callbacks["on_progress"]({"model_requests": 1, "attempts": 1, "accepted_messages": 0})
        with pytest.raises(ContractValidationError, match="closed"):
            kernel.callbacks["on_fact"]({"kind": "model_request", "payload": {}})
        with pytest.raises(ContractValidationError, match="closed"):
            old._publish_event(run_id, "diagnostic", {"code": "LATE", "category": "unknown"})
        assert records(current) == before
        assert not old._event_sources and not current._event_sources
    finally:
        current.close()


def test_inflight_tool_settles_before_paused_state_after_control_acceptance(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    calls = []

    def inspect_text(text):
        entered.set()
        assert release.wait(15)
        return {"characters": len(text)}

    tool = register_callable("inspect_text", "Inspect text.", {
        "type": "object", "properties": {"text": {"type": "string", "description": "Text"}},
        "required": ["text"], "additionalProperties": False,
    }, inspect_text)
    monkeypatch.setattr(WorkflowService, "_tools", staticmethod(lambda: (tool, final_answer_tool())))
    service = WorkflowService(
        tmp_path / "settle-order.sqlite",
        model_factory=lambda stage: HistoryAdapter(stage, calls),
    )
    try:
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "settle the accepted tool", uid())
        run_id = first_run_id(service, submission)
        assert entered.wait(15)
        receipt = service.interrupt(session_id, run_id, **control_args(service, session_id, run_id, uid()))
        assert receipt["status"] == "pausing"
        release.set()
        service.wait_for_idle(session_id)
        _, events = observe_all(service, session_id, run_id, monitor=True)
        accepted = next(event for event in events if event["event_type"] == "workflow_operation_result")
        pausing = next(event for event in events if event["event_type"] == "run_state"
                       and event["payload"]["status"] == "pausing")
        settled = next(event for event in events if event["event_type"] == "tool_progress"
                       and event["payload"]["status"] == "settled")
        paused = next(event for event in events if event["event_type"] == "run_state"
                      and event["payload"]["status"] == "paused")
        assert accepted["sequence"] < pausing["sequence"] < settled["sequence"] < paused["sequence"]
        assert len(calls) == 1
        assert not records(service).get("turn", [])
    finally:
        release.set()
        service.close()


def test_safe_b_paused_reroll_closes_old_source_before_new_start_and_retains_a(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    kernel = ReportingKernel()
    block_b = True
    trace, seen = [], set()

    class PreviewBlockingB(OfflineAdapter):
        def generate(self, messages, tools):
            nonlocal block_b
            if self.stage == "B" and block_b:
                block_b = False
                kernel.reporters[-1].preview("unaccepted B preview")
                entered.set()
                assert release.wait(15)
            return super().generate(messages, tools)

    service = WorkflowService(
        tmp_path / "b-reroll-events.sqlite", components=reporting_components(kernel),
        model_factory=PreviewBlockingB,
    )
    original_publish = service._publish_event

    def record_publish(*args, **kwargs):
        event = original_publish(*args, **kwargs)
        if event is not None and event["event_id"] not in seen:
            seen.add(event["event_id"])
            trace.append(event)
        return event

    monkeypatch.setattr(service, "_publish_event", record_publish)
    try:
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "keep old A", uid())
        assert entered.wait(15)
        view = service.get_session(session_id)
        old_b = view["nodes"][1]["run_id"]
        old_reporter = kernel.reporters[-1]
        service.interrupt(session_id, old_b, **control_args(service, session_id, old_b, uid()))
        release.set()
        service.wait_for_idle(session_id)
        before = records(service)
        old_a = next(run for run in before["run_record"] if run["node_binding_id"] == A_BINDING)
        view = service.get_session(session_id)
        replacement = service.reroll(
            session_id, submission["chain_run_id"], idempotency_key=uid(),
            expected_session_revision=view["revision"], expected_ref_revision=view["ref_revision"],
            expected_head_commit_id=view["head_commit_id"],
        )
        service.wait_for_idle(session_id)
        assert service.get_session(session_id)["error"] is None
        cleared = next(index for index, event in enumerate(trace)
                       if event["run_id"] == old_b and event["event_type"] == "preview_cleared"
                       and event["payload"]["reason_code"] == "pause_requested")
        closed = next(index for index, event in enumerate(trace)
                      if event["run_id"] == old_b and event["event_type"] == "run_state"
                      and event["payload"]["status"] == "closed")
        new_start = next(index for index, event in enumerate(trace)
                         if event["run_id"] == replacement["run_id"] and event["event_type"] == "run_started")
        assert cleared < closed < new_start
        after = records(service)
        assert next(run for run in after["run_record"] if run["run_id"] == old_a["run_id"]) == old_a
        assert all(turn in after["turn"] for turn in before["turn"])
        assert service._event_sources[old_b].sealed
        sequence = service._event_sources[old_b].sequence
        assert old_reporter("chapter.check.progress", REF, {"chapter": 2}) is None
        assert old_reporter.preview("late old B") is None
        assert service._event_sources[old_b].sequence == sequence
    finally:
        release.set()
        service.close()


def test_active_event_dedup_is_bounded_but_first_start_and_receipts_remain_stable(tmp_path):
    entered, release = Event(), Event()
    calls = []
    service = WorkflowService(
        tmp_path / "dedup.sqlite",
        model_factory=lambda stage: calls.append(stage) or BlockingAdapter(stage, entered, release),
    )
    try:
        session_id = service.create_session()["workflow_session_id"]
        submission = service.submit(session_id, "bounded observations", uid())
        run_id = first_run_id(service, submission)
        assert entered.wait(15)
        service._event_dedup_limit = 4
        with service._lock:
            started = service._event_once[(run_id, "run_started")]
            for index in range(20):
                service._publish_event(
                    run_id, "diagnostic", {"code": "TEST_OBSERVATION", "category": "unknown"},
                    once=("state:" if index % 2 else "command:") + str(index),
                )
            assert len(service._event_ephemeral_order[run_id]) == 4
            assert sum(key[0] == run_id for key in service._event_once) <= 5
            assert service._event_once[(run_id, "run_started")] == started
        arguments = control_args(service, session_id, run_id, uid())
        accepted = service.interrupt(session_id, run_id, **arguments)
        sequence = service._event_sources[run_id].sequence
        expected_calls = list(calls)
        assert service.interrupt(session_id, run_id, **arguments) == accepted
        assert service._event_sources[run_id].sequence == sequence
        assert calls == expected_calls
        release.set()
        service.wait_for_idle(session_id)
        service.resume(session_id, run_id, **control_args(service, session_id, run_id, uid()))
        service.wait_for_idle(session_id)
        assert service.get_session(session_id)["error"] is None
        assert service._event_once[(run_id, "run_started")] == started
        assert len(service._event_ephemeral_order[run_id]) <= 4
    finally:
        release.set()
        service.close()
