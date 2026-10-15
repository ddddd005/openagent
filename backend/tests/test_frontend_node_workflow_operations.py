"""Private consumers reuse workflow controls and checkpoint operations."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_service import GraphWorkflowService

from test_frontend_node_channel import prepared, request
from test_frontend_node_runtime import _create, _graph, _save
from test_graph_application_receipt_custody import raw_database
from test_graph_service import node


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-workflow-operations.sqlite")) as instance:
        yield instance


def complete_round(service, channel, owner, *, key):
    current = service.get_session(owner["session_id"])
    started = channel.handle(request(owner, "start", {
        "expected_session_revision": current["revision"], "idempotency_key": key,
    }))
    assert started["outcome"] == "succeeded"
    service.wait(started["result"]["start_result"]["active_chain_run_id"])
    current = service.get_session(owner["session_id"])
    assert current["status"] == "succeeded", current["chains"]
    return current


def fork_arguments(session, checkpoint_id, *, key="private-fork"):
    return {
        "checkpoint_id": checkpoint_id,
        "expected_session_revision": session["revision"],
        "expected_data_revision": session["data_revision"],
        "expected_head_revision": session["head_revision"],
        "idempotency_key": key,
    }


def test_real_checkpoint_reads_and_selected_fork_keep_exact_node_and_session_ownership(service):
    channel, owner, _, _, _ = prepared(service, session=True)
    source = complete_round(service, channel, owner, key="first-round")
    complete_round(service, channel, owner, key="second-round")
    source = service.get_session(owner["session_id"])
    before = raw_database(service.database)
    listing = channel.handle(request(owner, "checkpoints"))
    assert listing == {
        "schema_version": 1, "kind": "workflow.frontend-node-checkpoints", "owner": owner,
        "result": {
            "items": service.list_checkpoints(owner["session_id"]),
            "basis": {key: source[key] for key in ("revision", "data_revision", "head_revision")},
        },
    }
    assert len(listing["result"]["items"]) == 2
    selected = listing["result"]["items"][0]["checkpoint_id"]
    detail = channel.handle(request(owner, "checkpoint", {"checkpoint_id": selected}))
    assert detail == {
        "schema_version": 1, "kind": "workflow.frontend-node-checkpoint", "owner": owner,
        "result": service.read_checkpoint(owner["session_id"], checkpoint_id=selected),
    }
    assert set(detail["result"]) == {
        "schema_version", "checkpoint_id", "workflow_definition_id", "workflow_session_id",
        "fixed_definition_revision", "parent_checkpoint_id",
    }
    assert raw_database(service.database) == before

    arguments = fork_arguments(source, selected)
    forked = channel.handle(request(owner, "fork_checkpoint", arguments))
    assert forked["owner"] == owner and forked["outcome"] == "succeeded"
    result = forked["result"]
    assert set(result) == {"outcome", "fork_result", "failure_phase", "error", "next_owner"}
    assert result["failure_phase"] is None and result["error"] is None
    assert set(result["fork_result"]) == {
        "target_session_id", "target_checkpoint_id", "target_commit_id", "target_workflow_ref_id",
    }
    target_owner = result["next_owner"]
    assert target_owner == {
        **owner, "session_id": result["fork_result"]["target_session_id"],
    }
    assert target_owner["session_id"] != owner["session_id"]
    target = FrontendNodeChannel(service, target_owner)
    target_listing = target.handle(request(target_owner, "checkpoints"))["result"]["items"]
    assert target_listing == [{
        "checkpoint_id": result["fork_result"]["target_checkpoint_id"], "parent_checkpoint_id": None,
    }]
    target_state = target.handle(request(target_owner, "state"))
    assert target_state["snapshot"]["owner"]["workflow_session_id"] == target_owner["session_id"]
    assert target_state["snapshot"]["owner"]["node_binding_id"] == owner["node_binding_id"]
    assert service.get_session(owner["session_id"]) == source
    assert channel.handle(request(owner, "receipt", {
        "operation": "fork_checkpoint", "original_request": arguments,
    })) == forked
    with pytest.raises(ContractValidationError) as caught:
        target.handle(request(target_owner, "receipt", {
            "operation": "fork_checkpoint",
            "original_request": {"owner": owner, "parameters": arguments},
        }))
    assert caught.value.reason_code == "frontend_node_owner_mismatch"


def test_fork_original_receipt_survives_cold_service_without_dispatch(tmp_path, monkeypatch):
    database = tmp_path / "cold-private-fork.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        channel, owner, _, _, _ = prepared(service, session=True)
        source = complete_round(service, channel, owner, key="cold-source-round")
        selected = channel.handle(request(owner, "checkpoints"))["result"]["items"][0]["checkpoint_id"]
        arguments = fork_arguments(source, selected, key="cold-private-fork")
        original = channel.handle(request(owner, "fork_checkpoint", arguments))
        assert original["outcome"] == "succeeded"

    with closing(GraphWorkflowService(database)) as service:
        channel = FrontendNodeChannel(service, owner)

        def forbidden(*args, **kwargs):
            pytest.fail("Original checkpoint receipt observation dispatched workflow work")

        for name in ("fork_checkpoint", "create_session", "start", "control"):
            monkeypatch.setattr(service, name, forbidden)
        before = raw_database(service.database)
        assert channel.handle(request(owner, "receipt", {
            "operation": "fork_checkpoint", "original_request": arguments,
        })) == original
        assert channel.handle(request(owner, "fork_checkpoint", arguments)) == original
        assert raw_database(service.database) == before


def test_missing_fork_receipt_stays_unknown_without_dispatch(service, monkeypatch):
    channel, owner, _, _, _ = prepared(service, session=True)
    source = complete_round(service, channel, owner, key="unseen-receipt-source")
    selected = channel.handle(request(owner, "checkpoints"))["result"]["items"][0]["checkpoint_id"]
    arguments = fork_arguments(source, selected, key="never-dispatched-fork")
    monkeypatch.setattr(
        service, "fork_checkpoint",
        lambda *args, **kwargs: pytest.fail("Missing original receipt dispatched a fork"),
    )
    before = raw_database(service.database)
    result = channel.handle(request(owner, "receipt", {
        "operation": "fork_checkpoint", "original_request": arguments,
    }))
    assert result["outcome"] == "unknown"
    assert result["result"] == {
        "outcome": "unknown", "fork_result": None, "failure_phase": "fork_checkpoint",
        "error": None, "next_owner": None,
    }
    assert raw_database(service.database) == before


def test_foreign_checkpoint_is_denied_for_reads_and_fork_without_new_session(service):
    channel, owner, _, _, _ = prepared(service, session=True)
    source = complete_round(service, channel, owner, key="source-for-foreign-point")
    other, other_owner, _, _, _ = prepared(service, session=True)
    complete_round(service, other, other_owner, key="other-point")
    foreign = other.handle(request(other_owner, "checkpoints"))["result"]["items"][0]["checkpoint_id"]
    before = raw_database(service.database)
    with pytest.raises(ContractValidationError) as caught:
        channel.handle(request(owner, "checkpoint", {"checkpoint_id": foreign}))
    assert caught.value.reason_code == "checkpoint_owner_denied"
    assert raw_database(service.database) == before

    result = channel.handle(request(
        owner, "fork_checkpoint", fork_arguments(source, foreign, key="foreign-point-fork")))
    assert result["outcome"] == "failed"
    assert result["result"]["error"]["reason_code"] == "checkpoint_owner_denied"
    assert result["result"]["fork_result"] is None and result["result"]["next_owner"] is None
    assert service.get_session(owner["session_id"]) == source
    assert len(service.list_sessions(owner["workflow_definition_id"])) == 1
    assert len(service.list_sessions(other_owner["workflow_definition_id"])) == 1


@pytest.mark.parametrize(("field", "reason"), [
    ("expected_session_revision", "stale_revision"),
    ("expected_data_revision", "stale_data_revision"),
    ("expected_head_revision", "stale_head_revision"),
])
def test_fork_requires_all_three_original_revisions(service, field, reason):
    channel, owner, _, _, _ = prepared(service, session=True)
    source = complete_round(service, channel, owner, key="revision-source")
    selected = channel.handle(request(owner, "checkpoints"))["result"]["items"][0]["checkpoint_id"]
    arguments = fork_arguments(source, selected, key="wrong-" + field)
    arguments[field] += 1
    with pytest.raises(ContractValidationError) as caught:
        channel.handle(request(owner, "fork_checkpoint", arguments))
    assert caught.value.reason_code == reason
    assert service.get_session(owner["session_id"]) == source
    assert len(service.list_sessions(owner["workflow_definition_id"])) == 1


@pytest.mark.parametrize(("operation", "parameters"), [
    ("checkpoints", {}),
    ("checkpoint", {"checkpoint_id": str(uuid4())}),
    ("resume", {"expected_session_revision": 1, "idempotency_key": "no-session-resume"}),
    ("close", {"expected_session_revision": 1, "idempotency_key": "no-session-close"}),
    ("fork_checkpoint", {
        "checkpoint_id": str(uuid4()), "expected_session_revision": 1,
        "expected_data_revision": 0, "expected_head_revision": 1, "idempotency_key": "no-session-fork",
    }),
])
def test_session_operations_without_a_session_do_not_create_or_dispatch(service, operation, parameters):
    channel, owner, _, _, _ = prepared(service)
    before = raw_database(service.database)
    with pytest.raises(ContractValidationError) as caught:
        channel.handle(request(owner, operation, parameters))
    assert caught.value.reason_code == "invalid_request"
    assert raw_database(service.database) == before


@pytest.mark.parametrize("operation", ["checkpoints", "checkpoint"])
def test_checkpoint_reads_reject_external_selectors(service, operation):
    channel, owner, _, _, _ = prepared(service, session=True)
    parameters = {"session_id": str(uuid4())}
    if operation == "checkpoint":
        parameters["checkpoint_id"] = str(uuid4())
    before = raw_database(service.database)
    with pytest.raises(ContractValidationError):
        channel.handle(request(owner, operation, parameters))
    assert raw_database(service.database) == before


@pytest.mark.parametrize("action", ["resume", "close"])
def test_private_pause_then_control_reuses_real_runtime_without_replaying_completed_nodes(service, action):
    entered, release = Event(), Event()
    invocations = []

    def gate(config, inputs, context):
        invocations.append(context.node_run_id)
        entered.set()
        assert release.wait(10), "Private workflow control gate timed out"
        return {}

    service.registry.register(NodeDefinition(
        "test.private-control-gate", "1", "Gate", "Test", {}, {"type": "object"},
    ), gate)
    graph, messages, frontends = _graph(service, bound=True)
    gated = node(service.registry, "test.private-control-gate", 19801)
    gated["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    graph["nodes"].append(gated)
    graph["control_edges"] = [{
        "edge_id": str(uuid4()), "source_node_id": messages[0]["node_binding_id"],
        "target_node_id": gated["node_binding_id"],
    }, {
        "edge_id": str(uuid4()), "source_node_id": gated["node_binding_id"],
        "target_node_id": frontends[0]["node_binding_id"],
    }]
    graph = _save(service, graph)
    view = _create(service, graph)
    owner = {
        "workflow_definition_id": graph["workflow_definition_id"], "definition_revision": graph["revision"],
        "node_binding_id": frontends[0]["node_binding_id"], "session_id": view["workflow_session_id"],
    }
    channel = FrontendNodeChannel(service, owner)
    started = channel.handle(request(owner, "start", {
        "expected_session_revision": view["revision"], "idempotency_key": "start-gated-" + action,
    }))
    chain_id = started["result"]["start_result"]["active_chain_run_id"]
    try:
        assert entered.wait(5), service.get_session(owner["session_id"])["chains"]
        active = channel.handle(request(owner, "state"))
        paused_result = channel.handle(request(owner, "pause", {
            "expected_session_revision": active["run"]["revision"],
            "idempotency_key": "pause-before-" + action,
        }))
        assert paused_result["outcome"] == "succeeded"
    finally:
        release.set()
        service.wait(chain_id)
    paused = channel.handle(request(owner, "state"))
    assert paused["run"]["status"] == "paused"
    assert paused["run"]["can_resume"] and paused["run"]["can_close"]
    assert not paused["run"]["can_fork"]
    before_runs = deepcopy(service.get_run(owner["session_id"], chain_id)["node_runs"])
    parameters = {
        "expected_session_revision": paused["run"]["revision"], "idempotency_key": "private-" + action,
    }
    controlled = channel.handle(request(owner, action, parameters))
    assert controlled["outcome"] == "succeeded"
    if action == "resume":
        service.wait(chain_id)
    state = channel.handle(request(owner, "state"))
    assert state["run"]["status"] == ("succeeded" if action == "resume" else "closed")
    assert len(state["snapshot"]["entries"]) == (1 if action == "resume" else 0)
    gate_run = next(item for item in before_runs if item["node_binding_id"] == gated["node_binding_id"])
    assert invocations == [gate_run["run_id"]]
    after_runs = service.get_run(owner["session_id"], chain_id)["node_runs"]
    assert after_runs[:2] == before_runs[:2]
    assert len(channel.handle(request(owner, "checkpoints"))["result"]["items"]) == (
        1 if action == "resume" else 0)
    assert channel.handle(request(owner, "receipt", {
        "operation": action, "original_request": parameters,
    })) == controlled
