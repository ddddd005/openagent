"""Public prompt preparation freezes detached evidence, not live state."""

from __future__ import annotations

import copy
from pathlib import Path
from uuid import UUID

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import content_digest, dumps_pretty, loads_strict
from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_preparation import (
    make_prompt_context_config, prepare_prompt_context,
    validate_context_preparation, validate_prompt_context_config,
)
from phase1_agent.prompt_values import context_view_messages
from phase1_agent.prompt_variables import create_variable_registry, create_variable_snapshot


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


SESSION, BINDING, ROOT = uid(300), uid(301), uid(302)


def definition(number=1, **changes):
    value = {
        "schema_version": 1, "kind": "item", "item_id": uid(number), "revision": 1,
        "name": "Not sent as text", "text": "instruction", "role": "system",
        "enabled": True, "placement": "before", "depth": None, "order": 0,
        "interpolation": "literal", "source": {"kind": "configuration"},
    }
    value.update(changes)
    return value


def effective(number=1, instance=10, **changes):
    value = definition(number, **changes)
    value.pop("schema_version")
    value.pop("kind")
    value.update({
        "item_instance_id": uid(instance), "group_id": None, "group_revision": None,
        "group_instance_id": None, "input_name": "base", "declaration_index": 0,
    })
    return value


def collection(*items):
    return {"schema_version": 1, "kind": "prompt_collection", "items": list(items)}


def node_input(source=None, payload=None):
    return {
        "schema_version": 1, "input_id": uid(310), "port_id": "request",
        "payload_schema_ref": {"schema_id": "writing_text", "version": 1},
        "source": source or {"kind": "visible_message", "visible_message_id": uid(311)},
        "payload": {"text": "hello"} if payload is None else payload,
    }


def message(number=100, role="user", text="old"):
    return {
        "schema_version": 1, "message_id": uid(number), "role": role,
        "source": (
            {"kind": "human", "visible_message_id": uid(400)}
            if role == "user" else {"kind": "model", "request_id": uid(401)}
        ),
        "blocks": [{"kind": "text", "text": text}],
    }


def variables(**values):
    registry = create_variable_registry(
        workflow_id=uid(320), revision=2, definitions=[
            {"name": name, "type": "string", "default": value} for name, value in values.items()
        ],
    )
    return create_variable_snapshot(registry, workflow_session_id=SESSION, node_binding_id=BINDING)


def macro(number=200, **changes):
    value = {
        "schema_version": 1, "node_id": uid(number), "kind": "macro",
        "enabled": True, "select": {"mode": "all"},
    }
    value.update(changes)
    return value


def regex(number=200, pattern="a", replacement="aa", **changes):
    value = {
        **macro(number), "kind": "regex",
        "rule": {"pattern": pattern, "replacement": replacement, "flags": "", "mode": "all"},
    }
    value.update(changes)
    return value


def context_regex(number=210, pattern="hello", replacement="city", **changes):
    value = {
        "schema_version": 1, "kind": "context_regex", "node_id": uid(number),
        "enabled": True, "purposes": ["send"], "sources": ["human", "upstream_node", "model"],
        "rule": {"pattern": pattern, "replacement": replacement, "flags": "", "mode": "all"},
    }
    value.update(changes)
    return value


def lorebook(number=220, keyword="city", *, text="world", text_source="derived"):
    prompt = definition(number, text=text)
    return {
        "book": {
            "schema_version": 1, "kind": "lorebook", "book_id": uid(number + 1),
            "revision": 3, "name": "Book title", "entries": [{
                "entry_id": uid(number + 2), "revision": 4, "name": "Entry memo",
                "enabled": True, "keyword": keyword,
                "prompt_ref": {"item_id": prompt["item_id"], "revision": prompt["revision"]},
            }],
        },
        "request": {
            "schema_version": 1, "kind": "lorebook_request", "node_id": uid(number + 3),
            "book_instance_id": uid(number + 4), "input_name": f"book_{number}",
            "entry_instances": [{"entry_id": uid(number + 2), "item_instance_id": uid(number + 5)}],
            "scan": {"text_source": text_source, "sources": ["human", "upstream_node", "model"]},
        },
        "prompts": [prompt],
    }


