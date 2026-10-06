"""Trusted failure continuation and independent run-level model budgets."""

import asyncio
import copy
from dataclasses import replace

import httpx
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_message_history
from phase1_agent.contracts import ModelResponse
from phase1_agent.runtime import (
    KernelContractError,
    RunFailed,
    SnapshotKernel,
    verify_checkpoint,
)
from test_runtime_pause import ScriptedAdapter, batch, call, final, setup


def test_one_logical_request_can_use_four_transport_attempts():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(*[TimeoutError() for _ in range(3)], final("done"))
    facts, backoffs = [], []
    result = SnapshotKernel(backoff=backoffs.append).run(
        snapshot, tools, adapter, max_model_requests=1, on_fact=facts.append,
    )
    assert (result.model_requests, result.attempts) == (1, 4)
    assert backoffs == [1, 2, 3]
    requests = [fact for fact in facts if fact["kind"] == "model_request"]
    starts = [fact["payload"] for fact in facts if fact["kind"] == "model_attempt_started"]
    assert len(requests) == 1
    assert [start["retry_index"] for start in starts] == [0, 1, 2, 3]
    assert {start["request_id"] for start in starts} == {requests[0]["payload"]["request_id"]}


@pytest.mark.parametrize("budget, expected", [
    ({"max_model_requests": 1}, "model_request_budget_exhausted"),
    ({"max_model_requests": 8, "max_model_attempts": 1}, "model_attempt_budget_exhausted"),
])
def test_exhaustion_does_not_record_a_logical_request_without_a_possible_attempt(budget, expected):
    snapshot, tools = setup()
    adapter = ScriptedAdapter(batch(call("inspect_text", '{"text":"once"}', "provider-one")))
    facts = []
    with pytest.raises(RunFailed, match=expected) as failed:
        SnapshotKernel().run(snapshot, tools, adapter, on_fact=facts.append, **budget)
    checkpoint = failed.value.checkpoint
    assert checkpoint is not None
    assert not failed.value.recording_failed
    assert checkpoint.pending_tools == ()
    assert (checkpoint.model_requests, checkpoint.attempts) == (1, 1)
    assert len([fact for fact in facts if fact["kind"] == "model_request"]) == 1
    assert len(adapter.requests) == 1
    validate_message_history(snapshot["s0"] + list(checkpoint.messages))
    verify_checkpoint(checkpoint, snapshot)


def test_attempt_limit_during_retry_has_closed_checkpoint_and_no_extra_attempt():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(TimeoutError(), TimeoutError(), final("resumed"))
    facts = []
    kernel = SnapshotKernel(backoff=lambda _: None)
    with pytest.raises(RunFailed, match="model_attempt_budget_exhausted") as failed:
        kernel.run(snapshot, tools, adapter, max_model_attempts=2, on_fact=facts.append)
    checkpoint = failed.value.checkpoint
    assert checkpoint is not None
    assert (checkpoint.model_requests, checkpoint.attempts) == (1, 2)
    assert checkpoint.messages == checkpoint.pending_tools == ()
    assert len(adapter.requests) == 2
    result = kernel.run(
        snapshot, tools, adapter, max_model_attempts=3, checkpoint=checkpoint, on_fact=facts.append,
    )
    assert (result.model_requests, result.attempts) == (2, 3)
    requests = [fact["payload"] for fact in facts if fact["kind"] == "model_request"]
    assert [request["request_index"] for request in requests] == [1, 2]
    assert requests[0]["request_id"] != requests[1]["request_id"]
    assert requests[0]["messages"] == requests[1]["messages"]
    starts = [fact["payload"] for fact in facts if fact["kind"] == "model_attempt_started"]
    assert [start["attempt_index"] for start in starts] == [1, 2, 3]
    assert [start["retry_index"] for start in starts] == [0, 1, 0]


