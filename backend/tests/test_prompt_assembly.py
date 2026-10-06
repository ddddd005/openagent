"""Pure prompt placement and exact, protocol-safe message/source evidence."""

from __future__ import annotations

import copy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, content_digest
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.prompt_assembly import (
    PromptAssemblyLimits,
    assemble_prompt_collection,
    message_content_chars,
    validate_prompt_assembly,
)
from phase1_agent.prompt_errors import PromptProcessingError


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def item(number=1, **changes):
    value = {
        "item_id": uid(number), "revision": 2, "name": "title is not body",
        "text": "prompt", "role": "system", "enabled": True, "placement": "middle",
        "depth": 0, "order": 0, "interpolation": "variables",
        "source": {"kind": "configuration"}, "item_instance_id": uid(number + 100),
        "group_id": None, "group_revision": None, "group_instance_id": None,
        "input_name": "configured", "declaration_index": 0,
    }
    value.update(changes)
    return value


def collection(*items, version=1):
    return {"schema_version": version, "kind": "prompt_collection", "items": list(items)}


def message(number, source="human", text="root"):
    if source in ("human", "upstream_node"):
        return {
            "schema_version": 1, "message_id": uid(number), "role": "user",
            "source": (
                {"kind": "human", "visible_message_id": uid(number + 1000)}
                if source == "human" else {"kind": "upstream_node", "output_id": uid(number + 1000)}
            ),
            "blocks": [{"kind": "text", "text": text}],
        }
    return {
        "schema_version": 1, "message_id": uid(number),
        "role": "assistant" if source == "model" else "user",
        "source": {"kind": source, "request_id": uid(number + 1000)},
        "blocks": [{"kind": "text", "text": text}],
    }


def tool_batch():
    call = message(21, "model", "ordinary model text")
    call["blocks"].extend([{
        "kind": "tool_call", "tool_call_id": uid(200 + index), "tool_name": "inspect",
        "tool_definition_version": "1", "raw_arguments": "{}", "parsed_arguments": {},
    } for index in range(2)])
    results = [{
        "schema_version": 1, "message_id": uid(22 + index), "role": "tool",
        "source": {"kind": "tool", "tool_execution_id": uid(300 + index)},
        "blocks": [{
            "kind": "tool_result", "status": "success", "is_error": False,
            "tool_call_id": uid(200 + index), "tool_execution_id": uid(300 + index),
            "content": {"result": index}, "model_visible_text": f"result {index}",
        }],
    } for index in range(2)]
    return [call, *results, message(24, "model", '{"answer":"saved final"}')]


def history():
    return [message(20), *tool_batch(), message(25, "upstream_node")]


def floors(messages=None):
    messages = history() if messages is None else messages
    if len(messages) == 1:
        return [[messages[0]["message_id"]]]
    return [[uid(20)], [uid(21), uid(22), uid(23), uid(24)], [uid(25)]]


def assemble(items=(), messages=None, logical_floors=None, **kwargs):
    messages = [message(20)] if messages is None else messages
    return assemble_prompt_collection(
        collection(*items), messages,
        logical_floors=floors(messages) if logical_floors is None else logical_floors,
        prompt_message_ids=[uid(500 + index) for index in range(len(items))], **kwargs,
    )


def ids(result):
    return [value["message_id"] for value in result["messages"]]


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
@pytest.mark.parametrize("placement,depth", [("before", None), ("middle", 0), ("after", None)])
def test_three_roles_and_regions_emit_text_only_v3_prompt_provenance(role, placement, depth):
    result = assemble([item(role=role, placement=placement, depth=depth)])
    emitted = next(value for value in result["messages"] if value["source"]["kind"] == "prompt")
    assert emitted == validate_record("agent_message", {
        "schema_version": 3, "message_id": uid(500), "role": role,
        "source": {
            "kind": "prompt", "prompt_id": uid(1), "revision": 2,
            "item_instance_id": uid(101), "group_instance_id": None,
        },
        "blocks": [{"kind": "text", "text": "prompt"}],
    })
    assert "title is not body" not in emitted["blocks"][0]["text"]
    assert result["manifest"]["collection"]["items"][0]["name"] == "title is not body"
    assert validate_prompt_assembly(result) == result