def config(items=None, **kwargs):
    return make_prompt_context_config(
        collection(effective()) if items is None else collection(*items), **kwargs,
    )


def prepare(config_value=None, history=None, **kwargs):
    history = [] if history is None else history
    value = config() if config_value is None else config_value
    defaults = {
        "workflow_session_id": SESSION, "node_binding_id": BINDING,
        "parent_turn_id": None,
        "logical_floors": [] if not history else [[item["message_id"]] for item in history],
        "protected_blocks": [], "root_message_id": ROOT,
        "prompt_message_ids": [uid(500 + index) for index in range(len(value["collection"]["items"]))],
    }
    defaults.update(kwargs)
    return prepare_prompt_context(node_input(), history, config=value, **defaults)


def rehash(value):
    value["evidence_digest"] = content_digest({
        key: item for key, item in value.items() if key != "evidence_digest"
    })
    return value


def test_empty_context_still_has_exact_root_and_explicit_defaults():
    cfg = config(items=[])
    result = prepare(cfg)
    assert result["s0"] == result["canonical_messages"]
    root = result["canonical_messages"][-1]
    assert root["message_id"] == ROOT
    assert root["source"] == {"kind": "human", "visible_message_id": uid(311)}
    assert root["blocks"] == [{"kind": "text", "text": dumps_pretty(node_input()["payload"])}]
    assert result["logical_floors"] == [[ROOT]]
    assert result["root_locator"] == {"message_id": ROOT, "block_index": 0}
    assert set(result["config"]["limits"]) == {"processing", "context_regex", "lorebook", "assembly"}
    assert result["config"] == cfg
    assert validate_context_preparation(result) == result


def test_upstream_root_uses_explicit_source_without_human_relabeling():
    source = node_input(source={"kind": "upstream_output", "output_id": uid(700)})
    result = prepare_prompt_context(
        source, [], workflow_session_id=SESSION, node_binding_id=BINDING,
        parent_turn_id=None, logical_floors=[], protected_blocks=[], config=config(items=[]),
        root_message_id=ROOT,
    )
    assert result["s0"][-1]["source"] == {"kind": "upstream_node", "output_id": uid(700)}
    assert validate_context_preparation(result, node_input=source) == result


def test_history_is_kept_exactly_and_complete_floors_append_current_root():
    history = [message(), message(101, "assistant")]
    cfg = config(items=[
        effective(text="first", placement="middle", depth=2),
        effective(2, 11, text="last", placement="after"),
    ])
    original = copy.deepcopy((cfg, history))
    result = prepare(cfg, history)
    assert result["canonical_messages"][:-1] == history
    assert result["logical_floors"] == [[uid(100)], [uid(101)], [ROOT]]
    assert [item["blocks"][0]["text"] for item in result["s0"]] == [
        "old", "first", "old", dumps_pretty(node_input()["payload"]), "last",
    ]
    assert (cfg, history) == original
    assert validate_context_preparation(
        result, node_input=node_input(), history=history, workflow_session_id=SESSION,
        node_binding_id=BINDING, parent_turn_id=None,
        logical_floors=[[uid(100)], [uid(101)]], protected_blocks=[],
    ) == result


def test_result_and_config_return_independent_deep_copies():
    cfg = config()
    result = prepare(cfg)
    original = copy.deepcopy(result)
    checked = validate_context_preparation(result)
    cfg["collection"]["items"][0]["text"] = "new config"
    checked["s0"][0]["blocks"][0]["text"] = "tampered output"
    checked["config"]["collection"]["items"][0]["text"] = "tampered config"
    assert result == original
    assert result["s0"][0]["blocks"][0]["text"] == "instruction"


def test_macro_serial_passes_are_explicit_and_saved_trace_is_complete():
    cfg = config(
        items=[effective(text="{{a}}", interpolation="variables")],
        variables=variables(a="{{b}}", b="done"), steps=[macro(), macro(201)],
    )
    result = prepare(cfg)
    assert result["processing"]["stages"][0]["collection"]["items"][0]["text"] == "{{b}}"
    assert result["processing"]["stages"][1]["collection"]["items"][0]["text"] == "done"
    assert result["s0"][0]["blocks"][0]["text"] == "done"
    assert result["variables"] == cfg["variables"]
    assert result["processing"]["stages"][1]["trace"]["input_digest"] == (
        result["processing"]["stages"][0]["trace"]["output_digest"]
    )


