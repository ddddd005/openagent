"""Current frontend-node chat behavior retained from legacy output tests."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.frontend_node_evidence import FrontendNodeEvidenceAdapter
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow_operations import read_native_receipt

from frontend_node_test_support import create as _create, graph as _graph, save as _save
from test_frontend_node_channel import counts, request


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "debt-model-checkpoint-output.sqlite")) as instance:
        yield instance


def prepared(service):
    graph, messages, frontends = _graph(service, bound=True)
    graph = _save(service, graph)
    view = _create(service, graph)
    owner = {
        "workflow_definition_id": graph["workflow_definition_id"],
        "definition_revision": graph["revision"],
        "node_binding_id": frontends[0]["node_binding_id"],
        "session_id": view["workflow_session_id"],
    }
    channel = FrontendNodeChannel(service, owner)
    started = channel.handle(request(owner, "start", {
        "expected_session_revision": view["revision"],
        "idempotency_key": "debt-output-start",
    }))
    assert started["outcome"] == "succeeded", started
    service.wait(started["result"]["start_result"]["active_chain_run_id"])
    return channel, owner, graph, messages, frontends, view


def run_state(channel, owner):
    state = channel.handle(request(owner, "state"))
    assert state["snapshot"]["owner"] == {
        "workflow_definition_id": owner["workflow_definition_id"],
        "definition_revision": owner["definition_revision"],
        "node_binding_id": owner["node_binding_id"],
        "workflow_session_id": owner["session_id"],
        "object_key": "chat-0",
    }
    return state


def chat_parameters(state, *, key, round_id, candidate_id=None):
    result = {
        "round_id": round_id,
        "expected_session_revision": state["run"]["revision"],
        "expected_data_revision": state["run"]["data_revision"],
        "expected_head_revision": state["run"]["head_revision"],
        "expected_object_revision": state["run"]["object_revision"],
        "idempotency_key": key,
    }
    if candidate_id is not None:
        result["candidate_id"] = candidate_id
    return result


def test_current_candidate_selection_stays_inside_the_bound_session_and_round(service):
    channel, owner, graph, _, _, _ = prepared(service)
    source_state = run_state(channel, owner)
    source_round = source_state["snapshot"]["chat"]["rounds"][0]

    foreign_session = _create(service, graph)
    foreign_owner = {**owner, "session_id": foreign_session["workflow_session_id"]}
    foreign_channel = FrontendNodeChannel(service, foreign_owner)
    foreign_start = foreign_channel.handle(request(foreign_owner, "start", {
        "expected_session_revision": foreign_session["revision"],
        "idempotency_key": "debt-output-foreign-start",
    }))
    assert foreign_start["outcome"] == "succeeded", foreign_start
    service.wait(foreign_start["result"]["start_result"]["active_chain_run_id"])
    foreign_round = run_state(foreign_channel, foreign_owner)["snapshot"]["chat"]["rounds"][0]
    foreign_candidate = foreign_round["candidates"][0]
    assert foreign_round["round_id"] != source_round["round_id"]
    assert foreign_candidate["candidate_id"] != source_round["candidates"][0]["candidate_id"]

    before = service.get_session(owner["session_id"])
    forged = chat_parameters(
        source_state, key="debt-output-foreign-candidate",
        round_id=source_round["round_id"], candidate_id=foreign_candidate["candidate_id"],
    )
    rejected = channel.handle(request(owner, "select_candidate", forged))
    assert rejected["outcome"] == "failed"
    assert rejected["result"]["error"]["reason_code"] == "chat_candidate_not_found"
    assert service.get_session(owner["session_id"]) == before

    bound = channel._bind()
    dispatch_request = {
        "session": owner["session_id"], "object_key": "chat-0",
        **{name: value for name, value in forged.items() if name != "idempotency_key"},
    }
    assert read_native_receipt(
        service, "graph.chat.select",
        bound._child_key("select_candidate", forged["idempotency_key"]),
        dispatch_request,
    ) is None

    selected = source_round["candidates"][0]
    accepted_parameters = chat_parameters(
        run_state(channel, owner), key="debt-output-own-candidate",
        round_id=source_round["round_id"], candidate_id=selected["candidate_id"],
    )
    accepted = channel.handle(request(owner, "select_candidate", accepted_parameters))
    assert accepted["outcome"] == "succeeded", accepted
    current = run_state(channel, owner)
    assert current["snapshot"]["chat"]["rounds"][0]["selected_candidate_id"] == selected["candidate_id"]


def test_current_floor_fork_adopts_real_child_keeps_fixed_definition_and_parent(service, monkeypatch):
    channel, owner, graph, _, _, _ = prepared(service)
    state = run_state(channel, owner)
    source = service.get_session(owner["session_id"])
    round_id = state["snapshot"]["chat"]["rounds"][0]["round_id"]

    changed = deepcopy(graph)
    changed["nodes"][0]["configuration_policy"]["prompt_mode"] = "fixed"
    changed["nodes"][0]["config"]["text"] = "later definition input"
    asked = service.update_definition(
        changed, workflow_definition_id=owner["workflow_definition_id"],
        expected_edit_sequence=1, idempotency_key="debt-output-next-definition",
    )
    assert asked["outcome"] == "confirmation_required"
    derived = service.update_definition(
        changed, workflow_definition_id=owner["workflow_definition_id"],
        expected_edit_sequence=1, idempotency_key="debt-output-confirm-derivation",
        confirm_derivation=True,
    )
    assert derived["outcome"] == "derived"
    assert derived["workflow_definition_id"] != owner["workflow_definition_id"]

    original = chat_parameters(state, key="debt-output-floor-fork", round_id=round_id)
    forked = channel.handle(request(owner, "fork_floor", original))
    assert forked["outcome"] == "succeeded", forked
    result = forked["result"]
    target_id = result["fork_result"]["target_session_id"]
    assert target_id != owner["session_id"]
    assert result["next_owner"] == {**owner, "session_id": target_id}
    target = service.get_session(target_id)
    assert target["workflow_definition_id"] == owner["workflow_definition_id"]
    assert target["definition_revision"] == owner["definition_revision"] == 1
    assert service.get_definition(derived["workflow_definition_id"])["revision"] == 1
    assert service.get_session(owner["session_id"]) == source
    assert len(service.list_sessions(owner["workflow_definition_id"])) == 2

    monkeypatch.setattr(service, "chat_fork_floor", lambda *args, **kwargs: pytest.fail("fork replayed"))
    assert channel.handle(request(owner, "fork_floor", original)) == forked
    assert service.get_session(owner["session_id"]) == source
    assert len(service.list_sessions(owner["workflow_definition_id"])) == 2


def test_unknown_current_floor_fork_resolves_only_from_its_original_receipt(service, monkeypatch):
    channel, owner, _, _, _, _ = prepared(service)
    state = run_state(channel, owner)
    source = service.get_session(owner["session_id"])
    round_id = state["snapshot"]["chat"]["rounds"][0]["round_id"]
    original = chat_parameters(state, key="debt-output-unknown-floor-fork", round_id=round_id)
    native_fork = service.chat_fork_floor
    calls = []

    def unknown(session_id, **arguments):
        dispatch_key = arguments.pop("idempotency_key")
        dispatch_request = {"session": session_id, **deepcopy(arguments)}
        calls.append((dispatch_key, dispatch_request))
        raise OSError("private dispatch diagnostic")

    monkeypatch.setattr(service, "chat_fork_floor", unknown)
    pending = channel.handle(request(owner, "fork_floor", original))
    assert pending["outcome"] == "unknown"
    evidence = run_state(channel, owner)["pending"]
    assert len(evidence) == 1
    assert evidence[0]["request"] == {"owner": owner, "parameters": original}
    assert service.get_session(owner["session_id"]) == source
    assert len(calls) == 1

    dispatch_key, dispatch_request = calls[0]
    dispatch_arguments = deepcopy(dispatch_request)
    dispatch_arguments.pop("session")
    target = native_fork(
        owner["session_id"], **dispatch_arguments, idempotency_key=dispatch_key,
    )
    assert read_native_receipt(
        service, "graph.chat.fork-floor", dispatch_key, dispatch_request,
    ) == target

    monkeypatch.setattr(service, "chat_fork_floor", lambda *args, **kwargs: pytest.fail("fork replayed"))
    accepted = channel.handle(request(owner, "accept_receipt", {
        "operation": "fork_floor", "original_request": original,
    }))
    assert accepted["outcome"] == "succeeded"
    assert accepted["result"]["fork_result"] == target
    assert accepted["result"]["next_owner"] == {
        **owner, "session_id": target["target_session_id"],
    }
    assert channel.handle(request(owner, "fork_floor", original)) == accepted
    assert channel.handle(request(owner, "receipt", {
        "operation": "fork_floor", "original_request": original,
    })) == accepted
    assert service.get_session(owner["session_id"]) == source
    assert len(service.list_sessions(owner["workflow_definition_id"])) == 2
    assert len(calls) == 1


def test_current_floor_fork_does_not_dispatch_when_evidence_write_fails(service, monkeypatch):
    channel, owner, _, _, _, _ = prepared(service)
    state = run_state(channel, owner)
    source = service.get_session(owner["session_id"])
    original = chat_parameters(
        state, key="debt-output-evidence-failure",
        round_id=state["snapshot"]["chat"]["rounds"][0]["round_id"],
    )

    def fault(point):
        if point == "after_request_evidence_write":
            raise OSError("evidence write failed")

    calls = []
    monkeypatch.setattr(
        service, "chat_fork_floor",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    evidence = FrontendNodeEvidenceAdapter(
        service.database,
        store_factory=lambda: SqliteStore(service.database, fault_injector=fault),
    )
    guarded = FrontendNodeChannel(service, owner, evidence_adapter=evidence)
    before = counts(service)
    with pytest.raises(OSError, match="evidence write failed"):
        guarded.handle(request(owner, "fork_floor", original))
    assert calls == []
    assert service.get_session(owner["session_id"]) == source
    assert counts(service) == before
