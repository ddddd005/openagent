"""Identity, exact revision and explicit override guarantees for prompt config."""

import copy
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import pytest
from jsonschema import Draft202012Validator

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import loads_strict
from phase1_agent.prompt_config import (
    PROMPT_CONFIG_SCHEMAS,
    expand_prompt_config,
    prompt_record_identity,
    validate_prompt_record,
)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def item(number=1, revision=1, **changes):
    value = {
        "schema_version": 1,
        "kind": "item",
        "item_id": uid(number),
        "revision": revision,
        "name": "shared name",
        "text": "same text",
        "role": "system",
        "enabled": True,
        "placement": "before",
        "depth": None,
        "order": 0,
        "interpolation": "variables",
        "source": {"kind": "configuration"},
    }
    value.update(changes)
    return value


def member(instance=10, number=1, revision=1, **overrides):
    return {
        "item_instance_id": uid(instance),
        "item_id": uid(number),
        "revision": revision,
        "overrides": overrides,
    }


def group(number=2, revision=1, members=None):
    return {
        "schema_version": 1,
        "kind": "group",
        "group_id": uid(number),
        "revision": revision,
        "name": "reusable",
        "members": [member()] if members is None else members,
    }


def item_input(name="one", instance=20, number=1, revision=1, **overrides):
    return {"name": name, "kind": "item", **member(instance, number, revision, **overrides)}


def group_input(name="one", instance=30, number=2, revision=1, enabled=True, overrides=None):
    return {
        "name": name,
        "kind": "group",
        "group_instance_id": uid(instance),
        "group_id": uid(number),
        "revision": revision,
        "enabled": enabled,
        "member_overrides": [] if overrides is None else overrides,
    }


def config(*inputs):
    return {
        "schema_version": 1,
        "kind": "config",
        "config_id": uid(3),
        "revision": 1,
        "name": "Agent A",
        "inputs": list(inputs),
    }


def resolver(*records):
    catalog = {
        (
            record[{"item": "item_id", "group": "group_id"}[record["kind"]]],
            record["revision"],
        ): record
        for record in records
    }
    return lambda identity, revision: catalog[(identity, revision)]


@pytest.mark.parametrize("kind,value", [
    ("item", item()),
    ("group", group()),
    ("config", config(item_input())),
])
def test_record_schemas_validate_and_return_detached_values(kind, value):
    Draft202012Validator.check_schema(PROMPT_CONFIG_SCHEMAS[kind])
    result = validate_prompt_record(kind, value)
    assert result == value
    assert result is not value
    result["name"] = "changed"
    assert value["name"] != "changed"
    assert prompt_record_identity(kind, value) == (
        value[{"item": "item_id", "group": "group_id", "config": "config_id"}[kind]], 1,
    )


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
def test_all_prompt_text_roles_are_valid(role):
    assert validate_prompt_record("item", item(role=role))["role"] == role


@pytest.mark.parametrize("changes", [
    {"schema_version": 2},
    {"schema_version": True},
    {"revision": 0},
    {"revision": True},
    {"revision": 1.0},
    {"order": True},
    {"enabled": 1},
    {"role": "tool"},
    {"role": "model"},
    {"placement": "unknown"},
    {"placement": "middle", "depth": None},
    {"placement": "middle", "depth": True},
    {"placement": "middle", "depth": -1},
    {"depth": 0},
    {"interpolation": "script"},
    {"item_id": "shared name"},
    {"item_id": "00000000-0000-1000-8000-000000000001"},
    {"unknown": "not accepted"},
    {"source": {"kind": "model"}},
    {"source": {"kind": "configuration", "secret": "forbidden"}},
])
def test_item_invalid_fields_are_rejected(changes):
    with pytest.raises(ContractValidationError):
        validate_prompt_record("item", item(**changes))


def test_middle_depth_and_negative_order_are_explicit_valid_configuration():
    value = item(placement="middle", depth=0, order=-50)
    assert validate_prompt_record("item", value) == value


@pytest.mark.parametrize("kind,value", [
    ("item", item()),
    ("group", group()),
    ("config", config()),
])
def test_record_kind_must_match_selector(kind, value):
    value["kind"] = "unknown"
    with pytest.raises(ContractValidationError):
        validate_prompt_record(kind, value)


@pytest.mark.parametrize("value", [
    {"unexpected": object()},
    {"unexpected": float("nan")},
    {"unexpected": ("tuple",)},
    {1: "non-string key"},
    [],
    "not a record",
])
def test_python_values_and_non_object_records_are_not_persistable(value):
    with pytest.raises(ContractValidationError):
        validate_prompt_record("item", value)


def test_cycle_rejected_before_schema_or_copy():
    value = item()
    value["cycle"] = value
    with pytest.raises(ContractValidationError, match="Cyclic"):
        validate_prompt_record("item", value)


