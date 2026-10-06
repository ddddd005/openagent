"""Bounded, process-local observation of an authoritative node run.

The coordinator supplies state; retained events are never used to reconstruct
history. A callback is admitted under the serial gate and runs outside it.
Cancellation prevents further admission, while an admitted callback may finish.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
import threading
import time
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4
import weakref

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, validate_json_value
from .event_contracts import CORE_EVENT_TYPES


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _uuid4(value: Any, field: str) -> None:
    try:
        parsed = UUID(value) if type(value) is str else None
    except (ValueError, AttributeError):
        parsed = None
    _require(parsed is not None and parsed.version == 4 and str(parsed) == value,
             f"{field} must be a canonical UUID v4 string")


def _positive(value: Any, field: str) -> None:
    _require(type(value) is int and value > 0, f"{field} must be a positive integer")


class CallbackWorkerBudget:
    """Shared worker admission tokens; never retains sources or callbacks."""

    def __init__(self, limit: int = 32) -> None:
        _positive(limit, "callback worker limit")
        self._limit = limit
        self._in_use = 0
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    def reserve(self) -> _CallbackWorkerSlot:
        with self._lock:
            _require(self._in_use < self._limit,
                     "Subscriber callback worker capacity reached")
            self._in_use += 1
            return _CallbackWorkerSlot(self)

    def _release(self) -> None:
        with self._lock:
            _require(self._in_use > 0, "Callback worker token contract was violated")
            self._in_use -= 1


class _CallbackWorkerSlot:
    def __init__(self, budget: CallbackWorkerBudget) -> None:
        self._budget = budget
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if not self._released:
                self._released = True
                self._budget._release()


_PROCESS_CALLBACK_WORKERS = CallbackWorkerBudget(32)


class SubscriptionResetRequired(ContractValidationError):
    """A scan cursor cannot be continued; obtain a fresh state snapshot."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Subscription reset required: {reason}")


