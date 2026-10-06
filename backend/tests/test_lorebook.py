"""Single literal-keyword activation and honest versioned prompt provenance."""

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import content_digest, loads_strict
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.lorebook import (
    LorebookLimits, activate_lorebook, validate_lorebook, validate_lorebook_request,
)
from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_ports import diagnose_regex_ports, require_regex_port_schema
from phase1_agent.prompt_processing import process_prompt_collection, process_prompt_item
from phase1_agent.prompt_values import (
    collect_prompt_inputs, context_view_messages, make_context_view, render_prompt_text,
    validate_prompt_collection, validate_prompt_item,
)
from phase1_agent.prompt_variables import create_variable_registry, create_variable_snapshot


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def prompt(number=1, **changes):
    value = {
        "schema_version": 1, "kind": "item", "item_id": uid(number), "revision": 3,
        "name": "Prompt title not sent", "text": "The city is quiet.", "role": "system",
        "enabled": True, "placement": "before", "depth": None, "order": 0,
        "interpolation": "literal", "source": {"kind": "configuration"},
    }
    value.update(changes)
    return value


def entry(number=10, keyword="city", prompt_number=1, **changes):
    value = {
        "entry_id": uid(number), "revision": 2, "name": "Entry memo not sent",
        "enabled": True, "keyword": keyword,
        "prompt_ref": {"item_id": uid(prompt_number), "revision": 3},
    }
    value.update(changes)
    return value


def book(*entries):
    return {
        "schema_version": 1, "kind": "lorebook", "book_id": uid(100), "revision": 4,
        "name": "Book title not sent", "entries": list(entries) if entries else [entry()],
    }


def request(definition=None, **changes):
    definition = book() if definition is None else definition
    value = {
        "schema_version": 1, "kind": "lorebook_request", "node_id": uid(101),
        "book_instance_id": uid(102), "input_name": "world",
        "entry_instances": [
            {"entry_id": item["entry_id"], "item_instance_id": uid(200 + index)}
            for index, item in enumerate(definition["entries"])
        ],
        "scan": {"text_source": "original", "sources": ["human", "upstream_node", "model"]},
    }
    value.update(changes)
    return value


def message(number=300, text="city", source="human"):
    sources = {
        "human": {"kind": "human", "visible_message_id": uid(400)},
        "upstream_node": {"kind": "upstream_node", "output_id": uid(401)},
        "model": {"kind": "model", "request_id": uid(402)},
        "prompt": {
            "kind": "prompt", "prompt_id": uid(1), "revision": 3,
            "item_instance_id": uid(200), "group_instance_id": None,
        },
        "protocol_feedback": {"kind": "protocol_feedback", "request_id": uid(402)},
    }
    return {
        "schema_version": 3 if source == "prompt" else 1, "message_id": uid(number),
        "role": "assistant" if source == "model" else "user",
        "source": sources[source], "blocks": [{"kind": "text", "text": text}],
    }


def view(messages=None, *, purpose="send", protected=None):
    return make_context_view(
        [message()] if messages is None else messages,
        workflow_session_id=uid(500), node_binding_id=uid(501), parent_turn_id=None,
        projection_version=1, purpose=purpose, protected_blocks=[] if protected is None else protected,
    )


def activate(definition=None, selection=None, context=None, resolver=None, **kwargs):
    definition = book() if definition is None else definition
    return activate_lorebook(
        definition, request(definition) if selection is None else selection,
        view() if context is None else context,
        (lambda *args: prompt()) if resolver is None else resolver, **kwargs,
    )


