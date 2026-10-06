"""Real workflow boundaries publish typed observations, never a second history."""

from __future__ import annotations

import copy
from contextlib import closing
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.bindings import ComponentRegistry, ComponentSelection, WorkflowComponents
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.event_contracts import validate_event_projection
from phase1_agent.event_registry import EventBehaviorRegistry
from phase1_agent.runtime import SnapshotKernel
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import (
    A_BINDING, B_BINDING, OUTPUT_BINDING, OfflineAdapter, WorkflowService,
)
from phase1_agent.run_events import SubscriptionResetRequired


KERNEL = "f9bc1755-e853-417b-8443-d884fd9e7001"
DEFINITION = "f9bc1755-e853-417b-8443-d884fd9e7002"
REF = {"schema_id": "chapter.check.progress", "version": 1}
EMPTY = {"type": "object", "additionalProperties": False}
CAPABILITIES = frozenset({
    "json_final", "paired_tools", "bounded_retry", "pause_resume", "reroll",
    "events", "stream_preview",
})


def uid():
    return str(uuid4())


def records(service):
    with closing(SqliteStore(service.database)) as store:
        return store.read_bundle()


def first_run_id(service, submission):
    chain = next(row for row in records(service)["chain_run"]
                 if row["chain_run_id"] == submission["chain_run_id"])
    return chain["node_run_ids"][0]


def observe_all(service, session_id, run_id, *, monitor=False):
    projection_ref = "workflow.monitor" if monitor else "workflow.public"
    handle = service.subscribe_run_events(session_id, run_id, projection_ref=projection_ref)
    snapshot = copy.deepcopy(handle.snapshot)
    cursor = copy.deepcopy(handle.cursor)
    handle.cancel()
    cursor["after_sequence"] = 0
    handle = service.subscribe_run_events(
        session_id, run_id, projection_ref=projection_ref, cursor=cursor,
    )
    events = []
    while batch := handle.poll():
        events.extend(batch.events)
    handle.cancel()
    return snapshot, events


def complete(service, text="private input sentinel"):
    session_id = service.create_session()["workflow_session_id"]
    receipt = service.submit(session_id, text, uid())
    service.wait_for_idle(session_id)
    assert service.get_session(session_id)["error"] is None
    return session_id, receipt


class ReportingKernel(SnapshotKernel):
    def __init__(self, *, defect=None):
        super().__init__()
        self.reporters = []
        self.defect = defect

    def run(self, *args, on_event, **kwargs):
        self.reporters.append(on_event)
        if self.defect == "core":
            on_event("run_state", {"schema_id": "run-event.run_state", "version": 1}, {})
        elif self.defect == "bad_payload":
            on_event("chapter.check.progress", REF, {"chapter": "private wrong type"})
        else:
            on_event("chapter.check.progress", REF, {"chapter": 1})
            on_event.preview("half-preview-not-history")
        return super().run(*args, **kwargs)


def reporting_components(kernel, events=None):
    registry = ComponentRegistry()
    registry.register(
        "kernel", KERNEL, "1", kernel, capabilities=CAPABILITIES, config_schema=EMPTY,
        event_refs=[{"event_type": "chapter.check.progress", "event_version": 1}],
    )
    selection = ComponentSelection(
        KERNEL, "1", {"owner_component_id": KERNEL, "schema_version": 1, "payload": {}},
    )
    producer = registry.resolve("kernel", KERNEL, "1", config=selection.config).descriptor
    events = events if events is not None else EventBehaviorRegistry()
    events.register(
        "chapter.check.progress", 1, payload_schema_ref=REF, producer=producer,
        payload_schema={
            "type": "object", "properties": {"chapter": {"type": "integer", "minimum": 1}},
            "required": ["chapter"], "additionalProperties": False,
        },
        required_capabilities=("events",),
    )
    return WorkflowComponents(
        registry, kernels={"A": selection, "B": selection},
        workflow_definition_id=DEFINITION, event_registry=events,
    )