@pytest.mark.parametrize("depth,boundary,expected", [
    (0, 3, [20, 21, 22, 23, 24, 25, 500]),
    (1, 2, [20, 21, 22, 23, 24, 500, 25]),
    (2, 1, [20, 500, 21, 22, 23, 24, 25]),
    (3, 0, [500, 20, 21, 22, 23, 24, 25]),
    (99, 0, [500, 20, 21, 22, 23, 24, 25]),
])
def test_depth_crosses_complete_root_and_delta_floors_and_clamps(depth, boundary, expected):
    original = history()
    result = assemble([item(depth=depth)], original)
    assert ids(result) == [uid(number) for number in expected]
    assert result["manifest"]["entries"][0]["floor_boundary"] == boundary
    assert original == history()
    assert validate_prompt_assembly(result) == result


def test_before_and_after_stay_separate_from_middle_edge_even_with_extreme_order():
    result = assemble([
        item(1, placement="before", depth=None, order=100),
        item(2, depth=100, order=-100),
        item(3, placement="after", depth=None, order=-100),
        item(4, depth=0, order=100),
    ])
    assert ids(result) == [uid(500), uid(501), uid(20), uid(503), uid(502)]
    assert [entry["region"] for entry in result["manifest"]["entries"]] == [
        "before", "middle", "middle", "after",
    ]


def test_sorting_uses_order_then_global_collection_position_not_local_declaration_index():
    result = assemble([
        item(1, input_name="first", declaration_index=999, order=0),
        item(2, input_name="second", declaration_index=0, order=0),
        item(3, input_name="third", declaration_index=0, order=-1),
        item(4, input_name="fourth", declaration_index=1, order=0),
    ])
    assert ids(result) == [uid(20), uid(502), uid(500), uid(501), uid(503)]
    assert [entry["collection_index"] for entry in result["manifest"]["entries"]] == [2, 0, 1, 3]
    assert [entry["message_index"] for entry in result["manifest"]["entries"]] == [1, 2, 3, 4]


def test_reused_group_members_have_scoped_source_identity_and_distinct_message_ids():
    first = item(group_id=uid(80), group_revision=4, group_instance_id=uid(81))
    second = item(group_id=uid(80), group_revision=4, group_instance_id=uid(82))
    result = assemble([first, second])
    assert len(result["messages"]) == 3
    assert [m["source"]["group_instance_id"] for m in result["messages"][1:]] == [uid(81), uid(82)]
    assert result["manifest"]["collection"]["items"] == [first, second]


def test_macro_text_and_literal_text_are_not_processed_during_assembly():
    result = assemble([
        item(1, text="{{missing}}", interpolation="variables"),
        item(2, text=r"\{\{literal\}\}", interpolation="literal"),
    ])
    assert [m["blocks"][0]["text"] for m in result["messages"][1:]] == [
        "{{missing}}", r"\{\{literal\}\}",
    ]


def lorebook_item():
    digest = content_digest({"fixture": "activation"})
    return item(
        schema_version=2, kind="prompt_item",
        source={
            "kind": "lorebook", "book_id": uid(60), "book_revision": 3,
            "book_instance_id": uid(61), "entry_id": uid(62), "entry_revision": 4,
            "book_digest": digest,
            "activation": {
                "algorithm": "unicode-casefold-substring-v1", "unicode_version": "15.0.0",
                "node_id": uid(63), "view_digest": digest, "text_source": "derived",
                "message_id": uid(20), "block_index": 0, "text_digest": digest,
                "keyword_digest": digest,
            },
        },
    )


def test_lorebook_v2_emits_prompt_source_and_retains_full_activation_evidence_in_manifest():
    original = lorebook_item()
    result = assemble_prompt_collection(
        collection(original, version=2), [message(20)], logical_floors=[[uid(20)]],
        prompt_message_ids=[uid(500)],
    )
    emitted = result["messages"][-1]
    assert emitted["source"]["kind"] == "prompt"
    assert emitted["source"]["prompt_id"] == original["item_id"]
    assert result["manifest"]["collection"]["items"][0]["source"] == original["source"]
    assert validate_prompt_assembly(result) == result


