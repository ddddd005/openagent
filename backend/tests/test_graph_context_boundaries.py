"""History bindings use the selected baseline; current results require data edges."""

from threading import Event

import pytest

from phase1_agent.graph_contracts import NodeDefinition
from test_graph_agent_runtime import final
from test_graph_agent_service import graph, harness
from test_graph_service import create, edge, node, run


def context_branch(service, *, output_number=8):
    doc = graph(service)
    context = node(service.registry, "context", 7, source_node_id=doc["nodes"][3]["node_binding_id"])
    output = node(service.registry, "output", output_number, mode="prompt")
    doc["nodes"].extend([context, output])
    doc["edges"].append(edge(context, output, 99))
    return doc


@pytest.mark.parametrize("output_number", [0, 8])
def test_history_binding_does_not_consume_same_run_agent_result(harness, output_number):
    service, scripts, requests, _ = harness
    scripts.extend([[final("first result")], [final("second result")]])
    doc = context_branch(service, output_number=output_number)
    initial = create(service, doc)
    first = run(service, initial)
    assert first["nodes"][-1]["outputs"]["output"]["items"] == []
    second = run(service, first)
    history = second["nodes"][-1]["outputs"]["output"]
    assert "first result" in str(history)
    assert "second result" not in str(history)
    evidence = service.get_run(second["workflow_session_id"], second["selected_chain_run_id"])
    context_run = next(item for item in evidence["node_runs"] if item["node_binding_id"] == doc["nodes"][-2]["node_binding_id"])
    first_turn = first["private_states"][doc["nodes"][3]["node_binding_id"]]["head_turn_id"]
    assert context_run["reads"][0]["turn_ids"] == [first_turn]
    assert len(requests) == 2


@pytest.mark.parametrize("older_freeze", [False, True])
def test_paused_context_keeps_null_baseline_after_agent_has_completed(harness, monkeypatch, older_freeze):
    service, scripts, requests, _ = harness
    scripts.append([final("current result")])
    doc = context_branch(service, output_number=9)
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10)
        return {}

    service.registry.register(NodeDefinition("test.history-gate", "1", "Gate", "Test", {},
        {"type": "object"}, is_output=True), gate)
    doc["nodes"].insert(-2, node(service.registry, "test.history-gate", 8))
    if older_freeze:
        original = service._preflight_capabilities

        def old_preflight(repo, sid, plan):
            frozen = original(repo, sid, plan)
            frozen.pop("history_heads")
            return frozen

        monkeypatch.setattr(service, "_preflight_capabilities", old_preflight)
    initial = create(service, doc)
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"], idempotency_key="start")
    try:
        assert entered.wait(10)
        current = service.get_session(initial["workflow_session_id"])
        assert current["private_states"][doc["nodes"][3]["node_binding_id"]]["head_turn_id"] is not None
        service.control(current["workflow_session_id"], action="pause", expected_revision=current["revision"], idempotency_key="pause")
    finally:
        release.set()
        service.wait(started["active_chain_run_id"])
    paused = service.get_session(initial["workflow_session_id"])
    assert paused["status"] == "paused"
    service.control(paused["workflow_session_id"], action="resume", expected_revision=paused["revision"], idempotency_key="resume")
    service.wait(started["active_chain_run_id"])
    completed = service.get_session(initial["workflow_session_id"])
    assert completed["status"] == "succeeded"
    assert completed["nodes"][-1]["outputs"]["output"]["items"] == []
    assert len(requests) == 1


def test_explicit_context_delta_edge_can_consume_current_agent_result(harness):
    service, scripts, requests, _ = harness
    scripts.append([final("current result")])
    doc = graph(service)
    output = node(service.registry, "output", 8, mode="prompt")
    doc["nodes"].append(output)
    doc["edges"].append(edge(doc["nodes"][3], output, 99, source_port="context_delta"))
    completed = run(service, create(service, doc))
    assert completed["status"] == "succeeded"
    assert "current result" in str(completed["nodes"][-1]["outputs"]["output"])
    assert len(requests) == 1
