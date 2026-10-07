"""Shared current graph fixtures without retired workflow node implementations."""

from copy import deepcopy
from threading import Event
from uuid import UUID, uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from graph_test_plugin import register_content_probe


def uid(number):
    return str(UUID(int=number, version=4))


def node(registry, type_name, number, **config):
    component = type_name if "." in type_name else "tools." + type_name
    entry = registry.get(component, "1")
    return {
        "node_binding_id": uid(number), "component_id": component, "component_version": "1",
        "title": type_name, "position": {"x": number * 100, "y": 0},
        "config": {**deepcopy(entry.definition.default_config), **config} if entry else config,
    }


def edge(source, target, number, source_port="output", target_port="input", order=0):
    return {
        "edge_id": uid(1000 + number), "source_node_id": source["node_binding_id"],
        "source_port_id": source_port, "target_node_id": target["node_binding_id"],
        "target_port_id": target_port, "order": order,
    }


def document(nodes, edges):
    return {
        "schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Graph integration", "nodes": nodes, "edges": edges, "object_bindings": [],
    }


@pytest.fixture
def service(tmp_path):
    registry = create_package_registry().registry.detached()
    register_content_probe(registry)
    instance = GraphWorkflowService(tmp_path / "graph.sqlite", registry=registry)
    try:
        yield instance
    finally:
        instance.close()


def create(service, doc):
    service.save_definition(doc, expected_revision=0, idempotency_key=str(uuid4()))
    return service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def run(service, session, key=None):
    started = service.start(
        session["workflow_session_id"], expected_revision=session["revision"],
        idempotency_key=key or str(uuid4()),
    )
    service.wait(started["active_chain_run_id"])
    return service.get_session(session["workflow_session_id"])


def copy_current(service, view, doc, **options):
    return service.copy_session(
        view["workflow_session_id"], document=doc,
        expected_session_revision=view["revision"], expected_data_revision=view["data_revision"],
        expected_definition_revision=view["definition_revision"], expected_head_revision=view["head_revision"],
        idempotency_key=str(uuid4()), **options,
    )


def text_graph(registry):
    text = node(registry, "tools.text", 1, text="apple apple")
    regex = node(registry, "tools.regex", 2, pattern="apple", replacement="pear")
    output = node(registry, "tools.output", 3)
    output["public_outputs"] = ["output"]
    return {
        **document([text, regex, output], [edge(text, regex, 1), edge(regex, output, 2)]),
        "package_lock": list(registry.execution_package_lock),
    }


def gate_graph(service):
    entered, release = Event(), Event()

    def execute(config, inputs, context):
        entered.set()
        assert release.wait(10), "test gate timed out"
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.gate", "1", "Gate", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), execute)
    first = node(service.registry, "tools.text", 1)
    gate, output = node(service.registry, "test.gate", 2), node(service.registry, "tools.output", 4)
    value = document([first, gate, output], [edge(first, gate, 1), edge(gate, output, 2)])
    initial = create(service, value)
    started = service.start(
        initial["workflow_session_id"], expected_revision=initial["revision"], idempotency_key="gate-start",
    )
    assert entered.wait(5)
    return initial["workflow_session_id"], started["active_chain_run_id"], release
