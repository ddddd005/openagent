"""Deterministic same-process pause boundaries for the snapshot executor."""

from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.runtime import (
    KernelContractError,
    KernelPaused,
    KernelPauseRequested,
    RunFailed,
    SnapshotKernel,
)
from phase1_agent.tools import ToolExecutionError, final_answer_tool, register_callable


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "Input text"}},
    "required": ["text"],
    "additionalProperties": False,
}


def setup(implementation=None):
    snapshot = json.loads(EXAMPLE.read_text(encoding="utf-8"))["input_snapshot"][0]
    inspect = register_callable(
        "inspect_text", "Inspect text", SCHEMA,
        implementation or (lambda text: {"text": text}),
    )
    tools = (inspect, final_answer_tool())
    snapshot["tool_definitions"] = [{
        "name": tool.name,
        "version": "1",
        "description": tool.definition["function"]["description"],
        "parameters_schema": copy.deepcopy(tool.schema),
    } for tool in tools]
    return snapshot, tools


def call(name, raw, identity):
    return ModelToolCall(id=identity, name=name, raw_arguments=raw)


def batch(*calls):
    return ModelResponse("tool_calls", tool_calls=calls)


def final(text):
    return batch(call("final_answer", json.dumps({"answer": {"text": text}}), "provider-final"))


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


def test_pause_before_first_dispatch_preserves_zero_budget_and_resume_uses_first_request():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(final("done"))
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_boundary=lambda kind: False)
    checkpoint = caught.value.checkpoint
    assert (checkpoint.model_requests, checkpoint.attempts) == (0, 0)
    assert checkpoint.messages == checkpoint.pending_tools == ()
    assert adapter.requests == []

    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert (result.model_requests, result.attempts) == (1, 1)


def test_interrupt_while_nonstreaming_model_is_in_flight_discards_unaccepted_batch():
    started, release = Event(), Event()
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})

    class BlockingAdapter(ScriptedAdapter):
        def generate(self, messages, definitions):
            if not self.requests:
                started.set()
                assert release.wait(10)
            return super().generate(messages, definitions)

    adapter = BlockingAdapter(
        batch(call("inspect_text", '{"text":"discard"}', "provider-old")),
        final("new"),
    )
    interrupt = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            SnapshotKernel().run, snapshot, tools, adapter,
            on_boundary=lambda kind: not interrupt.is_set(),
        )
        assert started.wait(10)
        interrupt.set()
        release.set()
        with pytest.raises(KernelPaused) as caught:
            future.result(timeout=10)
    checkpoint = caught.value.checkpoint
    assert (checkpoint.model_requests, checkpoint.attempts) == (1, 1)
    assert checkpoint.messages == checkpoint.pending_tools == ()
    assert executed == []

    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert (result.model_requests, result.attempts) == (2, 2)
    assert result.final["value"] == {"text": "new"}
    assert executed == []


@pytest.mark.parametrize("failure", [TimeoutError("private"), asyncio.CancelledError()])
def test_interrupted_model_transport_or_cancellation_is_pause_not_model_failure(failure):
    snapshot, tools = setup()
    adapter = ScriptedAdapter(failure, final("resumed"))
    interrupt = Event()

    def boundary(kind):
        if kind == "before_request":
            return True
        interrupt.set()
        return False

    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_boundary=boundary)
    assert interrupt.is_set()
    assert (caught.value.checkpoint.model_requests, caught.value.checkpoint.attempts) == (1, 1)
    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=caught.value.checkpoint)
    assert result.final["value"] == {"text": "resumed"}
    assert (result.model_requests, result.attempts) == (2, 2)


def test_adapter_program_fault_is_not_masked_by_a_concurrent_pause():
    snapshot, tools = setup()
    failure = ValueError("PRIVATE-ADAPTER-ERROR")
    adapter = ScriptedAdapter(failure, final("must not dispatch"))
    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_boundary=lambda kind: kind == "before_request",
        )
    assert caught.value.code == "adapter_contract_error"
    assert caught.value.__cause__ is failure
    assert len(adapter.requests) == 1
    assert caught.value.messages == []
    assert (caught.value.model_requests, caught.value.attempts) == (1, 1)


