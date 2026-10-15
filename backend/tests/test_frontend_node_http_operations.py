"""Real private HTTP operations preserve exact owners and original requests."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_service import GraphWorkflowService

from test_frontend_node_http import command, owner, path, serving
from test_frontend_node_runtime import _create, _graph, _save
from test_graph_server import request
from test_graph_service import node


def _receipt(port, binding, operation, parameters, *, accept=False):
    return command(port, binding, "accept_receipt" if accept else "receipt", {
        "operation": operation,
        "original_request": {"owner": binding, "parameters": parameters},
    })


def _controlled_graph(service):
    entered, release = Event(), Event()
    calls = []

    def gate(config, inputs, context):
        calls.append("gate")
        entered.set()
        assert release.wait(10), "offline private HTTP gate timed out"
        return {}

    def tail(config, inputs, context):
        calls.append("tail")
        return {}

    for name, executor in (("gate", gate), ("tail", tail)):
        service.registry.register(NodeDefinition(
            "test.private-http-" + name, "1", name, "Test", {}, {"type": "object"}, is_output=True,
        ), executor)
    graph, _, frontends = _graph(service, bound=True)
    extra = [node(service.registry, "test.private-http-" + name, 51201 + index)
             for index, name in enumerate(("gate", "tail"))]
    for entry in extra:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    graph["nodes"].extend(extra)
    graph["control_edges"] = [{
        "edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
        "target_node_id": target["node_binding_id"],
    } for source, target in zip([frontends[0], extra[0]], extra)]
    graph = _save(service, graph)
    initial = _create(service, graph)
    return graph, owner(graph, frontends[0], initial), initial, entered, release, calls


def _pause_at_boundary(service, port, binding, initial, entered, release):
    status, started = command(port, binding, "start", {
        "expected_session_revision": initial["revision"], "idempotency_key": "http-operation-start",
    })
    assert status == 200 and started["outcome"] == "succeeded", started
    chain_id = started["result"]["start_result"]["active_chain_run_id"]
    try:
        assert entered.wait(5)
        running = command(port, binding, "state")[1]
        status, paused = command(port, binding, "pause", {
            "expected_session_revision": running["run"]["revision"], "idempotency_key": "http-operation-pause",
        })
        assert status == 200 and paused["outcome"] == "succeeded", paused
    finally:
        release.set()
    service.wait(chain_id)
    settled = command(port, binding, "state")[1]
    assert settled["run"]["status"] == "paused"
    assert settled["run"]["can_resume"] and settled["run"]["can_close"]
    return chain_id, settled


@pytest.mark.parametrize("operation", ["resume", "close"])
def test_real_http_control_receipts_do_not_redispatch_after_state_changes(tmp_path, monkeypatch, operation):
    with closing(GraphWorkflowService(tmp_path / ("http-" + operation + ".sqlite"))) as service:
        graph, binding, initial, entered, release, calls = _controlled_graph(service)
        try:
            with serving(service) as (_, port):
                chain_id, settled = _pause_at_boundary(
                    service, port, binding, initial, entered, release)
                parameters = {
                    "expected_session_revision": settled["run"]["revision"],
                    "idempotency_key": "http-operation-" + operation,
                }
                status, result = command(port, binding, operation, parameters)
                assert status == 200 and result["outcome"] == "succeeded", result
                assert result["operation"] == operation and result["result"]["next_owner"] is None
                service.wait(chain_id)
                state = command(port, binding, "state")[1]
                assert state["run"]["status"] == ("succeeded" if operation == "resume" else "closed")
                assert calls == (["gate", "tail"] if operation == "resume" else ["gate"])
                assert len(command(port, binding, "checkpoints")[1]["result"]["items"]) == (
                    1 if operation == "resume" else 0)

                monkeypatch.setattr(
                    service, "control", lambda *args, **kwargs: pytest.fail("receipt redispatched control"))
                for accept in (False, True):
                    assert _receipt(port, binding, operation, parameters, accept=accept) == (200, result)
                assert command(port, binding, operation, parameters) == (200, result)
                wrong_owner = {**binding, "node_binding_id": str(uuid4())}
                status, rejected = request(port, "POST", path(binding, "channel"), {
                    "owner": wrong_owner, "operation": operation, "parameters": parameters,
                })
                assert status == 409 and rejected["error"]["reason_code"] == "frontend_node_owner_mismatch"
                status, rejected = command(port, binding, "receipt", {
                    "operation": operation,
                    "original_request": {"owner": wrong_owner, "parameters": parameters},
                })
                assert status == 409 and rejected["error"]["reason_code"] == "frontend_node_owner_mismatch"
                assert calls == (["gate", "tail"] if operation == "resume" else ["gate"])
        finally:
            release.set()


@pytest.mark.parametrize("lost_response", [False, True])
def test_real_http_checkpoint_reads_fork_and_original_receipt_preserve_source(
        tmp_path, monkeypatch, lost_response):
    with closing(GraphWorkflowService(tmp_path / "http-checkpoint-operations.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2, bound=True)
        graph = _save(service, graph)
        initial = _create(service, graph)
        binding = owner(graph, frontends[0], initial)
        with serving(service) as (_, port):
            started = command(port, binding, "start", {
                "expected_session_revision": initial["revision"], "idempotency_key": "checkpoint-http-start",
            })[1]
            assert started["outcome"] == "succeeded"
            service.wait(started["result"]["start_result"]["active_chain_run_id"])
            source = deepcopy(service.get_session(initial["workflow_session_id"]))
            status, listed = command(port, binding, "checkpoints")
            assert status == 200 and listed["owner"] == binding
            assert len(listed["result"]["items"]) == 1
            checkpoint_id = listed["result"]["items"][0]["checkpoint_id"]
            status, point = command(port, binding, "checkpoint", {"checkpoint_id": checkpoint_id})
            assert status == 200 and point["owner"] == binding
            assert point["result"]["workflow_session_id"] == initial["workflow_session_id"]
            assert "state_storage" not in point["result"]
            basis = listed["result"]["basis"]
            parameters = {
                "checkpoint_id": checkpoint_id, "expected_session_revision": basis["revision"],
                "expected_data_revision": basis["data_revision"], "expected_head_revision": basis["head_revision"],
                "idempotency_key": "checkpoint-http-fork",
            }
            if lost_response:
                def fail_projection(*args, **kwargs):
                    raise RuntimeError("offline fork response lost after commit")

                with monkeypatch.context() as projection:
                    projection.setattr(FrontendNodeChannel, "_fork_projection", fail_projection)
                    status, error = command(port, binding, "fork_checkpoint", parameters)
                    assert status == 500 and error["error"]["reason_code"] == "internal_error"
                    assert "offline fork response" not in str(error)
                    assert len(service.list_sessions(graph["workflow_definition_id"])) == 2
                status, forked = _receipt(port, binding, "fork_checkpoint", parameters)
            else:
                status, forked = command(port, binding, "fork_checkpoint", parameters)
            assert status == 200 and forked["outcome"] == "succeeded", forked
            next_owner = forked["result"]["next_owner"]
            assert next_owner["node_binding_id"] == binding["node_binding_id"]
            assert next_owner["workflow_definition_id"] == binding["workflow_definition_id"]
            assert next_owner["session_id"] != binding["session_id"]
            assert service.get_session(binding["session_id"]) == source
            target = service.get_session(next_owner["session_id"])
            assert target["chains"] == [] and target["active_chain_run_id"] is None
            assert command(port, next_owner, "state")[1]["owner"] == next_owner
            assert len(command(port, next_owner, "checkpoints")[1]["result"]["items"]) == 1
            status, rejected = command(
                port, next_owner, "checkpoint", {"checkpoint_id": checkpoint_id})
            assert status == 403 and rejected["error"]["reason_code"] == "checkpoint_owner_denied"

            monkeypatch.setattr(
                service, "fork_checkpoint", lambda *args, **kwargs: pytest.fail("receipt redispatched fork"))
            assert _receipt(port, binding, "fork_checkpoint", parameters) == (200, forked)
            assert _receipt(port, binding, "fork_checkpoint", parameters, accept=True) == (200, forked)
            assert command(port, binding, "fork_checkpoint", parameters) == (200, forked)
            assert len(service.list_sessions(graph["workflow_definition_id"])) == 2

            other_owner = owner(graph, frontends[1], initial)
            status, rejected = request(port, "POST", path(other_owner, "channel"), {
                "owner": binding, "operation": "fork_checkpoint", "parameters": parameters,
            })
            assert status == 409 and rejected["error"]["reason_code"] == "frontend_node_owner_mismatch"
            status, rejected = command(port, other_owner, "receipt", {
                "operation": "fork_checkpoint",
                "original_request": {"owner": binding, "parameters": parameters},
            })
            assert status == 409 and rejected["error"]["reason_code"] == "frontend_node_owner_mismatch"
            assert service.get_session(binding["session_id"]) == source


@pytest.mark.parametrize("operation", ["resume", "close"])
def test_real_http_unknown_control_keeps_original_request_without_redispatch(tmp_path, monkeypatch, operation):
    with closing(GraphWorkflowService(tmp_path / ("unknown-" + operation + ".sqlite"))) as service:
        _, binding, initial, entered, release, _ = _controlled_graph(service)
        try:
            with serving(service) as (_, port):
                _, settled = _pause_at_boundary(
                    service, port, binding, initial, entered, release)
                calls = []

                def unknown(*args, **kwargs):
                    calls.append(deepcopy((args, kwargs)))
                    raise RuntimeError("offline simulated unknown transport")

                monkeypatch.setattr(service, "control", unknown)
                parameters = {
                    "expected_session_revision": settled["run"]["revision"],
                    "idempotency_key": "unknown-" + operation,
                }
                status, result = command(port, binding, operation, parameters)
                assert status == 200 and result["outcome"] == "unknown"
                assert result["result"]["control_result"] is None
                assert len(calls) == 1
                for _ in range(2):
                    assert command(port, binding, operation, parameters)[1]["outcome"] == "unknown"
                    assert _receipt(port, binding, operation, parameters)[1]["outcome"] == "unknown"
                status, rejected = _receipt(port, binding, operation, parameters, accept=True)
                assert status == 409
                assert rejected["error"]["reason_code"] == "frontend_node_receipt_unresolved"
                assert len(calls) == 1
                state = command(port, binding, "state")[1]
                assert state["run"]["status"] == "paused"
                assert len(state["pending"]) == 1
                assert state["pending"][0]["request"] == {"owner": binding, "parameters": parameters}
        finally:
            release.set()


def test_real_http_unknown_fork_keeps_original_request_without_creating_another_target(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "unknown-fork.sqlite")) as service:
        graph, _, frontends = _graph(service, bound=True)
        graph = _save(service, graph)
        initial = _create(service, graph)
        binding = owner(graph, frontends[0], initial)
        with serving(service) as (_, port):
            started = command(port, binding, "start", {
                "expected_session_revision": initial["revision"], "idempotency_key": "unknown-fork-start",
            })[1]
            assert started["outcome"] == "succeeded"
            service.wait(started["result"]["start_result"]["active_chain_run_id"])
            source = deepcopy(service.get_session(binding["session_id"]))
            listed = command(port, binding, "checkpoints")[1]["result"]
            basis = listed["basis"]
            parameters = {
                "checkpoint_id": listed["items"][0]["checkpoint_id"],
                "expected_session_revision": basis["revision"],
                "expected_data_revision": basis["data_revision"],
                "expected_head_revision": basis["head_revision"],
                "idempotency_key": "unknown-fork",
            }
            calls = []

            def unknown(*args, **kwargs):
                calls.append(deepcopy((args, kwargs)))
                raise RuntimeError("offline simulated unknown fork")

            monkeypatch.setattr(service, "fork_checkpoint", unknown)
            status, result = command(port, binding, "fork_checkpoint", parameters)
            assert status == 200 and result["outcome"] == "unknown"
            assert result["result"]["fork_result"] is None and result["result"]["next_owner"] is None
            for _ in range(2):
                assert command(port, binding, "fork_checkpoint", parameters)[1]["outcome"] == "unknown"
                assert _receipt(port, binding, "fork_checkpoint", parameters)[1]["outcome"] == "unknown"
            status, rejected = _receipt(port, binding, "fork_checkpoint", parameters, accept=True)
            assert status == 409 and rejected["error"]["reason_code"] == "frontend_node_receipt_unresolved"
            assert len(calls) == 1
            assert service.get_session(binding["session_id"]) == source
            assert len(service.list_sessions(graph["workflow_definition_id"])) == 1
            state = command(port, binding, "state")[1]
            assert len(state["pending"]) == 1
            assert state["pending"][0]["request"] == {"owner": binding, "parameters": parameters}
