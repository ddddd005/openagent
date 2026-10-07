"""Incremental nodes retain typed data and resolve variables only on replacement."""

from copy import deepcopy

import pytest

from phase1_agent.contract_json import canonical_bytes, content_digest, loads_strict
from phase1_agent.preparation_program import (
    execute_preparation_program, validate_preparation_program, validate_program_result,
)
from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_regex import RegexLimits
from phase1_agent.prompt_values import collection_from_config, make_context_view


COUNTDOWN = "\u5012\u8ba1\u65f6"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def collection(text="Instructions"):
    definition = {
        "schema_version": 1, "kind": "item", "item_id": uid(1001), "revision": 1,
        "name": "Prompt source", "text": text, "role": "system", "enabled": True,
        "placement": "before", "depth": None, "order": 0, "interpolation": "variables",
        "source": {"kind": "configuration"},
    }
    configuration = {
        "schema_version": 1, "kind": "config", "config_id": uid(1002), "revision": 1,
        "name": "Program inputs", "inputs": [{
            "name": "instructions", "kind": "item", "item_instance_id": uid(1003),
            "item_id": uid(1001), "revision": 1, "overrides": {},
        }],
    }
    return collection_from_config(configuration, lambda *_: definition, lambda *_: None)


def node(identity, kind, config, **inputs):
    return {"node_id": identity, "kind": kind, "config": config, "inputs": inputs}


def program(nodes, prompt=None, context=None):
    return {"schema_version": 1, "kind": "prompt_preparation_program",
            "nodes": nodes, "outputs": {"prompt": prompt, "context": context}}


def view(messages=None, protected=None):
    return make_context_view(
        [] if messages is None else messages, workflow_session_id=uid(9100),
        node_binding_id=uid(9103), parent_turn_id=None, projection_version=1,
        purpose="send", protected_blocks=[] if protected is None else protected,
    )


def root(text="hello"):
    return {"schema_version": 1, "input_id": uid(9101), "port_id": "request",
            "payload_schema_ref": {"schema_id": "writing_text", "version": 1},
            "source": {"kind": "visible_message", "visible_message_id": uid(9102)},
            "payload": {"text": text}}


def execute(value, *, materials=None, context=None, state=None, cached=None):
    return execute_preparation_program(
        value, collection("literal") if materials is None else materials,
        view() if context is None else context,
        {"revision": 0, "values": {}} if state is None else state,
        limits=RegexLimits(timeout_seconds=3), node_input=root(), cached=cached,
    )


def countdown_program():
    return program([
        node("register", "variable-register", {"name": COUNTDOWN, "type": "integer", "initial": 10}),
        node("source", "text", {"text": "{{" + COUNTDOWN + "}}"}),
        node("capture", "variable-replace", {"mode": "text"}, input="source"),
        node("register-b", "variable-register", {"name": "b", "type": "string"}, text="capture"),
        node("update", "variable-assign", {"name": COUNTDOWN, "operation": "subtract", "value": 1}),
        node("later", "text", {"text": "{{b}}/{{" + COUNTDOWN + "}}"}),
        node("replace-later", "variable-replace", {"mode": "text"}, input="later"),
        node("convert", "text-to-prompt", {
            "item_instance_id": uid(9199), "role": "system", "placement": "before",
            "depth": None, "order": 0,
        }, input="replace-later"),
    ], prompt="convert")


def test_unicode_dynamic_replacement_and_explicit_b_capture():
    result = execute(countdown_program())
    assert result["collection"]["items"][0]["text"] == "10/9"
    assert result["state"]["values"][COUNTDOWN]["value"] == 9
    assert result["state"]["values"]["b"]["value"] == "10"
    assert len(result["stages"]) == len(countdown_program()["nodes"])
    assert result["stages"][2]["output"]["text"] == "10"
    assert validate_program_result(
        result, collection("literal"), view(), {"revision": 0, "values": {}}, countdown_program(),
    ) == result


def test_registration_preserves_session_value_and_explicit_text_initial_after_edit():
    first = execute(countdown_program())
    state = first["state"]
    state["revision"] = 5
    state["values"]["b"] = {"type": "string", "source": "assignment", "value": "user-edited"}
    second = execute(countdown_program(), state=state)
    assert second["collection"]["items"][0]["text"] == "user-edited/8"
    assert second["state"]["values"]["b"]["value"] == "user-edited"


def test_single_pass_does_not_recursively_replace_macro_value():
    value = program([
        node("register", "variable-register", {"name": "a", "type": "string", "initial": "{{a}}"}),
        node("text", "text", {"text": "{{a}}"}),
        node("replace", "variable-replace", {"mode": "text"}, input="text"),
    ])
    assert execute(value)["stages"][-1]["output"]["text"] == "{{a}}"


@pytest.mark.parametrize("initial,expected", [(None, "missing_macro_variable"), ("", None)])
def test_unassigned_is_distinct_from_empty_text(initial, expected):
    config = {"name": COUNTDOWN, "type": "string"}
    if initial is not None:
        config["initial"] = initial
    value = program([
        node("register", "variable-register", config),
        node("text", "text", {"text": "{{" + COUNTDOWN + "}}"}),
        node("replace", "variable-replace", {"mode": "text"}, input="text"),
    ])
    if expected:
        with pytest.raises(PromptProcessingError) as failure:
            execute(value)
        assert failure.value.code == expected and failure.value.node_id == "replace"
    else:
        assert execute(value)["stages"][-1]["output"]["text"] == ""


