"""Private node channels expose only their bound input, run and display state."""

from contextlib import closing
from copy import deepcopy
from threading import Event

import pytest

from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_service import GraphWorkflowService

from frontend_node_test_support import create as _create, graph as _graph, save as _save
from test_graph_service import node


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-channel.sqlite")) as instance:
        yield instance


def prepared(service, *, session=False, bound=True, fixed=False):
    graph, messages, frontends = _graph(service, bound=bound)
    if fixed:
        messages[0]["configuration_policy"]["prompt_mode"] = "fixed"
    graph = _save(service, graph)
    view = _create(service, graph) if session else None
    owner = {
        "workflow_definition_id": graph["workflow_definition_id"], "definition_revision": graph["revision"],
        "node_binding_id": frontends[0]["node_binding_id"],
        "session_id": view["workflow_session_id"] if view else None,
    }
    return FrontendNodeChannel(service, owner), owner, graph, messages, view


def request(owner, operation, parameters=None):
    parameters = deepcopy(parameters or {})
    if operation in ("receipt", "accept_receipt") and "original_request" in parameters:
        original = parameters["original_request"]
        if "owner" not in original:
            parameters["original_request"] = {"owner": deepcopy(owner), "parameters": original}
    return {"owner": deepcopy(owner), "operation": operation, "parameters": parameters}


def counts(service):
    with closing(service._store()) as store:
        return {
            table: store._connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("records", "workflow_current_configuration", "idempotency", "pending_commands")
        }


def test_initial_state_reads_current_input_without_creating_a_session(service):
    channel, owner, graph, messages, _ = prepared(service)
    changed = service.patch_node_configuration(
        workflow_definition_id=graph["workflow_definition_id"],
        node_binding_id=messages[0]["node_binding_id"], component_id="tools.editable-input", component_version="1",
        changes={"text": "  current\r\n"}, expected_edit_sequence=1, idempotency_key="outside-input-edit",
    )
    assert changed["definition_revision"] == graph["revision"]
    assert service.get_definition(graph["workflow_definition_id"], 1)["nodes"][0]["config"]["text"] != "  current\r\n"
    before = counts(service)
    state = channel.handle(request(owner, "state"))
    assert state == {
        "schema_version": 1, "kind": "workflow.frontend-node-state", "owner": owner,
        "input": {"text": "  current\r\n", "edit_sequence": 2, "policy": "adjustable"},
        "run": None, "snapshot": None, "pending": [],
    }
    assert counts(service) == before


def test_state_has_no_input_when_unbound_and_real_snapshot_when_session_exists(service):
    channel, owner, _, _, view = prepared(service, session=True, bound=False)
    state = channel.handle(request(owner, "state"))
    assert state["input"] is None
    assert state["run"] == {
        "revision": view["revision"], "status": "idle", "can_start": True, "can_pause": False,
        "can_resume": False, "can_close": False, "can_fork": True,
        "data_revision": view["data_revision"], "head_revision": view["head_revision"],
        "object_revision": view["objects"]["chat-0"]["revision"],
    }
    assert state["snapshot"]["owner"]["workflow_session_id"] == view["workflow_session_id"]
    assert state["snapshot"]["chat"] == {"rounds": []}
    assert set(state) == {"schema_version", "kind", "owner", "input", "run", "snapshot", "pending"}


@pytest.mark.parametrize("mutation", [
    lambda packet: packet.update(context={"messages": []}),
    lambda packet: packet.update(operation="session.objects.read"),
    lambda packet: packet.update(parameters={"object_key": "other"}),
    lambda packet: packet["owner"].update(session_id="not-a-session"),
    lambda packet: packet["owner"].update(definition_revision=True),
    lambda packet: packet["owner"].update(component_id="frontend.node"),
])
def test_channel_rejects_unknown_fields_selectors_and_context_overrides(service, mutation):
    channel, owner, _, _, _ = prepared(service)
    packet = request(owner, "state")
    mutation(packet)
    before = counts(service)
    with pytest.raises(ContractValidationError):
        channel.handle(packet)
    assert counts(service) == before


