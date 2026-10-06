from __future__ import annotations

import copy
import json

import pytest

from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_variables import (
    assign_variable,
    create_variable_registry,
    create_variable_snapshot,
    fork_variable_snapshot,
    validate_variable_registry,
    validate_variable_snapshot,
)


def registry(definitions=None):
    if definitions is None:
        definitions = [
            {"name": "name", "type": "string", "default": ""},
            {"name": "counter", "type": "integer"},
            {"name": "weight", "type": "number", "default": 1.5},
            {"name": "enabled", "type": "boolean", "default": False},
        ]
    return create_variable_registry(workflow_id="workflow-1", revision=3, definitions=definitions)


def snapshot():
    return create_variable_snapshot(
        registry(), workflow_session_id="session-1", node_binding_id="binding-1",
    )


def test_registry_keeps_exact_definition_revision_and_defaults():
    record = registry()
    assert record["schema_version"] == 1
    assert record["source"] == {"kind": "workflow_definition"}
    assert record["workflow_id"] == "workflow-1"
    assert record["revision"] == 3
    assert record["definitions"][0]["default"] == ""
    assert "default" not in record["definitions"][1]
    assert json.loads(json.dumps(record)) == record


def test_create_and_validate_return_detached_registry():
    definitions = [{"name": "value", "type": "string", "default": "original"}]
    original = registry(definitions)
    definitions[0]["default"] = "changed"
    returned = validate_variable_registry(original)
    returned["definitions"][0]["default"] = "other"
    assert original["definitions"][0]["default"] == "original"


def test_snapshot_distinguishes_empty_default_from_unassigned_and_presets():
    record = snapshot()
    assert record["values"]["name"] == {
        "type": "string", "value": "",
        "source": {"kind": "default", "workflow_id": "workflow-1", "registry_revision": 3},
    }
    assert record["values"]["counter"] == {
        "type": "integer",
        "source": {"kind": "unassigned", "workflow_id": "workflow-1", "registry_revision": 3},
    }
    assert record["values"]["workflow_session_id"] == {
        "type": "string", "value": "session-1",
        "source": {"kind": "preset", "parameter": "workflow_session_id"},
    }
    assert json.loads(json.dumps(record)) == record


def test_sessions_initialize_independently_from_same_workflow():
    definitions = registry()
    first = create_variable_snapshot(
        definitions, workflow_session_id="first", node_binding_id="binding",
    )
    second = create_variable_snapshot(
        definitions, workflow_session_id="second", node_binding_id="binding",
    )
    changed = assign_variable(first, "name", "updated", node_id="assignment")
    assert second["values"]["name"]["value"] == ""
    assert first["values"]["name"]["value"] == ""
    assert changed["values"]["name"]["value"] == "updated"
    assert second["values"]["workflow_session_id"]["value"] == "second"


def test_assignment_overwrites_only_new_snapshot_and_tracks_source():
    original = snapshot()
    first = assign_variable(original, "counter", 2, node_id="node-1")
    second = assign_variable(first, "counter", 7, node_id="node-2")
    assert "value" not in original["values"]["counter"]
    assert first["values"]["counter"]["value"] == 2
    assert second["values"]["counter"] == {
        "type": "integer", "value": 7,
        "source": {
            "kind": "assignment", "workflow_id": "workflow-1",
            "registry_revision": 3, "node_id": "node-2",
        },
    }
    second["registry"]["definitions"][0]["default"] = "new"
    assert original["registry"]["definitions"][0]["default"] == ""


def test_fork_keeps_declared_values_and_rebinds_session_without_sharing():
    parent = assign_variable(snapshot(), "counter", 11, node_id="assign")
    child = fork_variable_snapshot(parent, workflow_session_id="child")
    assert child["values"]["counter"] == parent["values"]["counter"]
    assert child["values"]["workflow_session_id"]["value"] == "child"
    assert parent["values"]["workflow_session_id"]["value"] == "session-1"
    assert child["values"]["node_binding_id"]["value"] == "binding-1"
    child["values"]["counter"]["value"] = 5
    child["registry"]["definitions"][0]["default"] = "child"
    assert parent["values"]["counter"]["value"] == 11
    assert parent["registry"]["definitions"][0]["default"] == ""
    next_parent = assign_variable(parent, "counter", 20, node_id="parent")
    assert child["values"]["counter"]["value"] == 5
    assert next_parent["values"]["counter"]["value"] == 20


@pytest.mark.parametrize("kind,value", [
    ("string", 1), ("string", None), ("integer", True), ("integer", 1.0),
    ("number", False), ("number", "1.5"), ("boolean", 1), ("boolean", "true"),
    ("number", float("nan")), ("number", float("inf")), ("number", -float("inf")),
    ("string", object()), ("string", "\ud800"),
])
def test_rejects_wrong_type_nonfinite_live_or_invalid_utf8_defaults(kind, value):
    with pytest.raises(PromptProcessingError, match="Variable"):
        registry([{"name": "var", "type": kind, "default": value}])


