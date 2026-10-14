"""Uninstalled M3 request boundary; the existing kernel still owns tools/messages."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from uuid import uuid4

from .context_compaction import checkpoint_message, replace_compacted_messages
from .contract_errors import DiagnosticError, require
from .contract_graph import validate_context_message_history
from .contract_json import content_digest, validate_json_value
from .model_execution_contracts import (
    compaction_eligible_message_ids, expected_compaction_request, snapshot_wire_tools,
    validate_input_snapshot_v2,
)
from .model_request_budget import validate_context_reduction
from .model_request_contracts import validate_model_result, validate_operation_payload
from .runtime import CanonicalModelAdapter


@dataclass
class _Invocation:
    key: str
    messages: list
    purpose: str
    tools: list
    preparation: dict | None = None
    request: dict | None = None
    result: dict | None = None
    dispatch_started: bool = False
    discarded: bool = False
    events: dict = field(default_factory=dict)


@dataclass
class _Boundary:
    digest: str
    original: list
    accepted: list
    normal: _Invocation
    after_normal: _Invocation | None = None
    trigger: dict | None = None
    summary: _Invocation | None = None
    discarded: dict | None = None
    before: dict | None = None
    after: dict | None = None
    reduced: list | None = None
    operation: dict | None = None
    completion: dict | None = None


class ModelExecutionRequests:
    """Drive one normal boundary and at most one native compaction.

    ``host_call`` is the trusted invocation's public M2 capability bridge.
    ``publish_fact(id, event)`` must durably accept the same original ID/event
    before returning. A failed call retains that exact event and boundary.
    No tool is executed here and no new node/executor is registered.
    """

    def __init__(self, snapshot, *, host_call, publish_fact):
        self._snapshot = validate_input_snapshot_v2(snapshot)
        require(all(callable(callback) for callback in (host_call, publish_fact)),
                "model_execution_host_invalid", "Exact trusted request callbacks are required")
        payload = self._snapshot["config"]["payload"]
        self._binding = deepcopy(payload["public_model_binding"])
        self._policy = deepcopy(payload["context_compaction"]["policy"])
        self._tools = snapshot_wire_tools(self._snapshot)
        self._host_call, self._publish_fact = host_call, publish_fact
        self._boundaries: dict[str, _Boundary] = {}
        self._pending_key: str | None = None
        self._request_index = 0
        self._attempt_count = 0
        self._bridge_id = str(uuid4())
        self._closed = False
        self._lock = RLock()

    @property
    def bridge_id(self):
        return self._bridge_id

    @property
    def request_count(self):
        return self._request_index

    @property
    def attempt_count(self):
        return self._attempt_count

    def validate_snapshot(self, snapshot):
        require(not self._closed and content_digest(validate_input_snapshot_v2(snapshot))
                == content_digest(self._snapshot),
                "model_execution_bridge_mismatch", "Request bridge belongs to another snapshot or was released")

    def _call(self, operation, payload):
        capability = "model-projection:read" if operation == "measure-context" else "models:call"
        result = self._host_call(capability, operation, deepcopy(payload))
        return validate_operation_payload(operation, result, response=True)

    def _selection(self, invocation):
        return {"binding_id": self._binding["binding_id"], "request_key": invocation.key}

    def _prepare(self, invocation):
        if invocation.preparation is None:
            prepared = self._call("prepare-request", {
                **self._selection(invocation), "purpose": invocation.purpose,
                "messages": CanonicalModelAdapter._project(invocation.messages),
                "tools": invocation.tools,
            })
            settings = prepared["settings"]
            require(settings["binding_id"] == self._binding["binding_id"]
                    and settings["purpose"] == invocation.purpose
                    and settings["configuration"]["reference"] == self._binding["reference"],
                    "model_execution_preparation_mismatch", "Preparation differs from this accepted model input")
            invocation.preparation = prepared
        return invocation.preparation

    def _event(self, invocation, kind, payload):
        if kind not in invocation.events:
            invocation.events[kind] = {
                "id": str(uuid4()), "event": {"kind": kind, "payload": deepcopy(payload)}, "accepted": False,
            }
        saved = invocation.events[kind]
        require(saved["event"] == {"kind": kind, "payload": payload},
                "model_execution_event_conflict", "Retained execution event changed")
        if not saved["accepted"]:
            try:
                self._publish_fact(saved["id"], deepcopy(saved["event"]))
            except Exception as error:
                raise DiagnosticError(
                    "model_execution_fact_acceptance_failed",
                    "Original request bridge event could not be accepted", event_kind=kind,
                    event_id=saved["id"], cause_code=getattr(error, "reason_code", None) or "internal_error",
                ) from None
            saved["accepted"] = True

    def _dispatch(self, invocation):
        prepared = self._prepare(invocation)
        require(prepared["hard_capacity_ok"], "model_request_capacity_exceeded",
                "Prepared wire exceeds actual model capacity",
                capacity_diagnostic=deepcopy(prepared["capacity_diagnostic"]))
        if invocation.request is None:
            self._request_index += 1
            invocation.request = {
                "request_id": prepared["request_id"], "request_index": self._request_index,
                "messages": deepcopy(invocation.messages), "tools": deepcopy(invocation.tools),
                "settings_id": prepared["settings"]["settings_id"],
                "reserved_model_fact_ids": deepcopy(prepared["reserved_model_fact_ids"]),
            }
        self._event(invocation, "model_request", invocation.request)
        if invocation.result is None:
            if not invocation.dispatch_started:
                invocation.dispatch_started = True
                self._attempt_count += 1
            result = self._call("dispatch-request", {
                **self._selection(invocation), "preparation_id": prepared["preparation_id"],
            })
            result = validate_model_result(result, settings=prepared["settings"])
            expected_refs = [{"fact_id": prepared["reserved_model_fact_ids"][stage]}
                             for stage in ("request", "attempt", "outcome")]
            require(result["request_id"] == prepared["request_id"] and result["fact_refs"] == expected_refs,
                    "model_execution_result_mismatch", "Result does not acknowledge its reserved model facts")
            invocation.result = result
        self._event(invocation, "model_result", {
            "request_id": invocation.request["request_id"], "settings_id": invocation.request["settings_id"],
            "result": invocation.result,
        })
        return invocation.result

    def _measure(self, invocation, messages):
        evidence = self._call("measure-context", {
            "binding_id": self._binding["binding_id"],
            "preparation_id": invocation.preparation["preparation_id"],
            "messages": CanonicalModelAdapter._project(messages), "tools": self._tools,
        })
        require(evidence["settings_id"] == invocation.preparation["settings"]["settings_id"]
                and evidence["messages_digest"] == content_digest(CanonicalModelAdapter._project(messages))
                and evidence["tools_digest"] == content_digest(self._tools),
                "model_execution_measurement_mismatch", "Measurement differs from the retained work context")
        return evidence

    def _compact(self, boundary):
        trigger = self._prepare(boundary.normal)
        if boundary.trigger is None:
            covered = compaction_eligible_message_ids(
                self._snapshot, boundary.original, boundary.accepted)
            require(bool(covered), "context_compaction_no_eligible_text",
                    "No authorized closed text is eligible for compaction")
            compaction_id, instruction_id = str(uuid4()), str(uuid4())
            messages = expected_compaction_request(
                self._snapshot, boundary.original, covered,
                compaction_id=compaction_id, instruction_message_id=instruction_id,
                accepted_messages=boundary.accepted,
            )
            boundary.trigger = {
                "decision_id": str(uuid4()), "preparation": deepcopy(trigger),
                "compaction_id": compaction_id, "covered": covered, "checkpoint_id": str(uuid4()),
            }
            boundary.summary = _Invocation(boundary.normal.key + ":summary", messages, "compaction", [])
        decision = {field: deepcopy(boundary.trigger[field]) for field in ("decision_id", "preparation")}
        self._event(boundary.normal, "context_compaction_decided", decision)
        if boundary.discarded is None:
            discarded = self._call("discard-prepared-request", {
                **self._selection(boundary.normal), "preparation_id": trigger["preparation_id"],
            })
            require(discarded == {"request_id": trigger["request_id"], "discarded": True},
                    "model_execution_discard_mismatch", "Discard differs from the trigger preparation")
            boundary.discarded = discarded
            boundary.normal.discarded = True
        summary = boundary.summary
        self._prepare(summary)
        if boundary.before is None:
            boundary.before = self._measure(summary, boundary.original)
        result = self._dispatch(summary)
        if boundary.reduced is None:
            require(result["finish_reason"] == "stop" and not result["tool_calls"]
                    and type(result["content"]) is str and bool(result["content"].strip()),
                    "context_compaction_summary_invalid", "Summary must be nonempty pure text without tool calls")
            checkpoint = checkpoint_message(
                boundary.trigger["compaction_id"], boundary.trigger["checkpoint_id"], result["content"])
            boundary.reduced = replace_compacted_messages(
                boundary.original, boundary.trigger["covered"], checkpoint)
        if boundary.after is None:
            boundary.after = self._measure(summary, boundary.reduced)
        validate_context_reduction(boundary.before, boundary.after, summary.preparation["settings"])
        if boundary.operation is None:
            checkpoint = next(message for message in boundary.reduced
                              if message["message_id"] == boundary.trigger["checkpoint_id"])
            boundary.operation = {
                "kind": "compact", "compaction_id": boundary.trigger["compaction_id"],
                "before_message_ids": [message["message_id"] for message in boundary.original],
                "covered_message_ids": list(boundary.trigger["covered"]), "checkpoint": deepcopy(checkpoint),
                "after_message_ids": [message["message_id"] for message in boundary.reduced],
                "request": deepcopy(summary.request), "result": deepcopy(result),
                "capacity": {"before": deepcopy(boundary.before), "after": deepcopy(boundary.after)},
                "trigger_decision": {**decision, "discard": deepcopy(boundary.discarded)},
            }
        self._event(summary, "context_compaction_applied", boundary.operation)
        return boundary.reduced

    def advance(self, request_key, messages, *, accepted_messages=(), on_boundary=None):
        """Return the original result on reentry; never create another retry key."""
        with self._lock:
            require(not self._closed, "model_recovery_unavailable", "Request bridge was released")
            require(on_boundary is None or callable(on_boundary), "model_execution_request_invalid",
                    "Request boundary callback must be callable")
            require(type(request_key) is str and 0 < len(request_key) <= 384
                    and request_key.strip() == request_key,
                    "model_execution_request_invalid", "Logical request key must be exact and bounded")
            validate_json_value(messages)
            require(type(messages) is list and bool(messages), "model_execution_request_invalid",
                    "A normal boundary requires nonempty closed canonical messages")
            validate_context_message_history(messages)
            accepted = deepcopy(list(accepted_messages))
            validate_json_value(accepted)
            digest = content_digest({"messages": messages, "accepted_messages": accepted})
            boundary = self._boundaries.get(request_key)
            if boundary is not None:
                require(boundary.digest == digest, "model_request_conflict", "Original boundary input changed")
                if boundary.completion is not None:
                    return deepcopy(boundary.completion)
            require(self._pending_key in (None, request_key), "model_execution_boundary_pending",
                    "The original boundary is unresolved; a new logical key cannot bypass it")
            if boundary is None:
                boundary = _Boundary(digest, deepcopy(messages), accepted,
                                     _Invocation(request_key + ":normal", deepcopy(messages), "normal",
                                                 deepcopy(self._tools)))
                self._boundaries[request_key] = boundary
            self._pending_key = request_key
            preparation = self._prepare(boundary.normal)
            operations = []
            normal = boundary.normal
            current = boundary.original
            if preparation["requires_compaction"] and self._policy is not None and self._policy["enabled"]:
                current = self._compact(boundary)
                operations = [deepcopy(boundary.operation)]
                if on_boundary is not None:
                    on_boundary("after_compaction")
                if boundary.after_normal is None:
                    boundary.after_normal = _Invocation(
                        request_key + ":after", deepcopy(current), "normal", deepcopy(self._tools))
                normal = boundary.after_normal
                # The exact compacted view gets one normal preparation, even at a low trigger.
                self._prepare(normal)
            result = self._dispatch(normal)
            boundary.completion = {
                "request": deepcopy(normal.request), "result": deepcopy(result), "messages": deepcopy(current),
                "operations": operations,
                "model_requests": [deepcopy(invocation.request) for invocation in (
                    boundary.summary, normal) if invocation is not None],
            }
            self._pending_key = None
            return deepcopy(boundary.completion)

    def close(self):
        with self._lock:
            for boundary in self._boundaries.values():
                for invocation in (boundary.normal, boundary.summary, boundary.after_normal):
                    if (invocation is None or invocation.preparation is None or invocation.discarded
                            or invocation.dispatch_started or invocation.result is not None):
                        continue
                    prepared = invocation.preparation
                    discarded = self._call("discard-prepared-request", {
                        **self._selection(invocation), "preparation_id": prepared["preparation_id"],
                    })
                    require(discarded == {"request_id": prepared["request_id"], "discarded": True},
                            "model_execution_discard_mismatch",
                            "Close must discard only its exact undispatched preparation")
                    invocation.discarded = True
            self._closed = True