def test_saved_result_validation_does_not_rerun_macro_regex_or_read_live_state(monkeypatch):
    result = prepare(config(
        items=[effective(text="{{a}}", interpolation="variables")],
        variables=variables(a="a"), steps=[macro(), regex(201)],
        context_regex=[context_regex()],
    ))

    def unexpected(*args, **kwargs):
        raise AssertionError("Frozen validation must not execute a text-processing node")

    monkeypatch.setattr("phase1_agent.prompt_preparation.process_prompt_collection", unexpected)
    monkeypatch.setattr("phase1_agent.prompt_preparation.apply_context_regex", unexpected)
    assert validate_context_preparation(result) == result
    assert result["collection"]["items"][0]["text"] == "aa"
    assert "city" in result["s0"][-1]["blocks"][0]["text"]


def test_display_regex_is_not_sent_and_send_regex_does_not_change_display():
    cfg = config(
        context_regex=[
            context_regex(replacement="displayed", purposes=["display"]),
            context_regex(211, replacement="sent"),
        ],
    )
    result = prepare(cfg)
    canonical = result["canonical_messages"][-1]["blocks"][0]["text"]
    assert "hello" in canonical
    assert "sent" in result["s0"][-1]["blocks"][0]["text"]
    assert "displayed" in context_view_messages(result["display_view"])[-1]["blocks"][0]["text"]
    assert "sent" in context_view_messages(result["send_view"])[-1]["blocks"][0]["text"]
    assert len(result["context_regex"]["send"]) == len(result["context_regex"]["display"]) == 2


def test_final_text_and_mixed_tool_group_are_protected_with_trusted_locators():
    saved = loads_strict((
        Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
    ).read_text(encoding="utf-8"))
    tool_messages = copy.deepcopy(saved["turn"][0]["messages"][:2])
    tool_messages[0]["blocks"].insert(0, {"kind": "text", "text": "hello"})
    final = message(102, "assistant", '{"text":"hello"}')
    history = [message(), *tool_messages, final]
    protected = [{"message_id": uid(102), "block_index": 0}]
    result = prepare(
        config(context_regex=[context_regex()]), history,
        logical_floors=[[uid(100)], [item["message_id"] for item in history[1:]]],
        protected_blocks=protected,
    )
    assert result["send_view"]["messages"][:-1] == history
    assert context_view_messages(result["send_view"])[1:4] == history[1:4]
    assert result["protected_blocks"] == protected
    validate_context_preparation(
        result, history=history, protected_blocks=protected,
        logical_floors=[[uid(100)], [item["message_id"] for item in history[1:]]],
    )


@pytest.mark.parametrize("text_source,activated", [("original", False), ("derived", True)])
def test_lorebook_explicit_scan_source_observes_send_view_after_context_regex(text_source, activated):
    cfg = config(
        context_regex=[context_regex()], lorebooks=[lorebook(text_source=text_source)],
    )
    result = prepare(cfg, prompt_message_ids=None)
    assert (len(result["collection"]["items"]) == 2) == activated
    assert result["lorebook"][0]["trace"]["decisions"][0]["status"] == (
        "activated" if activated else "not_matched"
    )
    if activated:
        item = result["collection"]["items"][1]
        assert item["source"]["activation"]["view_digest"] == content_digest(result["send_view"])
        assert item["source"]["activation"]["message_id"] == ROOT
        assert result["s0"][1]["blocks"][0]["text"] == "world"


def test_display_only_match_does_not_activate_lorebook():
    cfg = config(
        context_regex=[context_regex(purposes=["display"])], lorebooks=[lorebook()],
    )
    result = prepare(cfg, prompt_message_ids=None)
    assert len(result["collection"]["items"]) == 1
    assert result["lorebook"][0]["trace"]["decisions"][0]["status"] == "not_matched"