def test_channel_rejects_another_valid_frontend_owner(service):
    channel, owner, _, _, _ = prepared(service)
    _, other, _, _, _ = prepared(service)
    with pytest.raises(ContractValidationError) as caught:
        channel.handle(request(other, "state"))
    assert caught.value.reason_code == "frontend_node_owner_mismatch"


@pytest.mark.parametrize("operation", ["receipt", "accept_receipt"])
def test_original_receipt_owner_cannot_name_another_valid_frontend(service, operation):
    channel, owner, _, _, _ = prepared(service)
    _, other, _, _, _ = prepared(service)
    before = counts(service)
    packet = request(owner, operation, {
        "operation": "start", "original_request": {
            "owner": other, "parameters": {"expected_session_revision": None, "idempotency_key": "wrong-original-owner"},
        },
    })
    with pytest.raises(ContractValidationError) as caught:
        channel.handle(packet)
    assert caught.value.reason_code == "frontend_node_owner_mismatch"
    assert counts(service) == before


@pytest.mark.parametrize(("operation", "parameters"), [
    ("patch_input", {"text": "x", "expected_edit_sequence": 1, "idempotency_key": "bad", "object_key": "other"}),
    ("patch_input", {"text": "x", "expected_edit_sequence": True, "idempotency_key": "bad"}),
    ("patch_input", {"text": "x", "expected_edit_sequence": 1, "idempotency_key": "bad", "confirm_derivation": None}),
    ("patch_input", {"text": None, "expected_edit_sequence": 1, "idempotency_key": "bad"}),
    ("start", {"expected_session_revision": None, "idempotency_key": "bad", "inputs": {"text": "override"}}),
    ("start", {"expected_session_revision": 1, "idempotency_key": "bad"}),
    ("start", {"idempotency_key": "bad"}),
    ("pause", {"expected_session_revision": 1, "idempotency_key": "bad"}),
])
def test_invalid_command_fields_are_rejected_before_evidence_or_dispatch(service, operation, parameters):
    channel, owner, _, _, _ = prepared(service)
    before = counts(service)
    with pytest.raises(ContractValidationError):
        channel.handle(request(owner, operation, parameters))
    assert counts(service) == before


def test_patch_projection_returns_only_own_text_and_original_receipt(service, monkeypatch):
    channel, owner, graph, messages, _ = prepared(service, session=True)
    parameters = {"text": "  patched\r\n", "expected_edit_sequence": 1, "idempotency_key": "channel-patch"}
    result = channel.handle(request(owner, "patch_input", parameters))
    assert result == {
        "schema_version": 1, "kind": "workflow.frontend-node-result", "owner": owner,
        "operation": "patch_input", "idempotency_key": "channel-patch", "outcome": "saved",
        "result": {"outcome": "saved", "edit_sequence": 2, "definition_revision": 1,
                   "text": "  patched\r\n", "next_owner": None},
    }
    assert channel.handle(request(owner, "state"))["pending"] == []
    monkeypatch.setattr(service, "_document", lambda *args: pytest.fail("receipt read looked up latest graph"))
    assert channel.handle(request(owner, "receipt", {
        "operation": "patch_input", "original_request": parameters,
    })) == result
    assert channel.handle(request(owner, "patch_input", parameters)) == result


def test_fixed_without_session_returns_exact_advanced_revision_owner(service):
    channel, owner, _, _, _ = prepared(service, fixed=True)
    result = channel.handle(request(owner, "patch_input", {
        "text": "new fixed", "expected_edit_sequence": 1, "idempotency_key": "fixed-next-revision",
    }))
    next_owner = {**owner, "definition_revision": 2}
    assert result["outcome"] == "saved" and result["result"]["next_owner"] == next_owner
    with pytest.raises(ContractValidationError) as caught:
        channel.handle(request(owner, "state"))
    assert caught.value.reason_code == "stale_revision"
    next_channel = FrontendNodeChannel(service, next_owner)
    assert next_channel.handle(request(next_owner, "state"))["input"] == {
        "text": "new fixed", "edit_sequence": 2, "policy": "fixed",
    }