def test_one_keyword_matches_once_and_preserves_exact_prompt_and_activation():
    definition, selection = book(), request()
    context = view([message(text="CITY city"), message(301, "city", "model")])
    original = copy.deepcopy((definition, selection, context))
    calls = []

    def resolve(identity, revision):
        calls.append((identity, revision))
        return prompt(role="assistant", placement="middle", depth=2, order=88)

    result = activate(definition, selection, context, resolve)
    assert calls == [(uid(1), 3)]
    assert len(result["collection"]["items"]) == 1
    item = result["collection"]["items"][0]
    assert result["collection"]["schema_version"] == item["schema_version"] == 2
    assert item["kind"] == "prompt_item"
    assert (item["role"], item["placement"], item["depth"], item["order"]) == ("assistant", "middle", 2, 88)
    assert item["text"] == "The city is quiet."
    assert item["source"]["book_id"] == uid(100)
    assert item["source"]["book_revision"] == 4
    assert item["source"]["entry_id"] == uid(10)
    assert item["source"]["entry_revision"] == 2
    assert item["source"]["activation"]["message_id"] == uid(300)
    assert item["source"]["activation"]["block_index"] == 0
    assert item["source"]["activation"]["keyword_digest"] == content_digest("city")
    assert item["group_id"] is item["group_revision"] is item["group_instance_id"] is None
    assert (definition, selection, context) == original
    assert activate(definition, selection, context, resolve) == result


@pytest.mark.parametrize("keyword,text,matched", [
    ("CITY", "a city skyline", True), ("city", "electricity", True),
    ("dragon", "Dragons", True), ("\u9f99", "\u5de8\u9f99\u6765\u4e86", True),
    ("STRASSE", "Stra\u00dfe", True), (" city ", "city", False),
    ("city", "quiet village", False), ("/city/i", "city", False),
    ("a,b", "a", False), ("a,b", "a,b", True),
    ("{{city}}", "city", False), ("{{city}}", "{{city}}", True),
])
def test_literal_casefold_substring_has_no_regex_tokenization_or_keyword_macro(keyword, text, matched):
    result = activate(book(entry(keyword=keyword)), context=view([message(text=text)]))
    assert bool(result["collection"]["items"]) == matched


def test_empty_memo_is_valid_and_never_enters_prompt_text():
    result = activate(book(entry(name="")))
    assert render_prompt_text(result["collection"], separator="\n", max_output_chars=100) == "The city is quiet."


def test_no_match_and_disabled_entry_do_not_resolve_prompt():
    def unexpected(*args):
        raise AssertionError("Inactive entries must not read prompt definitions")
    for definition in (book(entry(enabled=False)), book(entry(keyword="elsewhere"))):
        result = activate(definition, resolver=unexpected)
        assert result["collection"] == {"schema_version": 2, "kind": "prompt_collection", "items": []}


def test_disabled_prompt_does_not_emit_a_fake_enabled_item():
    result = activate(resolver=lambda *args: prompt(enabled=False))
    assert not result["collection"]["items"]
    assert result["trace"]["decisions"][0]["status"] == "prompt_disabled"


def test_order_is_declaration_order_and_same_text_is_not_deduplicated():
    definition = book(entry(), entry(11, "city", enabled=False), entry(12, "sky"))
    calls = []

    def resolver(*args):
        calls.append(args)
        return prompt()

    result = activate(definition, context=view([message(text="city sky")]), resolver=resolver)
    items = result["collection"]["items"]
    assert [item["source"]["entry_id"] for item in items] == [uid(10), uid(12)]
    assert [item["declaration_index"] for item in items] == [0, 2]
    assert [item["item_instance_id"] for item in items] == [uid(200), uid(202)]
    assert items[0]["text"] == items[1]["text"]
    assert len(calls) == 1


def test_no_recursive_activation_from_output_prompt_text():
    definition = book(entry(), entry(11, "secret"))
    result = activate(definition, resolver=lambda *args: prompt(text="secret"))
    assert [item["source"]["entry_id"] for item in result["collection"]["items"]] == [uid(10)]
    assert result["trace"]["decisions"][1]["status"] == "not_matched"


def test_different_messages_or_blocks_cannot_manufacture_a_keyword_match():
    split = message(text="ci")
    split["blocks"].append({"kind": "text", "text": "ty"})
    for messages in ([message(text="ci"), message(301, "ty")], [split]):
        assert not activate(context=view(messages))["collection"]["items"]


