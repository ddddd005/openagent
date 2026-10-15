"""Logical-chat adapters authorize one item and preserve original operation custody."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.frontend_node_evidence import (
    FRONTEND_NODE_EVIDENCE_OPERATIONS, FrontendNodeEvidenceAdapter,
    validate_frontend_node_evidence_request,
)
from phase1_agent.frontend_node_operations import FrontendNodeOperations
from phase1_agent.frontend_node_http import FrontendNodeHTTP
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.workflow_operations import read_native_receipt

from test_frontend_node_channel import counts, request
from test_frontend_node_operations import assert_another_thread_can_take_service_lock
from test_frontend_node_runtime import _create, _graph, _save


NATIVE = {
    "reroll": ("chat_reroll", "graph.chat.reroll"),
    "select_candidate": ("chat_select", "graph.chat.select"),
    "fork_floor": ("chat_fork_floor", "graph.chat.fork-floor"),
}
OUTER = {
    "reroll": "graph.frontend-node.reroll",
    "select_candidate": "graph.frontend-node.select-candidate",
    "fork_floor": "graph.frontend-node.fork-floor",
}


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-chat-channel.sqlite")) as instance:
        yield instance


def prepared(service, *, version="2", count=1):
    graph, _, frontends = _graph(service, bound=True, count=count)
    if version == "2":
        for frontend, binding in zip(frontends, graph["object_bindings"]):
            frontend["component_version"] = "2"
            binding["type_id"] = CHAT_CONTEXT_TYPE
    graph = _save(service, graph)
    view = _create(service, graph)
    owner = {
        "workflow_definition_id": graph["workflow_definition_id"], "definition_revision": graph["revision"],
        "node_binding_id": frontends[0]["node_binding_id"], "session_id": view["workflow_session_id"],
    }
    return FrontendNodeChannel(service, owner), owner, graph, frontends, view


def parameters(operation, view, *, key="chat-original"):
    result = {
        "round_id": str(uuid4()), "expected_session_revision": view["revision"],
        "expected_data_revision": view["data_revision"], "expected_head_revision": view["head_revision"],
        "expected_object_revision": view["objects"]["chat-0"]["revision"], "idempotency_key": key,
    }
    if operation == "select_candidate":
        result["candidate_id"] = str(uuid4())
    return result


def native_stub(service, monkeypatch, operation, view):
    method, namespace = NATIVE[operation]
    calls = []
    result = (dict(zip(
        ("target_session_id", "target_checkpoint_id", "target_commit_id", "target_workflow_ref_id"),
        (str(uuid4()) for _ in range(4)),
    )) if operation == "fork_floor" else deepcopy(view))

    def invoke(sid, **arguments):
        assert_another_thread_can_take_service_lock(service)
        key = arguments.pop("idempotency_key")
        native_request = {"session": sid, **deepcopy(arguments)}
        calls.append((key, native_request))
        return service._change(namespace, key, native_request, lambda repo: deepcopy(result))

    monkeypatch.setattr(service, method, invoke)
    return calls, result


def test_logical_chat_state_exposes_exact_item_guards_and_detached_snapshot(service):
    channel, owner, _, _, view = prepared(service)
    before = counts(service)
    state = channel.handle(request(owner, "state"))
    assert state["run"]["revision"] == view["revision"]
    for field in ("data_revision", "head_revision"):
        assert state["run"][field] == view[field]
    assert state["run"]["object_revision"] == view["objects"]["chat-0"]["revision"]
    assert state["snapshot"]["schema_version"] == 2
    assert state["snapshot"]["owner"]["object_key"] == "chat-0"
    assert state["snapshot"]["chat"] == {"rounds": []}
    state["snapshot"]["chat"]["rounds"].append("caller change")
    assert channel.handle(request(owner, "state"))["snapshot"]["chat"] == {"rounds": []}
    assert counts(service) == before


@pytest.mark.parametrize("operation", NATIVE)
def test_private_adapter_dispatches_independent_function_with_only_its_saved_item(
        service, monkeypatch, operation):
    channel, owner, _, _, view = prepared(service, count=2)
    original = parameters(operation, view)
    calls, native = native_stub(service, monkeypatch, operation, view)
    result = channel.handle(request(owner, operation, original))
    assert result["operation"] == operation and result["outcome"] == "succeeded"
    assert result["owner"] == owner
    expected = {"session": owner["session_id"], "object_key": "chat-0", **{
        name: value for name, value in original.items() if name != "idempotency_key"}}
    assert len(calls) == 1 and calls[0][1] == expected
    child_key = calls[0][0]
    assert child_key != original["idempotency_key"]
    assert read_native_receipt(service, NATIVE[operation][1], child_key, expected) == native
    outer_request = {**owner, **{
        name: value for name, value in original.items() if name != "idempotency_key"}}
    assert read_native_receipt(
        service, OUTER[operation], original["idempotency_key"], outer_request) == {
            "dispatch_key": child_key, "dispatch_request": expected}
    if operation == "fork_floor":
        assert result["result"]["fork_result"] == native
        assert result["result"]["next_owner"] == {**owner, "session_id": native["target_session_id"]}
    else:
        assert result["result"]["control_result"]["workflow_session_id"] == owner["session_id"]
        assert result["result"]["next_owner"] is None
    assert channel.handle(request(owner, "state"))["pending"] == []
    monkeypatch.setattr(service, NATIVE[operation][0], lambda *args, **kwargs: pytest.fail("re-dispatched"))
    monkeypatch.setattr(service, "_document", lambda *args: pytest.fail("replay re-read current definition"))
    assert channel.handle(request(owner, operation, original)) == result
    assert channel.handle(request(owner, "receipt", {
        "operation": operation, "original_request": original,
    })) == result
    assert channel.handle(request(owner, "accept_receipt", {
        "operation": operation, "original_request": original,
    })) == result


@pytest.mark.parametrize("operation", NATIVE)
def test_old_display_contract_cannot_call_logical_chat_operations(service, monkeypatch, operation):
    channel, owner, _, _, view = prepared(service, version="1")
    monkeypatch.setattr(service, NATIVE[operation][0], lambda *args, **kwargs: pytest.fail("old node dispatched"))
    before = counts(service)
    with pytest.raises(ContractValidationError) as rejected:
        channel.handle(request(owner, operation, parameters(operation, view)))
    assert rejected.value.reason_code == "frontend_node_chat_unavailable"
    after = counts(service)
    assert after["records"] == before["records"]
    assert after["workflow_current_configuration"] == before["workflow_current_configuration"]
    assert channel.handle(request(owner, "state"))["pending"] == []
    with closing(service._store()) as store:
        assert store.read_receipt(OUTER[operation], "chat-original") is None
        assert store.read_receipt(
            NATIVE[operation][1], channel._bound._child_key(operation, "chat-original")) is None


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(object_key="chat-1"),
    lambda value: value.update(node_binding_id=str(uuid4())),
    lambda value: value.update(session_id=str(uuid4())),
    lambda value: value.pop("expected_object_revision"),
    lambda value: value.update(expected_object_revision=True),
    lambda value: value.update(expected_object_revision=0),
    lambda value: value.update(expected_data_revision=-1),
    lambda value: value.update(expected_head_revision=0),
    lambda value: value.update(expected_session_revision=None),
    lambda value: value.update(round_id="other"),
    lambda value: value.update(candidate_id="other"),
    lambda value: value.update(idempotency_key=" "),
])
def test_new_commands_reject_foreign_selectors_and_invalid_guards_before_custody(service, mutation):
    channel, owner, _, _, view = prepared(service)
    original = parameters("select_candidate", view)
    mutation(original)
    before = counts(service)
    with pytest.raises(ContractValidationError):
        channel.handle(request(owner, "select_candidate", original))
    assert counts(service) == before
    with pytest.raises(ContractValidationError):
        validate_frontend_node_evidence_request(
            FRONTEND_NODE_EVIDENCE_OPERATIONS["select_candidate"],
            original["idempotency_key"], {"owner": owner, "parameters": original})


@pytest.mark.parametrize("operation", NATIVE)
def test_direct_bound_chat_adapter_requires_all_fields_and_forbids_item_selector(service, operation):
    channel, _, _, _, view = prepared(service)
    original = parameters(operation, view)
    before = counts(service)
    incomplete = {key: value for key, value in original.items() if key != "expected_object_revision"}
    for arguments in (incomplete, {**original, "object_key": "chat-1"}):
        with pytest.raises(ContractValidationError) as rejected:
            getattr(channel._bound, operation)(**arguments)
        assert rejected.value.reason_code == "invalid_request"
    assert counts(service) == before


@pytest.mark.parametrize("operation", NATIVE)
def test_unknown_chat_dispatch_remains_pending_and_is_never_retried(service, monkeypatch, operation):
    channel, owner, _, _, view = prepared(service)
    original = parameters(operation, view)
    calls = []

    def unknown(*args, **kwargs):
        calls.append(deepcopy(kwargs))
        raise OSError("private transport diagnostic")

    monkeypatch.setattr(service, NATIVE[operation][0], unknown)
    result = channel.handle(request(owner, operation, original))
    assert result["outcome"] == "unknown"
    assert result["result"]["error"] == {"reason_code": "workflow_operation_dispatch_unknown"}
    pending = channel.handle(request(owner, "state"))["pending"]
    assert len(pending) == 1 and pending[0]["request"] == {"owner": owner, "parameters": original}
    observed = channel.handle(request(owner, operation, original))
    assert observed["outcome"] == "unknown" and observed["result"]["error"] is None
    assert len(calls) == 1
    with pytest.raises(ContractValidationError) as unresolved:
        channel.handle(request(owner, "accept_receipt", {
            "operation": operation, "original_request": original}))
    assert unresolved.value.reason_code == "frontend_node_receipt_unresolved"
    assert channel.handle(request(owner, "state"))["pending"] == pending and len(calls) == 1


def test_delayed_original_native_receipt_can_resolve_without_a_second_dispatch(service, monkeypatch):
    channel, owner, _, _, view = prepared(service)
    operation = "reroll"
    original = parameters(operation, view)
    calls = []

    def unknown(sid, **arguments):
        key = arguments.pop("idempotency_key")
        calls.append((key, {"session": sid, **deepcopy(arguments)}))
        raise OSError("response unavailable")

    monkeypatch.setattr(service, "chat_reroll", unknown)
    assert channel.handle(request(owner, operation, original))["outcome"] == "unknown"
    key, native_request = calls[0]
    service._change("graph.chat.reroll", key, native_request, lambda repo: deepcopy(view))
    accepted = channel.handle(request(owner, "accept_receipt", {
        "operation": operation, "original_request": original}))
    assert accepted["outcome"] == "succeeded"
    assert channel.handle(request(owner, "state"))["pending"] == []
    assert channel.handle(request(owner, operation, original)) == accepted
    assert len(calls) == 1


def test_private_chat_receipt_and_custody_cannot_be_read_by_another_node(service, monkeypatch):
    channel, owner, graph, frontends, view = prepared(service, count=2)
    original = parameters("reroll", view)
    native_stub(service, monkeypatch, "reroll", view)
    channel.handle(request(owner, "reroll", original))
    other = FrontendNodeOperations(service).bind(**{
        **owner, "node_binding_id": frontends[1]["node_binding_id"]})
    before = counts(service)
    with pytest.raises(ContractValidationError) as conflict:
        other.read_receipt("reroll", original)
    assert conflict.value.reason_code == "idempotency_conflict"
    with pytest.raises(ContractValidationError) as evidence_conflict:
        FrontendNodeEvidenceAdapter(service.database).read_receipt(
            other, "reroll", {"owner": owner, "parameters": original})
    assert evidence_conflict.value.reason_code == "request_evidence_conflict"
    assert counts(service) == before


@pytest.mark.parametrize("operation", NATIVE)
def test_http_adapter_only_requests_execution_for_reroll_and_never_for_receipt(
        service, monkeypatch, operation):
    _, owner, _, _, view = prepared(service)
    original = parameters(operation, view)
    native_stub(service, monkeypatch, operation, view)
    accesses = []
    http = FrontendNodeHTTP(lambda execution: accesses.append(execution) or service, None)
    result = http.channel(owner, request(owner, operation, original))
    assert result["outcome"] == "succeeded"
    for reader in ("receipt", "accept_receipt"):
        assert http.channel(owner, request(owner, reader, {
            "operation": operation, "original_request": original})) == result
    assert accesses == [operation == "reroll", False, False]
