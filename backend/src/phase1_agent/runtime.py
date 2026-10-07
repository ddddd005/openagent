"""Non-streaming execution of a frozen agent input snapshot.

The caller owns run state and persistence. This module returns a detached,
closed message delta and ordered effective-context operations. Native
compaction never rewrites the accepted execution history.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID, uuid4

import httpx
from jsonschema import Draft202012Validator, ValidationError
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError
from referencing.exceptions import Unresolvable

from .adapter import DeepSeekAdapter, ProviderResponseError
from .contract_errors import ContractValidationError, ModelRequestError
from .contract_graph import validate_message_history, validate_pending_message_history
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .contracts import ModelResponse
from .contracts_v2 import validate_record
from .context_compaction import (
    TokenCounter, at_safety_watermark, checkpoint_message, compaction_instruction, compaction_parameters,
    eligible_compaction_ids, measure_context_capacity, policy_config,
    replace_compacted_messages, response_evidence, should_compact,
    validate_compaction_operation, validate_compaction_settings, validate_summary_result,
)
from .context_compaction_policy import effective_summary_prompt
from .prepared_request import check_prepared_request_capacity
from .prompt_errors import PromptProcessingError
from .tools import RegisteredTool, ToolExecutionError, ToolOutcomeUnknown


@dataclass
class KernelResult:
    messages: list[dict[str, Any]]
    final: dict[str, Any]
    model_requests: int
    attempts: int
    effective_messages: list[dict[str, Any]] = field(default_factory=list)
    context_operations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PendingTool:
    name: str
    raw_arguments: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class KernelCheckpoint:
    """Same-process accepted progress, never a durable workflow snapshot."""

    snapshot_id: str
    snapshot_digest: str
    messages: tuple[dict[str, Any], ...]
    pending_tools: tuple[PendingTool, ...]
    model_requests: int
    attempts: int
    final_corrections: int
    text_feedback: int
    seen_provider_ids: frozenset[str]
    integrity_tag: str
    effective_messages: tuple[dict[str, Any], ...] = ()
    context_operations: tuple[dict[str, Any], ...] = ()


class KernelPaused(Exception):
    def __init__(self, checkpoint: KernelCheckpoint):
        super().__init__("kernel paused")
        self.checkpoint = checkpoint


class KernelPauseRequested(Exception):
    """A tool-start callback denied dispatch under the run owner's lock."""


_CHECKPOINT_KEY = secrets.token_bytes(32)


def _checkpoint_tag(checkpoint: KernelCheckpoint) -> str:
    payload = {
        "snapshot_id": checkpoint.snapshot_id,
        "snapshot_digest": checkpoint.snapshot_digest,
        "messages": list(checkpoint.messages),
        "pending_tools": [{
            "name": item.name,
            "raw_arguments": item.raw_arguments,
            "arguments": item.arguments,
            "call_id": item.call_id,
        } for item in checkpoint.pending_tools],
        "model_requests": checkpoint.model_requests,
        "attempts": checkpoint.attempts,
        "final_corrections": checkpoint.final_corrections,
        "text_feedback": checkpoint.text_feedback,
        "seen_provider_ids": sorted(checkpoint.seen_provider_ids),
        "effective_messages": list(checkpoint.effective_messages),
        "context_operations": list(checkpoint.context_operations),
    }
    return hmac.new(_CHECKPOINT_KEY, canonical_bytes(payload), hashlib.sha256).hexdigest()


def verify_checkpoint(checkpoint: KernelCheckpoint, snapshot: dict) -> None:
    """Verify same-process ownership without dispatching any model or tool."""
    try:
        frozen = validate_record("input_snapshot", snapshot)
        seen_provider_ids = {
            block["tool_call_id"] for message in frozen["s0"]
            for block in message["blocks"] if block["kind"] == "tool_call"
        }
        if (type(checkpoint) is not KernelCheckpoint
                or checkpoint.snapshot_id != frozen["snapshot_id"]
                or checkpoint.snapshot_digest != content_digest(frozen)
                or type(checkpoint.model_requests) is not int
                or type(checkpoint.attempts) is not int
                or not 0 <= checkpoint.model_requests <= checkpoint.attempts
                or type(checkpoint.final_corrections) is not int
                or not 0 <= checkpoint.final_corrections <= 1
                or type(checkpoint.text_feedback) is not int
                or not 0 <= checkpoint.text_feedback <= 1
                or type(checkpoint.seen_provider_ids) is not frozenset
                or not seen_provider_ids <= checkpoint.seen_provider_ids
                or type(checkpoint.integrity_tag) is not str
                or not hmac.compare_digest(checkpoint.integrity_tag, _checkpoint_tag(checkpoint))):
            raise ContractValidationError("Invalid kernel checkpoint")
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError) as exc:
        raise ContractValidationError("Invalid kernel checkpoint") from exc


