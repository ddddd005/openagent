"""Offline uncertain-tool observations and fail-loud host contract boundaries."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

import phase1_agent.runtime as runtime
from phase1_agent.adapter import DeepSeekAdapter, ProviderResponseError
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_message_history, validate_turn_final
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.runtime import (
    CanonicalModelAdapter,
    KernelContractError,
    KernelPauseRequested,
    KernelPaused,
    RunFailed,
    SnapshotKernel,
)
from phase1_agent.tools import ToolOutcomeUnknown, final_answer_tool, register_callable


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "Input text"}},
    "required": ["text"],
    "additionalProperties": False,
}


def setup(implementation):
    snapshot = json.loads(EXAMPLE.read_text(encoding="utf-8"))["input_snapshot"][0]
    tools = (
        register_callable("inspect_text", "Inspect text", SCHEMA, implementation),
        final_answer_tool(),
    )
    snapshot["tool_definitions"] = [{
        "name": tool.name,
        "version": "1",
        "description": tool.definition["function"]["description"],
        "parameters_schema": copy.deepcopy(tool.schema),
    } for tool in tools]
    return snapshot, tools


def batch(*texts):
    return ModelResponse("tool_calls", tool_calls=tuple(
        ModelToolCall(id=f"provider-{text}", name="inspect_text", raw_arguments=json.dumps({"text": text}))
        for text in texts
    ))


def final(text="done"):
    return ModelResponse("tool_calls", tool_calls=(
        ModelToolCall(id="provider-final", name="final_answer", raw_arguments=json.dumps({"answer": {"text": text}})),
    ))


class ScriptedAdapter:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def generate(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def assert_observation(message, call_id, reason_code, execution_id):
    assert message["schema_version"] == 2
    assert message["role"] == "tool"
    assert message["source"] == {
        "kind": "runtime_tool_observation",
        "tool_call_id": call_id,
        "tool_execution_id": execution_id,
        "reason_code": reason_code,
    }
    block = message["blocks"][0]
    assert (block["tool_call_id"], block["tool_execution_id"]) == (call_id, execution_id)
    assert (block["status"], block["is_error"]) == ("error", True)
    assert block["content"]["reason_code"] == reason_code
    assert block["content"]["error"]
    assert json.loads(block["model_visible_text"]) == block["content"]
    guidance = block["content"]["retry_guidance"].lower()
    assert all(term in guidance for term in ("idempotent", "external state", "user", "new tool call"))
    assert "PRIVATE" not in repr(message)
    validate_record("agent_message", message)


@pytest.mark.parametrize("reason_code", ["outcome_unknown", "interrupted"])
def test_unknown_closes_batch_in_order_and_skips_all_remaining_dispatches(reason_code):
    executed, events = [], []

    def implementation(text):
        executed.append(text)
        if text == "uncertain":
            raise ToolOutcomeUnknown("PRIVATE-TOOL-SECRET", reason_code=reason_code)
        return {"text": text}

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(batch("settled", "uncertain", "skip-one", "skip-two"), final())
    result = SnapshotKernel().run(
        snapshot, tools, adapter,
        on_tool_event=lambda *event: events.append(event),
    )
    calls = [block["tool_call_id"] for block in result.messages[0]["blocks"]]
    assert executed == ["settled", "uncertain"]
    assert (result.model_requests, result.attempts) == (2, 2)
    assert [message["blocks"][0]["tool_call_id"] for message in result.messages[1:5]] == calls
    assert result.messages[1]["schema_version"] == 1
    assert result.messages[1]["blocks"][0]["content"] == {"text": "settled"}
    uncertain_execution = result.messages[2]["blocks"][0]["tool_execution_id"]
    assert UUID(uncertain_execution).version == 4
    assert_observation(result.messages[2], calls[1], reason_code, uncertain_execution)
    for index in (2, 3):
        assert_observation(result.messages[index + 1], calls[index], "never_started", None)
    assert adapter.requests[1] == snapshot["s0"] + result.messages[:5]
    assert [event[1] for event in events if event[0] == "start"] == [
        calls[0], calls[1], result.messages[5]["blocks"][0]["tool_call_id"],
    ]
    assert [event for event in events if event[0] == "skip"] == [
        ("skip", calls[2], None, "never_started"),
        ("skip", calls[3], None, "never_started"),
    ]
    validate_message_history(snapshot["s0"] + result.messages)
    validate_turn_final({
        "schema_version": 1,
        "turn_id": "00000000-0000-4000-8000-000000000099",
        "parent_turn_id": snapshot["parent_turn_id"],
        "run_id": "00000000-0000-4000-8000-000000000098",
        "snapshot_id": snapshot["snapshot_id"],
        "input_id": snapshot["input_id"],
        "messages": result.messages,
        "final": result.final,
        "projection_version": snapshot["projection_version"],
    }, snapshot)


def test_model_can_choose_a_new_verification_call_without_replaying_old_calls_or_budgets():
    executed = []

    def implementation(text):
        executed.append(text)
        if text == "uncertain":
            raise ToolOutcomeUnknown("PRIVATE")
        return {"text": text, "verified": True}

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(
        batch("uncertain", "skipped"),
        batch("verify-external-state"),
        final("verified"),
    )
    result = SnapshotKernel().run(snapshot, tools, adapter)
    old_calls = [block["tool_call_id"] for block in result.messages[0]["blocks"]]
    new_call = result.messages[3]["blocks"][0]["tool_call_id"]
    assert new_call not in old_calls
    assert executed == ["uncertain", "verify-external-state"]
    assert (result.model_requests, result.attempts) == (3, 3)
    assert len(adapter.requests) == 3
    assert result.final["value"] == {"text": "verified"}
    assert adapter.requests[1] == snapshot["s0"] + result.messages[:3]
    assert adapter.requests[2] == snapshot["s0"] + result.messages[:5]
    validate_message_history(snapshot["s0"] + result.messages)


def test_unknown_and_pause_close_entire_batch_before_checkpoint_and_do_not_resume_skipped_calls():
    executed = []
    interrupt = False

    def implementation(text):
        nonlocal interrupt
        executed.append(text)
        interrupt = True
        raise ToolOutcomeUnknown("PRIVATE")

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(batch("uncertain", "skip-one", "skip-two"), final())
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_boundary=lambda kind: not interrupt,
        )
    checkpoint = caught.value.checkpoint
    assert checkpoint.pending_tools == ()
    assert len(checkpoint.messages) == 4
    assert (checkpoint.model_requests, checkpoint.attempts) == (1, 1)
    assert executed == ["uncertain"]
    validate_message_history(snapshot["s0"] + list(checkpoint.messages))

    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert executed == ["uncertain"]
    assert result.messages[:4] == list(checkpoint.messages)
    assert adapter.requests[1] == snapshot["s0"] + list(checkpoint.messages)
    assert (result.model_requests, result.attempts) == (2, 2)
    assert result.final["value"] == {"text": "done"}


@pytest.mark.parametrize("pause_first", [False, True])
def test_unknown_preserves_closed_observations_at_budget_exhaustion_including_resume(pause_first):
    executed = []

    def implementation(text):
        executed.append(text)
        raise ToolOutcomeUnknown("PRIVATE")

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(batch("uncertain", "skipped"), final("unreachable"))
    checkpoint = None
    if pause_first:
        with pytest.raises(KernelPaused) as paused:
            SnapshotKernel().run(
                snapshot, tools, adapter, max_model_requests=1,
                on_boundary=lambda kind: kind != "after_tool",
            )
        checkpoint = paused.value.checkpoint
        assert checkpoint.pending_tools == ()
    with pytest.raises(RunFailed, match="model_request_budget_exhausted") as caught:
        SnapshotKernel().run(snapshot, tools, adapter, max_model_requests=1, checkpoint=checkpoint)
    assert executed == ["uncertain"]
    assert len(adapter.requests) == 1
    assert len(caught.value.messages) == 3
    assert (caught.value.model_requests, caught.value.attempts) == (1, 1)
    assert [message["source"]["reason_code"] for message in caught.value.messages[1:]] == [
        "outcome_unknown", "never_started",
    ]
    validate_message_history(snapshot["s0"] + caught.value.messages)


def test_canonical_adapter_forwards_every_observation_and_retry_guidance_to_provider_wire():
    executed, captures = [], []

    def implementation(text):
        executed.append(text)
        if text == "uncertain":
            raise ToolOutcomeUnknown("PRIVATE-TOOL-SECRET")
        return {"text": text}

    def handler(request):
        body = json.loads(request.content)
        captures.append(body)
        response = batch("settled", "uncertain", "skipped") if len(captures) == 1 else final()
        return httpx.Response(200, json={
            "id": "offline-response", "object": "chat.completion", "created": 1, "model": "offline",
            "choices": [{
                "index": 0, "finish_reason": response.finish_reason,
                "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": call.id, "type": "function",
                        "function": {"name": call.name, "arguments": call.raw_arguments},
                    } for call in response.tool_calls],
                },
            }],
        })

    snapshot, tools = setup(implementation)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = CanonicalModelAdapter(DeepSeekAdapter(
            api_key="PRIVATE-API-KEY", model="offline", max_retries=0, http_client=client,
        ))
        result = SnapshotKernel().run(snapshot, tools, adapter)
    calls = [block["tool_call_id"] for block in result.messages[0]["blocks"]]
    assert executed == ["settled", "uncertain"]
    assert len(captures) == 2
    second_wire = captures[1]["messages"][-4:]
    assert second_wire[0]["role"] == "assistant"
    assert [call["id"] for call in second_wire[0]["tool_calls"]] == calls
    assert [message["tool_call_id"] for message in second_wire[1:]] == calls
    assert [message["content"] for message in second_wire[1:]] == [
        message["blocks"][0]["model_visible_text"] for message in result.messages[1:4]
    ]
    assert json.loads(second_wire[2]["content"])["reason_code"] == "outcome_unknown"
    assert json.loads(second_wire[3]["content"])["reason_code"] == "never_started"
    assert "idempotent" in second_wire[2]["content"]
    assert "PRIVATE" not in json.dumps(captures)


@pytest.mark.parametrize("callback_name, code", [
    ("on_progress", "progress_error"),
    ("on_boundary", "control_error"),
    ("on_tool_event", "control_error"),
])
def test_callback_program_fault_preserves_cause_and_never_fabricates_a_tool_result(callback_name, code):
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    adapter = ScriptedAdapter(batch("unreachable"), final())
    failure = RuntimeError("PRIVATE-CALLBACK-ERROR")

    def broken(*args):
        raise failure

    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, **{callback_name: broken})
    assert caught.value.code == code
    assert caught.value.__cause__ is failure
    assert executed == []
    assert len(adapter.requests) <= 1
    assert all(message["role"] == "assistant" for message in caught.value.messages)


def test_invalid_internal_identity_is_a_loud_host_contract_error_without_dispatch(monkeypatch):
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    adapter = ScriptedAdapter(batch("unreachable"), final())
    monkeypatch.setattr(runtime, "_id", lambda: "invalid-internal-identity")
    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter)
    assert caught.value.code == "message_contract_error"
    assert isinstance(caught.value.__cause__, ContractValidationError)
    assert caught.value.messages == []
    assert executed == []
    assert len(adapter.requests) <= 1


def test_duplicate_internal_call_identity_cannot_partially_dispatch_accepted_batch(monkeypatch):
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    adapter = ScriptedAdapter(batch("one", "two"), final())
    identities = iter([
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000003",
    ])
    monkeypatch.setattr(runtime, "_id", lambda: next(identities))
    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter)
    assert caught.value.code == "message_contract_error"
    assert isinstance(caught.value.__cause__, ContractValidationError)
    assert caught.value.messages == []
    assert executed == []
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("failure", [KeyboardInterrupt("PRIVATE"), SystemExit("PRIVATE")])
def test_tool_host_termination_propagates_original_exception_and_stops_dispatch(failure):
    executed = []

    def implementation(text):
        executed.append(text)
        raise failure

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(batch("terminate", "skipped"), final())
    with pytest.raises(type(failure)) as caught:
        SnapshotKernel().run(snapshot, tools, adapter)
    assert caught.value is failure
    assert executed == ["terminate"]
    assert len(adapter.requests) == 1


def test_model_host_cancellation_without_pause_is_interruption_not_normal_unknown():
    snapshot, tools = setup(lambda text: {"text": text})
    failure = asyncio.CancelledError()
    adapter = ScriptedAdapter(failure, final())
    with pytest.raises(RunFailed, match="execution_interrupted") as caught:
        SnapshotKernel().run(snapshot, tools, adapter)
    assert not isinstance(caught.value, KernelContractError)
    assert caught.value.__cause__ is failure
    assert caught.value.messages == []
    assert (caught.value.model_requests, caught.value.attempts) == (1, 1)
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("fault_kind", ["invalid", "historical_duplicate", "batch_duplicate"])
def test_execution_identity_fault_is_rejected_before_tool_side_effect_without_event_callback(
    monkeypatch, fault_kind,
):
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    duplicate_id = str(uuid4())
    if fault_kind == "historical_duplicate":
        call_id = str(uuid4())
        snapshot["s0"].extend([
            {
                "schema_version": 1, "message_id": str(uuid4()), "role": "assistant",
                "source": {"kind": "model", "request_id": str(uuid4())},
                "blocks": [{
                    "kind": "tool_call", "tool_call_id": call_id, "tool_name": "inspect_text",
                    "tool_definition_version": "1", "raw_arguments": '{"text":"history"}',
                    "parsed_arguments": {"text": "history"},
                }],
            },
            {
                "schema_version": 1, "message_id": str(uuid4()), "role": "tool",
                "source": {"kind": "tool", "tool_execution_id": duplicate_id},
                "blocks": [{
                    "kind": "tool_result", "tool_call_id": call_id,
                    "tool_execution_id": duplicate_id, "status": "success", "is_error": False,
                    "content": {"text": "history"}, "model_visible_text": '{"text":"history"}',
                }],
            },
        ])
    validate_message_history(snapshot["s0"])
    adapter = ScriptedAdapter(
        batch("first", "unreachable") if fault_kind == "batch_duplicate" else batch("unreachable"),
        final(),
    )
    original_id = runtime._id
    next_identity = None

    def controlled_id():
        nonlocal next_identity
        if next_identity is not None:
            identity, next_identity = next_identity, None
            return identity
        return original_id()

    def boundary(kind):
        nonlocal next_identity
        if kind == "before_tool":
            next_identity = "invalid-execution-id" if fault_kind == "invalid" else duplicate_id
        return True

    monkeypatch.setattr(runtime, "_id", controlled_id)
    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_boundary=boundary)
    assert caught.value.code == "message_contract_error"
    assert isinstance(caught.value.__cause__, ContractValidationError)
    assert executed == (["first"] if fault_kind == "batch_duplicate" else [])
    assert len(adapter.requests) == 1
    assert len(caught.value.messages) == (2 if fault_kind == "batch_duplicate" else 1)


@pytest.mark.parametrize("failure", [KeyboardInterrupt("PRIVATE"), SystemExit("PRIVATE")])
@pytest.mark.parametrize("callback_failure", [
    RuntimeError("PRIVATE-SETTLE-ERROR"), KernelPauseRequested(),
])
def test_host_termination_is_not_overwritten_by_a_failed_settle_callback(failure, callback_failure):
    executed, events = [], []

    def implementation(text):
        executed.append(text)
        raise failure

    def tool_event(*event):
        events.append(event)
        if event[0] == "settle":
            raise callback_failure

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(batch("terminate", "unreachable"), final())
    with pytest.raises(type(failure)) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_tool_event=tool_event)
    assert caught.value is failure
    assert executed == ["terminate"]
    assert len(adapter.requests) == 1
    assert [event[0] for event in events] == ["queue", "queue", "start", "settle"]
    notes = "\n".join(getattr(failure, "__notes__", ()))
    assert "control_error" in notes
    assert "PRIVATE" not in notes


@pytest.mark.parametrize("failure, code", [
    (RuntimeError("PRIVATE-TOOL-ERROR"), "tool_contract_error"),
    (TypeError("PRIVATE-TOOL-ERROR"), "tool_contract_error"),
    (ContractValidationError("PRIVATE-TOOL-ERROR"), "tool_contract_error"),
    (asyncio.CancelledError(), "execution_interrupted"),
])
@pytest.mark.parametrize("callback_failure", [
    RuntimeError("PRIVATE-SETTLE-ERROR"), KernelPauseRequested(),
])
def test_tool_fault_original_cause_survives_a_failed_settle_callback(failure, code, callback_failure):
    executed, events = [], []

    def implementation(text):
        executed.append(text)
        raise failure

    def tool_event(*event):
        events.append(event)
        if event[0] == "settle":
            raise callback_failure

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(batch("failed", "unreachable"), final())
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_tool_event=tool_event)
    assert caught.value.code == code
    assert caught.value.__cause__ is failure
    assert isinstance(caught.value, KernelContractError) == (code == "tool_contract_error")
    assert executed == ["failed"]
    assert len(adapter.requests) == 1
    assert len(caught.value.messages) == 1
    assert caught.value.messages[0]["role"] == "assistant"
    assert [event[0] for event in events] == ["queue", "queue", "start", "settle"]
    notes = "\n".join(getattr(failure, "__notes__", ()))
    assert "control_error" in notes
    assert "PRIVATE" not in notes


def test_secondary_settle_failure_preserves_cause_when_exception_note_api_is_unavailable():
    class LegacyToolError(RuntimeError):
        add_note = None

    failure = LegacyToolError("PRIVATE-PRIMARY-ERROR")

    def implementation(text):
        raise failure

    def tool_event(*event):
        if event[0] == "settle":
            raise RuntimeError("PRIVATE-SECONDARY-ERROR")

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(batch("failed", "unreachable"), final())
    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_tool_event=tool_event)
    assert caught.value.code == "tool_contract_error"
    assert caught.value.__cause__ is failure
    assert len(adapter.requests) == 1
    assert len(caught.value.messages) == 1
    notes = "\n".join(getattr(failure, "__notes__", ()))
    assert "control_error" in notes
    assert "PRIVATE" not in notes


def test_recursive_output_schema_is_configuration_fault_not_model_correction():
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    snapshot["output_schema"] = {"$ref": "#"}
    adapter = ScriptedAdapter(final(), final("unreachable correction"))
    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter)
    assert caught.value.code == "configuration_error"
    assert isinstance(caught.value.__cause__, RecursionError)
    assert executed == []
    assert len(adapter.requests) == 1
    assert (caught.value.model_requests, caught.value.attempts) == (1, 1)
    assert len(caught.value.messages) == 1
    assert caught.value.messages[0]["role"] == "assistant"


def test_provider_response_error_is_protocol_failure_not_program_fault_or_retry():
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    failure = ProviderResponseError("PRIVATE-PROVIDER-DIAGNOSTIC")
    adapter = ScriptedAdapter(failure, batch("unreachable"), final())
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, adapter)
    assert caught.value.code == "protocol_error"
    assert not isinstance(caught.value, KernelContractError)
    assert caught.value.__cause__ is failure
    assert caught.value.messages == []
    assert (caught.value.model_requests, caught.value.attempts) == (1, 1)
    assert executed == []
    assert len(adapter.requests) == 1
    assert "PRIVATE" not in str(caught.value)


def test_http_200_empty_choices_flows_through_canonical_adapter_as_protocol_error_once():
    executed, captures = [], []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})

    def handler(request):
        captures.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "offline-response", "object": "chat.completion",
            "created": 1, "model": "offline", "choices": [],
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = CanonicalModelAdapter(DeepSeekAdapter(
            api_key="PRIVATE-API-KEY", model="offline", max_retries=0, http_client=client,
        ))
        with pytest.raises(RunFailed) as caught:
            SnapshotKernel().run(snapshot, tools, adapter)
    assert caught.value.code == "protocol_error"
    assert not isinstance(caught.value, KernelContractError)
    assert isinstance(caught.value.__cause__, ProviderResponseError)
    assert caught.value.messages == []
    assert (caught.value.model_requests, caught.value.attempts) == (1, 1)
    assert executed == []
    assert len(captures) == 1
    assert "PRIVATE" not in json.dumps(captures)
