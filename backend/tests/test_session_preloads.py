"""Preload configuration contracts against synthetic types and isolated stores."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import (
    GraphCompiler, NodeDefinition, NodePort, NodeRegistry, validate_graph_document,
)
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding, TypeRegistry
from phase1_agent.session_preloads import session_preload_bindings, validate_session_preloads
from phase1_agent.variable_contract import empty_variable_value, variable_data_type


STRUCTURE_TYPE = "test.preloaded-structure"


def structure_type(**changes):
    return replace(DataTypeDefinition(
        STRUCTURE_TYPE, 1,
        {"type": "object", "properties": {"count": {"type": "integer"}},
         "required": ["count"], "additionalProperties": False},
        checkpoint_admission=True), **changes)


def registry():
    result = NodeRegistry()
    result.data_types.register(structure_type())
    result.data_types.register(variable_data_type())
    result.register(NodeDefinition(
        "test.preload-output", "1", "Output", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT"),), is_output=True),
        lambda config, inputs, context: {
            "output": {"schema_version": 1, "kind": "workflow.text", "text": "offline"}})
    return result


def variable(*, assigned=True):
    return {"registered": True, "name": "counter", "value_type": "integer",
            "assigned": assigned, "value": 7 if assigned else None}


def document(*, variable_value=False, preloads=True):
    node_id = str(uuid4())
    binding = ObjectBinding(
        "data/counter", "workflow.variable" if variable_value else STRUCTURE_TYPE, 1, "shared",
        readers=(node_id,), writers=(node_id,)).to_dict()
    result = {
        "schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Preload", "nodes": [{
            "node_binding_id": node_id, "component_id": "test.preload-output",
            "component_version": "1", "title": "Output", "position": {"x": 0, "y": 0},
            "config": {}, "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
        }], "edges": [], "object_bindings": [binding] if preloads else [],
        "event_bindings": [{
            "event_id": "test.preload", "schema_version": 1, "display_name": "Preload",
            "audience": "management", "target_node_ids": [node_id],
            "payload_schema": {"type": "object"},
        }],
    }
    if preloads:
        result["session_preloads"] = [{
            "object_key": binding["object_key"],
            "value": variable() if variable_value else {"count": 7},
        }]
    return result


def update(service, value, sequence=0, *, confirm=None):
    return service.update_definition(
        value, workflow_definition_id=value["workflow_definition_id"],
        expected_edit_sequence=sequence, confirm_derivation=confirm,
        idempotency_key=str(uuid4()))


def test_values_are_detached_and_no_default_is_required_for_compile_or_save(tmp_path):
    active = registry()
    source = document()
    before = deepcopy(source)
    values = validate_session_preloads(source, active.data_types)
    values["data/counter"]["count"] = 99
    assert source == before
    assert active.data_types.get(STRUCTURE_TYPE, 1, scope="session").to_dict()["has_default"] is False
    plan = GraphCompiler(active).compile_event(source, "test.preload", 1)
    assert plan.document == before
    with closing(GraphWorkflowService(tmp_path / "preload.sqlite", registry=active)) as service:
        saved = update(service, source)
        assert service.get_definition(saved["workflow_definition_id"]) == saved["document"]
        assert service.get_definition(saved["workflow_definition_id"], 1) == before
    with closing(GraphWorkflowService(tmp_path / "preload.sqlite", registry=active)) as reopened:
        assert reopened.get_definition(saved["workflow_definition_id"]) == saved["document"]


@pytest.mark.parametrize("schema", [1, 2])
def test_preload_field_is_rejected_on_old_graph_schemas(schema):
    value = document()
    value["schema_version"] = schema
    for node in value["nodes"]:
        node.pop("configuration_policy")
    with pytest.raises(ContractValidationError) as error:
        validate_graph_document(value)
    assert error.value.reason_code == "graph_invalid_document"


def test_absent_preloads_preserve_old_document_shape_through_save_and_copy(tmp_path):
    value = document(preloads=False)
    before = deepcopy(value)
    assert validate_session_preloads(value, registry().data_types) == {}
    assert validate_graph_document(value) == before
    with closing(GraphWorkflowService(tmp_path / "legacy.sqlite", registry=registry())) as service:
        saved = update(service, value)
        copied = service.copy_definition(
            source_workflow_definition_id=value["workflow_definition_id"],
            expected_source_definition_revision=1, expected_source_edit_sequence=1,
            target_workflow_definition_id=str(uuid4()), target_name="Copy", idempotency_key=str(uuid4()))
        assert "session_preloads" not in saved["document"]
        assert "session_preloads" not in copied["document"]
        assert "session_preloads" not in service.get_definition(value["workflow_definition_id"], 1)


@pytest.mark.parametrize("entries", [
    None, {}, [{"object_key": "data/counter"}],
    [{"object_key": "data/counter", "value": {}, "type_id": STRUCTURE_TYPE}],
    [{"object_key": [], "value": {}}],
    [{"object_key": "data/counter", "value": {}}] * 1025,
])
def test_preload_array_and_row_shape_are_strict(entries):
    value = document()
    value["session_preloads"] = entries
    with pytest.raises(ContractValidationError) as error:
        validate_graph_document(value)
    assert error.value.reason_code == "session_preload_invalid"


@pytest.mark.parametrize("change,code", [
    ("duplicate", "session_preload_duplicate"),
    ("missing", "session_preload_binding_missing"),
    ("default", "session_preload_source_conflict"),
    ("registrar", "session_preload_source_conflict"),
])
def test_preload_has_one_existing_binding_and_one_initialization_source(change, code):
    value = document()
    if change == "duplicate":
        value["session_preloads"].append(deepcopy(value["session_preloads"][0]))
    elif change == "missing":
        value["session_preloads"][0]["object_key"] = "data/missing"
    elif change == "default":
        value["object_bindings"][0]["default_value"] = None
    else:
        value["object_bindings"][0]["registrar_node_id"] = value["nodes"][0]["node_binding_id"]
    with pytest.raises(ContractValidationError) as error:
        validate_graph_document(value)
    assert error.value.reason_code == code


@pytest.mark.parametrize("change,code", [
    ("version", "session_preload_type_unavailable"),
    ("unknown", "session_preload_type_unavailable"),
    ("value", "host_schema_mismatch"),
])
def test_save_rejects_unknown_exact_type_or_bad_initial_value(tmp_path, change, code):
    source = document()
    if change == "version":
        source["object_bindings"][0]["schema_version"] = 2
    elif change == "unknown":
        source["object_bindings"][0]["type_id"] = "test.absent"
    else:
        source["session_preloads"][0]["value"] = {"count": "wrong"}
    with closing(GraphWorkflowService(tmp_path / "invalid.sqlite", registry=registry())) as service:
        with pytest.raises(ContractValidationError) as error:
            update(service, source)
        assert error.value.reason_code == code
        assert service.list_definitions() == []


@pytest.mark.parametrize("scope,admission,code", [
    ("session", False, "session_preload_type_not_admitted"),
    ("global", False, "session_preload_type_unavailable"),
    ("content", False, "session_preload_type_unavailable"),
])
def test_preloads_require_admitted_exact_session_type(scope, admission, code):
    types = TypeRegistry()
    types.register(structure_type(scope=scope, checkpoint_admission=admission))
    with pytest.raises(ContractValidationError) as error:
        validate_session_preloads(document(), types)
    assert error.value.reason_code == code


@pytest.mark.parametrize("type_id,version", [
    ("workflow.variable", 2),
    ("workflow.chat-context", 1),
    ("workflow.chat-context", 2),
    ("workflow.effective-context", 4),
    ("workflow.effective-context", 5),
])
def test_special_type_identities_cannot_bypass_preload_admission_with_plain_schema(type_id, version):
    types = TypeRegistry()
    types.register(DataTypeDefinition(type_id, version, {"type": "object"}, checkpoint_admission=True))
    value = document()
    value["object_bindings"][0].update(type_id=type_id, schema_version=version)
    value["session_preloads"][0]["value"] = {}
    with pytest.raises(ContractValidationError) as error:
        validate_session_preloads(value, types)
    assert error.value.reason_code == "session_preload_type_unsupported"


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
@pytest.mark.parametrize("target", [
    "https://blocked.invalid/schema", "file:///blocked/schema.json", "relative-schema.json",
])
@pytest.mark.parametrize("position", ["root", "property", "composition"])
def test_preload_schema_nonlocal_references_are_rejected_without_retrieval(keyword, target, position):
    reference = {keyword: target}
    schema = reference
    if position == "property":
        schema = {"type": "object", "properties": {"count": reference}}
    elif position == "composition":
        schema = {"allOf": [{"$defs": {"nested": reference}}]}
    types = TypeRegistry()
    types.register(structure_type(schema=schema))
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        with pytest.raises(ContractValidationError) as error:
            validate_session_preloads(document(), types)
        assert error.value.reason_code == "session_preload_type_unsupported"
        assert error.value.diagnostics[0]["keyword"] == keyword
        blocked.assert_not_called()


def test_preload_schema_local_reference_validates_real_value_without_retrieval():
    types = TypeRegistry()
    types.register(structure_type(schema={
        "$defs": {"count": {"type": "integer"}},
        "type": "object", "properties": {"count": {"$ref": "#/$defs/count"}},
        "required": ["count"], "additionalProperties": False,
        "default": {"$ref": "https://business-value.invalid/not-a-schema"},
    }))
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        assert validate_session_preloads(document(), types) == {"data/counter": {"count": 7}}
        invalid = document()
        invalid["session_preloads"][0]["value"]["count"] = "wrong"
        with pytest.raises(ContractValidationError) as error:
            validate_session_preloads(invalid, types)
        assert error.value.reason_code == "host_schema_mismatch"
        blocked.assert_not_called()


@pytest.mark.parametrize("reference", ["#/$defs/missing", "#missing-anchor"])
def test_unresolved_local_schema_reference_has_a_visible_preload_diagnostic(reference):
    types = TypeRegistry()
    types.register(structure_type(schema={"$ref": reference}))
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        with pytest.raises(ContractValidationError) as error:
            validate_session_preloads(document(), types)
        assert error.value.reason_code == "session_preload_schema_reference_invalid"
        assert error.value.__cause__ is not None
        blocked.assert_not_called()


@pytest.mark.parametrize("annotation", ["default", "const"])
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_local_reference_cannot_execute_nonlocal_schema_hidden_in_data(annotation, keyword):
    types = TypeRegistry()
    types.register(structure_type(schema={
        "$ref": "#/" + annotation,
        annotation: {keyword: "https://blocked.invalid/schema"},
    }))
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        with pytest.raises(ContractValidationError) as error:
            validate_session_preloads(document(), types)
        assert error.value.reason_code == "session_preload_type_unsupported"
        blocked.assert_not_called()


@pytest.mark.parametrize("annotation", ["default", "const"])
def test_unreferenced_annotation_data_is_not_interpreted_as_schema(annotation):
    data = {"$ref": "https://business-value.invalid/not-a-schema"}
    types = TypeRegistry()
    types.register(structure_type(schema={"type": "object", annotation: data}))
    value = document()
    if annotation == "const":
        value["session_preloads"][0]["value"] = data
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        assert validate_session_preloads(value, types) == {
            "data/counter": value["session_preloads"][0]["value"]}
        blocked.assert_not_called()


def test_local_reference_chain_can_reach_a_valid_schema_inside_data():
    types = TypeRegistry()
    types.register(structure_type(schema={
        "$ref": "#/default", "default": {"$ref": "#/examples/0"},
        "examples": [{"type": "object", "required": ["count"],
                      "properties": {"count": {"type": "integer"}}}],
    }))
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        assert validate_session_preloads(document(), types) == {"data/counter": {"count": 7}}
        blocked.assert_not_called()


@pytest.mark.parametrize("breakout", [False, True])
def test_nested_resource_id_and_local_anchor_use_their_exact_reference_scope(breakout):
    types = TypeRegistry()
    child = {
        "$id": "child", "$ref": "#value", "$defs": {
            "value": {"$anchor": "value", "type": "object", "required": ["count"],
                      "properties": {"count": {"type": "integer"}}}},
        "default": {"$ref": "https://blocked.invalid/schema"},
    }
    if breakout:
        child["$defs"]["value"]["$ref"] = "#/default"
    types.register(structure_type(schema={
        "$id": "https://schemas.invalid/root", "$ref": "#/$defs/child",
        "$defs": {"child": child},
    }))
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        if breakout:
            with pytest.raises(ContractValidationError) as error:
                validate_session_preloads(document(), types)
            assert error.value.reason_code == "session_preload_type_unsupported"
        else:
            assert validate_session_preloads(document(), types) == {"data/counter": {"count": 7}}
        blocked.assert_not_called()


def test_finite_recursive_structure_schema_is_supported_without_scan_loops():
    types = TypeRegistry()
    types.register(structure_type(schema={
        "type": "object", "properties": {
            "count": {"type": "integer"},
            "children": {"type": "array", "items": {"$ref": "#"}},
        }, "required": ["count"], "additionalProperties": False,
    }))
    value = document()
    value["session_preloads"][0]["value"] = {
        "count": 1, "children": [{"count": 2, "children": [{"count": 3}]}]}
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        assert validate_session_preloads(value, types) == {
            "data/counter": value["session_preloads"][0]["value"]}
        blocked.assert_not_called()


@pytest.mark.parametrize("schema", [
    {"$ref": "#"},
    {"$ref": "#/default", "default": {"$ref": "#/const"}, "const": {"$ref": "#/default"}},
])
def test_unproductive_local_reference_cycle_has_a_visible_diagnostic(schema):
    types = TypeRegistry()
    types.register(structure_type(schema=schema))
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        with pytest.raises(ContractValidationError) as error:
            validate_session_preloads(document(), types)
        assert error.value.reason_code == "session_preload_schema_reference_invalid"
        assert isinstance(error.value.__cause__, RecursionError)
        blocked.assert_not_called()


@pytest.mark.parametrize("target", [
    {"$schema": None}, {"$schema": 42}, {"properties": 42}, 123,
])
def test_local_reference_to_invalid_schema_data_has_a_visible_diagnostic(target):
    types = TypeRegistry()
    types.register(structure_type(schema={"$ref": "#/default", "default": target}))
    with patch("urllib.request.urlopen", side_effect=AssertionError("No retrieval allowed")) as blocked:
        with pytest.raises(ContractValidationError) as error:
            validate_session_preloads(document(), types)
        assert error.value.reason_code == "session_preload_schema_reference_invalid"
        blocked.assert_not_called()


@pytest.mark.parametrize("callback", [
    "validator", "references", "reference_mapper", "migrator", "content_transformer",
    "artifact_bindings", "write_validator", "public_references",
])
def test_generic_preloads_reject_special_callbacks_before_executing_them(callback):
    def forbidden(*args):
        raise AssertionError("Unsupported preload callback must not run")

    types = TypeRegistry()
    types.register(structure_type(**{callback: forbidden}))
    with pytest.raises(ContractValidationError) as error:
        validate_session_preloads(document(), types)
    assert error.value.reason_code == "session_preload_type_unsupported"
    assert error.value.diagnostics[0]["callbacks"] == [callback]


@pytest.mark.parametrize("assigned", [True, False])
def test_preloaded_variable_uses_existing_exact_validator_and_can_be_unassigned(assigned):
    value = document(variable_value=True)
    value["session_preloads"][0]["value"] = variable(assigned=assigned)
    assert validate_session_preloads(value, registry().data_types) == {
        "data/counter": variable(assigned=assigned)}


def test_variable_shell_is_not_a_registered_preload():
    value = document(variable_value=True)
    value["session_preloads"][0]["value"] = empty_variable_value()
    with pytest.raises(ContractValidationError) as error:
        validate_graph_document(value)
    assert error.value.reason_code == "session_preload_variable_unregistered"
    value["session_preloads"][0]["value"] = variable()
    value["session_preloads"][0]["value"]["value"] = True
    with pytest.raises(ContractValidationError) as error:
        validate_session_preloads(value, registry().data_types)
    assert error.value.reason_code == "variable_type_mismatch"


def test_historical_binding_selection_does_not_read_configuration_initial_values():
    value = document(variable_value=True)
    value["session_preloads"][0]["value"] = {"invalid": "configuration initial value"}
    selected = session_preload_bindings(value, registry().data_types)
    assert selected["data/counter"].type_id == "workflow.variable"
    assert selected["data/counter"].readers == (value["nodes"][0]["node_binding_id"],)
    with pytest.raises(ContractValidationError):
        validate_session_preloads(value, registry().data_types)


def test_copy_preserves_declaration_acl_and_has_no_sessions(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "copy.sqlite", registry=registry())) as service:
        source = document()
        update(service, source)
        copied = service.copy_definition(
            source_workflow_definition_id=source["workflow_definition_id"],
            expected_source_definition_revision=1, expected_source_edit_sequence=1,
            target_workflow_definition_id=str(uuid4()), target_name="Copy", idempotency_key=str(uuid4()))
        assert copied["document"]["session_preloads"] == source["session_preloads"]
        assert copied["document"]["object_bindings"] == source["object_bindings"]
        assert service.list_sessions(copied["workflow_definition_id"]) == []
        assert copied["document"] == service.get_definition(copied["workflow_definition_id"])


def test_preload_change_advances_fixed_revision_without_session(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "fixed.sqlite", registry=registry())) as service:
        saved = update(service, document())
        changed = deepcopy(saved["document"])
        changed["session_preloads"][0]["value"]["count"] = 8
        result = update(service, changed, 1)
        assert (result["definition_revision"], result["edit_sequence"]) == (2, 2)
        assert result["changed_categories"] == ["fixed"]
        assert service.get_definition(result["workflow_definition_id"], 1)["session_preloads"][0]["value"] == {
            "count": 7}


def test_adding_preload_with_effective_session_requires_then_preserves_derivation(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "derive.sqlite", registry=registry())) as service:
        original = document(preloads=False)
        saved = update(service, original)
        session = service.create_session(saved["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
        proposed = deepcopy(saved["document"])
        new = document()
        new["object_bindings"][0]["readers"] = [original["nodes"][0]["node_binding_id"]]
        new["object_bindings"][0]["writers"] = [original["nodes"][0]["node_binding_id"]]
        proposed["object_bindings"] = new["object_bindings"]
        proposed["session_preloads"] = new["session_preloads"]
        pending = update(service, proposed, 1)
        assert pending["outcome"] == "confirmation_required"
        assert pending["changed_categories"] == ["fixed"]
        assert "session_preloads" not in service.get_definition(saved["workflow_definition_id"])
        derived = update(service, proposed, 1, confirm=True)
        assert derived["outcome"] == "derived"
        assert derived["workflow_definition_id"] != saved["workflow_definition_id"]
        assert derived["document"]["session_preloads"] == proposed["session_preloads"]
        assert service.get_session(session["workflow_session_id"])["workflow_definition_id"] == saved[
            "workflow_definition_id"]
        assert "session_preloads" not in service.get_definition(saved["workflow_definition_id"])
