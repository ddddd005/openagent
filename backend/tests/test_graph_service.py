"""Shared current graph fixtures without retired workflow node implementations."""

from copy import deepcopy
from threading import Event
from uuid import UUID, uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.content_contracts import text_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from graph_test_plugin import register_content_probe


_CURRENT_NODE_VERSIONS = {
    "agents.compaction-policy": "2",
    "agents.execute": "10",
    "context.assembly": "5",
    "context.merge": "6",
    "context.output": "5",
    "frontend.node": "2",
    "lorebook.global-activate": "1",
    "lorebook.global-reference": "1",
    "models.chat": "5",
    "models.source": "7",
    "prompts.assembly": "2",
    "prompts.global-reference": "2",
    "prompts.global-resolve": "2",
    "prompts.group": "2",
    "prompts.item": "3",
    "prompts.source": "2",
    "prompts.summary": "2",
    "prompts.tool": "2",
    "prompts.tool-summary": "2",
    "tools.current-input": "1",
    "tools.editable-input": "1",
    "tools.json-to-prompt": "1",
    "tools.json-to-text": "1",
    "tools.output": "1",
    "tools.prompt-to-json": "1",
    "tools.prompt-to-text": "1",
    "tools.regex": "1",
    "tools.text": "1",
    "tools.text-to-json": "1",
    "tools.text-to-prompt": "1",
    "tools.variable-assign": "1",
    "tools.variable-register": "2",
    "tools.variable-replace-all": "1",
    "tools.variable-replace-selected": "1",
    "tools.variable-to-content": "1",
}
_TEST_PLUGIN_NAMESPACES = {"lorebook": "1", "sample": "1", "test": "1"}


def uid(number):
    return str(UUID(int=number, version=4))


def node(registry, type_name, number, *, component_version=None, **config):
    component = type_name if "." in type_name else "tools." + type_name
    version = component_version or _CURRENT_NODE_VERSIONS.get(component)
    if version is None:
        namespace = component.partition(".")[0]
        version = _TEST_PLUGIN_NAMESPACES.get(namespace)
    if version is None:
        raise KeyError(f"Node fixture requires an explicit component version: {component}")
    entry = registry.get(component, version)
    return {
        "node_binding_id": uid(number), "component_id": component, "component_version": version,
        "title": type_name, "position": {"x": number * 100, "y": 0},
        "config": {**deepcopy(entry.definition.default_config), **config} if entry else config,
    }


def edge(source, target, number, source_port="output", target_port="input", order=0):
    return {
        "edge_id": uid(1000 + number), "source_node_id": source["node_binding_id"],
        "source_port_id": source_port, "target_node_id": target["node_binding_id"],
        "target_port_id": target_port, "order": order,
    }


def document(nodes, edges, *, object_bindings=()):
    return {
        "schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Graph integration", "nodes": nodes, "edges": edges,
        "object_bindings": deepcopy(list(object_bindings)),
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
    save_definition_fixture(service, doc)
    return service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def save_definition_fixture(service, doc):
    key = str(uuid4())
    request = {"document": deepcopy(doc), "expected_revision": 0}
    return service._change(
        "test.fixture.definition.save", key, request,
        lambda repo: service._save_definition(repo, request["document"], 0))


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
    value["schema_version"] = 3
    for item in value["nodes"]:
        item["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    saved = service.update_definition(
        value, workflow_definition_id=value["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))
    initial = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"], idempotency_key=str(uuid4()))
    started = service.start(
        initial["workflow_session_id"], expected_revision=initial["revision"], idempotency_key="gate-start",
    )
    assert entered.wait(5)
    return initial["workflow_session_id"], started["active_chain_run_id"], release


def test_service_persists_and_executes_creation_stable_plan_across_pause_resume(service):
    calls = []
    entered, release = Event(), Event()

    def independent(config, inputs, context):
        calls.append("independent")
        entered.set()
        assert release.wait(10), "scheduler test gate timed out"
        return {"output": text_content("independent")}

    def source(config, inputs, context):
        calls.append("source")
        return {"output": text_content("source")}

    def consumer(config, inputs, context):
        calls.append("consumer")
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.scheduler-independent", "1", "Independent", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), independent)
    service.registry.register(NodeDefinition(
        "test.scheduler-source", "1", "Source", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), source)
    service.registry.register(NodeDefinition(
        "test.scheduler-consumer", "1", "Consumer", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), consumer)

    consumer_node = node(service.registry, "test.scheduler-consumer", 900)
    independent_node = node(service.registry, "test.scheduler-independent", 100)
    source_node = node(service.registry, "test.scheduler-source", 800)
    output_node = node(service.registry, "tools.output", 50)
    value = document(
        [consumer_node, independent_node, source_node, output_node],
        [edge(source_node, consumer_node, 70), edge(consumer_node, output_node, 71)],
    )
    value["control_edges"] = [{
        "edge_id": uid(1072), "source_node_id": independent_node["node_binding_id"],
        "target_node_id": output_node["node_binding_id"],
    }]
    value["schema_version"] = 3
    for item in value["nodes"]:
        item["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    creation_order = [item["node_binding_id"] for item in value["nodes"]]
    saved = service.update_definition(
        value, workflow_definition_id=value["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key="scheduler-save",
    )
    reread = service.get_definition(saved["workflow_definition_id"])
    assert [item["node_binding_id"] for item in reread["nodes"]] == creation_order

    initial = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"], idempotency_key="scheduler-session",
    )
    started = service.start(
        initial["workflow_session_id"], expected_revision=initial["revision"],
        idempotency_key="scheduler-start",
    )
    chain_id = started["active_chain_run_id"]
    expected = [
        independent_node["node_binding_id"], source_node["node_binding_id"],
        consumer_node["node_binding_id"], output_node["node_binding_id"],
    ]
    try:
        assert entered.wait(5)
        running = service.get_session(initial["workflow_session_id"])
        service.control(
            running["workflow_session_id"], action="pause", expected_revision=running["revision"],
            idempotency_key="scheduler-pause",
        )
    finally:
        release.set()
    service.wait(chain_id)

    paused_run = service.get_run(initial["workflow_session_id"], chain_id)
    assert paused_run["chain"]["status"] == "paused"
    assert paused_run["chain"]["ordered_nodes"] == expected
    assert paused_run["chain"]["completed_nodes"] == [expected[0]]
    assert paused_run["chain"]["next_node_index"] == 1
    assert calls == ["independent"]
    assert [(row["node_binding_id"], row["run_id"]) for row in paused_run["node_runs"]] == list(
        zip(expected, paused_run["chain"]["node_run_ids"])
    )

    paused = service.get_session(initial["workflow_session_id"])
    service.control(
        paused["workflow_session_id"], action="resume", expected_revision=paused["revision"],
        idempotency_key="scheduler-resume",
    )
    service.wait(chain_id)

    completed = service.get_run(initial["workflow_session_id"], chain_id)
    assert completed["chain"]["status"] == "succeeded"
    assert completed["chain"]["ordered_nodes"] == expected
    assert completed["chain"]["completed_nodes"] == expected
    assert [row["node_binding_id"] for row in completed["node_runs"]] == expected
    assert [row["run_id"] for row in completed["node_runs"]] == completed["chain"]["node_run_ids"]
    assert all(row["status"] == "succeeded" for row in completed["node_runs"])
    assert calls == ["independent", "source", "consumer"]
