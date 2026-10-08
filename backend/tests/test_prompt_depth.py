"""Old logical-floor placement is authoritative across current prompt contracts."""

from copy import deepcopy

import pytest

from phase1_agent.content_contracts import (
    default_presentation, make_prompt_item, prompt_content, text_content,
)
from phase1_agent.context_prompt_v6 import _assemble, assemble_native_context_prompt, validate_native_context_prompt
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.prompt_assembly import assemble_prompt_collection
from phase1_agent.prompt_contract import _build_assembly, assemble_prompt, validate_ready_prompt
from phase1_agent.prompt_depth import LOGICAL_FLOOR_PLACEMENT
from phase1_agent.prompt_lifecycle import lifecycle_prompt_content, make_lifecycle_prompt_item

from test_prompt_lifecycle import ref, uid
from test_prompt_assembly import collection, item as old_item, message
from test_prompt_lifecycle import history_text, view as native_view


def material(depth, *, lifecycle=False, number=1, text="M", **changes):
    presentation = {**default_presentation(), "placement": "middle", "depth": depth, **changes}
    make = make_lifecycle_prompt_item if lifecycle else make_prompt_item
    return make(uid(700), uid(number), text, presentation)


def assemble(version, depth, *, materials=None):
    members = [material(depth, lifecycle=version == 6)] if materials is None else materials
    refs = {"context_ref": ref(900), "current_input_ref": ref(901)}
    if version == 6:
        messages = [history_text(index, role) for index, role in (
            (10, "user"), (11, "assistant"), (12, "user"), (13, "assistant"))]
        for entry, text in zip(messages, ("U1", "A1", "U2", "A2")):
            entry["blocks"][0]["text"] = text
        layout = [{"kind": "history", "round_id": uid(50 + index // 2), "compaction": "allowed"}
                  for index in range(4)]
        return assemble_native_context_prompt(
            [lifecycle_prompt_content(members)], native_view(messages, layout), text_content("U3"), **refs)


def texts(prompt):
    return [entry["blocks"][0]["text"] if "blocks" in entry else entry["content"]
            for entry in prompt["messages"]]


@pytest.mark.parametrize("version", [6])
@pytest.mark.parametrize("depth", [0, 1, 2, 3, 4, 5, 20, 99])
def test_current_context_depth_matches_old_root_delta_floors(version, depth):
    original = [message(index, source, text) for index, source, text in (
        (20, "human", "U1"), (21, "model", "A1"), (22, "human", "U2"),
        (23, "model", "A2"), (24, "human", "U3"))]
    old = assemble_prompt_collection(
        collection(old_item(depth=depth, text="M")), original,
        logical_floors=[[entry["message_id"]] for entry in original], prompt_message_ids=[uid(500)])
    result = assemble(version, depth)
    assert texts(result) == [entry["blocks"][0]["text"] for entry in old["messages"]]
    assert result["placement_profile"] == LOGICAL_FLOOR_PLACEMENT
    assert validate_native_context_prompt(result) == result


@pytest.mark.parametrize("version", [6])
def test_clamped_depths_share_boundary_and_use_order_before_original_item_order(version):
    result = assemble(version, 0, materials=[
        material(20, lifecycle=version == 6, number=1, text="later", order=10),
        material(99, lifecycle=version == 6, number=2, text="earlier", order=-10),
        material(0, lifecycle=version == 6, number=3, text="after-current"),
    ])
    assert texts(result) == ["earlier", "later", "U1", "A1", "U2", "A2", "U3", "after-current"]


@pytest.mark.parametrize("version", [6])
def test_disabled_material_does_not_participate_in_depth_placement(version):
    assert texts(assemble(version, 0, materials=[
        material(99, lifecycle=version == 6, enabled=False)])) == ["U1", "A1", "U2", "A2", "U3"]


@pytest.mark.parametrize("version", [6])
@pytest.mark.parametrize("profile", [None, False, "rounds", {}])
def test_unknown_placement_profile_is_rejected(version, profile):
    result = assemble(version, 0)
    result["placement_profile"] = profile
    with pytest.raises(ContractValidationError):
        validate_native_context_prompt(result)


@pytest.mark.parametrize("version", [6])
def test_frozen_round_based_prompt_remains_readable_without_rewriting_projection(version):
    result = assemble(version, 1)
    del result["placement_profile"]
    if version == 6:
        projection = _assemble(result["items"], result["context"], result["current_input"],
                               logical_floors=False)
        for key, value in zip(("messages", "provenance", "layout", "once_pending_item_ids"), projection):
            result[key] = value
    before = deepcopy(result)
    assert texts(result) == ["U1", "A1", "M", "U2", "A2", "U3"]
    assert validate_native_context_prompt(result) == before
    assert result == before
    result["placement_profile"] = LOGICAL_FLOOR_PLACEMENT
    with pytest.raises(ContractValidationError):
        validate_native_context_prompt(result)


@pytest.mark.parametrize("version", [6])
def test_removing_profile_cannot_reinterpret_new_depth_zero_projection(version):
    result = assemble(version, 0)
    del result["placement_profile"]
    with pytest.raises(ContractValidationError):
        validate_native_context_prompt(result)


def test_material_only_prompt_depth_zero_follows_current_input_and_old_freeze_is_readable():
    member = material(0)
    result = assemble_prompt([prompt_content([member])], text_content("U3"))
    assert result["assembly"]["messages"] == [
        {"role": "user", "content": "U3"}, {"role": "system", "content": "M"}]
    assert validate_ready_prompt(result) == result
    legacy = deepcopy(result)
    legacy["assembly"] = _build_assembly(
        legacy["items"], legacy["assembly"]["current_input"], source_output_refs=[],
        current_input_refs=[], logical_floors=False)
    assert validate_ready_prompt(legacy) == legacy
    assert legacy["assembly"]["messages"][0]["content"] == "M"


@pytest.mark.parametrize("depth", [0, 1, 20])
def test_empty_explicit_context_uses_current_root_and_clamps_excess_depth(depth):
    result = assemble_native_context_prompt(
        [lifecycle_prompt_content([material(depth, lifecycle=True)])], native_view(), text_content("U3"),
        context_ref=ref(900), current_input_ref=ref(901))
    assert texts(result) == (["U3", "M"] if depth == 0 else ["M", "U3"])


@pytest.mark.parametrize("depth,expected", [
    (0, ["checkpoint", "once", "U1", "A1", "U2", "M"]),
    (1, ["checkpoint", "once", "U1", "A1", "M", "U2"]),
    (2, ["checkpoint", "once", "U1", "M", "A1", "U2"]),
    (3, ["M", "checkpoint", "once", "U1", "A1", "U2"]),
])
def test_checkpoint_and_once_background_do_not_add_conversation_floors(depth, expected):
    checkpoint = {"schema_version": 4, "message_id": uid(5), "role": "user",
                  "source": {"kind": "context_checkpoint", "compaction_id": uid(6)},
                  "blocks": [{"kind": "text", "text": "checkpoint"}]}
    messages = [checkpoint, history_text(7), history_text(10), history_text(11, "assistant")]
    for entry, text in zip(messages, ("checkpoint", "once", "U1", "A1")):
        entry["blocks"][0]["text"] = text
    initial = native_view(messages, [
        {"kind": "summary", "round_id": None, "compaction": "allowed"},
        {"kind": "once", "round_id": uid(49), "compaction": "allowed"},
        {"kind": "history", "round_id": uid(50), "compaction": "allowed"},
        {"kind": "history", "round_id": uid(50), "compaction": "allowed"},
    ])
    result = assemble_native_context_prompt(
        [lifecycle_prompt_content([material(depth, lifecycle=True)])], initial, text_content("U2"),
        context_ref=ref(900), current_input_ref=ref(901))
    assert texts(result) == expected
    assert result["context"] == initial


def test_multiple_model_messages_in_one_delta_remain_a_single_floor():
    messages = [history_text(10), history_text(11, "assistant"), history_text(12, "assistant")]
    initial = native_view(messages, [
        {"kind": "history", "round_id": uid(50), "compaction": "allowed"} for _ in messages])
    result = assemble_native_context_prompt(
        [lifecycle_prompt_content([material(2, lifecycle=True)])], initial, text_content("U2"),
        context_ref=ref(900), current_input_ref=ref(901))
    assert texts(result) == ["10", "M", "11", "12", "U2"]
