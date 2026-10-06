from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from smolagents import Tool

from phase1_agent import tools as tool_registry
from phase1_agent.context import PromptContext
from phase1_agent.contracts import ModelResponse, ModelToolCall, RunInput, RunLimits
from phase1_agent.kernel import AgentKernel


class QueueAdapter:
    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def generate(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelResponse:
        self.requests.append((copy.deepcopy(list(messages)), copy.deepcopy(list(tools))))
        if not self.responses:
            raise AssertionError("Kernel made more model requests than expected")
        return self.responses.pop(0)


class RecordingTool(Tool):
    description = "Record a string value."
    inputs = {"value": {"type": "string", "description": "Value to record."}}
    output_type = "string"

    def __init__(self, name: str, effects: list[str]) -> None:
        self.name = name
        self.effects = effects
        super().__init__()

    def forward(self, value: str) -> str:
        self.effects.append(value)
        return f"recorded:{value}"


def _tool_call(call_id: str, name: str, raw_arguments: str) -> ModelToolCall:
    return ModelToolCall(
        id=call_id,
        name=name,
        raw_arguments=raw_arguments,
    )


def _call_ids_in_history(history: Sequence[dict[str, Any]]) -> list[str]:
    assistant_calls = [
        call
        for message in history
        if message.get("kind") == "assistant_calls"
        for call in message.get("calls", [])
    ]
    return [call["id"] for call in assistant_calls]


def test_kernel_integrates_registered_tool_json_final_and_committed_history() -> None:
    effects: list[str] = []
    registered_tool = tool_registry.register_tool(RecordingTool("g3_record", effects))
    final_tool = tool_registry.final_answer_tool()
    runtime_tools = (registered_tool, final_tool)

    raw_first_arguments = '{ "value" : "first" }'
    raw_second_arguments = '{"value":"second"}'
    final_json = '{"answer":{"answer":"complete","count":2}}'
    adapter = QueueAdapter(
        [
            ModelResponse(
                finish_reason="tool_calls",
                tool_calls=(
                    _tool_call("z", "g3_record", raw_first_arguments),
                    _tool_call("a", "g3_record", raw_second_arguments),
                ),
            ),
            ModelResponse(
                finish_reason="tool_calls",
                tool_calls=(_tool_call("final", "final_answer", final_json),),
            ),
        ]
    )
    context = PromptContext("G3 integration system prompt")
    s0 = context.build("g3-run-1", "Record two values.")
    assert [(message["kind"], message["source"]) for message in s0] == [
        ("text", "fixed_prompt"),
        ("text", "real_user"),
    ]
    original_s0 = copy.deepcopy(s0)

    result = AgentKernel().run(
        RunInput(
            run_id="g3-run-1",
            s0=s0,
            tools=runtime_tools,
            adapter=adapter,
            output_spec={
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["answer", "count"],
                "additionalProperties": False,
            },
            limits=RunLimits(max_model_requests=2),
            correction_message="Call final_answer with a valid JSON object.",
        )
    )

    assert result.status == "success"
    expected_output = {"answer": "complete", "count": 2}
    assert result.output == json.dumps(expected_output, ensure_ascii=False, indent=2)
    assert json.loads(result.output or "") == expected_output
    assert effects == ["first", "second"]
    assert len(adapter.requests) == 2

    first_request_tools = adapter.requests[0][1]
    assert first_request_tools == [tool.definition for tool in runtime_tools]
    assert tuple(adapter.requests[1][0][: len(original_s0)]) == original_s0
    assert s0 == original_s0

    assert _call_ids_in_history(result.history_delta) == ["z", "a", "final"]
    first_call = next(
        call
        for message in result.history_delta
        if message.get("kind") == "assistant_calls"
        for call in message.get("calls", [])
        if call["id"] == "z"
    )
    assert first_call["raw_arguments"] == raw_first_arguments

    tool_reply_ids = [
        message["tool_call_id"]
        for message in result.history_delta
        if message.get("role") == "tool"
    ]
    assert tool_reply_ids[:2] == ["z", "a"]

    context.commit("g3-run-1", result)
    assert tuple(context.history) == (s0[-1], *result.history_delta)
    next_messages = context.build("g3-run-2", "This is the next real user request.")
    assert next_messages[-1] == {
        "kind": "text",
        "role": "user",
        "content": "This is the next real user request.",
        "id": "g3-run-2:user",
        "source": "real_user",
    }
    assert (next_messages[0]["kind"], next_messages[0]["source"]) == ("text", "fixed_prompt")
    assert s0[-1] in next_messages
    assert result.history_delta[0] in next_messages


def test_kernel_preflights_every_call_before_running_any_tool() -> None:
    effects: list[str] = []
    registered_tool = tool_registry.register_tool(RecordingTool("g3_preflight", effects))
    adapter = QueueAdapter(
        [
            ModelResponse(
                finish_reason="tool_calls",
                tool_calls=(
                    _tool_call("valid-first", "g3_preflight", '{"value":"must-not-run"}'),
                    _tool_call("invalid-second", "g3_preflight", '{"unexpected":"missing value"}'),
                ),
            )
        ]
    )
    s0 = PromptContext("G3 preflight system prompt").build("g3-run-invalid", "Try both calls.")

    result = AgentKernel().run(
        RunInput(
            run_id="g3-run-invalid",
            s0=s0,
            tools=(registered_tool, tool_registry.final_answer_tool()),
            adapter=adapter,
            output_spec={"type": "object"},
            limits=RunLimits(max_model_requests=2),
            correction_message="Call final_answer with a valid JSON object.",
        )
    )

    assert result.status == "protocol_error"
    assert effects == []
    assert len(adapter.requests) == 1
