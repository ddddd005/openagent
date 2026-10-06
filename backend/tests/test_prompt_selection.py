"""Public prompt references are exact, detached and fully materialized."""

from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.prompt_preparation import make_prompt_context_config
from phase1_agent.prompt_selection import (
    compose_selected_prompt_context, make_prompt_selection_plan, resolve_prompt_selection,
    validate_prompt_selection, validate_prompt_selection_plan,
)
from phase1_agent.prompt_variables import create_variable_registry, create_variable_snapshot
from phase1_agent.variable_preparation import make_variable_assignment_plan
from test_prompt_preparation import context_regex, lorebook, macro, regex
from test_workflow_prepared_context import collection as default_collection
from test_workflow_prompt_configs import config, group, item, uid


def selection(nodes=None):
    return {
        "schema_version": 1, "kind": "workflow_prompt_selection",
        "nodes": {"A": {"config_id": uid(3), "revision": 1}} if nodes is None else nodes,
    }


class ExactCatalog:
    def __init__(self):
        self.records = {
            ("config", uid(3), 1): config(),
            ("group", uid(2), 1): group(),
            ("item", uid(1), 1): item(),
        }
        self.reads = []

    def get_revision(self, kind, identity, revision):
        self.reads.append((kind, identity, revision))
        return self.records.get((kind, identity, revision))

    def get_head(self, *_args):
        raise AssertionError("An exact selection must not read current catalog heads")


def resolve(value=None, catalog=None, **kwargs):
    args = {
        "workflow_definition_id": uid(100), "workflow_session_id": uid(101),
        "node_bindings": {"A": uid(102), "B": uid(103)},
    }
    args.update(kwargs)
    return resolve_prompt_selection(
        selection() if value is None else value, ExactCatalog() if catalog is None else catalog,
        **args,
    )


def test_exact_selection_materializes_reused_groups_and_explicit_scope_presets():
    catalog = ExactCatalog()
    result = resolve(catalog=catalog)["A"]
    assert result["schema_version"] == 1 and result["kind"] == "prompt_context_config"
    assert len(result["collection"]["items"]) == 2
    assert {row["group_instance_id"] for row in result["collection"]["items"]} == {uid(20), uid(21)}
    assert result["prompt_config"] == {"config": config(), "groups": [group()], "items": [item()]}
    assert result["steps"] == result["context_regex"] == result["lorebooks"] == []
    assert result["variables"]["registry"]["workflow_id"] == uid(100)
    assert result["variables"]["registry"]["revision"] == 1
    assert result["variables"]["registry"]["definitions"] == []
    assert result["variables"]["workflow_session_id"] == uid(101)
    assert result["variables"]["node_binding_id"] == uid(102)
    assert set(result["limits"]) == {"processing", "context_regex", "lorebook", "assembly"}
    assert catalog.reads == [("config", uid(3), 1), ("group", uid(2), 1), ("item", uid(1), 1)]


def test_two_nodes_share_exact_read_cache_but_have_detached_consumer_snapshots():
    catalog = ExactCatalog()
    result = resolve(selection({"A": {"config_id": uid(3), "revision": 1}, "B": {"config_id": uid(3), "revision": 1}}), catalog)
    assert len(catalog.reads) == 3
    assert result["A"]["variables"]["node_binding_id"] == uid(102)
    assert result["B"]["variables"]["node_binding_id"] == uid(103)
    result["A"]["collection"]["items"][0]["text"] = "A edit"
    assert result["B"]["collection"]["items"][0]["text"] == "Same text"
    assert catalog.records[("item", uid(1), 1)]["text"] == "Same text"


def test_b_only_and_empty_selection_do_not_materialize_other_stages():
    assert set(resolve(selection({"B": {"config_id": uid(3), "revision": 1}}))) == {"B"}
    catalog = ExactCatalog()
    assert resolve(selection({}), catalog) == {}
    assert catalog.reads == []


def test_disabled_references_are_still_materialized_and_validated():
    catalog = ExactCatalog()
    saved_config = catalog.records[("config", uid(3), 1)]
    for entry in saved_config["inputs"]:
        entry["enabled"] = False
    result = resolve(catalog=catalog)["A"]
    assert not result["collection"]["items"]
    assert result["prompt_config"]["groups"] == [group()]
    assert result["prompt_config"]["items"] == [item()]
    catalog.records.pop(("item", uid(1), 1))
    with pytest.raises(ContractValidationError) as caught:
        resolve(catalog=catalog)
    assert caught.value.status_code == 500
    assert caught.value.reason_code == "storage_contract_violation"


def test_selected_old_revision_does_not_follow_newer_definitions():
    catalog = ExactCatalog()
    updated = item(2)
    updated["text"] = "New unselected revision"
    catalog.records[("item", uid(1), 2)] = updated
    result = resolve(catalog=catalog)["A"]
    assert [row["text"] for row in result["collection"]["items"]] == ["Same text", "Same text"]
    assert all(revision == 1 for _kind, _identity, revision in catalog.reads)