@pytest.mark.parametrize("source,matched", [
    ("human", True), ("upstream_node", True), ("model", True),
    ("prompt", False), ("protocol_feedback", False),
])
def test_scan_uses_provenance_not_role(source, matched):
    assert bool(activate(context=view([message(source=source)]))["collection"]["items"]) == matched


def test_explicit_source_selection_is_obeyed():
    selection = request(scan={"text_source": "original", "sources": ["model"]})
    assert not activate(selection=selection)["collection"]["items"]
    assert activate(selection=selection, context=view([message(source="model")]))["collection"]["items"]


def test_tool_groups_are_not_scanned_and_text_final_is_not_modified():
    path = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
    messages = loads_strict(path.read_text(encoding="utf-8"))["turn"][0]["messages"][:2]
    messages[0]["blocks"].insert(0, {"kind": "text", "text": "city"})
    original = copy.deepcopy(messages)
    assert not activate(context=view(messages))["collection"]["items"]
    assert messages == original
    final = message(text='{"text":"city"}', source="model")
    context = view([final], protected=[{"message_id": uid(300), "block_index": 0}])
    assert activate(context=context)["collection"]["items"]
    assert context_view_messages(context) == [final]


def test_original_and_derived_scans_are_explicit_and_recorded():
    context = view([message(text="quiet")])
    context["overrides"] = [{"message_id": uid(300), "block_index": 0, "text": "city"}]
    original = copy.deepcopy(context)
    assert not activate(context=context)["collection"]["items"]
    result = activate(
        selection=request(scan={"text_source": "derived", "sources": ["human"]}), context=context,
    )
    evidence = result["collection"]["items"][0]["source"]["activation"]
    assert evidence["text_source"] == "derived"
    assert evidence["text_digest"] == content_digest("city")
    assert context == original


def test_display_context_cannot_silently_be_used_as_send_activation_input():
    with pytest.raises(ContractValidationError, match="send"):
        activate(context=view(purpose="display"))


@pytest.mark.parametrize("resolved", [prompt(revision=4), prompt(number=2)])
def test_exact_reference_mismatch_fails_instead_of_reading_latest(resolved):
    with pytest.raises(ContractValidationError, match="exact reference"):
        activate(resolver=lambda *args: resolved)


def test_missing_reference_and_resolver_program_failure_are_explicit():
    for error in (KeyError("missing"), RuntimeError("resolver program fault")):
        def broken(*args):
            raise error
        with pytest.raises(type(error)):
            activate(resolver=broken)


def test_empty_book_and_empty_context_are_valid_without_random_identity_generation():
    definition = book()
    definition["entries"] = []
    result = activate(definition, context=view([]))
    assert result["collection"]["schema_version"] == 2
    assert not result["collection"]["items"]
    assert result["trace"]["decisions"] == []


@pytest.mark.parametrize("keyword", ["", " \t\n", None, [], ["city"], 1, True, "\ud800"])
def test_keyword_must_be_one_nonempty_utf8_literal_string(keyword):
    with pytest.raises(ContractValidationError):
        validate_lorebook(book(entry(keyword=keyword)))


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(schema_version=True),
    lambda value: value.update(schema_version=2),
    lambda value: value.update(kind="group"),
    lambda value: value.update(book_id="book"),
    lambda value: value.update(revision=0),
    lambda value: value.update(revision=True),
    lambda value: value.update(extra=True),
    lambda value: value["entries"].append(copy.deepcopy(value["entries"][0])),
    lambda value: value["entries"][0].update(enabled=1),
    lambda value: value["entries"][0].update(revision=0),
    lambda value: value["entries"][0].update(entry_id="entry"),
    lambda value: value["entries"][0].update(keysecondary=[]),
    lambda value: value["entries"][0]["prompt_ref"].update(revision=True),
    lambda value: value["entries"][0]["prompt_ref"].update(extra=True),
])
def test_book_definitions_reject_unknown_complex_behaviors_and_bad_types(mutate):
    definition = book()
    mutate(definition)
    with pytest.raises(ContractValidationError):
        validate_lorebook(definition)


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(schema_version=True),
    lambda value: value.update(node_id="node"),
    lambda value: value.update(extra=True),
    lambda value: value["scan"].update(text_source="latest"),
    lambda value: value["scan"].update(sources=["user"]),
    lambda value: value["scan"].update(sources=[]),
    lambda value: value["scan"].update(sources=["human", "human"]),
    lambda value: value["entry_instances"].append(copy.deepcopy(value["entry_instances"][0])),
    lambda value: value["entry_instances"][0].update(item_instance_id="instance"),
])
def test_activation_request_is_strict_and_detached(mutate):
    selection = request()
    mutate(selection)
    with pytest.raises(ContractValidationError):
        validate_lorebook_request(selection)