def test_empty_collection_retains_exact_input_and_explicit_evidence():
    result = assemble([], history())
    assert result["messages"] == history()
    assert result["manifest"]["entries"] == []
    assert result["manifest"]["prompt_message_ids"] == []
    assert result["manifest"]["input_message_ids"] == [m["message_id"] for m in history()]
    assert validate_prompt_assembly(result) == result


def test_source_timestamps_same_text_and_final_representation_survive_without_deduplication():
    original = history()
    for value in original:
        value["created_at"] = "2026-10-01T00:00:00.000Z"
    original[-1]["blocks"][0]["text"] = "prompt"
    result = assemble([item(text="prompt")], original)
    by_id = {value["message_id"]: value for value in result["messages"]}
    assert [by_id[value["message_id"]] for value in original] == original
    assert by_id[uid(24)]["blocks"][0]["text"] == '{"answer":"saved final"}'
    assert by_id[uid(25)]["blocks"][0]["text"] == by_id[uid(500)]["blocks"][0]["text"]
    assert by_id[uid(25)]["source"]["kind"] == "upstream_node"
    assert by_id[uid(500)]["source"]["kind"] == "prompt"


def test_completed_tool_observations_and_protocol_feedback_remain_one_delta_floor():
    generated = tool_batch()[:1]
    generated[0]["blocks"] = generated[0]["blocks"][:2]
    content = {
        "reason_code": "never_started", "error": "not dispatched",
        "retry_guidance": "decide whether the operation is still needed",
    }
    generated.extend([{
        "schema_version": 2, "message_id": uid(22), "role": "tool",
        "source": {
            "kind": "runtime_tool_observation", "tool_call_id": uid(200),
            "tool_execution_id": None, "reason_code": "never_started",
        },
        "blocks": [{
            "kind": "tool_result", "status": "error", "is_error": True, "content": content,
            "tool_call_id": uid(200), "tool_execution_id": None,
            "model_visible_text": canonical_bytes(content).decode("utf-8"),
        }],
    }, message(23, "protocol_feedback", "runtime feedback"), message(24, "model")])
    original = [message(20), *generated, message(25)]
    result = assemble([item(depth=2)], original)
    assert ids(result) == [uid(20), uid(500), uid(21), uid(22), uid(23), uid(24), uid(25)]
    assert validate_prompt_assembly(result) == result


def test_result_and_revalidated_result_are_detached_from_every_input():
    original_collection = collection(item())
    original_messages = history()
    original_floors = floors()
    original_ids = [uid(500)]
    original = copy.deepcopy((original_collection, original_messages, original_floors, original_ids))
    result = assemble_prompt_collection(
        original_collection, original_messages, logical_floors=original_floors,
        prompt_message_ids=original_ids,
    )
    validated = validate_prompt_assembly(result)
    result["messages"][0]["blocks"][0]["text"] = "edited"
    result["manifest"]["collection"]["items"][0]["text"] = "edited"
    result["manifest"]["logical_floors"][0][0] = uid(900)
    result["manifest"]["prompt_message_ids"][0] = uid(901)
    assert (original_collection, original_messages, original_floors, original_ids) == original
    assert validated["messages"][0] == original_messages[0]
    assert validated["manifest"]["collection"] == original_collection


@pytest.mark.parametrize("logical_floors", [
    [], (), [[uid(20)], []], [[uid(20), uid(20)]], [[uid(999)]],
    [[uid(20)], [uid(21)]], [[True]], [[uid(20)], [uid(20)], [uid(20)]],
])
def test_floor_partition_rejects_empty_duplicate_unknown_and_non_json_floors(logical_floors):
    with pytest.raises(ContractValidationError):
        assemble([item()], logical_floors=logical_floors)


@pytest.mark.parametrize("bad_floors", [
    [[uid(20)], [uid(21)], [uid(22)], [uid(23), uid(24)], [uid(25)]],
    [[uid(20)], [uid(21), uid(22)], [uid(23)], [uid(24)], [uid(25)]],
])
def test_logical_floors_cannot_split_tool_call_result_groups(bad_floors):
    with pytest.raises(ContractValidationError):
        assemble([item()], history(), logical_floors=bad_floors)