@dataclass(frozen=True, init=False)
class EventProjection:
    """Fixed authorization and field selection, independent of registration.

    Public projections require a type/field allowlist. Private monitors may
    explicitly choose all declared events; neither mode runs custom projectors.
    """

    projection_ref: str
    projection_revision: str
    allowlist: Mapping[str, tuple[str, ...] | None] | None
    snapshot_fields: tuple[str, ...] | None
    include_private: bool
    _fingerprint: bytes

    def __init__(
        self, projection_ref: str, projection_revision: str, *,
        allowlist: Mapping[str, Sequence[str] | None] | None = None,
        snapshot_fields: Sequence[str] | None = (),
        include_private: bool = False,
    ) -> None:
        _require(type(projection_ref) is str and bool(projection_ref),
                 "Projection reference must be nonempty")
        _require(type(projection_revision) is str and bool(projection_revision),
                 "Projection revision must be nonempty")
        _require(type(include_private) is bool, "Projection visibility must be boolean")
        _require(include_private or allowlist is not None,
                 "Public projection requires an explicit event allowlist")
        _require(include_private or snapshot_fields is not None,
                 "Public projection requires explicit snapshot fields")
        normalized: dict[str, tuple[str, ...] | None] | None = None
        if allowlist is not None:
            _require(isinstance(allowlist, Mapping), "Event allowlist must be a mapping")
            normalized = {}
            for event_type, fields in allowlist.items():
                _require(type(event_type) is str and bool(event_type),
                         "Allowlisted event type must be nonempty")
                if fields is None:
                    _require(include_private, "Only private monitors may select a full payload")
                    normalized[event_type] = None
                    continue
                _require(not isinstance(fields, (str, bytes))
                         and isinstance(fields, Sequence),
                         "Allowlisted payload fields must be a sequence")
                field_tuple = tuple(fields)
                _require(all(type(item) is str and bool(item) for item in field_tuple)
                         and len(set(field_tuple)) == len(field_tuple),
                         "Allowlisted payload fields must be unique nonempty strings")
                normalized[event_type] = tuple(sorted(field_tuple))
        if snapshot_fields is not None:
            _require(not isinstance(snapshot_fields, (str, bytes))
                     and isinstance(snapshot_fields, Sequence),
                     "Snapshot fields must be a sequence")
        snapshot_tuple = None if snapshot_fields is None else tuple(snapshot_fields)
        if snapshot_fields is not None:
            _require(all(type(item) is str and bool(item) for item in snapshot_tuple)
                     and len(set(snapshot_tuple)) == len(snapshot_tuple),
                     "Snapshot fields must be unique nonempty strings")
            snapshot_tuple = tuple(sorted(snapshot_tuple))
        fingerprint = canonical_bytes({
            "projection_ref": projection_ref,
            "declared_revision": projection_revision,
            "allowlist": None if normalized is None else {
                key: None if value is None else list(value)
                for key, value in normalized.items()
            },
            "snapshot_fields": None if snapshot_tuple is None else list(snapshot_tuple),
            "include_private": include_private,
        })
        object.__setattr__(self, "projection_ref", projection_ref)
        object.__setattr__(self, "projection_revision", projection_revision)
        object.__setattr__(self, "allowlist",
                           None if normalized is None else MappingProxyType(normalized))
        object.__setattr__(self, "snapshot_fields", snapshot_tuple)
        object.__setattr__(self, "include_private", include_private)
        object.__setattr__(self, "_fingerprint", fingerprint)

    @classmethod
    def private(cls, projection_ref: str = "node-monitor", *,
                projection_revision: str = "1") -> EventProjection:
        return cls(projection_ref, projection_revision, include_private=True,
                   snapshot_fields=None)

    def project_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if not self.include_private and event["visibility"] != "business_candidate":
            return None
        fields = None
        if self.allowlist is not None:
            if event["event_type"] not in self.allowlist:
                return None
            fields = self.allowlist[event["event_type"]]
        result = deepcopy(event)
        if fields is not None:
            result["kind"] = "run_event_projection"
            result["source_schema_version"] = result["schema_version"]
            result["schema_version"] = 1
            result["source_payload_schema_ref"] = result.pop("payload_schema_ref")
            result["projection_ref"] = self.projection_ref
            result["projection_revision"] = self.cursor_revision
            result["payload"] = {
                field: deepcopy(event["payload"][field])
                for field in fields if field in event["payload"]
            }
        return result

    def project_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if self.snapshot_fields is None:
            return deepcopy(snapshot)
        return {
            field: deepcopy(snapshot[field])
            for field in self.snapshot_fields if field in snapshot
        }

    @property
    def cursor_revision(self) -> str:
        return sha256(self._fingerprint).hexdigest()


@dataclass(frozen=True)
class LiveBatch:
    events: list[dict[str, Any]]
    cursor: dict[str, Any]
    status: str = "events"
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        result = {"events": deepcopy(self.events), "cursor": deepcopy(self.cursor)}
        if self.status != "events":
            result["status"] = self.status
        if self.reason is not None:
            result["reason"] = self.reason
        return result


