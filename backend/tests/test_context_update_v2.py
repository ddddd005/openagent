"""Current context merge proof mutations over offline native graph runs."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.runtime import CanonicalModelAdapter
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore
from test_context_native_integration import NativeTransport, native_graph, node_output
from test_graph_service import create
from test_gemini_history import metadata
from test_model_package import profile_config
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import run


@pytest.fixture(scope="module")
def current_completed_round(tmp_path_factory):
    transport = NativeTransport()
    path = tmp_path_factory.mktemp("context-v5-proof") / "graph.sqlite"
    with closing(GraphWorkflowService(path, public_model_factory=transport.factory)) as service:
        return make_current_round(
            service, final_text="Current proof mutation matrix")


def make_current_round(service, *, final_text, policy=True, once=True,
                       provider_protocol=None, model_id=None):
    ModelDatabaseFixture.write(service, 1)
    if provider_protocol is not None:
        provider = service.get_global_resource(profile_config()["provider"])
        provider["update_sequence"] += 1
        provider["value"]["protocol"] = provider_protocol
        service.save_global_resource(
            provider, expected_sequence=provider["update_sequence"] - 1, idempotency_key=str(uuid4()))
    if model_id is not None:
        ModelDatabaseFixture.update_profile(service, model={"id": model_id})
    document, entries = native_graph(service, policy=policy, once=once)
    initial = create(service, document)
    final = run(service, initial, final_text)
    assert final["status"] == "succeeded", final["chains"]
    receipts = deepcopy(node_output(final, entries["a"]["agent"], "receipts"))
    history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
    facts_by_id = {fact["fact_id"]: fact for fact in history["runtime_facts"]}
    executor_facts = [facts_by_id[identity] for identity in receipts["fact_ids"]]
    model_fact_records = {
        fact["fact_id"]: fact for fact in history["runtime_facts"]
        if fact.get("kind") == "workflow.model-fact"
    }
    model_facts = {"requests": [{
        "request_id": request["request_id"],
        "settings_id": request["settings_id"],
        "facts": [model_fact_records[identity] for identity in request["fact_ids"]],
    } for request in receipts["model_requests"]]}
    with closing(SqliteStore(service.database)) as store:
        objects = SessionObjectStore(store, service.registry.data_types)

        def resolve(reference):
            return objects._resolve_write_artifact(
                final["workflow_session_id"], reference)["value"]

        prompt = resolve(receipts["frozen_prompt_ref"])
        binding = resolve(receipts["model_ref"])
        policy_ref = deepcopy(receipts["compaction_policy_ref"])
        policy = None if policy_ref is None else resolve(policy_ref)
    return {
        "service": service,
        "final": final,
        "entries": entries,
        "effective": deepcopy(node_output(final, entries["a"]["merge"])),
        "receipts": receipts,
        "executor_facts": executor_facts,
        "model_facts": model_facts,
        "prompt": prompt,
        "binding": binding,
        "policy": policy,
        "policy_ref": policy_ref,
    }


def event(facts, kind, index=0):
    return [fact["payload"]["payload"] for fact in facts if fact["payload"]["kind"] == kind][index]


def prove_current_round(round_facts, *, receipts=None, facts=None, models=None):
    from phase1_agent.context_update_v2 import prove_context_merge_execution_v2
    from phase1_agent.model_request_transport import resolve_model_request_transport

    return prove_context_merge_execution_v2(
        round_facts["prompt"], round_facts["binding"],
        round_facts["receipts"] if receipts is None else receipts,
        round_facts["executor_facts"] if facts is None else facts,
        model_facts=round_facts["model_facts"] if models is None else models,
        policy=round_facts["policy"], policy_ref=round_facts["policy_ref"],
        project_messages=CanonicalModelAdapter._project,
        resolve_transport=resolve_model_request_transport,
    )


class SignedNativeTransport:
    def __init__(self):
        self.calls, self.closes = [], 0

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                arguments = json.dumps({"answer": {"text": "Signed answer"}})
                response = ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "provider-final", "final_answer", arguments),), model=parameters["model"])
                calls = [(call.id, call.name, json.loads(call.raw_arguments)) for call in response.tool_calls]
                state = metadata(*calls, text=response.content or "")
                state["model"] = parameters["model"]
                return replace(response, thinking_summary="Visible summary", provider_metadata=state)

            def generate_prepared(self, prepared):
                return self.generate([], [])

            def close(self):
                fixture.closes += 1

        return Transport()


class CorrectionNativeTransport:
    def __init__(self, first_result):
        self.first_result = first_result
        self.calls, self.closes = [], 0

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                if len(fixture.calls) == 1 and fixture.first_result == "plain_response":
                    return ModelResponse("stop", "Needs final control.", model=parameters["model"])
                if len(fixture.calls) == 1:
                    return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                        "provider-invalid", "final_answer", "{}"),), model=parameters["model"])
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "provider-final", "final_answer",
                    json.dumps({"answer": {"text": "Accepted answer"}})),), model=parameters["model"])

            def generate_prepared(self, prepared):
                wire = prepared["transport_arguments"]
                return self.generate(wire["messages"], wire.get("tools", []))

            def close(self):
                fixture.closes += 1

        return Transport()


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
def test_exact_round_proof_rejects_modified_actual_evidence(current_completed_round, mutation):
    round_facts = current_completed_round
    receipts = deepcopy(round_facts["receipts"])
    facts = deepcopy(round_facts["executor_facts"])
    models = deepcopy(round_facts["model_facts"])
    assistant = event(facts, "message_accepted")["message"]
    request = next(event for event in (
        fact["payload"]["payload"] for fact in facts if fact["payload"]["kind"] == "model_request"
    ) if event["tools"])
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
        index = next(i for i, item in enumerate(round_facts["prompt"]["layout"])
                     if item["kind"] == layout_kind)
        request["messages"].pop(index)
    elif mutation == "request_tools":
        request["tools"][0]["function"]["description"] = "invented"
    elif mutation == "dispatch_identity":
        event(facts, "tool_dispatch")["tool_execution_id"] = str(uuid4())
    elif mutation == "missing_dispatch":
        index = next(i for i, fact in enumerate(facts) if fact["payload"]["kind"] == "tool_dispatch")
        removed = facts.pop(index)
        receipts["fact_ids"].remove(removed["fact_id"])
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
        receipts["model_requests"].pop(1)
        models["requests"].pop(1)
    elif mutation == "model_receipt_settings":
        identity = str(uuid4())
        receipts["model_requests"][0]["settings_id"] = identity
        models["requests"][0]["settings_id"] = identity
    elif mutation == "model_facts_order":
        models["requests"][0]["facts"].reverse()
    elif mutation == "model_settings_parameters":
        models["requests"][0]["facts"][0]["details"]["settings"]["parameters"]["max_tokens"] -= 1
    elif mutation == "compaction_current_covered":
        current_id = round_facts["prompt"]["messages"][-1]["message_id"]
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
        receipts["compaction_policy_ref"] = None
    else:
        from phase1_agent.context_v4 import build_context_execution_view_v5

        proof = prove_current_round(round_facts, receipts=receipts, facts=facts, models=models)
        changed = deepcopy(proof["projection_replay"])
        changed["generation_delta"] += 1
        with pytest.raises(ContractValidationError):
            build_context_execution_view_v5(
                round_facts["prompt"], proof["operations"], receipts, proof["final"],
                view_ref=round_facts["prompt"]["context_ref"],
                receipts_ref={"scope": "artifact", "output_id": str(uuid4())},
                projection_replay=changed,
            )
        return
    with pytest.raises(ContractValidationError):
        prove_current_round(round_facts, receipts=receipts, facts=facts, models=models)


def test_signed_provider_parts_survive_actual_kernel_and_pure_proof(tmp_path):
    transport = SignedNativeTransport()
    with closing(GraphWorkflowService(tmp_path / "signed-native.sqlite",
                                      public_model_factory=transport.factory)) as service:
        current = make_current_round(
            service, final_text="Signed provider parts.", policy=False, once=False,
            provider_protocol="gemini", model_id="gemini-2.5-flash")
        proof = prove_current_round(current)
        retained = current["effective"]["messages"]
        final = proof["final"]
        assert retained[-2]["message_id"] == final["message_id"]
        assert retained[-2]["provider_metadata"]["content"]["parts"][0]["thoughtSignature"].startswith(
            "synthetic-fixture-signature")
        assert retained[-1]["blocks"][0]["content"] == final["value"]
        changed = deepcopy(current["executor_facts"])
        accepted = event(changed, "message_accepted")["message"]
        accepted["provider_metadata"]["content"]["parts"][0]["thoughtSignature"] = "altered-signature"
        with pytest.raises(ContractValidationError):
            prove_current_round(current, facts=changed)


@pytest.mark.parametrize("first_result", ["plain_response", "invalid_final"])
def test_actual_feedback_and_final_correction_keep_the_original_request_proof(tmp_path, first_result):
    transport = CorrectionNativeTransport(first_result)
    with closing(GraphWorkflowService(tmp_path / f"feedback-{first_result}.sqlite",
                                      public_model_factory=transport.factory)) as service:
        current = make_current_round(
            service, final_text="Retain original request proof.", policy=False, once=False)
        proof = prove_current_round(current)
        assert proof["final"]["value"] == {"text": "Accepted answer"}
        facts = current["executor_facts"]
        request_ids = [
            fact["payload"]["payload"]["request_id"] for fact in facts
            if fact["payload"]["kind"] == "model_request"
        ]
        accepted_request_ids = [
            fact["payload"]["payload"]["message"]["source"]["request_id"] for fact in facts
            if fact["payload"]["kind"] == "message_accepted"
            and fact["payload"]["payload"]["message"].get("source", {}).get("kind") == "model"
        ]
        assert accepted_request_ids == request_ids
        if first_result == "plain_response":
            feedback = next(
                fact["payload"]["payload"]["message"]
                for fact in facts
                if fact["payload"]["kind"] == "message_accepted"
                and fact["payload"]["payload"]["message"].get("source", {}).get("kind")
                == "protocol_feedback"
            )
            feedback_text = "\n".join(
                block["text"] for block in feedback["blocks"] if block["kind"] == "text")
            assert feedback_text == "Call final_answer with a valid JSON answer."
            assert not any(block.get("text") == feedback_text
                           for message in current["effective"]["messages"] for block in message["blocks"])
        else:
            assert event(facts, "tool_settled")["message"]["blocks"][0]["content"] == {
                "error": "Invalid final answer."}