def test_retry_failure_continuation_preserves_completed_tools_and_cumulative_history():
    effects, facts = [], []
    snapshot, tools = setup(lambda text: effects.append(text) or {"text": text})
    frozen = copy.deepcopy(snapshot)
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"once"}', "provider-one")),
        *[TimeoutError("private transport detail") for _ in range(4)],
        final("done"),
    )
    kernel = SnapshotKernel(backoff=lambda _: None)
    with pytest.raises(RunFailed, match="model_retry_exhausted") as failed:
        kernel.run(snapshot, tools, adapter, on_fact=facts.append)
    checkpoint = failed.value.checkpoint
    assert checkpoint is not None
    assert checkpoint.pending_tools == ()
    assert (checkpoint.model_requests, checkpoint.attempts) == (2, 5)
    assert len(checkpoint.messages) == 2
    assert effects == ["once"]
    failed.value.messages[1]["blocks"][0]["content"]["text"] = "mutated failure copy"
    verify_checkpoint(checkpoint, snapshot)
    result = kernel.run(snapshot, tools, adapter, checkpoint=checkpoint, on_fact=facts.append)
    assert result.final["value"] == {"text": "done"}
    assert (result.model_requests, result.attempts) == (3, 6)
    assert result.messages[:2] == list(checkpoint.messages)
    assert adapter.requests[-1] == snapshot["s0"] + list(checkpoint.messages)
    assert effects == ["once"]
    assert snapshot == frozen
    requests = [fact["payload"] for fact in facts if fact["kind"] == "model_request"]
    assert [request["request_index"] for request in requests] == [1, 2, 3]
    assert len({request["request_id"] for request in requests}) == 3
    assert len([fact for fact in facts if fact["kind"] == "tool_dispatch"]) == 2
    assert "private transport detail" not in repr(facts)


def test_budget_extension_continuation_does_not_reset_counts_or_repeat_tools():
    effects = []
    snapshot, tools = setup(lambda text: effects.append(text) or {"text": text})
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"once"}', "provider-one")), final("done"),
    )
    kernel = SnapshotKernel()
    with pytest.raises(RunFailed, match="model_request_budget_exhausted") as failed:
        kernel.run(snapshot, tools, adapter, max_model_requests=1)
    checkpoint = failed.value.checkpoint
    with pytest.raises(RunFailed, match="model_request_budget_exhausted") as unchanged:
        kernel.run(snapshot, tools, adapter, max_model_requests=1, checkpoint=checkpoint)
    assert unchanged.value.checkpoint == checkpoint
    assert len(adapter.requests) == 1
    result = kernel.run(snapshot, tools, adapter, max_model_requests=2, checkpoint=checkpoint)
    assert (result.model_requests, result.attempts) == (2, 2)
    assert effects == ["once"]


def test_extended_logical_budget_above_eight_is_valid_and_not_the_initial_default():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(*[
        batch(call("inspect_text", '{"text":"more"}', f"provider-{index}"))
        for index in range(8)
    ], final("done"))
    result = SnapshotKernel().run(snapshot, tools, adapter, max_model_requests=9)
    assert (result.model_requests, result.attempts) == (9, 9)