def test_fixed_confirmation_and_derivation_preserve_original_owner(service):
    channel, owner, graph, _, view = prepared(service, session=True, fixed=True)
    original = {"text": "derived text", "expected_edit_sequence": 1, "idempotency_key": "fixed-ask"}
    asked = channel.handle(request(owner, "patch_input", original))
    assert asked["outcome"] == "confirmation_required"
    assert asked["result"]["next_owner"] is None
    assert service.get_definition(graph["workflow_definition_id"])["nodes"][0]["config"]["text"] != "derived text"
    confirmed = channel.handle(request(owner, "patch_input", {
        **original, "idempotency_key": "fixed-confirm", "confirm_derivation": True,
    }))
    target = confirmed["result"]["next_owner"]
    assert confirmed["outcome"] == "derived"
    assert target["workflow_definition_id"] != owner["workflow_definition_id"]
    assert target["definition_revision"] == 1 and target["session_id"] is None
    assert target["node_binding_id"] == owner["node_binding_id"]
    assert confirmed["owner"] == owner
    assert service.get_session(view["workflow_session_id"]) == view


def test_start_projects_both_native_phases_and_only_returns_next_owner(service):
    channel, owner, _, _, _ = prepared(service)
    parameters = {"expected_session_revision": None, "idempotency_key": "channel-start"}
    result = channel.handle(request(owner, "start", parameters))
    assert result["owner"] == owner and result["outcome"] == "succeeded"
    phases = result["result"]
    assert set(phases) == {"outcome", "create_result", "start_result", "failure_phase", "error", "next_owner"}
    for name in ("create_result", "start_result"):
        assert set(phases[name]) == {
            "workflow_definition_id", "definition_revision", "workflow_session_id",
            "revision", "status", "active_chain_run_id",
        }
    sid = phases["create_result"]["workflow_session_id"]
    assert phases["start_result"]["workflow_session_id"] == sid
    assert phases["next_owner"] == {**owner, "session_id": sid}
    service.wait(phases["start_result"]["active_chain_run_id"])
    assert channel.handle(request(owner, "receipt", {
        "operation": "start", "original_request": parameters,
    })) == result
    target_channel = FrontendNodeChannel(service, phases["next_owner"])
    state = target_channel.handle(request(phases["next_owner"], "state"))
    assert state["run"]["status"] == "succeeded"
    assert state["snapshot"]["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] == "message 0"


def test_partial_create_keeps_real_session_and_failed_start_diagnostic(service, monkeypatch):
    channel, owner, _, _, _ = prepared(service)

    def fail_start(*args, **kwargs):
        raise DiagnosticError("test_start_rejected", "Start rejected", status=409)

    monkeypatch.setattr(service, "start", fail_start)
    result = channel.handle(request(owner, "start", {
        "expected_session_revision": None, "idempotency_key": "channel-partial",
    }))
    assert result["outcome"] == "failed" and result["result"]["failure_phase"] == "start"
    sid = result["result"]["create_result"]["workflow_session_id"]
    assert result["result"]["start_result"] is None
    assert result["result"]["next_owner"] == {**owner, "session_id": sid}
    assert result["result"]["error"]["reason_code"] == "test_start_rejected"


def test_missing_receipt_returns_unknown_and_never_dispatches(service, monkeypatch):
    channel, owner, _, _, _ = prepared(service)
    monkeypatch.setattr(service, "create_session", lambda *args, **kwargs: pytest.fail("receipt created session"))
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: pytest.fail("receipt started run"))
    before = counts(service)
    result = channel.handle(request(owner, "receipt", {
        "operation": "start",
        "original_request": {"expected_session_revision": None, "idempotency_key": "never-dispatched"},
    }))
    assert result["outcome"] == "unknown"
    assert result["result"]["create_result"] is None and result["result"]["start_result"] is None
    assert result["result"]["next_owner"] is None
    assert counts(service) == before


