"""Draft-only input discovery preserves declarations without execution or writes."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from test_graph_service import service, document, node, edge, create


def test_incomplete_graph_retains_editable_inputs_and_diagnoses_compile_failure(service):
    source = node(service.registry, "current-input", 1, input_name="message")
    missing = node(service.registry, "output", 2)
    doc = document([source, missing], [])
    original = deepcopy(doc)
    result = GraphApplication(service).query("definition.inputs", {"document": doc})
    assert doc == original
    assert result["inputs"] == [{"name": "message", "data_type": "TEXT", "required": True,
                                "node_ids": [source["node_binding_id"]], "conflict": False}]
    assert result["required_for_run"] is None
    assert result["diagnostics"]
    assert service.list_sessions(doc["workflow_definition_id"]) == []


def test_disconnected_inputs_are_editable_but_not_required_for_current_targets(service):
    source = node(service.registry, "current-input", 1, input_name="message")
    unused = node(service.registry, "current-input", 3, input_name="unused")
    output = node(service.registry, "output", 2)
    doc = document([source, unused, output], [edge(source, output, 1)])
    doc["execution_roots"] = [output["node_binding_id"]]
    result = GraphApplication(service).query("definition.inputs", {"document": doc})
    assert {value["name"] for value in result["inputs"]} == {"message", "unused"}
    assert result["required_for_run"] == ["message"]
    assert result["diagnostics"] == []


def test_invalid_node_does_not_hide_other_plugin_declarations(service):
    source = node(service.registry, "current-input", 1, input_name="message")
    broken = node(service.registry, "current-input", 2, input_name=3)
    result = GraphApplication(service).query("definition.inputs", {"document": document([source, broken], [])})
    assert [value["name"] for value in result["inputs"]] == ["message"]
    assert any(value.get("node_id") == broken["node_binding_id"] for value in result["diagnostics"])


def test_duplicate_names_merge_nodes_without_creating_new_names(service):
    first = node(service.registry, "current-input", 1, input_name="message")
    second = node(service.registry, "current-input", 2, input_name="message")
    result = GraphApplication(service).query("definition.inputs", {"document": document([first, second], [])})
    assert len(result["inputs"]) == 1
    assert result["inputs"][0]["node_ids"] == [first["node_binding_id"], second["node_binding_id"]]


def test_consumer_cannot_read_draft_input_declarations(service):
    with pytest.raises(ContractValidationError) as failure:
        GraphApplication(service, scope="consumer").query("definition.inputs", {"document": {}})
    assert failure.value.reason_code == "application_scope_denied"


def test_consumer_incomplete_graph_keeps_text_declaration_and_real_blocking_reason(service, monkeypatch):
    source = node(service.registry, "current-input", 1, input_name="message")
    missing = node(service.registry, "output", 2)
    doc = document([source, missing], [])
    doc["control_edges"] = [{
        "edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
        "target_node_id": missing["node_binding_id"],
    }]
    session = create(service, doc)
    from phase1_agent.graph_contracts import GraphCompiler
    original_compile, calls = GraphCompiler.compile, []

    def compile_once(compiler, value):
        calls.append(value["workflow_definition_id"])
        return original_compile(compiler, value)

    monkeypatch.setattr(GraphCompiler, "compile", compile_once)
    consumer = service.get_consumer(session["workflow_session_id"])
    assert consumer["inputs"] == [{"name": "message", "data_type": "TEXT", "required": True,
                                  "node_ids": [source["node_binding_id"]]}]
    assert not consumer["can_submit"]
    assert consumer["diagnostics"][0]["reason_code"] == "graph_required_input_missing"
    assert calls == [doc["workflow_definition_id"]]