def test_lorebook_declaration_order_is_stable_and_no_activated_text_rescan():
    cfg = config(
        lorebooks=[
            lorebook(keyword="hello", text="dragon"),
            lorebook(240, keyword="hello", text="second"),
            lorebook(260, keyword="dragon", text="not recursively activated"),
        ],
    )
    result = prepare(cfg, prompt_message_ids=None)
    assert [item["text"] for item in result["collection"]["items"]] == ["instruction", "dragon", "second"]
    assert result["lorebook"][2]["trace"]["decisions"][0]["status"] == "not_matched"
    assert all(UUID(identity).version == 4 for identity in result["prompt_message_ids"])


def test_materialized_exact_config_is_kept_and_verified_without_catalog():
    item = definition()
    exact = {
        "schema_version": 1, "kind": "config", "config_id": uid(330), "revision": 3,
        "name": "Exact config", "inputs": [{
            "name": "base", "kind": "item", "item_instance_id": uid(10),
            "item_id": uid(1), "revision": 1, "overrides": {},
        }],
    }
    cfg = config(
        prompt_config={"config": exact, "items": [item], "groups": []},
    )
    assert prepare(cfg)["config"]["prompt_config"] == {"config": exact, "items": [item], "groups": []}
    bad = copy.deepcopy(cfg)
    bad["prompt_config"]["items"][0]["text"] = "changed exact definition"
    with pytest.raises(ContractValidationError, match="differ"):
        validate_prompt_context_config(bad)


@pytest.mark.parametrize("field", sorted(config()))
def test_config_requires_each_explicit_field(field):
    value = config()
    value.pop(field)
    with pytest.raises(ContractValidationError):
        validate_prompt_context_config(value)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("kind", "context"),
    ("steps", ()), ("context_regex", ()), ("lorebooks", ()),
    ("collection", "plain text"), ("variables", {}), ("limits", {}),
    ("prompt_config", {"latest": True}), ("unknown", True),
])
def test_config_rejects_unknown_versions_fields_and_implicit_inputs(field, value):
    cfg = config()
    cfg[field] = value
    with pytest.raises((ContractValidationError, PromptProcessingError)):
        validate_prompt_context_config(cfg)


@pytest.mark.parametrize("section", ["processing", "context_regex", "lorebook", "assembly"])
def test_limits_require_exact_explicit_fields(section):
    cfg = config()
    cfg["limits"][section]["unknown"] = 1
    with pytest.raises(ContractValidationError):
        validate_prompt_context_config(cfg)


def test_all_static_validation_precedes_any_processing(monkeypatch):
    cfg = config(context_regex=[context_regex()], lorebooks=[lorebook()])
    cfg["lorebooks"][0]["prompts"] = []
    dispatched = []
    monkeypatch.setattr(
        "phase1_agent.prompt_preparation.apply_context_regex",
        lambda *args, **kwargs: dispatched.append("regex"),
    )
    with pytest.raises(ContractValidationError, match="Missing"):
        prepare(cfg)
    assert dispatched == []


@pytest.mark.parametrize("which", ["node_id", "input_name", "book_instance", "item_instance"])
def test_duplicate_lorebook_and_cross_pipeline_identities_are_rejected(which):
    first, second = lorebook(), lorebook(240)
    if which == "node_id":
        second["request"]["node_id"] = first["request"]["node_id"]
    elif which == "input_name":
        second["request"]["input_name"] = first["request"]["input_name"]
    elif which == "book_instance":
        second["request"]["book_instance_id"] = first["request"]["book_instance_id"]
    else:
        second["request"]["entry_instances"][0]["item_instance_id"] = uid(10)
    with pytest.raises(ContractValidationError, match="Duplicate"):
        config(lorebooks=[first, second])


def test_node_count_limit_applies_to_entire_preparation_not_each_subpipeline():
    cfg = config(context_regex=[context_regex()], steps=[macro(enabled=False)])
    cfg["limits"]["processing"]["max_nodes"] = 1
    with pytest.raises(ContractValidationError, match="node limit"):
        prepare(cfg)


def test_variable_snapshot_requires_exact_preparation_scope():
    cfg = config(variables=variables(name="fixed"))
    cfg["variables"]["workflow_session_id"] = uid(900)
    cfg["variables"]["values"]["workflow_session_id"]["value"] = uid(900)
    with pytest.raises(ContractValidationError, match="another preparation scope"):
        prepare(cfg)