def test_real_run_scopes_freeze_definition_and_separate_result_stages(tmp_path):
    service = WorkflowService(tmp_path / "events.sqlite")
    try:
        session_id, receipt = complete(service)
        bundle = records(service)
        agents = [run for run in bundle["run_record"] if run["chain_run_id"] == receipt["chain_run_id"]]
        assert {run["node_binding_id"] for run in agents} == {A_BINDING, B_BINDING}
        for run in agents:
            snapshot, events = observe_all(service, session_id, run["run_id"], monitor=True)
            assert snapshot["workflow_definition_revision"] == 1
            assert snapshot["chain_run_id"] == receipt["chain_run_id"]
            assert snapshot["node_binding_id"] == run["node_binding_id"]
            assert "s0" not in snapshot and "messages" not in snapshot
            types = [event["event_type"] for event in events]
            assert types.index("run_started") < types.index("final_ready")
            assert types.index("final_ready") < types.index("archive_result") < types.index("ports_released")
            assert "workflow_published" not in types
            assert events[0]["sequence"] == 1
            assert all(event["run_id"] == run["run_id"] for event in events)
            final = next(event for event in events if event["event_type"] == "final_ready")
            assert "output_id" not in final["payload"]
            ports = next(event for event in events if event["event_type"] == "ports_released")
            assert ports["payload"]["ports"] == ["final", "context_delta"]
        output = bundle["node_run"][0]
        snapshot, events = observe_all(service, session_id, output["run_id"], monitor=True)
        assert snapshot["node_binding_id"] == OUTPUT_BINDING
        assert [event["event_type"] for event in events] == ["workflow_published", "workflow_published"]
        assert [event["payload"]["status"] for event in events] == ["pending", "succeeded"]
        assert events[0]["payload"]["delivery_id"] == events[1]["payload"]["delivery_id"]
        assert events[1]["payload"]["visible_message_id"] is not None
        assert "budget" not in snapshot and "run_started" not in [event["event_type"] for event in events]
    finally:
        service.close()


def test_public_observation_is_allowlisted_and_does_not_dispatch(tmp_path):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    service = WorkflowService(tmp_path / "observe.sqlite", model_factory=factory)
    try:
        session_id, receipt = complete(service)
        before = list(calls)
        run_id = first_run_id(service, receipt)
        run = next(row for row in records(service)["run_record"] if row["run_id"] == run_id)
        snapshot, events = observe_all(service, session_id, run["run_id"])
        assert snapshot["action_rejections"]
        assert all(validate_event_projection(event) == event for event in events)
        assert not {"request_attempt", "tool_progress", "diagnostic"} & {event["event_type"] for event in events}
        assert all("private input sentinel" not in repr(event) for event in events)
        handle = service.subscribe_run_events(session_id, run["run_id"])
        handle.cancel()
        handle.cancel()
        service.get_session(session_id)
        assert calls == before
        other = service.create_session()["workflow_session_id"]
        with pytest.raises(ContractValidationError, match="another workflow session"):
            service.subscribe_run_events(other, run["run_id"])
        assert calls == before
    finally:
        service.close()


def test_declared_kernel_behavior_is_private_and_preview_never_enters_history(tmp_path):
    kernel = ReportingKernel()
    service = WorkflowService(tmp_path / "reporting.sqlite", components=reporting_components(kernel))
    try:
        session_id, receipt = complete(service)
        run_id = first_run_id(service, receipt)
        _, monitor = observe_all(service, session_id, run_id, monitor=True)
        _, public = observe_all(service, session_id, run_id)
        behavior = next(event for event in monitor if event["event_type"] == "chapter.check.progress")
        assert behavior["payload"] == {"chapter": 1}
        assert "chapter.check.progress" not in {event["event_type"] for event in public}
        previews = [event for event in monitor if event["event_type"].startswith("preview_")]
        assert [event["event_type"] for event in previews] == ["preview_delta", "preview_cleared"]
        assert previews[0]["payload"]["preview_id"] == previews[1]["payload"]["preview_id"]
        assert all("half-preview-not-history" not in repr(turn) for turn in records(service)["turn"])
        old = kernel.reporters[0]
        assert old("chapter.check.progress", REF, {"chapter": 2}) is None
        assert old.preview("late") is None
    finally:
        service.close()


