"""Facts precede external dispatch and remain separate from accepted history."""

import copy
from dataclasses import replace

import pytest

from phase1_agent.runtime import KernelContractError, KernelPaused, RunFailed, SnapshotKernel
from phase1_agent.tools import ToolOutcomeUnknown
from test_runtime_pause import ScriptedAdapter, batch, call, final, setup


def test_complete_messages_and_parameters_are_recorded_before_tool_entry():
    facts = []

    def inspect(text):
        accepted = [event["payload"]["message"] for event in facts
                    if event["kind"] == "message_accepted"]
        assert accepted[-1]["blocks"][0]["parsed_arguments"] == {"text": text}
        assert accepted[-1]["blocks"][0]["raw_arguments"] == '{"text":"inspect"}'
        assert facts[-1]["kind"] == "tool_dispatch"
        return {"text": text}

    snapshot, tools = setup(inspect)
    adapter = ScriptedAdapter(batch(call("inspect_text", '{"text":"inspect"}', "p1")), final("done"))
    result = SnapshotKernel().run(snapshot, tools, adapter, on_fact=facts.append)
    messages = [event["payload"]["message"] for event in facts if event["kind"] == "message_accepted"]
    assert messages == result.messages
    settled = [event["payload"]["message"] for event in facts if event["kind"] == "tool_settled"]
    assert settled == [message for message in messages if message["role"] == "tool"]
    request = next(event["payload"] for event in facts if event["kind"] == "model_request")
    assert request["messages"] == snapshot["s0"]
    assert request["model_parameters"] == snapshot["model_parameters"]
    assert request["tools"] == [tool.definition for tool in tools]


@pytest.mark.parametrize("kind", [
    "model_request", "model_attempt_started", "model_attempt_finished",
    "message_accepted", "tool_dispatch", "tool_settled",
])
def test_fact_callback_failure_stops_without_extra_dispatch_or_recursive_write(kind):
    calls, facts = [], []
    snapshot, tools = setup(lambda text: calls.append(text) or {"text": text})
    adapter = ScriptedAdapter(batch(call("inspect_text", '{"text":"inspect"}', "p1")), final("done"))

    def failing(event):
        facts.append(copy.deepcopy(event))
        if event["kind"] == kind:
            raise OSError("sensitive storage detail")

    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_fact=failing)
    assert caught.value.code == "fact_persistence_error"
    assert isinstance(caught.value.__cause__, OSError)
    assert "sensitive" not in str(caught.value)
    assert len(adapter.requests) == (0 if kind in {"model_request", "model_attempt_started"} else 1)
    assert calls == (["inspect"] if kind == "tool_settled" else [])
    assert facts[-1]["kind"] == kind
    assert not any(event["kind"] == "execution_failed" for event in facts)


def test_attempt_identity_and_usage_survive_response_discard_at_pause_boundary():
    facts = []
    snapshot, tools = setup()
    response = replace(final("discarded"), usage={"prompt_tokens": 7, "completion_tokens": 3},
                       response_id="provider-response", model="fixture")
    adapter = ScriptedAdapter(response)
    with pytest.raises(KernelPaused) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_fact=facts.append,
                             on_boundary=lambda kind: kind != "before_accept")
    assert caught.value.checkpoint.messages == ()
    assert [event["kind"] for event in facts] == [
        "model_request", "model_attempt_started", "model_attempt_finished",
    ]
    finished = facts[-1]["payload"]
    assert finished["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
    assert finished["response_id"] == "provider-response"
    assert finished["outcome"] == "responded"
    assert facts[1]["payload"]["attempt_id"] == finished["attempt_id"]


def test_transport_retry_keeps_request_identity_and_records_unknown_usage():
    facts = []
    snapshot, tools = setup()
    adapter = ScriptedAdapter(ConnectionError("withheld provider detail"), final("done"))
    result = SnapshotKernel(backoff=lambda retry: None).run(snapshot, tools, adapter, on_fact=facts.append)
    assert (result.model_requests, result.attempts) == (1, 2)
    requests = [event["payload"] for event in facts if event["kind"] == "model_request"]
    starts = [event["payload"] for event in facts if event["kind"] == "model_attempt_started"]
    finished = [event["payload"] for event in facts if event["kind"] == "model_attempt_finished"]
    assert len(requests) == 1
    assert [attempt["request_id"] for attempt in starts] == [requests[0]["request_id"]] * 2
    assert starts[0]["attempt_id"] != starts[1]["attempt_id"]
    assert [attempt["attempt_index"] for attempt in starts] == [1, 2]
    assert [attempt["retry_index"] for attempt in starts] == [0, 1]
    assert [attempt["outcome"] for attempt in finished] == ["model_error", "responded"]
    assert all(attempt["usage"] is None for attempt in finished)
    assert "withheld provider detail" not in repr(facts)


def test_uncertain_batch_persists_explicit_never_started_without_execution_ids():
    calls, facts = [], []

    def inspect(text):
        calls.append(text)
        raise ToolOutcomeUnknown("untrusted detail")

    snapshot, tools = setup(inspect)
    adapter = ScriptedAdapter(batch(
        call("inspect_text", '{"text":"unknown"}', "p1"),
        call("inspect_text", '{"text":"skip"}', "p2"),
    ), final("done"))
    SnapshotKernel().run(snapshot, tools, adapter, on_fact=facts.append)
    assert calls == ["unknown"]
    settled = [event["payload"] for event in facts if event["kind"] == "tool_settled"]
    assert [value["outcome"] for value in settled] == ["outcome_unknown", "never_started", "success"]
    assert settled[1]["tool_execution_id"] is None
    assert settled[1]["message"]["source"]["reason_code"] == "never_started"
    assert "untrusted detail" not in repr(facts)


def test_callback_receives_detached_objects_without_changing_kernel_history():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(final("done"))

    def mutate(event):
        event["payload"].clear()

    result = SnapshotKernel().run(snapshot, tools, adapter, on_fact=mutate)
    assert result.final["value"] == {"text": "done"}
    assert result.messages[-1]["blocks"][0]["content"] == {"text": "done"}


def test_model_failure_is_classified_without_persisting_exception_text():
    facts = []
    snapshot, tools = setup()
    adapter = ScriptedAdapter(ValueError("sensitive adapter program detail"))
    with pytest.raises(KernelContractError) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_fact=facts.append)
    assert caught.value.code == "adapter_contract_error"
    assert facts[-1] == {"kind": "execution_failed", "payload": {
        "code": "adapter_contract_error", "category": "contract", "model_requests": 1, "attempts": 1,
    }}
    assert facts[-2]["payload"]["outcome"] == "adapter_contract_error"
    assert "sensitive adapter program detail" not in repr(facts)


def test_invalid_provider_batch_is_not_accepted_or_partially_dispatched():
    calls, facts = [], []
    snapshot, tools = setup(lambda text: calls.append(text))
    adapter = ScriptedAdapter(batch(
        call("inspect_text", '{"text":"valid"}', "p1"), call("missing", "{}", "p2"),
    ))
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(snapshot, tools, adapter, on_fact=facts.append)
    assert caught.value.code == "protocol_error"
    assert calls == []
    assert not any(event["kind"] in {"message_accepted", "tool_dispatch"} for event in facts)
    assert facts[-1]["payload"]["category"] == "protocol"
