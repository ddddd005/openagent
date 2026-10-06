"""Named client transport keeps the existing execution and read authorities."""

from copy import deepcopy
from hashlib import sha256

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_http import dispatch_graph

from test_graph_application import public_document
from test_graph_information_integration import (
    DISCOVER_ONLY,
    PRIVATE,
    PUBLIC,
    information_service,
)
from test_graph_service import create, service
from test_runtime_hosting_integration import graph


def command(service, operation, parameters, *, consumer=False):
    path = "/api/graph/consumer/commands" if consumer else "/api/graph/commands"
    return dispatch_graph(service, "POST", path, {
        "operation": operation, "parameters": deepcopy(parameters),
    })[1]


def query(service, operation, parameters=None, *, consumer=False):
    path = "/api/graph/consumer/queries" if consumer else "/api/graph/queries"
    return dispatch_graph(service, "POST", path, {
        "operation": operation, "parameters": deepcopy(parameters or {}),
    })[1]


def rejected(code, callback):
    with pytest.raises(ContractValidationError) as caught:
        callback()
    assert caught.value.reason_code == code


def test_named_consumer_replay_separates_original_receipt_from_current_observation(service):
    document = public_document(service)
    create(service, document)
    parameters = {
        "workflow_definition_id": document["workflow_definition_id"],
        "definition_revision": document["revision"],
        "idempotency_key": "plan12b-frozen-create",
    }
    original = deepcopy(parameters)
    created = command(service, "consumer.session.create", parameters, consumer=True)
    receipt = created["receipt"]
    accepted = receipt["accepted"]
    session_id = accepted["workflow_session_id"]
    assert receipt["request_sha256"] == sha256(canonical_bytes({
        "operation": "consumer.session.create", "parameters": original,
    })).hexdigest()
    assert accepted == created["result"]["receipt"]
    assert created["result"]["consumer"]["status"] == "idle"

    start_parameters = {
        "session_id": session_id,
        "expected_revision": accepted["session_revision"],
        "inputs": {},
        "idempotency_key": "plan12b-frozen-start",
    }
    started = command(service, "run.start", start_parameters)
    service.wait(started["result"]["active_chain_run_id"])
    replay = command(service, "consumer.session.create", original, consumer=True)
    assert parameters == original
    assert replay["receipt"] == receipt
    assert replay["result"]["receipt"] == created["result"]["receipt"]
    assert replay["result"]["consumer"]["status"] == "succeeded"
    assert replay["result"]["consumer"] != created["result"]["consumer"]
    assert command(service, "run.start", start_parameters) == started
    current = query(service, "session.read", {"session_id": session_id})
    assert len(current["chains"]) == 1
    assert current["revision"] > accepted["session_revision"]
    assert len(query(service, "candidate.list", {"session_id": session_id})["candidates"]) == 1
    assert query(service, "consumer.read", {"session_id": session_id}, consumer=True)["status"] == "succeeded"
    assert service._native_runtime is None


def test_named_directory_pagination_never_reads_provider_or_executes_nodes(information_service):
    service, provider, _ = information_service
    initial = create(service, graph(service))
    parameters = {"kind": "information_source", "package_id": "sample.hosted-work", "limit": 1}
    first = query(service, "registration.list", parameters)
    cursor, items = first["next_cursor"], first["items"]
    assert cursor is not None and len(items) == 1
    rejected("information_invalid_cursor", lambda: query(
        service, "registration.list", {**parameters, "limit": 2, "cursor": cursor}))
    while cursor is not None:
        page = query(service, "registration.list", {**parameters, "cursor": cursor})
        assert len(page["items"]) == 1
        items.extend(page["items"])
        cursor = page["next_cursor"]
    assert len(items) == 4
    assert all(item["availability"] == "unbound" for item in items)
    assert len({canonical_bytes(item["registration_ref"]) for item in items}) == 4
    public = query(service, "registration.list", {**parameters, "limit": 100}, consumer=True)
    assert {item["registration_ref"]["source_id"] for item in public["items"]} == {
        PUBLIC.source_id, DISCOVER_ONLY.source_id,
    }
    assert provider.reads == provider.factories == provider.handles == []
    assert query(service, "session.read", {"session_id": initial["workflow_session_id"]}) == initial