class RunFailed(Exception):
    def __init__(
        self, code: str, messages: list[dict[str, Any]], model_requests: int, attempts: int,
        checkpoint: KernelCheckpoint | None = None,
        recording_failed: bool = False,
    ):
        super().__init__(code)
        self.code = code
        self.messages = copy.deepcopy(messages)
        self.model_requests = model_requests
        self.attempts = attempts
        self.checkpoint = checkpoint
        self.recording_failed = recording_failed


class KernelContractError(RunFailed):
    """A host or component fault, never an ordinary model-visible observation."""


def _id() -> str:
    return str(uuid4())


def _message(role: str, source: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return validate_record("agent_message", {
        "schema_version": 1, "message_id": _id(), "role": role,
        "source": source, "blocks": blocks,
    })


def _result(call_id: str, execution_id: str, status: str, content: Any) -> dict[str, Any]:
    return _message("tool", {"kind": "tool", "tool_execution_id": execution_id}, [{
        "kind": "tool_result", "tool_call_id": call_id, "tool_execution_id": execution_id,
        "status": status, "is_error": status == "error", "content": content,
        "model_visible_text": canonical_bytes(content).decode("utf-8"),
    }])


def _observation(call_id: str, execution_id: str | None, reason_code: str) -> dict[str, Any]:
    content = {
        "reason_code": reason_code,
        "error": (
            "Not executed because an earlier tool outcome in this batch was uncertain."
            if reason_code == "never_started" else
            "The tool was interrupted; its external effects are not confirmed."
            if reason_code == "interrupted" else
            "The tool result and its external effects are not confirmed."
        ),
        "retry_guidance": (
            "Do not infer success, definite failure, or absence of effects from an uncertain result. "
            "Check whether the tool is idempotent and verify external state using available operation "
            "identifiers. If verification is impossible and repeating it could cause duplicate effects, "
            "ask the user. Decide whether to verify, retry, take another action, or stop. "
            "Any further execution must be a new tool call; the old batch is not automatically replayed."
        ),
    }
    return validate_record("agent_message", {
        "schema_version": 2, "message_id": _id(), "role": "tool",
        "source": {
            "kind": "runtime_tool_observation", "tool_call_id": call_id,
            "tool_execution_id": execution_id, "reason_code": reason_code,
        },
        "blocks": [{
            "kind": "tool_result", "tool_call_id": call_id,
            "tool_execution_id": execution_id, "status": "error", "is_error": True,
            "content": content, "model_visible_text": canonical_bytes(content).decode("utf-8"),
        }],
    })


class CanonicalModelAdapter:
    """Project canonical v2 history into a legacy DeepSeekAdapter's internal format.

    Construct the legacy adapter with max_retries=0. Credentials are neither
    inspected nor created here.
    """

    def __init__(self, legacy_adapter: Any):
        if isinstance(legacy_adapter, DeepSeekAdapter) and legacy_adapter.max_retries != 0:
            raise ValueError("CanonicalModelAdapter requires DeepSeekAdapter(max_retries=0)")
        self.legacy_adapter = legacy_adapter

    def close(self) -> None:
        close = getattr(self.legacy_adapter, "close", None)
        if callable(close):
            close()

    def generate(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> ModelResponse:
        return self.legacy_adapter.generate(self._project(messages), copy.deepcopy(list(tools)))

    def generate_compaction(self, messages, tools) -> ModelResponse:
        generate = getattr(self.legacy_adapter, "generate_compaction", self.legacy_adapter.generate)
        return generate(self._project(messages), copy.deepcopy(list(tools)))

    @staticmethod
    def _project(messages):
        legacy: list[dict[str, Any]] = []
        for raw in messages:
            message = validate_record("agent_message", raw)
            blocks = message["blocks"]
            if message["role"] == "tool":
                for block in blocks:
                    # The old adapter masks error statuses. The canonical error
                    # is already encoded in model_visible_text, so forward it intact.
                    legacy.append({
                        "kind": "tool_result", "role": "tool",
                        "tool_call_id": block["tool_call_id"], "status": "success",
                        "content": block["model_visible_text"],
                    })
            elif message["role"] == "assistant" and any(b["kind"] == "tool_call" for b in blocks):
                legacy.append({
                    "kind": "assistant_calls", "role": "assistant",
                    "content": "\n".join(b["text"] for b in blocks if b["kind"] == "text") or None,
                    "calls": [{
                        "id": b["tool_call_id"], "name": b["tool_name"],
                        "raw_arguments": b["raw_arguments"],
                    } for b in blocks if b["kind"] == "tool_call"],
                })
            else:
                legacy.append({
                    "kind": "text", "role": message["role"],
                    "content": "\n".join(b["text"] for b in blocks),
                })
        return legacy


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or 500 <= exc.status_code <= 599
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status <= 599
    if isinstance(exc, APIConnectionError) and isinstance(exc.__cause__, httpx.ProtocolError):
        return False
    return isinstance(exc, (APITimeoutError, APIConnectionError, httpx.TimeoutException,
                            httpx.NetworkError, TimeoutError, ConnectionError))


class SnapshotKernel:
    def __init__(self, *, backoff: Callable[[int], None] | None = None):
        self._backoff = backoff

    @staticmethod
    def validate_checkpoint(checkpoint: KernelCheckpoint, snapshot: dict) -> None:
        verify_checkpoint(checkpoint, snapshot)

    def run(
        self,
        snapshot: dict,
        tools: Sequence[RegisteredTool],
        adapter: Any,
        *,
        max_model_requests: int | None = 8,
        max_model_attempts: int | None = None,
        on_progress: Callable[[dict], None] | None = None,
        checkpoint: KernelCheckpoint | None = None,
        on_boundary: Callable[[str], bool] | None = None,
        on_tool_event: Callable[[str, str, str | None, str | None], None] | None = None,
        on_fact: Callable[[dict[str, Any]], None] | None = None,
        token_counter: TokenCounter | None = None,
    ) -> KernelResult:
        """Run until final_answer or an explicit control/error boundary.

        Passing None for both request limits disables the legacy quotas. The
        counters still record actual work and remain part of pause checkpoints.
        """
        messages: list[dict[str, Any]] = []
        effective_messages: list[dict[str, Any]] = []
        context_operations: list[dict[str, Any]] = []
        compaction_settings = None
        pending_tools: list[PendingTool] = []
        model_requests = 0
        attempts = 0
        final_corrections = 0
        text_feedback = 0
        seen_provider_ids: set[str] = set()
        seen_execution_ids: set[str] = set()
        snapshot_digest = ""
        snapshot_id = ""

        def fact(kind: str, payload: dict[str, Any]) -> None:
            if on_fact is None:
                return
            try:
                on_fact(copy.deepcopy({"kind": kind, "payload": payload}))
            except Exception as exc:
                # Failure to record a dispatch boundary must prevent dispatch,
                # not recursively attempt to write an error into the same store.
                raise KernelContractError(
                    "fact_persistence_error", messages, model_requests, attempts,
                ) from exc

        def note_secondary_failure(cause: BaseException, note: str) -> None:
            add_note = getattr(cause, "add_note", None)
            if callable(add_note):
                add_note(note)
            else:
                cause.__notes__ = [*getattr(cause, "__notes__", ()), note]

        def failure_fact(code: str, category: str, cause: BaseException | None) -> bool:
            if callable(on_fact):
                try:
                    fact("execution_failed", {
                        "code": code, "category": category,
                        "model_requests": model_requests, "attempts": attempts,
                    })
                except KernelContractError as exc:
                    if cause is None:
                        raise
                    note_secondary_failure(
                        cause, f"Execution-failure fact also failed ({exc.code}).",
                    )
                    return False
            return True

        def fail(code: str, cause: BaseException | None = None) -> None:
            category = ("interrupted" if code == "execution_interrupted" else
                        "protocol" if code in {"protocol_error", "invalid_final", "model_response_invalid"}
                        else "model")
            recoverable = code in {
                "protocol_error", "invalid_final",
                "model_request_budget_exhausted", "model_attempt_budget_exhausted",
                "model_retry_exhausted",
            }
            state = None
            if recoverable and not pending_tools:
                closed_history()
                state = make_checkpoint()
            recording_failed = not failure_fact(code, category, cause)
            if recording_failed:
                state = None
            raise RunFailed(
                code, messages, model_requests, attempts, state,
                recording_failed=recording_failed,
            ) from cause

        def contract_fail(code: str, cause: BaseException | None = None) -> None:
            failure_fact(code, "contract", cause)
            raise KernelContractError(code, messages, model_requests, attempts) from cause

        def checked_record(builder: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
            try:
                return builder(*args)
            except ContractValidationError as exc:
                contract_fail("message_contract_error", exc)

        def closed_history() -> None:
            try:
                validate_message_history(frozen["s0"] + messages)
            except ContractValidationError as exc:
                contract_fail("message_contract_error", exc)

        def make_checkpoint() -> KernelCheckpoint:
            state = KernelCheckpoint(
                snapshot_id=snapshot_id,
                snapshot_digest=snapshot_digest,
                messages=tuple(copy.deepcopy(messages)),
                pending_tools=tuple(copy.deepcopy(pending_tools)),
                model_requests=model_requests,
                attempts=attempts,
                final_corrections=final_corrections,
                text_feedback=text_feedback,
                seen_provider_ids=frozenset(seen_provider_ids),
                integrity_tag="",
                effective_messages=tuple(copy.deepcopy(effective_messages)),
                context_operations=tuple(copy.deepcopy(context_operations)),
            )
            return replace(state, integrity_tag=_checkpoint_tag(state))

        def pause() -> None:
            raise KernelPaused(make_checkpoint())

        def boundary(kind: str) -> None:
            if on_boundary is None:
                return
            try:
                allowed = on_boundary(kind)
            except KernelPauseRequested:
                pause()
            except Exception as exc:
                contract_fail("control_error", exc)
            if type(allowed) is not bool:
                contract_fail("control_error")
            if not allowed:
                pause()

        def tool_event(kind: str, call_id: str, execution_id: str | None = None,
                       outcome: str | None = None, message: dict | None = None) -> None:
            if on_tool_event is not None:
                try:
                    on_tool_event(kind, call_id, execution_id, outcome)
                except KernelPauseRequested as exc:
                    if kind == "start":
                        pause()
                    contract_fail("control_error", exc)
                except Exception as exc:
                    contract_fail("control_error", exc)
            if kind == "start":
                fact("tool_dispatch", {
                    "tool_call_id": call_id, "tool_execution_id": execution_id,
                })
            elif kind in {"settle", "skip"}:
                fact("tool_settled", {
                    "tool_call_id": call_id, "tool_execution_id": execution_id,
                    "outcome": outcome, "message": message,
                })

        def execution_identity() -> str:
            identity = _id()
            try:
                if type(identity) is not str:
                    raise ContractValidationError("Tool execution identity must be a canonical UUID v4")
                try:
                    parsed = UUID(identity)
                except (ValueError, AttributeError) as exc:
                    raise ContractValidationError("Tool execution identity must be a canonical UUID v4") from exc
                if parsed.version != 4 or str(parsed) != identity:
                    raise ContractValidationError("Tool execution identity must be a canonical UUID v4")
                if identity in seen_execution_ids:
                    raise ContractValidationError("Duplicate tool execution identity")
            except ContractValidationError as exc:
                contract_fail("message_contract_error", exc)
            seen_execution_ids.add(identity)
            return identity

        def observe_failed_execution(call_id: str, execution_id: str, cause: BaseException) -> None:
            try:
                tool_event("settle", call_id, execution_id, "unknown")
            except KernelContractError as exc:
                # A secondary control fault must not replace the original host failure.
                note_secondary_failure(
                    cause, f"Tool-settle notification also failed ({exc.code}).",
                )

        def backoff(retry: int) -> None:
            if self._backoff is not None:
                self._backoff(retry)
                return
            delay = min(0.25 * 2 ** (retry - 1), 2.0)
            if on_boundary is None:
                time.sleep(delay)
                return
            deadline = time.monotonic() + delay
            while True:
                boundary("before_request")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(remaining, 0.05))

        def accept(message: dict[str, Any]) -> None:
            fact("message_accepted", {"message": message})
            messages.append(message)
            effective_messages.append(copy.deepcopy(message))
            context_operations.append({"kind": "append", "message": copy.deepcopy(message)})
            if on_progress is not None:
                try:
                    on_progress({
                        "model_requests": model_requests,
                        "attempts": attempts,
                        "accepted_messages": len(messages),
                    })
                except Exception as exc:
                    contract_fail("progress_error", exc)

        try:
            if max_model_requests is not None and (
                    type(max_model_requests) is not int or not 1 <= max_model_requests <= 64):
                raise ValueError("Invalid request budget")
            if max_model_attempts is None and max_model_requests is not None:
                max_model_attempts = max_model_requests * 4
            if max_model_attempts is not None and (
                    type(max_model_attempts) is not int or not 1 <= max_model_attempts <= 256):
                raise ValueError("Invalid attempt budget")
            if on_progress is not None and not callable(on_progress):
                raise ValueError("Invalid progress callback")
            if on_boundary is not None and not callable(on_boundary):
                raise ValueError("Invalid boundary callback")
            if on_tool_event is not None and not callable(on_tool_event):
                raise ValueError("Invalid tool event callback")
            if on_fact is not None and not callable(on_fact):
                raise ValueError("Invalid execution fact callback")
            if token_counter is not None and not callable(token_counter):
                raise ValueError("Invalid token counter")
            if isinstance(adapter, DeepSeekAdapter) and adapter.max_retries != 0:
                raise ValueError("DeepSeekAdapter must use max_retries=0")
            frozen = validate_record("input_snapshot", snapshot)
            snapshot_id = frozen["snapshot_id"]
            snapshot_digest = content_digest(frozen)
            validate_message_history(frozen["s0"])
            compaction_settings = validate_compaction_settings(
                frozen["config"]["payload"].get("context_compaction"), frozen["s0"],
            )
            declared_output = frozen["model_parameters"].get("max_tokens")
            if (compaction_settings is not None and type(declared_output) is int
                    and declared_output > compaction_settings["output_reserve_tokens"]):
                raise ValueError("Native capacity reserve is smaller than the frozen model output limit")
            effective_messages = copy.deepcopy(frozen["s0"])
            definitions = frozen["tool_definitions"]
            registered = {tool.name: tool for tool in tools}
            if (len(registered) != len(tools) or len(definitions) != len(tools)
                    or len({item["name"] for item in definitions}) != len(definitions)
                    or "final_answer" not in registered):
                raise ValueError("Invalid tool registry")
            versions: dict[str, str] = {}
            wire_tools = []
            for item in definitions:
                tool = registered[item["name"]]
                definition = tool.definition
                function = definition["function"]
                if (definition["type"] != "function" or function["name"] != item["name"]
                        or canonical_bytes(copy.deepcopy(tool.schema)) != canonical_bytes(item["parameters_schema"])
                        or canonical_bytes(copy.deepcopy(function["parameters"])) != canonical_bytes(item["parameters_schema"])
                        or ("description" in item and item["description"] != function.get("description"))
                        or (getattr(tool, "version", item["version"]) != item["version"])):
                    raise ValueError("Frozen tool differs from registered implementation")
                versions[item["name"]] = item["version"]
                wire_tools.append(copy.deepcopy(definition))
            output_validator = Draft202012Validator(frozen["output_schema"])
            seen_provider_ids = {
                block["tool_call_id"] for message in frozen["s0"]
                for block in message["blocks"] if block["kind"] == "tool_call"
            }
            if checkpoint is not None:
                verify_checkpoint(checkpoint, frozen)
                messages = copy.deepcopy(list(checkpoint.messages))
                pending_tools = copy.deepcopy(list(checkpoint.pending_tools))
                if any(type(item) is not PendingTool for item in pending_tools):
                    raise ValueError("Invalid pending tool checkpoint")
                if pending_tools:
                    first = next((index for index in range(len(messages) - 1, -1, -1)
                                  if messages[index]["role"] == "assistant"), None)
                    if first is None:
                        raise ValueError("Pending tools have no accepted batch")
                    validate_message_history(frozen["s0"] + messages[:first])
                    batch = messages[first]
                    calls = [block for block in batch["blocks"] if block["kind"] == "tool_call"]
                    completed = messages[first + 1:]
                    if (not calls or len(completed) + len(pending_tools) != len(calls)
                            or any(message["role"] != "tool" or len(message["blocks"]) != 1
                                   or message["blocks"][0]["tool_call_id"] != calls[index]["tool_call_id"]
                                   for index, message in enumerate(completed))
                            or any(item.call_id != block["tool_call_id"]
                                   or item.name != block["tool_name"]
                                   or item.raw_arguments != block["raw_arguments"]
                                   or item.arguments != block["parsed_arguments"]
                                   for item, block in zip(pending_tools, calls[len(completed):]))):
                        raise ValueError("Pending batch differs from accepted history")
                    validate_pending_message_history(
                        frozen["s0"] + messages,
                        [item.call_id for item in pending_tools],
                    )
                else:
                    validate_message_history(frozen["s0"] + messages)
                model_requests = checkpoint.model_requests
                attempts = checkpoint.attempts
                final_corrections = checkpoint.final_corrections
                text_feedback = checkpoint.text_feedback
                seen_provider_ids = set(checkpoint.seen_provider_ids)
                effective_messages = (copy.deepcopy(list(checkpoint.effective_messages))
                                      if checkpoint.effective_messages else
                                      copy.deepcopy(frozen["s0"] + messages))
                context_operations = copy.deepcopy(list(checkpoint.context_operations))
                if pending_tools:
                    validate_pending_message_history(
                        effective_messages, [item.call_id for item in pending_tools],
                    )
                else:
                    validate_message_history(effective_messages)
            seen_execution_ids = {
                block["tool_execution_id"] for message in frozen["s0"] + messages
                for block in message["blocks"]
                if block["kind"] == "tool_result" and block["tool_execution_id"] is not None
            }
        except (ContractValidationError, ValueError, KeyError, TypeError, AttributeError,
                IndexError, RecursionError) as exc:
            contract_fail("configuration_error", exc)

        def maintain_context(*, safety_only: bool = False, allow_pause: bool = True) -> None:
            nonlocal effective_messages
            if compaction_settings is None:
                return
            try:
                before_capacity = measure_context_capacity(
                    effective_messages, wire_tools, compaction_settings, token_counter,
                )
                config = policy_config(compaction_settings)
            except (ContractValidationError, ValueError, KeyError, TypeError, AttributeError,
                    IndexError, RecursionError) as exc:
                contract_fail("context_capacity_invalid", exc)
            if config is None or not config["enabled"]:
                if at_safety_watermark(before_capacity):
                    fail("context_capacity_exceeded")
                return
            if safety_only and not at_safety_watermark(before_capacity):
                return
            # A lower trigger is not a compaction target. Reentering the same
            # accepted work view must first continue business, not summarize it
            # again merely because pause did not append another message.
            if (not at_safety_watermark(before_capacity) and context_operations
                    and context_operations[-1]["kind"] == "compact"
                    and context_operations[-1]["after_message_ids"] == [
                        message["message_id"] for message in effective_messages
                    ]):
                return
            if not should_compact(before_capacity, config):
                return
            if pending_tools:
                contract_fail("context_compaction_pending_tools")
            covered = eligible_compaction_ids(
                frozen["s0"], effective_messages, compaction_settings, messages,
            )
            if not covered:
                fail("context_compaction_no_eligible_text")
            compaction_id = _id()
            instruction = compaction_instruction(
                compaction_id, _id(), effective_summary_prompt(compaction_settings["policy"]),
                covered, effective_messages,
            )
            request_messages = copy.deepcopy(effective_messages) + [instruction]
            try:
                check_prepared_request_capacity(frozen, messages=request_messages)
            except PromptProcessingError as exc:
                contract_fail(exc.code, exc)
            except ContractValidationError as exc:
                contract_fail("message_contract_error", exc)
            try:
                auxiliary_settings = {**compaction_settings, "output_reserve_tokens":
                                      compaction_settings.get("summary_max_tokens",
                                                               compaction_settings["output_reserve_tokens"])}
                auxiliary_capacity = measure_context_capacity(
                    request_messages, wire_tools, auxiliary_settings, token_counter)
            except (ContractValidationError, ValueError, TypeError) as exc:
                contract_fail("context_capacity_invalid", exc)
            if auxiliary_capacity["total_tokens"] >= auxiliary_capacity["context_window_tokens"]:
                fail("context_compaction_request_capacity_exceeded")
            if auxiliary_capacity["input_tokens"] > compaction_settings.get(
                "max_cold_input_tokens", auxiliary_capacity["input_tokens"],
            ):
                fail("context_compaction_cold_input_budget_exceeded")
            request = {
                "messages": request_messages, "tools": copy.deepcopy(wire_tools),
                "model_parameters": compaction_parameters(frozen["model_parameters"], compaction_settings),
            }
            fact("context_compaction_started", {
                "compaction_id": compaction_id,
                "before_message_ids": [message["message_id"] for message in effective_messages],
                "covered_message_ids": covered, "request": request,
                "capacity": before_capacity,
            })
            try:
                generate = getattr(adapter, "generate_compaction", adapter.generate)
                response = generate(copy.deepcopy(request_messages), copy.deepcopy(wire_tools))
            except asyncio.CancelledError as exc:
                fact("context_compaction_finished", {
                    "compaction_id": compaction_id, "outcome": "execution_interrupted", "result": None,
                })
                fail("execution_interrupted", exc)
            except ModelRequestError as exc:
                fact("context_compaction_finished", {
                    "compaction_id": compaction_id, "outcome": "model_error", "result": None,
                })
                fail(exc.code, exc)
            except ProviderResponseError as exc:
                fact("context_compaction_finished", {
                    "compaction_id": compaction_id, "outcome": "protocol_error", "result": None,
                })
                fail("context_compaction_summary_invalid", exc)
            except (APIError, httpx.HTTPError, TimeoutError, ConnectionError) as exc:
                fact("context_compaction_finished", {
                    "compaction_id": compaction_id, "outcome": "model_error", "result": None,
                })
                fail("context_compaction_model_error", exc)
            except Exception as exc:
                fact("context_compaction_finished", {
                    "compaction_id": compaction_id, "outcome": "adapter_contract_error", "result": None,
                })
                contract_fail("adapter_contract_error", exc)
            except BaseException:
                fact("context_compaction_finished", {
                    "compaction_id": compaction_id, "outcome": "host_interrupted", "result": None,
                })
                raise
            if not isinstance(response, ModelResponse):
                fact("context_compaction_finished", {
                    "compaction_id": compaction_id, "outcome": "protocol_error", "result": None,
                })
                fail("context_compaction_summary_invalid")
            evidence = None
            try:
                candidate_evidence = response_evidence(response)
                evidence = validate_summary_result(candidate_evidence)
                validate_summary_result(evidence, successful=True)
            except (ContractValidationError, ValueError, TypeError, AttributeError, RecursionError) as exc:
                fact("context_compaction_finished", {
                    "compaction_id": compaction_id, "outcome": "protocol_error", "result": evidence,
                })
                fail("context_compaction_summary_invalid", exc)
            fact("context_compaction_finished", {
                "compaction_id": compaction_id, "outcome": "responded", "result": evidence,
            })
            candidate = checkpoint_message(compaction_id, _id(), evidence["text"])
            after = replace_compacted_messages(effective_messages, covered, candidate)
            try:
                after_capacity = measure_context_capacity(
                    after, wire_tools, compaction_settings, token_counter,
                )
            except (ContractValidationError, ValueError, TypeError) as exc:
                contract_fail("context_capacity_invalid", exc)
            if (after_capacity["input_tokens"] >= before_capacity["input_tokens"]
                    or at_safety_watermark(after_capacity)):
                fail("context_compaction_insufficient")
            operation = validate_compaction_operation({
                "kind": "compact", "compaction_id": compaction_id,
                "before_message_ids": [message["message_id"] for message in effective_messages],
                "covered_message_ids": covered, "checkpoint": candidate,
                "after_message_ids": [message["message_id"] for message in after],
                "request": request, "result": evidence,
                "capacity": {"before": before_capacity, "after": after_capacity},
            })
            # Record the accepted replacement before making it visible locally.
            fact("context_compaction_applied", operation)
            effective_messages = after
            context_operations.append(operation)
            if allow_pause:
                boundary("after_compaction")

        while True:
            while pending_tools:
                boundary("before_tool")
                pending = pending_tools[0]
                name, args, call_id = pending.name, pending.arguments, pending.call_id
                if name == "final_answer":
                    try:
                        registered[name].validate(args)
                        answer = args["answer"]
                        validate_json_value(answer)
                        output_validator.validate(answer)
                    except (Unresolvable, RecursionError) as exc:
                        contract_fail("configuration_error", exc)
                    except (ValidationError, KeyError, ContractValidationError):
                        status, content = "error", {"error": "Invalid final answer."}
                    else:
                        status, content = "success", answer
                    execution_id = execution_identity()
                    result_message = checked_record(_result, call_id, execution_id, status, content)
                    if status == "success":
                        try:
                            validate_message_history(frozen["s0"] + messages + [result_message])
                        except ContractValidationError as exc:
                            contract_fail("message_contract_error", exc)
                        boundary("before_final")
                    tool_event("start", call_id, execution_id)
                    tool_event("settle", call_id, execution_id, status, result_message)
                    accept(result_message)
                    pending_tools.pop(0)
                    if status == "success":
                        maintain_context(safety_only=True, allow_pause=False)
                        return KernelResult(copy.deepcopy(messages), {
                            "message_id": messages[-2]["message_id"], "value": copy.deepcopy(answer),
                        }, model_requests, attempts, copy.deepcopy(effective_messages),
                            copy.deepcopy(context_operations))
                    if final_corrections:
                        fail("invalid_final")
                    final_corrections += 1
                    boundary("after_tool")
                    continue

                execution_id = execution_identity()
                tool_event("start", call_id, execution_id)
                uncertain_reason = None
                try:
                    value = registered[name].execute(copy.deepcopy(args))
                except ToolOutcomeUnknown as exc:
                    uncertain_reason = exc.reason_code
                    if uncertain_reason not in {"outcome_unknown", "interrupted"}:
                        observe_failed_execution(call_id, execution_id, exc)
                        contract_fail("tool_contract_error", exc)
                except ToolExecutionError:
                    status, content = "error", {"error": "Tool execution failed."}
                except asyncio.CancelledError as exc:
                    observe_failed_execution(call_id, execution_id, exc)
                    fail("execution_interrupted", exc)
                except Exception as exc:
                    observe_failed_execution(call_id, execution_id, exc)
                    contract_fail("tool_contract_error", exc)
                except BaseException as exc:
                    # Observe the uncertain effect without swallowing host termination.
                    observe_failed_execution(call_id, execution_id, exc)
                    raise
                else:
                    try:
                        validate_json_value(value)
                    except ContractValidationError as exc:
                        observe_failed_execution(call_id, execution_id, exc)
                        contract_fail("tool_contract_error", exc)
                    else:
                        status, content = "success", value
                if uncertain_reason is not None:
                    observation = checked_record(_observation, call_id, execution_id, uncertain_reason)
                    tool_event("settle", call_id, execution_id, uncertain_reason, observation)
                    accept(observation)
                    pending_tools.pop(0)
                    # Close the accepted batch before yielding to pause or the next request.
                    while pending_tools:
                        skipped = pending_tools[0]
                        observation = checked_record(_observation, skipped.call_id, None, "never_started")
                        tool_event("skip", skipped.call_id, None, "never_started", observation)
                        accept(observation)
                        pending_tools.pop(0)
                    closed_history()
                    boundary("after_tool")
                    continue
                result_message = checked_record(_result, call_id, execution_id, status, content)
                tool_event("settle", call_id, execution_id, status, result_message)
                accept(result_message)
                pending_tools.pop(0)
                boundary("after_tool")

            boundary("before_request")
            closed_history()
            if max_model_requests is not None and model_requests >= max_model_requests:
                fail("model_request_budget_exhausted")
            if max_model_attempts is not None and attempts >= max_model_attempts:
                fail("model_attempt_budget_exhausted")
            maintain_context()
            request_messages = copy.deepcopy(effective_messages)
            try:
                check_prepared_request_capacity(frozen, messages=request_messages)
            except PromptProcessingError as exc:
                contract_fail(exc.code, exc)
            except ContractValidationError as exc:
                contract_fail("message_contract_error", exc)
            model_requests += 1
            request_id = _id()
            request_tools = copy.deepcopy(wire_tools)
            fact("model_request", {
                "request_id": request_id, "request_index": model_requests,
                "messages": request_messages, "tools": request_tools,
                "model_parameters": frozen["model_parameters"],
            })
            for retry in range(4):
                if retry:
                    boundary("before_request")
                if max_model_attempts is not None and attempts >= max_model_attempts:
                    fail("model_attempt_budget_exhausted")
                attempts += 1
                attempt_id = str(uuid4())
                fact("model_attempt_started", {
                    "request_id": request_id, "attempt_id": attempt_id,
                    "request_index": model_requests, "attempt_index": attempts,
                    "retry_index": retry,
                })

                def finish_attempt(
                    outcome: str, response: ModelResponse | None = None,
                ) -> None:
                    fact("model_attempt_finished", {
                        "request_id": request_id, "attempt_id": attempt_id,
                        "outcome": outcome,
                        "usage": response.usage if response is not None else None,
                        "response_id": response.response_id if response is not None else None,
                        "model": response.model if response is not None else None,
                    })

                try:
                    response = adapter.generate(copy.deepcopy(request_messages), copy.deepcopy(request_tools))
                except asyncio.CancelledError as exc:
                    finish_attempt("execution_interrupted")
                    boundary("before_accept")
                    fail("execution_interrupted", exc)
                except ProviderResponseError as exc:
                    finish_attempt("protocol_error")
                    boundary("before_accept")
                    fail("protocol_error", exc)
                except ModelRequestError as exc:
                    # Accepted service facts own retry permission, not this loop.
                    finish_attempt("protocol_error" if exc.code == "model_response_invalid" else "model_error")
                    fail(exc.code, exc)
                except Exception as exc:
                    if not isinstance(exc, (APIError, httpx.HTTPError, TimeoutError, ConnectionError)):
                        finish_attempt("adapter_contract_error")
                        contract_fail("adapter_contract_error", exc)
                    finish_attempt("model_error")
                    if not _retryable(exc):
                        fail("model_error", exc)
                    boundary("before_accept")
                    if retry == 3:
                        fail("model_retry_exhausted", exc)
                    if max_model_attempts is not None and attempts >= max_model_attempts:
                        fail("model_attempt_budget_exhausted")
                    try:
                        backoff(retry + 1)
                    except KernelPaused:
                        raise
                    except Exception as exc:
                        contract_fail("control_error", exc)
                except BaseException as exc:
                    try:
                        finish_attempt("host_interrupted")
                    except KernelContractError as record_error:
                        note_secondary_failure(
                            exc, f"Host-interruption fact also failed ({record_error.code}).",
                        )
                    raise
                else:
                    finish_attempt(
                        "responded" if isinstance(response, ModelResponse) else "protocol_error",
                        response if isinstance(response, ModelResponse) else None,
                    )
                    break

            if not isinstance(response, ModelResponse):
                fail("protocol_error")
            if response.finish_reason == "stop":
                if response.tool_calls or not isinstance(response.content, str) or not response.content:
                    fail("protocol_error")
                try:
                    validate_json_value(response.content)
                except ContractValidationError:
                    fail("protocol_error")
                boundary("before_accept")
                accept(checked_record(_message, "assistant", {"kind": "model", "request_id": request_id},
                                      [{"kind": "text", "text": response.content}]))
                if text_feedback:
                    fail("protocol_error")
                text_feedback += 1
                accept(checked_record(_message, "user", {"kind": "protocol_feedback", "request_id": request_id},
                                      [{"kind": "text", "text": "Call final_answer with a valid JSON answer."}]))
                continue
            if response.finish_reason != "tool_calls" or not response.tool_calls:
                fail("protocol_error")

            accepted: list[tuple[str, str, dict[str, Any], str]] = []
            batch_ids: set[str] = set()
            try:
                for call in response.tool_calls:
                    if (call.type != "function" or type(call.id) is not str or not call.id
                            or call.id in seen_provider_ids or call.id in batch_ids
                            or type(call.name) is not str or call.name not in registered):
                        raise ValueError("Invalid call identity or tool")
                    batch_ids.add(call.id)
                    args = loads_strict(call.raw_arguments)
                    if type(args) is not dict:
                        raise ValueError("Tool arguments must be an object")
                    if call.name != "final_answer":
                        registered[call.name].validate(args)
                    accepted.append((call.name, call.raw_arguments, args, _id()))
                if any(name == "final_answer" for name, _, _, _ in accepted) and len(accepted) != 1:
                    raise ValueError("Final must be exclusive")
                if response.content is not None:
                    if type(response.content) is not str:
                        raise ValueError("Invalid assistant content")
                    validate_json_value(response.content)
            except (Unresolvable, RecursionError) as exc:
                contract_fail("configuration_error", exc)
            except (ContractValidationError, ValidationError, ValueError, TypeError, AttributeError):
                fail("protocol_error")

            blocks = ([{"kind": "text", "text": response.content}]
                      if isinstance(response.content, str) and response.content else [])
            blocks.extend({
                "kind": "tool_call", "tool_call_id": internal_id, "tool_name": name,
                "tool_definition_version": versions[name], "raw_arguments": raw,
                "parsed_arguments": args,
            } for name, raw, args, internal_id in accepted)
            assistant = checked_record(_message, "assistant", {"kind": "model", "request_id": request_id}, blocks)
            boundary("before_accept")
            try:
                validate_pending_message_history(
                    frozen["s0"] + messages + [assistant],
                    [internal_id for _, _, _, internal_id in accepted],
                )
            except ContractValidationError as exc:
                contract_fail("message_contract_error", exc)
            accept(assistant)
            seen_provider_ids.update(batch_ids)
            pending_tools = [PendingTool(name, raw, copy.deepcopy(args), call_id)
                             for name, raw, args, call_id in accepted]
            for pending in pending_tools:
                tool_event("queue", pending.call_id)
