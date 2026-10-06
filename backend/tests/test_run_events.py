from copy import deepcopy
import gc
import json
import threading
from uuid import uuid4
import weakref

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.event_contracts import core_payload_schema_ref, validate_event_projection
from phase1_agent.event_registry import EventBehaviorRegistry
from phase1_agent.run_events import (
    CallbackWorkerBudget, EventProjection, RunEventSource, SubscriptionResetRequired,
)


def uid():
    return str(uuid4())


def producer(kind="transform"):
    return {
        "kind": kind, "component_id": uid(), "component_version": "1",
        "contract_version": 1, "capabilities": ["events"],
    }


def frozen(*, behaviors=False, kind="transform"):
    registry = EventBehaviorRegistry()
    descriptor = producer(kind)
    refs = []
    if behaviors:
        registry.register(
            "chapter.check.progress", 1,
            payload_schema_ref={"schema_id": "chapter-progress", "version": 1},
            payload_schema={
                "type": "object",
                "required": ["chapter", "progress", "private_notes"],
                "properties": {
                    "chapter": {"type": "integer", "minimum": 1},
                    "progress": {"type": "integer", "minimum": 0, "maximum": 100},
                    "private_notes": {"type": "string"},
                },
                "additionalProperties": False,
            },
            producer=descriptor,
        )
        refs = [{"event_type": "chapter.check.progress", "event_version": 1}]
    return registry.freeze(producer=descriptor, event_refs=refs), registry, descriptor


def source(**kwargs):
    declarations, _, _ = frozen()
    return RunEventSource(uid(), uid(), uid(), generation=uid(),
                          declarations=declarations, **kwargs)


def publish(bus, event_type="diagnostic", payload=None, **kwargs):
    if payload is None:
        payload = {"code": "model_failure", "category": "model"}
    return bus.publish(
        event_type, payload, payload_schema_ref=core_payload_schema_ref(event_type),
        generation=bus.generation, **kwargs,
    )


def public(**kwargs):
    return EventProjection(
        kwargs.pop("projection_ref", "workflow"),
        kwargs.pop("projection_revision", "v1"),
        allowlist=kwargs.pop("allowlist", {"diagnostic": ("code",)}),
        snapshot_fields=kwargs.pop("snapshot_fields", ("status",)),
        **kwargs,
    )


def test_source_sequences_are_per_run_and_envelopes_are_v2():
    first = source()
    second = source()
    one = publish(first)
    two = publish(first)
    other = publish(second)
    assert [one["sequence"], two["sequence"], other["sequence"]] == [1, 2, 1]
    assert first.stream_instance_id != second.stream_instance_id
    assert first.scope_ref != second.scope_ref
    assert one["generation"] == first.generation
    assert one["visibility"] == "private"
    assert one["schema_version"] == 2
    validate_record("run_event", one)


def test_source_default_private_and_registered_behavior_does_not_become_public():
    declarations, _, _ = frozen(behaviors=True)
    bus = RunEventSource(uid(), uid(), uid(), generation=uid(), declarations=declarations)
    observer = bus.subscribe(public(allowlist={"chapter.check.progress": ("progress",)}))
    event = bus.report(
        "chapter.check.progress", {"chapter": 1, "progress": 20, "private_notes": "secret"},
        payload_schema_ref={"schema_id": "chapter-progress", "version": 1},
        generation=bus.generation,
    )
    batch = observer.poll()
    assert event["sequence"] == 1
    assert batch.events == []
    assert batch.cursor["after_sequence"] == 1
    assert "secret" not in json.dumps(batch.to_json())
    assert "chapter.check.progress" not in json.dumps(batch.to_json())
    observer.cancel()


def test_public_projection_whitelists_types_fields_and_snapshot_without_mutation():
    bus = source()
    original_snapshot = {"status": "running", "messages": ["private tool result"]}
    bus.update_snapshot(original_snapshot)
    rules = {"diagnostic": ["code"]}
    projection = public(allowlist=rules)
    rules["diagnostic"].append("category")
    observer = bus.subscribe(projection)
    assert observer.snapshot == {"status": "running"}
    assert observer.snapshot_cursor == observer.cursor
    event = publish(bus, visibility="business_candidate")
    batch = observer.poll()
    assert batch.events[0]["payload"] == {"code": "model_failure"}
    assert batch.events[0]["event_id"] == event["event_id"]
    assert batch.events[0]["source_payload_schema_ref"] == event["payload_schema_ref"]
    assert "payload_schema_ref" not in batch.events[0]
    validate_event_projection(batch.events[0])
    observer.snapshot["status"] = "hacked"
    batch.events[0]["payload"]["code"] = "hacked"
    assert bus.redeliver(event)["payload"]["code"] == "model_failure"
    assert bus.subscribe(projection).snapshot["status"] == "running"
    assert original_snapshot["status"] == "running"
    bus.close()