@pytest.mark.parametrize("source", ["model", "protocol_feedback"])
def test_root_floor_cannot_guess_role_or_accept_protocol_messages(source):
    with pytest.raises(ContractValidationError, match="root logical floor"):
        assemble([item()], [message(20, source)])


def test_runtime_diagnostic_is_not_implicitly_counted_as_a_floor():
    diagnostic = {
        "schema_version": 2, "message_id": uid(26), "role": "system",
        "source": {
            "kind": "runtime_execution_observation", "closeout_id": uid(90),
            "source_run_id": uid(91),
        },
        "blocks": [{"kind": "text", "text": "interrupted execution"}],
    }
    messages = [message(20), diagnostic, message(25)]
    with pytest.raises(ContractValidationError, match="delta logical floor"):
        assemble([item()], messages, logical_floors=[[uid(20)], [uid(26)], [uid(25)]])


def test_no_existing_prompt_message_can_be_reintroduced_into_logical_history():
    old = assemble([item()])["messages"][-1]
    messages = [message(20), old, message(25)]
    with pytest.raises(ContractValidationError, match="delta logical floor"):
        assemble([item(2)], messages, logical_floors=[[uid(20)], [uid(500)], [uid(25)]])


@pytest.mark.parametrize("prompt_ids", [
    [], [uid(500), uid(501)], [uid(20)], ["not UUID"], [None], [True], (uid(500),),
])
def test_prompt_ids_are_explicit_unique_valid_and_one_per_item(prompt_ids):
    with pytest.raises(ContractValidationError):
        assemble_prompt_collection(
            collection(item()), [message(20)], logical_floors=[[uid(20)]],
            prompt_message_ids=prompt_ids,
        )


def test_duplicate_prompt_message_ids_rejected_even_for_distinct_items():
    with pytest.raises(ContractValidationError, match="unique"):
        assemble_prompt_collection(
            collection(item(1), item(2)), [message(20)], logical_floors=[[uid(20)]],
            prompt_message_ids=[uid(500), uid(500)],
        )


def test_duplicate_original_message_ids_rejected_before_assembly():
    with pytest.raises(ContractValidationError, match="Duplicate message"):
        assemble([], [message(20), message(20), message(20)],
                 logical_floors=[[uid(20)], [uid(20)], [uid(20)]])


@pytest.mark.parametrize("changes", [
    {"unknown": 1}, {"role": "tool"}, {"source": {"kind": "model", "request_id": uid(9)}},
    {"enabled": False}, {"depth": True}, {"revision": 0},
])
def test_malformed_item_and_source_are_rejected_without_history_change(changes):
    original = [message(20)]
    with pytest.raises(ContractValidationError):
        assemble([item(**changes)], original)
    assert original == [message(20)]


def test_lorebook_cannot_cross_legacy_collection_version_boundary():
    with pytest.raises(ContractValidationError, match="v1"):
        assemble([lorebook_item()])


@pytest.mark.parametrize("limits", [
    PromptAssemblyLimits(max_messages=1), PromptAssemblyLimits(max_total_chars=9),
])
def test_final_capacity_failure_does_not_trim_messages_or_modify_inputs(limits):
    original = [message(20)]
    with pytest.raises(PromptProcessingError) as error:
        assemble([item(text="prompt")], original, limits=limits)
    assert error.value.code == "prompt_assembly_limit"
    assert original == [message(20)]


def test_exact_capacity_and_unicode_character_not_byte_counting():
    result = assemble(
        [item(text="世界")], [message(20, text="hello")],
        limits=PromptAssemblyLimits(max_messages=2, max_total_chars=7),
    )
    assert result["manifest"]["usage"] == {"messages": 2, "total_chars": 7}
    assert result["manifest"]["limits"] == {"max_messages": 2, "max_total_chars": 7}
    assert validate_prompt_assembly(result) == result


