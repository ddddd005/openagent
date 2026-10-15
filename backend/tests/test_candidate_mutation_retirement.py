"""Retired candidate writes stay unreachable while their old receipts remain readable."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_identity import (
    APPLICATION_IDENTITY_OPERATION, application_command_digest, application_command_identity,
    application_origin_key,
)
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.storage import SqliteStore, _record_id

from test_graph_application_receipt_custody import raw_database
from test_graph_receipt_http import post, serve_receipts
from test_graph_service import create, gate_graph, run, service, text_graph
from test_server import call


RETIRED = ("candidate.select", "candidate.fork", "consumer.candidate.fork")


def _completed(service):
    document = text_graph(service.registry)
    view = run(service, create(service, document))
    candidate = service.list_graph_candidates(view["workflow_session_id"])["candidates"][0]
    request = {
        "session_id": view["workflow_session_id"],
        "candidate_id": candidate["candidate_id"],
        "expected_revision": view["revision"],
        "expected_data_revision": view["data_revision"],
        "expected_head_revision": view["head_revision"],
        "idempotency_key": str(uuid4()),
    }
    return document, view, candidate, request


def _insert_test_only_legacy_receipt(database, operation, scope, request, result):
    """Seed frozen historical rows without running any retired mutation implementation."""
    identity = application_command_identity(operation, scope, request)
    native = identity["native_receipt"]
    origin_digest = "workflow-op-v1:" + identity["request_sha256"]
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        connection.execute(
            "INSERT INTO idempotency "
            "(operation,key,digest,result_refs,result_payload,request_digest) VALUES(?,?,?,?,?,?)",
            (APPLICATION_IDENTITY_OPERATION, application_origin_key(identity), origin_digest, "[]",
             canonical_bytes([{"application_identity": identity}]).decode(), origin_digest),
        )
        connection.execute(
            "INSERT INTO idempotency "
            "(operation,key,digest,result_refs,result_payload,request_digest) VALUES(?,?,?,?,?,?)",
            (native["operation"], native["key"], native["request_digest"], "[]",
             canonical_bytes([{"graph_result": result}]).decode(), native["request_digest"]),
        )


def _replace_graph_record(store, kind, identity, record):
    store._connection.execute(
        "UPDATE records SET payload=? WHERE record_type=? AND record_id=?",
        (canonical_bytes(record).decode(), kind, _record_id(kind, identity)),
    )


def _test_only_consumer_child(service, document, parent, candidate):
    """Create persisted state-shaped fixture data for old consumer receipt validation."""
    child = service.create_session(
        document["workflow_definition_id"], parent["definition_revision"], idempotency_key=str(uuid4()))
    with closing(SqliteStore(service.database)) as store:
        repo = GraphRecordStore(store)
        source_commit = repo.get("workflow_commit", commit_id=candidate["candidate_id"])
        source_snapshot = repo.get("state_snapshot", state_snapshot_id=source_commit["state_snapshot_id"])
        chain = repo.get("chain_run", chain_run_id=source_commit["source"]["chain_run_id"])
        session = repo.get("workflow_session", workflow_session_id=child["workflow_session_id"])
        seed = repo.get("workflow_commit", commit_id=child["head_commit_id"])
        snapshot = repo.get("state_snapshot", state_snapshot_id=seed["state_snapshot_id"])
        expected_origin = {
            "kind": "fork_candidate",
            "workflow_session_id": parent["workflow_session_id"],
            "object_source_session_id": source_snapshot["workflow_session_id"],
            "workflow_definition_id": source_snapshot["workflow_definition_id"],
            "definition_revision": source_snapshot["definition_revision"],
            "head_commit_id": candidate["candidate_id"],
            "candidate_commit_id": candidate["candidate_id"],
            "state_mappings": [],
        }
        session["source"] = expected_origin
        seed["source"] = {"kind": "session_seed", "source": expected_origin}
        snapshot.update(
            node_states=deepcopy(source_snapshot["node_states"]),
            data_revision=source_snapshot["data_revision"],
            history_refs=deepcopy(source_snapshot["history_refs"]),
        )
        _replace_graph_record(store, "workflow_session",
                              {"workflow_session_id": child["workflow_session_id"]}, session)
        _replace_graph_record(store, "workflow_commit", {"commit_id": seed["commit_id"]}, seed)
        _replace_graph_record(store, "state_snapshot",
                              {"state_snapshot_id": snapshot["state_snapshot_id"]}, snapshot)
    return service.get_session(child["workflow_session_id"]), chain


def _fork_result(parent, candidate, chain_id):
    result = deepcopy(parent)
    result.update(
        workflow_session_id=str(uuid4()),
        revision=1,
        source={"kind": "fork_candidate", "workflow_session_id": parent["workflow_session_id"],
                "candidate_commit_id": candidate["candidate_id"]},
        active_chain_run_id=None,
        head_revision=1,
        head_commit_id=str(uuid4()),
        selected_chain_run_id=chain_id,
        chains=[],
    )
    return result


def test_retired_candidate_writes_are_absent_from_application_http_and_service(service):
    document, view, candidate, request = _completed(service)
    application = GraphApplication(service)
    consumer = application.for_consumer()
    session_ids = {row["workflow_session_id"]
                   for row in service.list_sessions(document["workflow_definition_id"])}
    before = service.get_session(view["workflow_session_id"])

    management_commands = {row["name"] for row in application.describe()["commands"]}
    consumer_commands = {row["name"] for row in consumer.describe()["commands"]}
    assert not set(RETIRED[:2]) & management_commands
    assert "consumer.candidate.fork" not in consumer_commands
    assert not hasattr(service, "select_graph_candidate")
    assert not hasattr(service, "fork_graph_candidate")
    assert not hasattr(service, "fork_consumer_candidate")
    assert not hasattr(service, "_candidate_state")

    for operation in RETIRED:
        with pytest.raises(ContractValidationError) as denied:
            application.command(operation, {})
        assert denied.value.reason_code == "not_found"
    with pytest.raises(ContractValidationError) as denied:
        consumer.command("consumer.candidate.fork", {})
    assert denied.value.reason_code == "not_found"

    for operation in RETIRED[:2]:
        with pytest.raises(ContractValidationError) as denied:
            dispatch_graph(service, "POST", "/api/graph/commands", {
                "operation": operation, "parameters": request,
            })
        assert denied.value.reason_code == "not_found"
    with pytest.raises(ContractValidationError) as denied:
        dispatch_graph(service, "POST", "/api/graph/consumer/commands", {
            "operation": "consumer.candidate.fork", "parameters": request,
        })
    assert denied.value.reason_code == "not_found"

    for suffix in ("select", "fork"):
        with pytest.raises(ContractValidationError) as denied:
            dispatch_graph(service, "POST",
                           f"/api/graph/sessions/{view['workflow_session_id']}/candidates/{suffix}", {})
        assert denied.value.reason_code == "not_found"

    status, http_candidates = dispatch_graph(
        service, "GET", f"/api/graph/sessions/{view['workflow_session_id']}/candidates")
    assert status == 200 and http_candidates["candidates"][0]["candidate_id"] == candidate["candidate_id"]
    assert service.get_session(view["workflow_session_id"]) == before
    assert {row["workflow_session_id"]
            for row in service.list_sessions(document["workflow_definition_id"])} == session_ids

    listed = application.query("candidate.list", {"session_id": view["workflow_session_id"]})
    assert listed["candidates"][0]["candidate_id"] == candidate["candidate_id"]
    assert not listed["candidates"][0]["can_select"]
    assert listed["candidates"][0]["diagnostic"] is None
    with pytest.raises(ContractValidationError) as denied:
        consumer.query("consumer.candidate.list", {
            "session_id": view["workflow_session_id"],
            "workflow_definition_id": view["workflow_definition_id"],
            "definition_revision": view["definition_revision"],
        })
    assert denied.value.reason_code == "not_found"


def test_management_candidates_remain_readable_during_another_active_run(service):
    sid, first_chain, release = gate_graph(service)
    release.set()
    service.wait(first_chain)
    view = service.get_session(sid)
    candidate = service.list_graph_candidates(sid)["candidates"][0]
    release.clear()
    active = service.start(sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
    try:
        projected = service.list_graph_candidates(sid)
        assert not any(row["can_select"] for row in projected["candidates"])
        assert [row["candidate_id"] for row in projected["candidates"]] == [candidate["candidate_id"]]
    finally:
        release.set()
        service.wait(active["active_chain_run_id"])


def test_archived_candidate_queries_preserve_saved_rows_and_the_bound_workflow_scope(service):
    _, view, candidate, _ = _completed(service)
    sid = view["workflow_session_id"]
    archived = service.archive_session(sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
    assert archived["archived"] is True
    assert service.get_session(sid)["readonly"] is True
    # Live-host initialization still changes file-header counters, not saved rows.
    before = raw_database(service.database)
    listed = service.list_graph_candidates(sid)
    assert listed["candidates"][0]["can_select"] is False
    assert listed["candidates"][0]["candidate_id"] == candidate["candidate_id"]
    with pytest.raises(ContractValidationError) as denied:
        GraphApplication(service).for_consumer().query("consumer.candidate.list", {
            "session_id": sid, "workflow_definition_id": str(uuid4()),
            "definition_revision": view["definition_revision"],
        })
    assert denied.value.reason_code == "not_found"
    assert raw_database(service.database) == before


@pytest.mark.parametrize("operation", ["candidate.select", "candidate.fork"])
def test_retired_management_receipts_match_exact_original_request_read_only(service, operation, monkeypatch):
    _, parent, candidate, request = _completed(service)
    result = deepcopy(parent)
    if operation == "candidate.select":
        result["revision"] = request["expected_revision"] + 1
        result["head_revision"] = request["expected_head_revision"] + 1
        result["head_commit_id"] = str(uuid4())
    else:
        result = _fork_result(parent, candidate, candidate["chain_run_id"])
    _insert_test_only_legacy_receipt(service.database, operation, "management", request, result)
    before = service.database.read_bytes()

    matched = read_graph_application_receipt(service.database, operation, request, scope="management")
    assert matched["outcome"] == "matched", matched
    assert matched["result"] == result
    assert matched["receipt"]["operation"] == operation
    assert matched["receipt"]["operation_scope"] == "management"
    assert matched["receipt"]["idempotency_key"] == request["idempotency_key"]
    assert matched["receipt"]["request_sha256"] == application_command_digest(operation, request)
    assert read_graph_application_receipt(
        service.database, operation, request, scope="management") == matched
    with serve_receipts(service.database, monkeypatch) as (_, port):
        assert post(port, "/api/graph/receipts/read", {
            "operation": operation, "parameters": request,
        }) == (200, matched)

    changed = {**request, "candidate_id": str(uuid4())}
    unknown = read_graph_application_receipt(service.database, operation, changed, scope="management")
    assert unknown["outcome"] == "unresolved" and unknown["reason_code"] == "request_mismatch"
    wrong_scope = read_graph_application_receipt(service.database, operation, request, scope="consumer")
    assert wrong_scope["outcome"] == "unresolved" and wrong_scope["reason_code"] == "application_scope_denied"
    assert service.database.read_bytes() == before


def test_retired_consumer_fork_receipt_is_read_only_and_scope_bound(service, monkeypatch):
    document, parent, candidate, request = _completed(service)
    child, chain = _test_only_consumer_child(service, document, parent, candidate)
    _insert_test_only_legacy_receipt(
        service.database, "consumer.candidate.fork", "consumer", request, child)
    before = service.database.read_bytes()

    matched = read_graph_application_receipt(
        service.database, "consumer.candidate.fork", request, scope="consumer")
    assert matched["outcome"] == "matched", matched
    assert matched["result"]["receipt"]["operation"] == "fork"
    assert matched["receipt"]["operation_scope"] == "consumer"
    assert matched["receipt"]["idempotency_key"] == request["idempotency_key"]
    assert matched["receipt"]["request_sha256"] == application_command_digest(
        "consumer.candidate.fork", request)
    assert matched["result"]["receipt"]["fork_source"] == {
        "workflow_session_id": parent["workflow_session_id"],
        "candidate_id": candidate["candidate_id"],
        "chain_run_id": chain["chain_run_id"],
        "workflow_definition_id": document["workflow_definition_id"],
        "definition_revision": document["revision"],
    }
    changed = {**request, "candidate_id": str(uuid4())}
    unknown = read_graph_application_receipt(
        service.database, "consumer.candidate.fork", changed, scope="consumer")
    assert unknown["outcome"] == "unresolved" and unknown["reason_code"] == "request_mismatch"
    wrong_scope = read_graph_application_receipt(
        service.database, "candidate.fork", request, scope="consumer")
    assert wrong_scope["outcome"] == "unresolved" and wrong_scope["reason_code"] == "application_scope_denied"
    with serve_receipts(service.database, monkeypatch) as (_, port):
        assert post(port, "/api/graph/consumer/receipts/read", {
            "operation": "consumer.candidate.fork", "parameters": request,
        }) == (200, matched)
    assert service.database.read_bytes() == before


def test_cold_http_fork_receipt_survives_child_continuation_without_replay(service, monkeypatch):
    document, parent, candidate, request = _completed(service)
    child, _ = _test_only_consumer_child(service, document, parent, candidate)
    _insert_test_only_legacy_receipt(
        service.database, "consumer.candidate.fork", "consumer", request, child)
    accepted = read_graph_application_receipt(
        service.database, "consumer.candidate.fork", request, scope="consumer")
    assert accepted["outcome"] == "matched"
    continued = run(service, child)
    assert continued["status"] == "succeeded"
    service.close()
    before = raw_database(service.database)
    with serve_receipts(service.database, monkeypatch) as (host, port):
        original = {"operation": "consumer.candidate.fork", "parameters": request}
        assert post(port, "/api/graph/consumer/receipts/read", original) == (200, accepted)
        assert post(port, "/api/graph/consumer/queries", {
            "operation": "receipt.read", "parameters": original,
        }) == (200, accepted)
        changed = {**request, "candidate_id": str(uuid4())}
        assert post(port, "/api/graph/consumer/receipts/read", {
            "operation": "consumer.candidate.fork", "parameters": changed,
        })[1]["outcome"] == "unresolved"
        status, _, body = call(port, "GET", f"/tavern/?graph_workflow={document['workflow_definition_id']}")
        assert status == 410 and b"legacy_frontend_entry_retired" in body and b"<script" not in body
        assert host._graph is None
    assert raw_database(service.database) == before


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
def test_frozen_consumer_fork_rejects_forged_source_child_and_checkpoint(service, mutate):
    document, parent, candidate, request = _completed(service)
    child, _ = _test_only_consumer_child(service, document, parent, candidate)
    _insert_test_only_legacy_receipt(
        service.database, "consumer.candidate.fork", "consumer", request, child)
    matched = read_graph_application_receipt(
        service.database, "consumer.candidate.fork", request, scope="consumer")
    assert matched["outcome"] == "matched"
    with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
        raw = connection.execute(
            "SELECT result_payload FROM idempotency WHERE operation=? AND key=?",
            ("graph.candidate.fork", request["idempotency_key"]),
        ).fetchone()[0]
        payload = loads_strict(raw)
        mutate(payload[0]["graph_result"])
        connection.execute(
            "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
            (canonical_bytes(payload).decode(), "graph.candidate.fork", request["idempotency_key"]),
        )
    before = raw_database(service.database)
    cold = read_graph_application_receipt(
        service.database, "consumer.candidate.fork", request, scope="consumer")
    assert cold["outcome"] == "unresolved" and cold["reason_code"] == "receipt_invalid"
    assert raw_database(service.database) == before


def test_frozen_consumer_fork_neither_creates_missing_database_nor_accepts_management_origin(service, tmp_path):
    database = tmp_path / "absent-consumer-fork.sqlite"
    _, parent, candidate, request = _completed(service)
    assert read_graph_application_receipt(
        database, "consumer.candidate.fork", request, scope="consumer")["outcome"] == "unresolved"
    assert not database.exists()
    child, _ = _test_only_consumer_child(
        service, service.get_definition(parent["workflow_definition_id"]), parent, candidate)
    _insert_test_only_legacy_receipt(service.database, "candidate.fork", "management", request, child)
    before = raw_database(service.database)
    cold = read_graph_application_receipt(
        service.database, "consumer.candidate.fork", request, scope="consumer")
    assert cold["outcome"] == "unresolved" and cold["reason_code"] == "application_identity_missing"
    assert raw_database(service.database) == before