def test_reference_instance_mapping_must_be_complete_not_arrival_order():
    for instances in ([], [{"entry_id": uid(11), "item_instance_id": uid(201)}]):
        with pytest.raises(ContractValidationError, match="complete entry set"):
            activate(selection=request(entry_instances=instances))


@pytest.mark.parametrize("field,maximum", [
    ("max_entries", 0), ("max_scan_blocks", 0), ("max_scan_chars", 3),
    ("max_keyword_chars", 3), ("max_output_chars", 1), ("max_work_units", 7),
])
def test_actual_limits_reject_without_partial_outputs_or_private_error_text(field, maximum):
    definition, context = book(), view()
    original = copy.deepcopy((definition, context))
    with pytest.raises(PromptProcessingError) as caught:
        activate(definition, context=context, limits=replace(LorebookLimits(), **{field: maximum}))
    assert caught.value.code == "lorebook_resource_limit"
    assert caught.value.node_id == uid(101)
    assert "city" not in str(caught.value)
    assert (definition, context) == original


def test_match_work_and_output_limits_are_shared_across_entries():
    definition = book(entry(), entry(11))
    for limits in (
        LorebookLimits(max_work_units=15), LorebookLimits(max_output_chars=30),
    ):
        with pytest.raises(PromptProcessingError):
            activate(definition, limits=limits)


def test_casefold_expansion_counts_against_real_character_limit():
    context = view([message(text="\u00df\u00df")])
    with pytest.raises(PromptProcessingError):
        activate(book(entry(keyword="s")), context=context, limits=LorebookLimits(max_scan_chars=3))


@pytest.mark.parametrize("field", [
    "max_entries", "max_scan_blocks", "max_scan_chars", "max_keyword_chars",
    "max_output_chars", "max_work_units",
])
@pytest.mark.parametrize("value", [True, -1, 1.0])
def test_limits_are_strict_nonnegative_integers(field, value):
    with pytest.raises(ContractValidationError):
        replace(LorebookLimits(), **{field: value})


def test_v1_collection_and_untagged_items_do_not_accept_lorebook_provenance():
    collection = activate()["collection"]
    item = collection["items"][0]
    untagged = copy.deepcopy(item)
    untagged.pop("schema_version")
    untagged.pop("kind")
    with pytest.raises(ContractValidationError):
        validate_prompt_item(untagged)
    legacy = copy.deepcopy(collection)
    legacy["schema_version"] = 1
    with pytest.raises(ContractValidationError, match="v1"):
        validate_prompt_collection(legacy)
    assert validate_prompt_collection(collection) == collection
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", item)


@pytest.mark.parametrize("mutate", [
    lambda item: item.update(schema_version=True),
    lambda item: item.update(schema_version=3),
    lambda item: item.update(kind="item"),
    lambda item: item["source"].update(book_revision=True),
    lambda item: item["source"].update(book_id="book"),
    lambda item: item["source"].update(extra=True),
    lambda item: item["source"].pop("book_digest"),
    lambda item: item["source"]["activation"].update(algorithm="regex"),
    lambda item: item["source"]["activation"].update(unicode_version="latest"),
    lambda item: item["source"]["activation"].update(text_source="latest"),
    lambda item: item["source"]["activation"].update(block_index=True),
    lambda item: item["source"]["activation"].update(text_digest="not a digest"),
    lambda item: item["source"]["activation"].update(message_id="message"),
    lambda item: item.update(group_id=uid(600), group_revision=1, group_instance_id=uid(601)),
])
def test_versioned_activation_provenance_is_strict(mutate):
    item = activate()["collection"]["items"][0]
    mutate(item)
    with pytest.raises(ContractValidationError):
        validate_prompt_item(item)