def test_unknown_original_request_is_exposed_as_exact_pending_evidence(service, monkeypatch):
    channel, owner, _, _, _ = prepared(service)
    parameters = {"expected_session_revision": None, "idempotency_key": "unknown-channel-create"}

    def interrupted(*args, **kwargs):
        raise RuntimeError("unknown create result")

    monkeypatch.setattr(service, "create_session", interrupted)
    outcome = channel.handle(request(owner, "start", parameters))
    assert outcome["outcome"] == "unknown"
    before = counts(service)
    state = channel.handle(request(owner, "state"))
    assert len(state["pending"]) == 1
    pending = state["pending"][0]
    assert pending["operation"] == "frontend-node.start" and pending["idempotency_key"] == parameters["idempotency_key"]
    assert pending["request"] == {"owner": owner, "parameters": parameters}
    assert counts(service) == before
    checked = channel.handle(request(owner, "receipt", {"operation": "start", "original_request": parameters}))
    assert checked["outcome"] == "unknown"
    with pytest.raises(ContractValidationError) as caught:
        channel.handle(request(owner, "accept_receipt", {"operation": "start", "original_request": parameters}))
    assert caught.value.reason_code == "frontend_node_receipt_unresolved"
    assert channel.handle(request(owner, "state"))["pending"] == [pending]


def test_accept_verified_receipt_only_closes_evidence_and_does_not_resubmit(service, monkeypatch):
    channel, owner, _, _, _ = prepared(service)
    parameters = {"text": "accepted input", "expected_edit_sequence": 1, "idempotency_key": "channel-accept"}
    original = channel.handle(request(owner, "patch_input", parameters))
    monkeypatch.setattr(channel._bound.__class__, "patch_input", lambda *args, **kwargs: pytest.fail("accept re-dispatched"))
    packet = request(owner, "accept_receipt", {"operation": "patch_input", "original_request": parameters})
    assert channel.handle(packet) == original
    assert channel.handle(packet) == original


def test_pause_state_distinguishes_request_acceptance_from_safe_point(service):
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "channel pause gate timed out"
        return {}

    service.registry.register(NodeDefinition(
        "test.channel-gate", "1", "Gate", "Test", {}, {"type": "object"}, is_output=True,
    ), gate)
    graph, _, frontends = _graph(service, bound=True)
    downstream = node(service.registry, "test.channel-gate", 19601)
    downstream["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    graph["nodes"].append(downstream)
    graph["control_edges"] = [{
        "edge_id": "d9f57787-59dd-4e84-82b8-4b8f5e463849",
        "source_node_id": frontends[0]["node_binding_id"], "target_node_id": downstream["node_binding_id"],
    }]
    graph = _save(service, graph)
    view = _create(service, graph)
    owner = {
        "workflow_definition_id": graph["workflow_definition_id"], "definition_revision": 1,
        "node_binding_id": frontends[0]["node_binding_id"], "session_id": view["workflow_session_id"],
    }
    channel = FrontendNodeChannel(service, owner)
    started = service.start(view["workflow_session_id"], expected_revision=view["revision"], idempotency_key="pause-gate")
    try:
        assert entered.wait(5)
        active = channel.handle(request(owner, "state"))
        assert active["run"]["status"] == "running" and active["run"]["can_pause"]
        result = channel.handle(request(owner, "pause", {
            "expected_session_revision": active["run"]["revision"], "idempotency_key": "channel-pause",
        }))
        assert result["outcome"] == "succeeded"
        assert result["result"]["control_result"]["status"] == "running"
        pausing = channel.handle(request(owner, "state"))
        assert pausing["run"]["status"] == "pausing" and not pausing["run"]["can_pause"]
    finally:
        release.set()
        service.wait(started["active_chain_run_id"])