class SubscriptionHandle:
    """Observation-only pull handle, optionally backed by one bounded worker."""

    def __init__(
        self, source: RunEventSource, projection: EventProjection,
        snapshot: dict[str, Any], cursor: dict[str, Any], queue_limit: int,
        callback: Callable[[LiveBatch], None] | None,
    ) -> None:
        self.snapshot = deepcopy(snapshot)
        self.cursor = deepcopy(cursor)
        self.snapshot_cursor = source._cursor(projection, source._sequence)
        self._source_ref = weakref.ref(source)
        self._projection = projection
        self._lock = source._lock
        self._condition = threading.Condition(self._lock)
        self._queue: deque[LiveBatch] = deque()
        self._queue_limit = queue_limit
        self._callback = callback
        self._active = True
        self._cancelled = False
        self._worker: threading.Thread | None = None
        self._worker_slot: _CallbackWorkerSlot | None = None
        self._worker_accounted = False
        self._in_callback = False
        self._error: dict[str, str] | None = None
        self._needs_reset = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def needs_reset(self) -> bool:
        with self._lock:
            return self._needs_reset

    @property
    def error(self) -> dict[str, str] | None:
        with self._lock:
            return deepcopy(self._error)

    @property
    def worker_alive(self) -> bool:
        worker = self._worker
        return worker is not None and worker.is_alive()

    def _enqueue(self, batch: LiveBatch) -> None:
        if not self._active:
            return
        if (batch.status == "events" and not batch.events and self._queue
                and self._queue[-1].status == "events"):
            previous = self._queue[-1]
            self._queue[-1] = LiveBatch(previous.events, deepcopy(batch.cursor))
            self._condition.notify_all()
            return
        if len(self._queue) >= self._queue_limit:
            self._queue.clear()
            self._queue.append(LiveBatch([], deepcopy(batch.cursor), "gap", "queue_overflow"))
            self._needs_reset = True
            self._active = False
            source = self._source_ref()
            if source is not None:
                source._subscriptions.discard(self)
        else:
            self._queue.append(batch)
        self._condition.notify_all()

    def _take(self, timeout: float | None) -> LiveBatch | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._queue:
                if not self._active or self._cancelled:
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            batch = self._queue.popleft()
            self.cursor = deepcopy(batch.cursor)
            if batch.status != "events":
                source = self._source_ref()
                if source is not None:
                    source._handles.discard(self)
            return batch

    def poll(self, timeout: float | None = 0.0) -> LiveBatch | None:
        _require(timeout is None or type(timeout) in (int, float)
                 and 0 <= timeout <= threading.TIMEOUT_MAX and isfinite(timeout),
                 "Subscription timeout must be within the supported nonnegative range")
        _require(self._callback is None or not self.worker_alive,
                 "Callback subscriptions cannot also be polled")
        return self._take(timeout)

    next_batch = poll

    def cancel(self) -> None:
        with self._condition:
            if self._cancelled:
                return
            self._cancelled = True
            self._active = False
            self._queue.clear()
            self._callback = None
            source = self._source_ref()
            if source is not None:
                source._subscriptions.discard(self)
                source._handles.discard(self)
            self._condition.notify_all()

    def join(self, timeout: float | None = 1.0) -> bool:
        """Wait only for an already-admitted callback; never hard-kill it."""
        _require(timeout is None or type(timeout) in (int, float)
                 and 0 <= timeout <= threading.TIMEOUT_MAX and isfinite(timeout),
                 "Worker timeout must be within the supported nonnegative range")
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        return not self.worker_alive

    def _start_worker(self) -> None:
        self._worker = threading.Thread(
            target=_callback_worker, args=(self,), daemon=True,
            name=f"run-event-subscriber-{uuid4()}",
        )
        self._worker.start()

    def _finish_worker(self) -> None:
        with self._lock:
            self._callback = None
            if self._worker_accounted:
                self._worker_accounted = False
                source = self._source_ref()
                if source is not None:
                    source._worker_count -= 1
                    source._handles.discard(self)
            slot, self._worker_slot = self._worker_slot, None
        if slot is not None:
            slot.release()


def _callback_worker(handle: SubscriptionHandle) -> None:
    try:
        while True:
            with handle._condition:
                while not handle._queue and handle._active and not handle._cancelled:
                    handle._condition.wait()
                if handle._cancelled or not handle._queue:
                    return
                batch = handle._queue.popleft()
                callback = handle._callback
                if callback is None:
                    return
                handle.cursor = deepcopy(batch.cursor)
                handle._in_callback = True
            try:
                callback(batch)
            except BaseException:
                with handle._condition:
                    handle._error = {"code": "subscriber_error",
                                     "reason": "callback_failed"}
                    handle._active = False
                    handle._queue.clear()
                    handle._callback = None
                    source = handle._source_ref()
                    if source is not None:
                        source._subscriptions.discard(handle)
                    handle._condition.notify_all()
                return
            finally:
                with handle._condition:
                    handle._in_callback = False
            if batch.status != "events":
                return
    finally:
        handle._finish_worker()


