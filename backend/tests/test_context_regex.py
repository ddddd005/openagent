"""Display/send projection scopes never rewrite historical or protocol facts."""

import copy
from pathlib import Path

import pytest

from phase1_agent.context_regex import apply_context_regex, validate_context_regex
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import loads_strict
from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_regex import RegexLimits
from phase1_agent.prompt_values import context_view_messages, make_context_view, validate_context_view


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def message(number, source="human", text="original"):
    sources = {
        "human": {"kind": "human", "visible_message_id": uid(300 + number)},
        "upstream_node": {"kind": "upstream_node", "output_id": uid(300 + number)},
        "model": {"kind": "model", "request_id": uid(300 + number)},
        "prompt": {"kind": "prompt", "prompt_id": uid(200), "revision": 1,
                   "item_instance_id": uid(201), "group_instance_id": None},
        "protocol_feedback": {"kind": "protocol_feedback", "request_id": uid(300 + number)},
        "runtime_execution_observation": {
            "kind": "runtime_execution_observation", "closeout_id": uid(301),
            "source_run_id": uid(302),
        },
    }
    return {
        "schema_version": 3 if source == "prompt" else (
            2 if source == "runtime_execution_observation" else 1
        ),
        "message_id": uid(number),
        "role": "assistant" if source == "model" else (
            "system" if source == "runtime_execution_observation" else "user"
        ),
        "source": sources[source], "blocks": [{"kind": "text", "text": text}],
    }


def view(messages=None, purpose="send", protected=None):
    return make_context_view(
        [message(10)] if messages is None else messages,
        workflow_session_id=uid(100), node_binding_id=uid(101), parent_turn_id=None,
        projection_version=1, purpose=purpose, protected_blocks=[] if protected is None else protected,
    )


def config(**changes):
    value = {
        "schema_version": 1, "kind": "context_regex", "node_id": uid(200), "enabled": True,
        "purposes": ["send"], "sources": ["human", "upstream_node", "model"],
        "rule": {"pattern": "original", "replacement": "derived", "flags": "", "mode": "all"},
    }
    value.update(changes)
    return value


def test_transform_is_detached_and_does_not_change_original_history():
    source = view()
    original = copy.deepcopy(source)
    result = apply_context_regex(source, config())
    assert source == original
    assert result["view"]["messages"] == original["messages"]
    assert context_view_messages(result["view"])[0]["blocks"][0]["text"] == "derived"
    assert result["trace"]["processed_blocks"] == [{"message_id": uid(10), "block_index": 0}]
    assert result["trace"]["regex_syntax"] == "python-re-v1"
    assert apply_context_regex(source, config()) == result


def test_display_and_send_are_separate_inputs_with_no_cross_write():
    source = [message(10)]
    send, display = view(source), view(source, purpose="display")
    first = apply_context_regex(display, config(purposes=["display"]))
    assert context_view_messages(first["view"])[0]["blocks"][0]["text"] == "derived"
    assert context_view_messages(send)[0]["blocks"][0]["text"] == "original"
    second = apply_context_regex(send, config(purposes=["display"]))
    assert second["view"] == send
    assert not second["trace"]["active"]


def test_sources_are_classified_by_provenance_not_user_role():
    messages = [message(10 + index, source) for index, source in enumerate([
        "human", "upstream_node", "model", "prompt", "protocol_feedback",
        "runtime_execution_observation",
    ])]
    result = apply_context_regex(view(messages), config(sources=["human"]))
    assert [
        message["blocks"][0]["text"] for message in context_view_messages(result["view"])
    ] == ["derived", "original", "original", "original", "original", "original"]
    result = apply_context_regex(view(messages), config())
    assert [
        message["blocks"][0]["text"] for message in context_view_messages(result["view"])
    ] == ["derived", "derived", "derived", "original", "original", "original"]


def test_closed_tool_group_including_assistant_text_is_not_transformed():
    path = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
    protocol = loads_strict(path.read_text(encoding="utf-8"))["turn"][0]["messages"][:2]
    protocol[0]["blocks"].insert(0, {"kind": "text", "text": "original"})
    source = view([message(10, "model"), *protocol])
    result = apply_context_regex(source, config())
    derived = context_view_messages(result["view"])
    assert derived[0]["blocks"][0]["text"] == "derived"
    assert derived[1:] == protocol
    assert source["messages"][1:] == protocol


def test_existing_override_cannot_hide_a_change_to_mixed_tool_protocol_message():
    path = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
    protocol = loads_strict(path.read_text(encoding="utf-8"))["turn"][0]["messages"][:2]
    protocol[0]["blocks"].insert(0, {"kind": "text", "text": "original"})
    source = view(protocol)
    source["overrides"] = [{
        "message_id": protocol[0]["message_id"], "block_index": 0, "text": "changed",
    }]
    with pytest.raises(ContractValidationError, match="protected"):
        validate_context_view(source)


def test_explicit_final_locator_and_only_that_ordinary_text_are_protected():
    messages = [message(10, "model"), message(11, "model", '{"text":"original"}')]
    source = view(messages, protected=[{"message_id": uid(11), "block_index": 0}])
    result = apply_context_regex(source, config())
    assert context_view_messages(result["view"])[0]["blocks"][0]["text"] == "derived"
    assert context_view_messages(result["view"])[1] == messages[1]
    assert result["view"]["protected_blocks"] == source["protected_blocks"]


def test_serial_node_uses_previous_projection_not_canonical_text():
    first = apply_context_regex(view(), config())
    second_config = config(node_id=uid(201), rule={
        "pattern": "derived", "replacement": "{{not_a_macro}}", "flags": "", "mode": "all",
    })
    second = apply_context_regex(first["view"], second_config)
    assert context_view_messages(second["view"])[0]["blocks"][0]["text"] == "{{not_a_macro}}"
    assert len(second["view"]["overrides"]) == 1
    assert second["view"]["messages"][0]["blocks"][0]["text"] == "original"
    assert second["trace"]["input_digest"] == first["trace"]["output_digest"]


def test_invalid_capture_is_rejected_even_when_no_source_matches():
    rule = {"pattern": "x", "replacement": r"\2", "flags": "", "mode": "all"}
    with pytest.raises(PromptProcessingError) as caught:
        apply_context_regex(view([message(10, "prompt")]), config(rule=rule))
    assert caught.value.code == "regex_invalid_replacement"
    assert caught.value.node_id == uid(200)


def test_disabled_regex_does_not_dispatch_worker(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("worker must not run")
    monkeypatch.setattr("phase1_agent.context_regex.regex_replace_many", unexpected)
    source = view()
    assert apply_context_regex(source, config(enabled=False))["view"] == source
    assert apply_context_regex(source, config(purposes=["display"]))["view"] == source


def test_invalid_limit_is_rejected_even_for_other_purpose():
    with pytest.raises(PromptProcessingError) as caught:
        apply_context_regex(view(), config(purposes=["display"]), limits=RegexLimits(timeout_seconds=float("nan")))
    assert caught.value.code == "regex_invalid_limits"


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"kind": "regex"}, {"node_id": "node"}, {"enabled": 1},
    {"extra": True}, {"sources": []}, {"sources": ["user"]}, {"sources": ["human", "human"]},
    {"sources": "human"}, {"purposes": []}, {"purposes": ["history"]},
    {"purposes": ["send", "send"]},
])
def test_context_regex_config_rejects_implicit_sources_and_history_mutation(changes):
    with pytest.raises(ContractValidationError):
        validate_context_regex(config(**changes))
