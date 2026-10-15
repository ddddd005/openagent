"""Logical chat values preserve all candidates without retained artifact dependencies."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.chat_contracts import (
    CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION, CHAT_CONTEXT_MAX_BYTES,
    append_chat_candidate, bind_chat_candidate_checkpoint, chat_context_data_type,
    empty_chat_context, remap_chat_context_checkpoints, validate_chat_context,
    validate_chat_context_write,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.host_sdk import TypeRegistry


def message(text="answer", *, role="assistant"):
    return {"message_id": str(uuid4()), "role": role, "text": text}


def chat_value(*, count=1, submitted_input="same user message", base=None):
    value, round_id = empty_chat_context(), str(uuid4())
    for index in range(count):
        value = append_chat_candidate(
            value, round_id=round_id, base_checkpoint_id=base, submitted_input=submitted_input,
            candidate_id=str(uuid4()), messages=[message("answer " + str(index))],
        )
    return value


def test_type_is_exact_session_admitted_inline_state_and_has_detached_empty_defaults():
    types = TypeRegistry()
    types.register(chat_context_data_type())
    definition = types.get(CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION, scope="session")
    assert definition.checkpoint_admission and definition.max_bytes == CHAT_CONTEXT_MAX_BYTES == 4_000_000
    assert definition.references is None and definition.reference_mapper is None
    assert types.default(CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION) == {"rounds": []}
    empty = empty_chat_context()
    empty["rounds"].append("outside modification")
    assert empty_chat_context() == {"rounds": []}
    value = chat_value(count=5)
    assert types.validate(CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION, value) == value
    assert types.references(CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION, value) == []
    checked = validate_chat_context(value)
    checked["rounds"][0]["candidates"][0]["messages"][0]["text"] = "outside modification"
    assert value["rounds"][0]["candidates"][0]["messages"][0]["text"] == "answer 0"


def test_candidates_keep_the_same_input_all_messages_and_stable_selected_identity():
    value = chat_value(count=5, submitted_input="")
    round_value = value["rounds"][0]
    assert round_value["submitted_input"] == ""
    assert len(round_value["candidates"]) == 5
    assert round_value["selected_candidate_id"] == round_value["candidates"][-1]["candidate_id"]
    selected = deepcopy(value)
    selected["rounds"][0]["selected_candidate_id"] = round_value["candidates"][0]["candidate_id"]
    assert validate_chat_context(selected) == selected
    assert len(selected["rounds"][0]["candidates"]) == 5
    assert selected["rounds"][0]["candidates"] == value["rounds"][0]["candidates"]


def test_absent_user_input_and_empty_text_are_valid_for_existing_output_only_graphs():
    value = append_chat_candidate(
        empty_chat_context(), round_id=str(uuid4()), base_checkpoint_id=None, submitted_input=None,
        candidate_id=str(uuid4()), messages=[message("", role="system")], select=False,
    )
    assert value["rounds"][0]["submitted_input"] is None
    assert value["rounds"][0]["selected_candidate_id"] is None
    assert value["rounds"][0]["candidates"][0]["messages"][0]["text"] == ""


def test_success_binding_is_explicit_exact_and_does_not_change_message_bodies():
    value = chat_value(count=2)
    original = deepcopy(value)
    round_value = value["rounds"][0]
    candidate_id = round_value["candidates"][0]["candidate_id"]
    point = str(uuid4())
    bound = bind_chat_candidate_checkpoint(
        value, round_id=round_value["round_id"], candidate_id=candidate_id, checkpoint_id=point)
    assert bound["rounds"][0]["candidates"][0]["result_checkpoint_id"] == point
    assert bound["rounds"][0]["candidates"][0]["messages"] == round_value["candidates"][0]["messages"]
    assert bound["rounds"][0]["candidates"][1] == round_value["candidates"][1]
    assert value == original
    assert bind_chat_candidate_checkpoint(
        bound, round_id=round_value["round_id"], candidate_id=candidate_id, checkpoint_id=point) == bound
    with pytest.raises(ContractValidationError) as caught:
        bind_chat_candidate_checkpoint(
            bound, round_id=round_value["round_id"], candidate_id=candidate_id,
            checkpoint_id=str(uuid4()))
    assert caught.value.reason_code == "chat_candidate_checkpoint_conflict"


@pytest.mark.parametrize("mutation", [
    "extra_root", "extra_round", "extra_candidate", "source_ref", "view_ref",
    "round_id", "duplicate_round", "candidate_id", "duplicate_candidate", "message_id",
    "duplicate_message", "role", "text", "base", "result", "input", "selection", "empty_messages",
])
def test_inexact_ambiguous_or_reference_bearing_values_are_rejected(mutation):
    value = chat_value(count=2)
    round_value = value["rounds"][0]
    candidate = round_value["candidates"][0]
    entry = candidate["messages"][0]
    if mutation == "extra_root":
        value["contexts"] = {}
    elif mutation == "extra_round":
        round_value["compute_state"] = {}
    elif mutation == "extra_candidate":
        candidate["variables"] = {}
    elif mutation in ("source_ref", "view_ref"):
        entry[mutation] = {"scope": "artifact", "output_id": str(uuid4())}
    elif mutation == "round_id":
        round_value["round_id"] = "not-a-round"
    elif mutation == "duplicate_round":
        value["rounds"].append(deepcopy(round_value))
    elif mutation == "candidate_id":
        candidate["candidate_id"] = True
    elif mutation == "duplicate_candidate":
        round_value["candidates"][1]["candidate_id"] = candidate["candidate_id"]
    elif mutation == "message_id":
        entry["message_id"] = None
    elif mutation == "duplicate_message":
        round_value["candidates"][1]["messages"][0]["message_id"] = entry["message_id"]
    elif mutation == "role":
        entry["role"] = "tool"
    elif mutation == "text":
        entry["text"] = {}
    elif mutation == "base":
        round_value["base_checkpoint_id"] = False
    elif mutation == "result":
        candidate["result_checkpoint_id"] = "not-a-point"
    elif mutation == "input":
        round_value["submitted_input"] = []
    elif mutation == "selection":
        round_value["selected_candidate_id"] = str(uuid4())
    else:
        candidate["messages"] = []
    with pytest.raises(ContractValidationError):
        validate_chat_context(value)


def test_chat_size_uses_the_existing_generic_four_megabyte_budget():
    value = chat_value()
    value["rounds"][0]["candidates"][0]["messages"][0]["text"] = "x" * 1_100_000
    validate_chat_context(value)
    value["rounds"][0]["candidates"][0]["messages"][0]["text"] = "x" * CHAT_CONTEXT_MAX_BYTES
    with pytest.raises(ContractValidationError) as caught:
        validate_chat_context(value)
    assert caught.value.reason_code == "host_value_too_large"


def test_default_writes_deny_unproven_put_delete_and_nonempty_initialization():
    validate_chat_context_write(empty_chat_context(), {"operation": "initialize"})
    for value, operation in ((chat_value(), "initialize"), (empty_chat_context(), "put"), (None, "delete")):
        with pytest.raises(ContractValidationError) as caught:
            validate_chat_context_write(value, {"operation": operation})
        assert caught.value.reason_code == "chat_write_evidence_required"
    supplied = lambda value, context: None
    assert chat_context_data_type(write_validator=supplied).write_validator is supplied


def test_append_rejects_changed_input_duplicate_candidate_and_a_round_with_later_rounds():
    value = chat_value()
    round_value = value["rounds"][0]
    parameters = {
        "round_id": round_value["round_id"], "base_checkpoint_id": None,
        "submitted_input": round_value["submitted_input"], "candidate_id": str(uuid4()),
        "messages": [message()],
    }
    with pytest.raises(ContractValidationError) as caught:
        append_chat_candidate(value, **{**parameters, "submitted_input": "different user message"})
    assert caught.value.reason_code == "chat_round_basis_mismatch"
    with pytest.raises(ContractValidationError):
        append_chat_candidate(value, **{
            **parameters, "candidate_id": round_value["candidates"][0]["candidate_id"],
        })
    later = append_chat_candidate(value, **{
        **parameters, "round_id": str(uuid4()), "submitted_input": "next user message",
    })
    with pytest.raises(ContractValidationError) as caught:
        append_chat_candidate(later, **parameters)
    assert caught.value.reason_code == "chat_round_locked"


def test_fork_remaps_only_checkpoint_associations_and_rejects_unmapped_saved_results():
    before, point, target_point = str(uuid4()), str(uuid4()), str(uuid4())
    value = chat_value(base=before)
    round_value = value["rounds"][0]
    value = bind_chat_candidate_checkpoint(
        value, round_id=round_value["round_id"],
        candidate_id=round_value["candidates"][0]["candidate_id"], checkpoint_id=point)
    frozen = deepcopy(value)
    mapped = remap_chat_context_checkpoints(value, {before: target_point, point: target_point})
    assert mapped["rounds"][0]["base_checkpoint_id"] == target_point
    assert mapped["rounds"][0]["candidates"][0]["result_checkpoint_id"] == target_point
    mapped["rounds"][0]["base_checkpoint_id"] = before
    mapped["rounds"][0]["candidates"][0]["result_checkpoint_id"] = point
    assert mapped == frozen == value
    with pytest.raises(ContractValidationError) as caught:
        remap_chat_context_checkpoints(value, {before: target_point})
    assert caught.value.reason_code == "chat_checkpoint_mapping_missing"
