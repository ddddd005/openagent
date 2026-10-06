"""Offline contract tests for the non-streaming snapshot executor."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from phase1_agent.adapter import DeepSeekAdapter
from phase1_agent.contract_graph import validate_message_history, validate_turn_final
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.runtime import CanonicalModelAdapter, KernelContractError, RunFailed, SnapshotKernel
from phase1_agent.tools import ToolExecutionError, final_answer_tool, register_callable


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


def inspect_text(text: str) -> dict:
    return {"length": len(text), "text": text}


SCHEMA = {
    "type": "object", "properties": {"text": {"type": "string", "description": "Input text"}},
    "required": ["text"], "additionalProperties": False,
}


def setup():
    snapshot = json.loads(EXAMPLE.read_text(encoding="utf-8"))["input_snapshot"][0]
    tools = (
        register_callable("inspect_text", "Inspect text", SCHEMA, inspect_text),
        final_answer_tool(),
    )
    snapshot["tool_definitions"] = [
        {
            "name": tool.name, "version": "1", "description": tool.definition["function"]["description"],
            "parameters_schema": copy.deepcopy(tool.schema),
        }
        for tool in tools
    ]
    return snapshot, tools


def call(name, raw, call_id="provider-1"):
    return ModelToolCall(id=call_id, name=name, raw_arguments=raw)


def batch(*calls, content=None):
    return ModelResponse("tool_calls", content=content, tool_calls=calls)


def final(value, call_id="provider-final"):
    return batch(call("final_answer", json.dumps({"answer": value}), call_id))


class ScriptedAdapter:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.inputs = []

    def generate(self, messages, tools):
        self.inputs.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def execute(adapter, *, snapshot=None, tools=None, **kwargs):
    if snapshot is None:
        snapshot, tools = setup()
    return SnapshotKernel(**{key: kwargs.pop(key) for key in ("backoff",) if key in kwargs}).run(
        snapshot, tools, adapter, **kwargs,
    )


def test_inspect_then_final_is_closed_v2_history_with_detached_progress_and_json_result():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"ok"}'), content="Inspecting."),
        final({"text": "done"}),
    )
    progress = []
    result = execute(adapter, snapshot=snapshot, tools=tools, on_progress=progress.append)
    assert (result.model_requests, result.attempts) == (2, 2)
    assert [m["role"] for m in result.messages] == ["assistant", "tool", "assistant", "tool"]
    assert result.messages[1]["blocks"][0]["content"] == {"length": 2, "text": "ok"}
    assert json.loads(result.messages[1]["blocks"][0]["model_visible_text"]) == {"length": 2, "text": "ok"}
    assert result.final == {"message_id": result.messages[2]["message_id"], "value": {"text": "done"}}
    assert progress[-1] == {"model_requests": 2, "attempts": 2, "accepted_messages": 4}
    assert all("blocks" not in event and "text" not in event for event in progress)
    assert adapter.inputs[0][0] == snapshot["s0"]
    assert adapter.inputs[0][1] == [copy.deepcopy(t.definition) for t in tools]
    assert adapter.inputs[1][0] == snapshot["s0"] + result.messages[:2]
    validate_message_history(snapshot["s0"] + result.messages)
    validate_turn_final({
        "schema_version": 1, "turn_id": "00000000-0000-4000-8000-000000000099",
        "parent_turn_id": snapshot["parent_turn_id"],
        "run_id": "00000000-0000-4000-8000-000000000098",
        "snapshot_id": snapshot["snapshot_id"], "input_id": snapshot["input_id"],
        "messages": result.messages, "final": result.final,
        "projection_version": snapshot["projection_version"],
    }, snapshot)
    for message in result.messages:
        validate_record("agent_message", message)
        assert UUID(message["message_id"]).version == 4
        for block in message["blocks"]:
            if block["kind"] == "tool_call":
                assert UUID(block["tool_call_id"]).version == 4
            if block["kind"] == "tool_result":
                assert UUID(block["tool_execution_id"]).version == 4
    assert len({m["source"]["request_id"] for m in result.messages if m["role"] == "assistant"}) == 2
    result.messages[1]["blocks"][0]["content"]["text"] = "changed"
    assert adapter.inputs[1][0][-1]["blocks"][0]["content"]["text"] == "ok"


@pytest.mark.parametrize("change", [
    lambda snapshot, tools: snapshot["tool_definitions"][0]["parameters_schema"].update(required=[]),
    lambda snapshot, tools: snapshot["tool_definitions"].pop(),
    lambda snapshot, tools: snapshot["tool_definitions"].append(copy.deepcopy(snapshot["tool_definitions"][0])),
    lambda snapshot, tools: snapshot["tool_definitions"][0].update(description="other"),
    lambda snapshot, tools: snapshot["s0"][0].update(blocks=[]),
])
def test_bad_frozen_configuration_zero_dispatch(change):
    snapshot, tools = setup()
    change(snapshot, tools)
    adapter = ScriptedAdapter(final({"text": "ok"}))
    with pytest.raises(KernelContractError) as caught:
        execute(adapter, snapshot=snapshot, tools=tools)
    assert caught.value.code == "configuration_error"
    assert caught.value.__cause__ is not None
    assert caught.value.messages == []
    assert (caught.value.model_requests, caught.value.attempts) == (0, 0)
    assert adapter.inputs == []


def test_sdk_retry_setting_is_explicit_and_legacy_default_remains_one():
    snapshot, tools = setup()
    legacy = DeepSeekAdapter(api_key="offline", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(500)
    )))
    try:
        assert legacy.max_retries == 1
        with pytest.raises(ValueError):
            CanonicalModelAdapter(legacy)
        with pytest.raises(RunFailed, match="configuration_error") as caught:
            execute(legacy, snapshot=snapshot, tools=tools)
        assert caught.value.attempts == 0
    finally:
        legacy._client.close()


@pytest.mark.parametrize("responses", [
    (batch(call("inspect_text", '{"text":"x"}'), call("missing", "{}")),),
    (batch(call("inspect_text", '{"text":"x"}', "same"),
           call("inspect_text", '{"text":"y"}', "same")),),
    (batch(call("inspect_text", '{"text":"x","text":"y"}')),),
    (batch(call("inspect_text", '{"text":NaN}')),),
    (batch(call("inspect_text", '{"text":12}')),),
    (batch(call("inspect_text", '{"text":"x"}'), call("final_answer", '{"answer":{}}', "f")),),
    (batch(call("inspect_text", '{"text":"x"}', "repeat")),
     batch(call("inspect_text", '{"text":"y"}', "repeat"))),
])
def test_invalid_model_batches_never_partially_dispatch(responses):
    observed = []
    snapshot, tools = setup()
    tools = (register_callable("inspect_text", "Inspect text", SCHEMA, lambda text: observed.append(text)), tools[1])
    adapter = ScriptedAdapter(*responses)
    with pytest.raises(RunFailed) as caught:
        execute(adapter, snapshot=snapshot, tools=tools)
    assert caught.value.code == "protocol_error"
    assert observed == (["x"] if len(responses) == 2 else [])
    assert caught.value.attempts == len(responses)
    if len(responses) == 1:
        assert caught.value.messages == []
    else:
        assert len(caught.value.messages) == 2
        validate_message_history(caught.value.messages)


def test_tool_error_is_paired_and_model_can_continue_without_diagnostic_leak():
    def implementation(text):
        raise ToolExecutionError("PRIVATE-SECRET")

    snapshot, tools = setup()
    tools = (register_callable("inspect_text", "Inspect text", SCHEMA, implementation), tools[1])
    adapter = ScriptedAdapter(batch(call("inspect_text", '{"text":"x"}')), final({"text": "recovered"}))
    result = execute(adapter, snapshot=snapshot, tools=tools)
    error = result.messages[1]["blocks"][0]
    assert error["status"] == "error" and error["is_error"] is True
    assert "error" in error["content"]
    assert "PRIVATE-SECRET" not in repr(result)
    assert adapter.inputs[1][0][-1]["blocks"][0] == error


@pytest.mark.parametrize("failure", [
    RuntimeError("PRIVATE-SECRET"),
    TypeError("PRIVATE-SECRET"),
    ContractValidationError("PRIVATE-SECRET"),
])
def test_tool_program_fault_is_loud_and_never_fabricates_a_result(failure):
    def implementation(text):
        raise failure

    snapshot, tools = setup()
    broken = register_callable("inspect_text", "Inspect text", SCHEMA, implementation)
    adapter = ScriptedAdapter(batch(call("inspect_text", '{"text":"x"}')), final({"text": "unreachable"}))
    with pytest.raises(KernelContractError) as caught:
        execute(adapter, snapshot=snapshot, tools=(broken, tools[1]))
    assert caught.value.code == "tool_contract_error"
    assert caught.value.__cause__ is failure
    assert len(caught.value.messages) == len(adapter.inputs) == 1
    assert caught.value.messages[0]["role"] == "assistant"
    assert "PRIVATE-SECRET" not in str(caught.value)


def test_non_json_tool_return_is_loud_contract_error_without_a_result():
    snapshot, tools = setup()
    invalid = register_callable("inspect_text", "Inspect text", SCHEMA, lambda text: object())
    adapter = ScriptedAdapter(batch(call("inspect_text", '{"text":"x"}')), final({"text": "unreachable"}))
    with pytest.raises(KernelContractError) as caught:
        execute(adapter, snapshot=snapshot, tools=(invalid, tools[1]))
    assert caught.value.code == "tool_contract_error"
    assert isinstance(caught.value.__cause__, ContractValidationError)
    assert len(caught.value.messages) == len(adapter.inputs) == 1
    assert caught.value.messages[0]["role"] == "assistant"


@pytest.mark.parametrize("answer", [{"bad": 1}, {"text": 17}])
def test_final_output_schema_error_pairs_once_then_corrects(answer):
    adapter = ScriptedAdapter(final(answer, "wrong"), final({"text": "valid"}, "correct"))
    result = execute(adapter)
    assert result.messages[1]["blocks"][0]["status"] == "error"
    assert result.final["value"] == {"text": "valid"}
    assert result.model_requests == 2
    adapter = ScriptedAdapter(final(answer, "wrong"), final(answer, "again"))
    with pytest.raises(RunFailed) as caught:
        execute(adapter)
    assert caught.value.code == "invalid_final"
    assert len(caught.value.messages) == 4
    validate_message_history(caught.value.messages)


def test_invalid_final_tool_parameters_pair_before_single_correction():
    adapter = ScriptedAdapter(batch(call("final_answer", "{}")), final({"text": "valid"}))
    result = execute(adapter)
    assert result.messages[1]["blocks"][0]["status"] == "error"
    assert result.final["value"] == {"text": "valid"}


def test_text_stop_has_only_one_feedback_and_no_text_final():
    adapter = ScriptedAdapter(ModelResponse("stop", content='{"text":"a"}'), final({"text": "b"}))
    result = execute(adapter)
    assert result.messages[1]["source"]["kind"] == "protocol_feedback"
    assert result.final["value"] == {"text": "b"}
    adapter = ScriptedAdapter(ModelResponse("stop", content="a"), ModelResponse("stop", content="b"))
    with pytest.raises(RunFailed) as caught:
        execute(adapter)
    assert caught.value.code == "protocol_error"
    assert [m["source"]["kind"] for m in caught.value.messages] == [
        "model", "protocol_feedback", "model",
    ]
    with pytest.raises(RunFailed, match="protocol_error"):
        execute(ScriptedAdapter(ModelResponse("stop", content="\ud800")))


@pytest.mark.parametrize("failure", [PermissionError("PRIVATE-SECRET"), ValueError("PRIVATE-SECRET")])
def test_adapter_program_fault_is_not_recorded_retried_or_masked(failure):
    adapter = ScriptedAdapter(failure)
    with pytest.raises(KernelContractError) as caught:
        execute(adapter)
    assert caught.value.code == "adapter_contract_error"
    assert caught.value.__cause__ is failure
    assert (caught.value.model_requests, caught.value.attempts) == (1, 1)
    assert caught.value.messages == []
    assert "PRIVATE-SECRET" not in str(caught.value)


def test_retry_keeps_logical_request_identical_and_attempt_budget_is_independent():
    backoffs = []
    adapter = ScriptedAdapter(TimeoutError(), TimeoutError(), final({"text": "yes"}))
    result = execute(adapter, backoff=backoffs.append)
    assert (result.model_requests, result.attempts) == (1, 3)
    assert backoffs == [1, 2]
    assert adapter.inputs[0] == adapter.inputs[1] == adapter.inputs[2]
    adapter = ScriptedAdapter(TimeoutError(), TimeoutError(), final({"text": "unreachable"}))
    with pytest.raises(RunFailed) as caught:
        execute(adapter, max_model_requests=2, max_model_attempts=2)
    assert caught.value.code == "model_attempt_budget_exhausted"
    assert (caught.value.model_requests, caught.value.attempts) == (1, 2)
    assert len(adapter.inputs) == 2


def test_retry_limit_is_four_attempts_and_default_logical_budget_is_eight():
    adapter = ScriptedAdapter(*[TimeoutError() for _ in range(5)])
    with pytest.raises(RunFailed) as caught:
        execute(adapter)
    assert caught.value.code == "model_retry_exhausted"
    assert (caught.value.model_requests, caught.value.attempts) == (1, 4)
    adapter = ScriptedAdapter(*[
        batch(call("inspect_text", '{"text":"x"}', f"call-{n}"))
        for n in range(9)
    ])
    with pytest.raises(RunFailed) as caught:
        execute(adapter)
    assert caught.value.code == "model_request_budget_exhausted"
    assert (caught.value.model_requests, caught.value.attempts) == (8, 8)
    assert len(adapter.inputs) == 8
    assert len(caught.value.messages) == 16
    with pytest.raises(RunFailed, match="configuration_error"):
        execute(ScriptedAdapter(), max_model_requests=65)


def test_retry_only_429_5xx_timeout_connection():
    def status_error(code):
        request = httpx.Request("GET", "https://offline.invalid/")
        response = httpx.Response(code, request=request)
        return httpx.HTTPStatusError("PRIVATE-SECRET", request=request, response=response)

    for status in (429, 500, 503):
        adapter = ScriptedAdapter(status_error(status), final({"text": "yes"}))
        assert execute(adapter).attempts == 2
    for status in (400, 401, 403, 422):
        adapter = ScriptedAdapter(status_error(status))
        with pytest.raises(RunFailed) as caught:
            execute(adapter)
        assert caught.value.attempts == 1
        assert caught.value.code == "model_error"
        assert caught.value.checkpoint is None


def test_failure_messages_are_detached_from_callback_mutations():
    events = []
    adapter = ScriptedAdapter(batch(call("inspect_text", '{"text":"x"}')), PermissionError("SECRET"))
    with pytest.raises(RunFailed) as caught:
        execute(adapter, on_progress=events.append)
    before = copy.deepcopy(caught.value.messages)
    events[0]["accepted_messages"] = 999
    adapter.inputs[1][0][-1]["blocks"][0]["content"]["text"] = "tampered"
    assert caught.value.messages == before


def test_canonical_adapter_projects_error_text_without_masking_or_credentials():
    snapshot, tools = setup()
    captures = []

    def handler(request):
        body = json.loads(request.content)
        captures.append(body)
        if len(captures) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "external", "type": "function",
                "function": {"name": "inspect_text", "arguments": '{"text":"x"}'},
            }]}
            reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "final-external", "type": "function",
                "function": {"name": "final_answer", "arguments": '{"answer":{"text":"done"}}'},
            }]}
            reason = "tool_calls"
        return httpx.Response(200, json={
            "id": "response", "object": "chat.completion", "created": 1, "model": "offline",
            "choices": [{"index": 0, "message": message, "finish_reason": reason}],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    legacy = DeepSeekAdapter(api_key="PRIVATE-KEY", model="offline", max_retries=0, http_client=client)
    wrapped = CanonicalModelAdapter(legacy)
    broken = register_callable("inspect_text", "Inspect text", SCHEMA,
                               lambda text: (_ for _ in ()).throw(ToolExecutionError("PRIVATE-ERROR")))
    try:
        result = execute(wrapped, snapshot=snapshot, tools=(broken, tools[1]))
    finally:
        client.close()
    assert result.final["value"] == {"text": "done"}
    assert captures[0]["tools"] == [copy.deepcopy(t.definition) for t in tools]
    assert captures[1]["messages"][-1]["content"] == result.messages[1]["blocks"][0]["model_visible_text"]
    assert "PRIVATE" not in json.dumps(captures)
    assert captures[1]["messages"][-1]["tool_call_id"] == result.messages[0]["blocks"][0]["tool_call_id"]


def test_canonical_adapter_has_no_sdk_retry_layer():
    bodies = []

    def handler(request):
        bodies.append(request.content)
        if len(bodies) == 1:
            return httpx.Response(503, json={"error": {"message": "temporary"}})
        return httpx.Response(200, json={
            "id": "response", "object": "chat.completion", "created": 1, "model": "offline",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "final", "type": "function", "function": {
                        "name": "final_answer", "arguments": '{"answer":{"text":"ok"}}',
                    },
                }],
            }}],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = CanonicalModelAdapter(DeepSeekAdapter(
        api_key="offline", model="offline", http_client=client, max_retries=0,
    ))
    try:
        result = execute(adapter)
    finally:
        client.close()
    assert (result.model_requests, result.attempts) == (1, 2)
    assert len(bodies) == 2 and bodies[0] == bodies[1]
