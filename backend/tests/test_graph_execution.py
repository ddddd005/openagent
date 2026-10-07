"""Serial graph coverage through current independent content packages."""

import copy

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.content_contracts import prompt_content
from phase1_agent.graph_contracts import (
    GraphCompiler, GraphDiagnosticError, NodeDefinition, NodePort, validate_graph_document,
)
from phase1_agent.graph_execution import default_private_states, execute_graph
from graph_test_plugin import PROBE_COMPONENT, register_content_probe


def _registry():
    return create_package_registry().registry.detached()


def identity(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def node(number, component="tools.text", config=None, registry=None):
    registry = registry or _registry()
    entry = registry.get(component, "1")
    default = copy.deepcopy(entry.definition.default_config) if entry else {}
    return {
        "node_binding_id": identity(number), "component_id": component, "component_version": "1",
        "title": component, "position": {"x": 0, "y": 0},
        "config": {**default, **copy.deepcopy(config or {})},
    }


def edge(number, source, target, source_port="output", target_port="input", order=0):
    return {
        "edge_id": identity(number), "source_node_id": identity(source), "source_port_id": source_port,
        "target_node_id": identity(target), "target_port_id": target_port, "order": order,
    }


def document(nodes, edges=()):
    return {
        "schema_version": 2, "workflow_definition_id": identity(900), "revision": 1,
        "name": "Graph", "nodes": nodes, "edges": list(edges), "object_bindings": [],
    }


def run_graph(value, registry=None, **kwargs):
    registry = registry or _registry()
    plan = GraphCompiler(registry).compile(value)
    return execute_graph(
        plan, registry, workflow_session_id=identity(901), chain_run_id=identity(902),
        node_run_ids={node_id: identity(1000 + index) for index, node_id in enumerate(plan.ordered_node_ids)},
        state=kwargs.pop("state", {"revision": 0, "values": {}}), **kwargs,
    )


def material(number, text, *, purpose="prompt", protected=False, role="system", source=None, metadata=None):
    return {
        "item_instance_id": identity(number), "text": text, "role": role, "placement": "before",
        "depth": None, "order": 0, "enabled": True, "purpose": purpose,
        "source": source or {"kind": "fixture", "revision": 3}, "protected": protected,
        "metadata": metadata or {"floor_id": identity(800), "format": "text"},
    }


def prompt_node(number, items):
    return node(number, "prompts.source", {"value": prompt_content(items)})


def test_zero_agent_text_regex_output_ignores_unreachable_unknown_nodes():
    config = {"mode": "text", "pattern": "old", "replacement": "new", "flags": [], "replaceMode": "all"}
    value = document([
        node(1, config={"text": "old text"}), node(2, "tools.regex", config),
        node(3, "tools.output"), node(4, "missing.plugin", {"anything": [1, 2]}),
    ], [edge(20, 1, 2), edge(21, 2, 3)])
    result = run_graph(value)
    assert result.status == "succeeded"
    assert result.outputs[identity(3)]["output"]["text"] == "new text"
    assert set(result.outputs) == {identity(1), identity(2), identity(3)}


def test_no_output_graph_saves_but_cannot_start():
    value = document([node(1, "missing.plugin")])
    assert validate_graph_document(value) == value
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(_registry()).compile(value)
    assert caught.value.reason_code == "graph_no_outputs"


def test_unreachable_unknown_writer_does_not_change_state_or_private_defaults():
    value = document([
        node(1, config={"text": "ok"}), node(2, "tools.output"),
        node(3, "missing.writer", {"value": "unused"}),
    ], [edge(10, 1, 2)])
    result = run_graph(value)
    assert result.state == {"revision": 0, "values": {}}
    assert identity(3) not in result.outputs


def test_every_output_must_validate_before_any_executor_runs():
    registry = _registry()
    register_content_probe(registry)
    value = document([
        node(1, config={"text": "value"}), node(2, PROBE_COMPONENT, registry=registry),
        node(3, "tools.output"), node(4, "tools.output"),
    ], [edge(10, 1, 2), edge(11, 2, 3, source_port="left")])
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(registry).compile(value)
    assert caught.value.reason_code == "graph_required_input_missing"
    assert caught.value.diagnostics[0]["node_id"] == identity(4)


@pytest.mark.parametrize("mode", ["text", "prompt"])
def test_independent_plugin_preserves_same_type_output_identity_and_executes_once(mode):
    registry = _registry()
    register_content_probe(registry)
    source = node(1, config={"text": "value"}) if mode == "text" else prompt_node(1, [material(80, "value")])
    value = document([
        source, node(2, PROBE_COMPONENT, {"mode": mode}, registry),
        node(3, "tools.output", {"mode": mode}), node(4, "tools.output", {"mode": mode}),
    ], [edge(10, 1, 2), edge(11, 2, 3, "left"), edge(12, 2, 4, "right")])
    result = run_graph(value, registry)
    assert result.status == "succeeded"
    left, right = result.outputs[identity(3)]["output"], result.outputs[identity(4)]["output"]
    actual = (left["text"], right["text"]) if mode == "text" else (
        left["items"][0]["text"], right["items"][0]["text"],
    )
    assert actual == ("left:value", "right:value")
    assert result.private_states == {identity(2): {"count": 1}}
    assert sum(run["node_binding_id"] == identity(2) for run in result.node_runs) == 1


def test_named_multi_input_order_overrides_node_and_edge_array_order():
    registry = _registry()
    input_order = []

    def collect(config, inputs, context):
        input_order.extend(item["text"] for value in inputs["input"] for item in value["items"])
        return {"output": prompt_content([
            item for value in inputs["input"] for item in value["items"]
        ])}

    registry.register(NodeDefinition(
        "test.input-order", "1", "Ordered collector", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "PROMPT", multiple=True, data_schema_version=2),),
        outputs=(NodePort("output", "PROMPT", data_schema_version=2),),
    ), collect)
    value = document([
        prompt_node(1, [material(81, "first")]), prompt_node(2, [material(82, "second")]),
        node(3, "test.input-order", registry=registry), node(4, "tools.output", {"mode": "prompt"}),
    ], [edge(12, 3, 4), edge(10, 1, 3, order=7), edge(11, 2, 3, order=2)])
    result = run_graph(value, registry)
    assert input_order == ["second", "first"]
    assert [item["text"] for item in result.outputs[identity(4)]["output"]["items"]] == ["second", "first"]


