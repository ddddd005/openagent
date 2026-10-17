"""Offline regressions for an explicitly unlimited kernel invocation."""

from collections import Counter
import json

from phase1_agent.contract_graph import validate_message_history
from phase1_agent.runtime import SnapshotKernel

from test_runtime import batch, call, final, setup


class RetryingLoopAdapter:
    """Three transient failures followed by a unique tool response per request."""

    def __init__(self, request_count):
        self.request_count = request_count
        self.calls = 0

    def generate(self, messages, tools):
        request_index, retry_index = divmod(self.calls, 4)
        self.calls += 1
        assert request_index < self.request_count, "Kernel continued after final_answer"
        if retry_index < 3:
            raise TimeoutError("offline transient fixture")
        provider_id = f"unlimited.provider.{request_index}"
        if request_index == self.request_count - 1:
            return final({"text": "finished by final_answer"}, call_id=provider_id)
        return batch(call("inspect_text", json.dumps({"text": str(request_index)}), provider_id))


def test_unlimited_kernel_exceeds_old_request_and_attempt_maxima_and_preserves_actual_counts():
    snapshot, tools = setup()
    adapter = RetryingLoopAdapter(request_count=65)
    facts = Counter()
    last_progress = {}

    def accept_fact(event):
        facts[event["kind"]] += 1

    def progress(event):
        last_progress.update(event)

    result = SnapshotKernel(backoff=lambda retry: None).run(
        snapshot, tools, adapter, on_fact=accept_fact, on_progress=progress)
    assert result.final["value"] == {"text": "finished by final_answer"}
    assert (result.model_requests, result.attempts, adapter.calls) == (65, 260, 260)
    assert facts["model_request"] == 65
    assert facts["model_attempt_started"] == facts["model_attempt_finished"] == 260
    assert facts["tool_dispatch"] == facts["tool_settled"] == 65
    assert facts["message_accepted"] == 130 and facts["execution_failed"] == 0
    assert last_progress == {"model_requests": 65, "attempts": 260, "accepted_messages": 130}
    validate_message_history(snapshot["s0"] + result.messages)
