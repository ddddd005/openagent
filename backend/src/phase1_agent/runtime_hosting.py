"""Public cooperative hosting, with one control authority per invocation.

Handles own their business state. The host retains only opaque continuation,
control state, accepted receipts and completed outputs. No durable restoration
or thread termination is provided by this protocol.
"""

from __future__ import annotations

import copy
import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, ValidationError

from .contract_json import canonical_bytes, validate_json_value
from .host_sdk import HostContractError, bounded_name, ensure
from .runtime_executor_contracts import ExecutorReference, ExecutorRegistry


@dataclass(frozen=True)
class InvocationOwner:
    workflow_session_id: str
    chain_run_id: str
    node_binding_id: str
    node_run_id: str

    def to_dict(self) -> dict:
        value = dict(vars(self))
        for identity in value.values():
            try:
                parsed = UUID(identity) if type(identity) is str else None
            except ValueError:
                parsed = None
            ensure(parsed is not None and parsed.version == 4 and str(parsed) == identity,
                   "runtime_invalid_owner", "Invocation owner identities must be canonical UUID4")
        return value

    @classmethod
    def from_dict(cls, value: Any) -> InvocationOwner:
        ensure(type(value) is dict and set(value) == {
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"},
            "runtime_invalid_owner", "Invocation owner fields are invalid")
        result = cls(**value)
        result.to_dict()
        return result


@dataclass(frozen=True)
class HostedOutcome:
    status: str
    outputs: dict | None = None
    receipt: Any = None
    continuation_ref: str | None = None


class _SafePoint(BaseException):
    """Internal transfer of control; ordinary executor error handlers cannot swallow it."""


@dataclass
class _Invocation:
    owner: InvocationOwner
    reference: ExecutorReference
    handle: Any
    context: Any
    status: str = "ready"
    generation: int = 1
    control_epoch: int = 0
    pause_requested: bool = False
    pause_request_id: str | None = None
    continuation: Any = None
    continuation_ref: str | None = None
    outputs: dict | None = None
    receipt: Any = None
    result_envelope: dict | None = None
    disposed: bool = False
    disposal_error: str | None = None
    release_requested: bool = False
    fact_sequence: int = 0
    facts: dict[str, tuple[bytes, Any]] = field(default_factory=dict)
    pending_facts: dict[str, dict] = field(default_factory=dict)
    commands: dict[str, tuple[str, dict]] = field(default_factory=dict)


class ExecutorCallbacks:
    """Generation-bound reporting and read-only cooperative pause controls."""

    def __init__(self, host: RuntimeHost, owner: InvocationOwner, generation: int):
        self._host, self.owner, self.generation = host, owner, generation

    @property
    def pause_requested(self) -> bool:
        return self._host._requested(self.owner, self.generation)

    @property
    def control_epoch(self) -> int:
        return self._host._epoch(self.owner, self.generation)

    def pause_point(self, continuation: Any) -> bool:
        """Return False without a request; otherwise yield a validated safe point."""
        return self._host._pause_point(self.owner, self.generation, continuation)

    def publish_fact(self, fact_id: str, payload: dict) -> Any:
        return self._host._fact(self.owner, self.generation, fact_id, payload)

    def report_progress(self, payload: dict) -> None:
        self._host._progress(self.owner, self.generation, payload)


class RuntimeHost:
    """Route control to exact owners and reject obsolete reporting permissions.

    ``factory(config, inputs, context)`` returns a handle exposing
    ``advance(callbacks, continuation) -> named_outputs`` and ``dispose()``.
    The optional pause adapter is ``adapter(handle, continuation) -> bool``.
    A fact sink must accept an ownership envelope before a caller can proceed.
    Result sinks are optional: without one, returned outputs are candidates for
    the graph's existing validation and atomic result/write-set settlement.
    """

    def __init__(self, registry: ExecutorRegistry, *, fact_sink: Callable | None = None,
                 progress_sink: Callable | None = None, result_sink: Callable | None = None,
                 information_router=None, information_binding_sink: Callable | None = None,
                 information_component_resolver: Callable | None = None):
        self.registry = registry.detached(frozen=True)
        self._fact_sink, self._progress_sink, self._result_sink = fact_sink, progress_sink, result_sink
        self._information_router = information_router
        self._information_binding_sink = information_binding_sink
        self._information_component_resolver = information_component_resolver
        self._invocations: dict[InvocationOwner, _Invocation] = {}
        self._node_runs: dict[str, InvocationOwner] = {}
        self._lock = threading.RLock()

    def start(self, owner: InvocationOwner, reference: ExecutorReference, config: dict,
              inputs: dict, context: Any) -> dict:
        owner.to_dict()
        reference.to_dict()
        validate_json_value([config, inputs])
        ensure(type(config) is dict and type(inputs) is dict,
               "runtime_invalid_input", "Hosted config and inputs must be objects")
        with self._lock:
            ensure(owner not in self._invocations and owner.node_run_id not in self._node_runs,
                   "runtime_duplicate_invocation", "Invocation already exists or its node run has another owner")
            executor = self.registry.get(reference)
            ensure(executor is not None, "runtime_missing_executor", "Exact executor is unavailable")
            handle = executor.factory(copy.deepcopy(config), copy.deepcopy(inputs), context)
            valid = callable(getattr(handle, "advance", None)) and callable(getattr(handle, "dispose", None))
            if not valid and callable(getattr(handle, "dispose", None)):
                handle.dispose()
            ensure(valid, "runtime_invalid_handle", "Hosted handle must implement advance and dispose")
            self._invocations[owner] = _Invocation(owner, reference, handle, context)
            self._node_runs[owner.node_run_id] = owner
            invocation = self._invocations[owner]
        try:
            self._bind_information(invocation)
        except BaseException:
            with self._lock:
                invocation.status = "failed"
                self._dispose(invocation)
            raise
        return self.snapshot(owner)

    def _bind_information(self, invocation: _Invocation) -> None:
        self._bind_node_information(
            invocation.owner, invocation.generation, invocation.handle, invocation.context,
            invocation=invocation)

    def _bind_node_information(self, owner, generation, handle, context, *, invocation=None) -> None:
        if self._information_router is None:
            return
        if self._information_component_resolver is not None:
            component_id, component_version = self._information_component_resolver(owner.to_dict(), context)
        else:
            definition = getattr(context, "definition", None)
            ensure(definition is not None, "information_missing_node_type",
                   "Information binding requires an exact node declaration")
            component_id, component_version = definition.component_id, definition.component_version
        definitions = self._information_router.bind_node(
            owner, generation, component_id, component_version, handle=handle, context=context)
        try:
            if invocation is not None:
                with self._lock:
                    ensure(invocation.generation == generation and not invocation.disposed
                           and not invocation.release_requested,
                           "information_live_unavailable", "Invocation was released during information binding")
            if definitions and self._information_binding_sink is not None:
                # Durable sinks may enter application storage; never hold the host lock here.
                self._information_binding_sink(owner.to_dict(), generation, definitions)
        except BaseException:
            self._information_router.release(owner, generation)
            raise

    def bind_node_information(self, context) -> None:
        """Bind a plain node's optional sources without creating a hosted handle."""
        owner = InvocationOwner(*(getattr(context, key) for key in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")))
        owner.to_dict()
        self._bind_node_information(owner, 1, None, context)

    def contains(self, owner: InvocationOwner) -> bool:
        with self._lock:
            return owner in self._invocations

    def context(self, owner: InvocationOwner) -> Any:
        with self._lock:
            return self._lookup(owner).context

    def snapshot(self, owner: InvocationOwner) -> dict:
        with self._lock:
            return self._snapshot(self._lookup(owner))

    def request_pause(self, owner: InvocationOwner, request_id: str | None = None) -> dict:
        identity = request_id or str(uuid4())
        ensure(bounded_name(identity), "runtime_invalid_command", "Control command requires a bounded identity")
        with self._lock:
            invocation = self._lookup(owner)
            previous = self._command(invocation, identity, "pause")
            if previous is not None:
                return previous
            ensure(invocation.status in ("ready", "running", "paused"),
                   "runtime_terminal_invocation", "Terminal invocation cannot be paused")
            if not invocation.pause_requested:
                invocation.control_epoch += 1
                invocation.pause_requested = True
                invocation.pause_request_id = identity
            receipt = self._snapshot(invocation)
            invocation.commands[identity] = ("pause", receipt)
            return copy.deepcopy(receipt)

    def resume(self, owner: InvocationOwner, request_id: str) -> dict:
        ensure(bounded_name(request_id), "runtime_invalid_command", "Resume requires a bounded command identity")
        with self._lock:
            invocation = self._lookup(owner)
            previous = self._command(invocation, request_id, "resume")
            if previous is not None:
                return previous
            ensure(invocation.status == "paused" and invocation.continuation is not None
                   and not invocation.disposed and invocation.disposal_error is None, "runtime_not_paused",
                   "Only an acknowledged live same-process safe point can resume")
            invocation.generation += 1
            invocation.control_epoch += 1
            invocation.pause_requested = False
            invocation.pause_request_id = None
            invocation.status = "ready"
            receipt = self._snapshot(invocation)
            invocation.commands[request_id] = ("resume", receipt)
        try:
            self._bind_information(invocation)
        except BaseException:
            with self._lock:
                invocation.status = "failed"
                self._dispose(invocation)
            raise
        return copy.deepcopy(receipt)

    def drive(self, owner: InvocationOwner) -> HostedOutcome:
        with self._lock:
            invocation = self._lookup(owner)
            if invocation.status == "paused":
                return HostedOutcome("paused", continuation_ref=invocation.continuation_ref)
            if invocation.status == "succeeded":
                return HostedOutcome("succeeded", copy.deepcopy(invocation.outputs), copy.deepcopy(invocation.receipt))
            ensure(invocation.status == "ready" and not invocation.disposed,
                   "runtime_invocation_unavailable", "Invocation is running, failed or closed")
            invocation.status = "running"
            generation, handle, continuation = invocation.generation, invocation.handle, invocation.continuation
            callbacks = ExecutorCallbacks(self, owner, generation)
        try:
            outputs = handle.advance(callbacks, continuation)
            validate_json_value(outputs)
            ensure(type(outputs) is dict, "runtime_invalid_result", "Executor must return named output candidates")
            envelope = self._envelope(invocation, generation)
            envelope["outputs"] = copy.deepcopy(outputs)
            with self._lock:
                self._active(owner, generation)
                invocation.outputs = copy.deepcopy(outputs)
                invocation.result_envelope = copy.deepcopy(envelope)
                receipt = self._result_sink(copy.deepcopy(envelope)) if self._result_sink is not None else None
                invocation.outputs, invocation.receipt = copy.deepcopy(outputs), copy.deepcopy(receipt)
                invocation.status = "succeeded"
                invocation.pause_requested = False
                invocation.pause_request_id = None
                invocation.continuation = None
                self._dispose(invocation)
                return HostedOutcome("succeeded", copy.deepcopy(outputs), copy.deepcopy(receipt))
        except _SafePoint:
            with self._lock:
                ensure(invocation.status == "paused", "runtime_invalid_safe_point", "Safe point was not acknowledged")
                if invocation.release_requested:
                    self._dispose(invocation)
                    invocation.status = "closed"
                    return HostedOutcome("closed")
                return HostedOutcome("paused", continuation_ref=invocation.continuation_ref)
        except BaseException as error:
            with self._lock:
                recoverable = getattr(error, "reason_code", None) is None and (invocation.outputs is not None or (
                    callable(getattr(invocation.handle, "has_pending_acceptance", None))
                    and invocation.handle.has_pending_acceptance()))
                invocation.status = "acceptance_pending" if recoverable else "failed"
                invocation.pause_requested = False
                invocation.pause_request_id = None
                invocation.continuation = None
                if not recoverable:
                    self._dispose(invocation)
            raise

    def has_pending_acceptance(self, owner: InvocationOwner) -> bool:
        with self._lock:
            return owner in self._invocations and self._invocations[owner].status == "acceptance_pending"

    def accepts_fact_envelope(self, envelope: dict) -> bool:
        """Authorize active reports or exact retained local-commit replays."""
        with self._lock:
            owner = InvocationOwner.from_dict(envelope["owner"])
            invocation = self._invocations.get(owner)
            if invocation is None or invocation.status != "running" or invocation.disposed:
                return False
            pending = invocation.pending_facts.get(envelope.get("fact_id"))
            return envelope.get("generation") == invocation.generation or pending == envelope

    def retry_acceptance(self, owner: InvocationOwner) -> HostedOutcome:
        """Reuse a completed result, never restart the executor's business loop."""
        with self._lock:
            invocation = self._lookup(owner)
            ensure(invocation.status == "acceptance_pending" and not invocation.disposed,
                   "runtime_acceptance_unavailable", "No retained result acceptance is available")
            invocation.generation += 1
            generation = invocation.generation
            invocation.status = "running"
            callbacks = ExecutorCallbacks(self, owner, generation)
        try:
            self._bind_information(invocation)
            outputs = invocation.outputs
            if outputs is None:
                recover = getattr(invocation.handle, "recover_acceptance", None)
                ensure(callable(recover), "runtime_acceptance_unavailable",
                       "Executor has no completed result acceptance adapter")
                outputs = recover(callbacks)
                validate_json_value(outputs)
                ensure(type(outputs) is dict, "runtime_invalid_result", "Executor must return named output candidates")
            with self._lock:
                self._active(owner, generation)
                invocation.outputs = copy.deepcopy(outputs)
                envelope = invocation.result_envelope
                if envelope is None:
                    envelope = self._envelope(invocation, generation)
                    envelope["outputs"] = copy.deepcopy(outputs)
                    invocation.result_envelope = copy.deepcopy(envelope)
                invocation.receipt = (self._result_sink(copy.deepcopy(envelope)) if self._result_sink is not None else None)
                invocation.status = "succeeded"
                self._dispose(invocation)
                return HostedOutcome("succeeded", copy.deepcopy(outputs), copy.deepcopy(invocation.receipt))
        except BaseException as error:
            with self._lock:
                invocation.status = "acceptance_pending" if getattr(error, "reason_code", None) is None else "failed"
                if invocation.status == "failed":
                    self._dispose(invocation)
            raise

    def release(self, owner: InvocationOwner) -> None:
        """Cooperate with in-flight work; never terminate a worker or remote call."""
        with self._lock:
            invocation = self._lookup(owner)
            if self._information_router is not None:
                self._information_router.release(owner)
            if invocation.status == "running":
                invocation.release_requested = True
                return
            self._dispose(invocation)
            invocation.status = "closed"
            invocation.context = None
            invocation.outputs = None
            invocation.result_envelope = None

    @property
    def active_handle_count(self) -> int:
        with self._lock:
            return sum(not item.disposed for item in self._invocations.values())

    def _lookup(self, owner: InvocationOwner) -> _Invocation:
        ensure(isinstance(owner, InvocationOwner) and owner in self._invocations,
               "runtime_owner_mismatch", "Hosted invocation does not belong to this exact owner")
        return self._invocations[owner]

    def _active(self, owner: InvocationOwner, generation: int) -> _Invocation:
        invocation = self._lookup(owner)
        ensure(invocation.generation == generation and invocation.status == "running"
               and not invocation.disposed, "runtime_stale_callback",
               "Callback no longer has an active generation permission")
        return invocation

    @staticmethod
    def _command(invocation: _Invocation, identity: str, kind: str) -> dict | None:
        previous = invocation.commands.get(identity)
        if previous is None:
            return None
        ensure(previous[0] == kind, "runtime_command_conflict", "Command identity was reused for another action")
        return copy.deepcopy(previous[1])

    def _snapshot(self, invocation: _Invocation) -> dict:
        return {"owner": invocation.owner.to_dict(), "executor_ref": invocation.reference.to_dict(),
                "generation": invocation.generation, "control_epoch": invocation.control_epoch,
                "status": invocation.status, "requested": invocation.pause_requested,
                "request_id": invocation.pause_request_id, "continuation_ref": invocation.continuation_ref,
                "continuation_mode": self.registry.get(invocation.reference).definition.continuation_mode,
                "supported_controls": list(self.registry.supported_controls(invocation.reference)),
                "disposed": invocation.disposed, "disposal_error": invocation.disposal_error,
                "fact_sequence": invocation.fact_sequence}

    @staticmethod
    def _envelope(invocation: _Invocation, generation: int) -> dict:
        return {"schema_version": 1, "owner": invocation.owner.to_dict(),
                "executor_ref": invocation.reference.to_dict(), "generation": generation}

    def _requested(self, owner: InvocationOwner, generation: int) -> bool:
        with self._lock:
            return self._active(owner, generation).pause_requested

    def _epoch(self, owner: InvocationOwner, generation: int) -> int:
        with self._lock:
            return self._active(owner, generation).control_epoch

    def _pause_point(self, owner: InvocationOwner, generation: int, continuation: Any) -> bool:
        with self._lock:
            invocation = self._active(owner, generation)
            if not invocation.pause_requested:
                return False
            support = self.registry.pause_support(invocation.reference)
            if support is None:
                return False
            ensure(continuation is not None and support.adapter(invocation.handle, continuation) is True,
                   "runtime_invalid_safe_point", "Pause adapter did not confirm a retained safe point")
            invocation.continuation = continuation
            invocation.continuation_ref = str(uuid4())
            invocation.status = "paused"
            raise _SafePoint()

    def _fact(self, owner: InvocationOwner, generation: int, fact_id: str, payload: dict) -> Any:
        validate_json_value(payload)
        ensure(bounded_name(fact_id), "runtime_invalid_fact", "Fact identity must be a bounded name")
        ensure(type(payload) is dict and len(canonical_bytes(payload)) <= 1_000_000,
               "runtime_invalid_fact", "Fact payload must be a bounded object")
        with self._lock:
            invocation = self._active(owner, generation)
            definition = self.registry.get(invocation.reference).definition
            try:
                Draft202012Validator(definition.fact_schema).validate(payload)
            except ValidationError as exc:
                raise HostContractError("runtime_invalid_fact", "Executor fact schema mismatch") from exc
            digest = hashlib.sha256(canonical_bytes(payload)).digest()
            previous = invocation.facts.get(fact_id)
            if previous is not None:
                ensure(previous[0] == digest, "runtime_fact_conflict", "Fact identity cannot change payload")
                return copy.deepcopy(previous[1])
            ensure(self._fact_sink is not None, "runtime_fact_sink_unavailable",
                   "Facts must be accepted before external execution can proceed")
            envelope = self._envelope(invocation, generation)
            envelope.update({"sequence": invocation.fact_sequence + 1, "fact_id": fact_id,
                             "payload": copy.deepcopy(payload)})
            pending = invocation.pending_facts.get(fact_id)
            if pending is not None:
                ensure(pending["payload"] == payload, "runtime_fact_conflict",
                       "Pending fact identity cannot change payload")
                envelope = copy.deepcopy(pending)
            invocation.pending_facts[fact_id] = copy.deepcopy(envelope)
            receipt = self._fact_sink(envelope)
            invocation.fact_sequence += 1
            invocation.facts[fact_id] = (digest, copy.deepcopy(receipt))
            invocation.pending_facts.pop(fact_id, None)
            return copy.deepcopy(receipt)

    def _progress(self, owner: InvocationOwner, generation: int, payload: dict) -> None:
        validate_json_value(payload)
        ensure(type(payload) is dict and len(canonical_bytes(payload)) <= 64_000,
               "runtime_invalid_progress", "Progress must be a bounded object")
        with self._lock:
            invocation = self._active(owner, generation)
            if self._progress_sink is not None:
                envelope = self._envelope(invocation, generation)
                envelope["payload"] = copy.deepcopy(payload)
                self._progress_sink(envelope)

    def _dispose(self, invocation: _Invocation) -> None:
        if self._information_router is not None:
            self._information_router.release(invocation.owner)
        if invocation.disposed:
            return
        try:
            invocation.handle.dispose()
        except Exception:
            invocation.disposal_error = "runtime_dispose_failed"
            return
        invocation.disposed = True
        invocation.disposal_error = None
        invocation.handle = None
        invocation.continuation = None
        invocation.pending_facts.clear()
