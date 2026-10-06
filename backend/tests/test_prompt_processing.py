"""Prompt preprocessing is serial, bounded, pure and replayable."""

import copy
from dataclasses import replace

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_processing import (
    PromptProcessingLimits, process_prompt_collection, process_prompt_item,
    process_text, validate_prompt_steps,
)
from phase1_agent.prompt_regex import RegexLimits
from phase1_agent.prompt_variables import create_variable_registry, create_variable_snapshot


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def item(number=10, text="{{a}}", **changes):
    result = {
        "item_id": uid(1), "revision": 1, "name": "source item", "text": text,
        "role": "system", "enabled": True, "placement": "before", "depth": None,
        "order": 0, "interpolation": "variables", "source": {"kind": "configuration"},
        "item_instance_id": uid(number), "group_id": None, "group_revision": None,
        "group_instance_id": None, "input_name": "named", "declaration_index": 0,
    }
    result.update(changes)
    return result


def collection(*items):
    return {"schema_version": 1, "kind": "prompt_collection", "items": list(items)}


def variables(**values):
    registry = create_variable_registry(
        workflow_id=uid(100), revision=1,
        definitions=[{"name": name, "type": "string", "default": value} for name, value in values.items()],
    )
    return create_variable_snapshot(registry, workflow_session_id=uid(101), node_binding_id=uid(102))


def step(number=200, kind="macro", **changes):
    result = {
        "schema_version": 1, "node_id": uid(number), "kind": kind, "enabled": True,
        "select": {"mode": "all"},
    }
    if kind == "regex":
        result["rule"] = {"pattern": "original", "replacement": "{{b}}", "flags": "", "mode": "all"}
    result.update(changes)
    return result


def test_single_node_never_reprocesses_generated_macros_but_next_node_can():
    source = collection(item())
    snapshot = variables(a="{{b}}", b="resolved")
    original = copy.deepcopy((source, snapshot))
    first = process_prompt_collection(source, [step()], variables=snapshot)
    assert first["value"]["items"][0]["text"] == "{{b}}"
    second = process_prompt_collection(source, [step(), step(201)], variables=snapshot)
    assert second["value"]["items"][0]["text"] == "resolved"
    assert (source, snapshot) == original
    assert second == process_prompt_collection(source, [step(), step(201)], variables=snapshot)
    metadata = copy.deepcopy(second["value"]["items"][0])
    metadata["text"] = "{{a}}"
    assert metadata == source["items"][0]
    assert second["trace"][0]["output_digest"] == second["trace"][1]["input_digest"]
    assert second["syntax"] == {"macro": "simple-variables-v1", "regex": "python-re-v1"}


def test_regex_never_invokes_macro_and_following_macro_is_explicit():
    original = process_text("original", [step(kind="regex")])
    assert original["value"] == "{{b}}"
    processed = process_text(
        "original", [step(kind="regex"), step(201)], variables=variables(b="resolved"),
    )
    assert processed["value"] == "resolved"


def test_literal_item_skips_macro_in_every_node_and_preserves_metadata():
    source = item(text="{{missing}}", interpolation="literal")
    result = process_prompt_item(source, [step(), step(201)])
    assert result["value"] == source
    assert all(trace["processed_instances"] == [] for trace in result["trace"])
    regex = step(kind="regex", rule={
        "pattern": "missing", "replacement": "other", "flags": "", "mode": "all",
    })
    assert process_prompt_item(source, [regex])["value"]["text"] == "{{other}}"


def test_selector_is_scoped_by_group_and_preserves_declared_item_order():
    first = item(group_id=uid(3), group_revision=1, group_instance_id=uid(4))
    second = item(group_id=uid(3), group_revision=1, group_instance_id=uid(5))
    selector = {"mode": "instances", "instances": [{
        "group_instance_id": uid(5), "item_instance_id": uid(10),
    }]}
    result = process_prompt_collection(
        collection(first, second), [step(select=selector)], variables=variables(a="chosen"),
    )
    assert [entry["text"] for entry in result["value"]["items"]] == ["{{a}}", "chosen"]
    assert result["trace"][0]["processed_instances"] == selector["instances"]


def test_selector_missing_is_not_silently_ignored_even_if_disabled():
    selector = {"mode": "instances", "instances": [{
        "group_instance_id": None, "item_instance_id": uid(999),
    }]}
    with pytest.raises(PromptProcessingError) as caught:
        process_prompt_collection(collection(item()), [step(select=selector, enabled=False)])
    assert caught.value.code == "prompt_selector_missing"