def test_accepted_multi_tool_batch_resumes_pending_in_order_without_reexecuting_settled_tool():
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    adapter = ScriptedAdapter(batch(
        call("inspect_text", '{"text":"one"}', "provider-one"),
        call("inspect_text", '{"text":"two"}', "provider-two"),
    ), final("done"))
    events = []
    tool_checks = 0

    def boundary(kind):
        nonlocal tool_checks
        if kind == "before_tool":
            tool_checks += 1
            return tool_checks == 1
        return True

    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter, on_boundary=boundary,
            on_tool_event=lambda *event: events.append(event),
        )
    checkpoint = caught.value.checkpoint
    first_call, second_call = [
        block["tool_call_id"] for block in checkpoint.messages[0]["blocks"]
        if block["kind"] == "tool_call"
    ]
    assert executed == ["one"]
    assert len(checkpoint.messages) == 2
    assert len(checkpoint.pending_tools) == 1
    assert checkpoint.pending_tools[0].call_id == second_call
    assert checkpoint.pending_tools[0].arguments == {"text": "two"}
    assert checkpoint.seen_provider_ids >= {"provider-one", "provider-two"}
    assert [event[0] for event in events] == ["queue", "queue", "start", "settle"]
    assert [event[1] for event in events] == [first_call, second_call, first_call, first_call]
    assert events[2][2] == events[3][2]
    assert events[3][3] == "success"

    result = SnapshotKernel().run(
        snapshot, tools, adapter, checkpoint=checkpoint,
        on_tool_event=lambda *event: events.append(event),
    )
    assert executed == ["one", "two"]
    assert (result.model_requests, result.attempts) == (2, 2)
    assert adapter.requests[1][-1]["blocks"][0]["tool_call_id"] == second_call
    assert [message["blocks"][0]["tool_call_id"] for message in result.messages[1:3]] == [
        first_call, second_call,
    ]
    assert [event[0] for event in events if event[1] == first_call] == [
        "queue", "start", "settle",
    ]
    assert [event[0] for event in events if event[1] == second_call] == [
        "queue", "start", "settle",
    ]


def test_pause_during_started_tool_waits_for_settle_before_returning_checkpoint():
    started, release, interrupt = Event(), Event(), Event()
    executed = []

    def implementation(text):
        started.set()
        assert release.wait(10)
        executed.append(text)
        return {"text": text}

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"once"}', "provider-one")),
        final("done"),
    )
    events = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            SnapshotKernel().run, snapshot, tools, adapter,
            on_boundary=lambda kind: not interrupt.is_set(),
            on_tool_event=lambda *event: events.append(event),
        )
        assert started.wait(10)
        interrupt.set()
        assert not future.done()
        release.set()
        with pytest.raises(KernelPaused) as caught:
            future.result(timeout=10)
    checkpoint = caught.value.checkpoint
    assert checkpoint.pending_tools == ()
    assert len(checkpoint.messages) == 2
    assert executed == ["once"]
    assert [event[0] for event in events] == ["queue", "start", "settle"]
    assert events[-1][3] == "success"

    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert executed == ["once"]


def test_tool_error_after_side_effect_is_paired_and_never_reexecuted_on_resume():
    effects = []

    def implementation(text):
        effects.append(text)
        raise ToolExecutionError("PRIVATE-TOOL-ERROR")

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"effect persists"}', "provider-one")),
        final("done"),
    )
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_boundary=lambda kind: kind != "after_tool",
        )
    checkpoint = caught.value.checkpoint
    assert effects == ["effect persists"]
    assert checkpoint.pending_tools == ()
    tool_result = checkpoint.messages[-1]["blocks"][0]
    assert tool_result["status"] == "error"
    assert "PRIVATE-TOOL-ERROR" not in str(checkpoint)

    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert effects == ["effect persists"]


