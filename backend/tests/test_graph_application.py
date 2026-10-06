"""Application command scopes and receipts over offline graph execution."""

from copy import deepcopy
from contextlib import closing
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS, QUERIES
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.global_resources import legacy_content_to_current
from phase1_agent.host_sdk import WriteIntent

from test_graph_service import create, gate_graph, service, text_graph
from test_graph_session_objects import object_graph, task_package
from test_graph_server import host_server, request
from resource_fixtures import resource


def public_document(service):
    doc = text_graph(service.registry)
    doc["nodes"][-1]["public_outputs"] = ["output"]
    return doc


def test_named_commands_and_queries_preserve_original_receipts(service):
    app = GraphApplication(service)
    doc = public_document(service)
    save = {"document": doc, "expected_revision": 0, "idempotency_key": "application-save"}
    saved = app.command("definition.save", save)
    assert saved["result"] == doc and saved["receipt"]["accepted"]["definition_revision"] == 1
    created = app.command("session.create", {"workflow_definition_id": doc["workflow_definition_id"],
        "definition_revision": 1, "idempotency_key": "application-create"})
    session = created["result"]
    command = {"session_id": session["workflow_session_id"], "expected_revision": session["revision"],
               "idempotency_key": "application-start", "inputs": {}}
    original = deepcopy(command)
    started = app.command("run.start", command)
    assert command == original
    assert started["receipt"]["authority"] == "service_receipt"
    assert started["receipt"]["idempotency_key"] == command["idempotency_key"]
    service.wait(started["result"]["active_chain_run_id"])
    final = app.query("session.read", {"session_id": session["workflow_session_id"]})
    assert final["status"] == "succeeded"
    replay = GraphApplication(service).command("run.start", original)
    assert replay == started
    assert len(final["chains"]) == 1
    assert app.query("run.read", {"session_id": session["workflow_session_id"],
        "chain_id": started["result"]["active_chain_run_id"]})["chain"]["status"] == "succeeded"
    with pytest.raises(ContractValidationError) as conflict:
        app.command("run.start", {**original, "inputs": {"text": "different"}})
    assert conflict.value.reason_code == "idempotency_conflict"
    assert service._native_runtime is None


def test_consumer_receipt_remains_stable_after_fresh_observation_changes(service):
    management, consumer = GraphApplication(service), GraphApplication(service, scope="consumer")
    doc = public_document(service)
    view = create(service, doc)
    original = {"workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1,
                "idempotency_key": "consumer-application-create"}
    created = consumer.command("consumer.session.create", original)
    sid = created["receipt"]["accepted"]["workflow_session_id"]
    assert created["result"]["consumer"]["status"] == "idle"
    started = management.command("run.start", {"session_id": sid,
        "expected_revision": created["receipt"]["accepted"]["session_revision"], "idempotency_key": "run-after-create"})
    service.wait(started["result"]["active_chain_run_id"])
    replay = consumer.command("consumer.session.create", original)
    assert replay["receipt"] == created["receipt"]
    assert replay["result"]["receipt"] == created["result"]["receipt"]
    assert replay["result"]["consumer"]["status"] == "succeeded"
    assert replay["result"] != created["result"]
    assert len(management.query("definition.sessions", {"workflow_definition_id": doc["workflow_definition_id"]})) == 2
    public = consumer.query("consumer.read", {"session_id": sid})
    assert public["outputs"][0]["payload"]["text"] == "pear pear"
    assert not {"data", "objects", "private_states", "messages", "runtime_facts"} & public.keys()
    assert view["workflow_session_id"] != sid


@pytest.mark.parametrize("name,parameters", [
    ("session.read", {}), ("object.list", {}), ("object.read", {}),
    ("manifest.read", {}), ("run.read", {}), ("archive.read", {}),
    ("candidate.list", {}), ("resource.read", {}), ("catalog.node-types", {}),
])
def test_consumer_scope_cannot_dispatch_management_queries(service, name, parameters):
    with pytest.raises(ContractValidationError) as failure:
        GraphApplication(service, scope="consumer").query(name, parameters)
    assert failure.value.reason_code == "application_scope_denied" and failure.value.status_code == 403


@pytest.mark.parametrize("name", [
    "definition.save", "run.start", "run.control", "candidate.select", "candidate.fork",
    "object.write", "context.adopt", "resource.save", "packages.configure",
])
def test_consumer_scope_cannot_dispatch_management_commands(service, name):
    with pytest.raises(ContractValidationError) as failure:
        GraphApplication(service, scope="consumer").command(name, {})
    assert failure.value.reason_code == "application_scope_denied" and failure.value.status_code == 403


