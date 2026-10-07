"""Generic producer-reference descriptions expose neither payloads nor arbitrary runs."""

from contextlib import closing
import json
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.content_contracts import json_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import create, document, edge, node, uid


def fixture(tmp_path, query=None, *, capability=True):
    registry = create_package_registry().registry.detached()
    captured = {}
    registry.register(NodeDefinition(
        "sample.producer", "1", "Producer", "Sample", {"note": "private-producer-config"},
        {"type": "object", "properties": {"note": {"type": "string"}},
         "required": ["note"], "additionalProperties": False},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        input_storage="references",
        private_state_schema={"type": "object", "properties": {"secret": {"type": "string"}},
                              "required": ["secret"], "additionalProperties": False},
        private_state_default={"secret": "private-producer-business"}),
        lambda config, inputs, context: {"output": inputs["input"]})

    def read(config, inputs, context):
        captured["context"] = context
        operation = query or (lambda ctx, owner: ctx.host_call(
            "artifacts:read", "describe-input-origin", {"port": "result"}))
        return {"output": json_content(operation(context, service))}

    registry.register(NodeDefinition(
        "sample.origin", "1", "Origin", "Sample", {}, {"type": "object", "additionalProperties": False},
        inputs=(NodePort("result", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "JSON", data_schema_version=2),), is_output=True,
        capabilities=("artifacts:read",) if capability else (), input_storage="references"), read)
    service = GraphWorkflowService(tmp_path / "origin.sqlite", registry=registry)
    source = node(service.registry, "tools.text", 1, text="private-input-body")
    producer = node(service.registry, "sample.producer", 2)
    reader = node(service.registry, "sample.origin", 3)
    graph = document([source, producer, reader], [
        edge(source, producer, 1), edge(producer, reader, 2, target_port="result")])
    return service, graph, captured


def run(service, initial):
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                            idempotency_key=str(uuid4()))
    chain_id = started["active_chain_run_id"]
    service.wait(chain_id)
    return service.get_session(initial["workflow_session_id"]), chain_id


def test_origin_reports_only_frozen_producer_identity_and_accepted_input_references(tmp_path):
    service, graph, captured = fixture(tmp_path)
    with closing(service):
        final, chain_id = run(service, create(service, graph))
        assert final["status"] == "succeeded"
        described = final["nodes"][2]["outputs"]["output"]["value"]
        records = service.get_run(final["workflow_session_id"], chain_id)
        producer = records["node_runs"][1]
        assert set(described) == {"output_id", "producer", "component_id", "component_version", "input_refs"}
        assert described["output_id"] == producer["output_refs"]["output"]
        assert described["producer"] == {
            "workflow_session_id": final["workflow_session_id"], "chain_run_id": chain_id,
            "node_binding_id": uid(2), "node_run_id": producer["run_id"]}
        assert described["component_id"] == "sample.producer" and described["component_version"] == "1"
        assert described["input_refs"] == producer["input_refs"]
        assert "private-input-body" not in json.dumps(described)
        assert "private-producer-config" not in json.dumps(described)
        assert "private-producer-business" not in json.dumps(described)
        with pytest.raises(Exception) as caught:
            service._host_call(captured["context"], "artifacts:read", "describe-input-origin", {"port": "result"})
        assert caught.value.reason_code == "runtime_origin_owner_mismatch"


@pytest.mark.parametrize("change,expected", [
    ("run_selector", "runtime_origin_request_invalid"),
    ("output_selector", "runtime_origin_request_invalid"),
    ("unknown_port", "runtime_input_unbound"),
    ("unbound_artifact", "runtime_input_unbound"),
    ("false_node_owner", "runtime_origin_owner_mismatch"),
    ("foreign_run", "runtime_origin_owner_mismatch"),
])
def test_origin_rejects_self_selected_or_unbound_producers_and_wrong_invocation(tmp_path, change, expected):
    def query(context, service):
        payload = {"port": "result"}
        if change == "run_selector":
            payload["run_id"] = uid(999)
        elif change == "output_selector":
            payload["output_id"] = uid(999)
        elif change == "unknown_port":
            payload["port"] = "not-an-input"
        elif change == "unbound_artifact":
            context._input_refs["result"][0]["output_id"] = uid(999)
        elif change == "false_node_owner":
            context.node_binding_id = uid(2)
        else:
            # An upstream run is real and successful, but it is not this caller.
            from phase1_agent.graph_store import GraphRecordStore
            with closing(service._store()) as store:
                chain = GraphRecordStore(store).get("chain_run", chain_run_id=context.chain_run_id)
            context.node_run_id = chain["node_run_ids"][1]
            context.node_binding_id = uid(2)
        try:
            return context.host_call("artifacts:read", "describe-input-origin", payload)
        finally:
            # Keep event ownership unchanged so the rejection is persisted normally.
            context.node_binding_id = uid(3)

    service, graph, captured = fixture(tmp_path, query)
    with closing(service):
        final, _ = run(service, create(service, graph))
        assert final["status"] == "failed"
        assert final["nodes"][2]["diagnostic"]["code"] == expected


def test_origin_requires_declared_artifact_capability(tmp_path):
    service, graph, _ = fixture(tmp_path, capability=False)
    with closing(service):
        final, _ = run(service, create(service, graph))
        assert final["status"] == "failed"
        assert final["nodes"][2]["diagnostic"]["code"] == "graph_capability_denied"


def test_accepted_artifact_from_another_run_cannot_replace_the_bound_input(tmp_path):
    previous = []

    def query(context, service):
        if previous:
            context._input_refs["result"][0]["output_id"] = previous[0]
        described = context.host_call("artifacts:read", "describe-input-origin", {"port": "result"})
        previous.append(described["output_id"])
        return described

    service, graph, _ = fixture(tmp_path, query)
    with closing(service):
        first, _ = run(service, create(service, graph))
        assert first["status"] == "succeeded"
        second, _ = run(service, first)
        assert second["status"] == "failed"
        assert second["nodes"][2]["diagnostic"]["code"] == "runtime_input_unbound"


def test_origin_uses_source_definition_revision_instead_of_current_registry_version(tmp_path):
    def query(context, service):
        source = service.registry.get("sample.producer", "1").definition
        from dataclasses import replace
        service.registry.register(replace(source, component_version="2"), lambda *args: {})
        return context.host_call("artifacts:read", "describe-input-origin", {"port": "result"})

    service, graph, _ = fixture(tmp_path, query)
    with closing(service):
        final, _ = run(service, create(service, graph))
        assert final["status"] == "succeeded"
        assert final["nodes"][2]["outputs"]["output"]["value"]["component_version"] == "1"