@pytest.mark.parametrize("modification,code", [
    (lambda value: value["edges"][0].update(source_port_id="not-real"), "graph_unknown_port"),
    (lambda value: value["nodes"][0].update(component_id="missing.type"), "graph_missing_node_type"),
    (lambda value: value["nodes"][0]["config"].update(unknown=True), "graph_invalid_config"),
    (lambda value: value["edges"].append(edge(12, 1, 2)), "graph_input_cardinality"),
])
def test_invalid_reachable_graph_reports_precise_location(modification, code):
    value = document([node(1), node(2, "tools.output")], [edge(10, 1, 2)])
    modification(value)
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(_registry()).compile(value)
    assert caught.value.reason_code == code
    assert caught.value.diagnostics[0]["dependent_outputs"] == [identity(2)]


def test_cycle_and_multi_input_order_conflict_are_static_failures():
    value = document([node(1, "tools.regex"), node(2, "tools.regex"), node(3, "tools.output")],
                     [edge(10, 1, 2), edge(11, 2, 1), edge(12, 2, 3)])
    with pytest.raises(GraphDiagnosticError, match="cycle"):
        GraphCompiler(_registry()).compile(value)
    value = document([
        prompt_node(1, [material(80, "one")]), prompt_node(2, [material(81, "two")]),
        node(3, "prompts.summary"), node(4, "tools.output", {"mode": "prompt"}),
    ], [edge(10, 1, 3), edge(11, 2, 3), edge(12, 3, 4)])
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(_registry()).compile(value)
    assert caught.value.reason_code == "graph_input_order_conflict"


def test_type_mismatch_is_not_silently_converted():
    value = document([prompt_node(1, [material(80, "text")]), node(2, "tools.output")], [edge(10, 1, 2)])
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(_registry()).compile(value)
    assert caught.value.reason_code == "graph_type_mismatch"


def test_failure_stops_unrelated_branch_and_keeps_prior_results():
    registry = _registry()

    def broken(config, inputs, context):
        context.private_write({"changed": True})
        raise ValueError("expected node failure")

    registry.register(NodeDefinition(
        "test.failure", "1", "Failure", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        capabilities=("private:write",), private_state_schema={"type": "object"},
    ), broken)
    value = document([
        node(1, config={"text": "kept"}), node(2, "test.failure", registry=registry),
        node(3, "tools.output"), node(4, config={"text": "unrelated"}), node(5, "tools.output"),
    ], [edge(10, 1, 2), edge(11, 2, 3), edge(12, 4, 5)])
    result = run_graph(value, registry)
    assert result.status == "failed"
    assert identity(2) not in result.private_states
    assert set(result.outputs) == {identity(1)}
    assert result.node_runs[-1]["event"] == "failed"
    assert identity(4) not in result.outputs