def test_collection_merge_processing_and_rendering_preserve_new_provenance():
    activated = activate(resolver=lambda *args: prompt(text="{{name}}", interpolation="variables"))["collection"]
    configuration = copy.deepcopy(activated["items"][0])
    configuration.pop("schema_version")
    configuration.pop("kind")
    configuration["source"] = {"kind": "configuration"}
    configuration["item_instance_id"] = uid(700)
    merged = collect_prompt_inputs(["configured", "world"], {
        "world": activated, "configured": configuration,
    })
    assert merged["schema_version"] == 2
    snapshot = create_variable_snapshot(
        create_variable_registry(workflow_id=uid(800), revision=1, definitions=[
            {"name": "name", "type": "string", "default": "Alice"},
        ]), workflow_session_id=uid(500), node_binding_id=uid(501),
    )
    macro = {
        "schema_version": 1, "node_id": uid(900), "kind": "macro",
        "enabled": True, "select": {"mode": "all"},
    }
    result = process_prompt_collection(merged, [macro], variables=snapshot)["value"]
    assert result["schema_version"] == 2
    assert result["items"][1]["source"] == activated["items"][0]["source"]
    assert render_prompt_text(result, separator="|", max_output_chars=100) == "Alice|Alice"
    regex = {
        "schema_version": 1, "node_id": uid(901), "kind": "regex",
        "enabled": True, "select": {"mode": "all"},
        "rule": {"pattern": "Alice", "replacement": "Bob", "flags": "", "mode": "all"},
    }
    result = process_prompt_collection(result, [regex])["value"]
    assert render_prompt_text(result, separator="|", max_output_chars=100) == "Bob|Bob"
    assert validate_prompt_item(result["items"][1])["source"] == activated["items"][0]["source"]
    assert process_prompt_item(result["items"][1], [])["value"] == result["items"][1]
    empty = copy.deepcopy(activated)
    empty["items"] = []
    assert collect_prompt_inputs(["world"], {"world": empty})["schema_version"] == 2
    assert process_prompt_collection(empty, [])["value"]["schema_version"] == 2


def test_repeated_books_keep_distinct_explicit_instances_and_same_text():
    first = activate()["collection"]
    selection = request(book_instance_id=uid(103), entry_instances=[
        {"entry_id": uid(10), "item_instance_id": uid(201)},
    ])
    second = activate(selection=selection)["collection"]
    merged = collect_prompt_inputs(["first", "second"], {"first": first, "second": second})
    assert len(merged["items"]) == 2
    assert merged["items"][0]["text"] == merged["items"][1]["text"]
    with pytest.raises(ContractValidationError, match="Duplicate scoped"):
        collect_prompt_inputs(["first", "again"], {"first": first, "again": first})


@pytest.mark.parametrize("schema_id", ["prompt_item", "prompt_collection"])
@pytest.mark.parametrize("explicit_mode", [False, True])
def test_regex_ports_keep_exact_v2_type_and_refuse_implicit_version_conversion(schema_id, explicit_mode):
    edges = [{
        "connection_id": uid(910 + index), "direction": direction, "port_id": direction,
        "schema_ref": {"schema_id": schema_id, "version": 2},
    } for index, direction in enumerate(("input", "output"))]
    assert require_regex_port_schema(uid(900), None, edges) == {"schema_id": schema_id, "version": 2}
    edges[1]["schema_ref"]["version"] = 1
    original = copy.deepcopy(edges)
    mode = schema_id if explicit_mode else None
    result = diagnose_regex_ports(uid(900), mode, edges)
    assert not result["executable"]
    assert result["mode"] == mode
    assert result["resolved_schema_ref"] is None
    assert {item["code"] for item in result["diagnostics"]} == {"regex_port_version_conflict"}
    assert edges == original
    with pytest.raises(PromptProcessingError):
        require_regex_port_schema(uid(900), mode, edges)