def test_visible_sequence_jumps_and_empty_batches_advance_source_scan_cursor():
    bus = source()
    observer = bus.subscribe(public())
    publish(bus)
    publish(bus, visibility="business_candidate")
    publish(bus)
    publish(bus, visibility="business_candidate")
    batches = [observer.poll() for _ in range(3)]
    assert [batch.cursor["after_sequence"] for batch in batches] == [1, 3, 4]
    assert [event["sequence"] for batch in batches for event in batch.events] == [2, 4]
    assert all(batch.status == "events" for batch in batches)
    assert not observer.needs_reset
    observer.cancel()


def test_private_monitor_requires_explicit_private_projection():
    bus = source()
    monitor = bus.subscribe(EventProjection.private())
    event = publish(bus)
    assert monitor.poll().events == [event]
    monitor.cancel()
    with pytest.raises(ContractValidationError, match="explicit event allowlist"):
        EventProjection("public", "1")


def test_snapshot_comes_from_authoritative_state_not_retained_history():
    state = {"status": "running", "stable_turn_ids": ["already-saved"], "private": "secret"}
    bus = source(snapshot_provider=lambda: deepcopy(state), buffer_limit=1)
    publish(bus)
    publish(bus)
    state["status"] = "paused"
    observer = bus.subscribe(public(snapshot_fields=("status", "stable_turn_ids")))
    assert observer.snapshot == {"status": "paused", "stable_turn_ids": ["already-saved"]}
    assert observer.cursor["after_sequence"] == 2
    assert observer.poll() is None
    assert bus.retained_event_count == 1
    observer.cancel()


def test_snapshot_live_handoff_uses_same_serial_gate_as_owner_state():
    lock = threading.RLock()
    snapshot_entered = threading.Event()
    snapshot_release = threading.Event()
    publication_done = threading.Event()
    state = {"status": "prepared"}
    results = {}

    def get_snapshot():
        snapshot_entered.set()
        assert snapshot_release.wait(2)
        return deepcopy(state)

    bus = source(lock=lock, snapshot_provider=get_snapshot)

    def subscribe():
        results["handle"] = bus.subscribe(public())

    def change_state():
        with lock:
            state["status"] = "running"
            publish(bus, visibility="business_candidate")
        publication_done.set()

    subscription_thread = threading.Thread(target=subscribe)
    publication_thread = threading.Thread(target=change_state)
    try:
        subscription_thread.start()
        assert snapshot_entered.wait(2)
        publication_thread.start()
        assert not publication_done.wait(0.02)
        snapshot_release.set()
        subscription_thread.join(2)
        publication_thread.join(2)
        assert not subscription_thread.is_alive()
        assert not publication_thread.is_alive()
        handle = results["handle"]
        assert handle.snapshot == {"status": "prepared"}
        assert handle.snapshot_cursor["after_sequence"] == 0
        assert handle.poll().events[0]["sequence"] == 1
    finally:
        snapshot_release.set()
        subscription_thread.join(2)
        if publication_thread.ident is not None:
            publication_thread.join(2)
        bus.close()


def test_retained_resume_replays_source_interval_then_continues_live():
    bus = source()
    projection = public()
    first = bus.subscribe(projection)
    cursor = deepcopy(first.cursor)
    first.cancel()
    publish(bus)
    original = publish(bus, visibility="business_candidate")
    second = bus.subscribe(projection, cursor=cursor)
    assert second.cursor["after_sequence"] == 0
    assert second.snapshot_cursor["after_sequence"] == 2
    replay = second.poll()
    assert replay.events == [projection.project_event(original)]
    assert replay.cursor["after_sequence"] == 2
    publish(bus)
    assert second.poll().cursor["after_sequence"] == 3
    second.cancel()


def test_retained_resume_of_filtered_interval_returns_empty_batch_with_watermark():
    bus = source()
    projection = public()
    observer = bus.subscribe(projection)
    cursor = deepcopy(observer.cursor)
    observer.cancel()
    publish(bus)
    publish(bus)
    resumed = bus.subscribe(projection, cursor=cursor)
    batch = resumed.poll()
    assert batch.events == []
    assert batch.cursor["after_sequence"] == 2
    resumed.cancel()