@pytest.mark.parametrize("defect", ["core", "bad_payload"])
def test_invalid_producer_events_fail_loudly_without_model_or_success(tmp_path, defect):
    calls = []
    kernel = ReportingKernel(defect=defect)
    service = WorkflowService(
        tmp_path / (defect + ".sqlite"), components=reporting_components(kernel),
        model_factory=lambda stage: calls.append(stage) or OfflineAdapter(stage),
    )
    try:
        session_id = service.create_session()["workflow_session_id"]
        receipt = service.submit(session_id, "input", uid())
        service.wait_for_idle(session_id)
        assert service.get_session(session_id)["error"]["code"] == "STORAGE_OR_COORDINATION_FAILED"
        assert records(service).get("turn", []) == []
        assert records(service).get("workflow_candidate", []) == []
        assert calls == ["A"]
        _, events = observe_all(service, session_id, first_run_id(service, receipt), monitor=True)
        assert "chapter.check.progress" not in {event["event_type"] for event in events}
        assert not {"final_ready", "archive_result", "ports_released", "workflow_published"} & {
            event["event_type"] for event in events
        }
    finally:
        service.close()


def test_service_close_releases_observer_resources(tmp_path):
    service = WorkflowService(tmp_path / "close.sqlite")
    session_id, receipt = complete(service)
    handle = service.subscribe_run_events(session_id, first_run_id(service, receipt))
    sources = list(service._event_sources.values())
    service.close()
    assert all(source.closed and source.subscriber_count == 0 for source in sources)
    assert handle.cancelled
    assert not service._event_sources


@pytest.mark.parametrize("boundary", ["archive", "ports"])
def test_result_storage_retry_projects_original_stages_without_kernel_replay(tmp_path, monkeypatch, boundary):
    calls = []
    service = WorkflowService(
        tmp_path / (boundary + ".sqlite"),
        model_factory=lambda stage: calls.append(stage) or OfflineAdapter(stage),
    )
    original_archive = SqliteStore.archive_success
    original_save = SqliteStore._save_bundle_transaction

    def fail_archive(store, values, key):
        run = next(value for kind, value in values if kind == "run_record")
        if run["node_binding_id"] == B_BINDING:
            raise RuntimeError("private-secret-archive-error")
        return original_archive(store, values, key)

    def fail_ports(store, values, key, **kwargs):
        if key.startswith("output:"):
            run = store.get_record("run_record", {"run_id": key.removeprefix("output:")})
            if run["node_binding_id"] == B_BINDING:
                raise RuntimeError("private-secret-ports-error")
        return original_save(store, values, key, **kwargs)

    monkeypatch.setattr(SqliteStore, "archive_success" if boundary == "archive" else "_save_bundle_transaction",
                        fail_archive if boundary == "archive" else fail_ports)
    try:
        session_id = service.create_session()["workflow_session_id"]
        service.submit(session_id, "keep the original result", uid())
        service.wait_for_idle(session_id)
        view = service.get_session(session_id)
        assert view["available_actions"] == ["retry_archive"]
        run_id = next(node["run_id"] for node in view["nodes"] if node["node_binding_id"] == B_BINDING)
        _, before = observe_all(service, session_id, run_id, monitor=True)
        ready = next(event for event in before if event["event_type"] == "final_ready")
        failed = next(event for event in before
                      if event["event_type"] == ("archive_result" if boundary == "archive" else "ports_released")
                      and event["payload"]["status"] == "failed")
        assert "private-secret" not in repr(failed)
        if boundary == "ports":
            assert failed["payload"]["output_id"] is None
            assert failed["payload"]["ports"] == []
        assert "workflow_published" not in {event["event_type"] for event in before}
        expected_calls = list(calls)
        if boundary == "ports":
            service._active_runs.pop(run_id, None)
        monkeypatch.setattr(SqliteStore, "archive_success", original_archive)
        monkeypatch.setattr(SqliteStore, "_save_bundle_transaction", original_save)
        key = uid()
        result = service.retry_archive(
            session_id, run_id, idempotency_key=key, expected_session_revision=view["revision"],
        )
        assert result["status"] == "succeeded"
        assert calls == expected_calls
        _, after = observe_all(service, session_id, run_id, monitor=True)
        assert [event for event in after if event["event_type"] == "final_ready"] == [ready]
        archived = [event for event in after if event["event_type"] == "archive_result"
                    and event["payload"]["status"] == "succeeded"]
        released = [event for event in after if event["event_type"] == "ports_released"
                    and event["payload"]["status"] == "succeeded"]
        assert len(archived) == len(released) == 1
        assert archived[0]["payload"]["result_id"] == released[0]["payload"]["result_id"] == ready["payload"]["result_id"]
        assert service.retry_archive(
            session_id, run_id, idempotency_key=key, expected_session_revision=view["revision"],
        ) == result
        _, replayed = observe_all(service, session_id, run_id, monitor=True)
        assert replayed == after
        assert calls == expected_calls
    finally:
        service.close()


