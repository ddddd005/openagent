"""General-tools contracts exercised through the public loader and SDK."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import (
    create_content_package, default_presentation, json_content, make_prompt_item,
    prompt_content, text_content,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_execution import NodeExecutionContext
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.tool_package import create_tool_package


NODE_ID = str(UUID(int=1, version=4))
KEY = "variable/value"


@pytest.fixture
def registry():
    return CapabilityPackageLoader((create_tool_package(), create_content_package())).load(
        {"workflow.tools": "1.0.0"}).registry


def object_state(registry, *, value=None, type_id="workflow.variable", schema_version=1):
    value = registry.data_types.default(type_id, schema_version) if value is None else deepcopy(value)
    binding = ObjectBinding(KEY, type_id, schema_version, "shared",
                            readers=(NODE_ID,), writers=(NODE_ID,)).to_dict()
    return {KEY: {"type_id": type_id, "schema_version": schema_version, "value": value,
                  "binding": binding, "revision": 1, "revision_id": str(uuid4()), "deleted": False}}


def assigned(name="value", value_type="string", value="Alice"):
    return {"registered": True, "name": name, "value_type": value_type,
            "assigned": True, "value": value}


def invoke(registry, name, *, config=None, inputs=None, objects=None, external=None):
    entry = registry.get("tools." + name, "1")
    config = {**deepcopy(entry.definition.default_config), **(config or {})}
    registry.validate_config(entry, config)
    inputs = deepcopy(inputs or {})
    ports, output_ports = entry.definition.ports(config)
    for port in ports:
        if port.port_id in inputs:
            inputs[port.port_id] = registry.validate_content(
                inputs[port.port_id], port.data_type, port.data_schema_version)
    context = NodeExecutionContext(
        definition=entry.definition, node_binding_id=NODE_ID, workflow_session_id=str(uuid4()),
        chain_run_id=str(uuid4()), node_run_id=str(uuid4()), state={}, private_states={},
        external_inputs=external or {}, object_states=objects or {}, type_registry=registry.data_types,
    )
    result = entry.executor(config, inputs, context)
    port_map = {port.port_id: port for port in output_ports}
    assert set(result) <= set(port_map)
    assert {port.port_id for port in output_ports if port.required} <= set(result)
    result = {name: registry.validate_content(value, port_map[name].data_type, 2)
              for name, value in result.items()}
    return result, context


def settle(objects, context):
    for intent in context.object_writes:
        item = objects[intent["object_key"]]
        assert intent["expected_revision"] == item["revision"]
        item.update(value=deepcopy(intent["value"]), revision=item["revision"] + 1,
                    revision_id=str(uuid4()))


def test_independent_package_exports_exact_v2_ports_and_no_agent(registry):
    assert list(registry.package_lock) == [
        {"package_id": "workflow.content", "version": "1.0.0"},
        {"package_id": "workflow.tools", "version": "1.0.0"},
    ]
    assert len(registry.catalog()) == 15
    for item in registry.catalog():
        assert item["component_id"].startswith("tools.")
        port_sets = [(item["inputs"], item["outputs"])] + [
            (mode["inputs"], mode["outputs"]) for mode in item.get("port_modes", {}).values()]
        assert all(port["data_schema_version"] == 2
                   for inputs, outputs in port_sets for port in inputs + outputs)
    for name in ("variable-register", "variable-assign"):
        definition = registry.get("tools." + name, "1").definition
        assert definition.outputs == () and not definition.is_output


def test_text_current_input_and_output_do_not_interpret_macros(registry):
    literal = "{{missing}} <PRESET>"
    result, _ = invoke(registry, "text", config={"text": literal})
    assert result["output"] == text_content(literal)
    for external in (literal, text_content(literal)):
        current, _ = invoke(registry, "current-input", external={"text": external})
        output, _ = invoke(registry, "output", inputs={"input": current["output"]})
        assert output == result
    entry = registry.get("tools.current-input", "1")
    assert entry.external_inputs_declaration(entry.definition.default_config) == [
        {"name": "text", "data_type": "TEXT", "required": True}]
    with pytest.raises(ContractValidationError):
        invoke(registry, "current-input", external={"text": {"schema_version": 1,
               "kind": "workflow.text", "text": "old"}})
    with pytest.raises(ContractValidationError):
        entry.external_inputs_validator(entry.definition.default_config, {})


def test_text_to_prompt_identity_and_enabled_text_projection(registry):
    result, _ = invoke(registry, "text-to-prompt", inputs={"input": text_content("hello")})
    repeated, _ = invoke(registry, "text-to-prompt", inputs={"input": text_content("changed")})
    assert result["output"]["items"][0]["item_instance_id"] == repeated["output"]["items"][0]["item_instance_id"]
    assert result["output"]["items"][0]["source"]["kind"] == "conversion"
    projected, _ = invoke(registry, "prompt-to-text", inputs={"input": result["output"]})
    assert projected["output"] == text_content("hello")
    disabled = {**default_presentation(), "enabled": False}
    result, _ = invoke(registry, "text-to-prompt", config={"presentation": disabled},
                       inputs={"input": text_content("off")})
    assert result["output"]["items"] == []
    bad_depth = {**default_presentation(), "depth": 0}
    with pytest.raises(ContractValidationError):
        invoke(registry, "text-to-prompt", config={"presentation": bad_depth},
               inputs={"input": text_content("")})


@pytest.mark.parametrize("value", [None, True, 7, 1.25, "中文", [1, "x"], {"b": 2, "a": {"z": False}}])
def test_strict_json_canonical_roundtrip(registry, value):
    encoded, _ = invoke(registry, "json-to-text", inputs={"input": json_content(value)})
    parsed, _ = invoke(registry, "text-to-json", inputs={"input": encoded["output"]})
    assert parsed["output"] == json_content(value)
    if type(value) is dict:
        assert encoded["output"]["text"] == '{"a":{"z":false},"b":2}'


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', "NaN", "Infinity", '{"x":', '"\\ud800"'])
def test_json_parser_rejects_ambiguous_or_non_json_values(registry, text):
    with pytest.raises(ContractValidationError):
        invoke(registry, "text-to-json", inputs={"input": text_content(text)})


def test_prompt_json_uses_complete_v2_envelope_and_preserves_protected_facts(registry):
    item = make_prompt_item(NODE_ID, "fact", "tool fact", default_presentation(),
                            source={"kind": "test", "reference": "{{value}}"})
    item.update(protected=True, role="tool", metadata={"protocol": {"text": "fact"}})
    original = prompt_content([item])
    converted, _ = invoke(registry, "prompt-to-json", inputs={"input": original})
    restored, _ = invoke(registry, "json-to-prompt", inputs={"input": converted["output"]})
    assert restored["output"] == original
    with pytest.raises(ContractValidationError):
        invoke(registry, "prompt-to-text", inputs={"input": original})
    with pytest.raises(ContractValidationError):
        invoke(registry, "json-to-prompt", inputs={"input": json_content({"items": [item]})})
    legacy = {**original, "schema_version": 1}
    with pytest.raises(ContractValidationError):
        invoke(registry, "json-to-prompt", inputs={"input": json_content(legacy)})


def test_registration_default_shell_initializes_once_without_output(registry):
    objects = object_state(registry)
    assert objects[KEY]["value"]["registered"] is False
    result, first = invoke(registry, "variable-register", config={
        "has_initial": True, "initial_value": "Alice",
    }, objects=objects)
    assert result == {} and first.object_writes[0]["expected_revision"] == 1
    settle(objects, first)
    result, second = invoke(registry, "variable-register", config={
        "has_initial": True, "initial_value": "must not reset",
    }, objects=objects)
    assert result == {} and second.object_writes == []
    assert objects[KEY]["value"] == assigned()
    for config in ({"name": "different"}, {"value_type": "number"}):
        with pytest.raises(ContractValidationError) as conflict:
            invoke(registry, "variable-register", config=config, objects=objects)
        assert conflict.value.reason_code == "variable_registration_conflict"
    with pytest.raises(ContractValidationError):
        invoke(registry, "variable-register",
               config={"name": "bad-name"}, objects=objects)


def test_registration_does_not_later_backfill_initial_value(registry):
    objects = object_state(registry)
    _, first = invoke(registry, "variable-register", objects=objects)
    settle(objects, first)
    _, repeated = invoke(registry, "variable-register",
                         config={"has_initial": True, "initial_value": "late"}, objects=objects)
    assert repeated.object_writes == []
    assert objects[KEY]["value"] == {**assigned(), "assigned": False, "value": None}


@pytest.mark.parametrize(("value_type", "value", "expected"), [
    ("string", "中文", "中文"), ("integer", 12, "12"),
    ("number", 1.5, "1.5"), ("boolean", False, "false"),
])
def test_assignment_and_explicit_scalar_sources(registry, value_type, value, expected):
    objects = object_state(registry, value=assigned(value_type=value_type, value=value))
    result, update = invoke(registry, "variable-assign",
                            inputs={"value": json_content(value)}, objects=objects)
    assert result == {}
    settle(objects, update)
    source, read = invoke(registry, "variable-to-content", objects=objects)
    assert source["output"] == text_content(expected)
    assert read.reads[0]["revision"] == 2
    literal, _ = invoke(registry, "variable-to-content", config={"mode": "json"}, objects=objects)
    assert literal["output"] == json_content(value)


def test_numeric_add_subtract_and_bool_are_strictly_typed(registry):
    objects = object_state(registry, value=assigned(value_type="integer", value=7))
    _, update = invoke(registry, "variable-assign",
                       config={"operation": "add", "value": 3}, objects=objects)
    settle(objects, update)
    _, update = invoke(registry, "variable-assign",
                       config={"operation": "subtract", "value": 2}, objects=objects)
    settle(objects, update)
    assert objects[KEY]["value"]["value"] == 8
    for bad in (True, 1.5, "2"):
        with pytest.raises(ContractValidationError) as mismatch:
            invoke(registry, "variable-assign", config={"value": bad}, objects=objects)
        assert mismatch.value.reason_code == "variable_type_mismatch"
    with pytest.raises(ContractValidationError):
        invoke(registry, "variable-assign", config={"value": 2**53}, objects=objects)


@pytest.mark.parametrize("name", ["variable-assign", "variable-to-content"])
def test_unregistered_and_unassigned_use_is_rejected(registry, name):
    with pytest.raises(ContractValidationError) as unregistered:
        invoke(registry, name, objects=object_state(registry))
    assert unregistered.value.reason_code == "variable_not_registered"
    if name == "variable-to-content":
        unassigned = {**assigned(), "assigned": False, "value": None}
        with pytest.raises(ContractValidationError) as missing:
            invoke(registry, name, objects=object_state(registry, value=unassigned))
        assert missing.value.reason_code == "variable_unassigned"


def test_variable_object_invariants_and_binding_type_are_enforced(registry):
    for value in (
        {**assigned(), "registered": False},
        {**assigned(), "assigned": False},
        assigned(value_type="integer", value=True),
    ):
        with pytest.raises(ContractValidationError):
            registry.data_types.validate("workflow.variable", 1, value)
    from phase1_agent.host_sdk import DataTypeDefinition
    wrong = registry.detached()
    wrong.data_types.register(DataTypeDefinition("example.other", 1, {}, default_value=assigned()))
    with pytest.raises(ContractValidationError) as mismatch:
        invoke(wrong, "variable-to-content", objects=object_state(wrong, type_id="example.other"))
    assert mismatch.value.reason_code == "variable_binding_type_mismatch"


def test_explicit_all_and_selected_replacement_preserve_literal_values(registry):
    objects = object_state(registry, value=assigned(value="{{other}}"))
    original = text_content("{{value}} / {{other}}")
    selected, _ = invoke(registry, "variable-replace-selected", config={
        "object_keys": [KEY], "names": ["value"],
    }, inputs={"input": original}, objects=objects)
    assert selected["output"] == text_content("{{other}} / {{other}}")
    assert original["text"] == "{{value}} / {{other}}"
    with pytest.raises(ContractValidationError) as missing:
        invoke(registry, "variable-replace-all", config={"object_keys": [KEY]},
               inputs={"input": original}, objects=objects)
    assert missing.value.reason_code == "variable_not_registered"
    unassigned = {**assigned(), "assigned": False, "value": None}
    with pytest.raises(ContractValidationError) as missing:
        invoke(registry, "variable-replace-all", config={"object_keys": [KEY]},
               inputs={"input": text_content("{{value}}")},
               objects=object_state(registry, value=unassigned))
    assert missing.value.reason_code == "variable_unassigned"


def test_prompt_replacement_scope_preserves_source_id_and_whole_protected_item(registry):
    objects = object_state(registry, value=assigned(value="renamed"))
    editable = make_prompt_item(NODE_ID, "edit", "{{value}}", default_presentation(),
                                source={"kind": "test", "id": "{{value}}"},
                                metadata={"{{value}}": ["{{value}}", 5, False]})
    protected = make_prompt_item(NODE_ID, "fact", "{{value}}", default_presentation(),
                                 metadata={"{{value}}": "{{value}}"})
    protected["protected"] = True
    original = prompt_content([editable, protected])
    body, _ = invoke(registry, "variable-replace-all", config={
        "mode": "prompt", "object_keys": [KEY],
    }, inputs={"input": original}, objects=objects)
    assert body["output"]["items"][0]["text"] == "renamed"
    assert body["output"]["items"][0]["metadata"] == editable["metadata"]
    assert body["output"]["items"][1] == protected
    all_fields, _ = invoke(registry, "variable-replace-all", config={
        "mode": "prompt", "scope": "all", "object_keys": [KEY],
    }, inputs={"input": original}, objects=objects)
    changed = all_fields["output"]["items"][0]
    assert changed["metadata"] == {"renamed": ["renamed", 5, False]}
    assert changed["source"] == editable["source"]
    assert changed["item_instance_id"] == editable["item_instance_id"]
    assert all_fields["output"]["items"][1] == protected
    assert original == prompt_content([editable, protected])


def test_json_business_keys_only_change_in_all_scope_and_collisions_fail(registry):
    objects = object_state(registry, value=assigned(value="new"))
    original = json_content({"{{value}}": ["{{value}}", {"x": 8, "bool": True, "null": None}]})
    body, _ = invoke(registry, "variable-replace-all", config={
        "mode": "json", "object_keys": [KEY],
    }, inputs={"input": original}, objects=objects)
    assert body["output"]["value"] == {"{{value}}": ["new", {"x": 8, "bool": True, "null": None}]}
    all_fields, _ = invoke(registry, "variable-replace-all", config={
        "mode": "json", "scope": "all", "object_keys": [KEY],
    }, inputs={"input": original}, objects=objects)
    assert "new" in all_fields["output"]["value"]
    with pytest.raises(ContractValidationError) as conflict:
        invoke(registry, "variable-replace-all", config={
            "mode": "json", "scope": "all", "object_keys": [KEY],
        }, inputs={"input": json_content({"{{value}}": "a", "new": "b"})}, objects=objects)
    assert conflict.value.reason_code == "content_field_collision"


def test_variable_values_and_replacement_have_no_legacy_one_megabyte_limit(registry):
    large = "x" * 1_100_000
    objects = object_state(registry, value=assigned(value=large))
    result, _ = invoke(registry, "variable-replace-all", config={"object_keys": [KEY]},
                       inputs={"input": text_content("{{value}}")}, objects=objects)
    assert result["output"]["text"] == large
    _, update = invoke(registry, "variable-assign", config={"value": large}, objects=objects)
    assert update.object_writes[0]["value"]["value"] == large
    with pytest.raises(ContractValidationError):
        registry.data_types.validate("workflow.variable", 1, assigned(value="x" * 4_000_000))


def test_regex_uses_declared_structural_fields_and_validates_result(registry):
    original = json_content({"apple": ["apple", 4, True]})
    changed, _ = invoke(registry, "regex", config={
        "mode": "json", "scope": "all", "pattern": "apple", "replacement": "pear",
    }, inputs={"input": original})
    assert changed["output"]["value"] == {"pear": ["pear", 4, True]}
    item = make_prompt_item(NODE_ID, "edit", "apple", default_presentation())
    item["protected"] = True
    unchanged, _ = invoke(registry, "regex", config={"mode": "prompt", "pattern": "apple",
                          "replacement": "pear"}, inputs={"input": prompt_content([item])})
    assert unchanged["output"]["items"] == [item]
    with pytest.raises(ContractValidationError):
        invoke(registry, "regex", config={"mode": "prompt", "scope": "all",
               "pattern": "system", "replacement": "not-a-role"},
               inputs={"input": prompt_content([make_prompt_item(
                   NODE_ID, "edit", "body", default_presentation())])})


def test_regex_all_handles_more_than_1024_declared_business_strings(registry):
    items = [make_prompt_item(
        NODE_ID, str(index), "apple", default_presentation(),
        source={"kind": "test", "note": "apple"}, metadata={"tag": "apple"},
    ) for index in range(300)]
    result, _ = invoke(registry, "regex", config={
        "mode": "prompt", "scope": "all", "pattern": "apple", "replacement": "pear",
    }, inputs={"input": prompt_content(items)})
    assert len(result["output"]["items"]) == 300
    for original, changed in zip(items, result["output"]["items"]):
        assert changed["text"] == "pear" and changed["metadata"] == {"tag": "pear"}
        assert changed["source"] == original["source"]
        assert changed["item_instance_id"] == original["item_instance_id"]
