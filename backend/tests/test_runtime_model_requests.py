"""Schema 2 kernel boundaries use original service requests without retries."""

from copy import deepcopy

import pytest

from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.model_execution_contracts import validate_input_snapshot_v2
from phase1_agent.model_execution_requests import ModelExecutionRequests
from phase1_agent.runtime import KernelPaused, RunFailed, SnapshotKernel
from phase1_agent.tools import final_answer_tool, register_callable
from test_model_execution_requests import snapshot
from test_model_request_integration import IntegratedHost


class KernelHarness:
    def __init__(self, directory):
        self.host = IntegratedHost(directory)
        self.effects, self.publications, self.kernel_events = [], [], []
        self.fail_kind = None
        lookup = register_callable(
            "lookup", "Synthetic lookup",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            lambda: self.effects.append("lookup") or {"value": "found"},
        )
        self.tools = (lookup, final_answer_tool())
        value = snapshot(self.host, enabled=False, long_history=False)
        value["tool_definitions"] = [{
            "name": tool.name, "version": "1",
            "description": tool.definition["function"]["description"],
            "parameters_schema": deepcopy(tool.schema),
        } for tool in self.tools]
        self.snapshot = validate_input_snapshot_v2(value)
        self.bridge = ModelExecutionRequests(
            self.snapshot,
            host_call=lambda capability, operation, payload: self.host.mock.service(
                self.host.mock.context, capability, operation, payload),
            publish_fact=self.publish,
        )
        self.host.mock.response = ModelResponse("tool_calls", tool_calls=(
            ModelToolCall("provider-final", "final_answer", '{"answer":{"text":"done"}}'),
        ), model="synthetic-chat")

    def publish(self, identity, event):
        self.publications.append((identity, deepcopy(event)))
        if event["kind"] == self.fail_kind:
            self.fail_kind = None
            raise OSError("Synthetic acknowledgement loss")

    def run(self, **options):
        return SnapshotKernel().run(
            self.snapshot, self.tools, request_bridge=self.bridge,
            on_fact=lambda event: self.kernel_events.append(deepcopy(event)), **options,
        )

    def save_output_limit(self):
        profile = self.host.resources.profile
        profile["update_sequence"] += 1
        profile["value"]["runtime"]["output_limit_tokens"] = 16000
        self.host.resources.save(profile)


@pytest.fixture
def kernel_host(tmp_path):
    harness = KernelHarness(tmp_path)
    yield harness
    harness.bridge.close()


@pytest.mark.parametrize("event_kind,attempts", [("model_request", 0), ("model_result", 1)])
def test_event_ack_loss_preserves_original_request_and_result_without_replay(
        kernel_host, event_kind, attempts):
    fixture = kernel_host
    fixture.fail_kind = event_kind
    with pytest.raises(RunFailed) as caught:
        fixture.run()
    failed = caught.value
    assert failed.code == "model_execution_fact_acceptance_failed"
    assert (failed.model_requests, failed.attempts) == (1, attempts)
    assert failed.checkpoint.request_boundary_index == 0
    assert failed.checkpoint.request_bridge_id == fixture.bridge.bridge_id
    assert len(fixture.host.mock.communications) == attempts
    retained = fixture.publications[-1]
    facts_before = deepcopy(fixture.host.facts())
    assert not any(event["kind"] == "execution_failed" for event in fixture.kernel_events)

    fixture.save_output_limit()
    result = fixture.run(checkpoint=failed.checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert (result.model_requests, result.attempts) == (1, 1)
    assert len(fixture.host.mock.communications) == fixture.host.read_count == 1
    assert [publication for publication in fixture.publications if publication == retained] == [
        retained, retained,
    ]
    assert fixture.host.facts()[:len(facts_before)] == facts_before
    request = next(event["payload"] for _, event in fixture.publications if event["kind"] == "model_request")
    model_message = result.messages[0]
    assert model_message["source"]["request_id"] == request["request_id"]
    assert fixture.host.mock.communications[0]["parameters"]["max_tokens"] == 32000
    assert not any(event["kind"] in {"model_attempt_started", "model_attempt_finished"}
                   for event in fixture.kernel_events)


def test_pause_before_accept_retains_real_response_and_resumes_without_dispatch(kernel_host):
    fixture = kernel_host
    with pytest.raises(KernelPaused) as caught:
        fixture.run(on_boundary=lambda kind: kind != "before_accept")
    checkpoint = caught.value.checkpoint
    assert (checkpoint.model_requests, checkpoint.attempts, checkpoint.request_boundary_index) == (1, 1, 1)
    assert checkpoint.pending_model_message is not None
    assert checkpoint.messages == checkpoint.pending_tools == ()
    SnapshotKernel.validate_checkpoint(checkpoint, fixture.snapshot, request_bridge=fixture.bridge)
    before = deepcopy(fixture.host.facts())

    result = fixture.run(checkpoint=checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert (result.model_requests, result.attempts) == (1, 1)
    assert len(fixture.host.mock.communications) == 1 and fixture.host.facts() == before


def test_real_tool_runs_between_original_model_requests(kernel_host):
    fixture = kernel_host
    fixture.host.mock.response = ModelResponse("tool_calls", tool_calls=(
        ModelToolCall("provider-lookup", "lookup", "{}"),
    ), model="synthetic-chat")
    communications = 0

    def next_response():
        nonlocal communications
        communications += 1
        if communications == 2:
            fixture.host.mock.response = ModelResponse("tool_calls", tool_calls=(
                ModelToolCall("provider-final", "final_answer", '{"answer":{"text":"done"}}'),
            ), model="synthetic-chat")

    fixture.host.mock.on_communication = next_response
    result = fixture.run()
    assert result.final["value"] == {"text": "done"}
    assert (result.model_requests, result.attempts) == (2, 2)
    assert fixture.effects == ["lookup"]
    assert len(fixture.host.mock.communications) == 2
    assert len(fixture.host.facts()) == 6
    assert [message["role"] for message in result.messages] == ["assistant", "tool", "assistant", "tool"]


def test_schema2_requires_explicit_bridge_before_any_model_or_tool_execution(kernel_host):
    fixture = kernel_host
    with pytest.raises(RunFailed) as caught:
        SnapshotKernel().run(fixture.snapshot, fixture.tools)
    assert caught.value.code == "configuration_error"
    assert fixture.host.mock.communications == [] and fixture.effects == []
    assert fixture.host.facts() == []
