"""Temporary-database prompt runs, bindings and current-only global resources."""

from contextlib import closing
from threading import Event
from uuid import uuid4

from phase1_agent.content_contracts import text_content
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.global_resources import GlobalResourceStore
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.prompt_package import PROMPT_RESOURCE_TYPE
from phase1_agent.storage import SqliteStore

from test_prompt_package import (
    graph_document, graph_edge, graph_node, loaded, member, register_sink, uid,
)
from test_graph_service import save_definition_fixture


def registry():
    result = loaded().registry.detached()
    register_sink(result)
    result.register(NodeDefinition(
        "test.text-source", "1", "Current text", "Test", {"text": "question"},
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
         "additionalProperties": False},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),), input_storage="references",
    ), lambda config, inputs, context: {"output": text_content(config["text"])})
    return result


def create(service, document):
    save_definition_fixture(service, document)
    return service.create_session(document["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def execute(service, view):
    started = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                            idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    final = service.get_session(view["workflow_session_id"])
    assert final["status"] == "succeeded", final
    return final


def output_texts(view, output):
    return [item["text"] for item in next(
        node for node in view["nodes"] if node["node_binding_id"] == output["node_binding_id"]
    )["outputs"]["output"]["items"]]


def write_resource(service, reference, text, sequence):
    record = {**reference, "data_schema_version": 2, "update_sequence": sequence,
              "value": {"enabled": True, "members": [member(50, text)]}}
    with closing(SqliteStore(service.database)) as store:
        GlobalResourceStore(store, service.registry.data_types).write(
            record, expected_sequence=sequence - 1, idempotency_key=str(uuid4()))


def test_real_service_assembly_persists_exact_artifacts_and_reopens_without_input_body_copies(tmp_path):
    installed = registry()
    group = graph_node(installed, "prompts.group", 1, members=[member(11, "instructions")])
    source = graph_node(installed, "test.text-source", 2)
    assembly = graph_node(installed, "prompts.assembly", 3)
    output = graph_node(installed, "test.prompt-sink", 4)
    doc = graph_document([group, source, assembly, output], [
        graph_edge(20, group, assembly), graph_edge(21, source, assembly, target_port="current_input"),
        graph_edge(22, assembly, output),
    ])
    database = tmp_path / "prompts.sqlite"
    with closing(GraphWorkflowService(database, registry=installed)) as service:
        final = execute(service, create(service, doc))
        history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
        runs = {run["node_binding_id"]: run for run in history["node_runs"]}
        assembly_run = runs[assembly["node_binding_id"]]
        frozen = next(node for node in final["nodes"]
                      if node["node_binding_id"] == assembly["node_binding_id"])["outputs"]["output"]
        assert frozen["assembly"]["messages"] == [
            {"role": "system", "content": "instructions"}, {"role": "user", "content": "question"}]
        manifest = frozen["assembly"]["manifest"]
        assert manifest["source_output_refs"] == [{
            "edge_id": uid(20), "output_id": runs[group["node_binding_id"]]["output_refs"]["output"], "order": 0}]
        assert manifest["current_input_refs"] == [{
            "edge_id": uid(21), "output_id": runs[source["node_binding_id"]]["output_refs"]["output"], "order": 0}]
        assert assembly_run["input_storage"] == "references" and assembly_run["input_values"] == {}
        assert assembly_run["input_refs"]["input"][0]["output_id"] == manifest["source_output_refs"][0]["output_id"]
        for run in history["node_runs"]:
            if run["node_binding_id"] in (group["node_binding_id"], assembly["node_binding_id"]):
                assert run["input_values"] == {}
    with closing(GraphWorkflowService(database, registry=installed)) as reopened:
        restored = reopened.get_session(final["workflow_session_id"])
        assert restored["nodes"] == final["nodes"]
        assert reopened.get_run(final["workflow_session_id"], final["selected_chain_run_id"]) == history


def test_global_edit_before_resource_use_reads_current_and_keeps_materialized_history(tmp_path):
    installed = registry()
    entered, released = Event(), Event()

    def block(config, inputs, context):
        entered.set()
        assert released.wait(5), "Test resource editing did not release the node"
        return {"output": inputs["input"]}

    installed.register(NodeDefinition(
        "test.block-reference", "1", "Gate", "Test", {}, {"type": "object", "additionalProperties": False},
        inputs=(NodePort("input", "GLOBAL_RESOURCE_REF"),),
        outputs=(NodePort("output", "GLOBAL_RESOURCE_REF"),), capabilities=("resources:read",),
        input_storage="references",
    ), block, resource_input_ports=("input",))
    reference = ResourceIdentity("workspace", PROMPT_RESOURCE_TYPE, uid(60)).to_dict()
    source = graph_node(installed, "prompts.global-reference", 1, reference=reference)
    gate = graph_node(installed, "test.block-reference", 2)
    resolve = graph_node(installed, "prompts.global-resolve", 3)
    output = graph_node(installed, "test.prompt-sink", 4)
    assembly = graph_node(installed, "prompts.assembly", 5)
    doc = graph_document([source, gate, resolve, assembly, output], [
        graph_edge(20, source, gate), graph_edge(21, gate, resolve),
        graph_edge(22, resolve, assembly), graph_edge(23, assembly, output)])
    with closing(GraphWorkflowService(tmp_path / "frozen.sqlite", registry=installed)) as service:
        write_resource(service, reference, "old-frame-body", 1)
        initial = create(service, doc)
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        try:
            assert entered.wait(5)
            write_resource(service, reference, "new-current-body", 2)
        finally:
            released.set()
        service.wait(started["active_chain_run_id"])
        first = service.get_session(initial["workflow_session_id"])
        assert first["status"] == "succeeded", first
        assert output_texts(first, output) == ["new-current-body"]
        history = service.get_run(first["workflow_session_id"], first["selected_chain_run_id"])
        persisted_inputs = canonical_bytes(history["chain"]["inputs"]).decode("utf-8")
        assert "old-frame-body" not in persisted_inputs and "new-current-body" not in persisted_inputs
        reference_run = next(run for run in history["node_runs"]
                             if run["node_binding_id"] == source["node_binding_id"])
        reference_output = next(value for value in history["outputs"]
                                if value["output_id"] == reference_run["output_refs"]["output"])
        assert reference_output["payload"] == {
            "schema_version": 1, "kind": "workflow.global-resource-ref", "reference": reference}
        write_resource(service, reference, "next-current-body", 3)
        assert service.get_run(first["workflow_session_id"], first["selected_chain_run_id"]) == history
        second = execute(service, first)
        assert output_texts(second, output) == ["next-current-body"]
        assert service.get_run(first["workflow_session_id"], first["selected_chain_run_id"]) == history
        assert not service._resource_frames
