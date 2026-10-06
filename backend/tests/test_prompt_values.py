"""Pure collection identity, explicit rendering, and context text projections."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_message_history
from phase1_agent.contract_json import loads_strict
from phase1_agent.prompt_config import expand_prompt_config
from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_values import (
    collect_prompt_inputs,
    collection_from_config,
    context_view_messages,
    make_context_view,
    render_prompt_text,
    validate_context_view,
    validate_prompt_collection,
    validate_prompt_item,
)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def definition(**changes):
    value = {
        "schema_version": 1, "kind": "item", "item_id": uid(1), "revision": 1,
        "name": "same name", "text": "same text", "role": "system",
        "enabled": True, "placement": "before", "depth": None, "order": 0,
        "interpolation": "variables", "source": {"kind": "configuration"},
    }
    value.update(changes)
    return value


def effective(instance=10, **changes):
    item = definition()
    item.pop("schema_version")
    item.pop("kind")
    item.update({
        "item_instance_id": uid(instance), "group_id": None, "group_revision": None,
        "group_instance_id": None, "input_name": "configured", "declaration_index": 3,
    })
    item.update(changes)
    return item


def collection(*items):
    return {"schema_version": 1, "kind": "prompt_collection", "items": list(items)}


def message(source="human", number=100, role=None):
    sources = {
        "human": {"kind": "human", "visible_message_id": uid(200)},
        "upstream_node": {"kind": "upstream_node", "output_id": uid(201)},
        "model": {"kind": "model", "request_id": uid(202)},
        "protocol_feedback": {"kind": "protocol_feedback", "request_id": uid(203)},
        "prompt": {
            "kind": "prompt", "prompt_id": uid(204), "revision": 1,
            "item_instance_id": uid(205), "group_instance_id": None,
        },
        "runtime_execution_observation": {
            "kind": "runtime_execution_observation", "closeout_id": uid(206),
            "source_run_id": uid(207),
        },
    }
    return {
        "schema_version": (
            3 if source == "prompt" else 2 if source == "runtime_execution_observation" else 1
        ),
        "message_id": uid(number),
        "role": role or (
            "assistant" if source == "model"
            else "system" if source == "runtime_execution_observation" else "user"
        ),
        "source": sources[source], "blocks": [{"kind": "text", "text": "original"}],
    }


def view(messages=None, **changes):
    value = {
        "schema_version": 1, "kind": "context_view", "workflow_session_id": uid(300),
        "node_binding_id": uid(301), "parent_turn_id": None, "projection_version": 1,
        "purpose": "send", "messages": [message()] if messages is None else messages,
        "overrides": [], "protected_blocks": [],
    }
    value.update(changes)
    return value


def override(number=100, index=0, text="derived"):
    return {"message_id": uid(number), "block_index": index, "text": text}


def tool_sequence():
    path = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
    saved = loads_strict(path.read_text(encoding="utf-8"))
    return copy.deepcopy(saved["turn"][0]["messages"][:2])


def test_effective_item_is_detached_and_preserves_configuration_metadata():
    original = effective()
    result = validate_prompt_item(original)
    assert result == original
    result["source"]["kind"] = "changed"
    result["text"] = "changed"
    assert original["source"] == {"kind": "configuration"}
    assert original["text"] == "same text"


@pytest.mark.parametrize("changes", [
    {"schema_version": 1}, {"kind": "item"}, {"unknown": "not allowed"},
    {"enabled": False}, {"enabled": 1}, {"role": "tool"},
    {"source": {"kind": "lorebook"}}, {"source": {"kind": "configuration", "extra": 1}},
    {"revision": True}, {"revision": 0}, {"revision": 1.0},
    {"order": False}, {"item_instance_id": uid(10).upper().replace("4000", "400A")},
    {"item_instance_id": "not an identity"}, {"item_id": uid(1).replace("4000", "1000")},
    {"input_name": ""}, {"input_name": 1}, {"declaration_index": -1},
    {"declaration_index": True}, {"declaration_index": 1.0},
    {"placement": "middle"}, {"depth": 0}, {"interpolation": "eval"},
    {"group_id": uid(2)}, {"group_revision": 1}, {"group_instance_id": uid(20)},
    {"group_id": uid(2), "group_revision": True, "group_instance_id": uid(20)},
    {"text": "\ud800"},
])
def test_effective_item_rejects_non_effective_and_inconsistent_fields(changes):
    with pytest.raises(ContractValidationError):
        validate_prompt_item(effective(**changes))


def test_effective_item_requires_every_original_field():
    for field in effective():
        value = effective()
        value.pop(field)
        with pytest.raises(ContractValidationError):
            validate_prompt_item(value)


def test_grouped_item_retains_complete_group_identity():
    value = effective(group_id=uid(2), group_revision=3, group_instance_id=uid(20))
    assert validate_prompt_item(value) == value


@pytest.mark.parametrize("value", [
    [], "not an item", {"extra": object()}, {"extra": float("nan")}, {1: "not JSON"},
])
def test_prompt_values_reject_non_json_or_non_object_input(value):
    with pytest.raises(ContractValidationError):
        validate_prompt_item(value)
    with pytest.raises(ContractValidationError):
        validate_prompt_collection(value)


def test_cycle_is_rejected_before_copying():
    value = effective()
    value["text"] = value
    with pytest.raises(ContractValidationError, match="Cyclic"):
        validate_prompt_item(value)


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 3}, {"kind": "draft"},
    {"items": ()}, {"items": None}, {"extra": 1},
])
def test_collection_envelope_is_strict(changes):
    value = collection()
    value.update(changes)
    with pytest.raises(ContractValidationError):
        validate_prompt_collection(value)


def test_collection_keeps_same_text_with_distinct_instances_and_returns_copy():
    original = collection(effective(10), effective(11))
    result = validate_prompt_collection(original)
    assert result == original
    result["items"][0]["text"] = "changed"
    assert original["items"][0]["text"] == "same text"


def test_collection_rejects_repeat_identity_even_with_different_text_and_revision():
    with pytest.raises(ContractValidationError, match="Duplicate scoped"):
        validate_prompt_collection(collection(
            effective(), effective(text="different", revision=2),
        ))


def test_scoped_identity_allows_standalone_and_repeated_group_instances():
    items = [
        effective(10),
        effective(10, group_id=uid(2), group_revision=1, group_instance_id=uid(20)),
        effective(10, group_id=uid(2), group_revision=1, group_instance_id=uid(21)),
    ]
    assert validate_prompt_collection(collection(*items))["items"] == items
    with pytest.raises(ContractValidationError, match="Duplicate scoped"):
        validate_prompt_collection(collection(items[1], copy.deepcopy(items[1])))


def test_collection_from_config_uses_exact_expansion_without_changing_schema():
    item = definition()
    group = {
        "schema_version": 1, "kind": "group", "group_id": uid(2), "revision": 1,
        "name": "group", "members": [{
            "item_instance_id": uid(10), "item_id": uid(1), "revision": 1,
            "overrides": {"role": "assistant", "placement": "middle", "depth": 2},
        }],
    }
    config = {
        "schema_version": 1, "kind": "config", "config_id": uid(3), "revision": 1,
        "name": "config", "inputs": [
            {"name": "grouped", "kind": "group", "group_instance_id": uid(20),
             "group_id": uid(2), "revision": 1, "enabled": True, "member_overrides": []},
            {"name": "disabled", "kind": "item", "item_instance_id": uid(12),
             "item_id": uid(1), "revision": 1, "overrides": {"enabled": False}},
        ],
    }
    resolve_item = lambda *_: item
    resolve_group = lambda *_: group
    original = copy.deepcopy((item, group, config))
    result = collection_from_config(config, resolve_item, resolve_group)
    assert result == collection(*expand_prompt_config(config, resolve_item, resolve_group))
    assert len(result["items"]) == 1
    assert result["items"][0]["depth"] == 2
    assert (item, group, config) == original


def test_named_collection_order_ignores_dictionary_arrival_and_preserves_metadata():
    first = effective(10, declaration_index=8, order=80, input_name="old-first")
    second = effective(11, declaration_index=1, order=-80, input_name="old-second")
    inputs = {"second": collection(second), "first": first}
    original = copy.deepcopy(inputs)
    result = collect_prompt_inputs(["first", "second"], inputs)
    assert result["items"] == [first, second]
    assert inputs == original
    assert collect_prompt_inputs(["second", "first"], inputs)["items"] == [second, first]
    result["items"][0]["text"] = "changed"
    assert inputs == original


def test_empty_declared_inputs_and_empty_collection_are_valid():
    assert collect_prompt_inputs([], {}) == collection()
    assert collect_prompt_inputs(["empty"], {"empty": collection()}) == collection()


@pytest.mark.parametrize("order,inputs", [
    (["one", "one"], {"one": effective()}),
    (["one"], {}), ([], {"one": effective()}),
    (["one"], {"one": effective(), "unknown": effective(11)}),
    ([""], {"": effective()}), ([1], {"one": effective()}),
    (("one",), {"one": effective()}), (["one"], []),
    (["one"], {"one": "plain text"}),
    (["one"], {"one": definition()}),
])
def test_named_collection_requires_complete_distinct_typed_inputs(order, inputs):
    with pytest.raises(ContractValidationError):
        collect_prompt_inputs(order, inputs)


def test_repeated_identity_across_distinct_inputs_is_not_silently_deduplicated():
    with pytest.raises(ContractValidationError, match="Duplicate scoped"):
        collect_prompt_inputs(["one", "two"], {
            "one": effective(), "two": collection(effective(text="different")),
        })


def test_rendering_is_explicit_and_uses_characters_including_separator():
    value = collection(effective(10, text="α"), effective(11, text="β"))
    original = copy.deepcopy(value)
    assert render_prompt_text(value, separator="::", max_output_chars=4) == "α::β"
    assert value == original
    with pytest.raises(PromptProcessingError) as error:
        render_prompt_text(value, separator="::", max_output_chars=3)
    assert error.value.code == "prompt_output_limit"
    assert "α" not in str(error.value)


def test_empty_render_and_zero_limit_do_not_insert_separator():
    assert render_prompt_text(collection(), separator="|", max_output_chars=0) == ""
    assert render_prompt_text(collection(effective(text="")), separator="|", max_output_chars=0) == ""
    with pytest.raises(PromptProcessingError):
        render_prompt_text(collection(effective(text=""), effective(11, text="")),
                           separator="|", max_output_chars=0)


@pytest.mark.parametrize("separator,limit", [
    (1, 5), ("\ud800", 5), ("|", True), ("|", -1), ("|", 1.0),
])
def test_render_rejects_invalid_separator_and_resource_limit(separator, limit):
    with pytest.raises(ContractValidationError):
        render_prompt_text(collection(), separator=separator, max_output_chars=limit)


@pytest.mark.parametrize("purpose", ["send", "display"])
def test_context_constructor_keeps_original_and_detaches_for_each_purpose(purpose):
    messages = [message()]
    result = make_context_view(
        messages, workflow_session_id=uid(300), node_binding_id=uid(301),
        parent_turn_id=uid(302), projection_version=2, purpose=purpose, protected_blocks=[],
    )
    assert result == view(messages, parent_turn_id=uid(302), projection_version=2, purpose=purpose)
    assert result["overrides"] == []
    result["messages"][0]["blocks"][0]["text"] = "changed"
    assert messages[0]["blocks"][0]["text"] == "original"


@pytest.mark.parametrize("source", ["human", "upstream_node", "model"])
def test_allowed_sources_project_only_requested_text_and_keep_envelope(source):
    original_message = message(source)
    original_message["blocks"].append({"kind": "text", "text": "second"})
    value = view([original_message], overrides=[override(index=1, text="")])
    original = copy.deepcopy(value)
    checked = validate_context_view(value)
    assert checked == original
    materialized = context_view_messages(value)
    assert materialized[0]["blocks"] == [
        {"kind": "text", "text": "original"}, {"kind": "text", "text": ""},
    ]
    assert materialized[0]["source"] == original_message["source"]
    assert materialized[0]["message_id"] == original_message["message_id"]
    assert value == original
    materialized[0]["source"]["kind"] = "changed"
    checked["overrides"][0]["text"] = "changed"
    assert value == original


def test_display_and_send_materializations_are_separate_without_history_edits():
    messages = [message()]
    display = view(messages, purpose="display", overrides=[override(text="display")])
    send = view(messages, purpose="send", overrides=[override(text="send")])
    assert context_view_messages(display)[0]["blocks"][0]["text"] == "display"
    assert context_view_messages(send)[0]["blocks"][0]["text"] == "send"
    assert messages[0]["blocks"][0]["text"] == "original"
    assert display["purpose"] == "display" and send["purpose"] == "send"


@pytest.mark.parametrize("source,role", [
    ("prompt", "system"), ("prompt", "user"), ("prompt", "assistant"),
    ("protocol_feedback", "user"), ("runtime_execution_observation", "system"),
])
def test_business_source_not_role_controls_override_permission(source, role):
    value = view([message(source, role=role)], overrides=[override()])
    with pytest.raises(ContractValidationError, match="protected"):
        validate_context_view(value)
    value["overrides"] = []
    assert validate_context_view(value) == value


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"kind": "messages"},
    {"workflow_session_id": None}, {"workflow_session_id": "bad"},
    {"node_binding_id": uid(1).replace("4000", "1000")},
    {"parent_turn_id": ""}, {"projection_version": True}, {"projection_version": 0},
    {"projection_version": 1.0}, {"purpose": "both"}, {"purpose": []},
    {"messages": ()}, {"overrides": ()}, {"protected_blocks": ()}, {"unknown": 1},
])
def test_context_envelope_rejects_invalid_identity_version_type_and_unknown_fields(changes):
    with pytest.raises(ContractValidationError):
        validate_context_view(view(**changes))


def test_context_view_requires_all_envelope_fields():
    for field in view():
        value = view()
        value.pop(field)
        with pytest.raises(ContractValidationError):
            validate_context_view(value)


@pytest.mark.parametrize("changes", [
    {"message_id": "bad"}, {"message_id": uid(999)}, {"block_index": True},
    {"block_index": -1}, {"block_index": 1.0}, {"block_index": 1},
    {"text": 1}, {"text": "\ud800"}, {"source": "do not alter"},
])
def test_text_mapping_rejects_missing_targets_types_and_unknown_fields(changes):
    edit = override()
    edit.update(changes)
    with pytest.raises(ContractValidationError):
        validate_context_view(view(overrides=[edit]))


def test_text_mapping_requires_all_fields_and_unique_target():
    for field in override():
        edit = override()
        edit.pop(field)
        with pytest.raises(ContractValidationError):
            validate_context_view(view(overrides=[edit]))
    with pytest.raises(ContractValidationError, match="Duplicate context"):
        validate_context_view(view(overrides=[override(), override(text="another")]))


def test_context_rejects_original_duplicate_message_identity():
    with pytest.raises(ContractValidationError, match="Duplicate message"):
        validate_context_view(view([message(), message()]))


def test_context_keeps_tool_protocol_data_and_order_identical():
    messages = tool_sequence()
    messages[0]["blocks"].insert(0, {"kind": "text", "text": "ordinary assistant text"})
    value = view(messages)
    original = copy.deepcopy(value)
    projected = context_view_messages(value)
    assert projected == messages
    validate_message_history(projected)
    assert value == original
    value["overrides"] = [override(
        number=int(messages[0]["message_id"][-12:], 16), text="derived assistant text",
    )]
    with pytest.raises(ContractValidationError, match="protected"):
        context_view_messages(value)


def test_context_override_never_targets_call_or_result_blocks():
    messages = tool_sequence()
    for target in messages:
        edit = {
            "message_id": target["message_id"], "block_index": 0, "text": "do not rewrite",
        }
        with pytest.raises(ContractValidationError, match="protected"):
            validate_context_view(view(messages, overrides=[edit]))


@pytest.mark.parametrize("reason", ["never_started", "interrupted", "outcome_unknown"])
def test_runtime_unknown_and_unexecuted_tool_evidence_stays_original_and_uneditable(reason):
    assistant, result = tool_sequence()
    call = assistant["blocks"][0]
    call["tool_name"] = "external_action"
    execution_id = None if reason == "never_started" else uid(600)
    content = {
        "reason_code": reason, "error": "Runtime evidence",
        "retry_guidance": "Verify idempotency and external state before retrying.",
    }
    observation = {
        "schema_version": 2, "message_id": result["message_id"], "role": "tool",
        "source": {
            "kind": "runtime_tool_observation", "tool_call_id": call["tool_call_id"],
            "tool_execution_id": execution_id, "reason_code": reason,
        },
        "blocks": [{
            "kind": "tool_result", "tool_call_id": call["tool_call_id"],
            "tool_execution_id": execution_id, "status": "error", "is_error": True,
            "content": content, "model_visible_text": json.dumps(content),
        }],
    }
    messages = [assistant, observation]
    value = view(messages)
    assert context_view_messages(value) == messages
    value["overrides"] = [{
        "message_id": observation["message_id"], "block_index": 0, "text": "forged success",
    }]
    with pytest.raises(ContractValidationError, match="protected"):
        validate_context_view(value)


def test_context_rejects_unsettled_and_reordered_tool_protocol():
    messages = tool_sequence()
    with pytest.raises(ContractValidationError, match="Unsettled"):
        validate_context_view(view(messages[:1]))
    with pytest.raises(ContractValidationError, match="pending"):
        validate_context_view(view(list(reversed(messages))))


def test_context_uses_original_message_argument_and_source_validation():
    messages = tool_sequence()
    messages[0]["blocks"][0]["parsed_arguments"] = {"forged": True}
    with pytest.raises(ContractValidationError, match="parsed_arguments"):
        validate_context_view(view(messages))
    with pytest.raises(ContractValidationError):
        validate_context_view(view([message("human", role="assistant")]))


def test_context_accepts_legacy_and_new_prompts_without_relabeling_them():
    legacy = message("prompt", role="system")
    legacy["schema_version"] = 1
    legacy["source"].pop("item_instance_id")
    legacy["source"].pop("group_instance_id")
    messages = [legacy, message("prompt", number=101, role="assistant"), message(number=102)]
    value = view(messages)
    assert context_view_messages(value) == messages
    assert [item["schema_version"] for item in value["messages"]] == [1, 3, 1]


def test_empty_context_sequence_is_a_valid_closed_view():
    assert validate_context_view(view([])) == view([])
    assert context_view_messages(view([])) == []


def test_explicit_structured_final_locator_preserves_final_without_guessing_text():
    assistant = message("model")
    assistant["blocks"] = [
        {"kind": "text", "text": '{"text":"final"}'},
        {"kind": "text", "text": "ordinary"},
    ]
    final_locator = {"message_id": uid(100), "block_index": 0}
    value = view(
        [assistant], protected_blocks=[final_locator],
        overrides=[override(index=1, text="derived ordinary")],
    )
    projected = context_view_messages(value)
    assert projected[0]["blocks"] == [
        {"kind": "text", "text": '{"text":"final"}'},
        {"kind": "text", "text": "derived ordinary"},
    ]
    assert value["messages"][0] == assistant
    value["overrides"] = [override(text='{"text":"forged"}')]
    with pytest.raises(ContractValidationError, match="protected"):
        validate_context_view(value)


def test_constructor_detaches_explicit_protected_locator_input():
    protected = [{"message_id": uid(100), "block_index": 0}]
    result = make_context_view(
        [message("model")], workflow_session_id=uid(300), node_binding_id=uid(301),
        parent_turn_id=None, projection_version=1, purpose="send", protected_blocks=protected,
    )
    result["protected_blocks"][0]["block_index"] = 1
    assert protected[0]["block_index"] == 0


@pytest.mark.parametrize("locator", [
    {"message_id": "bad", "block_index": 0},
    {"message_id": uid(999), "block_index": 0},
    {"message_id": uid(100), "block_index": True},
    {"message_id": uid(100), "block_index": -1},
    {"message_id": uid(100), "block_index": 1.0},
    {"message_id": uid(100), "block_index": 1},
    {"message_id": uid(100)},
    {"block_index": 0},
    {"message_id": uid(100), "block_index": 0, "text": "not allowed"},
])
def test_protected_locators_are_strict_and_must_exist(locator):
    with pytest.raises(ContractValidationError):
        validate_context_view(view(protected_blocks=[locator]))


def test_protected_locators_are_unique_and_only_identify_text():
    locator = {"message_id": uid(100), "block_index": 0}
    with pytest.raises(ContractValidationError, match="Duplicate protected"):
        validate_context_view(view(protected_blocks=[locator, copy.deepcopy(locator)]))
    messages = tool_sequence()
    locator = {"message_id": messages[0]["message_id"], "block_index": 0}
    with pytest.raises(ContractValidationError, match="text block"):
        validate_context_view(view(messages, protected_blocks=[locator]))
