"""One real-user turn with a frozen prompt snapshot and paired tool history."""

from __future__ import annotations

import copy
import json
from uuid import uuid4
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .contracts import Message, ModelResponse, RunInput, RunResult, RunStatus


class _InvalidBatch(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _InvalidBatch("duplicate argument key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise _InvalidBatch(f"invalid JSON constant: {value}")


def _error(category: str, stage: str) -> dict[str, str]:
    return {"category": category, "stage": stage}


class AgentKernel:
    def run(self, run_input: RunInput) -> RunResult:
        tail: list[Message] = []
        dispatched = False
        corrections = 0
        seen_ids: set[str] = set()

        def result(
            status: RunStatus,
            *,
            output: str | None = None,
            error: dict[str, str] | None = None,
            diagnostics: dict[str, Any] | None = None,
        ) -> RunResult:
            return RunResult(
                status=status,
                output=output,
                history_delta=tuple(copy.deepcopy(tail)),
                error=error,
                diagnostics=diagnostics,
                dispatch_started=dispatched,
            )

        try:
            if not run_input.run_id or run_input.limits.max_model_requests < 1:
                raise ValueError("run_id and positive model request budget are required")
            if not 0 <= run_input.limits.correction_budget <= 1:
                raise ValueError("correction budget must be zero or one")
            if not run_input.correction_message.strip():
                raise ValueError("explicit correction message is required")
            tools = {tool.name: tool for tool in run_input.tools}
            if len(tools) != len(run_input.tools) or "final_answer" not in tools:
                raise ValueError("tool names must be unique and include final_answer")
            Draft202012Validator.check_schema(run_input.output_spec)
            output_validator = Draft202012Validator(copy.deepcopy(run_input.output_spec))
            s0 = tuple(copy.deepcopy(run_input.s0))
            definitions = tuple(copy.deepcopy(tool.definition) for tool in run_input.tools)
            for message in s0:
                if message.get("kind") == "assistant_calls":
                    seen_ids.update(call["id"] for call in message["calls"])
        except (ValueError, KeyError, TypeError, AttributeError, SchemaError) as exc:
            return result("protocol_error", error=_error(type(exc).__name__, "configuration"))

        for _ in range(run_input.limits.max_model_requests):
            if run_input.cancellation is not None and run_input.cancellation.is_set():
                return result("cancelled")

            messages = copy.deepcopy(s0 + tuple(tail))
            request_tools = copy.deepcopy(definitions)
            dispatched = True
            try:
                response = run_input.adapter.generate(messages, request_tools)
            except Exception as exc:
                if run_input.cancellation is not None and run_input.cancellation.is_set():
                    return result("cancelled")
                return result("model_error", error=_error(type(exc).__name__, "model_request"))

            if run_input.cancellation is not None and run_input.cancellation.is_set():
                # A completed batch can still be recorded with unexecuted results.
                if response.finish_reason != "tool_calls":
                    return result("cancelled")

            if response.finish_reason == "tool_calls":
                try:
                    accepted, has_final = self._preflight(response, tools, seen_ids)
                except _InvalidBatch as exc:
                    return result("protocol_error", error=_error(str(exc), "tool_preflight"))
                tail.append(
                    {
                        "kind": "assistant_calls",
                        "id": uuid4().hex,
                        "role": "assistant",
                        "source": "assistant_turn",
                        "calls": accepted,
                        **({"content": response.content} if response.content is not None else {}),
                    }
                )
                seen_ids.update(call["id"] for call in accepted)

                if run_input.cancellation is not None and run_input.cancellation.is_set() and has_final:
                    tail.append(self._tool_result(accepted[0], "final_control", "not_executed", "cancelled before final"))
                    return result("cancelled")

                if has_final:
                    call = accepted[0]
                    answer = call["arguments"]["answer"]
                    try:
                        output_validator.validate(answer)
                    except ValidationError:
                        tail.append(self._tool_result(call, "final_control", "error", "output_spec validation failed"))
                        if corrections < run_input.limits.correction_budget:
                            corrections += 1
                            tail.append(self._feedback(run_input.correction_message))
                            continue
                        return result("protocol_error", error=_error("invalid_final", "output_spec"))
                    output = json.dumps(answer, ensure_ascii=False, indent=2)
                    tail.append(self._tool_result(call, "final_control", "success", output))
                    return result("success", output=output)

                failures = 0
                for call in accepted:
                    if run_input.cancellation is not None and run_input.cancellation.is_set():
                        tail.append(self._tool_result(call, "tool_execution", "not_executed", "cancelled before execution"))
                        continue
                    try:
                        value = tools[call["name"]].execute(call["arguments"])
                        content = str(value)
                    except Exception:
                        failures += 1
                        uncertain = run_input.cancellation is not None and run_input.cancellation.is_set()
                        tail.append(
                            self._tool_result(
                                call,
                                "tool_execution",
                                "uncertain" if uncertain else "error",
                                "tool outcome uncertain" if uncertain else "tool execution failed",
                            )
                        )
                    else:
                        tail.append(self._tool_result(call, "tool_execution", "success", content))
                if run_input.cancellation is not None and run_input.cancellation.is_set():
                    return result("cancelled", diagnostics={"tool_errors": failures})
                continue

            if response.finish_reason == "stop":
                if response.tool_calls or not isinstance(response.content, str) or not response.content:
                    return result("protocol_error", error=_error("incomplete_text", "model_response"))
                tail.append(
                    {
                        "kind": "text",
                        "id": uuid4().hex,
                        "role": "assistant",
                        "source": "assistant_turn",
                        "content": response.content,
                    }
                )
                if corrections < run_input.limits.correction_budget:
                    corrections += 1
                    tail.append(self._feedback(run_input.correction_message))
                    continue
                return result("protocol_error", error=_error("missing_final", "model_response"))

            if response.finish_reason == "length":
                return result("protocol_error", error=_error("truncated_response", "model_response"))
            return result("model_error", error=_error("unexpected_finish_reason", "model_response"))

        return result("steps_exhausted", error=_error("request_budget_exhausted", "model_request"))

    @staticmethod
    def _preflight(
        response: ModelResponse, tools: dict[str, Any], seen_ids: set[str]
    ) -> tuple[list[dict[str, Any]], bool]:
        if not response.tool_calls:
            raise _InvalidBatch("empty tool batch")
        accepted: list[dict[str, Any]] = []
        ids = set(seen_ids)
        for call in response.tool_calls:
            if call.type != "function" or not isinstance(call.id, str) or not call.id:
                raise _InvalidBatch("invalid tool call ID or type")
            if call.id in ids:
                raise _InvalidBatch("duplicate tool call ID")
            ids.add(call.id)
            if not isinstance(call.name, str) or call.name not in tools:
                raise _InvalidBatch("unknown tool")
            if not isinstance(call.raw_arguments, str):
                raise _InvalidBatch("missing raw arguments")
            try:
                arguments = json.loads(
                    call.raw_arguments, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
                )
            except (ValueError, TypeError) as exc:
                raise _InvalidBatch("invalid argument JSON") from exc
            if not isinstance(arguments, dict):
                raise _InvalidBatch("arguments must be an object")
            try:
                tools[call.name].validate(arguments)
            except ValidationError as exc:
                raise _InvalidBatch("arguments do not match tool schema") from exc
            accepted.append(
                {
                    "id": call.id,
                    "name": call.name,
                    "raw_arguments": call.raw_arguments,
                    "arguments": arguments,
                }
            )
        has_final = any(call["name"] == "final_answer" for call in accepted)
        if has_final and len(accepted) != 1:
            raise _InvalidBatch("final_answer must be the only call")
        return accepted, has_final

    @staticmethod
    def _tool_result(call: dict[str, Any], source: str, status: str, content: str) -> Message:
        return {
            "kind": "tool_result",
            "id": uuid4().hex,
            "role": "tool",
            "source": source,
            "tool_call_id": call["id"],
            "name": call["name"],
            "status": status,
            "content": content,
        }

    @staticmethod
    def _feedback(content: str) -> Message:
        return {
            "kind": "text",
            "id": uuid4().hex,
            "role": "user",
            "source": "protocol_feedback",
            "content": content,
        }
