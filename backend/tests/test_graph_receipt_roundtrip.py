"""Actual application commands retain origins for each native receipt family."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.global_resources import legacy_content_to_current
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import WriteIntent
from phase1_agent.storage import SqliteStore
from phase1_agent.workbench_resources import WorkbenchResourceStore

from resource_fixtures import resource
from test_context_failure_audit import _candidate_id
from test_context_integration import context_graph, synthetic_package
from test_graph_event_contracts import event_graph
from test_graph_service import create, document, edge, node, run, service, text_graph
from test_graph_session_objects import object_graph, task_package


def roundtrip(app, operation, parameters):
    original = deepcopy(parameters)
    accepted = app.command(operation, parameters)
    frozen = read_graph_application_receipt(app._service.database, operation, original, scope=app.scope)
    assert frozen["outcome"] == "matched", frozen
    assert set(frozen) == {"schema_version", "kind", "outcome", "reason_code", "receipt", "result"}
    assert frozen["reason_code"] == "receipt_matched"
    assert frozen["receipt"] == accepted["receipt"]
    if operation.startswith("consumer."):
        assert frozen["result"] == {"receipt": accepted["result"]["receipt"]}
        assert set(frozen["result"]["receipt"]) == {
            "workflow_definition_id", "definition_revision", "workflow_session_id", "session_revision",
            "status", "chain_run_id", "operation", "idempotency_key",
        }
    else:
        assert frozen["result"] == accepted["result"]
    assert app.query("receipt.read", {"operation": operation, "parameters": original}) == frozen
    assert parameters == original
    return accepted["result"]


def initial(app, doc, *, consumer=False):
    roundtrip(app, "definition.save", {"document": doc, "expected_revision": 0,
                                     "idempotency_key": str(uuid4())})
    owner = app.for_consumer() if consumer else app
    return roundtrip(owner, "consumer.session.create" if consumer else "session.create", {
        "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1,
        "idempotency_key": str(uuid4()),
    })


def gate_document(service, entered, release):
    def execute(config, inputs, context):
        entered.set()
        assert release.wait(5), "test gate timed out"
        return {"output": inputs["input"]}
    service.registry.register(NodeDefinition(
        "test.receipt-gate", "1", "Receipt Gate", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT"),), outputs=(NodePort("output", "TEXT"),),
    ), execute)
    first, gate, output = [node(service.registry, kind, index) for kind, index in (
        ("text", 1), ("test.receipt-gate", 2), ("output", 3),
    )]
    return document([first, gate, output], [edge(first, gate, 1), edge(gate, output, 2)])


def test_definition_create_rebind_data_copy_and_source_free_legacy_migration_roundtrip(service):
    app = GraphApplication(service)
    doc = text_graph(service.registry)
    view = initial(app, doc)
    revision = deepcopy(doc)
    revision.update(revision=2, name="Rebound application")
    roundtrip(app, "definition.save", {"document": revision, "expected_revision": 1,
                                     "idempotency_key": str(uuid4())})
    sid = view["workflow_session_id"]
    view = roundtrip(app, "session.rebind", {
        "session_id": sid, "definition_revision": 2, "expected_revision": view["revision"],
        "expected_data_revision": view["data_revision"], "expected_head_revision": view["head_revision"],
        "idempotency_key": str(uuid4()),
    })
    view = roundtrip(app, "session.data.update", {
        "session_id": sid, "expected_revision": view["revision"],
        "expected_data_revision": view["data_revision"], "idempotency_key": str(uuid4()),
    })
    copied_document = deepcopy(revision)
    copied_document.update(workflow_definition_id=str(uuid4()), revision=1)
    copied = roundtrip(app, "session.copy", {
        "session_id": sid, "document": copied_document, "expected_session_revision": view["revision"],
        "expected_data_revision": view["data_revision"], "expected_definition_revision": 2,
        "expected_head_revision": view["head_revision"], "idempotency_key": str(uuid4()),
    })
    assert copied["workflow_session_id"] != sid
    migration_document = deepcopy(doc)
    migration_document["workflow_definition_id"] = str(uuid4())
    migrated = roundtrip(app, "legacy.migrate", {
        "document": migration_document, "source_session_id": None,
        "expected_source_revision": None, "mappings": [], "idempotency_key": str(uuid4()),
    })
    assert migrated["session"]["source"]["kind"] == "legacy_migration"


def test_completed_start_and_both_candidate_commands_read_the_original_frozen_results(service):
    app = GraphApplication(service)
    view = initial(app, text_graph(service.registry))
    sid = view["workflow_session_id"]
    request = {"session_id": sid, "expected_revision": view["revision"], "idempotency_key": str(uuid4())}
    started = roundtrip(app, "run.start", request)
    service.wait(started["active_chain_run_id"])
    assert read_graph_application_receipt(service.database, "run.start", request)["result"] == started
    view = service.get_session(sid)
    candidate = service.list_graph_candidates(sid)["candidates"][0]["candidate_id"]
    parameters = {
        "session_id": sid, "candidate_id": candidate, "expected_revision": view["revision"],
        "expected_data_revision": view["data_revision"], "expected_head_revision": view["head_revision"],
        "idempotency_key": str(uuid4()),
    }
    forked = roundtrip(app, "candidate.fork", parameters)
    assert forked["workflow_session_id"] != sid
    roundtrip(app, "candidate.select", {**parameters, "idempotency_key": str(uuid4())})


@pytest.mark.parametrize("consumer", [False, True])
def test_management_and_consumer_events_have_distinct_durable_origins(service, consumer):
    app = GraphApplication(service)
    doc = event_graph()
    doc["nodes"] = doc["nodes"][:4]
    doc["nodes"][1]["public_outputs"] = ["output"]
    created = initial(app, doc, consumer=consumer)
    view = created["receipt"] if consumer else created
    owner = app.for_consumer() if consumer else app
    parameters = {
        "session_id": view["workflow_session_id"], "workflow_definition_id": doc["workflow_definition_id"],
        "definition_revision": 1, "event_id": "frontend.append", "event_schema_version": 1,
        "payload": {"text": "named event"}, "expected_revision": view["session_revision"] if consumer else view["revision"],
        "idempotency_key": str(uuid4()),
    }
    operation = "consumer.event.submit" if consumer else "event.submit"
    result = roundtrip(owner, operation, parameters)
    chain = result["receipt"]["chain_run_id"] if consumer else result["active_chain_run_id"]
    service.wait(chain)
    frozen = read_graph_application_receipt(service.database, operation, parameters, scope=owner.scope)
    assert frozen["outcome"] == "matched"


@pytest.mark.parametrize("consumer", [False, True])
def test_management_and_consumer_start_pause_resume_and_close_roundtrip(service, consumer):
    entered, release = Event(), Event()
    app = GraphApplication(service)
    doc = gate_document(service, entered, release)
    doc["nodes"][-1]["public_outputs"] = ["output"]
    created = initial(app, doc, consumer=consumer)
    view = created["receipt"] if consumer else created
    sid = view["workflow_session_id"]
    owner = app.for_consumer() if consumer else app
    operation = "consumer.run.start" if consumer else "run.start"
    started = roundtrip(owner, operation, {
        "session_id": sid, "expected_revision": view["session_revision"] if consumer else view["revision"],
        "idempotency_key": str(uuid4()),
    })
    chain = started["receipt"]["chain_run_id"] if consumer else started["active_chain_run_id"]
    control = "consumer.run.control" if consumer else "run.control"
    try:
        assert entered.wait(5)
        current = service.get_session(sid)
        roundtrip(owner, control, {"session_id": sid, "action": "pause",
            "expected_revision": current["revision"], "idempotency_key": str(uuid4())})
    finally:
        release.set()
    service.wait(chain)
    paused = service.get_session(sid)
    assert paused["status"] == "paused"
    if consumer:
        roundtrip(owner, control, {"session_id": sid, "action": "close",
            "expected_revision": paused["revision"], "idempotency_key": str(uuid4())})
    else:
        roundtrip(owner, control, {"session_id": sid, "action": "resume",
            "expected_revision": paused["revision"], "idempotency_key": str(uuid4())})
        service.wait(chain)


def test_object_write_roundtrip_uses_the_outer_application_identity(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "object-roundtrip.sqlite",
        capability_packages=[task_package()],
        enabled_packages={"workflow.compat": "1.0.0", "example.tasks": "1.0.0"},
    )) as service:
        app = GraphApplication(service)
        view = initial(app, object_graph(service))
        roundtrip(app, "object.write", {
            "session_id": view["workflow_session_id"], "node_id": view["nodes"][0]["node_binding_id"],
            "writes": [WriteIntent("main", 1, "receipt-object-edit", {"count": 8}).to_dict()],
            "expected_revision": view["revision"], "idempotency_key": str(uuid4()),
        })


@pytest.mark.parametrize("explicit_scope", [False, True])
def test_resource_import_roundtrip_uses_the_original_omitted_or_explicit_scope(service, explicit_scope):
    record = resource("legacy roundtrip body")
    with closing(SqliteStore(service.database)) as store:
        WorkbenchResourceStore(store).write("content", record, expected_revision=0, idempotency_key=str(uuid4()))
    app = GraphApplication(service)
    parameters = {"legacy_id": record["resource_id"], "idempotency_key": str(uuid4())}
    if explicit_scope:
        parameters["scope"] = "workspace"
    imported = roundtrip(app, "resource.import", parameters)
    assert imported["reference"]["scope"] == "workspace"
    changed = legacy_content_to_current(record)
    changed["update_sequence"] = 2
    changed["value"]["members"][0]["text"] = "replacement"
    roundtrip(app, "resource.save", {
        "record": changed, "expected_sequence": 1, "idempotency_key": str(uuid4()),
    })
    roundtrip(app, "resource.delete", {
        "identity": imported["reference"], "expected_sequence": 2, "idempotency_key": str(uuid4()),
    })
    assert read_graph_application_receipt(service.database, "resource.import", parameters)["result"] == imported


def test_context_adoption_roundtrip_and_malformed_object_receipts_stay_unresolved(tmp_path, monkeypatch):
    calls = []
    with closing(GraphWorkflowService(tmp_path / "adoption-roundtrip.sqlite",
        capability_packages=[synthetic_package(calls)],
        enabled_packages={"workflow.context-test.synthetic": "1.0.0"},
    )) as service:
        original = service._host_call

        def fail_save(context, capability, operation, payload):
            if capability == "context:validate" and payload.get("operation") == "save":
                raise OSError("injected save failure")
            return original(context, capability, operation, payload)

        monkeypatch.setattr(service, "_host_call", fail_save)
        final = run(service, create(service, context_graph(service)))
        assert final["status"] == "failed"
        output = _candidate_id(service, final)
        writer = next(entry["node_binding_id"] for entry in final["nodes"] if entry["label"] == "context.save")
        monkeypatch.setattr(service, "_host_call", original)
        app = GraphApplication(service)
        closed = roundtrip(app, "run.control", {
            "session_id": final["workflow_session_id"], "action": "close",
            "expected_revision": final["revision"], "idempotency_key": str(uuid4()),
        })
        parameters = {"session_id": closed["workflow_session_id"], "node_id": writer,
            "view_output_id": output, "expected_revision": closed["revision"], "idempotency_key": str(uuid4())}
        adopted = roundtrip(app, "context.adopt", parameters)
        receipt = adopted["results"][0]
        variants = [
            [{"arbitrary": "receipt"}], [{**receipt, "revision_id": "not-an-id"}],
            [{**receipt, "revision": True}], [{**receipt, "deleted": True}],
            [{**receipt, "object_key": "another-object"}], [receipt, receipt],
        ]
        with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
            raw = connection.execute(
                "SELECT result_payload FROM idempotency WHERE operation=? AND key=?",
                ("graph.context.adopt", parameters["idempotency_key"]),
            ).fetchone()[0]
            for malformed in variants:
                payload = loads_strict(raw)
                payload[0]["graph_result"]["results"] = malformed
                connection.execute(
                    "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
                    (canonical_bytes(payload).decode("utf-8"), "graph.context.adopt", parameters["idempotency_key"]),
                )
                read = read_graph_application_receipt(service.database, "context.adopt", parameters)
                assert read["outcome"] == "unresolved" and read["reason_code"] == "receipt_invalid", read
            connection.execute(
                "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
                (raw, "graph.context.adopt", parameters["idempotency_key"]),
            )
        again = {**parameters, "expected_revision": adopted["session"]["revision"], "idempotency_key": str(uuid4())}
        assert roundtrip(app, "context.adopt", again)["results"] == []
        assert len(calls) == 1