@pytest.mark.parametrize("definition", [
    {"name": "x", "type": "string", "extra": True},
    {"name": "x", "type": "unknown"},
    {"name": "x", "type": ["string"]},
    {"name": "x"},
    {"name": "", "type": "string"},
    {"name": "invalid-name", "type": "string"},
    {"name": "space name", "type": "string"},
    {"name": "1name", "type": "string"},
    {"name": "workflow_session_id", "type": "string"},
    {"name": "node_binding_id", "type": "string"},
])
def test_definition_shape_name_and_type_are_strict(definition):
    with pytest.raises(PromptProcessingError):
        registry([definition])


def test_duplicate_definition_rejected_even_when_identical():
    with pytest.raises(PromptProcessingError, match="repeated"):
        registry([{"name": "x", "type": "string"}] * 2)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("revision", True),
    ("revision", 1.0), ("revision", 0), ("workflow_id", ""), ("workflow_id", " "),
    ("definitions", ()), ("source", {"kind": "runtime"}), ("extra", 1),
])
def test_registry_envelope_is_strict(field, value):
    record = registry()
    record[field] = value
    with pytest.raises(PromptProcessingError):
        validate_variable_registry(record)


@pytest.mark.parametrize("name,value", [
    ("counter", True), ("counter", 1.5), ("weight", False), ("enabled", "true"),
    ("name", None), ("weight", float("inf")), ("name", object()),
])
def test_assignment_rejects_type_or_live_values_and_preserves_original(name, value):
    original = snapshot()
    before = copy.deepcopy(original)
    with pytest.raises(PromptProcessingError) as error:
        assign_variable(original, name, value, node_id="assign")
    assert error.value.node_id == "assign"
    assert original == before


@pytest.mark.parametrize("name", ["unknown", "workflow_session_id", "node_binding_id", "a b"])
def test_assignment_requires_declared_nonpreset_name(name):
    with pytest.raises(PromptProcessingError):
        assign_variable(snapshot(), name, "value", node_id="assign")


@pytest.mark.parametrize("name,value", [
    ("counter", -10), ("weight", 2), ("weight", -0.0), ("enabled", True), ("name", ""),
])
def test_assignment_accepts_exact_types_without_text_coercion(name, value):
    record = assign_variable(snapshot(), name, value, node_id="assign")
    assert type(record["values"][name]["value"]) is type(value)
    assert record["values"][name]["value"] == value


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(extra=1),
    lambda x: x.update(schema_version=True),
    lambda x: x["values"].pop("counter"),
    lambda x: x["values"].update(unknown={"type": "string", "value": ""}),
    lambda x: x["values"]["name"].update(extra=1),
    lambda x: x["values"]["name"].update(type="integer"),
    lambda x: x["values"]["name"].update(value="not-default"),
    lambda x: x["values"]["counter"].update(value=1),
    lambda x: x["values"]["name"].pop("value"),
    lambda x: x["values"]["name"]["source"].update(registry_revision=99),
    lambda x: x["values"]["name"]["source"].update(registry_revision=3.0),
    lambda x: x["values"]["name"]["source"].update(kind="unknown"),
    lambda x: x["values"]["name"]["source"].update(extra=1),
    lambda x: x["values"]["workflow_session_id"].update(value="other"),
    lambda x: x["values"]["node_binding_id"]["source"].update(parameter="other"),
])
def test_tampered_snapshot_is_rejected(mutation):
    record = snapshot()
    mutation(record)
    with pytest.raises(PromptProcessingError):
        validate_variable_snapshot(record)


def test_boolean_source_revision_not_accepted_as_integer_one():
    record = create_variable_snapshot(
        create_variable_registry(
            workflow_id="workflow", revision=1,
            definitions=[{"name": "x", "type": "integer", "default": 1}],
        ),
        workflow_session_id="session", node_binding_id="binding",
    )
    record["values"]["x"]["source"]["registry_revision"] = True
    with pytest.raises(PromptProcessingError):
        validate_variable_snapshot(record)


@pytest.mark.parametrize("operation", [
    lambda: create_variable_snapshot(registry(), workflow_session_id="", node_binding_id="node"),
    lambda: create_variable_snapshot(registry(), workflow_session_id="session", node_binding_id=object()),
    lambda: assign_variable(snapshot(), "counter", 1, node_id=" "),
    lambda: fork_variable_snapshot(snapshot(), workflow_session_id=""),
])
def test_explicit_identities_are_required(operation):
    with pytest.raises(PromptProcessingError):
        operation()


def test_cycles_and_non_json_mappings_are_rejected():
    value = registry()
    value["definitions"] = value
    with pytest.raises(PromptProcessingError):
        validate_variable_registry(value)
    from types import MappingProxyType
    with pytest.raises(PromptProcessingError):
        validate_variable_registry(MappingProxyType(registry()))