def test_invalid_utf8_rejected():
    with pytest.raises(ContractValidationError, match="UTF-8"):
        validate_prompt_record("item", item(text="\ud800"))


def test_unknown_kind_rejected():
    with pytest.raises(ContractValidationError, match="Unknown prompt"):
        validate_prompt_record("other", item())


def test_repeated_definition_and_same_text_are_not_deduplicated():
    value = config(item_input("first", 20), item_input("second", 21))
    expanded = expand_prompt_config(value, resolver(item()), resolver())
    assert len(expanded) == 2
    assert [record["item_instance_id"] for record in expanded] == [uid(20), uid(21)]
    assert [record["declaration_index"] for record in expanded] == [0, 1]
    assert all(record["text"] == "same text" for record in expanded)
    assert all(record["group_instance_id"] is None for record in expanded)


def test_same_group_twice_keeps_stable_member_and_distinct_group_instances():
    definition = group(members=[member(10), member(11)])
    value = config(group_input("first", 30), group_input("second", 31))
    expanded = expand_prompt_config(value, resolver(item()), resolver(definition))
    assert len(expanded) == 4
    assert [record["item_instance_id"] for record in expanded] == [uid(10), uid(11)] * 2
    assert [record["group_instance_id"] for record in expanded] == [uid(30), uid(30), uid(31), uid(31)]
    assert [record["declaration_index"] for record in expanded] == [0, 1, 2, 3]
    assert all(record["group_id"] == uid(2) and record["group_revision"] == 1 for record in expanded)


def test_instance_namespace_is_local_to_each_group():
    definition = group(members=[member(20)])
    value = config(item_input("standalone", 20), group_input("group", 30))
    expanded = expand_prompt_config(value, resolver(item()), resolver(definition))
    assert [record["item_instance_id"] for record in expanded] == [uid(20), uid(20)]
    assert [record["group_instance_id"] for record in expanded] == [None, uid(30)]


@pytest.mark.parametrize("value", [
    config(item_input("same", 20), item_input("same", 21)),
    config(item_input("first", 20), item_input("second", 20)),
    config(group_input("first", 30), group_input("second", 30)),
    config(item_input("item", 30), group_input("group", 30)),
])
def test_duplicate_input_names_or_top_level_instances_are_rejected(value):
    with pytest.raises(ContractValidationError, match="Duplicate prompt"):
        validate_prompt_record("config", value)


def test_duplicate_group_member_instance_rejected_even_when_definition_matches():
    with pytest.raises(ContractValidationError, match="group member instance"):
        validate_prompt_record("group", group(members=[member(), member()]))


def test_duplicate_same_scope_override_targets_rejected():
    patch = {"item_instance_id": uid(10), "overrides": {"text": "override"}}
    with pytest.raises(ContractValidationError, match="member override target"):
        validate_prompt_record("config", config(group_input(overrides=[patch, copy.deepcopy(patch)])))


@pytest.mark.parametrize("overrides", [
    {"item_id": uid(999)},
    {"item_instance_id": uid(999)},
    {"revision": 2},
    {"source": {"kind": "configuration"}},
    {"secret": "not accepted"},
    {"order": True},
    {"enabled": "false"},
])
def test_overrides_cannot_change_identity_or_inject_unknown_metadata(overrides):
    reference = item_input()
    reference["overrides"] = overrides
    with pytest.raises(ContractValidationError):
        validate_prompt_record("config", config(reference))


def test_nearest_override_wins_without_mutating_definitions():
    definition = item(text="definition", order=10)
    reusable = group(members=[member(text="preset", order=20, role="user")])
    patch = {"item_instance_id": uid(10), "overrides": {"text": "local", "order": 30}}
    value = config(group_input(overrides=[patch]))
    original = copy.deepcopy((definition, reusable, value))
    expanded = expand_prompt_config(value, resolver(definition), resolver(reusable))
    assert expanded[0]["text"] == "local"
    assert expanded[0]["order"] == 30
    assert expanded[0]["role"] == "user"
    assert expanded[0]["source"] == {"kind": "configuration"}
    assert (definition, reusable, value) == original
    expanded[0]["source"]["kind"] = "changed"
    assert definition["source"]["kind"] == "configuration"


def test_definition_disabled_can_be_explicitly_enabled_by_nearest_override():
    expanded = expand_prompt_config(
        config(item_input(enabled=True)), resolver(item(enabled=False)), resolver(),
    )
    assert len(expanded) == 1
    assert expanded[0]["enabled"] is True


def test_disabled_entries_are_excluded_without_reassigning_later_indexes():
    value = config(
        item_input("disabled", 20, enabled=False),
        group_input("disabled group", 30, enabled=False),
        item_input("enabled", 21),
    )
    expanded = expand_prompt_config(value, resolver(item()), resolver(group(members=[member(10), member(11)])))
    assert len(expanded) == 1
    assert expanded[0]["declaration_index"] == 3