def test_enabled_macro_has_no_implicit_live_variable_fallback():
    cfg = config(
        items=[effective(text="{{name}}", interpolation="variables")], steps=[macro()],
    )
    with pytest.raises(PromptProcessingError) as caught:
        prepare(cfg)
    assert caught.value.code == "prompt_variables_missing"


def test_literal_macro_does_not_require_variables_or_expand_text():
    result = prepare(config(items=[effective(text="{{name}}")], steps=[macro()]))
    assert result["collection"]["items"][0]["text"] == "{{name}}"
    assert result["processing"]["stages"][0]["trace"]["processed_instances"] == []


def test_final_overall_capacity_refuses_prompt_plus_root_instead_of_cropping():
    cfg = config(items=[effective(text="x" * 20)])
    cfg["limits"]["assembly"]["max_total_chars"] = 25
    with pytest.raises(PromptProcessingError):
        prepare(cfg)


def test_display_capacity_is_enforced_separately_from_final_send():
    cfg = config(context_regex=[
        context_regex(pattern="hello", replacement="x" * 300, purposes=["display"]),
    ])
    cfg["limits"]["assembly"]["max_total_chars"] = 100
    with pytest.raises(PromptProcessingError):
        prepare(cfg)


def test_prompt_expansion_capacity_applies_before_assembly():
    cfg = config(
        items=[effective(text="{{name}}", interpolation="variables")],
        variables=variables(name="x" * 30), steps=[macro()],
    )
    cfg["limits"]["processing"]["max_total_chars"] = 20
    with pytest.raises(PromptProcessingError):
        prepare(cfg)


def test_lorebook_work_limit_is_shared_across_books():
    cfg = config(items=[], lorebooks=[lorebook(keyword="hello"), lorebook(240, keyword="hello")])
    root_text = dumps_pretty(node_input()["payload"])
    cfg["limits"]["lorebook"]["max_work_units"] = len(root_text.casefold()) + len("hello")
    with pytest.raises(PromptProcessingError) as caught:
        prepare(cfg, prompt_message_ids=None)
    assert caught.value.code == "lorebook_resource_limit"


@pytest.mark.parametrize("prompt_ids", [
    [], [ROOT], ["not-a-message-id"], [uid(500), uid(501)],
])
def test_prompt_message_ids_are_exact_unique_and_do_not_alias_root(prompt_ids):
    with pytest.raises(ContractValidationError):
        prepare(prompt_message_ids=prompt_ids)


def test_root_identity_cannot_alias_selected_history():
    with pytest.raises(ContractValidationError, match="Duplicate"):
        prepare(history=[message(), message(101, "assistant")], root_message_id=uid(100))


def test_history_boundary_validation_happens_before_context_workers(monkeypatch):
    cfg = config(context_regex=[context_regex()])
    dispatched = []
    monkeypatch.setattr(
        "phase1_agent.prompt_preparation.apply_context_regex",
        lambda *args, **kwargs: dispatched.append("regex"),
    )
    with pytest.raises(ContractValidationError):
        prepare(cfg, history=[message(), message(101, "assistant")], logical_floors=[])
    assert dispatched == []


@pytest.mark.parametrize("field", sorted(prepare()))
def test_preparation_requires_each_versioned_field(field):
    value = prepare()
    value.pop(field)
    with pytest.raises(ContractValidationError):
        validate_context_preparation(value)


def test_mutation_is_detected_before_any_evidence_interpretation():
    result = prepare()
    result["s0"][0]["blocks"][0]["text"] = "changed"
    with pytest.raises(ContractValidationError, match="digest"):
        validate_context_preparation(result)