@pytest.mark.parametrize("value", [
    None, [], "latest", {"schema_version": 1, "kind": "workflow_prompt_selection"},
    {"schema_version": True, "kind": "workflow_prompt_selection", "nodes": {}},
    {"schema_version": 2, "kind": "workflow_prompt_selection", "nodes": {}},
    {"schema_version": 1, "kind": "config", "nodes": {}},
    {"schema_version": 1, "kind": "workflow_prompt_selection", "nodes": [], "extra": True},
    selection({"Output": {"config_id": uid(3), "revision": 1}}),
    selection({"A": None}), selection({"A": {"config_id": uid(3)}}),
    selection({"A": {"config_id": uid(3), "revision": "latest"}}),
    selection({"A": {"config_id": uid(3), "revision": True}}),
    selection({"A": {"config_id": uid(3), "revision": 1.0}}),
    selection({"A": {"config_id": uid(3), "revision": 0}}),
    selection({"A": {"config_id": uid(3), "revision": 2**63}}),
    selection({"A": {"config_id": "private-invalid-id", "revision": 1}}),
    selection({"A": {"config_id": uid(3), "revision": 1, "steps": []}}),
])
def test_public_selection_rejects_unknown_versions_implicit_latest_and_private_rules(value):
    with pytest.raises(ContractValidationError) as caught:
        validate_prompt_selection(value)
    assert caught.value.status_code == 400 and caught.value.reason_code == "invalid_request"
    assert "private-invalid-id" not in str(caught.value)


def test_selection_and_plan_are_returned_as_detached_strict_values():
    source = selection()
    result = validate_prompt_selection(source)
    result["nodes"]["A"]["revision"] = 2
    assert source["nodes"]["A"]["revision"] == 1
    contexts = resolve()
    plan = make_prompt_selection_plan(contexts)
    assert plan["schema_version"] == 1 and plan["kind"] == "workflow_prompt_selection_plan"
    checked = validate_prompt_selection_plan(plan)
    contexts["A"]["collection"]["items"][0]["text"] = "Changed caller"
    checked["nodes"]["A"]["collection"]["items"][0]["text"] = "Changed checked"
    assert plan["nodes"]["A"]["collection"]["items"][0]["text"] == "Same text"


@pytest.mark.parametrize("change", [
    lambda value: value.update(schema_version=True),
    lambda value: value.update(schema_version=2),
    lambda value: value.update(kind="selection"),
    lambda value: value.update(extra="not allowed"),
    lambda value: value.update(nodes={"Output": {}}),
    lambda value: value["nodes"]["A"].update(schema_version=99),
])
def test_frozen_plan_rejects_unknown_structure_and_context_versions(change):
    value = make_prompt_selection_plan(resolve())
    change(value)
    with pytest.raises(ContractValidationError):
        validate_prompt_selection_plan(value)


@pytest.mark.parametrize("bad_field", ["missing", "invalid", "different_revision"])
def test_bad_stored_material_cannot_masquerade_as_selected_definition(bad_field):
    catalog = ExactCatalog()
    if bad_field == "missing":
        catalog.records.pop(("group", uid(2), 1))
    elif bad_field == "invalid":
        catalog.records[("item", uid(1), 1)]["role"] = "tool"
    else:
        catalog.records[("config", uid(3), 1)]["revision"] = 2
    with pytest.raises(ContractValidationError) as caught:
        resolve(catalog=catalog)
    assert caught.value.status_code == 500 and caught.value.reason_code == "storage_contract_violation"


def test_missing_selected_config_is_a_not_found_not_a_storage_failure():
    catalog = ExactCatalog()
    catalog.records.clear()
    with pytest.raises(ContractValidationError) as caught:
        resolve(catalog=catalog)
    assert caught.value.status_code == 404 and caught.value.reason_code == "not_found"


@pytest.mark.parametrize("changes", [
    {"workflow_definition_id": "not-an-id"}, {"workflow_session_id": "not-an-id"},
    {"node_bindings": {}}, {"node_bindings": {"A": uid(102), "B": uid(102)}},
    {"node_bindings": {"A": uid(102), "B": "not-an-id"}},
])
def test_explicit_workflow_and_consumer_scope_is_required(changes):
    with pytest.raises(ContractValidationError):
        resolve(**changes)


def test_exact_reader_program_errors_are_not_silently_treated_as_missing():
    class Broken(ExactCatalog):
        def get_revision(self, *_args):
            raise RuntimeError("fixture read failure")
    with pytest.raises(RuntimeError, match="fixture read failure"):
        resolve(catalog=Broken())