class RunEventSource:
    """Serial run source with independent bounded subscriber queues.

    A shared coordinator RLock makes snapshot and live admission atomic with
    state changes. Callback workers never request coordinator state or invoke
    callbacks under this gate. Pausing does not seal the source.
    """

    def __init__(
        self, workflow_session_id: str, node_binding_id: str, run_id: str, *,
        generation: str,
        declarations: Any = None,
        validator: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
        owner_check: Callable[[str], bool] | None = None,
        snapshot_provider: Callable[[], dict[str, Any]] | None = None,
        lock: Any = None,
        buffer_limit: int = 256,
        max_subscribers: int = 32,
        max_projections: int = 32,
        callback_budget: CallbackWorkerBudget | None = None,
    ) -> None:
        for field, value in (
            ("workflow_session_id", workflow_session_id),
            ("node_binding_id", node_binding_id), ("run_id", run_id),
            ("generation", generation),
        ):
            _uuid4(value, field)
        _positive(buffer_limit, "buffer_limit")
        _positive(max_subscribers, "max_subscribers")
        _positive(max_projections, "max_projections")
        _require(callback_budget is None or isinstance(callback_budget, CallbackWorkerBudget),
                 "Callback budget must be a shared worker budget")
        _require(declarations is not None or callable(validator),
                 "Run event source requires frozen payload validation")
        _require(declarations is None or validator is None,
                 "Use declarations or a validator, not both")
        _require(owner_check is None or callable(owner_check),
                 "Owner check must be callable")
        _require(snapshot_provider is None or callable(snapshot_provider),
                 "Snapshot provider must be callable")
        self._lock = lock if lock is not None else threading.RLock()
        self._scope = {
            "workflow_session_id": workflow_session_id,
            "node_binding_id": node_binding_id, "run_id": run_id,
        }
        self._stream_instance_id = str(uuid4())
        self._generation = generation
        self._declarations = declarations
        self._validator = validator
        self._owner_check = owner_check
        self._snapshot_provider = snapshot_provider
        self._snapshot: dict[str, Any] = {}
        self._buffer_limit = buffer_limit
        self._max_subscribers = max_subscribers
        self._max_projections = max_projections
        self._callback_budget = (
            _PROCESS_CALLBACK_WORKERS if callback_budget is None else callback_budget
        )
        self._events: deque[dict[str, Any]] = deque()
        self._by_id: dict[str, dict[str, Any]] = {}
        self._sequence = 0
        self._subscriptions: set[SubscriptionHandle] = set()
        self._handles: weakref.WeakSet[SubscriptionHandle] = weakref.WeakSet()
        self._worker_count = 0
        self._projections: dict[tuple[str, str], bytes] = {}
        self._sealed = False
        self._closed = False

    @property
    def stream_instance_id(self) -> str:
        return self._stream_instance_id

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def generation(self) -> str:
        with self._lock:
            return self._generation

    @property
    def scope_ref(self) -> dict[str, str]:
        return deepcopy(self._scope)

    @property
    def sealed(self) -> bool:
        with self._lock:
            return self._sealed

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    @property
    def callback_worker_count(self) -> int:
        with self._lock:
            return self._worker_count

    @property
    def retained_event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        _require(type(snapshot) is dict, "Run snapshot must be an object")
        validate_json_value(snapshot)
        with self._lock:
            _require(not self._closed, "Run event source is closed")
            self._snapshot = deepcopy(snapshot)

    def advance_generation(self, generation: str) -> None:
        _uuid4(generation, "generation")
        with self._lock:
            _require(not self._closed and not self._sealed,
                     "Closed run event source cannot advance generation")
            self._generation = generation

    revoke = advance_generation

    def _validate_payload(
        self, event_type: str, payload_schema_ref: dict[str, Any],
        payload: dict[str, Any], *, behavior: bool,
    ) -> None:
        _require(type(event_type) is str and bool(event_type),
                 "Event type must be nonempty")
        _require(type(payload_schema_ref) is dict, "Payload schema reference must be an object")
        _require(type(payload) is dict, "Event payload must be an object")
        validate_json_value(payload_schema_ref)
        validate_json_value(payload)
        _require(set(payload_schema_ref) == {"schema_id", "version"}
                 and type(payload_schema_ref["schema_id"]) is str
                 and bool(payload_schema_ref["schema_id"])
                 and type(payload_schema_ref["version"]) is int
                 and payload_schema_ref["version"] > 0,
                 "Payload schema reference requires exact id and positive version")
        _require(behavior or event_type in CORE_EVENT_TYPES,
                 "Core publication requires a core event type")
        _require(not behavior or event_type not in CORE_EVENT_TYPES,
                 "Behavior reports cannot impersonate core events")
        if self._declarations is not None:
            method = self._declarations.validate_report if behavior else (
                self._declarations.validate_core
            )
            method(event_type, payload_schema_ref, payload)
        else:
            self._validator(event_type, payload_schema_ref, payload)

    def publish(
        self, event_type: str, payload: dict[str, Any], *,
        payload_schema_ref: dict[str, Any], generation: str,
        visibility: str = "private", event_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self._publish(event_type, payload, payload_schema_ref=payload_schema_ref,
                             generation=generation, visibility=visibility,
                             event_id=event_id, behavior=False)

    def report(
        self, event_type: str, payload: dict[str, Any], *,
        payload_schema_ref: dict[str, Any], generation: str,
        visibility: str = "private", event_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self._publish(event_type, payload, payload_schema_ref=payload_schema_ref,
                             generation=generation, visibility=visibility,
                             event_id=event_id, behavior=True)

    def _publish(
        self, event_type: str, payload: dict[str, Any], *,
        payload_schema_ref: dict[str, Any], generation: str, visibility: str,
        event_id: str | None, behavior: bool,
    ) -> dict[str, Any] | None:
        _uuid4(generation, "generation")
        with self._lock:
            if (self._closed or self._sealed or generation != self._generation
                    or self._owner_check is not None and not self._owner_check(generation)):
                return None
            _require(type(visibility) is str and visibility in ("private", "business_candidate"),
                     "Unsupported event visibility")
            self._validate_payload(event_type, payload_schema_ref, payload, behavior=behavior)
            if event_id is not None:
                _uuid4(event_id, "event_id")
                original = self._by_id.get(event_id)
                _require(original is not None,
                         "Event identity is not retained by this source")
                _require(
                    original["event_type"] == event_type
                    and original["payload_schema_ref"] == payload_schema_ref
                    and original["payload"] == payload
                    and original["visibility"] == visibility
                    and original["generation"] == generation,
                    "Conflicting event redelivery",
                )
                return deepcopy(original)
            self._sequence += 1
            event = {
                "schema_version": 2, "event_id": str(uuid4()), **self._scope,
                "sequence": self._sequence, "event_type": event_type,
                "payload_schema_ref": deepcopy(payload_schema_ref),
                "payload": deepcopy(payload), "visibility": visibility,
                "generation": generation,
            }
            self._events.append(event)
            self._by_id[event["event_id"]] = event
            if len(self._events) > self._buffer_limit:
                expired = self._events.popleft()
                del self._by_id[expired["event_id"]]
            for handle in tuple(self._subscriptions):
                projected = handle._projection.project_event(event)
                handle._enqueue(LiveBatch(
                    [] if projected is None else [projected],
                    self._cursor(handle._projection, self._sequence),
                ))
            return deepcopy(event)

    def redeliver(self, event: dict[str, Any]) -> dict[str, Any]:
        """Ignore an exact retained source event without assigning a new identity."""
        _require(type(event) is dict, "Redelivered event must be an object")
        validate_json_value(event)
        _uuid4(event.get("event_id"), "event_id")
        with self._lock:
            _require(not self._closed, "Run event source is closed")
            original = self._by_id.get(event.get("event_id"))
            _require(original is not None, "Event identity is not retained by this source")
            _require(canonical_bytes(original) == canonical_bytes(event),
                     "Conflicting event redelivery")
            return deepcopy(original)

    def _cursor(self, projection: EventProjection, sequence: int) -> dict[str, Any]:
        return {
            "stream_instance_id": self._stream_instance_id,
            "scope_ref": deepcopy(self._scope),
            "projection_revision": projection.cursor_revision,
            "after_sequence": sequence,
        }

    def _validate_cursor(self, cursor: dict[str, Any], projection: EventProjection) -> int:
        if type(cursor) is not dict or set(cursor) != {
            "stream_instance_id", "scope_ref", "projection_revision", "after_sequence",
        }:
            raise SubscriptionResetRequired("invalid_cursor")
        if cursor["stream_instance_id"] != self._stream_instance_id:
            raise SubscriptionResetRequired("stream_changed")
        if cursor["scope_ref"] != self._scope:
            raise SubscriptionResetRequired("scope_changed")
        if cursor["projection_revision"] != projection.cursor_revision:
            raise SubscriptionResetRequired("projection_changed")
        after = cursor["after_sequence"]
        if type(after) is not int or after < 0 or after > self._sequence:
            raise SubscriptionResetRequired("invalid_scan_watermark")
        retained_start = self._events[0]["sequence"] if self._events else self._sequence + 1
        if after < retained_start - 1:
            raise SubscriptionResetRequired("source_buffer_lost")
        return after

    def subscribe(
        self, projection: EventProjection, *, cursor: dict[str, Any] | None = None,
        queue_limit: int = 64, callback: Callable[[LiveBatch], None] | None = None,
    ) -> SubscriptionHandle:
        _require(type(projection) is EventProjection, "Subscription requires a fixed projection")
        _positive(queue_limit, "queue_limit")
        _require(callback is None or callable(callback), "Subscriber callback must be callable")
        with self._lock:
            _require(not self._closed, "Run event source is closed")
            _require(len(self._handles) < self._max_subscribers,
                     "Run subscriber limit reached")
            _require(callback is None or self._worker_count < self._max_subscribers,
                     "Run callback worker limit reached")
            projection_key = (projection.projection_ref, projection.projection_revision)
            known = self._projections.get(projection_key)
            if known is not None and known != projection._fingerprint:
                raise SubscriptionResetRequired("projection_changed")
            _require(known is not None or len(self._projections) < self._max_projections,
                     "Run projection limit reached")
            after = self._sequence if cursor is None else self._validate_cursor(cursor, projection)
            snapshot = self._snapshot_provider() if self._snapshot_provider else self._snapshot
            _require(type(snapshot) is dict, "Run snapshot provider must return an object")
            validate_json_value(snapshot)
            handle = SubscriptionHandle(
                self, projection, projection.project_snapshot(snapshot),
                self._cursor(projection, after), queue_limit, callback,
            )
            self._projections[projection_key] = projection._fingerprint
            self._handles.add(handle)
            if cursor is not None and after < self._sequence:
                events = [
                    projected for event in self._events if event["sequence"] > after
                    if (projected := projection.project_event(event)) is not None
                ]
                handle._enqueue(LiveBatch(events, self._cursor(projection, self._sequence)))
            if self._sealed:
                handle._enqueue(LiveBatch([], self._cursor(projection, self._sequence),
                                          "closed", "run_terminal"))
                handle._active = False
            else:
                self._subscriptions.add(handle)
            if callback is not None:
                try:
                    handle._worker_slot = self._callback_budget.reserve()
                    self._worker_count += 1
                    handle._worker_accounted = True
                    handle._start_worker()
                except BaseException as exc:
                    handle.cancel()
                    handle._finish_worker()
                    if isinstance(exc, ContractValidationError):
                        raise
                    if isinstance(exc, Exception):
                        raise ContractValidationError(
                            "Subscriber callback worker failed to start"
                        ) from None
                    raise
            return handle

    def seal(self) -> None:
        """End live admission, preserving only bounded replay and current state."""
        with self._lock:
            if self._sealed or self._closed:
                return
            self._sealed = True
            for handle in tuple(self._subscriptions):
                handle._enqueue(LiveBatch(
                    [], self._cursor(handle._projection, self._sequence),
                    "closed", "run_terminal",
                ))
                handle._active = False
                handle._condition.notify_all()
            self._subscriptions.clear()

    def close(self) -> None:
        """Release source buffers and references without controlling the run."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for handle in tuple(self._handles):
                handle.cancel()
            self._subscriptions.clear()
            self._handles.clear()
            self._events.clear()
            self._by_id.clear()
            self._projections.clear()
            self._snapshot.clear()
            self._snapshot_provider = None
            self._owner_check = None
            self._declarations = None
            self._validator = None

    release = close