def test_abrupt_tool_exit_marks_unknown_and_never_returns_resumable_checkpoint():
    effects = []

    failure = asyncio.CancelledError()

    def implementation(text):
        effects.append(text)
        raise failure

    snapshot, tools = setup(implementation)
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"uncertain"}', "provider-one")),
        final("must not dispatch"),
    )
    events = []
    with pytest.raises(RunFailed, match="execution_interrupted") as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_tool_event=lambda *event: events.append(event),
        )
    assert effects == ["uncertain"]
    assert caught.value.__cause__ is failure
    assert len(adapter.requests) == 1
    assert [event[0] for event in events] == ["queue", "start", "settle"]
    assert events[-1][3] == "unknown"
    assert events[1][2] == events[2][2]
    assert len(caught.value.messages) == 1
    assert caught.value.messages[0]["role"] == "assistant"


def test_tool_start_callback_can_atomically_reject_dispatch_after_boundary_grant():
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"once"}', "provider-one")),
        final("done"),
    )
    events = []

    def deny_start(kind, call_id, execution_id, outcome):
        events.append((kind, call_id, execution_id, outcome))
        if kind == "start":
            raise KernelPauseRequested()

    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_boundary=lambda kind: True, on_tool_event=deny_start,
        )
    checkpoint = caught.value.checkpoint
    assert executed == []
    assert len(checkpoint.pending_tools) == 1
    assert [event[0] for event in events] == ["queue", "start"]
    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert executed == ["once"]


def test_valid_final_can_pause_before_finalizing_without_requerying_model():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(final("done"))
    events = []
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_boundary=lambda kind: kind != "before_final",
            on_tool_event=lambda *event: events.append(event),
        )
    checkpoint = caught.value.checkpoint
    assert len(adapter.requests) == 1
    assert len(checkpoint.messages) == len(checkpoint.pending_tools) == 1
    assert [event[0] for event in events] == ["queue"]
    result = SnapshotKernel().run(
        snapshot, tools, adapter, checkpoint=checkpoint,
        on_boundary=lambda kind: True,
        on_tool_event=lambda *event: events.append(event),
    )
    assert result.final["value"] == {"text": "done"}
    assert len(adapter.requests) == 1
    assert [event[0] for event in events] == ["queue", "start", "settle"]


def test_pause_during_retry_starts_new_logical_request_without_resetting_attempts():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(TimeoutError(), final("done"))
    checked = 0

    def boundary(kind):
        nonlocal checked
        if kind == "before_request":
            checked += 1
            return checked == 1
        return True

    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel(backoff=lambda _: None).run(
            snapshot, tools, adapter, on_boundary=boundary,
        )
    checkpoint = caught.value.checkpoint
    assert (checkpoint.model_requests, checkpoint.attempts) == (1, 1)
    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert (result.model_requests, result.attempts) == (2, 2)


def test_exhausted_budget_can_settle_pending_tool_but_never_sends_another_model_request():
    effects = []
    snapshot, tools = setup(lambda text: effects.append(text) or {"text": text})
    adapter = ScriptedAdapter(batch(
        call("inspect_text", '{"text":"first"}', "provider-one"),
        call("inspect_text", '{"text":"pending"}', "provider-two"),
    ))
    checks = 0

    def boundary(kind):
        nonlocal checks
        if kind == "before_tool":
            checks += 1
            return checks == 1
        return True

    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter, max_model_requests=1,
            on_boundary=boundary,
        )
    checkpoint = caught.value.checkpoint
    assert (checkpoint.model_requests, checkpoint.attempts) == (1, 1)
    assert len(checkpoint.pending_tools) == 1
    assert effects == ["first"]

    with pytest.raises(RunFailed, match="model_request_budget_exhausted") as exhausted:
        SnapshotKernel().run(
            snapshot, tools, adapter, max_model_requests=1,
            checkpoint=checkpoint,
        )
    assert effects == ["first", "pending"]
    assert len(adapter.requests) == 1
    assert (exhausted.value.model_requests, exhausted.value.attempts) == (1, 1)
    assert len(exhausted.value.messages) == 3