def test_prompt_regex_preserves_all_metadata_and_fixed_literal_macro():
    materials = collection("city {{literal}}")
    value = program([
        node("prompt", "prompt-source", {"instances": [{
            "group_instance_id": None, "item_instance_id": uid(1003),
        }]}),
        node("regex", "regex", {
            "mode": "prompt", "rule": {"pattern": "city", "replacement": "town", "flags": "", "mode": "all"},
        }, input="prompt"),
    ], prompt="regex")
    result = execute(value, materials=materials)
    output = result["collection"]["items"][0]
    assert output["text"] == "town {{literal}}"
    assert {key: entry for key, entry in output.items() if key != "text"} == {
        key: entry for key, entry in materials["items"][0].items() if key != "text"
    }


def test_text_regex_uses_python_capture_and_explicit_root_input():
    value = program([
        node("root", "text", {"source": "root_input"}),
        node("regex", "regex", {
            "mode": "text", "rule": {"pattern": "(hello)", "replacement": r"\1!", "flags": "", "mode": "all"},
        }, input="root"),
    ])
    result = execute(value)
    assert result["stages"][-1]["output"] == {"kind": "text", "text": "hello!"}


def test_context_macro_derives_send_only_and_preserves_final_protection():
    messages = [{
        "schema_version": 1, "message_id": uid(9201), "role": "user",
        "source": {"kind": "human", "visible_message_id": uid(9202)},
        "blocks": [{"kind": "text", "text": "{{" + COUNTDOWN + "}}"}],
    }, {
        "schema_version": 1, "message_id": uid(9203), "role": "assistant",
        "source": {"kind": "model", "request_id": uid(9204)},
        "blocks": [{"kind": "text", "text": "{{" + COUNTDOWN + "}}"}],
    }]
    context = view(messages, [{"message_id": uid(9203), "block_index": 0}])
    value = program([
        node("register", "variable-register", {"name": COUNTDOWN, "type": "integer", "initial": 9}),
        node("context", "context-source", {}),
        node("replace", "variable-replace", {"mode": "prompt"}, input="context"),
    ], context="replace")
    result = execute(value, context=context)
    assert result["view"]["messages"] == messages
    assert result["view"]["overrides"] == [{"message_id": uid(9201), "block_index": 0, "text": "9"}]
    assert result["view"]["protected_blocks"] == context["protected_blocks"]
    assert context["overrides"] == []


def test_shared_node_cache_does_not_update_variable_twice():
    first = execute(countdown_program())
    nodes = {entry["node_id"]: entry for entry in countdown_program()["nodes"]}
    cache = {stage["node_id"]: {
        "config_digest": content_digest(nodes[stage["node_id"]]), "output": stage["output"],
    } for stage in first["stages"]}
    second = execute(countdown_program(), state=first["state"], cached=cache)
    assert second["state"]["values"][COUNTDOWN]["value"] == 9
    assert second["collection"]["items"][0]["text"] == "10/9"
    assert all(stage["cached"] for stage in second["stages"])


@pytest.mark.parametrize("bad", [
    [node("loop", "regex", {"mode": "text", "rule": {"pattern": "", "replacement": "", "flags": "", "mode": "all"}},
          input="loop")],
    [node("one", "text", {"text": "one"}), node("one", "text", {"text": "two"})],
    [node("reserved", "variable-register", {"name": "workflow_session_id", "type": "string"})],
])
def test_program_rejects_cycles_duplicate_nodes_and_reserved_names(bad):
    with pytest.raises(PromptProcessingError):
        validate_preparation_program(program(bad))


def test_frozen_program_rejects_metadata_or_protected_context_mutation():
    result = execute(countdown_program())
    mutated = deepcopy(result)
    mutated["stages"][-1]["output"]["items"][0]["role"] = "assistant"
    mutated["evidence_digest"] = content_digest({
        key: entry for key, entry in mutated.items() if key != "evidence_digest"
    })
    with pytest.raises(PromptProcessingError):
        validate_program_result(
            mutated, collection("literal"), view(), {"revision": 0, "values": {}}, countdown_program(),
        )


def test_macro_expansion_rejects_output_growth_before_joining_large_value():
    value = program([
        node("register", "variable-register", {"name": "a", "type": "string", "initial": "x" * 600_000}),
        node("source", "text", {"text": "{{a}}{{a}}"}),
        node("replace", "variable-replace", {"mode": "text"}, input="source"),
    ])
    with pytest.raises(PromptProcessingError) as failure:
        execute(value)
    assert failure.value.code == "macro_output_limit"


@pytest.mark.parametrize("initial,operation", [(2**53 - 1, "add"), (-(2**53 - 1), "subtract")])
def test_integer_update_rejects_cross_language_overflow(initial, operation):
    value = program([
        node("register", "variable-register", {"name": "counter", "type": "integer", "initial": initial}),
        node("update", "variable-assign", {"name": "counter", "operation": operation, "value": 1}),
    ])
    with pytest.raises(PromptProcessingError) as failure:
        execute(value)
    assert failure.value.code == "variable_type_mismatch"


def test_collector_order_is_explicit_and_survives_canonical_storage():
    nodes = []
    order, inputs = [], {}
    for index in range(12):
        source_id, convert_id, port = f"text-{index}", f"convert-{index}", f"input-{index}"
        nodes.extend([
            node(source_id, "text", {"text": str(index)}),
            node(convert_id, "text-to-prompt", {
                "item_instance_id": uid(9600 + index), "role": "system", "placement": "before",
                "depth": None, "order": index,
            }, input=source_id),
        ])
        inputs[port] = convert_id
        order.append(port)
    nodes.append(node("collect", "prompt-collector", {"input_order": order}, **inputs))
    value = program(nodes, prompt="collect")
    stored = loads_strict(canonical_bytes(value).decode("utf-8"))
    assert [item["text"] for item in execute(stored)["collection"]["items"]] == [str(index) for index in range(12)]