@pytest.mark.parametrize("change,reason", [
    (lambda cursor: cursor.update(stream_instance_id=uid()), "stream_changed"),
    (lambda cursor: cursor["scope_ref"].update(run_id=uid()), "scope_changed"),
    (lambda cursor: cursor["scope_ref"].update(node_binding_id=uid()), "scope_changed"),
    (lambda cursor: cursor["scope_ref"].update(workflow_session_id=uid()), "scope_changed"),
    (lambda cursor: cursor.update(projection_revision="old"), "projection_changed"),
    (lambda cursor: cursor.update(after_sequence=-1), "invalid_scan_watermark"),
    (lambda cursor: cursor.update(after_sequence=True), "invalid_scan_watermark"),
    (lambda cursor: cursor.update(after_sequence=100), "invalid_scan_watermark"),
    (lambda cursor: cursor.update(extra="secret"), "invalid_cursor"),
])
def test_wrong_cursor_requires_explicit_snapshot_reset(change, reason):
    bus = source()
    projection = public()
    observer = bus.subscribe(projection)
    cursor = deepcopy(observer.cursor)
    observer.cancel()
    change(cursor)
    with pytest.raises(SubscriptionResetRequired) as error:
        bus.subscribe(projection, cursor=cursor)
    assert error.value.reason == reason
    assert bus.subscriber_count == 0


def test_new_source_same_run_rejects_old_stream_cursor():
    first = source()
    observer = first.subscribe(public())
    cursor = deepcopy(observer.cursor)
    observer.cancel()
    declarations, _, _ = frozen()
    second = RunEventSource(
        first.scope_ref["workflow_session_id"], first.scope_ref["node_binding_id"],
        first.scope_ref["run_id"], generation=first.generation, declarations=declarations,
    )
    with pytest.raises(SubscriptionResetRequired, match="stream_changed"):
        second.subscribe(public(), cursor=cursor)


def test_lost_source_buffer_is_gap_but_exact_retained_boundary_is_valid():
    bus = source(buffer_limit=2)
    projection = public()
    observer = bus.subscribe(projection)
    old_cursor = deepcopy(observer.cursor)
    observer.cancel()
    for _ in range(3):
        publish(bus)
    with pytest.raises(SubscriptionResetRequired, match="source_buffer_lost"):
        bus.subscribe(projection, cursor=old_cursor)
    old_cursor["after_sequence"] = 1
    valid = bus.subscribe(projection, cursor=old_cursor)
    assert valid.poll().cursor["after_sequence"] == 3
    assert not valid.needs_reset
    valid.cancel()


def test_projection_revision_binds_reference_rules_visibility_and_snapshot_fields():
    bus = source()
    initial = public()
    observer = bus.subscribe(initial)
    cursor = deepcopy(observer.cursor)
    observer.cancel()
    changes = [
        public(projection_ref="another"),
        public(projection_revision="v2"),
        public(allowlist={"diagnostic": ("category",)}),
        public(snapshot_fields=("messages",)),
        public(include_private=True),
    ]
    for changed in changes:
        with pytest.raises(SubscriptionResetRequired, match="projection_changed"):
            bus.subscribe(changed, cursor=cursor)
    with pytest.raises(SubscriptionResetRequired, match="projection_changed"):
        bus.subscribe(changes[2])
    private_observer = bus.subscribe(EventProjection.private(projection_revision="v1"))
    assert private_observer.cursor["projection_revision"] != cursor["projection_revision"]
    private_observer.cancel()


def test_same_event_redelivery_is_idempotent_and_conflict_does_not_advance_source():
    bus = source()
    observer = bus.subscribe(EventProjection.private())
    event = publish(bus)
    assert observer.poll().events == [event]
    assert bus.redeliver(deepcopy(event)) == event
    assert publish(bus, event_id=event["event_id"]) == event
    assert bus.sequence == 1
    assert observer.poll() is None
    for field, value in [
        ("payload", {"code": "different", "category": "model"}),
        ("sequence", 2), ("run_id", uid()), ("generation", uid()),
        ("visibility", "business_candidate"), ("event_id", uid()),
    ]:
        conflicting = deepcopy(event)
        conflicting[field] = value
        with pytest.raises(ContractValidationError):
            bus.redeliver(conflicting)
    with pytest.raises(ContractValidationError, match="Conflicting event redelivery"):
        publish(bus, payload={"code": "different", "category": "model"},
                event_id=event["event_id"])
    assert bus.sequence == 1
    observer.cancel()


def test_evicted_identity_is_not_reinvented_or_retained_unboundedly():
    bus = source(buffer_limit=1)
    event = publish(bus)
    publish(bus)
    with pytest.raises(ContractValidationError, match="not retained"):
        bus.redeliver(event)
    with pytest.raises(ContractValidationError, match="not retained"):
        publish(bus, event_id=event["event_id"])
    assert bus.retained_event_count == 1
    assert bus.sequence == 2


