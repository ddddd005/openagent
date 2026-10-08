"""Restricted consumer forks retain native CAS and read-only source evidence."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_identity import native_command_request
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import create, gate_graph, run, service, text_graph


def candidate_query(view):
    return {"session_id": view["workflow_session_id"],
            "workflow_definition_id": view["workflow_definition_id"],
            "definition_revision": view["definition_revision"]}


def fork_request(view, candidate_id):
    return {"session_id": view["workflow_session_id"], "candidate_id": candidate_id,
            "expected_revision": view["revision"], "expected_data_revision": view["data_revision"],
            "expected_head_revision": view["head_revision"], "idempotency_key": str(uuid4())}


def rebind(service, view, doc):
    revision = deepcopy(doc)
    revision["revision"] = view["definition_revision"] + 1
    service.save_definition(revision, expected_revision=view["definition_revision"], idempotency_key=str(uuid4()))
    return service.rebind_session(
        view["workflow_session_id"], definition_revision=revision["revision"],
        expected_revision=view["revision"], expected_data_revision=view["data_revision"],
        expected_head_revision=view["head_revision"], idempotency_key=str(uuid4()))


def test_projection_is_strict_and_fork_uses_old_completed_definition_without_parent_changes(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "consumer-forks.sqlite")) as service:
        app = GraphApplication(service).for_consumer()
        doc = text_graph(service.registry)
        first = run(service, create(service, doc))
        candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
        rebound = rebind(service, first, doc)
        parent = run(service, rebound)
        prior = deepcopy(service.get_session(parent["workflow_session_id"]))
        projected = app.query("consumer.candidate.list", candidate_query(parent))
        assert set(projected) == {
            "schema_version", "kind", "workflow_definition_id", "definition_revision",
            "workflow_session_id", "session_revision", "data_revision", "head_revision",
            "status", "can_fork", "candidates"}
        assert projected["kind"] == "workflow.consumer-candidates" and projected["can_fork"]
        assert projected["session_revision"] == parent["revision"]
        assert projected["data_revision"] == parent["data_revision"]
        assert projected["head_revision"] == parent["head_revision"]
        old = next(row for row in projected["candidates"] if row["candidate_id"] == candidate["candidate_id"])
        assert old == {key: candidate[key] for key in (
            "candidate_id", "chain_run_id", "source_session_id",
            "source_workflow_definition_id", "source_definition_revision")}
        assert old["source_definition_revision"] == 1 and parent["definition_revision"] == 2
        request = fork_request(parent, candidate["candidate_id"])
        native = native_command_request("consumer.candidate.fork", request)
        assert native == native_command_request("candidate.fork", request)
        accepted = app.command("consumer.candidate.fork", request)
        receipt = accepted["result"]["receipt"]
        assert receipt["operation"] == "fork" and receipt["chain_run_id"] is None
        assert receipt["workflow_session_id"] != parent["workflow_session_id"]
        assert receipt["definition_revision"] == 1 and receipt["session_revision"] == 1
        assert receipt["fork_source"] == {
            "workflow_session_id": parent["workflow_session_id"],
            "candidate_id": candidate["candidate_id"], "chain_run_id": candidate["chain_run_id"],
            "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1}
        assert accepted["result"]["consumer"]["workflow_session_id"] == receipt["workflow_session_id"]
        assert accepted["receipt"]["accepted"] == receipt
        assert service.get_session(parent["workflow_session_id"]) == prior
        cold = read_graph_application_receipt(
            service.database, "consumer.candidate.fork", request, scope="consumer")
        assert cold["outcome"] == "matched", cold
        assert cold["receipt"] == accepted["receipt"]
        assert cold["result"] == {"receipt": receipt}
        assert not {"consumer", "objects", "private_states", "data"} & cold["result"].keys()
        with pytest.raises(ContractValidationError) as denied:
            app.query("consumer.candidate.list", {
                **candidate_query(parent), "definition_revision": 1})
        assert denied.value.reason_code == "consumer_definition_mismatch"


def test_replay_original_fork_survives_later_parent_and_child_work_without_another_child(tmp_path):
    database = tmp_path / "consumer-replay.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        app = GraphApplication(service).for_consumer()
        doc = text_graph(service.registry)
        first = run(service, create(service, doc))
        candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
        request = fork_request(first, candidate["candidate_id"])
        accepted = app.command("consumer.candidate.fork", request)
        child = service.get_session(accepted["result"]["receipt"]["workflow_session_id"])
        run(service, child)
        parent = run(service, first)
        rebind(service, parent, doc)
        replay = app.command("consumer.candidate.fork", request)
        assert replay["receipt"] == accepted["receipt"]
        assert replay["result"]["receipt"] == accepted["result"]["receipt"]
        assert replay["result"]["consumer"]["session_revision"] > 1
        assert len(service.list_sessions(doc["workflow_definition_id"])) == 2
        with pytest.raises(ContractValidationError) as conflict:
            app.command("consumer.candidate.fork", {**request, "expected_head_revision": 999})
        assert conflict.value.reason_code == "idempotency_conflict"
    before = database.read_bytes()
    cold = read_graph_application_receipt(database, "consumer.candidate.fork", request, scope="consumer")
    assert cold["outcome"] == "matched", cold
    assert cold["receipt"] == accepted["receipt"]
    assert cold["result"] == {"receipt": accepted["result"]["receipt"]}
    assert database.read_bytes() == before


def test_inherited_completed_candidate_is_available_but_cross_session_candidate_is_not(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "consumer-inherited.sqlite")) as service:
        app = GraphApplication(service).for_consumer()
        doc = text_graph(service.registry)
        parent = run(service, create(service, doc))
        candidate = service.list_graph_candidates(parent["workflow_session_id"])["candidates"][0]
        first = app.command("consumer.candidate.fork", fork_request(parent, candidate["candidate_id"]))
        child = service.get_session(first["result"]["receipt"]["workflow_session_id"])
        projected = app.query("consumer.candidate.list", candidate_query(child))
        assert projected["candidates"] == [{key: candidate[key] for key in (
            "candidate_id", "chain_run_id", "source_session_id",
            "source_workflow_definition_id", "source_definition_revision")}]
        assert projected["candidates"][0]["source_session_id"] == parent["workflow_session_id"]
        second = app.command("consumer.candidate.fork", fork_request(child, candidate["candidate_id"]))
        assert second["result"]["receipt"]["fork_source"]["workflow_session_id"] == child["workflow_session_id"]
        assert second["result"]["receipt"]["fork_source"]["chain_run_id"] == candidate["chain_run_id"]
        independent = run(service, create(service, {
            **deepcopy(doc), "workflow_definition_id": str(uuid4())}))
        other_candidate = service.list_graph_candidates(independent["workflow_session_id"])["candidates"][0]
        with pytest.raises(ContractValidationError) as denied:
            app.command("consumer.candidate.fork", fork_request(child, other_candidate["candidate_id"]))
        assert denied.value.reason_code == "graph_candidate_scope_mismatch"


def test_missing_implementation_and_invalid_request_do_not_create_child(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "consumer-availability.sqlite")) as service:
        app = GraphApplication(service).for_consumer()
        doc = text_graph(service.registry)
        parent = run(service, create(service, doc))
        candidate = service.list_graph_candidates(parent["workflow_session_id"])["candidates"][0]
        request = fork_request(parent, candidate["candidate_id"])
        for name, body in (
            ("consumer.candidate.fork", {**request, "candidate_id": "not-an-id"}),
            ("consumer.candidate.fork", {**request, "expected_data_revision": True}),
            ("consumer.candidate.fork", {**request, "workflow_definition_id": doc["workflow_definition_id"]}),
            ("consumer.candidate.list", {**candidate_query(parent), "objects": True}),
        ):
            with pytest.raises(ContractValidationError) as denied:
                (app.query if name.endswith(".list") else app.command)(name, body)
            assert denied.value.reason_code == "invalid_request"
        implementation = service.registry._nodes.pop(("tools.regex", "1"))
        try:
            projected = app.query("consumer.candidate.list", candidate_query(parent))
            assert not projected["can_fork"] and projected["candidates"] == []
            with pytest.raises(ContractValidationError) as denied:
                app.command("consumer.candidate.fork", request)
            assert denied.value.reason_code == "consumer_candidate_unavailable"
        finally:
            service.registry._nodes[("tools.regex", "1")] = implementation
        assert len(service.list_sessions(doc["workflow_definition_id"])) == 1


def test_pause_inflight_candidate_status_matches_consumer_projection(service):
    app = GraphApplication(service).for_consumer()
    sid, chain_id, release = gate_graph(service)
    try:
        parent = service.get_session(sid)
        app.command("consumer.run.control", {
            "session_id": sid, "action": "pause", "expected_revision": parent["revision"],
            "idempotency_key": str(uuid4())})
        consumer = service.get_consumer(sid)
        assert consumer["status"] == "pausing"
        projected = app.query("consumer.candidate.list", candidate_query(service.get_session(sid)))
        assert projected["status"] == consumer["status"]
        assert not projected["can_fork"] and projected["candidates"] == []
    finally:
        release.set()
    service.wait(chain_id)
    assert service.get_consumer(sid)["status"] == "paused"


@pytest.mark.parametrize("mutate", [
    lambda result: result["source"].update(head_commit_id=str(uuid4())),
    lambda result: result["source"].update(object_source_session_id=str(uuid4())),
    lambda result: result["source"].update(state_mappings=[{"action": "reset"}]),
    lambda result: result.update(workflow_session_id=str(uuid4())),
    lambda result: result.update(definition_revision=2),
    lambda result: result.update(head_commit_id=str(uuid4())),
    lambda result: result.update(selected_chain_run_id=str(uuid4())),
    lambda result: result.update(history_refs=[]),
])
def test_readonly_receipt_rejects_forged_fork_source_child_and_checkpoint_evidence(tmp_path, mutate):
    database = tmp_path / "consumer-evidence.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        app = GraphApplication(service).for_consumer()
        parent = run(service, create(service, text_graph(service.registry)))
        candidate = service.list_graph_candidates(parent["workflow_session_id"])["candidates"][0]
        request = fork_request(parent, candidate["candidate_id"])
        accepted = app.command("consumer.candidate.fork", request)
        assert accepted["result"]["receipt"]["fork_source"]["candidate_id"] == candidate["candidate_id"]
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        raw = connection.execute(
            "SELECT result_payload FROM idempotency WHERE operation=? AND key=?",
            ("graph.candidate.fork", request["idempotency_key"]),
        ).fetchone()[0]
        payload = loads_strict(raw)
        mutate(payload[0]["graph_result"])
        connection.execute(
            "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
            (canonical_bytes(payload).decode("utf-8"), "graph.candidate.fork", request["idempotency_key"]),
        )
    cold = read_graph_application_receipt(database, "consumer.candidate.fork", request, scope="consumer")
    assert cold["outcome"] == "unresolved" and cold["reason_code"] == "receipt_invalid", cold


def test_readonly_fork_receipt_does_not_create_missing_database_or_accept_management_origin(tmp_path):
    database = tmp_path / "missing.sqlite"
    request = {"session_id": str(uuid4()), "candidate_id": str(uuid4()),
               "expected_revision": 1, "expected_data_revision": 0, "expected_head_revision": 1,
               "idempotency_key": str(uuid4())}
    assert read_graph_application_receipt(
        database, "consumer.candidate.fork", request, scope="consumer")["outcome"] == "unresolved"
    assert not database.exists()
    database = tmp_path / "management-origin.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        parent = run(service, create(service, text_graph(service.registry)))
        candidate = service.list_graph_candidates(parent["workflow_session_id"])["candidates"][0]
        request = fork_request(parent, candidate["candidate_id"])
        GraphApplication(service).command("candidate.fork", request)
        with pytest.raises(ContractValidationError) as denied:
            GraphApplication(service).for_consumer().command("consumer.candidate.fork", request)
        assert denied.value.reason_code == "consumer_candidate_receipt_origin_mismatch"
        assert len(service.list_sessions(parent["workflow_definition_id"])) == 2
    cold = read_graph_application_receipt(database, "consumer.candidate.fork", request, scope="consumer")
    assert cold["outcome"] == "unresolved" and cold["reason_code"] == "application_identity_missing"