def test_named_non_agent_reader_preserves_exact_binding_and_cannot_finish_execution(information_service):
    service, provider, _ = information_service
    initial = create(service, graph(service))
    session_id = initial["workflow_session_id"]
    started = command(service, "run.start", {
        "session_id": session_id, "expected_revision": initial["revision"],
        "idempotency_key": "plan12b-non-agent-start",
    })
    chain_id = started["result"]["active_chain_run_id"]
    try:
        assert provider.entered.wait(5)
        before = query(service, "session.read", {"session_id": session_id})
        routes = query(service, "registration.list", {
            "session_id": session_id, "chain_id": chain_id,
            "kind": "information_binding", "limit": 100,
        })["items"]
        route = next(item for item in routes if item["registration_ref"] == PUBLIC.to_dict())
        parameters = {
            "session_id": session_id, "reference": route["registration_ref"],
            "owner": deepcopy(route["owner"]), "generation": route["generation"],
            "source_scope": "live", "limit": 1, "cursor": None,
        }
        original = deepcopy(parameters)
        page = query(service, "information.read", parameters, consumer=True)
        assert parameters == original
        assert page["source_ref"] == parameters["reference"]
        assert page["owner"] == parameters["owner"]
        assert page["generation"] == parameters["generation"]
        assert page["source_scope"] == "live"
        assert page["items"] == [{"dispatched": 1}]
        assert provider.reads == [{
            "owner": original["owner"], "generation": original["generation"],
            "limit": 1, "cursor": None,
        }]
        for reference in (PRIVATE, DISCOVER_ONLY):
            rejected("information_read_denied", lambda: query(
                service, "information.read", {**parameters, "reference": reference.to_dict()}, consumer=True))
        rejected("information_invalid_limit", lambda: query(
            service, "information.read", {**parameters, "limit": 9}, consumer=True))
        rejected("information_invalid_generation", lambda: query(
            service, "information.read", {**parameters, "generation": True}, consumer=True))
        rejected("information_unbound", lambda: query(
            service, "information.read", {**parameters, "generation": route["generation"] + 1}, consumer=True))
        rejected("invalid_request", lambda: query(
            service, "information.read", {**parameters, "completed": True}, consumer=True))
        rejected("not_found", lambda: command(
            service, "run.complete", {
                "session_id": session_id, "expected_revision": before["revision"],
                "idempotency_key": "plan12b-client-done",
            }))
        assert len(provider.reads) == 1
        assert provider.handles[0].dispatched == 1
        assert query(service, "session.read", {"session_id": session_id}) == before
        assert query(service, "candidate.list", {"session_id": session_id})["candidates"] == []
    finally:
        provider.proceed.set()
    service.wait(chain_id)
    final = query(service, "session.read", {"session_id": session_id})
    assert final["status"] == "succeeded"
    assert final["head_commit_id"] != initial["head_commit_id"]
    assert provider.handles[0].dispatched == 1
    rejected("information_live_unavailable", lambda: query(
        service, "information.read", parameters, consumer=True))
    assert query(service, "information.read", {
        **parameters, "source_scope": "history", "limit": 8,
    }, consumer=True)["owner"] == original["owner"]


@pytest.mark.parametrize("operation", [
    "object.artifact.read", "object.read", "session.read", "manifest.read",
])
def test_named_consumer_scope_rejects_management_before_dispatch(service, operation):
    rejected("application_scope_denied", lambda: query(service, operation, {}, consumer=True))
    rejected("invalid_request", lambda: dispatch_graph(
        service, "POST", "/api/graph/consumer/queries",
        {"operation": operation, "parameters": {}, "scope": "management"}))
