import copy

import pytest

from phase1_agent.graph_contracts import GraphDiagnosticError, validate_content_value
from phase1_agent.graph_nodes import create_default_registry
from phase1_agent.workbench_resources import CORE_SESSION_NOTE

from test_graph_execution import document, edge, identity, material, node, prompt_node, run_graph


def test_catalog_contains_only_declarations_and_explicit_mode_port_variants():
    registry = create_default_registry()
    catalog = {entry["component_id"]: entry for entry in registry.catalog()}
    assert catalog["workflow.output"]["is_output"] is True
    assert catalog["workflow.text"]["is_output"] is False
    assert catalog["workflow.regex"]["port_modes"]["prompt"]["inputs"][0]["data_type"] == "PROMPT"
    assert catalog["workflow.agent"]["executable"] is True
    assert catalog["workflow.model-provider"]["executable"] is True
    assert registry.get("workflow.agent", "1").executor is None
    assert registry.get("workflow.model-provider", "1").executor is None
    catalog["workflow.text"]["default_config"]["text"] = "modified"
    assert registry.get("workflow.text", "1").definition.default_config == {"text": ""}


def test_current_input_is_explicit_and_literal_text_needs_no_external_input():
    value = document([node(1, "workflow.current-input"), node(2, "workflow.output")], [edge(10, 1, 2)])
    assert run_graph(value, external_inputs={"text": "input"}).outputs[identity(2)]["output"]["text"] == "input"
    assert run_graph(value).diagnostic["code"] == "graph_external_input_missing"
    literal = document([node(1, config={"text": "literal"}), node(2, "workflow.output")], [edge(10, 1, 2)])
    assert run_graph(literal).status == "succeeded"


def test_prompt_item_instances_are_scoped_by_binding_and_group_members_remain_ordered():
    registry = create_default_registry()
    item = copy.deepcopy(registry.get("workflow.prompt-item", "1").definition.default_config)
    item["text"] = "same definition"
    value = document([node(1, "workflow.prompt-item", item), node(2, "workflow.prompt-item", item),
                      node(3, "workflow.prompt-summary"), node(4, "workflow.output", {"mode": "prompt"})],
                     [edge(10, 1, 3, order=0), edge(11, 2, 3, order=1), edge(12, 3, 4)])
    items = run_graph(value).outputs[identity(4)]["output"]["items"]
    assert len(items) == 2 and items[0]["item_instance_id"] != items[1]["item_instance_id"]
    assert items[0]["source"]["item_id"] == items[1]["source"]["item_id"]
    group = {"groupId": identity(80), "revision": 5, "enabled": True,
             "members": [{"id": identity(81), "name": "first", **item},
                         {"id": identity(82), "name": "second", **{**item, "text": "second"}}]}
    value = document([node(1, "workflow.prompt-group", group), node(2, "workflow.output", {"mode": "prompt"})],
                     [edge(10, 1, 2)])
    items = run_graph(value).outputs[identity(2)]["output"]["items"]
    assert [item["text"] for item in items] == ["same definition", "second"]
    assert all(item["source"]["group_revision"] == 5 for item in items)


def test_prompt_regex_preserves_context_tool_and_configuration_structure_and_protection():
    items = [
        material(80, "old", purpose="prompt", source={"kind": "global", "resource_id": identity(70), "revision": 4}),
        material(81, "old", purpose="context", source={"kind": "context", "session_id": identity(71), "floor": "delta"}),
        material(82, "old", purpose="tool-description", source={"kind": "tool", "name": "read", "version": "1"}),
        material(83, '{"description":"old"}', purpose="tool-schema", metadata={"format": "json", "tool": {"name": "read", "version": "1"}}),
        material(84, "old", purpose="context", protected=True, role="tool", metadata={"canonical": {"blocks": [{"kind": "text", "text": "old"}]}}),
    ]
    rule = {"mode": "prompt", "pattern": "old", "replacement": "new", "flags": [], "replaceMode": "all"}
    value = document([prompt_node(1, items), node(2, "workflow.regex", rule),
                      node(3, "workflow.output", {"mode": "prompt"})], [edge(10, 1, 2), edge(11, 2, 3)])
    result = run_graph(value)
    assert result.status == "succeeded"
    changed = result.outputs[identity(3)]["output"]["items"]
    assert [item["text"] for item in changed] == ["new", "new", "new", '{"description":"new"}', "old"]
    for original, derived in zip(items, changed):
        assert {key: value for key, value in original.items() if key != "text"} == {
            key: value for key, value in derived.items() if key != "text"
        }
    assert value["nodes"][0]["config"]["value"]["items"] == items