def test_queue_overflow_is_explicit_gap_only_for_slow_subscriber():
    bus = source()
    slow = bus.subscribe(public(), queue_limit=1)
    fast = bus.subscribe(public(), queue_limit=1)
    publish(bus, visibility="business_candidate")
    assert fast.poll().cursor["after_sequence"] == 1
    publish(bus, visibility="business_candidate")
    assert fast.poll().cursor["after_sequence"] == 2
    gap = slow.poll()
    assert gap.status == "gap"
    assert gap.reason == "queue_overflow"
    assert gap.events == []
    assert gap.cursor["after_sequence"] == 2
    assert slow.needs_reset
    assert not fast.needs_reset
    assert bus.subscriber_count == 1
    publish(bus)
    assert slow.poll() is None
    assert fast.poll().cursor["after_sequence"] == 3
    fresh = bus.subscribe(public())
    assert fresh.cursor["after_sequence"] == 3
    bus.close()


def test_nonpreview_semantics_are_never_silently_merged():
    bus = source()
    observer = bus.subscribe(EventProjection.private(), queue_limit=2)
    call_id = uid()
    execution_id = uid()
    for outcome in ("pending", "settled"):
        publish(bus, "tool_progress", {
            "tool_call_id": call_id,
            "tool_execution_id": None if outcome == "pending" else execution_id,
            "model_order": 1, "status": outcome,
            "outcome": None if outcome == "pending" else "success",
        })
    assert [observer.poll().events[0]["payload"]["status"] for _ in range(2)] == [
        "pending", "settled",
    ]
    observer.cancel()


def test_repeated_cancel_clears_queue_removes_source_ref_and_never_controls_run():
    bus = source()
    observer = bus.subscribe(public())
    publish(bus, visibility="business_candidate")
    observer.cancel()
    observer.cancel()
    assert observer.cancelled
    assert observer.poll() is None
    assert bus.subscriber_count == 0
    assert publish(bus)["sequence"] == 2
    assert not bus.sealed


def test_cancel_wakes_a_blocked_pull_observer():
    bus = source()
    observer = bus.subscribe(public())
    started = threading.Event()
    done = threading.Event()
    result = []

    def wait_for_batch():
        started.set()
        result.append(observer.poll(None))
        done.set()

    worker = threading.Thread(target=wait_for_batch)
    try:
        worker.start()
        assert started.wait(2)
        observer.cancel()
        assert done.wait(2)
        worker.join(2)
        assert result == [None]
    finally:
        observer.cancel()
        worker.join(2)


def test_subscriber_callback_failure_is_isolated_and_sanitized():
    bus = source()
    entered = threading.Event()
    error_seen = threading.Event()

    def failing(batch):
        entered.set()
        raise RuntimeError("authorization=private-api-key and raw supplier failure")

    failed = bus.subscribe(public(), callback=failing)
    healthy = bus.subscribe(public(), callback=lambda batch: error_seen.set())
    try:
        publish(bus, visibility="business_candidate")
        assert entered.wait(2)
        assert error_seen.wait(2)
        assert failed.join(2)
        assert failed.error == {"code": "subscriber_error", "reason": "callback_failed"}
        assert "private-api-key" not in json.dumps(failed.error)
        assert bus.sequence == 1
        assert bus.subscriber_count == 1
        assert publish(bus, visibility="business_candidate")["sequence"] == 2
    finally:
        failed.cancel()
        healthy.cancel()
        assert failed.join(2)
        assert healthy.join(2)
        bus.close()
    assert bus.callback_worker_count == 0


def test_slow_callback_does_not_block_publisher_or_other_subscribers_and_cancel_stops_queue():
    bus = source()
    slow_entered = threading.Event()
    release_slow = threading.Event()
    fast_seen = threading.Event()
    dispatch_done = threading.Event()
    slow_calls = []
    callback_threads = []

    def slow(batch):
        callback_threads.append(threading.get_ident())
        slow_calls.append(batch.cursor["after_sequence"])
        slow_entered.set()
        assert release_slow.wait(2)

    slow_handle = bus.subscribe(public(), callback=slow, queue_limit=2)
    fast_handle = bus.subscribe(public(), callback=lambda batch: fast_seen.set())

    def dispatch():
        publish(bus, visibility="business_candidate")
        dispatch_done.set()

    dispatch_thread = threading.Thread(target=dispatch)
    try:
        dispatch_thread.start()
        assert dispatch_done.wait(2)
        assert slow_entered.wait(2)
        assert fast_seen.wait(2)
        assert callback_threads[0] != dispatch_thread.ident
        publish(bus, visibility="business_candidate")
        slow_handle.cancel()
        slow_handle.cancel()
        assert bus.subscriber_count == 1
        publish(bus, visibility="business_candidate")
        release_slow.set()
        assert slow_handle.join(2)
        assert slow_calls == [1]
    finally:
        release_slow.set()
        slow_handle.cancel()
        fast_handle.cancel()
        dispatch_thread.join(2)
        assert slow_handle.join(2)
        assert fast_handle.join(2)
        bus.close()
    assert bus.callback_worker_count == 0


