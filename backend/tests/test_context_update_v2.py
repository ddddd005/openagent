"""Pure schema 2 proof mutations over a completed real offline kernel round."""

from copy import deepcopy
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from phase1_agent.context_update import validate_context_update
from phase1_agent.context_update_v2 import make_context_update_v2
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.execution_facts_v2 import prove_execution_facts_v2
from phase1_agent.runtime import CanonicalModelAdapter
from test_gemini_history import metadata
from test_model_kernel_integration import KernelHarness


@pytest.fixture(scope="module")
def completed_round(tmp_path_factory):
    host = KernelHarness(tmp_path_factory.mktemp("context-v2-round"))
    try:
        outcome = host.runtime.drive(host.owner)
        assert outcome.status == "succeeded"
        packet = outcome.outputs["context"]
        yield host, packet, host.executor_facts(packet), host.model_facts(packet)
    finally:
        host.close()


def event(facts, kind, index=0):
    return [fact["payload"]["payload"] for fact in facts if fact["payload"]["kind"] == kind][index]


@pytest.mark.parametrize("mutation", [
    "assistant_raw_arguments", "assistant_parsed_arguments", "assistant_tool_version",
    "assistant_request", "assistant_extra_text", "model_result_content", "request_fixed_removed",
    "request_current_removed", "request_tools", "dispatch_identity", "missing_dispatch",
    "settlement_visible_text", "settlement_status", "terminal_final", "terminal_requests",
    "terminal_attempts_bool", "terminal_snapshot_input", "model_receipt_missing",
    "model_receipt_settings", "model_facts_order", "model_settings_parameters",
    "compaction_current_covered", "compaction_summary", "compaction_decision",
    "compaction_settings", "compaction_measurement", "compaction_checkpoint",
    "unknown_event", "envelope_generation", "packet_policy", "packet_generation",
])
def test_exact_round_proof_rejects_modified_actual_evidence(completed_round, mutation):
    host, original_packet, original_facts, original_models = completed_round
    packet, facts, models = deepcopy(original_packet), deepcopy(original_facts), deepcopy(original_models)
    assistant = event(facts, "message_accepted")["message"]
    request = event(facts, "model_request")
    compact = event(facts, "context_compaction_applied")
    terminal = event(facts, "agent_result")
    if mutation == "assistant_raw_arguments":
        assistant["blocks"][0]["raw_arguments"] = "{ }"
    elif mutation == "assistant_parsed_arguments":
        assistant["blocks"][0]["parsed_arguments"] = {"invented": True}
    elif mutation == "assistant_tool_version":
        assistant["blocks"][0]["tool_definition_version"] = "2"
    elif mutation == "assistant_request":
        assistant["source"]["request_id"] = str(uuid4())
    elif mutation == "assistant_extra_text":
        assistant["blocks"].insert(0, {"kind": "text", "text": "invented"})
    elif mutation == "model_result_content":
        event(facts, "model_result")["result"]["content"] = "invented"
    elif mutation in ("request_fixed_removed", "request_current_removed"):
        layout_kind = "fixed" if mutation == "request_fixed_removed" else "current"
        index = next(i for i, item in enumerate(host.prompt["layout"]) if item["kind"] == layout_kind)
        request["messages"].pop(index)
    elif mutation == "request_tools":
        request["tools"][0]["function"]["description"] = "invented"
    elif mutation == "dispatch_identity":
        event(facts, "tool_dispatch")["tool_execution_id"] = str(uuid4())
    elif mutation == "missing_dispatch":
        index = next(i for i, fact in enumerate(facts) if fact["payload"]["kind"] == "tool_dispatch")
        removed = facts.pop(index)
        packet["receipts"]["fact_ids"].remove(removed["fact_id"])
        for sequence, fact in enumerate(facts, 1):
            fact["sequence"] = sequence
    elif mutation == "settlement_visible_text":
        event(facts, "tool_settled")["message"]["blocks"][0]["model_visible_text"] = "invented"
    elif mutation == "settlement_status":
        event(facts, "tool_settled")["outcome"] = "error"
    elif mutation == "terminal_final":
        terminal["final"]["value"]["text"] = "invented"
    elif mutation == "terminal_requests":
        terminal["model_requests"] += 1
    elif mutation == "terminal_attempts_bool":
        terminal["attempts"] = True
    elif mutation == "terminal_snapshot_input":
        terminal["snapshot"]["input_id"] = str(uuid4())
    elif mutation == "model_receipt_missing":
        packet["receipts"]["model_requests"].pop(1)
        models["requests"].pop(1)
    elif mutation == "model_receipt_settings":
        identity = str(uuid4())
        packet["receipts"]["model_requests"][0]["settings_id"] = identity
        models["requests"][0]["settings_id"] = identity
    elif mutation == "model_facts_order":
        models["requests"][0]["facts"].reverse()
    elif mutation == "model_settings_parameters":
        models["requests"][0]["facts"][0]["details"]["settings"]["parameters"]["max_tokens"] -= 1
    elif mutation == "compaction_current_covered":
        current_id = host.prompt["messages"][-1]["message_id"]
        compact["covered_message_ids"].append(current_id)
    elif mutation == "compaction_summary":
        compact["result"]["content"] = "invented"
    elif mutation == "compaction_decision":
        compact["trigger_decision"]["decision_id"] = str(uuid4())
    elif mutation == "compaction_settings":
        compact["request"]["settings_id"] = str(uuid4())
    elif mutation == "compaction_measurement":
        compact["capacity"]["after"]["measurement"]["input_tokens"] += 1
    elif mutation == "compaction_checkpoint":
        compact["checkpoint"]["blocks"][0]["text"] = "invented"
    elif mutation == "unknown_event":
        facts[0]["payload"]["kind"] = "model_attempt_started"
    elif mutation == "envelope_generation":
        facts[-1]["generation"] = 0
    elif mutation == "packet_policy":
        packet["compaction_policy_ref"] = None
    else:
        packet["next_view"]["generation"] += 1
    with pytest.raises(ContractValidationError):
        host.prove(packet, facts, models)


