"""Closed request boundaries against real temporary SQLite and mock communication."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.agent_executor import AGENT_COMPONENT_ID
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.model_execution_contracts import (
    AGENT_EXECUTOR_REF_V2, model_request_stream_id, prove_model_request,
    replay_model_context_operations, validate_compaction_proof, validate_input_snapshot_v2,
)
from phase1_agent.model_execution_requests import ModelExecutionRequests
from phase1_agent.runtime import CanonicalModelAdapter
from test_model_request_fact_reader import FactHarness
from test_model_request_integration import IntegratedHost


def human(text):
    return validate_record("agent_message", {
        "schema_version": 1, "message_id": str(uuid4()), "role": "user",
        "source": {"kind": "human", "visible_message_id": str(uuid4())},
        "blocks": [{"kind": "text", "text": text}],
    })


def snapshot(host, *, enabled=True, long_history=True):
    messages = [human("previous context " * (500 if long_history else 1)), human("current input")]
    model_ref = {"scope": "artifact", "output_id": host.mock.output_id}
    payload = {
        "public_agent": {}, "executor_ref": AGENT_EXECUTOR_REF_V2,
        "frozen_prompt_ref": {"scope": "artifact", "output_id": str(uuid4())},
        "model_ref": model_ref, "public_model_binding": host.mock.binding,
        "frozen_compaction_policy_ref": {"scope": "artifact", "output_id": str(uuid4())},
        "context_compaction": {
            "policy": {"schema_version": 2, "kind": "workflow.context-compaction-policy",
                       "enabled": enabled, "keep_depth": 0, "summary_prompt": "Summarize permitted text."},
            "layout": [{"kind": "history", "round_id": "previous", "compaction": "allowed"},
                       {"kind": "current", "round_id": None, "compaction": "never"}],
        },
    }
    return validate_input_snapshot_v2({
        "schema_version": 2, "snapshot_id": str(uuid4()),
        "workflow_session_id": host.mock.context.workflow_session_id,
        "node_binding_id": host.mock.context.node_binding_id,
        "input_id": str(uuid4()), "parent_turn_id": None,
        "component_id": AGENT_COMPONENT_ID, "component_version": "public-3",
        "config": {"owner_component_id": AGENT_COMPONENT_ID, "schema_version": 1, "payload": payload},
        "s0": messages, "tool_definitions": [
            {"name": "lookup", "version": "1", "description": "Synthetic lookup",
             "parameters_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
            {"name": "final_answer", "version": "1", "description": "Finish",
             "parameters_schema": {"type": "object", "properties": {
                 "text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}},
        ],
        "output_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                          "required": ["text"], "additionalProperties": False},
        "projection_version": 1,
    })


class DriverHarness:
    def __init__(self, directory, **options):
        self.host = IntegratedHost(directory)
        self.snapshot = snapshot(self.host, **options)
        self.events, self.calls, self.preparations, self.results = {}, [], [], []
        self.publish_hook = None
        self.driver = ModelExecutionRequests(
            self.snapshot, host_call=self.call, publish_fact=self.publish)
        self.owner = {key: getattr(self.host.mock.context, key) for key in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")}
        self.closed = False

    def call(self, capability, operation, payload):
        self.calls.append((capability, operation, deepcopy(payload)))
        result = self.host.mock.service(self.host.mock.context, capability, operation, payload)
        if operation == "prepare-request":
            self.preparations.append(deepcopy(result))
        elif operation == "dispatch-request":
            self.results.append(deepcopy(result))
        return result

    def publish(self, identity, event):
        assert identity not in self.events or self.events[identity] == event
        self.events[identity] = deepcopy(event)
        if self.publish_hook is not None:
            self.publish_hook(identity, event)

    def save_runtime(self, *, output=None, context=None):
        resources = self.host.resources
        resources.profile["update_sequence"] += 1
        if output is not None:
            resources.profile["value"]["runtime"]["output_limit_tokens"] = output
        if context is not None:
            resources.profile["value"]["runtime"]["context_length_tokens"] = context
        resources.save(resources.profile)

    def facts(self):
        result = {}
        for fact in self.host.facts():
            result.setdefault(fact["request_id"], []).append(fact)
        return result

    def proof_arguments(self):
        return {"owner": self.owner, "project_messages": CanonicalModelAdapter._project,
                "resolve_transport": self.host.mock.resolve_transport}

    def close(self):
        if self.closed:
            return
        self.driver.close()
        self.host.mock.close()
        self.closed = True


@pytest.fixture
def driver(tmp_path):
    fixture = DriverHarness(tmp_path)
    yield fixture
    fixture.close()


def settle_lookup(result):
    """Synthetic accepted tool batch; the request bridge never executes the tool."""
    execution_id, call_id = str(uuid4()), str(uuid4())
    assistant = validate_record("agent_message", {
        "schema_version": 1, "message_id": str(uuid4()), "role": "assistant",
        "source": {"kind": "model", "request_id": result["request_id"]},
        "blocks": [{"kind": "tool_call", "tool_call_id": call_id, "tool_name": "lookup",
                    "tool_definition_version": "1", "raw_arguments": "{}", "parsed_arguments": {}}],
    })
    tool = validate_record("agent_message", {
        "schema_version": 1, "message_id": str(uuid4()), "role": "tool",
        "source": {"kind": "tool", "tool_execution_id": execution_id},
        "blocks": [{"kind": "tool_result", "tool_call_id": call_id, "tool_execution_id": execution_id,
                    "status": "success", "is_error": False, "content": {"value": "found"},
                    "model_visible_text": '{"value":"found"}'}],
    })
    return [assistant, tool]


def test_current_boundaries_compact_once_and_prove_immutable_historical_wire(driver):
    mock = driver.host.mock
    mock.response = ModelResponse("tool_calls", tool_calls=(
        ModelToolCall("upstream-call-1", "lookup", "{}"),), model="synthetic-chat")
    first = driver.driver.advance("normal:1", driver.snapshot["s0"])
    first_facts = deepcopy(driver.host.facts())
    accepted = settle_lookup(first["result"])
    messages = driver.snapshot["s0"] + accepted
    driver.save_runtime(output=16000, context=10000)

    def publish(identity, event):
        if event["kind"] == "context_compaction_decided":
            driver.save_runtime(output=12000, context=11000)
    driver.publish_hook = publish
    mock.response = ModelResponse("stop", "Short summary", model="synthetic-chat")
    mock.on_communication = lambda: driver.save_runtime(output=8000, context=8000)
    second = driver.driver.advance("normal:2", messages, accepted_messages=accepted)
    mock.on_communication = None
    assert len(mock.communications) == 3 and driver.host.read_count == 4
    assert [item["parameters"]["max_tokens"] for item in mock.communications] == [32000, 12000, 8000]
    operation = second["operations"][0]
    facts = driver.facts()
    summary = facts[operation["request"]["request_id"]][0]["details"]["settings"]
    after = facts[second["request"]["request_id"]][0]["details"]["settings"]
    assert summary["budget"]["normal_output_limit_tokens"] == 12000
    assert after["parameters"]["max_tokens"] == 8000
    assert operation["capacity"]["before"]["settings_id"] == operation["capacity"]["after"]["settings_id"]
    assert operation["capacity"]["after"]["measurement"]["output_reserve_tokens"] == 12000
    assert operation["trigger_decision"]["preparation"]["settings"]["parameters"]["max_tokens"] == 16000
    assert operation["request"]["tools"] == []
    assert second["request"]["tools"]
    assert not {value for value in operation["trigger_decision"]["preparation"]["reserved_model_fact_ids"].values()
                }.intersection(fact["fact_id"] for fact in driver.host.facts())
    assert driver.host.facts()[:3] == first_facts
    assert [request["request_index"] for request in [first["request"]] + second["model_requests"]] == [1, 2, 3]
    proof_arguments = driver.proof_arguments()
    for request in [first["request"]] + second["model_requests"]:
        proof = prove_model_request(
            request, facts[request["request_id"]], binding=mock.binding,
            stream_id=model_request_stream_id(request["request_id"]), **proof_arguments)
        assert proof["settings"]["settings_id"] == request["settings_id"]
    validate_compaction_proof(
        operation, snapshot=driver.snapshot, model_facts=facts, accepted_messages=accepted, **proof_arguments)
    counts = (driver.host.read_count, len(mock.communications), len(mock.broker.freeze_calls), len(driver.host.facts()))
    driver.save_runtime(output=5000, context=9000)
    replayed = replay_model_context_operations(
        driver.snapshot, [{"kind": "append", "message": message} for message in accepted] + [operation],
        model_facts=facts, accepted_messages=accepted, **proof_arguments)
    assert replayed["messages"] == second["messages"]
    assert driver.driver.advance("normal:2", messages, accepted_messages=accepted) == second
    assert counts == (driver.host.read_count, len(mock.communications), len(mock.broker.freeze_calls),
                      len(driver.host.facts()))
    dispatched_ids = {result["request_id"] for result in driver.results}
    reader_host = FactHarness.from_integrated(
        driver.host, preparations=[prepared for prepared in driver.preparations
                                   if prepared["request_id"] in dispatched_ids], results=driver.results)
    read = reader_host.read()
    assert [item["request_id"] for item in read["requests"]] == [
        request["request_id"] for request in [first["request"]] + second["model_requests"]]
    authorized_facts = {item["request_id"]: item["facts"] for item in read["requests"]}
    assert authorized_facts == facts
    replayed = replay_model_context_operations(
        driver.snapshot, [{"kind": "append", "message": message} for message in accepted] + [operation],
        model_facts=authorized_facts, accepted_messages=accepted, **proof_arguments)
    assert replayed["messages"] == second["messages"]
    assert counts == (driver.host.read_count, len(mock.communications), len(mock.broker.freeze_calls),
                      len(driver.host.facts()))
    public = canonical_bytes({"events": list(driver.events.values()), "facts": driver.host.facts(),
                              "first": first, "second": second})
    assert b"synthetic-integration-key" not in public and b"credential_ref" not in public


def test_original_outcome_ack_loss_reentry_uses_same_request_without_communication(driver):
    driver.host.drop_outcome_ack = True
    with pytest.raises(ContractValidationError) as error:
        driver.driver.advance("normal:1", driver.snapshot["s0"])
    assert error.value.reason_code == "model_fact_acceptance_failed"
    original_facts, event_ids = deepcopy(driver.host.facts()), set(driver.events)
    driver.save_runtime(output=16000)
    result = driver.driver.advance("normal:1", driver.snapshot["s0"])
    assert len(driver.host.mock.communications) == 1 and driver.host.read_count == 1
    assert driver.host.facts() == original_facts and event_ids < set(driver.events)
    assert [event["kind"] for identity, event in driver.events.items()
            if identity not in event_ids] == ["model_result"]
    assert result["request"]["settings_id"] == original_facts[0]["details"]["settings"]["settings_id"]


def test_unknown_cannot_be_bypassed_with_a_new_key_or_retried_after_configuration_save(driver):
    driver.host.mock.transport_error = TimeoutError("Synthetic uncertainty")
    with pytest.raises(ContractValidationError) as error:
        driver.driver.advance("normal:1", driver.snapshot["s0"])
    assert error.value.reason_code == "model_dispatch_unknown"
    facts = deepcopy(driver.host.facts())
    driver.save_runtime(output=16000)
    with pytest.raises(ContractValidationError) as error:
        driver.driver.advance("normal:2", driver.snapshot["s0"])
    assert error.value.reason_code == "model_execution_boundary_pending"
    with pytest.raises(ContractValidationError) as error:
        driver.driver.advance("normal:1", driver.snapshot["s0"])
    assert error.value.reason_code == "model_dispatch_unknown"
    driver.close()
    assert driver.host.facts() == facts and len(driver.host.mock.communications) == 1


def test_executor_event_ack_loss_retains_id_and_prevents_communication_until_original_acceptance(driver):
    seen = []
    def publish(identity, event):
        seen.append(identity)
        driver.publish_hook = None
        raise OSError("Synthetic executor event acknowledgement loss")
    driver.publish_hook = publish
    with pytest.raises(ContractValidationError) as error:
        driver.driver.advance("normal:1", driver.snapshot["s0"])
    assert error.value.reason_code == "model_execution_fact_acceptance_failed"
    assert driver.host.mock.communications == []
    driver.save_runtime(output=16000)
    result = driver.driver.advance("normal:1", driver.snapshot["s0"])
    assert list(driver.events)[:1] == seen and len(driver.events) == 2
    assert len(driver.host.mock.communications) == 1
    assert driver.host.mock.communications[0]["parameters"]["max_tokens"] == 32000
    assert driver.host.read_count == 1 and result["operations"] == []


def test_close_discards_only_its_exact_undispatched_preparation(driver):
    def reject_request_fact(identity, event):
        if event["kind"] == "model_request":
            raise OSError("Synthetic request fact acceptance failure")

    driver.publish_hook = reject_request_fact
    with pytest.raises(ContractValidationError) as error:
        driver.driver.advance("normal:1", driver.snapshot["s0"])
    assert error.value.reason_code == "model_execution_fact_acceptance_failed"
    assert len(driver.host.mock.communications) == 0
    preparation = driver.preparations[0]
    driver.close()
    discarded = [payload for _, operation, payload in driver.calls
                 if operation == "discard-prepared-request"]
    assert discarded == [{
        "binding_id": driver.snapshot["config"]["payload"]["public_model_binding"]["binding_id"],
        "request_key": "normal:1:normal",
        "preparation_id": preparation["preparation_id"],
    }]
    assert not any(operation == "dispatch-request" for _, operation, _ in driver.calls)


def test_compaction_applied_event_ack_loss_retains_summary_and_never_resends_it(driver):
    driver.save_runtime(output=12000, context=10000)
    lost_ids = []
    def publish(identity, event):
        if event["kind"] == "context_compaction_applied":
            lost_ids.append(identity)
            driver.publish_hook = None
            raise OSError("Synthetic accepted-compaction event acknowledgement loss")
    driver.publish_hook = publish
    driver.host.mock.response = ModelResponse("stop", "summary", model="synthetic-chat")
    with pytest.raises(ContractValidationError) as error:
        driver.driver.advance("normal:1", driver.snapshot["s0"])
    assert error.value.reason_code == "model_execution_fact_acceptance_failed"
    summary_facts = deepcopy(driver.host.facts())
    assert len(driver.host.mock.communications) == 1
    driver.save_runtime(output=8000, context=8000)
    result = driver.driver.advance("normal:1", driver.snapshot["s0"])
    assert len(driver.host.mock.communications) == 2
    assert driver.host.facts()[:3] == summary_facts
    assert result["operations"][0] == driver.events[lost_ids[0]]["payload"]
    assert [item["parameters"]["max_tokens"] for item in driver.host.mock.communications] == [12000, 8000]


def test_same_key_concurrent_reentry_has_one_boundary_event_and_one_communication(driver):
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: driver.driver.advance("normal:1", driver.snapshot["s0"]), range(2)))
    assert results[0] == results[1]
    assert len(driver.host.mock.communications) == 1 and len(driver.events) == 2
    assert driver.host.read_count == 1


def test_changed_same_key_messages_are_a_conflict(driver):
    driver.driver.advance("normal:1", driver.snapshot["s0"])
    changed = deepcopy(driver.snapshot["s0"])
    changed[-1]["blocks"][0]["text"] = "new"
    with pytest.raises(ContractValidationError) as error:
        driver.driver.advance("normal:1", changed)
    assert error.value.reason_code == "model_request_conflict"
    assert len(driver.host.mock.communications) == 1


def test_disabled_policy_uses_hard_capacity_even_above_the_early_trigger(tmp_path):
    harness = DriverHarness(tmp_path, enabled=False)
    try:
        harness.save_runtime(output=16000, context=10000)
        result = harness.driver.advance("normal:1", harness.snapshot["s0"])
        assert result["operations"] == [] and len(harness.host.mock.communications) == 1
        assert not any(event["kind"] == "context_compaction_decided" for event in harness.events.values())
    finally:
        harness.close()


def test_pending_tool_batch_is_rejected_before_prepare_or_communication(driver):
    accepted = settle_lookup({"request_id": str(uuid4())})
    with pytest.raises(ContractValidationError):
        driver.driver.advance("normal:1", driver.snapshot["s0"] + accepted[:1])
    assert driver.host.read_count == 0 and driver.host.mock.communications == []


def test_summary_must_shrink_without_another_automatic_compaction(driver):
    driver.save_runtime(output=12000, context=10000)
    driver.host.mock.response = ModelResponse("stop", "summary text " * 2000, model="synthetic-chat")
    for _ in range(2):
        with pytest.raises(ContractValidationError) as error:
            driver.driver.advance("normal:1", driver.snapshot["s0"])
        assert error.value.reason_code == "model_context_reduction_invalid"
    assert len(driver.host.mock.communications) == 1
    assert len(driver.host.facts()) == 3