def test_delivery_failure_is_not_node_success_and_retry_preserves_delivery_identity(tmp_path, monkeypatch):
    calls = []
    original = WorkflowService._select_workflow_candidate

    def fail(*args, **kwargs):
        raise RuntimeError("private-secret-delivery-error")

    monkeypatch.setattr(WorkflowService, "_select_workflow_candidate", fail)
    service = WorkflowService(
        tmp_path / "delivery.sqlite",
        model_factory=lambda stage: calls.append(stage) or OfflineAdapter(stage),
    )
    try:
        session_id = service.create_session()["workflow_session_id"]
        service.submit(session_id, "publish once", uid())
        service.wait_for_idle(session_id)
        view = service.get_session(session_id)
        assert view["available_actions"] == ["retry_publish"]
        bundle = records(service)
        output = bundle["node_run"][0]
        _, before = observe_all(service, session_id, output["run_id"], monitor=True)
        assert [event["payload"]["status"] for event in before] == ["pending", "failed"]
        assert all(event["payload"]["visible_message_id"] is None for event in before)
        assert all("private-secret" not in repr(event) for event in before)
        delivery_id = before[0]["payload"]["delivery_id"]
        assert before[1]["payload"]["delivery_id"] == delivery_id
        expected_calls = list(calls)
        monkeypatch.setattr(WorkflowService, "_select_workflow_candidate", original)
        b_run = next(run for run in bundle["run_record"] if run["node_binding_id"] == B_BINDING)
        service.retry_publish(
            session_id, b_run["run_id"], idempotency_key=uid(), expected_session_revision=view["revision"],
        )
        _, after = observe_all(service, session_id, output["run_id"], monitor=True)
        assert [event["payload"]["status"] for event in after] == ["pending", "failed", "succeeded"]
        assert {event["payload"]["delivery_id"] for event in after} == {delivery_id}
        assert after[-1]["payload"]["visible_message_id"] is not None
        assert calls == expected_calls
    finally:
        service.close()


def test_historical_observation_sources_are_bounded_and_evicted_cursors_reset(tmp_path):
    service = WorkflowService(tmp_path / "retention.sqlite", event_source_limit=2)
    try:
        session_id = service.create_session()["workflow_session_id"]
        submissions = []
        for index in range(3):
            submission = service.submit(session_id, "history " + str(index), uid())
            service.wait_for_idle(session_id)
            assert service.get_session(session_id)["error"] is None
            submissions.append(submission)
        run_ids = [first_run_id(service, submission) for submission in submissions]
        handle = service.subscribe_run_events(session_id, run_ids[0])
        cursor = copy.deepcopy(handle.cursor)
        original_stream = cursor["stream_instance_id"]
        handle.cancel()
        for run_id in run_ids[1:]:
            handle = service.subscribe_run_events(session_id, run_id)
            assert service._event_sources[run_id].sealed
            handle.cancel()
            assert len(service._event_sources) <= 2
        assert run_ids[0] not in service._event_sources
        with pytest.raises(SubscriptionResetRequired):
            service.subscribe_run_events(session_id, run_ids[0], cursor=cursor)
        handle = service.subscribe_run_events(session_id, run_ids[0])
        assert handle.cursor["stream_instance_id"] != original_stream
        assert len(service._event_sources) <= 2
        handle.cancel()
    finally:
        service.close()
