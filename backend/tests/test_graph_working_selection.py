from copy import deepcopy
from dataclasses import replace

import pytest

from phase1_agent.graph_contracts import GraphCompiler, GraphDiagnosticError, NodeDefinition, NodePort
from test_graph_execution import _registry, document, edge, identity, node, run_graph


def _writer(registry, *, execution_class="working"):
    definition = NodeDefinition(
        "test.selection-writer", "1", "Writer", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        execution_class=execution_class,
    )
    registry.register(definition, lambda config, inputs, context: {})
    return definition


def test_connected_terminal_writer_runs_and_isolated_working_roots_do_not():
    registry = _registry()
    _writer(registry)
    value = document([
        node(4, "test.selection-writer", registry=registry),
        node(8, registry=registry),
        node(1, registry=registry),
        node(2, "tools.output", registry=registry),
    ], [edge(10, 1, 2), edge(11, 1, 4)])

    plan = GraphCompiler(registry).compile(value)
    assert plan.ordered_node_ids == (identity(1), identity(4), identity(2))
    result = run_graph(value, registry)
    assert result.status == "succeeded"
    assert [item["node_binding_id"] for item in result.node_runs] == list(plan.ordered_node_ids)
    assert identity(8) not in result.outputs


def test_non_working_connected_component_is_not_executed():
    registry = _registry()
    calls = []
    registry.register(NodeDefinition(
        "test.non-working", "1", "Non-working", "Test", {}, {"type": "object"},
        execution_class="non_working",
    ), lambda config, inputs, context: calls.append("unexpected") or {})
    value = document([
        node(1, registry=registry), node(2, "tools.output", registry=registry),
        node(3, "test.non-working", registry=registry), node(4, "test.non-working", registry=registry),
    ], [edge(10, 1, 2)])
    value["control_edges"] = [{
        "edge_id": identity(20), "source_node_id": identity(3), "target_node_id": identity(4),
    }]
    result = run_graph(value, registry)
    assert result.status == "succeeded"
    assert set(result.outputs) == {identity(1), identity(2)}
    assert calls == []


@pytest.mark.parametrize("control", [False, True])
def test_non_working_upstream_is_reported_without_running_or_fabricating_input(control):
    registry = _registry()
    _writer(registry)
    source = registry.get_definition("tools.text", "1")
    registry.register(replace(
        source, component_id="test.non-working-source", execution_class="non_working",
    ), lambda config, inputs, context: {})
    value = document([
        node(1, "test.non-working-source", registry=registry), node(2, "tools.output", registry=registry),
        node(3, registry=registry),
    ], [edge(10, 1, 2)] if not control else [edge(10, 3, 2)])
    if control:
        value["control_edges"] = [{
            "edge_id": identity(20), "source_node_id": identity(1), "target_node_id": identity(2),
        }]
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(registry).compile(value)
    assert caught.value.reason_code == "graph_non_working_dependency"
    assert caught.value.diagnostics[0]["node_id"] == identity(1)


def test_event_can_explicitly_target_isolated_non_working_node_without_ordinary_outputs():
    registry = _registry()
    registry.register(NodeDefinition(
        "test.event-only", "1", "Event", "Test", {}, {"type": "object"},
        execution_class="non_working",
    ), lambda config, inputs, context: {})
    value = document([node(1, "test.event-only", registry=registry)])
    value["event_bindings"] = [{
        "event_id": "test.activate", "schema_version": 1, "display_name": "Activate", "audience": "management",
        "target_node_ids": [identity(1)], "payload_schema": {"type": "object"},
    }]
    compiler = GraphCompiler(registry)
    assert compiler.compile_event(value, "test.activate", 1, external_inputs={}).ordered_node_ids == (identity(1),)
    with pytest.raises(GraphDiagnosticError, match="output"):
        compiler.compile(value)


def test_event_declaration_does_not_disable_normal_connected_output_and_frozen_plan_keeps_declaration():
    registry = _registry()
    definition = _writer(registry)
    value = document([
        node(1, registry=registry), node(2, "tools.output", registry=registry),
        node(3, "test.selection-writer", registry=registry),
    ], [edge(10, 1, 2), edge(11, 1, 3)])
    value["event_bindings"] = [{
        "event_id": "test.activate", "schema_version": 1, "display_name": "Activate", "audience": "management",
        "target_node_ids": [identity(3)], "payload_schema": {"type": "object"},
    }]
    frozen = registry.detached(frozen=True)
    plan = GraphCompiler(frozen).compile(value)
    registry.update_declarations(replace(definition, execution_class="non_working"))

    assert identity(3) in plan.ordered_node_ids
    assert plan.definitions[identity(3)].execution_class == "working"
    assert identity(3) in GraphCompiler(frozen).compile(value).ordered_node_ids
    assert identity(3) not in GraphCompiler(registry).compile(value).ordered_node_ids
    changed = deepcopy(plan.definitions[identity(3)].config_schema)
    plan.definitions[identity(3)].config_schema["additionalProperties"] = False
    assert frozen.get_definition("test.selection-writer", "1").config_schema == changed