@pytest.mark.parametrize("budgets", [
    {"max_model_requests": True},
    {"max_model_requests": 0},
    {"max_model_requests": 65},
    {"max_model_requests": 1.5},
    {"max_model_attempts": True},
    {"max_model_attempts": 0},
    {"max_model_attempts": 257},
    {"max_model_attempts": 1.5},
])
def test_invalid_budget_configuration_has_no_checkpoint_or_dispatch(budgets):
    snapshot, tools = setup()
    adapter = ScriptedAdapter(final("unreachable"))
    with pytest.raises(KernelContractError, match="configuration_error") as failed:
        SnapshotKernel().run(snapshot, tools, adapter, **budgets)
    assert failed.value.checkpoint is None
    assert adapter.requests == []


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_nonretryable_transport_failure_never_offers_recovery_checkpoint(status):
    request = httpx.Request("GET", "https://offline.invalid/")
    failure = httpx.HTTPStatusError(
        "private provider detail", request=request, response=httpx.Response(status, request=request),
    )
    snapshot, tools = setup()
    adapter = ScriptedAdapter(failure, final("unreachable"))
    with pytest.raises(RunFailed, match="model_error") as failed:
        SnapshotKernel().run(snapshot, tools, adapter)
    assert failed.value.checkpoint is None
    assert (failed.value.model_requests, failed.value.attempts) == (1, 1)
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 403])
def test_nonretryable_transport_failure_is_not_masked_by_a_concurrent_pause(status):
    request = httpx.Request("GET", "https://offline.invalid/")
    failure = httpx.HTTPStatusError(
        "private provider detail", request=request, response=httpx.Response(status, request=request),
    )
    snapshot, tools = setup()
    adapter = ScriptedAdapter(failure, final("unreachable"))
    boundaries = []

    def boundary(kind):
        boundaries.append(kind)
        return kind == "before_request"

    with pytest.raises(RunFailed, match="model_error") as failed:
        SnapshotKernel().run(snapshot, tools, adapter, on_boundary=boundary)
    assert failed.value.checkpoint is None
    assert failed.value.__cause__ is failure
    assert boundaries == ["before_request"]
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("failure, expected", [
    (ValueError("adapter bug"), "adapter_contract_error"),
    (asyncio.CancelledError(), "execution_interrupted"),
])
def test_program_fault_and_host_cancellation_never_offer_recovery_checkpoint(failure, expected):
    snapshot, tools = setup()
    adapter = ScriptedAdapter(failure)
    with pytest.raises(RunFailed, match=expected) as failed:
        SnapshotKernel().run(snapshot, tools, adapter)
    assert failed.value.checkpoint is None


def test_failure_fact_persistence_error_suppresses_recovery_but_preserves_primary_model_failure():
    snapshot, tools = setup()
    failure = TimeoutError("private transport detail")
    adapter = ScriptedAdapter(*[failure for _ in range(4)])

    def reject_failure(event):
        if event["kind"] == "execution_failed":
            raise OSError("private storage detail")

    with pytest.raises(RunFailed, match="model_retry_exhausted") as failed:
        SnapshotKernel(backoff=lambda _: None).run(snapshot, tools, adapter, on_fact=reject_failure)
    assert failed.value.checkpoint is None
    assert failed.value.recording_failed
    assert failed.value.__cause__ is failure
    assert failure.__notes__ == ["Execution-failure fact also failed (fact_persistence_error)."]
    assert len(adapter.requests) == 4


def test_failure_fact_persistence_error_without_model_cause_is_loud_contract_failure():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(ModelResponse("invalid"))

    def reject_failure(event):
        if event["kind"] == "execution_failed":
            raise OSError("private storage detail")

    with pytest.raises(KernelContractError, match="fact_persistence_error") as failed:
        SnapshotKernel().run(snapshot, tools, adapter, on_fact=reject_failure)
    assert failed.value.checkpoint is None


@pytest.mark.parametrize("mutate", [
    lambda checkpoint: replace(checkpoint, attempts=checkpoint.attempts + 1),
    lambda checkpoint: replace(checkpoint, model_requests=0),
    lambda checkpoint: replace(checkpoint, seen_provider_ids=frozenset()),
    lambda checkpoint: checkpoint.messages[1]["blocks"][0]["content"].update(text="tampered"),
])
def test_tampered_failure_checkpoint_is_rejected_before_new_dispatch(mutate):
    snapshot, tools = setup()
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"once"}', "provider-one")), final("unreachable"),
    )
    with pytest.raises(RunFailed, match="model_request_budget_exhausted") as failed:
        SnapshotKernel().run(snapshot, tools, adapter, max_model_requests=1)
    checkpoint = failed.value.checkpoint
    altered = mutate(checkpoint) or checkpoint
    with pytest.raises(ContractValidationError, match="Invalid kernel checkpoint"):
        verify_checkpoint(altered, snapshot)
    with pytest.raises(KernelContractError, match="configuration_error") as rejected:
        SnapshotKernel().run(snapshot, tools, adapter, checkpoint=altered)
    assert rejected.value.checkpoint is None
    assert len(adapter.requests) == 1