def test_discovery_reports_only_bound_operation_scope_and_idempotency(service):
    consumer = GraphApplication(service, scope="consumer").describe()
    assert {item["name"] for item in consumer["commands"]} == {
        "consumer.session.create", "consumer.run.start", "consumer.run.control", "consumer.event.submit"}
    assert all(item["scope"] == "consumer" for item in consumer["queries"])
    assert consumer["boundary"]["caller"] == "trusted_local"
    assert consumer["boundary"]["remote_plugin_authentication"] is False
    management = GraphApplication(service).describe()
    packages = next(item for item in management["commands"] if item["name"] == "packages.configure")
    assert packages["idempotency"] == "none"
    assert len({item.name for item in COMMANDS}) == len(COMMANDS)
    assert len({item.name for item in QUERIES}) == len(QUERIES)


def test_unknown_operations_fields_and_control_actions_are_rejected_before_effects(service):
    app = GraphApplication(service)
    initial = create(service, public_document(service))
    sid = initial["workflow_session_id"]
    valid = {"session_id": sid, "expected_revision": initial["revision"], "idempotency_key": "strict"}
    malformed = [
        ("run.start", {**valid, "rogue": True}),
        ("run.start", {**valid, "inputs": []}),
        ("run.start", {**valid, "expected_revision": True}),
        ("run.start", {**valid, "idempotency_key": " "}),
        ("run.control", {**valid, "action": "force_replay"}),
        ("run.control", {**valid, "action": []}),
    ]
    for name, parameters in malformed:
        with pytest.raises(ContractValidationError) as failure:
            app.command(name, parameters)
        assert failure.value.reason_code == "invalid_request"
    with pytest.raises(ContractValidationError) as unknown:
        app.command("run.force_replay", valid)
    assert unknown.value.reason_code == "not_found"
    assert app.query("session.read", {"session_id": sid}) == initial


def test_actions_and_control_remain_callable_while_graph_is_paused(service):
    app = GraphApplication(service)
    sid, chain_id, release = gate_graph(service)
    try:
        running = app.query("session.actions", {"session_id": sid})
        assert running["available_actions"] == ["pause"] and not running["can_submit"]
        pause = app.command("run.control", {"session_id": sid, "action": "pause",
            "expected_revision": running["session_revision"], "idempotency_key": "application-pause"})
        assert pause["receipt"]["operation"] == "run.control"
    finally:
        release.set()
    service.wait(chain_id)
    paused = app.query("session.actions", {"session_id": sid})
    assert paused["status"] == "paused" and paused["available_actions"] == ["resume", "close"]
    parameters = {"session_id": sid, "action": "resume", "expected_revision": paused["session_revision"],
                  "idempotency_key": "application-resume"}
    resumed = app.command("run.control", parameters)
    service.wait(chain_id)
    assert app.command("run.control", parameters)["receipt"] == resumed["receipt"]
    finished = app.query("session.actions", {"session_id": sid})
    assert finished["status"] == "succeeded" and finished["can_submit"] and not finished["available_actions"]
    assert len(app.query("run.read", {"session_id": sid, "chain_id": chain_id})["node_runs"]) == 3


def test_object_commands_use_original_service_permissions_and_receipts(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "application-objects.sqlite", capability_packages=[task_package()],
                                      enabled_packages={"workflow.compat": "1.0.0", "example.tasks": "1.0.0"})) as service:
        app = GraphApplication(service)
        doc = object_graph(service)
        view = create(service, doc)
        sid, writer = view["workflow_session_id"], view["nodes"][0]["node_binding_id"]
        parameters = {"session_id": sid, "node_id": writer,
            "writes": [WriteIntent("main", 1, "application-edit", {"count": 8}).to_dict()],
            "expected_revision": view["revision"], "idempotency_key": "application-object-write"}
        accepted = app.command("object.write", parameters)
        assert accepted["result"]["session"]["objects"]["main"]["value"] == {"count": 8}
        assert app.command("object.write", parameters) == accepted
        assert app.query("object.list", {"session_id": sid})["objects"]["main"]["value"] == {"count": 8}
        with pytest.raises(ContractValidationError) as denied:
            app.query("object.read", {"session_id": sid, "object_key": "main", "node_id": doc["nodes"][-1]["node_binding_id"]})
        assert denied.value.reason_code == "session_object_access_denied"


