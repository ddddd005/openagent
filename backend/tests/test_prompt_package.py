"""Independent prompt contracts without an Agent package or network calls."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import (
    create_content_package, default_presentation, make_prompt_item, prompt_content, text_content,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.global_resources import global_resource_reference
from phase1_agent.graph_contracts import GraphCompiler, NodeDefinition, NodePort
from phase1_agent.graph_execution import execute_graph
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.prompt_package import (
    PROMPT_FRONTEND_EXTENSIONS, PROMPT_RESOURCE_TYPE, assemble_prompt, create_prompt_package, merge_prompt_materials,
    validate_prompt_resource, validate_ready_prompt,
)


def uid(number):
    return str(UUID(int=number, version=4))


def presentation(**changes):
    return {**default_presentation(), **changes}


def item(number, text, **changes):
    value = make_prompt_item(uid(900), uid(number), text, presentation(),
                             source={"kind": "fixture", "member_id": uid(number)})
    value["item_instance_id"] = uid(number)
    value.update(changes)
    return value


def member(number, text, **changes):
    return {"id": uid(number), "text": text, "presentation": presentation(**changes), "metadata": {}}


def loaded(*, tools=None):
    return CapabilityPackageLoader((create_content_package(), create_prompt_package(tool_catalog=tools))).load(
        {"workflow.prompts": "1.0.0"})


def graph_node(registry, component, number, **config):
    entry = registry.get(component, "1")
    return {"node_binding_id": uid(number), "component_id": component, "component_version": "1",
            "title": component, "position": {"x": 0, "y": 0},
            "config": {**deepcopy(entry.definition.default_config), **config}}


def graph_edge(number, source, target, *, source_port="output", target_port="input", order=0):
    return {"edge_id": uid(number), "source_node_id": source["node_binding_id"],
            "target_node_id": target["node_binding_id"], "source_port_id": source_port,
            "target_port_id": target_port, "order": order}


def graph_document(nodes, edges):
    return {"schema_version": 1, "workflow_definition_id": uid(990), "revision": 1,
            "name": "Independent prompts", "nodes": nodes, "edges": edges}


def register_sink(registry):
    # A test consumer proves prompt execution does not need the Agent package.
    registry.register(NodeDefinition(
        "test.prompt-sink", "1", "Prompt sink", "Test", {}, {"type": "object", "additionalProperties": False},
        inputs=(NodePort("input", "PROMPT", data_schema_version=2),),
        outputs=(NodePort("output", "PROMPT", data_schema_version=2),), is_output=True,
    ), lambda config, inputs, context: {"output": inputs["input"]})


def run_graph(registry, doc, **kwargs):
    plan = GraphCompiler(registry).compile(doc)
    return execute_graph(
        plan, registry, workflow_session_id=uid(991), chain_run_id=uid(992),
        node_run_ids={node_id: uid(2000 + index) for index, node_id in enumerate(plan.ordered_node_ids)},
        state={"revision": 0, "values": {}}, **kwargs,
    )


def test_prompt_package_loads_with_only_content_and_no_agent_or_tools_dependency():
    capabilities = loaded()
    assert capabilities.package_lock == (
        {"package_id": "workflow.content", "version": "1.0.0"},
        {"package_id": "workflow.prompts", "version": "1.0.0"},
    )
    registry = capabilities.registry
    assert {entry["component_id"] for entry in registry.catalog()} == {
        "prompts.item", "prompts.group", "prompts.source", "prompts.summary", "prompts.assembly",
        "prompts.global-reference", "prompts.global-resolve", "prompts.tool", "prompts.tool-summary",
    }
    assert registry.get("workflow.agent", "1") is None
    assert registry.data_types.get(PROMPT_RESOURCE_TYPE, 1, scope="global") is not None
    assert registry.get("prompts.global-resolve", "1").resource_input_ports == ("input",)


def test_prompt_package_declares_exact_current_panel_and_reference_editor_without_compat():
    package = create_prompt_package()
    capabilities = CapabilityPackageLoader((create_content_package(), package)).load(
        {"workflow.prompts": "1.0.0"})
    expected = sorted([
        {**deepcopy(row), "schema_version": 2, "host_protocol_version": 1,
         "package_id": "workflow.prompts", "package_version": "1.0.0"}
        for row in PROMPT_FRONTEND_EXTENSIONS
    ], key=lambda row: row["extension_id"])
    assert list(capabilities.frontend_extensions) == expected
    assert {(row["extension_id"], row["entrypoint"]) for row in expected} == {
        ("workflow.prompts.workbench-panel", "workflow.prompts.workbench.panel"),
        ("workflow.prompts.node-fields", "workflow.prompts.workbench.node-fields"),
        ("workflow.prompts.node-fields-v2", "workflow.prompts.workbench.node-fields-v2"),
    }
    manifest = package.manifest.to_dict()
    assert manifest["schema_version"] == 1
    assert manifest["exports"]["frontend_extensions"] == [
        {"extension_id": row["extension_id"]} for row in PROMPT_FRONTEND_EXTENSIONS]
    assert next(row for row in expected if row["kind"] == "field-editor")["binding"]["target"] == {
        "component_id": "prompts.global-reference", "component_version": "1"}
    loaded_manifest = next(row for row in capabilities.package_manifests
                           if row["package_id"] == "workflow.prompts")
    assert loaded_manifest["exports"] == manifest["exports"]
    assert capabilities.registry.execution_package_lock == capabilities.package_lock
    assert capabilities.registry.get("workflow.global-content", "2") is None
    assert capabilities.registry.data_types.get("workflow.global-content", 1, scope="global") is None


def test_material_order_is_independent_of_input_arrays_and_uses_integer_uuid_tiebreak():
    values = [
        item(8, "after", placement="after", order=-100),
        item(7, "middle near", placement="middle", depth=0, order=-100),
        item(6, "middle deep", placement="middle", depth=3),
        item(3, "before later", order=2),
        item(2, "before higher ID"),
        item(1, "before lower ID"),
    ]
    first = merge_prompt_materials([prompt_content(values)])
    second = merge_prompt_materials([prompt_content(list(reversed(values)))])
    assert first == second
    assert [entry["text"] for entry in first["items"]] == [
        "before lower ID", "before higher ID", "before later", "middle deep", "middle near", "after",
    ]


def test_business_equal_materials_deduplicate_keep_smallest_id_and_deterministic_origins():
    left, right = item(2, "same"), item(1, "same")
    original = deepcopy([left, right])
    actual = merge_prompt_materials([prompt_content([left]), prompt_content([right, left])])
    assert len(actual["items"]) == 1
    kept = actual["items"][0]
    assert kept["item_instance_id"] == uid(1)
    assert kept["source"] == {"kind": "merged", "origins": [
        {"kind": "fixture", "member_id": uid(1)},
        {"kind": "fixture", "member_id": uid(2)},
    ]}
    assert merge_prompt_materials([prompt_content([right]), prompt_content([left])]) == actual
    assert [left, right] == original
    assert merge_prompt_materials([actual, actual]) == actual


@pytest.mark.parametrize("change", [
    {"role": "user"}, {"order": 1}, {"enabled": False}, {"purpose": "tool-description"},
    {"metadata": {"label": "different"}}, {"placement": "after"},
])
def test_same_text_with_different_business_semantics_does_not_deduplicate(change):
    values = [item(1, "same"), item(2, "same", **change)]
    assert len(merge_prompt_materials([prompt_content(values)])["items"]) == 2


@pytest.mark.parametrize("change", [{"text": "different"}, {"source": {"kind": "different"}}])
def test_repeated_stable_identity_with_content_or_source_conflict_is_rejected(change):
    first = item(1, "same")
    other = {**deepcopy(first), **change}
    with pytest.raises(ContractValidationError) as caught:
        merge_prompt_materials([prompt_content([first]), prompt_content([other])])
    assert caught.value.reason_code == "graph_prompt_identity_conflict"


@pytest.mark.parametrize("change", [
    {"protected": True}, {"purpose": "context"}, {"role": "tool", "protected": True},
])
def test_context_or_protocol_facts_do_not_enter_ordinary_prompt_sorting(change):
    with pytest.raises(ContractValidationError):
        merge_prompt_materials([prompt_content([item(1, "protected", **change)])])


def test_assembly_places_depth_zero_after_current_input_and_before_after_region():
    values = prompt_content([
        item(3, "after", placement="after"),
        item(2, "middle", placement="middle", depth=0),
        item(1, "before"),
        item(4, "disabled", enabled=False),
    ])
    sources = [{"edge_id": uid(60), "output_id": uid(61), "order": 4}]
    current_refs = [{"edge_id": uid(62), "output_id": uid(63), "order": 0}]
    result = assemble_prompt([values], text_content("question"), source_output_refs=sources,
                             current_input_refs=current_refs)
    assert result["assembly"]["messages"] == [
        {"role": "system", "content": "before"}, {"role": "user", "content": "question"},
        {"role": "system", "content": "middle"}, {"role": "system", "content": "after"},
    ]
    assert result["assembly"]["manifest"] == {
        "ordered_item_ids": [uid(1), uid(2), uid(3)],
        "source_output_refs": sources, "current_input_refs": current_refs,
    }
    assert validate_ready_prompt(result) == result
    assert loaded().registry.validate_content(result, "PROMPT", 2) == result


def test_material_only_assembly_and_current_input_only_assembly_are_supported():
    assert assemble_prompt([prompt_content([item(1, "material")])])["assembly"]["current_input"] is None
    assert assemble_prompt([], text_content("question"))["assembly"]["messages"] == [
        {"role": "user", "content": "question"}]
    with pytest.raises(ContractValidationError) as caught:
        assemble_prompt([])
    assert caught.value.reason_code == "graph_prompt_empty"


def test_content_only_consumer_validates_frozen_prompt_without_loading_prompt_nodes():
    registry = CapabilityPackageLoader((create_content_package(),)).load(
        {"workflow.content": "1.0.0"}).registry
    frozen = assemble_prompt([prompt_content([item(1, "instructions")])], text_content("question"))
    assert registry.get("prompts.assembly", "1") is None
    assert registry.validate_content(frozen, "PROMPT", 2) == frozen
    frozen["assembly"]["messages"][0]["content"] = "forged"
    with pytest.raises(ContractValidationError):
        registry.validate_content(frozen, "PROMPT", 2)


@pytest.mark.parametrize("refs", [{}, "", False])
def test_assembly_does_not_silently_accept_false_like_invalid_artifact_binding_arrays(refs):
    with pytest.raises(ContractValidationError) as caught:
        assemble_prompt([], text_content("question"), source_output_refs=refs)
    assert caught.value.reason_code == "graph_prompt_artifact_refs_invalid"


def test_history_depth_requires_future_explicit_context_anchor():
    with pytest.raises(ContractValidationError) as caught:
        assemble_prompt([prompt_content([item(1, "deep", placement="middle", depth=1)])],
                        text_content("question"))
    assert caught.value.reason_code == "graph_prompt_history_anchor_required"


@pytest.mark.parametrize("tamper", [
    lambda result: result["assembly"]["messages"][0].update(content="forged"),
    lambda result: result["assembly"]["manifest"].update(ordered_item_ids=[]),
    lambda result: result["items"].reverse(),
    lambda result: result["assembly"]["manifest"].update(
        source_output_refs=[{"edge_id": uid(60), "output_id": "latest", "order": 0}]),
    lambda result: result["assembly"].update(current_input={"role": "assistant", "content": "forged"}),
])
def test_readiness_rejects_legal_json_with_forged_messages_order_or_bindings(tamper):
    result = assemble_prompt([prompt_content([item(1, "first"), item(2, "second")])],
                             text_content("question"))
    tamper(result)
    with pytest.raises(ContractValidationError):
        validate_ready_prompt(result)


def test_fixed_group_source_increment_and_shared_upstream_execute_without_agent():
    registry = loaded().registry.detached()
    register_sink(registry)
    group = graph_node(registry, "prompts.group", 1, members=[member(11, "fixed"), member(12, "second")])
    source = graph_node(registry, "prompts.source", 2,
                        value=prompt_content([item(13, "configured")]))
    summary_a = graph_node(registry, "prompts.summary", 3)
    summary_b = graph_node(registry, "prompts.summary", 4)
    output_a = graph_node(registry, "test.prompt-sink", 5)
    output_b = graph_node(registry, "test.prompt-sink", 6)
    doc = graph_document([output_b, source, summary_b, group, output_a, summary_a], [
        graph_edge(30, group, summary_a, order=0), graph_edge(31, source, summary_a, order=1),
        graph_edge(32, group, summary_b), graph_edge(33, summary_a, output_a),
        graph_edge(34, summary_b, output_b),
    ])
    result = run_graph(registry, doc)
    assert result.status == "succeeded"
    assert len([entry for entry in result.node_runs if entry["node_binding_id"] == group["node_binding_id"]]) == 1
    assert {entry["text"] for entry in result.outputs[output_a["node_binding_id"]]["output"]["items"]} == {
        "fixed", "second", "configured"}
    assert len(result.outputs[output_b["node_binding_id"]]["output"]["items"]) == 2


def test_source_explicit_text_increment_has_stable_identity_and_preserves_fixed_materials():
    registry = loaded().registry
    entry = registry.get("prompts.source", "1")
    config = {**deepcopy(entry.definition.default_config), "value": prompt_content([item(1, "fixed")])}
    context = SimpleNamespace(node_binding_id=uid(7))
    first = entry.executor(config, {"input": text_content("increment")}, context)["output"]
    second = entry.executor(config, {"input": text_content("changed")}, context)["output"]
    assert {entry["text"] for entry in first["items"]} == {"fixed", "increment"}
    incremental = next(entry for entry in first["items"] if entry["text"] == "increment")
    changed = next(entry for entry in second["items"] if entry["text"] == "changed")
    assert incremental["item_instance_id"] == changed["item_instance_id"]
    assert config["value"] == prompt_content([item(1, "fixed")])


def test_global_reference_contains_only_identity_and_resolver_needs_no_second_config():
    registry = loaded().registry
    reference = ResourceIdentity("workspace", PROMPT_RESOURCE_TYPE, uid(9)).to_dict()
    reference_entry = registry.get("prompts.global-reference", "1")
    envelope = reference_entry.executor({"reference": reference}, {}, SimpleNamespace())["output"]
    assert envelope == global_resource_reference(reference)
    assert "secret-resource-body" not in canonical_bytes(envelope).decode()
    assert reference_entry.resource_dependencies_declaration({"reference": reference}) == [
        {"kind": "global-resource", "reference": reference}]
    calls = []
    record = {"envelope_version": 1, **{key: reference[key] for key in ("scope", "type_id", "resource_id")},
              "data_schema_version": 1, "update_sequence": 1,
              "value": {"enabled": True, "members": [member(10, "secret-resource-body")]}}
    context = SimpleNamespace(node_binding_id=uid(11), reads=[],
        host_call=lambda capability, operation, payload: (
            calls.append((capability, operation, deepcopy(payload))) or deepcopy(record)))
    resolved = registry.get("prompts.global-resolve", "1").executor({}, {"input": envelope}, context)["output"]
    assert resolved["items"][0]["text"] == "secret-resource-body"
    assert calls == [("resources:read", "current-global-resource", reference)]
    assert resolved["items"][0]["source"]["reference"] == reference
    assert "update_sequence" not in resolved["items"][0]["source"]
    assert registry.get("prompts.global-resolve", "1").definition.default_config == {}


@pytest.mark.parametrize("value", [
    {"enabled": True, "members": [member(1, "one"), member(1, "two")]},
    {"enabled": True, "members": [member(1, "invalid depth", depth=1)]},
    {"enabled": True, "members": [], "kind": "role_card"},
])
def test_global_prompt_resource_rejects_conflicts_invalid_presentation_and_role_cards(value):
    with pytest.raises(ContractValidationError):
        validate_prompt_resource(value)


def test_disabled_global_resource_is_rejected_before_execution():
    registry = loaded().registry
    reference = ResourceIdentity("workspace", PROMPT_RESOURCE_TYPE, uid(9)).to_dict()
    record = {**reference, "data_schema_version": 1, "update_sequence": 1,
              "value": {"enabled": False, "members": []}}
    with pytest.raises(ContractValidationError) as caught:
        registry.get("prompts.global-reference", "1").resource_preflight_validator(
            {"reference": reference}, [record])
    assert caught.value.reason_code == "global_content_disabled"


def test_tool_description_and_schema_use_injected_exact_catalog_and_separate_stable_ids():
    tool_schema = {"type": "object", "properties": {"text": {"type": "string"}},
                   "required": ["text"], "additionalProperties": False}
    catalog = {("inspect_text", "1"): {"description": "Inspect a text", "parameters": tool_schema}}
    registry = loaded(tools=catalog).registry
    catalog[("inspect_text", "1")]["description"] = "mutated"
    tool = registry.get("prompts.tool", "1")
    config = deepcopy(tool.definition.default_config)
    tool.config_validator(config)
    first = tool.executor(config, {}, SimpleNamespace(node_binding_id=uid(20)))
    second = tool.executor(config, {}, SimpleNamespace(node_binding_id=uid(20)))
    assert first == second
    description, schema = first["tool-descriptions"]["items"][0], first["tool-schemas"]["items"][0]
    assert description["text"] == "Inspect a text"
    assert schema["text"] == canonical_bytes(tool_schema).decode()
    assert description["item_instance_id"] != schema["item_instance_id"]
    assert description["purpose"] == "tool-description" and schema["purpose"] == "tool-schema"
    with pytest.raises(ContractValidationError) as caught:
        tool.config_validator({**config, "tool_ref": {"name": "inspect_text", "version": "2"}})
    assert caught.value.reason_code == "graph_tool_not_registered"
