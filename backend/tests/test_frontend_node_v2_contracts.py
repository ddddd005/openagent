"""Logical chat appends retain exact execution evidence outside stored values."""

from copy import deepcopy

import pytest

from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE, empty_chat_context
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_v2_contracts import (
    build_frontend_node_chat_append, frontend_node_chat_append_data_type,
    prepare_frontend_node_chat_adoption, validate_frontend_node_chat_append,
    validate_frontend_node_chat_write,
)
from phase1_agent.host_sdk import ObjectBinding
from frontend_node_test_support import binding, ref, uid


def proof_fixture():
    current = {
        "type_id": CHAT_CONTEXT_TYPE, "schema_version": 1, "deleted": False,
        "revision": 2, "revision_id": uid(11),
        "binding": ObjectBinding(
            "chat", CHAT_CONTEXT_TYPE, 1, "shared", readers=(uid(3),),
            writers=(uid(3), uid(7)), registrar_node_id=uid(3)).to_dict(),
        "value": empty_chat_context(),
    }
    metadata = {
        "round_id": uid(60), "base_checkpoint_id": None, "submitted_input": "original input",
        "candidate_id": uid(61),
    }
    proof = build_frontend_node_chat_append(
        current, workflow_session_id=uid(1), object_key="chat",
        source_output_refs=[binding(21), binding(22)], contents=[text_content("first"), text_content("")],
        role="assistant", node_run_id=uid(4), chat_append_context=metadata)
    prepared = prepare_frontend_node_chat_adoption(
        workflow_session_id=uid(1), object_key="chat", object_record=current,
        proof=proof, proof_ref=ref(30))
    artifacts = {
        uid(21): {"value": text_content("first")}, uid(22): {"value": text_content("")},
        uid(30): {
            "value": proof, "component_id": "frontend.node", "component_version": "2",
            "consumed_config": {"object_key": "chat", "role": "assistant"},
            "config": {"object_key": "stale", "role": "system"},
            "chat_append_context": deepcopy(metadata),
            "producer": {"workflow_session_id": uid(1), "chain_run_id": uid(2),
                         "node_binding_id": uid(3), "node_run_id": uid(4)},
            "input_refs": {"content": [binding(21), binding(22)]},
            "reads": [{"kind": "object_read", "object_key": "chat", "revision": 2, "revision_id": uid(11)}],
        },
    }
    context = {
        "workflow_session_id": uid(1), "object_key": "chat", "writer_node_id": uid(3),
        "current_record": current, "operation": "put", "write_evidence": prepared["write_evidence"],
        "resolve_artifact": lambda reference: deepcopy(artifacts[reference["output_id"]]),
    }
    return current, proof, prepared, artifacts, context


def test_inline_chat_value_keeps_sources_only_in_transient_execution_proof():
    current, proof, prepared, _, context = proof_fixture()
    value = prepared["desired_value"]
    assert value["rounds"][0]["submitted_input"] == "original input"
    assert [message["text"] for message in value["rounds"][0]["candidates"][0]["messages"]] == ["first", ""]
    assert value["rounds"][0]["candidates"][0]["result_checkpoint_id"] is None
    assert "source_ref" not in str(value) and "view_ref" not in str(value)
    assert "proof_ref" not in str(prepared["write_intent"])
    assert current["value"] == empty_chat_context()
    assert proof["source_output_refs"] == [binding(21), binding(22)]
    definition = frontend_node_chat_append_data_type()
    assert definition.schema_version == 2
    assert definition.references(proof) == [ref(21), ref(22)]
    assert definition.artifact_bindings(proof) == [binding(21), binding(22)]
    validate_frontend_node_chat_write(value, context)