def test_callback_worker_slots_remain_bounded_until_admitted_callbacks_finish():
    bus = source(max_subscribers=1)
    entered = threading.Event()
    release = threading.Event()
    handle = bus.subscribe(
        public(), callback=lambda batch: (entered.set(), release.wait(2)),
    )
    try:
        publish(bus, visibility="business_candidate")
        assert entered.wait(2)
        handle.cancel()
        assert bus.subscriber_count == 0
        with pytest.raises(ContractValidationError, match="worker limit"):
            bus.subscribe(public(), callback=lambda batch: None)
        release.set()
        assert handle.join(2)
        replacement = bus.subscribe(public(), callback=lambda batch: None)
        replacement.cancel()
        assert replacement.join(2)
    finally:
        release.set()
        handle.cancel()
        assert handle.join(2)
        bus.close()
    assert bus.callback_worker_count == 0


def test_subscriber_and_projection_tables_are_bounded():
    bus = source(max_subscribers=1, max_projections=2)
    first = bus.subscribe(public())
    with pytest.raises(ContractValidationError, match="subscriber limit"):
        bus.subscribe(public())
    first.cancel()
    second = bus.subscribe(public(projection_revision="v2"))
    second.cancel()
    with pytest.raises(ContractValidationError, match="projection limit"):
        bus.subscribe(public(projection_revision="v3"))


def test_seal_drains_terminal_then_releases_workers_and_close_releases_source():
    bus = source()
    closed_seen = threading.Event()
    callback_batches = []

    def observer_callback(batch):
        callback_batches.append(batch)
        if batch.status == "closed":
            closed_seen.set()

    handle = bus.subscribe(EventProjection.private(), callback=observer_callback)
    try:
        original = publish(bus)
        bus.update_snapshot({"status": "succeeded", "turn_ids": ["saved"]})
        bus.seal()
        bus.seal()
        assert closed_seen.wait(2)
        assert handle.join(2)
        assert [batch.status for batch in callback_batches] == ["events", "closed"]
        assert callback_batches[0].events == [original]
        assert bus.subscriber_count == 0
        assert bus.callback_worker_count == 0
        assert publish(bus) is None
        terminal = bus.subscribe(EventProjection.private())
        assert terminal.snapshot == {"status": "succeeded", "turn_ids": ["saved"]}
        assert terminal.cursor["after_sequence"] == 1
        assert terminal.poll().status == "closed"
        assert terminal.poll() is None
        assert bus.retained_event_count == 1
        bus.close()
        bus.release()
        assert bus.retained_event_count == 0
        with pytest.raises(ContractValidationError, match="closed"):
            bus.subscribe(public())
    finally:
        handle.cancel()
        assert handle.join(2)
        bus.close()


def test_close_cancels_terminal_and_overflow_handles_without_retaining_providers():
    class Provider:
        def __call__(self):
            return {"status": "running"}

    provider = Provider()
    provider_ref = weakref.ref(provider)
    bus = source(snapshot_provider=provider)
    terminal = bus.subscribe(public())
    overflow = bus.subscribe(public(), queue_limit=1)
    publish(bus, visibility="business_candidate")
    publish(bus, visibility="business_candidate")
    assert overflow.needs_reset
    bus.seal()
    del provider
    assert provider_ref() is not None
    bus.close()
    gc.collect()
    assert provider_ref() is None
    assert terminal.cancelled and overflow.cancelled
    assert terminal.poll() is None
    assert overflow.poll() is None