def test_disabled_node_is_retained_in_trace_without_evaluation():
    result = process_text("{{missing}}", [step(enabled=False)])
    assert result["value"] == "{{missing}}"
    assert result["trace"][0]["input_digest"] == result["trace"][0]["output_digest"]
    assert not result["trace"][0]["processed_text"]


def test_missing_macro_value_is_locatable_and_redacted():
    private_text = "secret {{unknown}}"
    with pytest.raises(PromptProcessingError) as caught:
        process_prompt_item(item(text=private_text), [step()], variables=variables(a="value"))
    error = caught.value
    assert error.node_id == uid(200)
    assert error.item_instance_id == uid(10)
    assert error.offset == 7
    assert "secret" not in str(error)
    assert "unknown" not in str(error)


def test_macro_requires_explicit_snapshot_not_live_environment():
    with pytest.raises(PromptProcessingError) as caught:
        process_text("{{workflow_session_id}}", [step()])
    assert caught.value.code == "prompt_variables_missing"


@pytest.mark.parametrize("limit,source,steps,snapshot", [
    (PromptProcessingLimits(max_nodes=0), collection(item(text="")), [step()], variables(a="")),
    (PromptProcessingLimits(max_items=0), collection(item(text="")), [], None),
    (PromptProcessingLimits(max_total_chars=2), collection(item(text="long")), [], None),
    (PromptProcessingLimits(max_total_chars=10), collection(item()), [step()], variables(a="x" * 11)),
    (PromptProcessingLimits(max_total_chars=10), collection(item(), item(11)), [step()], variables(a="xxxxxx")),
])
def test_pipeline_limits_apply_to_input_and_intermediate_outputs(limit, source, steps, snapshot):
    original = copy.deepcopy(source)
    with pytest.raises(PromptProcessingError) as caught:
        process_prompt_collection(source, steps, variables=snapshot, limits=limit)
    assert caught.value.code == "prompt_pipeline_limit"
    assert source == original


def test_regex_limits_are_cumulative_across_selected_items():
    limits = PromptProcessingLimits(regex=RegexLimits(max_matches=1, timeout_seconds=3))
    with pytest.raises(PromptProcessingError) as caught:
        process_prompt_collection(
            collection(item(text="original"), item(11, "original")), [step(kind="regex")], limits=limits,
        )
    assert caught.value.code == "regex_match_limit"
    assert caught.value.node_id == uid(200)


def test_empty_collection_still_validates_regex_capture_references():
    regex = step(kind="regex", rule={
        "pattern": "x", "replacement": r"\2", "flags": "", "mode": "all",
    })
    with pytest.raises(PromptProcessingError) as caught:
        process_prompt_collection(collection(), [regex])
    assert caught.value.code == "regex_invalid_replacement"


@pytest.mark.parametrize("mutate", [
    lambda steps: steps.append(copy.deepcopy(steps[0])),
    lambda steps: steps[0].update(schema_version=True),
    lambda steps: steps[0].update(kind="expression"),
    lambda steps: steps[0].update(enabled=1),
    lambda steps: steps[0].update(node_id="node"),
    lambda steps: steps[0].update(extra=True),
    lambda steps: steps[0].update(select={"mode": "recent"}),
    lambda steps: steps[0].update(select={"mode": "all", "instances": []}),
    lambda steps: steps[0].update(select={"mode": "instances", "instances": [
        {"group_instance_id": None, "item_instance_id": uid(10)},
        {"group_instance_id": None, "item_instance_id": uid(10)},
    ]}),
])
def test_step_schema_is_strict(mutate):
    steps = [step()]
    mutate(steps)
    with pytest.raises(ContractValidationError):
        validate_prompt_steps(steps)


def test_text_input_cannot_recover_prompt_metadata_or_use_instance_selector():
    with pytest.raises(ContractValidationError):
        process_text("plain", [step(select={"mode": "instances", "instances": []})])


@pytest.mark.parametrize("change", [
    {"max_nodes": True}, {"max_items": -1}, {"max_total_chars": 1.0}, {"macro": {}},
    {"regex": RegexLimits(timeout_seconds=float("nan"))},
])
def test_invalid_limits_are_rejected_before_even_disabled_processing(change):
    with pytest.raises(ContractValidationError):
        replace(PromptProcessingLimits(), **change)