def configured_context(version=2):
    registry = create_variable_registry(
        workflow_id=uid(100), revision=1, definitions=[
            {"name": "name", "type": "string", "default": "default name"},
            {"name": "counter", "type": "integer", "default": 0},
        ],
    )
    variables = create_variable_snapshot(
        registry, workflow_session_id=uid(500), node_binding_id=uid(501),
    )
    value = make_prompt_context_config(
        default_collection("configured prompt"), variables=variables,
        steps=[macro(200), regex(201, "Same", "Processed")],
        lorebooks=[lorebook(220)], context_regex=[context_regex(210)],
        variable_plan=make_variable_assignment_plan([{
            "node_id": "assign-name", "name": "name",
            "source": {"kind": "constant", "value": "assigned name"},
        }]) if version == 2 else None,
    )
    value["limits"]["assembly"]["max_total_chars"] = 12345
    return value


@pytest.mark.parametrize("version", [1, 2])
def test_selected_material_preserves_prepared_version_variables_processing_and_capacity(version):
    selected, default = resolve()["A"], configured_context(version)
    originals = deepcopy((selected, default))
    combined = compose_selected_prompt_context(selected, default)
    assert combined["collection"] == selected["collection"]
    assert combined["prompt_config"] == selected["prompt_config"]
    assert combined["schema_version"] == version
    assert all(combined[field] == value for field, value in default.items()
               if field not in ("collection", "prompt_config"))
    assert combined["variables"]["registry"]["definitions"] == default["variables"]["registry"]["definitions"]
    if version == 2:
        assert combined["variable_plan"] == default["variable_plan"]
    else:
        assert "variable_plan" not in combined
    combined["variables"]["values"]["name"]["value"] = "caller changed"
    combined["steps"][0]["enabled"] = False
    combined["collection"]["items"][0]["text"] = "caller changed prompt"
    assert (selected, default) == originals


def test_basic_selection_without_prepared_default_retains_preset_only_v1():
    selected = resolve()["A"]
    combined = compose_selected_prompt_context(selected)
    assert combined == selected
    assert combined["schema_version"] == 1
    assert combined["variables"]["registry"]["definitions"] == []
    combined["collection"]["items"][0]["text"] = "caller changed"
    assert selected["collection"]["items"][0]["text"] == "Same text"


def test_combined_selector_cannot_silently_drop_unavailable_configured_instances():
    selected, default = resolve()["A"], configured_context()
    instance = default["collection"]["items"][0]
    default["steps"][0]["select"] = {"mode": "instances", "instances": [{
        "group_instance_id": instance["group_instance_id"],
        "item_instance_id": instance["item_instance_id"],
    }]}
    with pytest.raises(ContractValidationError, match="configured selector"):
        compose_selected_prompt_context(selected, default)


def test_selected_collection_keeps_matching_instance_and_potential_lorebook_selectors():
    selected, default = resolve()["A"], configured_context()
    selected_instance = selected["collection"]["items"][0]
    lorebook_instance = default["lorebooks"][0]["request"]["entry_instances"][0]
    default["steps"][0]["select"] = {"mode": "instances", "instances": [
        {field: selected_instance[field] for field in ("group_instance_id", "item_instance_id")},
        {"group_instance_id": None, "item_instance_id": lorebook_instance["item_instance_id"]},
    ]}
    combined = compose_selected_prompt_context(selected, default)
    assert combined["steps"] == default["steps"]
    assert combined["lorebooks"] == default["lorebooks"]


@pytest.mark.parametrize("mutation,match", [
    (lambda value: value["variables"]["registry"].update(workflow_id=uid(999)), "registry owner"),
    (lambda value: value["variable_plan"]["assignments"][0].update(name="unknown"), "registered"),
    (lambda value: value["variable_plan"]["assignments"][0].update(name="counter"), "declared type"),
    (lambda value: value["variable_plan"]["assignments"][0].update(
        name="counter", source={"kind": "root_input_text"},
    ), "registered string"),
])
def test_known_variable_plan_incompatibilities_refuse_combined_configuration(mutation, match):
    selected, default = resolve()["A"], configured_context()
    if match == "registry owner":
        registry = create_variable_registry(
            workflow_id=uid(999), revision=1,
            definitions=default["variables"]["registry"]["definitions"],
        )
        default["variables"] = create_variable_snapshot(
            registry, workflow_session_id=uid(500), node_binding_id=uid(501),
        )
    else:
        mutation(default)
    with pytest.raises(ContractValidationError, match=match):
        compose_selected_prompt_context(selected, default)


def test_selected_collection_cannot_bypass_configured_processing_capacity():
    selected, default = resolve()["A"], configured_context()
    default["limits"]["processing"]["max_items"] = 1
    with pytest.raises(ContractValidationError, match="limits"):
        compose_selected_prompt_context(selected, default)


@pytest.mark.parametrize("target", ["selected", "default"])
def test_composition_fully_validates_each_configuration_before_using_material(target):
    selected, default = resolve()["A"], configured_context()
    value = selected if target == "selected" else default
    value["schema_version"] = 99
    with pytest.raises(ContractValidationError):
        compose_selected_prompt_context(selected, default)
