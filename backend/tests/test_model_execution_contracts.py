"""Synthetic M3 proof fixtures; no current product data or communication."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.context_compaction import checkpoint_message, replace_compacted_messages
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.contracts import ModelResponse
from phase1_agent.model_execution_contracts import (
    AGENT_EXECUTOR_REF_V2, compaction_eligible_message_ids, expected_compaction_request,
    model_request_stream_id, prove_model_request, replay_model_context_operations,
    snapshot_wire_tools, validate_agent_receipts_v2, validate_compaction_proof,
    validate_input_snapshot_v2,
)
from phase1_agent.runtime import CanonicalModelAdapter
from test_model_request_service import ServiceHarness


def text_message(text, *, role="user", source=None):
    return {"schema_version": 1, "message_id": str(uuid4()), "role": role,
            "source": source or {"kind": "human", "visible_message_id": str(uuid4())},
            "blocks": [{"kind": "text", "text": text}]}


class ModelProofHarness:
    """Reusable pure proof composition over the M2 fake host and transport."""

    def __init__(self, *, tools=None):
        self.mock = ServiceHarness()
        self.owner = {key: getattr(self.mock.context, key) for key in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")}
        component_id = "9c5a4e10-c67c-4d3d-9df8-2c43cf9e19c4"
        self.messages = [text_message("historical context " + "x" * 230_000), text_message("current input")]
        self.snapshot = validate_input_snapshot_v2({
            "schema_version": 2, "snapshot_id": str(uuid4()),
            "workflow_session_id": self.owner["workflow_session_id"],
            "node_binding_id": self.owner["node_binding_id"], "input_id": str(uuid4()),
            "parent_turn_id": None, "component_id": component_id, "component_version": "public-3",
            "config": {"owner_component_id": component_id, "schema_version": 1, "payload": {
                "public_agent": {}, "executor_ref": deepcopy(AGENT_EXECUTOR_REF_V2),
                "frozen_prompt_ref": {"scope": "artifact", "output_id": str(uuid4())},
                "model_ref": {"scope": "artifact", "output_id": self.mock.output_id},
                "public_model_binding": deepcopy(self.mock.binding),
                "frozen_compaction_policy_ref": {"scope": "artifact", "output_id": str(uuid4())},
                "context_compaction": {"policy": {
                    "schema_version": 2, "kind": "workflow.context-compaction-policy",
                    "enabled": True, "keep_depth": 0, "summary_prompt": "Summarize selected history.",
                }, "layout": [{"kind": "history", "round_id": str(uuid4())},
                              {"kind": "current", "round_id": None}]},
            }}, "s0": deepcopy(self.messages), "tool_definitions": [] if tools is None else tools,
            "output_schema": {"type": "object"}, "projection_version": 1,
        })
        self.tools = snapshot_wire_tools(self.snapshot)
        self.requests = []

    def prepare(self, key, messages, purpose="normal", *, tools=None):
        return self.mock.prepare(key=key, purpose=purpose, messages=CanonicalModelAdapter._project(messages),
                                 tools=self.tools if tools is None else tools)

    def request(self, prepared, messages, *, tools=None):
        value = {"request_id": prepared["request_id"], "request_index": len(self.requests) + 1,
                 "messages": deepcopy(messages), "tools": deepcopy(self.tools if tools is None else tools),
                 "settings_id": prepared["settings"]["settings_id"],
                 "reserved_model_fact_ids": deepcopy(prepared["reserved_model_fact_ids"])}
        self.requests.append(value)
        return value

    def facts(self):
        result = {}
        for fact in self.mock.accepted.values():
            result.setdefault(fact["request_id"], []).append(deepcopy(fact))
        return result

    def measure(self, prepared, messages):
        return self.mock.service(self.mock.context, "model-projection:read", "measure-context", {
            "binding_id": self.mock.binding["binding_id"], "preparation_id": prepared["preparation_id"],
            "messages": CanonicalModelAdapter._project(messages), "tools": deepcopy(self.tools)})

    def prove(self, request, facts=None, **overrides):
        parameters = dict(owner=self.owner, binding=self.mock.binding,
                          stream_id=model_request_stream_id(request["request_id"]),
                          project_messages=CanonicalModelAdapter._project,
                          resolve_transport=self.mock.resolve_transport, input_refs=self.mock.input_refs)
        parameters.update(overrides)
        return prove_model_request(request, self.facts()[request["request_id"]] if facts is None else facts,
                                   **parameters)

    def compact(self):
        trigger = self.prepare("trigger:1", self.messages)
        assert trigger["requires_compaction"]
        discard = self.mock.service(self.mock.context, "models:call", "discard-prepared-request", {
            "binding_id": self.mock.binding["binding_id"], "request_key": "trigger:1",
            "preparation_id": trigger["preparation_id"]})
        self.mock.profile["update_sequence"] += 1
        self.mock.profile["value"]["runtime"]["output_limit_tokens"] = 16_000
        compaction_id, instruction_id = str(uuid4()), str(uuid4())
        covered = compaction_eligible_message_ids(self.snapshot, self.messages)
        maintenance = expected_compaction_request(
            self.snapshot, self.messages, covered, compaction_id=compaction_id,
            instruction_message_id=instruction_id)
        prepared = self.prepare("compact:1", maintenance, "compaction", tools=[])
        request = self.request(prepared, maintenance, tools=[])
        before = self.measure(prepared, self.messages)
        self.mock.response = ModelResponse("stop", "accepted summary")
        result = self.mock.dispatch(prepared, key="compact:1")
        checkpoint = checkpoint_message(compaction_id, str(uuid4()), result["content"])
        after_messages = replace_compacted_messages(self.messages, covered, checkpoint)
        after = self.measure(prepared, after_messages)
        return {"kind": "compact", "compaction_id": compaction_id,
                "before_message_ids": [item["message_id"] for item in self.messages],
                "covered_message_ids": covered, "checkpoint": checkpoint,
                "after_message_ids": [item["message_id"] for item in after_messages],
                "request": request, "result": result, "capacity": {"before": before, "after": after},
                "trigger_decision": {"decision_id": str(uuid4()), "preparation": trigger, "discard": discard}}

    def prove_compaction(self, operation, **overrides):
        parameters = dict(snapshot=self.snapshot, owner=self.owner, model_facts=self.facts(),
                          project_messages=CanonicalModelAdapter._project,
                          resolve_transport=self.mock.resolve_transport)
        parameters.update(overrides)
        return validate_compaction_proof(operation, **parameters)

    def replay(self, operations, **overrides):
        parameters = dict(owner=self.owner, model_facts=self.facts(),
                          project_messages=CanonicalModelAdapter._project,
                          resolve_transport=self.mock.resolve_transport)
        parameters.update(overrides)
        return replay_model_context_operations(self.snapshot, operations, **parameters)

    def close(self):
        self.mock.close()


@pytest.fixture
def proof_host():
    host = ModelProofHarness()
    yield host
    host.close()


def test_snapshot2_keeps_exact_fixed_inputs_without_whole_run_parameters(proof_host):
    snapshot = validate_input_snapshot_v2(proof_host.snapshot)
    assert "model_parameters" not in snapshot
    assert set(snapshot["config"]["payload"]["context_compaction"]) == {"policy", "layout"}
    proof_host.mock.profile["value"]["runtime"]["output_limit_tokens"] = 1
    assert validate_input_snapshot_v2(snapshot) == snapshot
    snapshot["config"]["payload"]["public_model_binding"]["authorization"]["purposes"].clear()
    assert proof_host.snapshot["config"]["payload"]["public_model_binding"]["authorization"]["purposes"]


@pytest.mark.parametrize("mutation", [
    lambda snapshot: snapshot.update(schema_version=1),
    lambda snapshot: snapshot.update(model_parameters={"model": "old"}),
    lambda snapshot: snapshot["config"]["payload"]["context_compaction"].update(context_window_tokens=400000),
    lambda snapshot: snapshot["config"]["payload"].update(transport_projection=[]),
    lambda snapshot: snapshot["config"]["payload"]["context_compaction"]["policy"].update(trigger_tokens=0),
    lambda snapshot: snapshot["config"]["payload"]["executor_ref"].update(exact_version="1.0.0"),
])
def test_snapshot_rejects_previous_model_frame_and_versions(proof_host, mutation):
    snapshot = deepcopy(proof_host.snapshot)
    mutation(snapshot)
    with pytest.raises(ContractValidationError):
        validate_input_snapshot_v2(snapshot)


def test_request_proof_matches_saved_settings_exact_wire_estimate_result_and_reserved_ids(proof_host):
    messages = [text_message("one ordinary request")]
    prepared = proof_host.prepare("normal:1", messages)
    request = proof_host.request(prepared, messages)
    result = proof_host.mock.dispatch(prepared)
    proof = proof_host.prove(request)
    assert proof["result"] == result
    assert proof["settings"] == prepared["settings"]
    assert proof["measurement"]["measurement"] == prepared["measurement"]
    assert len(proof_host.mock.communications) == 1
    assert proof["facts"][0]["details"]["wire_request"] == proof_host.mock.communications[0]["prepared"]["wire_request"]


@pytest.mark.parametrize("mutation", [
    lambda request, facts: facts[0].update(node_run_id=str(uuid4())),
    lambda request, facts: facts[1].update(sequence=1),
    lambda request, facts: facts.reverse(),
    lambda request, facts: request["reserved_model_fact_ids"].update(outcome=str(uuid4())),
    lambda request, facts: request.update(settings_id=str(uuid4())),
    lambda request, facts: facts[0]["details"]["wire_request"].update(model="forged"),
    lambda request, facts: facts[0]["details"]["measurement"].update(input_tokens=1, total_tokens=32001),
    lambda request, facts: facts[2]["details"]["response"].update(settings_id=str(uuid4())),
    lambda request, facts: facts[2]["details"].update(attempt_id=str(uuid4())),
    lambda request, facts: request["messages"][0]["blocks"][0].update(text="changed intent"),
])
def test_request_proof_rejects_forged_identity_phase_wire_measurement_and_original_request(
        proof_host, mutation):
    messages = [text_message("ordinary request")]
    prepared = proof_host.prepare("normal:1", messages)
    request = proof_host.request(prepared, messages)
    proof_host.mock.dispatch(prepared)
    facts = proof_host.facts()[request["request_id"]]
    mutation(request, facts)
    with pytest.raises(ContractValidationError):
        proof_host.prove(request, facts)
    assert len(proof_host.mock.communications) == 1


def test_foreign_stream_and_input_artifact_claims_are_rejected(proof_host):
    messages = [text_message("request")]
    prepared = proof_host.prepare("normal:1", messages)
    request = proof_host.request(prepared, messages)
    proof_host.mock.dispatch(prepared)
    for extra in ({"stream_id": model_request_stream_id(str(uuid4()))},
                  {"input_refs": {"model": [{"output_id": str(uuid4())}]}}):
        with pytest.raises(ContractValidationError):
            proof_host.prove(request, **extra)


def test_terminal_unknown_proof_preserves_uncertainty_without_inventing_response(proof_host):
    messages = [text_message("request")]
    prepared = proof_host.prepare("normal:1", messages)
    request = proof_host.request(prepared, messages)
    proof_host.mock.transport_error = TimeoutError("uncertain")
    with pytest.raises(ContractValidationError):
        proof_host.mock.dispatch(prepared)
    proof = proof_host.prove(request)
    assert proof["classification"] == "dispatch_unknown" and proof["result"] is None
    assert len(proof_host.mock.communications) == 1


def test_compaction_uses_original_trigger_new_summary_settings_and_next_normal_latest_settings(proof_host):
    operation = proof_host.compact()
    assert proof_host.prove_compaction(operation) == operation
    trigger = operation["trigger_decision"]["preparation"]
    summary = proof_host.prove(operation["request"])["settings"]
    assert trigger["settings"]["configuration"]["update_sequence"] == 1
    assert summary["configuration"]["update_sequence"] == 2
    assert trigger["settings"]["budget"]["normal_output_limit_tokens"] == 32000
    assert summary["budget"]["normal_output_limit_tokens"] == 16000
    assert operation["capacity"]["before"]["settings_id"] == operation["capacity"]["after"]["settings_id"]
    assert operation["capacity"]["after"]["measurement"]["output_reserve_tokens"] == 16000
    replay = proof_host.replay([operation])
    proof_host.mock.profile["update_sequence"] += 1
    proof_host.mock.profile["value"]["runtime"]["output_limit_tokens"] = 8000
    normal2 = proof_host.prepare("normal:2", replay["messages"])
    request2 = proof_host.request(normal2, replay["messages"])
    proof_host.mock.response = None
    proof_host.mock.dispatch(normal2, key="normal:2")
    assert proof_host.prove(request2)["settings"]["configuration"]["update_sequence"] == 3
    assert normal2["measurement"]["output_reserve_tokens"] == 8000
    assert len(proof_host.mock.communications) == 2


@pytest.mark.parametrize("mutation", [
    lambda operation: operation["trigger_decision"]["discard"].update(request_id=str(uuid4())),
    lambda operation: operation["trigger_decision"]["preparation"].update(requires_compaction=False),
    lambda operation: operation["request"]["messages"][-1]["blocks"][0].update(text="forged instruction"),
    lambda operation: operation["capacity"]["before"].update(settings_id=str(uuid4())),
    lambda operation: operation["capacity"]["after"]["projection"].update(projection_version="different@1"),
    lambda operation: operation["capacity"]["after"]["measurement"].update(estimator_version="2"),
    lambda operation: operation["checkpoint"]["blocks"][0].update(text="invented summary"),
    lambda operation: operation.update(covered_message_ids=[operation["before_message_ids"][-1]]),
    lambda operation: operation["result"].update(content="changed accepted summary"),
])
def test_compaction_rejects_changed_trigger_request_measurement_coverage_and_result(proof_host, mutation):
    operation = proof_host.compact()
    mutation(operation)
    with pytest.raises(ContractValidationError):
        proof_host.prove_compaction(operation)
    assert len(proof_host.mock.communications) == 1


def test_compaction_replay_never_reads_current_resources_credentials_or_transport(proof_host):
    operation = proof_host.compact()
    snapshot = (len(proof_host.mock.reads), len(proof_host.mock.checks),
                len(proof_host.mock.broker.authorize_calls), len(proof_host.mock.created),
                len(proof_host.mock.communications), len(proof_host.mock.accepted))
    proof_host.mock.profile.clear()
    proof_host.mock.provider.clear()
    proof_host.mock.broker.values.clear()
    replay = proof_host.replay([operation])
    assert [message["message_id"] for message in replay["messages"]] == operation["after_message_ids"]
    assert replay["compaction_ids"] == [operation["compaction_id"]]
    assert snapshot == (len(proof_host.mock.reads), len(proof_host.mock.checks),
                        len(proof_host.mock.broker.authorize_calls), len(proof_host.mock.created),
                        len(proof_host.mock.communications), len(proof_host.mock.accepted))


def test_replay_rejects_duplicate_original_append_and_forged_append_receipts(proof_host):
    operation = proof_host.compact()
    with pytest.raises(ContractValidationError):
        proof_host.replay([operation, {"kind": "append", "message": proof_host.messages[0]}])
    new = text_message("model increment", role="assistant",
                       source={"kind": "model", "request_id": operation["request"]["request_id"]})
    with pytest.raises(ContractValidationError):
        proof_host.replay([operation, {"kind": "append", "message": new}], accepted_messages=[])


def test_policy_preserves_fixed_current_recent_and_protocol_messages(proof_host):
    snapshot = deepcopy(proof_host.snapshot)
    snapshot["config"]["payload"]["context_compaction"]["policy"]["keep_depth"] = 1
    assert compaction_eligible_message_ids(snapshot, snapshot["s0"]) == []
    snapshot["config"]["payload"]["context_compaction"]["policy"]["keep_depth"] = 0
    snapshot["config"]["payload"]["context_compaction"]["layout"][0]["compaction"] = "never"
    assert compaction_eligible_message_ids(snapshot, snapshot["s0"]) == []


def test_forged_generated_increment_cannot_make_current_input_eligible(proof_host):
    claimed = text_message("forged model text", role="assistant",
                           source={"kind": "model", "request_id": str(uuid4())})
    claimed["message_id"] = proof_host.messages[-1]["message_id"]
    with pytest.raises(ContractValidationError):
        compaction_eligible_message_ids(proof_host.snapshot, proof_host.messages, [claimed])
    with pytest.raises(ContractValidationError):
        compaction_eligible_message_ids(proof_host.snapshot, proof_host.messages, [proof_host.messages[-1]])


def test_summary_request_without_tools_still_measures_normal_tools_for_context_proof():
    host = ModelProofHarness(tools=[{
        "name": "inspect_text", "version": "1", "description": "Inspect text.",
        "parameters_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }])
    try:
        operation = host.compact()
        assert operation["request"]["tools"] == []
        assert host.prove_compaction(operation) == operation
        assert host.mock.communications[0]["prepared"]["wire_request"]["tools"] == []
        before_measurement = operation["capacity"]["before"]
        no_tools_before = host.mock.measure(
            {"preparation_id": host.facts()[operation["request"]["request_id"]][0]["details"]["preparation_id"]},
            CanonicalModelAdapter._project(host.messages))
        assert before_measurement["tools_digest"] != no_tools_before["tools_digest"]
        assert before_measurement["measurement"]["input_tokens"] > no_tools_before["measurement"]["input_tokens"]
    finally:
        host.close()


def test_low_trigger_is_not_an_implicit_post_summary_target(proof_host):
    proof_host.mock.profile["value"]["runtime"]["context_length_tokens"] = 1
    operation = proof_host.compact()
    proof = proof_host.prove(operation["request"])
    assert operation["capacity"]["after"]["measurement"]["total_tokens"] > proof["settings"]["budget"]["trigger_total_tokens"]
    assert operation["capacity"]["after"]["hard_capacity_ok"]
    assert proof_host.prove_compaction(operation) == operation


def test_provided_estimator_is_replayed_with_its_exact_identity_and_counter(proof_host):
    proof_host.mock.counter = len
    proof_host.mock.estimator_id, proof_host.mock.estimator_version = "synthetic-wire-bytes", "1"
    operation = proof_host.compact()
    assert operation["capacity"]["after"]["measurement"]["token_count_kind"] == "provided"
    assert proof_host.prove_compaction(operation) == operation
    proof_host.mock.estimator_version = "2"
    with pytest.raises(ContractValidationError):
        proof_host.prove_compaction(operation)


def receipt_fixture(host):
    payload = host.snapshot["config"]["payload"]
    return {"schema_version": 2, "kind": "workflow.agent-receipts", "owner": host.owner,
            "executor_ref": deepcopy(AGENT_EXECUTOR_REF_V2), "snapshot_id": host.snapshot["snapshot_id"],
            "frozen_prompt_ref": payload["frozen_prompt_ref"], "model_ref": payload["model_ref"],
            "compaction_policy_ref": payload["frozen_compaction_policy_ref"],
            "unit_id": str(uuid4()), "fact_ids": [str(uuid4())], "model_requests": [{
                "request_id": request["request_id"], "settings_id": request["settings_id"],
                "fact_ids": [request["reserved_model_fact_ids"][stage] for stage in ("request", "attempt", "outcome")],
            } for request in host.requests]}


def test_receipts2_list_only_exact_accepted_model_requests_and_contain_no_private_fields(proof_host):
    operation = proof_host.compact()
    receipt = receipt_fixture(proof_host)
    assert validate_agent_receipts_v2(receipt) == receipt
    assert receipt["model_requests"][0]["fact_ids"] == [ref["fact_id"] for ref in operation["result"]["fact_refs"]]
    public = canonical_bytes([proof_host.snapshot, receipt, operation, proof_host.prove(operation["request"])])
    for forbidden in (b"synthetic-private-key", b"credential_ref", b"base_url", b"lease"):
        assert forbidden not in public
    duplicated = deepcopy(receipt)
    duplicated["model_requests"].append(deepcopy(duplicated["model_requests"][0]))
    with pytest.raises(ContractValidationError):
        validate_agent_receipts_v2(duplicated)
    old = deepcopy(receipt)
    old["schema_version"] = 1
    with pytest.raises(ContractValidationError):
        validate_agent_receipts_v2(old)