def test_pause_is_not_a_stream_end_and_generation_advances_without_new_stream():
    bus = source()
    observer = bus.subscribe(EventProjection.private())
    stream = bus.stream_instance_id
    old_generation = bus.generation
    pause_payload = {
        "previous_status": "running", "status": "paused", "phase": None,
        "available_actions": ["resume"], "action_rejections": {}, "reason_code": None,
    }
    pause = publish(bus, "run_state", pause_payload)
    assert observer.poll().events == [pause]
    assert not bus.sealed
    bus.advance_generation(uid())
    assert bus.stream_instance_id == stream
    assert bus.sequence == 1
    assert bus.publish(
        "diagnostic", {"code": "late", "category": "model"},
        payload_schema_ref=core_payload_schema_ref("diagnostic"), generation=old_generation,
        visibility="business_candidate",
    ) is None
    assert bus.sequence == 1
    assert observer.poll() is None
    resumed = publish(bus)
    assert resumed["sequence"] == 2
    assert resumed["generation"] != old_generation
    assert observer.poll().events == [resumed]
    assert bus.redeliver(pause) == pause
    observer.cancel()


def test_owner_revocation_rejects_late_report_without_assigning_identity():
    permitted = {"value": True}
    bus = source(owner_check=lambda generation: permitted["value"])
    observer = bus.subscribe(EventProjection.private())
    assert publish(bus)["sequence"] == 1
    observer.poll()
    permitted["value"] = False
    assert publish(bus) is None
    assert bus.sequence == 1
    assert observer.poll() is None
    observer.cancel()


def test_same_component_two_bindings_isolate_actual_run_identity():
    declarations, _, descriptor = frozen(behaviors=True)
    session = uid()
    first = RunEventSource(session, uid(), uid(), generation=uid(), declarations=declarations)
    second = RunEventSource(session, uid(), uid(), generation=uid(), declarations=declarations)
    observers = [bus.subscribe(EventProjection.private()) for bus in (first, second)]
    events = [
        bus.report(
            "chapter.check.progress", {"chapter": 1, "progress": 20, "private_notes": ""},
            payload_schema_ref={"schema_id": "chapter-progress", "version": 1},
            generation=bus.generation,
        )
        for bus in (first, second)
    ]
    assert events[0]["node_binding_id"] != events[1]["node_binding_id"]
    assert events[0]["run_id"] != events[1]["run_id"]
    assert [observer.poll().events for observer in observers] == [[events[0]], [events[1]]]
    assert "group_path" not in events[0]
    assert declarations.producer == descriptor
    for observer in observers:
        observer.cancel()


def test_generic_transform_report_needs_no_model_state_turn_or_agent_profile():
    declarations, registry, descriptor = frozen(behaviors=True, kind="transform")
    bus = RunEventSource(uid(), uid(), uid(), generation=uid(), declarations=declarations)
    bus.update_snapshot({"status": "computing", "component_version": descriptor["component_version"]})
    monitor = bus.subscribe(EventProjection.private())
    registry.unregister("chapter.check.progress", 1)
    event = bus.report(
        "chapter.check.progress", {"chapter": 2, "progress": 80, "private_notes": ""},
        payload_schema_ref={"schema_id": "chapter-progress", "version": 1},
        generation=bus.generation,
    )
    assert monitor.snapshot["status"] == "computing"
    assert monitor.poll().events == [event]
    assert "turn_id" not in event["payload"]
    assert "input_snapshot_id" not in event["payload"]
    monitor.cancel()


def test_unknown_bad_or_core_impersonating_report_fails_without_emitting():
    declarations, _, _ = frozen(behaviors=True)
    bus = RunEventSource(uid(), uid(), uid(), generation=uid(), declarations=declarations)
    observer = bus.subscribe(EventProjection.private())
    for event_type, ref, payload in [
        ("not.declared", {"schema_id": "unknown", "version": 1}, {}),
        ("chapter.check.progress", {"schema_id": "chapter-progress", "version": 2},
         {"chapter": 1, "progress": 20, "private_notes": ""}),
        ("chapter.check.progress", {"schema_id": "chapter-progress", "version": 1},
         {"chapter": 1, "progress": 101, "private_notes": ""}),
        ("run_state", core_payload_schema_ref("run_state"), {}),
        ("workflow_published", core_payload_schema_ref("workflow_published"), {}),
    ]:
        with pytest.raises(ContractValidationError):
            bus.report(event_type, payload, payload_schema_ref=ref, generation=bus.generation)
    with pytest.raises(ContractValidationError):
        bus.publish(
            "chapter.check.progress", {}, payload_schema_ref={"schema_id": "chapter-progress", "version": 1},
            generation=bus.generation,
        )
    assert bus.sequence == 0
    assert observer.poll() is None
    observer.cancel()


