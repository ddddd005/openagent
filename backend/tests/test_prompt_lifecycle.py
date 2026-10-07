"""Explicit prompt lifetime and native assembly without external model calls."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import (
    create_content_package, default_presentation, prompt_content, text_content,
)
from phase1_agent.context_prompt_v6 import (
    assemble_native_context_prompt, native_context_prompt_references, validate_native_context_prompt,
)
from phase1_agent.context_v4 import validate_native_context_view
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_message_history
from phase1_agent.graph_contracts import GraphCompiler
from phase1_agent.global_resources import global_resource_reference
from phase1_agent.prompt_lifecycle import (
    PROMPT_MATERIALS_TYPE, lifecycle_prompt_content, make_lifecycle_prompt_item,
    merge_lifecycle_prompt_materials, transform_lifecycle_prompt_materials,
    validate_lifecycle_prompt_materials,
)
from phase1_agent.prompt_lifecycle_nodes import validate_lifecycle_prompt_resource
from phase1_agent.prompt_package import create_prompt_package, validate_prompt_resource


def uid(number):
    return str(UUID(int=number, version=4))


def ref(number):
    return {"scope": "artifact", "output_id": uid(number)}


def item(number, text="background", *, role="user", lifecycle="context_once",
         compaction="allowed", **changes):
    presentation = {**default_presentation(), "role": role, **changes}
    return make_lifecycle_prompt_item(
        uid(100), uid(number), text, presentation, lifecycle=lifecycle, compaction=compaction,
        source={"kind": "fixture", "member_id": uid(number)},
    )


def content(*items):
    return merge_lifecycle_prompt_materials([lifecycle_prompt_content(list(items))])


def view(messages=None, layout=None, *, consumed=None):
    return validate_native_context_view({
        "schema_version": 4, "kind": "workflow.context-view",
        "owner": {"workflow_session_id": uid(800), "object_key": "context", "agent_node_id": uid(801)},
        "basis": {"revision_id": uid(802), "head_revision": 1},
        "messages": messages or [], "layout": layout or [], "accepted_delta_ids": [],
        "applied_delta_ids": [], "once_injected_item_ids": consumed or [], "generation": 0,
        "derivation": {"operation": "read", "input_view_ref": None, "update_ref": None},
    })


def assemble(materials, initial=None, **changes):
    return assemble_native_context_prompt(
        materials, initial or view(), text_content("Current task"),
        context_ref=ref(900), current_input_ref=ref(901), **changes,
    )


def loaded():
    return CapabilityPackageLoader((create_content_package(), create_prompt_package())).load(
        {"workflow.prompts": "1.0.0"})


def test_new_materials_do_not_accept_old_prompt_envelope_or_implicit_lifetime():
    modern = content(item(1))
    assert validate_lifecycle_prompt_materials(modern) == modern
    with pytest.raises(ContractValidationError):
        merge_lifecycle_prompt_materials([prompt_content([])])
    forged = deepcopy(modern)
    del forged["items"][0]["lifecycle"]
    with pytest.raises(ContractValidationError):
        validate_lifecycle_prompt_materials(forged)


def test_aggregation_preserves_all_pre_dedup_origin_identities_and_is_idempotent():
    first, second = item(1), item(2)
    combined = merge_lifecycle_prompt_materials([content(second), content(first)])
    expected = sorted([first["item_instance_id"], second["item_instance_id"]], key=lambda value: UUID(value).int)
    assert len(combined["items"]) == 1
    assert combined["items"][0]["origin_item_ids"] == expected
    assert merge_lifecycle_prompt_materials([combined, content(first), combined]) == combined
    assert first["origin_item_ids"] == [first["item_instance_id"]]


def test_lifetime_and_permission_are_business_semantics_not_metadata():
    original = item(1)
    other = item(2, lifecycle="per_request", compaction="never")
    assert len(content(original, other)["items"]) == 2
    with pytest.raises(ContractValidationError):
        item(3, role="system", compaction="allowed")
    with pytest.raises(ContractValidationError):
        item(4, lifecycle="per_request", compaction="allowed")
    assert item(5, role="system", compaction="never")["lifecycle"] == "context_once"


@pytest.mark.parametrize("field", ["text", "source"])
def test_duplicate_stable_identity_with_conflicting_content_or_provenance_is_rejected(field):
    first = item(1)
    forged = deepcopy(first)
    if field == "text":
        forged[field] = "changed"
    else:
        forged[field]["member_id"] = uid(99)
    with pytest.raises(ContractValidationError) as caught:
        merge_lifecycle_prompt_materials([content(first), lifecycle_prompt_content([forged])])
    assert caught.value.reason_code == "graph_prompt_identity_conflict"


def test_transform_preserves_lifetime_and_complete_origins():
    original = content(item(1), item(2))
    transformed = transform_lifecycle_prompt_materials(original, lambda texts: [value.upper() for value in texts], "text")
    assert transformed["items"][0]["text"] == "BACKGROUND"
    assert transformed["items"][0]["origin_item_ids"] == original["items"][0]["origin_item_ids"]
    assert transformed["items"][0]["lifecycle"] == "context_once"
    assert transformed["items"][0]["compaction"] == "allowed"


def test_new_producers_are_explicit_versions_and_keep_old_resource_semantics():
    registry = loaded().registry
    assert registry.data_types.get(PROMPT_MATERIALS_TYPE, 1, scope="content")
    modern = registry.get("prompts.item", "2")
    legacy = registry.get("prompts.item", "1")
    assert modern.definition.outputs[0].data_type == PROMPT_MATERIALS_TYPE
    assert legacy.definition.outputs[0].data_type == "PROMPT"
    config = deepcopy(modern.definition.default_config)
    output = modern.executor(config, {}, SimpleNamespace(node_binding_id=uid(5)))["output"]
    assert output["items"][0]["lifecycle"] == "per_request"
    assert output["items"][0]["compaction"] == "never"
    assert registry.validate_content(output, PROMPT_MATERIALS_TYPE, 1) == output
    assert validate_prompt_resource({"enabled": True, "members": []}) == {"enabled": True, "members": []}
    assert validate_lifecycle_prompt_resource({"enabled": True, "members": []}) == {"enabled": True, "members": []}


def test_resource_two_requires_explicit_member_lifetime_and_never_elevates_system_rules():
    member = {"id": uid(1), "text": "rule", "presentation": default_presentation(),
              "metadata": {}, "lifecycle": "context_once", "compaction": "never"}
    value = {"enabled": True, "members": [member]}
    assert validate_lifecycle_prompt_resource(value) == value
    with pytest.raises(ContractValidationError):
        validate_prompt_resource(value)
    with pytest.raises(ContractValidationError):
        validate_lifecycle_prompt_resource({
            "enabled": True, "members": [{key: val for key, val in member.items() if key != "lifecycle"}]})
    with pytest.raises(ContractValidationError):
        validate_lifecycle_prompt_resource({"enabled": True, "members": [{**member, "compaction": "allowed"}]})


def test_global_version_two_source_requires_resource_two_without_implicit_upgrade():
    registry = loaded().registry
    reference = {"envelope_version": 1, "scope": "workspace",
                 "type_id": "workflow.prompt-resource", "resource_id": uid(1)}
    record = {**reference, "data_schema_version": 1, "update_sequence": 1,
              "value": {"enabled": True, "members": []}}
    modern = registry.get("prompts.global-reference", "2")
    with pytest.raises(ContractValidationError):
        modern.resource_preflight_validator({"reference": reference}, [record])
    record["data_schema_version"] = 2
    modern.resource_preflight_validator({"reference": reference}, [record])
    with pytest.raises(ContractValidationError):
        registry.get("prompts.global-reference", "1").resource_preflight_validator(
            {"reference": reference}, [record])


def test_global_resource_edits_keep_once_identity_and_do_not_use_resource_sequence_as_identity():
    registry = loaded().registry
    reference = {"envelope_version": 1, "scope": "workspace",
                 "type_id": "workflow.prompt-resource", "resource_id": uid(1)}
    member = {"id": uid(2), "text": "original", "presentation": {
        **default_presentation(), "role": "user"},
        "metadata": {}, "lifecycle": "context_once", "compaction": "allowed"}
    record = {**reference, "data_schema_version": 2, "update_sequence": 1,
              "value": {"enabled": True, "members": [member]}}
    context = SimpleNamespace(node_binding_id=uid(3), reads=[],
                              host_call=lambda *args: deepcopy(record))
    envelope = global_resource_reference(reference)
    entry = registry.get("prompts.global-resolve", "2")
    first = entry.executor({}, {"input": envelope}, context)["output"]
    record["update_sequence"] = 2
    record["value"]["members"][0]["text"] = "edited"
    second = entry.executor({}, {"input": envelope}, context)["output"]
    assert first["items"][0]["origin_item_ids"] == second["items"][0]["origin_item_ids"]
    assert second["items"][0]["text"] == "edited"
    assert len(assemble([second], view(consumed=first["items"][0]["origin_item_ids"]))["messages"]) == 1


def test_tool_materials_are_per_request_and_never_opt_into_compaction():
    tools = {("inspect_text", "1"): {"description": "Inspect text",
                                    "parameters": {"type": "object", "properties": {}}}}
    registry = CapabilityPackageLoader((create_content_package(), create_prompt_package(tool_catalog=tools))).load(
        {"workflow.prompts": "1.0.0"}).registry
    entry = registry.get("prompts.tool", "2")
    entry.config_validator(entry.definition.default_config)
    result = entry.executor(deepcopy(entry.definition.default_config), {},
                            SimpleNamespace(node_binding_id=uid(3)))
    for value in result.values():
        assert validate_lifecycle_prompt_materials(value) == value
        assert value["items"][0]["lifecycle"] == "per_request"
        assert value["items"][0]["compaction"] == "never"


def test_assembly_injects_once_and_fixed_materials_but_only_marks_pending_consumption():
    once = item(1)
    fixed = item(2, "system rule", role="system", lifecycle="per_request", compaction="never")
    initial = view()
    result = assemble([content(once, fixed)], initial)
    assert [entry["kind"] for entry in result["layout"]].count("once") == 1
    assert [entry["kind"] for entry in result["layout"]].count("fixed") == 1
    assert result["once_pending_item_ids"] == once["origin_item_ids"]
    assert initial["once_injected_item_ids"] == []
    assert result["context"]["once_injected_item_ids"] == []
    assert validate_native_context_prompt(result) == result
    validate_message_history(result["messages"])


def test_consumed_once_item_edit_is_not_reinjected_and_branch_inherits_consumption():
    once = item(1)
    first = assemble([content(once)])
    consumed = view(consumed=once["origin_item_ids"])
    edited = item(1, "edited original background")
    next_prompt = assemble([content(edited)], consumed)
    assert len(next_prompt["messages"]) == 1
    assert next_prompt["once_pending_item_ids"] == []
    forked = deepcopy(consumed)
    forked["owner"]["workflow_session_id"] = uid(850)
    assert len(assemble([content(edited)], forked)["messages"]) == 1
    assert first["context"]["once_injected_item_ids"] == []


def test_partially_consumed_merged_item_does_not_repeat_text_but_adopts_new_origin_ids_later():
    first, second = item(1), item(2)
    consumed = view(consumed=first["origin_item_ids"])
    result = assemble([content(first, second)], consumed)
    assert len(result["messages"]) == 1
    assert result["once_pending_item_ids"] == second["origin_item_ids"]
    assert consumed["once_injected_item_ids"] == first["origin_item_ids"]


def test_per_request_text_never_consumes_once_ids_and_disabled_once_item_remains_unconsumed():
    fixed = item(1, lifecycle="per_request", compaction="never")
    disabled = item(2, enabled=False)
    result = assemble([content(fixed, disabled)])
    assert result["once_pending_item_ids"] == []
    assert len(result["messages"]) == 2
    assert result["layout"][0]["kind"] == "fixed"


def history_text(number, role="user"):
    return {"schema_version": 3, "message_id": uid(number), "role": role,
            "source": {"kind": "prompt", "prompt_id": uid(number + 100), "revision": 1,
                       "item_instance_id": uid(number + 200), "group_instance_id": None},
            "blocks": [{"kind": "text", "text": str(number)}]}


def test_middle_placement_anchors_complete_history_rounds_and_canonical_ids_are_stable():
    messages = [history_text(10), history_text(11, "assistant"), history_text(12)]
    layout = [{"kind": "history", "round_id": uid(50), "compaction": "allowed"},
              {"kind": "history", "round_id": uid(50), "compaction": "allowed"},
              {"kind": "history", "round_id": uid(51), "compaction": "allowed"}]
    initial = view(messages, layout)
    material = item(1, "between rounds", lifecycle="per_request", compaction="never",
                    placement="middle", depth=1)
    prompt = assemble([content(material)], initial)
    assert [message["blocks"][0]["text"] for message in prompt["messages"]] == [
        "10", "11", "between rounds", "12", "Current task"]
    assert [prompt["messages"][index]["message_id"] for index in (0, 1, 3)] == [
        message["message_id"] for message in messages]
    second = assemble_native_context_prompt(
        [content(material)], initial, text_content("Current task"),
        context_ref=ref(950), current_input_ref=ref(901))
    assert second["messages"] == prompt["messages"]


def test_middle_material_never_splits_a_closed_tool_batch():
    call = {"schema_version": 1, "message_id": uid(10), "role": "assistant",
            "source": {"kind": "model", "request_id": uid(11)}, "blocks": [{
                "kind": "tool_call", "tool_call_id": uid(12), "tool_name": "inspect_text",
                "tool_definition_version": "1", "raw_arguments": "{}", "parsed_arguments": {}}]}
    result = {"schema_version": 1, "message_id": uid(13), "role": "tool",
              "source": {"kind": "tool", "tool_execution_id": uid(14)}, "blocks": [{
                  "kind": "tool_result", "tool_call_id": uid(12), "tool_execution_id": uid(14),
                  "status": "success", "is_error": False, "content": "ok", "model_visible_text": "ok"}]}
    initial = view([call, result], [
        {"kind": "history", "round_id": uid(50), "compaction": "never"},
        {"kind": "history", "round_id": uid(50), "compaction": "never"}])
    material = item(1, "before complete batch", lifecycle="per_request", compaction="never",
                    placement="middle", depth=1)
    prompt = assemble([content(material)], initial)
    assert prompt["messages"][1:3] == [call, result]
    validate_message_history(prompt["messages"])
    forged = deepcopy(initial)
    forged["layout"][1]["round_id"] = uid(51)
    with pytest.raises(ContractValidationError):
        assemble([content(material)], forged)


def test_once_system_material_stays_protected_in_native_projection():
    material = item(1, "system constraint", role="system", compaction="never")
    prompt = assemble([content(material)])
    assert prompt["messages"][0]["role"] == "system"
    assert prompt["layout"][0] == {"kind": "once", "round_id": None, "compaction": "never"}
    assert prompt["once_pending_item_ids"] == material["origin_item_ids"]

@pytest.mark.parametrize("change", [
    lambda result: result["messages"][0]["blocks"][0].update(text="forged"),
    lambda result: result["layout"][0].update(compaction="never"),
    lambda result: result.update(once_pending_item_ids=[]),
    lambda result: result["current_input"].update(message_id=uid(999)),
    lambda result: result["provenance"][0].update(lifecycle="per_request"),
])
def test_frozen_native_prompt_rejects_modified_projection_or_lifetime_state(change):
    result = assemble([content(item(1))])
    change(result)
    with pytest.raises(ContractValidationError):
        validate_native_context_prompt(result)


def test_native_prompt_references_exact_context_current_input_and_material_artifacts():
    refs = [{"edge_id": uid(910), "output_id": uid(911), "order": 0}]
    result = assemble([content(item(1))], source_output_refs=refs)
    assert native_context_prompt_references(result) == [ref(900), ref(901), ref(911)]


@pytest.mark.parametrize("refs", [{}, "", False])
def test_native_assembly_does_not_silently_default_invalid_artifact_reference_arrays(refs):
    with pytest.raises(ContractValidationError):
        assemble([], source_output_refs=refs)


def test_graph_compiler_does_not_connect_old_material_port_to_new_material_consumer():
    registry = loaded().registry
    first = registry.get("prompts.item", "1").definition
    second = registry.get("prompts.summary", "2").definition
    nodes = [{
        "node_binding_id": uid(number), "component_id": definition.component_id,
        "component_version": definition.component_version, "title": definition.display_name,
        "position": {"x": 0, "y": 0}, "config": deepcopy(definition.default_config),
    } for number, definition in ((1, first), (2, second))]
    document = {"schema_version": 1, "workflow_definition_id": uid(990), "revision": 1,
                "name": "Explicit material contracts", "nodes": nodes,
                "edges": [{"edge_id": uid(3), "source_node_id": uid(1), "source_port_id": "output",
                           "target_node_id": uid(2), "target_port_id": "input", "order": 0}]}
    with pytest.raises(ContractValidationError):
        GraphCompiler(registry).compile(document)
