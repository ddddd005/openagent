"""Versioned public execution facts, separate from history and private checkpoints."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import re
from typing import Any
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history, validate_pending_message_history
from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .contracts_v2 import validate_record
from .context_compaction import (
    compaction_instruction, compaction_parameters, eligible_compaction_ids,
    is_compactable_text, measure_context_capacity,
    policy_config, replace_compacted_messages, should_compact, validate_capacity,
    validate_compaction_operation, validate_compaction_request, validate_compaction_settings,
    validate_summary_result,
)
from .context_compaction_policy import effective_summary_prompt


EXECUTION_FACT_SCHEMA_VERSION = 1
EXECUTION_FACT_KINDS = frozenset((
    "message_accepted", "model_request", "model_attempt_started",
    "model_attempt_finished", "tool_dispatch", "tool_settled", "execution_failed",
    "context_compaction_started", "context_compaction_finished", "context_compaction_applied",
))
_IDENTITY_FIELDS = (
    "run_id", "chain_run_id", "workflow_session_id", "node_binding_id", "snapshot_id",
)
_FIELDS = {
    "schema_version", "fact_id", *_IDENTITY_FIELDS, "generation", "sequence",
    "kind", "created_at", "payload",
}
_PAYLOAD_FIELDS = {
    "message_accepted": {"message"},
    "model_request": {"request_id", "request_index", "messages", "tools", "model_parameters"},
    "model_attempt_started": {
        "request_id", "attempt_id", "request_index", "attempt_index", "retry_index",
    },
    "model_attempt_finished": {
        "request_id", "attempt_id", "outcome", "usage", "response_id", "model",
    },
    "tool_dispatch": {"tool_call_id", "tool_execution_id"},
    "tool_settled": {"tool_call_id", "tool_execution_id", "outcome", "message"},
    "execution_failed": {"code", "category", "model_requests", "attempts"},
    "context_compaction_started": {
        "compaction_id", "before_message_ids", "covered_message_ids", "request", "capacity",
    },
    "context_compaction_finished": {"compaction_id", "outcome", "result"},
    "context_compaction_applied": {
        "kind", "compaction_id", "before_message_ids", "covered_message_ids",
        "checkpoint", "after_message_ids", "request", "result", "capacity",
    },
}
_ATTEMPT_OUTCOMES = frozenset((
    "responded", "model_error", "protocol_error", "adapter_contract_error",
    "execution_interrupted", "host_interrupted",
))
_TOOL_OUTCOMES = frozenset((
    "success", "error", "outcome_unknown", "interrupted", "never_started", "unknown",
))
_UTC_MILLIS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z\Z",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _uuid(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _integer(value: Any, minimum: int = 1) -> bool:
    return type(value) is int and value >= minimum


def _timestamp(value: Any) -> bool:
    if type(value) is not str or _UTC_MILLIS.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True


def validate_execution_fact(value: dict[str, Any]) -> dict[str, Any]:
    """Validate one fact and return a detached strict-JSON value."""
    validate_json_value(value)
    _require(type(value) is dict and set(value) == _FIELDS,
             "Execution fact fields differ from schema")
    _require(type(value["schema_version"]) is int
             and value["schema_version"] == EXECUTION_FACT_SCHEMA_VERSION,
             "Unsupported execution fact schema version")
    _require(all(_uuid(value[field]) for field in
                 ("fact_id", *_IDENTITY_FIELDS, "generation")),
             "Execution fact identities must be canonical UUID4")
    _require(_integer(value["sequence"]), "Execution fact sequence must be positive")
    _require(_timestamp(value["created_at"]), "Execution fact timestamp must be UTC milliseconds")
    kind, payload = value["kind"], value["payload"]
    _require(type(kind) is str and kind in EXECUTION_FACT_KINDS, "Unknown execution fact kind")
    _require(type(payload) is dict and set(payload) == _PAYLOAD_FIELDS[kind],
             "Execution fact payload fields differ from schema")
    if kind == "message_accepted":
        message = validate_record("agent_message", payload["message"])
        _require((message["role"] == "assistant" and message["source"]["kind"] == "model")
                 or message["role"] == "tool"
                 or (message["role"] == "user" and
                     message["source"]["kind"] == "protocol_feedback"),
                 "Accepted execution messages must be kernel-produced increments")
    elif kind == "model_request":
        _require(_uuid(payload["request_id"]) and _integer(payload["request_index"]),
                 "Invalid model request identity or index")
        _require(type(payload["messages"]) is list, "Request messages must be a JSON array")
        for message in payload["messages"]:
            validate_record("agent_message", message)
        _require(type(payload["tools"]) is list
                 and all(type(tool) is dict for tool in payload["tools"])
                 and type(payload["model_parameters"]) is dict,
                 "Request tools and parameters must be complete JSON objects")
    elif kind == "model_attempt_started":
        _require(_uuid(payload["request_id"]) and _uuid(payload["attempt_id"])
                 and all(_integer(payload[field]) for field in
                         ("request_index", "attempt_index"))
                 and _integer(payload["retry_index"], 0) and payload["retry_index"] <= 3,
                 "Invalid model attempt identity or index")
    elif kind == "model_attempt_finished":
        _require(_uuid(payload["request_id"]) and _uuid(payload["attempt_id"])
                 and type(payload["outcome"]) is str
                 and payload["outcome"] in _ATTEMPT_OUTCOMES,
                 "Invalid model attempt finish")
        _require(payload["usage"] is None or type(payload["usage"]) is dict,
                 "Usage must be reported JSON or null when unknown")
        _require(all(payload[field] is None or type(payload[field]) is str
                     for field in ("response_id", "model")), "Invalid model response metadata")
    elif kind == "context_compaction_started":
        _require(_uuid(payload["compaction_id"]), "Invalid compaction identity")
        request = validate_compaction_request(payload["request"], payload["compaction_id"])
        before = request["messages"][:-1]
        _require(payload["before_message_ids"] == [message["message_id"] for message in before],
                 "Compaction request must preserve its complete before prefix")
        covered = payload["covered_message_ids"]
        _require(type(covered) is list and bool(covered)
                 and all(_uuid(identity) for identity in covered) and len(set(covered)) == len(covered)
                 and [message["message_id"] for message in before
                      if message["message_id"] in covered] == covered
                 and all(is_compactable_text(message) for message in before
                         if message["message_id"] in covered),
                 "Compaction coverage must be ordered text, not tools or current input")
        validate_capacity(payload["capacity"])
    elif kind == "context_compaction_finished":
        _require(_uuid(payload["compaction_id"]) and payload["outcome"] in _ATTEMPT_OUTCOMES,
                 "Invalid compaction finish")
        _require(payload["result"] is not None if payload["outcome"] == "responded" else True,
                 "Responded compaction must retain its response")
        if payload["result"] is not None:
            validate_summary_result(payload["result"], successful=payload["outcome"] == "responded")
    elif kind == "context_compaction_applied":
        validate_compaction_operation(payload)
    elif kind in {"tool_dispatch", "tool_settled"}:
        _require(_uuid(payload["tool_call_id"]), "Invalid tool call identity")
        if kind == "tool_dispatch":
            _require(_uuid(payload["tool_execution_id"]), "Invalid tool dispatch identity")
        else:
            _require(type(payload["outcome"]) is str and payload["outcome"] in _TOOL_OUTCOMES,
                     "Unknown tool settlement outcome")
            _require(payload["tool_execution_id"] is None
                     if payload["outcome"] == "never_started"
                     else _uuid(payload["tool_execution_id"]),
                     "Only never_started may omit tool execution identity")
            if payload["outcome"] == "unknown":
                _require(payload["message"] is None, "Unknown tool outcome cannot claim a result")
            else:
                message = validate_record("agent_message", payload["message"])
                _require(message["role"] == "tool" and len(message["blocks"]) == 1,
                         "Tool settlement requires one complete tool result message")
                block = message["blocks"][0]
                _require(block["tool_call_id"] == payload["tool_call_id"]
                         and block["tool_execution_id"] == payload["tool_execution_id"],
                         "Tool settlement identity differs from result message")
                if payload["outcome"] in {"success", "error"}:
                    _require(message["schema_version"] == 1
                             and block["status"] == payload["outcome"],
                             "Tool settlement outcome differs from result message")
                else:
                    _require(message["schema_version"] == 2
                             and message["source"]["reason_code"] == payload["outcome"],
                             "Tool settlement outcome differs from observation")
    else:
        _require(type(payload["code"]) is str and bool(payload["code"])
                 and type(payload["category"]) is str
                 and payload["category"] in {"model", "protocol", "contract", "interrupted"}
                 and _integer(payload["model_requests"], 0)
                 and _integer(payload["attempts"], 0), "Invalid execution failure facts")
    return loads_strict(canonical_bytes(value).decode("utf-8"))


class ExecutionFactHistory:
    """Check causal references while allowing an honestly unfinished tail."""

    def __init__(
        self, snapshot: dict[str, Any] | None = None,
        *, owner: dict[str, Any] | None = None,
        token_counter=None,
    ) -> None:
        self._snapshot = snapshot
        self._owner = owner
        self._facts: list[dict[str, Any]] = []
        self._accepted: list[dict[str, Any]] = []
        self._effective = list(snapshot["s0"]) if snapshot is not None else []
        descriptor = (snapshot.get("config", {}).get("payload", {}).get("context_compaction")
                      if snapshot is not None else None)
        self._compaction_settings = (validate_compaction_settings(descriptor, snapshot["s0"])
                                     if snapshot is not None else None)
        self._token_counter = token_counter
        self._compactions: dict[str, dict[str, Any]] = {}
        self._compaction_results: dict[str, dict[str, Any]] = {}
        self._active_compaction: str | None = None
        self._message_ids = {
            item["message_id"] for item in snapshot["s0"]
        } if snapshot is not None else set()
        self._requests: dict[str, dict[str, Any]] = {}
        self._attempts: dict[str, dict[str, Any]] = {}
        self._finished: dict[str, dict[str, Any]] = {}
        self._assistant_requests: set[str] = set()
        self._calls: dict[str, dict[str, Any]] = {}
        self._seen_calls = {
            block["tool_call_id"] for message in snapshot["s0"] for block in message["blocks"]
            if block["kind"] == "tool_call"
        } if snapshot is not None else set()
        self._pending_calls: list[str] = []
        self._uncertain_batch = False
        self._dispatches: dict[str, str] = {}
        self._settled: dict[str, dict[str, Any]] = {}
        self._execution_ids = {
            block["tool_execution_id"] for message in snapshot["s0"] for block in message["blocks"]
            if block["kind"] == "tool_result" and block["tool_execution_id"] is not None
        } if snapshot is not None else set()
        self._generations: set[str] = set()
        self._fact_ids: set[str] = set()

    @property
    def facts(self) -> list[dict[str, Any]]:
        return list(self._facts)

    @property
    def effective_messages(self) -> list[dict[str, Any]]:
        return loads_strict(canonical_bytes(self._effective).decode("utf-8"))

    def append(self, fact: dict[str, Any]) -> dict[str, Any]:
        value = validate_execution_fact(fact)
        if self._owner is not None:
            _require(all(value[field] == self._owner[field] for field in _IDENTITY_FIELDS),
                     "Execution fact history differs from run ownership")
        _require(value["sequence"] == len(self._facts) + 1, "Execution fact sequence is not contiguous")
        _require(value["fact_id"] not in self._fact_ids, "Duplicate execution fact identity")
        if self._facts:
            previous = self._facts[-1]
            _require(all(value[field] == previous[field] for field in _IDENTITY_FIELDS),
                     "Execution fact history changed run ownership")
            if value["generation"] != previous["generation"]:
                _require(value["generation"] not in self._generations,
                         "Execution fact generation was already retired")
        self._generations.add(value["generation"])
        payload = value["payload"]
        kind = value["kind"]
        if kind == "message_accepted":
            self._accept(payload["message"])
        elif kind == "model_request":
            self._request(payload)
        elif kind == "model_attempt_started":
            self._start_attempt(payload)
        elif kind == "model_attempt_finished":
            self._finish_attempt(payload)
        elif kind == "context_compaction_started":
            self._start_compaction(payload)
        elif kind == "context_compaction_finished":
            self._finish_compaction(payload)
        elif kind == "context_compaction_applied":
            self._apply_compaction(payload)
        elif kind == "tool_dispatch":
            call_id, execution_id = payload["tool_call_id"], payload["tool_execution_id"]
            _require(call_id in self._calls and call_id not in self._dispatches
                     and call_id not in self._settled, "Tool dispatch has no unexecuted accepted call")
            _require(bool(self._pending_calls) and call_id == self._pending_calls[0]
                     and not self._uncertain_batch,
                     "Tool dispatch bypassed call order or an uncertain batch")
            _require(execution_id not in self._execution_ids, "Tool execution identity was reused")
            self._dispatches[call_id] = execution_id
            self._execution_ids.add(execution_id)
        elif kind == "tool_settled":
            self._settle(payload)
        else:
            _require(payload["model_requests"] == len(self._requests)
                     and payload["attempts"] == len(self._attempts),
                     "Execution failure counters differ from recorded facts")
        self._fact_ids.add(value["fact_id"])
        self._facts.append(value)
        return value

    def _accept(self, message: dict[str, Any]) -> None:
        _require(self._active_compaction is None,
                 "Accepted messages cannot overlap an unfinished context replacement")
        _require(message["message_id"] not in self._message_ids, "Accepted message identity was reused")
        source = message["source"]
        _require(not self._pending_calls or message["role"] == "tool",
                 "Pending tools must close before another accepted message")
        if message["role"] == "assistant":
            request_id = source["request_id"]
            _require(request_id in self._requests and request_id not in self._assistant_requests
                     and request_id == next(reversed(self._requests))
                     and any(item["request_id"] == request_id and item["outcome"] == "responded"
                             for item in self._finished.values()),
                     "Assistant acceptance has no responded model attempt")
            self._assistant_requests.add(request_id)
            for block in message["blocks"]:
                if block["kind"] == "tool_call":
                    call_id = block["tool_call_id"]
                    _require(call_id not in self._seen_calls, "Accepted tool call identity was reused")
                    if self._snapshot is not None:
                        definitions = {item["name"]: item for item in self._snapshot["tool_definitions"]}
                        _require(block["tool_name"] in definitions
                                 and block["tool_definition_version"] ==
                                 definitions[block["tool_name"]]["version"],
                                 "Accepted tool call differs from frozen registry")
                    self._calls[call_id] = block
                    self._seen_calls.add(call_id)
                    self._pending_calls.append(call_id)
        elif message["role"] == "tool":
            _require(len(message["blocks"]) == 1, "Accepted tool result must contain one result")
            call_id = message["blocks"][0]["tool_call_id"]
            _require(bool(self._pending_calls) and call_id == self._pending_calls[0],
                     "Accepted tool results must retain call order")
            settled = self._settled.get(call_id)
            _require(settled is not None and settled["message"] is not None
                     and canonical_bytes(settled["message"]) == canonical_bytes(message),
                     "Accepted tool message differs from prior settlement")
            self._pending_calls.pop(0)
            if not self._pending_calls:
                self._uncertain_batch = False
        elif source["kind"] == "protocol_feedback":
            _require(source["request_id"] in self._requests,
                     "Protocol feedback refers to an unrecorded request")
        self._message_ids.add(message["message_id"])
        prefix = self._accepted + [message]
        if self._pending_calls:
            validate_pending_message_history(prefix, self._pending_calls)
        else:
            validate_message_history(prefix)
        self._accepted.append(message)
        self._effective.append(message)

    def _request(self, payload: dict[str, Any]) -> None:
        request_id = payload["request_id"]
        _require(request_id not in self._requests
                 and payload["request_index"] == len(self._requests) + 1,
                 "Model request identity or sequence was reused")
        _require(not any(attempt_id not in self._finished for attempt_id in self._attempts),
                 "Model request overlaps an unfinished attempt")
        _require(not self._pending_calls, "Model request bypassed unaccepted tool settlements")
        _require(self._active_compaction is None,
                 "Model request bypassed an unfinished context replacement")
        validate_message_history(payload["messages"])
        if self._snapshot is not None:
            _require(canonical_bytes(payload["messages"]) ==
                     canonical_bytes(self._effective),
                     "Model request does not match the accepted effective history")
            self._check_request_basis(payload)
        self._requests[request_id] = payload

    def _check_request_basis(self, payload: dict[str, Any], *, compaction=False) -> None:
        if self._snapshot is not None:
            parameters = (compaction_parameters(self._snapshot["model_parameters"], self._compaction_settings)
                          if compaction else self._snapshot["model_parameters"])
            _require(canonical_bytes(payload["model_parameters"]) ==
                     canonical_bytes(parameters),
                     "Model request parameters differ from frozen snapshot")
            definitions = self._snapshot["tool_definitions"]
            _require(len(payload["tools"]) == len(definitions),
                     "Model request tool registry differs from frozen snapshot")
            for wire, definition in zip(payload["tools"], definitions):
                function = wire.get("function")
                _require(wire.get("type") == "function" and type(function) is dict
                         and function.get("name") == definition["name"]
                         and canonical_bytes(function.get("parameters")) ==
                         canonical_bytes(definition["parameters_schema"])
                         and ("description" not in definition or
                              function.get("description") == definition["description"]),
                         "Model request tool differs from frozen snapshot")

    def _start_compaction(self, payload: dict[str, Any]) -> None:
        identity = payload["compaction_id"]
        _require(identity not in self._compactions and self._active_compaction is None,
                 "Compaction identity was reused or another replacement is active")
        _require(not self._pending_calls
                 and not any(attempt_id not in self._finished for attempt_id in self._attempts),
                 "Context compaction requires a closed tool and request boundary")
        before = payload["request"]["messages"][:-1]
        if self._snapshot is not None:
            _require(canonical_bytes(before) == canonical_bytes(self._effective),
                     "Compaction request changed the accepted prefix")
            settings = self._compaction_settings
            _require(settings is not None, "Compaction requires frozen native settings")
            config = policy_config(settings)
            _require(config is not None and config["enabled"]
                     and should_compact(payload["capacity"], config),
                     "Compaction bypassed its enabled policy or trigger")
            _require(payload["covered_message_ids"] == eligible_compaction_ids(
                self._snapshot["s0"], self._effective, settings, self._accepted,
            ), "Compaction changed fixed/current material or protected recent history")
            instruction = payload["request"]["messages"][-1]
            _require(instruction == compaction_instruction(
                identity, instruction["message_id"], effective_summary_prompt(settings["policy"]),
                payload["covered_message_ids"], self._effective,
            ), "Compaction instruction differs from its frozen prompt and permitted range")
            _require(payload["capacity"]["context_window_tokens"] == settings["context_window_tokens"]
                     and payload["capacity"]["output_reserve_tokens"] == settings["output_reserve_tokens"],
                     "Compaction changed the frozen capacity limits")
            if payload["capacity"]["token_count_kind"] == "utf8_bytes_estimate" or self._token_counter:
                _require(payload["capacity"] == measure_context_capacity(
                    self._effective, payload["request"]["tools"], settings, self._token_counter,
                ), "Compaction capacity differs from its request")
                auxiliary_settings = {
                    **settings, "output_reserve_tokens": settings.get(
                        "summary_max_tokens", settings["output_reserve_tokens"]),
                }
                auxiliary = measure_context_capacity(
                    payload["request"]["messages"], payload["request"]["tools"],
                    auxiliary_settings, self._token_counter,
                )
                _require(auxiliary["total_tokens"] < auxiliary["context_window_tokens"],
                         "Compaction request exceeds its frozen context capacity")
                _require(auxiliary["input_tokens"] <= settings.get(
                    "max_cold_input_tokens", auxiliary["input_tokens"]),
                    "Compaction request exceeds its frozen cold input budget")
            self._check_request_basis(payload["request"], compaction=True)
        self._compactions[identity] = payload
        self._active_compaction = identity

    def _finish_compaction(self, payload: dict[str, Any]) -> None:
        identity = payload["compaction_id"]
        _require(identity == self._active_compaction and identity in self._compactions
                 and identity not in self._compaction_results,
                 "Compaction finish has no unmatched maintenance dispatch")
        self._compaction_results[identity] = payload

    def _apply_compaction(self, payload: dict[str, Any]) -> None:
        identity = payload["compaction_id"]
        started = self._compactions.get(identity)
        finished = self._compaction_results.get(identity)
        _require(identity == self._active_compaction and started is not None
                 and finished is not None and finished["outcome"] == "responded",
                 "Context replacement has no accepted successful summary")
        _require(all(canonical_bytes(payload[key]) == canonical_bytes(started[key]) for key in
                     ("before_message_ids", "covered_message_ids", "request"))
                 and canonical_bytes(payload["result"]) == canonical_bytes(finished["result"])
                 and payload["capacity"]["before"] == started["capacity"],
                 "Context replacement differs from its accepted maintenance facts")
        before = payload["request"]["messages"][:-1]
        if self._snapshot is not None:
            _require(canonical_bytes(before) == canonical_bytes(self._effective),
                     "Context replacement used a stale effective view")
        _require(payload["checkpoint"]["message_id"] not in self._message_ids,
                 "Context checkpoint reused an original message identity")
        after = replace_compacted_messages(before, payload["covered_message_ids"], payload["checkpoint"])
        if self._compaction_settings is not None and (
            payload["capacity"]["after"]["token_count_kind"] == "utf8_bytes_estimate" or self._token_counter
        ):
            _require(payload["capacity"]["after"] == measure_context_capacity(
                after, payload["request"]["tools"], self._compaction_settings, self._token_counter,
            ), "Context replacement capacity differs from its derived view")
        self._effective = after
        self._message_ids.add(payload["checkpoint"]["message_id"])
        self._active_compaction = None

    def _start_attempt(self, payload: dict[str, Any]) -> None:
        request_id, attempt_id = payload["request_id"], payload["attempt_id"]
        request = self._requests.get(request_id)
        _require(request is not None and request == list(self._requests.values())[-1]
                 and request["request_index"] == payload["request_index"],
                 "Model attempt refers to another request")
        prior = [item for item in self._attempts.values() if item["request_id"] == request_id]
        _require(attempt_id not in self._attempts
                 and payload["attempt_index"] == len(self._attempts) + 1
                 and payload["retry_index"] == len(prior),
                 "Model attempt indexes are not contiguous")
        _require(not any(item not in self._finished for item in self._attempts),
                 "Model attempt overlaps an unfinished attempt")
        _require(not any(item["request_id"] == request_id and item["outcome"] == "responded"
                         for item in self._finished.values()), "Responded request cannot retry")
        self._attempts[attempt_id] = payload

    def _finish_attempt(self, payload: dict[str, Any]) -> None:
        attempt_id = payload["attempt_id"]
        attempt = self._attempts.get(attempt_id)
        _require(attempt is not None and attempt["request_id"] == payload["request_id"]
                 and attempt_id not in self._finished, "Model attempt finish has no unfinished start")
        self._finished[attempt_id] = payload

    def _settle(self, payload: dict[str, Any]) -> None:
        call_id = payload["tool_call_id"]
        _require(call_id in self._calls and call_id not in self._settled,
                 "Tool settlement has no unsettled accepted call")
        _require(bool(self._pending_calls) and call_id == self._pending_calls[0],
                 "Tool settlements must retain call order")
        if payload["outcome"] == "never_started":
            _require(call_id not in self._dispatches and self._uncertain_batch,
                     "Never_started requires a prior uncertain result in this batch")
        else:
            _require(self._dispatches.get(call_id) == payload["tool_execution_id"],
                     "Tool settlement does not match its authorized dispatch")
        if payload["outcome"] in {"outcome_unknown", "interrupted"}:
            self._uncertain_batch = True
        self._settled[call_id] = payload


def validate_execution_fact_history(
    facts: Sequence[dict[str, Any]], *, snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Validate references without inventing results for unfinished calls."""
    _require(isinstance(facts, Sequence) and not isinstance(facts, (str, bytes)),
             "Execution facts must be a sequence")
    history = ExecutionFactHistory(snapshot)
    for fact in facts:
        history.append(fact)
    return history.facts
