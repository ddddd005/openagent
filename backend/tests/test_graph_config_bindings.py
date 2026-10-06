"""Business node bindings validate identity without introducing execution edges."""

from copy import deepcopy

import pytest

from phase1_agent.graph_contracts import GraphCompiler, GraphDiagnosticError, NodeDefinition, NodePort
from phase1_agent.graph_nodes import create_default_registry
from test_graph_execution import document, edge, identity, node


def binding_graph(*, agent_is_target=False):
    registry = create_default_registry()
    registry.register(NodeDefinition(
        "test.bound-context", "1", "Bound context", "Test",
        {"agent_node_id": identity(2)}, {"type": "object"},
        outputs=(NodePort("output", "TEXT"),), is_output=not agent_is_target,
        config_node_references=({
            "config_field": "agent_node_id", "multiple": False,
            "target_types": [{"component_id": "test.bound-agent", "component_version": "2"}],
        },),
    ), lambda config, inputs, context: {})
    registry.register(NodeDefinition(
        "test.bound-agent", "2", "Bound Agent", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT"),), is_output=agent_is_target,
    ), lambda config, inputs, context: {})
    reader = node(1, "test.bound-context", registry=registry)
    agent = {**node(2, "test.bound-agent"), "component_version": "2"}
    value = document([reader, agent], [edge(20, 1, 2)] if agent_is_target else [])
    return registry, value


def test_binding_does_not_activate_an_unconsumed_agent():
    registry, value = binding_graph()
    plan = GraphCompiler(registry).compile(value)
    assert plan.ordered_node_ids == (identity(1),)


def test_binding_to_a_downstream_agent_does_not_form_a_cycle():
    registry, value = binding_graph(agent_is_target=True)
    plan = GraphCompiler(registry).compile(value)
    assert plan.ordered_node_ids == (identity(1), identity(2))
    assert value["edges"] == [edge(20, 1, 2)]


@pytest.mark.parametrize("change, reason", [
    ("missing", "graph_missing_config_reference"),
    ("version", "graph_config_reference_type_mismatch"),
    ("component", "graph_config_reference_type_mismatch"),
])
def test_binding_rejects_an_absent_or_wrong_agent_declaration(change, reason):
    registry, original = binding_graph()
    value = deepcopy(original)
    if change == "missing":
        value["nodes"][0]["config"]["agent_node_id"] = identity(99)
    elif change == "version":
        value["nodes"][1]["component_version"] = "1"
    else:
        value["nodes"][1]["component_id"] = "workflow.text"
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(registry).compile(value)
    assert caught.value.reason_code == reason
    assert caught.value.diagnostics[0]["node_id"] == identity(1)
