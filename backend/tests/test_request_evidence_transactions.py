"""Independent evidence/business/resolution facts under lost acknowledgements."""

from contextlib import closing
import multiprocessing
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.request_evidence import RequestEvidenceService, validate_evidence_request
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow_host import WorkflowHost
from test_request_evidence import evidence


def resolution(record):
    return {**{key: record[key] for key in ("operation", "idempotency_key", "request_digest")},
            "resolution": "verified"}


def lose_ack(callback, *args, **kwargs):
    callback(*args, **kwargs)
    raise ConnectionError("synthetic acknowledgement loss after commit")


def test_evidence_commit_lost_ack_has_no_business_then_current_record_can_be_read(tmp_path):
    path = tmp_path / "evidence-ack.sqlite"
    host = WorkflowHost(path)
    app = GraphApplication(host)
    request = evidence()
    try:
        with pytest.raises(ConnectionError):
            lose_ack(app.command, "request-evidence.write", request)
        record = app.query("request-evidence.read", {
            "owner_kind": request["owner_kind"], "owner_id": request["owner_id"]})[0]
        assert record["resolution"] == "pending"
        assert record["request"] == request["request"]
        assert read_graph_application_receipt(path, "request-evidence.write", request)["outcome"] == "matched"
        assert read_graph_application_receipt(path, request["operation"], request["request"])["outcome"] == "unresolved"
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM global_resource_current").fetchone()[0] == 0
        assert host._graph is None
    finally:
        host.close()


def test_business_commits_lost_ack_resolve_rolls_back_then_resolve_ack_lost(tmp_path):
    path = tmp_path / "three-commits.sqlite"
    host, request = WorkflowHost(path), evidence()
    app = GraphApplication(host)
    try:
        record = app.command("request-evidence.write", request)["result"]
        with pytest.raises(ConnectionError):
            lose_ack(app.command, request["operation"], request["request"])
        business = read_graph_application_receipt(path, request["operation"], request["request"])
        assert business["outcome"] == "matched"

        def fail(point):
            if point == "after_request_evidence_write":
                raise RuntimeError("synthetic resolution rollback")

        broken = RequestEvidenceService(path, store_factory=lambda: SqliteStore(path, fault_injector=fail))
        with pytest.raises(RuntimeError, match="resolution rollback"):
            broken.resolve_evidence(**resolution(record))
        assert app.query("request-evidence.read", {
            "owner_kind": record["owner_kind"], "owner_id": record["owner_id"]}) == [record]
        assert read_graph_application_receipt(path, request["operation"], request["request"]) == business
        assert read_graph_application_receipt(path, "request-evidence.resolve", resolution(record))["outcome"] == "unresolved"
        with pytest.raises(ConnectionError):
            lose_ack(app.command, "request-evidence.resolve", resolution(record))
        current = app.query("request-evidence.read", {
            "owner_kind": record["owner_kind"], "owner_id": record["owner_id"]})
        assert current == [{**record, "resolution": "verified"}]
        assert read_graph_application_receipt(path, "request-evidence.resolve", resolution(record))["outcome"] == "matched"
        assert read_graph_application_receipt(path, request["operation"], request["request"]) == business
    finally:
        host.close()


def test_definite_business_rejection_can_be_verified_without_success_receipt(tmp_path):
    path = tmp_path / "rejected.sqlite"
    host, request = WorkflowHost(path), evidence()
    request["request"]["expected_sequence"] = 55
    request["request"]["record"]["update_sequence"] = 56
    app = GraphApplication(host)
    try:
        record = app.command("request-evidence.write", request)["result"]
        with pytest.raises(ContractValidationError) as rejected:
            app.command(request["operation"], request["request"])
        assert rejected.value.reason_code == "stale_revision"
        assert read_graph_application_receipt(path, request["operation"], request["request"])["outcome"] == "unresolved"
        assert app.command("request-evidence.resolve", resolution(record))["result"]["resolution"] == "verified"
    finally:
        host.close()


def test_frozen_write_replay_a_never_changes_new_pending_b(tmp_path):
    path = tmp_path / "frozen.sqlite"
    app = GraphApplication(WorkflowHost(path))
    first = evidence()
    original = app.command("request-evidence.write", first)
    app.command("request-evidence.resolve", resolution(original["result"]))
    second = evidence(owner=first["owner_id"])
    pending = app.command("request-evidence.write", second)["result"]
    with closing(sqlite3.connect(path)) as connection:
        before = list(connection.iterdump())
    assert app.command("request-evidence.write", first) == original
    assert read_graph_application_receipt(path, "request-evidence.write", first)["result"] == original["result"]
    records = app.query("request-evidence.read", {
        "owner_kind": first["owner_kind"], "owner_id": first["owner_id"]})
    assert records == [{**original["result"], "resolution": "verified"}, pending]
    with closing(sqlite3.connect(path)) as connection:
        assert list(connection.iterdump()) == before