def test_constructor_replays_effective_messages_and_strict_version_dispatch(completed_round):
    host, packet, facts, models = completed_round
    final = event(facts, "agent_result")["final"]
    effective = prove_execution_facts_v2(
        facts, host.prompt, host.mock.binding, packet["receipts"], model_facts=models,
        policy=host.policy, policy_ref=host.policy_ref, project_messages=CanonicalModelAdapter._project,
        resolve_transport=host.mock.resolve_transport)["effective_messages"]
    rebuilt = make_context_update_v2(
        host.prompt, packet["operations"], packet["receipts"], final,
        effective_messages=effective, policy_ref=host.policy_ref)
    assert rebuilt == packet
    assert validate_context_update(packet) == packet
    with pytest.raises(ContractValidationError):
        make_context_update_v2(host.prompt, packet["operations"], packet["receipts"], final,
                               effective_messages=effective[:-1], policy_ref=host.policy_ref)
    for version in (True, 2.0, 3):
        invalid = {**deepcopy(packet), "schema_version": version}
        with pytest.raises(ContractValidationError):
            validate_context_update(invalid)
    assert host.prove(packet, facts, models) == final


def test_signed_provider_parts_survive_actual_kernel_and_pure_proof(tmp_path):
    host = KernelHarness(tmp_path)
    try:
        provider = host.integrated.resources.provider
        provider["update_sequence"] += 1
        provider["value"]["protocol"] = "gemini"
        host.integrated.resources.save(provider)
        profile = host.integrated.resources.profile
        profile["update_sequence"] += 1
        profile["value"]["model"]["id"] = "gemini-2.5-flash"
        host.integrated.resources.save(profile)
        model = profile["value"]["model"]["id"]

        def signed_communication():
            host.communication()
            response = host.mock.response
            calls = [(call.id, call.name, json.loads(call.raw_arguments)) for call in response.tool_calls]
            state = metadata(*calls, text=response.content or "")
            state["model"] = model
            host.mock.response = replace(response, model=model, thinking_summary="Visible summary",
                                         provider_metadata=state)

        host.mock.on_communication = signed_communication
        outcome = host.runtime.drive(host.owner)
        assert outcome.status == "succeeded"
        packet = outcome.outputs["context"]
        facts, models = host.executor_facts(packet), host.model_facts(packet)
        final = host.prove(packet, facts, models)
        retained = packet["next_view"]["messages"]
        assert retained[-2]["message_id"] == final["message_id"]
        assert retained[-2]["provider_metadata"]["content"]["parts"][0]["thoughtSignature"].startswith(
            "synthetic-fixture-signature")
        assert retained[-1]["blocks"][0]["content"] == final["value"]
        changed = deepcopy(facts)
        accepted = event(changed, "message_accepted")["message"]
        accepted["provider_metadata"]["content"]["parts"][0]["thoughtSignature"] = "altered-signature"
        with pytest.raises(ContractValidationError):
            host.prove(packet, changed, models)
    finally:
        host.close()


@pytest.mark.parametrize("first_result", ["plain_response", "invalid_final"])
def test_actual_feedback_and_final_correction_keep_the_original_request_proof(tmp_path, first_result):
    host = KernelHarness(tmp_path)
    try:
        def communication():
            index = len(host.mock.communications)
            if index == 1:
                host.mock.response = (
                    ModelResponse("stop", "Needs final control.", model="mock-model")
                    if first_result == "plain_response" else
                    ModelResponse("tool_calls", tool_calls=(
                        ModelToolCall("provider-invalid", "final_answer", "{}"),), model="mock-model"))
            elif index == 2:
                host.mock.response = ModelResponse("tool_calls", tool_calls=(
                    ModelToolCall("provider-lookup", "lookup", "{}"),), model="mock-model")
            elif index == 3:
                host.mock.response = ModelResponse("stop", "Short retained summary.", model="mock-model")
                host.save_runtime(8000, 8000)
            else:
                host.mock.response = ModelResponse("tool_calls", tool_calls=(
                    ModelToolCall("provider-final", "final_answer",
                                  json.dumps({"answer": {"text": "Accepted answer"}})),), model="mock-model")

        host.mock.on_communication = communication
        outcome = host.runtime.drive(host.owner)
        assert outcome.status == "succeeded"
        packet = outcome.outputs["context"]
        facts, models = host.executor_facts(packet), host.model_facts(packet)
        assert host.prove(packet, facts, models)["value"] == {"text": "Accepted answer"}
        if first_result == "plain_response":
            assert not any(message["source"]["kind"] == "protocol_feedback"
                           for message in packet["next_view"]["messages"])
        else:
            assert event(facts, "tool_settled")["message"]["blocks"][0]["content"] == {
                "error": "Invalid final answer."}
    finally:
        host.close()