def test_lifecycle_callbacks_advance_revision_before_next_node():
    observed = []

    def persist(event):
        observed.append((event["event"], event["node_binding_id"], event["state"]["revision"]))
        if event["event"] == "succeeded":
            event["state"]["revision"] += 1

    value = document([node(1), node(2, "tools.output")], [edge(10, 1, 2)])
    result = run_graph(value, on_node=persist)
    assert [event[0] for event in observed] == ["running", "succeeded", "running", "succeeded"]
    assert [event[2] for event in observed] == [0, 0, 1, 1]
    assert result.state["revision"] == 2


def test_pause_and_resume_boundary_does_not_repeat_plugin_or_source():
    registry = _registry()
    register_content_probe(registry)
    value = document([node(1), node(2, PROBE_COMPONENT, registry=registry), node(3, "tools.output")],
                     [edge(10, 1, 2), edge(11, 2, 3, "left")])
    finished = []
    result = run_graph(
        value, registry, should_pause=lambda: len(finished) >= 2,
        on_node=lambda event: finished.append(event) if event["event"] == "succeeded" else None,
    )
    assert result.status == "paused" and result.next_node_index == 2
    resumed = run_graph(
        value, registry, start_index=result.next_node_index, outputs=result.outputs,
        node_runs=result.node_runs, private_states=result.private_states, state=result.state,
    )
    assert resumed.status == "succeeded"
    assert resumed.private_states[identity(2)]["count"] == 1
    assert len(resumed.node_runs) == 3


def test_invalid_resume_cannot_claim_an_unexecuted_boundary():
    value = document([node(1), node(2, "tools.output")], [edge(10, 1, 2)])
    with pytest.raises(GraphDiagnosticError) as caught:
        run_graph(value, start_index=1)
    assert caught.value.reason_code == "graph_invalid_resume"


def test_missing_external_input_is_rejected_before_any_node_side_effect():
    registry = _registry()
    value = document([node(1, "tools.current-input"), node(2, "tools.output")], [edge(10, 1, 2)])
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(registry).compile(value, external_inputs={})
    assert caught.value.reason_code == "graph_external_input_missing"
    assert GraphCompiler(registry).compile(value, external_inputs={"text": ""}).ordered_node_ids


def test_node_with_no_output_ports_can_be_declared_as_an_execution_target():
    registry = _registry()
    registry.register(NodeDefinition(
        "test.side-effect", "1", "Write", "Test", {}, {"type": "object"},
        is_output=True, capabilities=("private:write",), private_state_schema={"type": "object"},
    ), lambda config, inputs, context: (context.private_write({"done": True}) or {}))
    result = run_graph(document([node(1, "test.side-effect", registry=registry)]), registry)
    assert result.status == "succeeded" and result.outputs[identity(1)] == {}
    assert result.private_states[identity(1)] == {"done": True}


def test_private_defaults_do_not_call_executors_and_copy_requires_explicit_migration():
    registry = _registry()
    register_content_probe(registry)
    definition = registry.get(PROBE_COMPONENT, "1").definition
    assert default_private_states(document([node(1, PROBE_COMPONENT, registry=registry)]), registry) == {
        identity(1): {"count": 0},
    }
    copied = registry.migrate_private_state({"count": 4}, definition, definition)
    copied["count"] = 8
    assert registry.migrate_private_state({"count": 4}, definition, definition) == {"count": 4}
    target = registry.get("tools.text", "1").definition
    with pytest.raises(GraphDiagnosticError):
        registry.migrate_private_state({"count": 4}, definition, target)
    assert registry.migrate_private_state({"count": 4}, definition, target, policy="reset") == {}


def test_private_state_access_is_scoped_and_requires_declared_capability():
    registry = _registry()
    registry.register(NodeDefinition(
        "test.no-capability", "1", "Denied", "Test", {}, {"type": "object"}, is_output=True,
    ), lambda config, inputs, context: (context.private_read() or {}))
    result = run_graph(document([node(1, "test.no-capability", registry=registry)]), registry)
    assert result.status == "failed" and result.diagnostic["code"] == "graph_capability_denied"