def _compete(database, request, barrier, queue):
    service = RequestEvidenceService(database)
    barrier.wait(timeout=15)
    try:
        record = service.write_evidence(**request)
        queue.put(("saved", record["idempotency_key"]))
    except ContractValidationError as error:
        queue.put((error.reason_code, request["idempotency_key"]))


def test_two_process_same_owner_competition_has_one_pending(tmp_path):
    path = tmp_path / "competition.sqlite"
    with closing(SqliteStore(path)):
        pass
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    requests = [evidence(owner="same-owner") for _ in range(2)]
    workers = [context.Process(target=_compete, args=(str(path), request, barrier, queue))
               for request in requests]
    try:
        for worker in workers:
            worker.start()
        results = [queue.get(timeout=25) for _ in workers]
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive() and worker.exitcode == 0
        assert sorted(row[0] for row in results) == ["request_evidence_pending", "saved"]
        rows = RequestEvidenceService(path).read_evidence("resource-channel", "same-owner")
        assert len(rows) == 1 and rows[0]["idempotency_key"] == next(key for status, key in results if status == "saved")
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        queue.close()


def test_single_request_canonical_byte_boundary_is_exact(tmp_path):
    request = {"workflow_definition_id": str(uuid4()), "document": {"value": ""},
               "expected_edit_sequence": 0, "idempotency_key": "capacity"}
    request["document"]["value"] = "x" * (3_900_000 - len(canonical_bytes(request)))
    assert len(canonical_bytes(request)) == 3_900_000
    digest = validate_evidence_request("definition.update", "capacity", request)
    service = RequestEvidenceService(tmp_path / "capacity.sqlite")
    saved = service.write_evidence("definition.update", "capacity", "workflow-configuration",
                                  request["workflow_definition_id"], request)
    assert saved["request_digest"] == digest
    request["document"]["value"] += "x"
    with pytest.raises(ContractValidationError) as error:
        validate_evidence_request("definition.update", "capacity", request)
    assert error.value.reason_code == "request_evidence_capacity_exceeded"
    assert error.value.status_code == 413


def test_reopen_discovers_configuration_copy_and_delete_after_both_workflows_are_gone(tmp_path):
    from test_graph_service import text_graph
    from test_graph_receipt_roundtrip import fixed_configuration
    from test_workflow_configuration_copy import copy_request

    path = tmp_path / "orphan-workflows.sqlite"
    requests = []
    with closing(WorkflowHost(path)) as host:
        app = GraphApplication(host)
        document = fixed_configuration(text_graph(host.graph_management_service.registry))
        source = document["workflow_definition_id"]
        save = {"workflow_definition_id": source, "document": document,
                "expected_edit_sequence": 0, "idempotency_key": str(uuid4())}
        copy = copy_request(source)
        remove = {"identity": source, "expected_revision": 1, "idempotency_key": str(uuid4())}
        for kind, operation, request in (
            ("workflow-configuration", "definition.update", save),
            ("workflow-configuration-copy", "definition.copy", copy),
            ("workflow-management", "definition.delete", remove),
        ):
            evidence_request = {"operation": operation, "idempotency_key": request["idempotency_key"],
                                "owner_kind": kind, "owner_id": source, "request": request}
            record = app.command("request-evidence.write", evidence_request)["result"]
            with pytest.raises(ConnectionError):
                lose_ack(app.command, operation, request)
            requests.append(record)
        app.command("definition.delete", {"identity": copy["target_workflow_definition_id"],
                    "expected_revision": 1, "idempotency_key": str(uuid4())})
        assert app.query("definition.summaries", {"include_archived": True}) == []
    with closing(WorkflowHost(path)) as host:
        app = GraphApplication(host)
        page = app.query("request-evidence.pending")
        assert len(page["items"]) == 3
        assert host._graph is None and host._management_graph is None
        for record in requests:
            assert any(row["request_digest"] == record["request_digest"] for row in page["items"])
            detail = app.query("request-evidence.read", {
                "owner_kind": record["owner_kind"], "owner_id": record["owner_id"]})
            assert detail == [record]
            receipt = app.query("receipt.read", {"operation": record["operation"],
                                                 "parameters": record["request"]})
            assert receipt["outcome"] == "matched"
            app.command("request-evidence.resolve", resolution(record))
        assert app.query("request-evidence.pending")["items"] == []
        assert host._graph is None and host._management_graph is None
        assert app.query("definition.summaries", {"include_archived": True}) == []