def test_public_behavior_projection_redacts_required_private_field_without_claiming_source_schema():
    declarations, _, _ = frozen(behaviors=True)
    bus = RunEventSource(uid(), uid(), uid(), generation=uid(), declarations=declarations)
    projection = public(allowlist={"chapter.check.progress": ("chapter", "progress")})
    observer = bus.subscribe(projection)
    original = bus.report(
        "chapter.check.progress",
        {"chapter": 1, "progress": 40, "private_notes": "private tool result"},
        payload_schema_ref={"schema_id": "chapter-progress", "version": 1},
        generation=bus.generation, visibility="business_candidate",
    )
    view = observer.poll().events[0]
    assert view["kind"] == "run_event_projection"
    assert view["schema_version"] == 1
    assert view["source_schema_version"] == 2
    assert view["payload"] == {"chapter": 1, "progress": 40}
    assert view["source_payload_schema_ref"] == original["payload_schema_ref"]
    assert "payload_schema_ref" not in view
    assert "private tool result" not in json.dumps(view)
    assert view["projection_ref"] == projection.projection_ref
    assert view["projection_revision"] == observer.cursor["projection_revision"]
    for field in ("event_id", "run_id", "node_binding_id", "sequence", "generation"):
        assert view[field] == original[field]
    validate_event_projection(view)
    with pytest.raises(ContractValidationError):
        validate_record("run_event", view)
    observer.cancel()


def test_private_behavior_monitor_preserves_full_validated_payload_with_allowlist_none():
    declarations, _, _ = frozen(behaviors=True)
    bus = RunEventSource(uid(), uid(), uid(), generation=uid(), declarations=declarations)
    monitor = bus.subscribe(EventProjection(
        "monitor", "v1", include_private=True,
        allowlist={"chapter.check.progress": None}, snapshot_fields=None,
    ))
    event = bus.report(
        "chapter.check.progress",
        {"chapter": 1, "progress": 40, "private_notes": "visible only to monitor"},
        payload_schema_ref={"schema_id": "chapter-progress", "version": 1},
        generation=bus.generation,
    )
    assert monitor.poll().events == [event]
    validate_record("run_event", event)
    assert "kind" not in event
    with pytest.raises(ContractValidationError, match="Only private monitors"):
        public(allowlist={"chapter.check.progress": None})
    monitor.cancel()


def test_all_filtered_batches_only_coalesce_scan_watermarks_without_false_overflow():
    bus = source()
    observer = bus.subscribe(public(), queue_limit=1)
    for _ in range(10):
        publish(bus)
    batch = observer.poll()
    assert batch.events == []
    assert batch.cursor["after_sequence"] == 10
    assert batch.status == "events"
    assert not observer.needs_reset
    assert observer.poll() is None
    visible = publish(bus, visibility="business_candidate")
    for _ in range(10):
        publish(bus)
    batch = observer.poll()
    assert batch.events == [public().project_event(visible)]
    assert batch.cursor["after_sequence"] == 21
    assert not observer.needs_reset
    observer.cancel()


@pytest.mark.parametrize("method", ["poll", "join"])
@pytest.mark.parametrize("timeout", [
    -1, True, "0", float("inf"), float("nan"),
    pytest.param(threading.TIMEOUT_MAX + 1, id="above-platform-limit"),
    pytest.param(1e100, id="finite-overflow"),
    pytest.param(10 ** 1000, id="integer-overflow"),
])
def test_invalid_observation_timeout_is_contract_error(method, timeout):
    bus = source()
    observer = bus.subscribe(public())
    with pytest.raises(ContractValidationError):
        getattr(observer, method)(timeout)
    observer.cancel()


@pytest.mark.parametrize("method", ["poll", "join"])
@pytest.mark.parametrize("timeout", [None, 0, 0.01])
def test_supported_observation_timeouts_remain_valid(method, timeout):
    bus = source()
    observer = bus.subscribe(public())
    publish(bus, visibility="business_candidate")
    result = getattr(observer, method)(timeout)
    if method == "poll":
        assert result.cursor["after_sequence"] == 1
    else:
        assert result is True
    observer.cancel()


@pytest.mark.parametrize("field,value", [
    ("buffer_limit", 0), ("buffer_limit", True),
    ("max_subscribers", 0), ("max_projections", 0),
])
def test_source_resource_limits_must_be_positive_integers(field, value):
    with pytest.raises(ContractValidationError):
        source(**{field: value})


def test_default_callback_budget_is_shared_across_sources_with_process_limit():
    first, second = source(), source()
    assert first._callback_budget is second._callback_budget
    assert first._callback_budget.limit == 32
    first.close()
    second.close()