def test_tool_arguments_and_result_content_are_included_in_character_capacity():
    result = assemble([], history())
    total = result["manifest"]["usage"]["total_chars"]
    plain = sum(
        len(block["text"])
        for m in history() for block in m["blocks"] if block["kind"] == "text"
    )
    assert total > plain
    with pytest.raises(PromptProcessingError):
        assemble([], history(), limits=PromptAssemblyLimits(max_total_chars=plain))
    assert assemble([], history(), limits=PromptAssemblyLimits(max_total_chars=total))


def test_public_content_counter_matches_assembly_usage_without_mutating_inputs():
    original = history()
    assert message_content_chars(original) == assemble([], original)["manifest"]["usage"]["total_chars"]
    assert original == history()


@pytest.mark.parametrize("invalid", [
    (), [message(20), message(20)], tool_batch()[:1], [{"message_id": uid(20)}],
])
def test_public_content_counter_rejects_noncanonical_or_unsettled_history(invalid):
    with pytest.raises(ContractValidationError):
        message_content_chars(invalid)


@pytest.mark.parametrize("changes", [
    {"max_messages": True}, {"max_messages": -1}, {"max_messages": 1.0},
    {"max_total_chars": None}, {"max_total_chars": -1}, {"max_total_chars": "10"},
])
def test_limits_reject_invalid_numeric_types(changes):
    with pytest.raises(ContractValidationError):
        PromptAssemblyLimits(**changes)


@pytest.mark.parametrize("limit", [None, {}, {"max_messages": 2}, 5])
def test_assembly_and_validation_require_explicit_limit_class(limit):
    with pytest.raises(ContractValidationError):
        assemble([], limits=limit)
    with pytest.raises(ContractValidationError):
        validate_prompt_assembly(assemble(), limits=limit)


@pytest.mark.parametrize("location", ["assembly", "manifest", "entry", "limits", "usage"])
def test_assembly_evidence_rejects_unknown_fields(location):
    value = assemble([item()])
    target = {
        "assembly": value, "manifest": value["manifest"],
        "entry": value["manifest"]["entries"][0],
        "limits": value["manifest"]["limits"], "usage": value["manifest"]["usage"],
    }[location]
    target["extra"] = "not admitted"
    with pytest.raises(ContractValidationError):
        validate_prompt_assembly(value)


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(schema_version=True),
    lambda v: v.update(kind="text"),
    lambda v: v["manifest"].update(schema_version=2),
    lambda v: v["manifest"].update(kind="draft"),
    lambda v: v["manifest"]["usage"].update(total_chars=True),
    lambda v: v["manifest"]["entries"][0].update(message_index=0),
    lambda v: v["manifest"]["entries"][0].update(floor_boundary=0),
    lambda v: v["manifest"]["entries"][0].update(region="after"),
    lambda v: v["manifest"]["entries"][0].update(collection_index=1),
    lambda v: v["manifest"].update(entries=[]),
    lambda v: v["manifest"].update(input_message_ids=[uid(999)]),
    lambda v: v["manifest"].update(input_message_ids=[uid(20), uid(20)]),
    lambda v: v["messages"][-1]["blocks"][0].update(text="fabricated"),
    lambda v: v["messages"][-1]["source"].update(revision=3),
    lambda v: v["messages"].reverse(),
])
def test_revalidation_reconstructs_and_rejects_mutated_messages_or_mapping(mutation):
    value = assemble([item()])
    mutation(value)
    with pytest.raises(ContractValidationError):
        validate_prompt_assembly(value)


def test_revalidation_rejects_missing_and_non_json_evidence():
    value = assemble([item()])
    for field in value:
        changed = copy.deepcopy(value)
        changed.pop(field)
        with pytest.raises(ContractValidationError):
            validate_prompt_assembly(changed)
    value["manifest"]["collection"]["items"][0]["text"] = value
    with pytest.raises(ContractValidationError, match="Cyclic"):
        validate_prompt_assembly(value)


def test_validator_enforces_callers_capacity_even_when_manifest_allows_more():
    value = assemble([item()])
    with pytest.raises(PromptProcessingError):
        validate_prompt_assembly(value, limits=PromptAssemblyLimits(max_messages=1))