def test_disabled_group_cannot_be_enabled_by_member_override():
    patch = {"item_instance_id": uid(10), "overrides": {"enabled": True}}
    expanded = expand_prompt_config(
        config(group_input(enabled=False, overrides=[patch])),
        resolver(item()), resolver(group()),
    )
    assert expanded == []


def test_disabled_references_are_still_resolved_and_validated():
    calls = []

    def invalid_item(identity, revision):
        calls.append((identity, revision))
        return item(role="tool")

    with pytest.raises(ContractValidationError):
        expand_prompt_config(
            config(group_input(enabled=False)), invalid_item, resolver(group()),
        )
    assert calls == [(uid(1), 1)]


def test_unknown_member_override_rejected_for_disabled_group():
    patch = {"item_instance_id": uid(999), "overrides": {"text": "unknown target"}}
    with pytest.raises(ContractValidationError, match="not in the referenced group"):
        expand_prompt_config(
            config(group_input(enabled=False, overrides=[patch])),
            resolver(item()), resolver(group()),
        )


def test_invalid_effective_position_rejected_even_for_disabled_item():
    with pytest.raises(ContractValidationError):
        expand_prompt_config(
            config(item_input(placement="middle", enabled=False)),
            resolver(item()), resolver(),
        )


def test_position_and_depth_can_be_overridden_together():
    expanded = expand_prompt_config(
        config(item_input(placement="middle", depth=2)),
        resolver(item()), resolver(),
    )
    assert expanded[0]["placement"] == "middle"
    assert expanded[0]["depth"] == 2


@pytest.mark.parametrize("changes", [{"item_id": uid(999)}, {"revision": 2}])
def test_resolver_cannot_substitute_another_definition_or_latest_revision(changes):
    with pytest.raises(ContractValidationError, match="does not match its reference"):
        expand_prompt_config(config(item_input()), lambda *_: item(**changes), resolver())


def test_group_resolver_cannot_substitute_latest_revision():
    with pytest.raises(ContractValidationError, match="does not match its reference"):
        expand_prompt_config(config(group_input()), resolver(item()), lambda *_: group(revision=2))


def test_exact_revisions_are_independent_of_catalog_latest_or_selectability():
    historical = item(text="revision one")
    latest = item(revision=2, text="revision two")
    expanded = expand_prompt_config(config(item_input(revision=1)), resolver(historical, latest), resolver())
    assert expanded[0]["text"] == "revision one"
    # A frozen resolver remains usable after deletion from the editable catalog.
    frozen_items = resolver(copy.deepcopy(historical))
    expanded_again = expand_prompt_config(config(item_input(revision=1)), frozen_items, resolver())
    assert expanded_again == expanded


def test_same_exact_reference_is_resolved_once_per_expansion():
    calls = []

    def changing_provider(identity, revision):
        calls.append((identity, revision))
        return item(text=f"read {len(calls)}")

    expanded = expand_prompt_config(
        config(item_input("first", 20), item_input("second", 21)), changing_provider, resolver(),
    )
    assert calls == [(uid(1), 1)]
    assert [record["text"] for record in expanded] == ["read 1", "read 1"]


def test_expansion_preserves_declaration_order_and_does_not_apply_insertion_sort():
    expanded = expand_prompt_config(
        config(item_input("z", 21, order=5), item_input("a", 20, order=-5)),
        resolver(item()), resolver(),
    )
    assert [record["input_name"] for record in expanded] == ["z", "a"]
    assert [record["order"] for record in expanded] == [5, -5]


def test_nested_group_reference_is_not_in_version_one():
    nested = {
        "kind": "group", "group_instance_id": uid(40), "group_id": uid(2),
        "revision": 1, "enabled": True, "member_overrides": [],
    }
    with pytest.raises(ContractValidationError):
        validate_prompt_record("group", group(members=[nested]))


def test_expansion_requires_callable_resolvers_even_for_empty_config():
    with pytest.raises(ContractValidationError, match="callable"):
        expand_prompt_config(config(), None, None)


def test_prompt_schema_export_matches_separate_runtime_registry():
    package = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(dir=package / "tests", prefix="prompt-export-") as directory:
        output = Path(directory) / "prompts.json"
        subprocess.run(
            [
                sys.executable, str(package / "scripts" / "export_prompt_config_schemas.py"),
                "--output", str(output),
            ],
            cwd=package, check=True, capture_output=True, text=True,
        )
        exported = loads_strict(output.read_text(encoding="utf-8"))
        assert exported["$defs"] == PROMPT_CONFIG_SCHEMAS
        assert set(exported["$defs"]) == {"item", "group", "config"}
        committed = loads_strict((package / "schemas" / "prompt-config-v1.schema.json").read_text(encoding="utf-8"))
        assert committed == exported