def test_resume_preserves_final_correction_budget():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(
        batch(call("final_answer", "{}", "provider-invalid-one")),
        batch(call("final_answer", "{}", "provider-invalid-two")),
    )
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_boundary=lambda kind: kind != "after_tool",
        )
    checkpoint = caught.value.checkpoint
    assert checkpoint.final_corrections == 1
    assert len(checkpoint.messages) == 2
    with pytest.raises(RunFailed, match="invalid_final") as failed:
        SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert (failed.value.model_requests, failed.value.attempts) == (2, 2)
    assert len(failed.value.messages) == 4


def test_resume_preserves_seen_provider_ids_without_repeating_completed_tool():
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"once"}', "provider-reused")),
        batch(call("inspect_text", '{"text":"twice"}', "provider-reused")),
    )
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_boundary=lambda kind: kind != "after_tool",
        )
    with pytest.raises(RunFailed, match="protocol_error"):
        SnapshotKernel().run(snapshot, tools, adapter, checkpoint=caught.value.checkpoint)
    assert executed == ["once"]


@pytest.mark.parametrize("mutate", [
    lambda checkpoint: checkpoint.pending_tools[0].arguments.update(text="tampered"),
    lambda checkpoint: checkpoint.messages[0]["blocks"][0].update(
        raw_arguments='{"text":"tampered"}',
    ),
    lambda checkpoint: replace(checkpoint, pending_tools=()),
])
def test_tampered_pending_checkpoint_cannot_dispatch_tool(mutate):
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"original"}', "provider-one")),
        final("must not dispatch"),
    )
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_boundary=lambda kind: kind != "before_tool",
        )
    checkpoint = caught.value.checkpoint
    altered = mutate(checkpoint) or checkpoint
    with pytest.raises(RunFailed, match="configuration_error"):
        SnapshotKernel().run(snapshot, tools, adapter, checkpoint=altered)
    assert executed == []
    assert len(adapter.requests) == 1


def test_tampered_half_paired_history_cannot_dispatch_remaining_tool():
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    adapter = ScriptedAdapter(batch(
        call("inspect_text", '{"text":"first"}', "provider-one"),
        call("inspect_text", '{"text":"second"}', "provider-two"),
    ), final("must not dispatch"))
    checks = 0

    def boundary(kind):
        nonlocal checks
        if kind == "before_tool":
            checks += 1
            return checks == 1
        return True

    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_boundary=boundary)
    checkpoint = caught.value.checkpoint
    checkpoint.messages[1]["blocks"][0]["tool_call_id"] = checkpoint.pending_tools[0].call_id
    with pytest.raises(RunFailed, match="configuration_error"):
        SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert executed == ["first"]
    assert len(adapter.requests) == 1


def test_default_backoff_polls_pause_before_next_retry_dispatch():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(TimeoutError(), final("must not dispatch"))
    checks = 0

    def boundary(kind):
        nonlocal checks
        if kind == "before_request":
            checks += 1
            return checks < 3
        return True

    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_boundary=boundary)
    assert checks == 3
    assert (caught.value.checkpoint.model_requests, caught.value.checkpoint.attempts) == (1, 1)
    assert len(adapter.requests) == 1


def test_resume_rejects_changed_frozen_snapshot_before_any_new_dispatch():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(final("must not dispatch"))
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_boundary=lambda kind: False)
    changed = copy.deepcopy(snapshot)
    changed["model_parameters"]["max_tokens"] = 42
    with pytest.raises(RunFailed, match="configuration_error"):
        SnapshotKernel().run(changed, tools, adapter, checkpoint=caught.value.checkpoint)
    assert adapter.requests == []
