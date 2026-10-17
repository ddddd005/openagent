"""Public graph consumers use declared ports and frozen session history."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.content_contracts import text_content
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from workflow_test_support import AgentTransportFixture, graph as agent_graph
from workflow_test_support import run as run_agent
from legacy_consumer_receipt_fixture import seed_legacy_consumer_receipt
from test_graph_service import (
    create, document, edge, node, run, save_definition_fixture, service, text_graph, uid,
)
from test_graph_server import host_server, request
from test_models_service_integration import ModelDatabaseFixture


def expose(document, *node_ids):
    for item in document["nodes"]:
        if not node_ids or item["node_binding_id"] in node_ids:
            item["public_outputs"] = ["output"]
    return document


def query(document, node_id, port_id="output"):
    return {"workflow_definition_id": document["workflow_definition_id"],
            "definition_revision": document["revision"], "node_id": node_id, "port_id": port_id}


def test_declarations_zero_agent_no_output_and_no_private_state(service):
    doc = expose(text_graph(service.registry), uid(3))
    session = create(service, doc)
    consumer = service.get_consumer(session["workflow_session_id"])
    assert consumer["inputs"] == [] and consumer["can_submit"]
    assert consumer["outputs"][0]["availability"] == "unproduced"
    assert consumer["outputs"][0]["payload"] is None
    assert {"data", "private_states", "messages", "input_values", "facts", "snapshot"}.isdisjoint(consumer)
    assert all("outputs" not in row for row in consumer["nodes"])
    final = run(service, session)
    result = service.read_public_output(final["workflow_session_id"], **query(doc, uid(3)))["output"]
    assert result["payload"] == text_content("pear pear")
    assert result["source"]["workflow_definition_id"] == doc["workflow_definition_id"]
    assert not hasattr(service, "_native_runtime")
    empty = create(service, document([], []))
    view = service.get_consumer(empty["workflow_session_id"])
    assert not view["can_submit"] and view["diagnostics"][0]["code"] == "graph_no_outputs"


def test_relevant_external_inputs_are_discovered_by_registration(service):
    source = node(service.registry, "current-input", 1, input_name="question")
    dormant = node(service.registry, "current-input", 2, input_name="unrelated")
    target = node(service.registry, "output", 3)
    doc = document([source, dormant, target], [edge(source, target, 1)])
    session = create(service, doc)
    assert service.get_consumer(session["workflow_session_id"])["inputs"] == [
        {"name": "question", "data_type": "TEXT", "required": True, "node_ids": [uid(1)]}]
    original = service.registry.get("tools.current-input", "1").definition
    custom = replace(original, component_id="plugin.external-input", display_name="External probe")
    entry = service.registry.get("tools.current-input", "1")
    service.registry.register(custom, entry.executor, external_inputs_validator=entry.external_inputs_validator,
        external_inputs_declaration=entry.external_inputs_declaration)
    source["component_id"] = custom.component_id
    new_session = create(service, document([source, target], [edge(source, target, 2)]))
    assert service.get_consumer(new_session["workflow_session_id"])["inputs"][0]["name"] == "question"
    assert "external_inputs_declaration" not in custom.to_dict()


def test_pending_chain_never_exposes_previous_round(service):
    entered, release = Event(), Event()
    block = False
    def execute(config, inputs, context):
        if block:
            entered.set()
            assert release.wait(10)
        return {"output": text_content("fresh")}
    service.registry.register(NodeDefinition("plugin.block", "1", "Block", "test", {},
        {"type": "object", "additionalProperties": False},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),)), execute)
    source = node(service.registry, "plugin.block", 1, component_version="1")
    target = node(service.registry, "output", 2)
    target["public_outputs"] = ["output"]
    doc = document([source, target], [edge(source, target, 1)])
    final = run(service, create(service, doc))
    first_run = final["nodes"][-1]["run_id"]
    block = True
    started = service.start(final["workflow_session_id"], expected_revision=final["revision"], idempotency_key="next")
    try:
        assert entered.wait(5)
        current = service.get_consumer(final["workflow_session_id"])["outputs"][0]
        assert current["availability"] == "unproduced" and current["payload"] is None
        assert current["run_id"] != first_run
    finally:
        release.set()
        service.wait(started["active_chain_run_id"])


def test_public_output_requires_original_session_and_current_definition(service):
    doc = expose(text_graph(service.registry), uid(3))
    first = run(service, create(service, doc))
    other = service.create_session(doc["workflow_definition_id"], doc["revision"], idempotency_key=str(uuid4()))
    original_run = first["nodes"][-1]["run_id"]
    assert service.read_public_output(
        first["workflow_session_id"], **query(doc, uid(3)), run_id=original_run,
    )["output"]["availability"] == "produced"
    with pytest.raises(ContractValidationError) as outside:
        service.read_public_output(other["workflow_session_id"], **query(doc, uid(3)), run_id=original_run)
    assert outside.value.status_code == 404
    unrelated_revision = deepcopy(doc)
    unrelated_revision["revision"] = 2
    unrelated_revision["nodes"][-1]["public_outputs"] = []
    with pytest.raises(ContractValidationError) as stale_definition:
        service.read_public_output(
            first["workflow_session_id"], **query(unrelated_revision, uid(3)), run_id=original_run,
        )
    assert stale_definition.value.reason_code == "consumer_definition_mismatch"
    with pytest.raises(ContractValidationError) as mismatch:
        service.read_public_output(first["workflow_session_id"], **query(unrelated_revision, uid(3)))
    assert mismatch.value.reason_code == "consumer_definition_mismatch"
    private_doc = text_graph(service.registry)
    private_doc["nodes"][-1]["public_outputs"] = []
    private = run(service, create(service, private_doc))
    with pytest.raises(ContractValidationError) as private_history:
        service.read_public_output(
            private["workflow_session_id"], **query(private_doc, uid(3)),
            run_id=private["nodes"][-1]["run_id"],
        )
    assert private_history.value.reason_code == "output_not_public"


def test_consumer_http_receipts_and_history_do_not_return_private_records(tmp_path, monkeypatch):
    with host_server(tmp_path) as (host, port):
        doc = expose(text_graph(host.graph_service.registry), uid(3))
        save_definition_fixture(host.graph_service, doc)
        create_parameters = {
            "workflow_definition_id": doc["workflow_definition_id"],
            "definition_revision": 1, "idempotency_key": "session",
        }
        created = seed_legacy_consumer_receipt(
            host.graph_service, "consumer.session.create", create_parameters,
        )
        sid = created["result"]["receipt"]["workflow_session_id"]
        path = "/api/graph/sessions/" + sid
        start_parameters = {
            "session_id": sid, "expected_revision": created["result"]["receipt"]["session_revision"],
            "idempotency_key": "start", "inputs": {},
        }
        started = seed_legacy_consumer_receipt(
            host.graph_service, "consumer.run.start", start_parameters,
        )
        chain_id = started["result"]["receipt"]["chain_run_id"]
        host.graph_service.wait(chain_id)
        for operation, parameters, origin in (
            ("consumer.session.create", create_parameters, created),
            ("consumer.run.start", start_parameters, started),
        ):
            status, frozen = request(port, "POST", "/api/graph/consumer/receipts/read", {
                "operation": operation, "parameters": parameters,
            })
            assert status == 200 and frozen["outcome"] == "matched"
            assert frozen["receipt"] == origin["receipt"]
            assert frozen["result"] == {"receipt": origin["result"]["receipt"]}
            assert request(port, "POST", "/api/graph/consumer/receipts/read", {
                "operation": operation, "parameters": parameters,
            })[1]["receipt"] == frozen["receipt"]
        _, discovered = request(port, "GET", path + "/outputs/public")
        assert discovered["outputs"][0]["availability"] == "produced"
        _, history = request(port, "POST", path + "/outputs/history", query(doc, uid(3)))
        assert len(history["outputs"]) == 1
        assert request(port, "POST", path + "/outputs/read", {**query(doc, uid(3)), "public": False})[0] == 400
        _, summary = request(port, "GET", path + "/actions")
        assert set(summary) == {
            "schema_version", "kind", "scope",
            "workflow_definition_id", "definition_revision", "workflow_session_id",
            "session_revision", "status", "can_submit", "available_actions",
            "start_operation", "control_operation",
        }
        assert summary["schema_version"] == 1
        assert summary["kind"] == "workflow.application-actions"
        assert summary["scope"] == "management"
        for consumer in (created["result"]["consumer"], started["result"]["consumer"]):
            assert "private_states" not in consumer and "data" not in consumer
        assert not hasattr(host, "_legacy") and not hasattr(host.graph_service, "_native_runtime")


def test_current_agent_uses_same_public_ports_and_separate_delivery(tmp_path, monkeypatch):
    transport = AgentTransportFixture()
    with closing(GraphWorkflowService(
        tmp_path / "public-agent.sqlite", public_model_factory=transport.factory,
    )) as instance:
        ModelDatabaseFixture.write(instance, 1)
        doc = agent_graph(instance)
        execute = next(row for row in doc["nodes"] if row["component_id"] == "agents.execute")
        execute["public_outputs"] = ["result", "receipts"]
        doc["nodes"][-1]["public_outputs"] = ["output"]
        view = run_agent(instance, create(instance, doc), "public question")
        consumer = instance.get_consumer(view["workflow_session_id"])
        assert [row["data_type"] for row in consumer["outputs"]] == ["TEXT", "AGENT_RECEIPTS", "TEXT"]
        assert consumer["outputs"][0]["payload"] == text_content("accepted answer")
        receipts = consumer["outputs"][1]["payload"]
        assert receipts["schema_version"] == 2 and len(receipts["model_requests"]) == 1
        observed = next(row for row in consumer["nodes"] if row["node_binding_id"] == execute["node_binding_id"])
        assert observed["budget"] is None and "agent" not in observed and "snapshot" not in consumer
        assert view["messages"] == [] and view["status"] == "succeeded" and len(transport.calls) == 1


def test_public_control_receipts_preserve_current_pause_resume_identity(tmp_path, monkeypatch):
    transport = AgentTransportFixture(gate=True)
    with closing(GraphWorkflowService(
        tmp_path / "public-pause.sqlite", public_model_factory=transport.factory,
    )) as instance:
        ModelDatabaseFixture.write(instance, 1)
        doc = agent_graph(instance)
        execute = next(row for row in doc["nodes"] if row["component_id"] == "agents.execute")
        doc["nodes"][-1]["public_outputs"] = ["output"]
        session = create(instance, doc)
        application = GraphApplication(instance)
        started = application.command("run.start", {
            "session_id": session["workflow_session_id"], "expected_revision": session["revision"],
            "idempotency_key": "current-start", "inputs": {"text": "pause question"},
        })
        assert started["receipt"]["operation"] == "run.start"
        chain_id = started["result"]["active_chain_run_id"]
        try:
            assert transport.entered.wait(5)
            current = instance.get_consumer(session["workflow_session_id"])
            paused = application.command("run.control", {
                "session_id": session["workflow_session_id"], "action": "pause",
                "expected_revision": current["session_revision"], "idempotency_key": "current-pause",
            })
            assert paused["receipt"]["operation"] == "run.control"
        finally:
            transport.proceed.set()
            instance.wait(chain_id)
        current = instance.get_consumer(session["workflow_session_id"])
        assert current["status"] == "paused" and "resume" in current["available_actions"]
        active_run = next(row for row in current["nodes"]
                          if row["node_binding_id"] == execute["node_binding_id"])["run_id"]
        resumed = application.command("run.control", {
            "session_id": session["workflow_session_id"], "action": "resume",
            "expected_revision": current["session_revision"], "idempotency_key": "current-resume",
        })
        assert resumed["receipt"]["operation"] == "run.control"
        instance.wait(chain_id)
        final_view = instance.get_consumer(session["workflow_session_id"])
        final_run = next(row for row in final_view["nodes"]
                         if row["node_binding_id"] == execute["node_binding_id"])["run_id"]
        assert final_view["status"] == "succeeded" and final_run == active_run
        assert len(transport.calls) == 1
