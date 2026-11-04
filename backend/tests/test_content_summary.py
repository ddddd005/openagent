"""Structured aggregation stays separate from the final frozen Agent input."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.content_contracts import default_presentation, make_prompt_item, prompt_content, text_content
from phase1_agent.content_summary import summarize_content, validate_content_summary
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import GraphCompiler, GraphDiagnosticError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.lorebook_engine import default_lorebook_entry

from test_context_native_integration import NativeTransport, native_graph, node_output, versioned_node
from test_graph_service import create, edge
from test_models_service_integration import ModelDatabaseFixture
from test_prompt_lifecycle import content, item, ref, uid, view
from workflow_test_support import run


def test_summary_retains_optional_inputs_without_applying_lifetime_or_placement():
    materials = content(item(1, "Once background"), item(2, "Late rule", placement="middle", depth=40))
    original = deepcopy(materials)
    result = summarize_content(materials=[materials], view=view(), context_ref=ref(1),
                               current_input=text_content("task"), current_input_ref=ref(2))
    assert result["materials"] == original
    assert result["current_input"] == text_content("task")
    assert set(result) == {
        "schema_version", "kind", "materials", "source_output_refs",
        "context", "context_ref", "current_input", "current_input_ref",
    }
    assert materials == original
    assert summarize_content([])["materials"]["items"] == []
    assert "context" not in summarize_content([materials])


def test_summary_can_merge_materials_and_retain_original_context_references():
    first = summarize_content([content(item(1))], view=view(), context_ref=ref(1),
                              current_input=text_content("task"), current_input_ref=ref(2))
    result = summarize_content([content(item(2, "another"))], summary=first)
    assert len(result["materials"]["items"]) == 2
    assert result["context_ref"] == first["context_ref"]
    assert result["current_input_ref"] == first["current_input_ref"]
    assert summarize_content([], summary=result) == result
    with pytest.raises(ContractValidationError, match="different contexts or current inputs"):
        summarize_content([], summary=first, current_input=text_content("other"), current_input_ref=ref(3))


def test_ordinary_prompt_materials_are_explicitly_projected_but_snapshots_are_rejected():
    ordinary = prompt_content([make_prompt_item(uid(1), uid(2), "ordinary", default_presentation())])
    result = summarize_content([ordinary])
    projected = result["materials"]["items"][0]
    assert projected["text"] == "ordinary" and projected["lifecycle"] == "per_request"
    assert projected["source"]["original"] == ordinary["items"][0]["source"]
    frozen = {**ordinary, "stage": "assembled", "assembly": {}}
    with pytest.raises(ContractValidationError):
        summarize_content([frozen])


@pytest.mark.parametrize("change", [
    lambda value: value.pop("context_ref"),
    lambda value: value.update(messages=[]),
    lambda value: value["current_input"].update(kind="workflow.prompt"),
])
def test_summary_rejects_partial_references_and_frozen_projection_fields(change):
    value = summarize_content([], view=view(), context_ref=ref(1),
                              current_input=text_content("task"), current_input_ref=ref(2))
    change(value)
    with pytest.raises(ContractValidationError):
        validate_content_summary(value)


def summary_graph(service):
    document, native = native_graph(service, policy=False, once=False)
    lane = native["a"]
    lane["assembly"]["component_version"] = "7"
    current = next(node for node in document["nodes"] if node["component_id"] == "tools.current-input")
    summary = versioned_node(service, "prompts.summary", "3", 9100)
    lorebook = versioned_node(service, "lorebook.group", "2", 9101)
    combined = versioned_node(service, "prompts.summary", "3", 9102)
    triggered = default_lorebook_entry(uid(9200))
    triggered.update(text="WORLD_RULE", primary_keywords=["dragon"])
    skipped = default_lorebook_entry(uid(9201))
    skipped.update(text="METADATA_MUST_NOT_TRIGGER", primary_keywords=["METADATA_TRIGGER"])
    lane["fixed"]["config"]["metadata"] = {"trigger_marker": "METADATA_TRIGGER"}
    lorebook["config"]["entries"] = [triggered, skipped]
    document["nodes"].extend([summary, lorebook, combined])
    document["edges"] = [row for row in document["edges"]
                         if row["target_node_id"] != lane["assembly"]["node_binding_id"]]
    document["edges"].extend([
        edge(lane["read"], summary, 9300, target_port="view"),
        edge(current, summary, 9301, target_port="current_input"),
        edge(lane["fixed"], summary, 9302),
        edge(summary, lorebook, 9303, target_port="structured"),
        edge(summary, combined, 9304, target_port="summary"),
        edge(lorebook, combined, 9305),
        edge(combined, lane["assembly"], 9306, target_port="summary"),
    ])
    return document, {**lane, "summary": summary, "lorebook": lorebook, "combined": combined}


@pytest.mark.parametrize("global_resource", [False, True])
def test_summary_lorebook_final_assembly_and_context_writeback_two_rounds(tmp_path, global_resource):
    transport = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "summary.sqlite",
                                      public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, nodes = summary_graph(service)
        if global_resource:
            from phase1_agent.graph_application import GraphApplication
            from test_tavern_global_lorebook import identity, record, save

            resource = record(entries=deepcopy(nodes["lorebook"]["config"]["entries"]))
            save(GraphApplication(service), resource)
            reference = versioned_node(service, "lorebook.global-reference", "1", 9600)
            reference["config"]["reference"] = {"envelope_version": 1, **identity(resource)}
            document["nodes"].append(reference)
            nodes["lorebook"].update(component_id="lorebook.global-activate", config={"object_keys": []})
            document["edges"].append(edge(reference, nodes["lorebook"], 9601, target_port="resource"))
        initial = create(service, document)
        first = run(service, initial, "A dragon appears.")
        assert first["status"] == "succeeded", first["chains"]
        summary = node_output(first, nodes["summary"])
        assert summary["kind"] == "workflow.content-summary" and "messages" not in summary
        assert {item["text"] for item in node_output(first, nodes["lorebook"])["items"]} == {"WORLD_RULE"}
        prompt = node_output(first, nodes["assembly"])
        assert prompt["stage"] == "assembled" and prompt["schema_version"] == 6
        assert prompt["context_ref"] == summary["context_ref"]
        assert prompt["current_input_ref"] == summary["current_input_ref"]
        assert "WORLD_RULE" in str(transport.calls[0]["messages"])
        assert "FIXED_RULE_a" in str(transport.calls[0]["messages"])
        assert "METADATA_MUST_NOT_TRIGGER" not in str(transport.calls)
        assert "WORLD_RULE" not in str(first["objects"]["context-a"]["value"])
        frozen = deepcopy(summary)
        second = run(service, first, "Continue.")
        assert second["status"] == "succeeded", second["chains"]
        assert len(transport.calls) == 2
        assert second["objects"]["context-a"]["revision"] == 3
        assert node_output(first, nodes["summary"]) == frozen
        assert node_output(second, nodes["summary"])["context"]["messages"]


def test_new_worldbook_ports_reject_frozen_agent_prompt_at_compile_time(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "reject.sqlite")) as service:
        ModelDatabaseFixture.write(service, 1)
        document, nodes = summary_graph(service)
        document["edges"] = [row for row in document["edges"]
                             if row["source_node_id"] != nodes["lorebook"]["node_binding_id"]]
        for row in document["edges"]:
            if row["target_node_id"] == nodes["lorebook"]["node_binding_id"]:
                row["source_node_id"] = nodes["assembly"]["node_binding_id"]
        with pytest.raises(GraphDiagnosticError) as raised:
            GraphCompiler(service.registry).compile(document)
        assert raised.value.reason_code == "graph_type_mismatch"


def test_text_input_accepts_explicit_prompt_to_text_projection(tmp_path):
    transport = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "explicit-text.sqlite",
                                      public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, nodes = summary_graph(service)
        nodes["lorebook"]["config"]["entries"][1]["text"] = "PROMPT_TEXT_RULE"
        nodes["lorebook"]["config"]["entries"][1]["primary_keywords"] = ["FIXED_RULE_a"]
        document["edges"] = [row for row in document["edges"]
                             if row["target_node_id"] != nodes["lorebook"]["node_binding_id"]]
        convert = versioned_node(service, "tools.prompt-to-text", "2", 9400)
        document["nodes"].append(convert)
        document["edges"].extend([
            edge(nodes["fixed"], convert, 9401),
            edge(convert, nodes["lorebook"], 9402, target_port="text"),
        ])
        result = run(service, create(service, document), "No keyword here.")
        assert result["status"] == "succeeded", result["chains"]
        assert {item["text"] for item in node_output(result, nodes["lorebook"])["items"]} == {
            "PROMPT_TEXT_RULE"}
        assert node_output(result, convert) == text_content("FIXED_RULE_a")


def test_text_input_does_not_implicitly_convert_structured_prompt(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "reject-text.sqlite")) as service:
        ModelDatabaseFixture.write(service, 1)
        document, nodes = summary_graph(service)
        document["edges"] = [row for row in document["edges"]
                             if row["target_node_id"] != nodes["lorebook"]["node_binding_id"]]
        document["edges"].append(edge(nodes["fixed"], nodes["lorebook"], 9500, target_port="text"))
        with pytest.raises(GraphDiagnosticError) as raised:
            GraphCompiler(service.registry).compile(document)
        assert raised.value.reason_code == "graph_type_mismatch"


def test_input_assembly_reports_missing_current_input_without_calling_model(tmp_path):
    transport = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "incomplete.sqlite",
                                      public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, nodes = summary_graph(service)
        document["edges"] = [row for row in document["edges"]
                             if row["target_port_id"] != "current_input"]
        result = run(service, create(service, document), "must not be guessed")
        assert result["status"] == "failed"
        assembly = next(node for node in result["nodes"]
                        if node["node_binding_id"] == nodes["assembly"]["node_binding_id"])
        assert assembly["diagnostic"]["code"] == "input_assembly_incomplete"
        assert transport.calls == []


def test_input_assembly_keeps_direct_inputs_and_paired_tool_prompt(tmp_path):
    from test_variable_tool_bindings import VariableToolTransport, _registered, _run, _service, build_graph

    transport = VariableToolTransport([("final_answer", {"answer": {"text": "accepted"}})])
    with closing(_service(tmp_path / "tools.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, nodes = build_graph(service)
        nodes["assembly"]["component_version"] = "7"
        initial = _registered(service, document)
        _chain, result = _run(service, initial, text="Direct input")
        assert result["status"] == "succeeded", result["chains"]
        prompt = node_output(result, nodes["assembly"])
        assert prompt["current_input"]["blocks"][0]["text"] == "Direct input"
        assert any(item["purpose"] == "tool-description" for item in prompt["items"])
        assert any(tool["function"]["name"] == "score_read" for tool in transport.calls[0]["tools"])
        descriptions = [item["text"] for item in prompt["items"] if item["purpose"] == "tool-description"]
        assert all(description in str(transport.calls[0]["messages"]) for description in descriptions)