@pytest.mark.parametrize("change", [
    "metadata", "unselected_text", "trace", "final_collection", "s0",
    "root", "source", "scope", "protected", "projection", "floor", "prompt_id",
])
def test_structural_corruption_is_rejected_even_with_recomputed_digest(change):
    result = prepare(config(
        items=[effective(text="literal")], steps=[macro()], context_regex=[context_regex()],
    ))
    if change == "metadata":
        result["processing"]["stages"][0]["collection"]["items"][0]["role"] = "assistant"
    elif change == "unselected_text":
        result["processing"]["stages"][0]["collection"]["items"][0]["text"] = "changed"
    elif change == "trace":
        result["processing"]["stages"][0]["trace"]["processed_text"] = True
    elif change == "final_collection":
        result["collection"]["items"][0]["text"] = "changed"
    elif change == "s0":
        result["s0"][0]["blocks"][0]["text"] = "changed"
    elif change == "root":
        result["root_locator"]["block_index"] = True
    elif change == "source":
        result["canonical_messages"][-1]["source"]["visible_message_id"] = uid(999)
    elif change == "scope":
        result["send_view"]["node_binding_id"] = uid(999)
    elif change == "protected":
        result["send_view"]["protected_blocks"] = [{"message_id": ROOT, "block_index": 0}]
    elif change == "projection":
        result["send_view"]["projection_version"] = 2
    elif change == "floor":
        result["logical_floors"] = []
    else:
        result["prompt_message_ids"] = [ROOT]
    with pytest.raises(ContractValidationError):
        validate_context_preparation(rehash(result))


@pytest.mark.parametrize("kwargs", [
    {"workflow_session_id": uid(999)}, {"node_binding_id": uid(999)},
    {"parent_turn_id": uid(999)}, {"history": [message()]},
    {"protected_blocks": [{"message_id": ROOT, "block_index": 0}]},
    {"logical_floors": [[uid(100)]]}, {"node_input": node_input(payload={"text": "different"})},
    {"config": config(items=[])},
])
def test_owner_supplied_trusted_inputs_are_not_replaced_by_self_asserted_evidence(kwargs):
    with pytest.raises(ContractValidationError, match="trusted"):
        validate_context_preparation(prepare(), **kwargs)


def test_forged_context_override_cannot_change_nonselected_source():
    history = [message(), message(101, "assistant")]
    result = prepare(config(context_regex=[context_regex(sources=["human"])]), history)
    stage = result["context_regex"]["send"][0]
    override = {"message_id": uid(101), "block_index": 0, "text": "forged assistant"}
    stage["view"]["overrides"].append(override)
    stage["trace"]["output_digest"] = content_digest(stage["view"])
    result["send_view"] = stage["view"]
    with pytest.raises(ContractValidationError, match="unselected"):
        validate_context_preparation(rehash(result))


def test_context_trace_requires_canonical_override_order():
    history = [message(), message(101, "assistant")]
    result = prepare(config(context_regex=[context_regex()]), history)
    stage = result["context_regex"]["send"][0]
    stage["view"]["overrides"].reverse()
    stage["trace"]["output_digest"] = content_digest(stage["view"])
    result["send_view"] = stage["view"]
    with pytest.raises(ContractValidationError, match="different order"):
        validate_context_preparation(rehash(result))


def test_lorebook_activated_source_cannot_be_forged_or_detached_from_exact_book():
    result = prepare(config(lorebooks=[lorebook(keyword="hello")]), prompt_message_ids=None)
    result["lorebook"][0]["collection"]["items"][0]["source"]["entry_revision"] = 99
    with pytest.raises(ContractValidationError, match="Lorebook activation correspondence"):
        validate_context_preparation(rehash(result))


@pytest.mark.parametrize("target", ["processed_text", "processing_limits", "context_active", "context_block", "lorebook_revision"])
def test_evidence_uses_strict_json_number_and_boolean_types(target):
    result = prepare(config(
        steps=[macro()], context_regex=[context_regex()],
        lorebooks=[lorebook(keyword="hello", text_source="original")],
    ), prompt_message_ids=None)
    if target == "processed_text":
        result["processing"]["stages"][0]["trace"]["processed_text"] = 0
    elif target == "processing_limits":
        result["processing"]["limits"]["regex"]["timeout_seconds"] = 1
    elif target == "context_active":
        result["context_regex"]["send"][0]["trace"]["active"] = 1
    elif target == "context_block":
        result["context_regex"]["send"][0]["trace"]["processed_blocks"][0]["block_index"] = False
    else:
        result["lorebook"][0]["trace"]["decisions"][0]["entry_revision"] = 4.0
    with pytest.raises(ContractValidationError):
        validate_context_preparation(rehash(result))