def test_resource_receipts_are_identity_only_and_fresh_queries_read_current_head(service):
    app = GraphApplication(service)
    record = legacy_content_to_current(resource("application-original"))
    original = {"record": record, "expected_sequence": 0, "idempotency_key": str(uuid4())}
    first = app.command("resource.save", original)
    reference = first["result"]["reference"]
    assert first["receipt"]["accepted"] == first["result"]
    changed = deepcopy(record)
    changed["update_sequence"] += 1
    changed["value"]["members"][0]["text"] = "application-current"
    app.command("resource.save", {"record": changed, "expected_sequence": 1, "idempotency_key": str(uuid4())})
    assert app.command("resource.save", original) == first
    assert app.query("resource.read", {"identity": reference}) == changed
    assert dispatch_graph(service, "POST", "/api/graph/resources/list", {}) == (200, [changed])
    assert "application-original" not in repr(first["receipt"])
    deleted = app.command("resource.delete", {"identity": reference, "expected_sequence": 2,
                                             "idempotency_key": str(uuid4())})
    assert deleted["receipt"]["accepted"]["deleted"]
    assert app.query("resource.read", {"identity": reference}) is None
    assert dispatch_graph(service, "POST", "/api/graph/resources/list", {}) == (200, [])


def test_legacy_http_routes_really_use_application_command_and_query(service, monkeypatch):
    calls = []
    command, query = GraphApplication.command, GraphApplication.query

    def command_spy(self, name, parameters):
        calls.append(("command", name))
        return command(self, name, parameters)

    def query_spy(self, name, parameters=None):
        calls.append(("query", name))
        return query(self, name, parameters)

    monkeypatch.setattr(GraphApplication, "command", command_spy)
    monkeypatch.setattr(GraphApplication, "query", query_spy)
    doc = public_document(service)
    assert dispatch_graph(service, "POST", "/api/graph/definitions", {
        "document": doc, "expected_revision": 0, "idempotency_key": "legacy-http-save"}) == (201, doc)
    assert dispatch_graph(service, "GET", "/api/graph/definitions/" + doc["workflow_definition_id"]) == (200, doc)
    _, session = dispatch_graph(service, "POST", "/api/graph/sessions", {
        "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1, "idempotency_key": "legacy-http-create"})
    sid = session["workflow_session_id"]
    assert dispatch_graph(service, "GET", "/api/graph/sessions/" + sid + "/consumer")[1]["kind"] == "workflow.consumer"
    assert calls == [("command", "definition.save"), ("query", "definition.read"),
                     ("command", "session.create"), ("query", "consumer.read")]


def test_http_envelopes_preserve_original_identity_and_consumer_scope(tmp_path, monkeypatch):
    with host_server(tmp_path, monkeypatch) as (host, port):
        discovery = request(port, "GET", "/api/graph/application")[1]
        assert discovery["scope"] == "management"
        assert request(port, "GET", "/api/graph/consumer/application")[1]["scope"] == "consumer"
        doc = public_document(host.graph_service)
        save = {"operation": "definition.save", "parameters": {
            "document": doc, "expected_revision": 0, "idempotency_key": "http-application-save"}}
        status, saved = request(port, "POST", "/api/graph/commands", save)
        assert status == 201 and saved["receipt"]["accepted"]["definition_revision"] == 1
        assert request(port, "POST", "/api/graph/commands", save) == (201, saved)
        assert request(port, "POST", "/api/graph/queries", {"operation": "definition.read",
            "parameters": {"identity": doc["workflow_definition_id"], "revision": 1}}) == (200, doc)
        status, denied = request(port, "POST", "/api/graph/consumer/commands", save)
        assert status == 403 and denied["error"]["reason_code"] == "application_scope_denied"
        assert request(port, "POST", "/api/graph/consumer/queries",
            {"operation": "object.list", "parameters": {}})[0] == 403
        assert request(port, "POST", "/api/graph/consumer/commands", {**save, "scope": "management"})[0] == 400
        assert request(port, "POST", "/api/graph/commands", save, origin=False)[0] == 403
        assert request(port, "POST", "/api/graph/commands", {"operation": "not.registered", "parameters": {}})[0] == 404
        assert request(port, "POST", "/api/graph/queries", {"operation": "definition.read", "parameters": {
            "identity": doc["workflow_definition_id"], "revision": True}})[0] == 400
        assert host._legacy is None and host.graph_service._native_runtime is None


@pytest.mark.parametrize("path", ["/api/graph/resources/list", "/api/graph/resources/read"])
def test_platform_post_queries_still_require_an_object_body(service, path):
    with pytest.raises(ContractValidationError) as failure:
        dispatch_graph(service, "POST", path, None)
    assert failure.value.reason_code == "invalid_request"


def test_bound_consumer_application_cannot_be_upgraded_through_http(service):
    consumer = GraphApplication(service, scope="consumer")
    assert dispatch_graph(consumer, "GET", "/api/graph/application")[1]["scope"] == "consumer"
    with pytest.raises(ContractValidationError) as failure:
        dispatch_graph(consumer, "POST", "/api/graph/commands", {"operation": "definition.save", "parameters": {}})
    assert failure.value.reason_code == "application_scope_denied"