def test_variable_replacement_reads_value_at_its_execution_step_and_not_at_run_start():
    state = {"revision": 2, "values": {"倒计时": {"type": "integer", "source": "assignment", "value": 10}}}

    def change_after_source(event):
        if event["node_binding_id"] == identity(1) and event["event"] == "succeeded":
            event["state"]["values"]["倒计时"]["value"] = 9
            event["state"]["revision"] = 3

    value = document([node(1, config={"text": "剩余{{倒计时}}"}), node(2, "workflow.variable-replace"),
                      node(3, "workflow.output")], [edge(10, 1, 2), edge(11, 2, 3)])
    result = run_graph(value, state=state, on_node=change_after_source)
    assert result.outputs[identity(3)]["output"]["text"] == "剩余9"
    assert result.node_runs[1]["reads"][0]["revision"] == 3
    assert state["values"]["倒计时"]["value"] == 10


def test_variable_prompt_replacement_preserves_protected_items_and_does_not_recurse():
    state = {"revision": 1, "values": {"内容": {"type": "string", "source": "assignment", "value": "{{other}}"}}}
    items = [material(80, "{{内容}}"), material(81, "{{内容}}", protected=True)]
    value = document([prompt_node(1, items), node(2, "workflow.variable-replace", {"mode": "prompt"}),
                      node(3, "workflow.output", {"mode": "prompt"})], [edge(10, 1, 2), edge(11, 2, 3)])
    changed = run_graph(value, state=state).outputs[identity(3)]["output"]["items"]
    assert changed[0]["text"] == "{{other}}" and changed[1] == items[1]


def test_macro_limit_applies_to_entire_prompt_batch_without_repeated_value_snapshots():
    state = {"revision": 1, "values": {"value": {"type": "string", "source": "assignment", "value": "x" * 600_000}}}
    value = document([prompt_node(1, [material(80, "{{value}}"), material(81, "{{value}}")]),
                      node(2, "workflow.variable-replace", {"mode": "prompt"}),
                      node(3, "workflow.output", {"mode": "prompt"})], [edge(10, 1, 2), edge(11, 2, 3)])
    result = run_graph(value, state=state)
    assert result.status == "failed" and result.diagnostic["code"] == "macro_output_limit"
    assert len(result.node_runs[-1]["reads"]) == 1
    assert "values" not in result.node_runs[-1]["reads"][0]


def test_variable_registration_does_not_overwrite_dynamic_current_values():
    config = {"name": "倒计时", "valueType": "integer", "hasInitialValue": True, "initialValue": 10}
    value = document([node(1, "workflow.variable-register", config), node(2, "workflow.output")],
                     [edge(10, 1, 2, "text")])
    assert run_graph(value).outputs[identity(2)]["output"]["text"] == "10"
    state = {"revision": 3, "values": {"倒计时": {"type": "integer", "source": "assignment", "value": 2}}}
    assert run_graph(value, state=state).outputs[identity(2)]["output"]["text"] == "2"


def test_unassigned_registration_preserves_absence_and_reports_missing_value_to_consumers():
    config = {"name": "unassigned", "valueType": "string", "hasInitialValue": False, "initialValue": None}
    value = document([node(1, "workflow.variable-register", config), node(2, "workflow.output")],
                     [edge(10, 1, 2, "text")])
    result = run_graph(value)
    assert result.status == "failed" and result.diagnostic["code"] == "graph_linked_output_missing"
    assert result.state["values"]["unassigned"] == {"type": "string", "source": "unassigned"}


