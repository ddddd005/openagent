"""Independent workflow operations work without frontend nodes or model calls."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.workflow_operations import WorkflowOperations

from test_graph_service import document, edge, node


def _save(service, *, gate=None, external=False):
    source = (node(service.registry, "tools.current-input", 51001, input_name="text")
              if external else node(service.registry, "tools.text", 51001, text="ordinary workflow"))
    output = node(service.registry, "tools.output", 51003)
    nodes, edges = [source, output], [edge(source, output, 51001)]
    if gate is not None:
        service.registry.register(NodeDefinition(
            "test.workflow-operation-gate", "1", "Gate", "Test", {}, {"type": "object"},
            inputs=(NodePort("input", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), gate)
        middle = node(service.registry, "test.workflow-operation-gate", 51002)
        nodes = [source, middle, output]
        edges = [edge(source, middle, 51001), edge(middle, output, 51002)]
    graph = document(nodes, edges)
    graph["schema_version"] = 3
    graph["package_lock"] = list(service.registry.execution_package_lock)
    for entry in nodes:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))
    assert saved["outcome"] == "saved"
    assert all(not item["component_id"].startswith("frontend.") for item in nodes)
    return saved["document"]


def _start(operations, graph, *, view=None, key=None, inputs=None):
    return operations.start(
        workflow_definition_id=graph["workflow_definition_id"],
        definition_revision=graph["revision"],
        session_id=None if view is None else view["workflow_session_id"],
        expected_session_revision=None if view is None else view["revision"],
        idempotency_key=key or str(uuid4()),
        **({"inputs": inputs} if inputs is not None else {}))


def _finish(service, result):
    assert result["outcome"] == "succeeded", result
    started = result["start_result"]
    service.wait(started["active_chain_run_id"])
    return service.get_session(started["workflow_session_id"])


def _fork_parameters(view, checkpoint_id, **changes):
    return {
        "session_id": view["workflow_session_id"],
        "checkpoint_id": checkpoint_id,
        "expected_session_revision": view["revision"],
        "expected_data_revision": view["data_revision"],
        "expected_head_revision": view["head_revision"],
        "idempotency_key": str(uuid4()),
        **changes,
    }


def test_ordinary_graph_start_checkpoint_fork_and_continue_without_frontend_node(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "ordinary-operations.sqlite")) as service:
        graph = _save(service)
        operations = WorkflowOperations(service)
        first = _start(operations, graph, key="first")
        source = _finish(service, first)
        source = _finish(service, _start(operations, graph, view=source))
        assert source["status"] == "succeeded"
        checkpoints = operations.list_checkpoints(source["workflow_session_id"])
        assert len(checkpoints) == 2
        selected = operations.read_checkpoint(
            source["workflow_session_id"], checkpoints[0]["checkpoint_id"])
        assert selected["checkpoint_id"] == checkpoints[0]["checkpoint_id"]
        assert selected["parent_checkpoint_id"] is None
        before_source = deepcopy(source)
        parameters = _fork_parameters(source, selected["checkpoint_id"])
        forked = operations.fork_checkpoint(**parameters)
        assert forked["outcome"] == "succeeded", forked
        target_id = forked["fork_result"]["target_session_id"]
        target = service.get_session(target_id)
        assert target["active_chain_run_id"] is None and target["chains"] == []
        assert len(operations.list_checkpoints(target_id)) == 1
        target = _finish(service, _start(operations, graph, view=target))
        assert target["status"] == "succeeded"
        assert len(operations.list_checkpoints(target_id)) == 2
        assert service.get_session(source["workflow_session_id"]) == before_source
        state = operations.read_session_state(target_id)
        assert state["workflow_session_id"] == target_id
        assert state["status"] == "succeeded"
        assert "nodes" not in state and "outputs" not in state

        monkeypatch.setattr(
            service, "fork_checkpoint", lambda *args, **kwargs: pytest.fail("fork replay dispatched"))
        assert operations.fork_checkpoint(**parameters) == forked
        assert len(service.list_sessions(graph["workflow_definition_id"])) == 2


def test_current_input_runs_through_independent_start_and_preserves_exact_original_inputs(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "ordinary-inputs.sqlite")) as service:
        graph = _save(service, external=True)
        operations = WorkflowOperations(service)
        inputs = {"text": "caller-owned input"}
        started = _start(operations, graph, key="external-first", inputs=inputs)
        inputs["text"] = "later edit"
        source = _finish(service, started)
        assert source["status"] == "succeeded"
        run = service.get_run(
            source["workflow_session_id"], started["start_result"]["active_chain_run_id"])
        assert run["chain"]["inputs"]["text"] == "caller-owned input"
        assert len(run["outputs"]) == 2
        assert all(item["payload"]["text"] == "caller-owned input" for item in run["outputs"])
        assert len(operations.list_checkpoints(source["workflow_session_id"])) == 1

        monkeypatch.setattr(service, "start", lambda *args, **kwargs: pytest.fail("start replay dispatched"))
        assert _start(
            operations, graph, key="external-first", inputs={"text": "caller-owned input"}) == started
        with pytest.raises(ContractValidationError) as failure:
            _start(operations, graph, key="external-first", inputs={"text": "different request"})
        assert failure.value.reason_code == "idempotency_conflict"
        assert len(service.list_sessions(graph["workflow_definition_id"])) == 1


@pytest.mark.parametrize("field", [
    "expected_session_revision", "expected_data_revision", "expected_head_revision",
])
def test_independent_fork_requires_all_native_cas_values(tmp_path, field):
    with closing(GraphWorkflowService(tmp_path / ("cas-" + field + ".sqlite"))) as service:
        graph = _save(service)
        operations = WorkflowOperations(service)
        source = _finish(service, _start(operations, graph))
        checkpoint_id = operations.list_checkpoints(source["workflow_session_id"])[0]["checkpoint_id"]
        before = deepcopy(service.list_sessions(graph["workflow_definition_id"]))
        parameters = _fork_parameters(source, checkpoint_id)
        parameters[field] += 1
        with pytest.raises(ContractValidationError) as failure:
            operations.fork_checkpoint(**parameters)
        assert failure.value.reason_code in ("stale_revision", "stale_data_revision", "stale_head_revision")
        assert service.list_sessions(graph["workflow_definition_id"]) == before


def test_independent_fork_does_not_accept_another_sessions_checkpoint(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "wrong-checkpoint-owner.sqlite")) as service:
        graph = _save(service)
        operations = WorkflowOperations(service)
        first = _finish(service, _start(operations, graph))
        second = _finish(service, _start(operations, graph))
        other_checkpoint = operations.list_checkpoints(second["workflow_session_id"])[0]["checkpoint_id"]
        before = deepcopy(service.list_sessions(graph["workflow_definition_id"]))
        result = operations.fork_checkpoint(**_fork_parameters(first, other_checkpoint))
        assert result["outcome"] == "failed" and result["fork_result"] is None
        assert service.list_sessions(graph["workflow_definition_id"]) == before


@pytest.mark.parametrize("action", ["resume", "close"])
def test_independent_pause_then_control_uses_the_existing_completed_boundary(tmp_path, action):
    entered, release = Event(), Event()
    calls = []

    def gate(config, inputs, context):
        calls.append(context.node_run_id)
        entered.set()
        assert release.wait(10), "offline operation gate timed out"
        return {"output": inputs["input"]}

    with closing(GraphWorkflowService(tmp_path / ("control-" + action + ".sqlite"))) as service:
        graph = _save(service, gate=gate)
        operations = WorkflowOperations(service)
        started = _start(operations, graph)
        assert started["outcome"] == "succeeded", started
        session_id = started["start_result"]["workflow_session_id"]
        chain_id = started["start_result"]["active_chain_run_id"]
        try:
            assert entered.wait(5)
            running = service.get_session(session_id)
            paused = operations.control(
                session_id=session_id, action="pause", expected_session_revision=running["revision"],
                idempotency_key="pause")
            assert paused["outcome"] == "succeeded", paused
        finally:
            release.set()
        service.wait(chain_id)
        settled = service.get_session(session_id)
        assert settled["status"] == "paused"
        assert len(calls) == 1 and len(settled["chains"][0]["completed_nodes"]) == 2
        result = operations.control(
            session_id=session_id, action=action, expected_session_revision=settled["revision"],
            idempotency_key=action)
        assert result["outcome"] == "succeeded", result
        service.wait(chain_id)
        final = service.get_session(session_id)
        assert len(calls) == 1
        if action == "resume":
            assert final["status"] == "succeeded"
            assert len(operations.list_checkpoints(session_id)) == 1
        else:
            assert final["active_chain_run_id"] is None
            assert final["chains"][0]["status"] == "closed"
            assert operations.list_checkpoints(session_id) == []