def test_shared_callback_budget_remains_occupied_across_source_close_and_rebuild():
    budget = CallbackWorkerBudget(2)
    first, second, replacement = [source(callback_budget=budget) for _ in range(3)]
    entered_first, entered_second = threading.Event(), threading.Event()
    release_first, release_second = threading.Event(), threading.Event()
    fast_seen = threading.Event()
    first_handle = first.subscribe(
        public(), callback=lambda batch: (entered_first.set(), release_first.wait(2)),
    )
    second_handle = second.subscribe(
        public(), callback=lambda batch: (entered_second.set(), release_second.wait(2)),
    )
    replacement_handle = None
    try:
        publish(first, visibility="business_candidate")
        publish(second, visibility="business_candidate")
        assert entered_first.wait(2)
        assert entered_second.wait(2)
        assert budget.in_use == 2
        first.close()
        second.close()
        assert budget.in_use == 2
        assert first.subscriber_count == second.subscriber_count == 0
        with pytest.raises(ContractValidationError, match="capacity reached"):
            replacement.subscribe(public(), callback=lambda batch: None)
        assert replacement.subscriber_count == 0
        assert replacement.callback_worker_count == 0
        assert replacement.sequence == 0
        pull = replacement.subscribe(public())
        assert publish(replacement, visibility="business_candidate")["sequence"] == 1
        assert pull.poll().cursor["after_sequence"] == 1
        pull.cancel()
        release_first.set()
        assert first_handle.join(2)
        assert budget.in_use == 1
        replacement_handle = replacement.subscribe(
            public(), callback=lambda batch: fast_seen.set(),
        )
        assert budget.in_use == 2
        publish(replacement, visibility="business_candidate")
        assert fast_seen.wait(2)
        replacement_handle.cancel()
        assert replacement_handle.join(2)
        assert budget.in_use == 1
        release_second.set()
        assert second_handle.join(2)
        assert budget.in_use == 0
    finally:
        release_first.set()
        release_second.set()
        first_handle.cancel()
        second_handle.cancel()
        if replacement_handle is not None:
            replacement_handle.cancel()
            assert replacement_handle.join(2)
        assert first_handle.join(2)
        assert second_handle.join(2)
        first.close()
        second.close()
        replacement.close()
    assert budget.in_use == 0


def test_shared_budget_token_survives_source_gc_until_admitted_callback_finishes():
    budget = CallbackWorkerBudget(1)
    bus = source(callback_budget=budget)
    entered, release = threading.Event(), threading.Event()
    handle = bus.subscribe(
        public(), callback=lambda batch: (entered.set(), release.wait(2)),
    )
    replacement = None
    try:
        publish(bus, visibility="business_candidate")
        assert entered.wait(2)
        scope, generation = bus.scope_ref, bus.generation
        source_ref = weakref.ref(bus)
        bus.close()
        del bus
        gc.collect()
        assert source_ref() is None
        assert budget.in_use == 1
        declarations, _, _ = frozen()
        replacement = RunEventSource(
            scope["workflow_session_id"], scope["node_binding_id"], scope["run_id"],
            generation=generation, declarations=declarations, callback_budget=budget,
        )
        with pytest.raises(ContractValidationError, match="capacity reached"):
            replacement.subscribe(public(), callback=lambda batch: None)
        release.set()
        assert handle.join(2)
        assert budget.in_use == 0
        next_handle = replacement.subscribe(public(), callback=lambda batch: None)
        next_handle.cancel()
        assert next_handle.join(2)
    finally:
        release.set()
        handle.cancel()
        assert handle.join(2)
        if replacement is not None:
            replacement.close()
    assert budget.in_use == 0


def test_callback_worker_start_failure_rolls_back_shared_and_source_slots(monkeypatch):
    budget = CallbackWorkerBudget(1)
    bus = source(callback_budget=budget)

    def failing_start(self):
        raise RuntimeError("private startup diagnostic and api_key=secret")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", failing_start)
        with pytest.raises(ContractValidationError, match="failed to start") as error:
            bus.subscribe(public(), callback=lambda batch: None)
    assert "api_key" not in str(error.value)
    assert budget.in_use == 0
    assert bus.callback_worker_count == 0
    assert bus.subscriber_count == 0
    assert bus.sequence == 0
    handle = bus.subscribe(public(), callback=lambda batch: None)
    handle.cancel()
    assert handle.join(2)
    assert budget.in_use == 0
    bus.close()


def test_callback_budget_only_counts_tokens_and_release_is_idempotent():
    budget = CallbackWorkerBudget(1)
    slot = budget.reserve()
    assert budget.in_use == 1
    with pytest.raises(ContractValidationError, match="capacity reached"):
        budget.reserve()
    slot.release()
    slot.release()
    assert budget.in_use == 0
    with pytest.raises(ContractValidationError):
        CallbackWorkerBudget(True)