@pytest.mark.parametrize("mutate", [
    lambda source: source.update(component_version="1"),
    lambda source: source.update(component_id="fake.node"),
    lambda source: source["consumed_config"].update(object_key="foreign"),
    lambda source: source["consumed_config"].update(role="user"),
    lambda source: source["producer"].update(node_binding_id=uid(7)),
    lambda source: source["producer"].update(node_run_id=uid(7)),
    lambda source: source["producer"].update(workflow_session_id=uid(7)),
    lambda source: source["input_refs"].update(content=[binding(21)]),
    lambda source: source.update(reads=[]),
    lambda source: source.pop("chat_append_context"),
    lambda source: source["chat_append_context"].update(round_id=uid(90)),
    lambda source: source["chat_append_context"].update(candidate_id=uid(90)),
    lambda source: source["chat_append_context"].update(submitted_input="changed input"),
    lambda source: source["value"]["messages"][0].update(text="fabricated"),
    lambda source: source["value"]["messages"][0].update(message_id=uid(90)),
    lambda source: source["value"]["source_output_refs"].reverse(),
])
def test_proof_rejects_forged_invocation_round_sources_and_body(mutate):
    _, _, prepared, artifacts, context = proof_fixture()
    mutate(artifacts[uid(30)])
    with pytest.raises(ContractValidationError):
        validate_frontend_node_chat_write(prepared["desired_value"], context)


@pytest.mark.parametrize("mutation", ["missing_evidence", "owner", "registrar", "writer", "basis", "body", "source"])
def test_proof_rejects_missing_evidence_wrong_owner_or_changed_store_and_sources(mutation):
    _, _, prepared, artifacts, context = proof_fixture()
    if mutation == "missing_evidence":
        context.pop("write_evidence")
    elif mutation == "owner":
        context["object_key"] = "foreign"
    elif mutation == "registrar":
        context["current_record"]["binding"]["registrar_node_id"] = uid(7)
    elif mutation == "writer":
        context["writer_node_id"] = uid(7)
    elif mutation == "basis":
        context["current_record"]["revision"] = 9
    elif mutation == "body":
        prepared["desired_value"]["rounds"][0]["candidates"][0]["messages"][0]["text"] = "changed"
    else:
        artifacts[uid(21)]["value"] = text_content("changed")
    with pytest.raises(ContractValidationError):
        validate_frontend_node_chat_write(prepared["desired_value"], context)


def test_exact_adoption_does_not_create_a_new_round_or_drop_previous_candidates():
    current, proof, prepared, _, _ = proof_fixture()
    current["value"] = prepared["desired_value"]
    current["revision"] += 1
    next_proof = build_frontend_node_chat_append(
        current, workflow_session_id=uid(1), object_key="chat", source_output_refs=[binding(23)],
        contents=[text_content("second reply")], role="assistant", node_run_id=uid(5),
        chat_append_context={**proof["round"], "candidate_id": uid(62)})
    next_value = prepare_frontend_node_chat_adoption(
        workflow_session_id=uid(1), object_key="chat", object_record=current,
        proof=next_proof, proof_ref=ref(31))["desired_value"]
    assert len(next_value["rounds"]) == 1
    assert [candidate["candidate_id"] for candidate in next_value["rounds"][0]["candidates"]] == [uid(61), uid(62)]
    assert next_value["rounds"][0]["selected_candidate_id"] == uid(62)
    assert current["value"] == prepared["desired_value"]


@pytest.mark.parametrize("mutate", [
    lambda proof: proof.update(schema_version=True),
    lambda proof: proof.update(extra=True),
    lambda proof: proof["round"].update(extra=True),
    lambda proof: proof["round"].update(round_id="not-a-uuid"),
    lambda proof: proof["round"].update(submitted_input={}),
    lambda proof: proof["messages"][0].update(source_ref=ref(21)),
    lambda proof: proof["source_output_refs"][0].update(order=True),
    lambda proof: proof.update(source_output_refs=[]),
])
def test_append_proof_rejects_unrecognized_or_inexact_fields(mutate):
    _, proof, _, _, _ = proof_fixture()
    mutate(proof)
    with pytest.raises(ContractValidationError):
        validate_frontend_node_chat_append(proof)