def test_checkpoint_from_another_process_key_is_not_recoverable(monkeypatch):
    snapshot, tools = setup()
    adapter = ScriptedAdapter(ModelResponse("invalid"), final("unreachable"))
    with pytest.raises(RunFailed, match="protocol_error") as failed:
        SnapshotKernel().run(snapshot, tools, adapter)
    monkeypatch.setattr("phase1_agent.runtime._CHECKPOINT_KEY", b"another-process-secret")
    with pytest.raises(ContractValidationError, match="Invalid kernel checkpoint"):
        verify_checkpoint(failed.value.checkpoint, snapshot)
    with pytest.raises(KernelContractError, match="configuration_error"):
        SnapshotKernel().run(snapshot, tools, adapter, checkpoint=failed.value.checkpoint)
    assert len(adapter.requests) == 1


def test_protocol_failure_continuation_preserves_text_feedback_limit():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(
        ModelResponse("stop", content="first"), ModelResponse("stop", content="second"),
        ModelResponse("stop", content="third"), final("done"),
    )
    kernel = SnapshotKernel()
    with pytest.raises(RunFailed, match="protocol_error") as failed:
        kernel.run(snapshot, tools, adapter)
    checkpoint = failed.value.checkpoint
    assert checkpoint.text_feedback == 1
    with pytest.raises(RunFailed, match="protocol_error") as failed_again:
        kernel.run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert failed_again.value.checkpoint.text_feedback == 1
    messages = list(failed_again.value.checkpoint.messages)
    assert len([message for message in messages
                if message["source"]["kind"] == "protocol_feedback"]) == 1
    result = kernel.run(snapshot, tools, adapter, checkpoint=failed_again.value.checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert (result.model_requests, result.attempts) == (4, 4)


def test_invalid_final_failure_continuation_preserves_correction_limit():
    snapshot, tools = setup()
    adapter = ScriptedAdapter(*[
        batch(call("final_answer", "{}", f"invalid-{index}")) for index in range(3)
    ], final("done"))
    kernel = SnapshotKernel()
    with pytest.raises(RunFailed, match="invalid_final") as failed:
        kernel.run(snapshot, tools, adapter)
    checkpoint = failed.value.checkpoint
    assert checkpoint.final_corrections == 1
    with pytest.raises(RunFailed, match="invalid_final") as failed_again:
        kernel.run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert failed_again.value.checkpoint.final_corrections == 1
    assert (failed_again.value.model_requests, failed_again.value.attempts) == (3, 3)
    result = kernel.run(snapshot, tools, adapter, checkpoint=failed_again.value.checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert (result.model_requests, result.attempts) == (4, 4)


def test_failure_continuation_preserves_seen_provider_ids():
    effects = []
    snapshot, tools = setup(lambda text: effects.append(text) or {"text": text})
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":"once"}', "same-provider")),
        *[TimeoutError() for _ in range(4)],
        batch(call("inspect_text", '{"text":"repeat"}', "same-provider")),
    )
    kernel = SnapshotKernel(backoff=lambda _: None)
    with pytest.raises(RunFailed, match="model_retry_exhausted") as failed:
        kernel.run(snapshot, tools, adapter)
    with pytest.raises(RunFailed, match="protocol_error") as rejected:
        kernel.run(snapshot, tools, adapter, checkpoint=failed.value.checkpoint)
    assert rejected.value.checkpoint is not None
    assert effects == ["once"]
    assert len(rejected.value.messages) == 2