def test_numeric_assignment_uses_existing_session_registration_and_safe_range():
    config = {"name": "count", "valueType": "integer", "operation": "subtract", "value": 1}
    value = document([node(1, "workflow.variable-assign", config), node(2, "workflow.output")],
                     [edge(10, 1, 2, "text")])
    state = {"revision": 2, "values": {"count": {"type": "integer", "source": "assignment", "value": 10}}}
    result = run_graph(value, state=state)
    assert result.outputs[identity(2)]["output"]["text"] == "9"
    assert result.state["values"]["count"]["value"] == 9
    assert run_graph(value).diagnostic["code"] == "variable_not_registered"


def test_text_prompt_conversion_has_explicit_presentation_and_separator():
    value = document([node(1, config={"text": "plain"}), node(2, "workflow.text-to-prompt"),
                      node(3, "workflow.prompt-to-text", {"separator": "|"}), node(4, "workflow.output")],
                     [edge(10, 1, 2), edge(11, 2, 3), edge(12, 3, 4)])
    result = run_graph(value)
    assert result.outputs[identity(2)]["output"]["items"][0]["role"] == "system"
    assert result.outputs[identity(4)]["output"]["text"] == "plain"


def test_protected_context_cannot_be_implicitly_flattened():
    value = document([prompt_node(1, [material(80, "closed fact", protected=True)]),
                      node(2, "workflow.prompt-to-text"), node(3, "workflow.output")],
                     [edge(10, 1, 2), edge(11, 2, 3)])
    result = run_graph(value)
    assert result.status == "failed" and result.diagnostic["code"] == "graph_protected_content"


def test_session_data_write_and_json_projection_preserve_original_schema():
    definition = {**CORE_SESSION_NOTE, "schema": {"type": "object", "properties": {"count": {"type": "integer"}},
                                                "required": ["count"], "additionalProperties": False},
                  "default": {"count": 0}}
    value = document([node(1, config={"text": '{"count":3}'}),
                      node(2, "workflow.session-data-write", {"definition": definition, "value": {"count": 0}}),
                      node(3, "workflow.json-to-text"), node(4, "workflow.output")],
                     [edge(10, 1, 2, target_port="text"), edge(11, 2, 3, "json"), edge(12, 3, 4)])
    result = run_graph(value)
    assert result.state["data"]["core:session_note"] == {"definition": definition, "value": {"count": 3}}
    assert result.outputs[identity(4)]["output"]["text"] == '{"count": 3}'


def test_registered_shared_definition_cannot_be_changed_by_same_key():
    definition = {**CORE_SESSION_NOTE, "revision": 2}
    state = {"revision": 1, "values": {}, "data": {"core:session_note": {"definition": CORE_SESSION_NOTE, "value": "old"}}}
    value = document([node(1, "workflow.session-data-write", {"definition": definition, "value": "new"}),
                      node(2, "workflow.json-to-text"), node(3, "workflow.output")],
                     [edge(10, 1, 2, "json"), edge(11, 2, 3)])
    result = run_graph(value, state=state)
    assert result.status == "failed" and result.diagnostic["code"] == "session_data_definition_conflict"
    assert result.state == state


def test_regex_worker_errors_are_structured_and_do_not_complete_output():
    rule = {"mode": "text", "pattern": "(", "replacement": "", "flags": [], "replaceMode": "all"}
    value = document([node(1, config={"text": "input"}), node(2, "workflow.regex", rule),
                      node(3, "workflow.output")], [edge(10, 1, 2), edge(11, 2, 3)])
    result = run_graph(value)
    assert result.status == "failed" and result.diagnostic["code"] == "regex_invalid_pattern"
    assert identity(3) not in result.outputs


def test_prompt_boundary_rejects_forged_tool_role_and_old_assembly_evidence():
    value = {"schema_version": 1, "kind": "workflow.prompt", "stage": "materials",
             "items": [material(80, "fact", role="tool")], "assembly": None}
    with pytest.raises(GraphDiagnosticError):
        validate_content_value(value, "PROMPT")
    value.update(stage="assembled", items=[], assembly={"old": "evidence"})
    with pytest.raises(GraphDiagnosticError):
        validate_content_value(value, "PROMPT")
