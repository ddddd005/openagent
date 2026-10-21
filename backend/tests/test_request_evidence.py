from contextlib import closing
from copy import deepcopy
import importlib.util
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_identity import application_command_digest
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.request_evidence import RequestEvidenceService
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow_host import WorkflowHost
from test_content_configuration import prompt_record, reference


def resource_request():
    return {"record": prompt_record(), "expected_sequence": 0, "idempotency_key": str(uuid4())}


def evidence(request=None, owner="resource:prompts"):
    request = request or resource_request()
    return {"operation": "resource.save", "idempotency_key": request["idempotency_key"],
            "owner_kind": "resource-channel", "owner_id": owner, "request": request}


def test_write_reopen_resolve_retains_exact_request_without_dispatch(tmp_path):
    path = tmp_path / "evidence.sqlite"
    host = WorkflowHost(path)
    app = GraphApplication(host)
    parameters = evidence()
    original = deepcopy(parameters)
    saved = app.command("request-evidence.write", parameters)
    record = saved["result"]
    assert parameters == original
    assert host._graph is None and host._providers is None and host._content is None
    assert record["resolution"] == "pending" and record["request"] == parameters["request"]
    assert len(record["request_digest"]) == 64
    assert saved["receipt"]["accepted"]["request_digest"] == record["request_digest"]
    reopened = GraphApplication(WorkflowHost(path))
    assert reopened.query("request-evidence.read", {
        "owner_kind": "resource-channel", "owner_id": parameters["owner_id"]}) == [record]
    assert reopened.command("request-evidence.write", parameters) == saved
    resolution = {"operation": record["operation"], "idempotency_key": record["idempotency_key"],
                  "request_digest": record["request_digest"], "resolution": "verified"}
    resolved = reopened.command("request-evidence.resolve", resolution)["result"]
    assert resolved == {**record, "resolution": "verified"}
    assert reopened.query("request-evidence.read", {
        "owner_kind": record["owner_kind"], "owner_id": record["owner_id"]}) == [resolved]
    assert read_graph_application_receipt(path, "request-evidence.write", parameters)["result"] == record
    assert read_graph_application_receipt(path, "request-evidence.resolve", resolution)["result"] == resolved
    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("SELECT COUNT(*) FROM local_editor_drafts").fetchone()[0] == 0
        assert store._connection.execute("SELECT COUNT(*) FROM global_resource_current").fetchone()[0] == 0


def test_single_pending_owner_rejects_second_page_and_owner_reassignment(tmp_path):
    service = RequestEvidenceService(tmp_path / "evidence.sqlite")
    first = evidence()
    service.write_evidence(**first)
    with pytest.raises(ContractValidationError) as blocked:
        service.write_evidence(**evidence(owner=first["owner_id"]))
    assert blocked.value.reason_code == "request_evidence_pending"
    for replacement in ({**first, "owner_id": "different-owner"},
                        {**first, "request": {**first["request"], "expected_sequence": 12}}):
        with pytest.raises(ContractValidationError) as conflict:
            service.write_evidence(**replacement)
        assert conflict.value.reason_code == "idempotency_conflict"
    assert service.read_evidence(first["owner_kind"], first["owner_id"])[0]["request"] == first["request"]


def test_archive_keeps_request_and_prevents_resolution_reinterpretation(tmp_path):
    service = RequestEvidenceService(tmp_path / "evidence.sqlite")
    first = evidence()
    record = service.write_evidence(**first)
    parameters = {key: record[key] for key in ("operation", "idempotency_key", "request_digest")}
    with pytest.raises(ContractValidationError) as wrong_digest:
        service.resolve_evidence(**{**parameters, "request_digest": "0" * 64}, resolution="archived")
    assert wrong_digest.value.reason_code == "request_evidence_conflict"
    archived = service.resolve_evidence(**parameters, resolution="archived")
    assert archived["request"] == first["request"]
    assert service.resolve_evidence(**parameters, resolution="archived") == archived
    with pytest.raises(ContractValidationError):
        service.resolve_evidence(**parameters, resolution="verified")
    next_request = evidence()
    service.write_evidence(**next_request)
    assert len(service.read_evidence(first["owner_kind"], first["owner_id"])) == 2


def test_same_business_key_is_namespaced_by_operation(tmp_path):
    service = RequestEvidenceService(tmp_path / "evidence.sqlite")
    first = evidence()
    service.write_evidence(**first)
    name_request = {"reference": reference(first["request"]["record"]), "display_name": "Display name",
                    "expected_metadata_sequence": 0, "idempotency_key": first["idempotency_key"]}
    second = {**first, "operation": "content.metadata.update", "owner_id": "resource:names",
              "request": name_request}
    service.write_evidence(**second)
    assert service.read_evidence(second["owner_kind"], second["owner_id"])[0]["request"] == name_request


def test_application_origins_and_receipts_namespace_the_same_business_key(tmp_path):
    path = tmp_path / "evidence.sqlite"
    app = GraphApplication(WorkflowHost(path))
    first = evidence()
    name_request = {"reference": reference(first["request"]["record"]), "display_name": "Display name",
                    "expected_metadata_sequence": 0, "idempotency_key": first["idempotency_key"]}
    second = {**first, "operation": "content.metadata.update", "owner_id": "resource:names",
              "request": name_request}
    for parameters in (first, second):
        record = app.command("request-evidence.write", parameters)["result"]
        assert read_graph_application_receipt(path, "request-evidence.write", parameters)["result"] == record
        resolved_request = {key: record[key] for key in ("operation", "idempotency_key", "request_digest")}
        resolved_request["resolution"] = "verified"
        resolved = app.command("request-evidence.resolve", resolved_request)["result"]
        assert read_graph_application_receipt(path, "request-evidence.resolve", resolved_request)["result"] == resolved


@pytest.mark.parametrize("field", ["secret", "API-Key", "authorization", "password"])
def test_secrets_rejected_without_persisting_evidence(tmp_path, field):
    path = tmp_path / "evidence.sqlite"
    request = resource_request()
    request["record"]["value"]["members"][0]["metadata"] = {field: "synthetic-do-not-store"}
    service = RequestEvidenceService(path)
    with pytest.raises(ContractValidationError) as failure:
        service.write_evidence(**evidence(request))
    assert failure.value.reason_code == "request_evidence_secret_disallowed"
    assert service.read_evidence("resource-channel", "resource:prompts") == []


def test_consumer_denied_and_invalid_or_nested_commands_not_stored(tmp_path):
    app = GraphApplication(WorkflowHost(tmp_path / "evidence.sqlite"))
    with pytest.raises(ContractValidationError) as denied:
        app.for_consumer().command("request-evidence.write", evidence())
    assert denied.value.reason_code == "application_scope_denied"
    for parameters in ({**evidence(), "operation": "request-evidence.write"},
                       {**evidence(), "request": {"idempotency_key": str(uuid4())}}):
        with pytest.raises(ContractValidationError):
            app.command("request-evidence.write", parameters)
    assert app.query("request-evidence.read", {
        "owner_kind": "resource-channel", "owner_id": "resource:prompts"}) == []


def test_evidence_failure_rolls_back_and_reset_preserves_all_resolutions(tmp_path):
    path = tmp_path / "evidence.sqlite"

    def fault(point):
        if point == "after_request_evidence_write":
            raise RuntimeError("injected evidence failure")

    broken = RequestEvidenceService(path, store_factory=lambda: SqliteStore(path, fault_injector=fault))
    with pytest.raises(RuntimeError, match="injected"):
        broken.write_evidence(**evidence())
    assert broken.read_evidence("resource-channel", "resource:prompts") == []
    workspace = tmp_path / "reset-scope"
    database = workspace / ".local" / "develop" / "workflow.sqlite"
    service = RequestEvidenceService(database)
    for index, resolution in enumerate(("pending", "verified", "archived")):
        record = service.write_evidence(**evidence(owner=f"owner:{index}"))
        if resolution != "pending":
            service.resolve_evidence(operation=record["operation"], idempotency_key=record["idempotency_key"],
                                     request_digest=record["request_digest"], resolution=resolution)
    spec = importlib.util.spec_from_file_location(
        "evidence_reset", Path(__file__).parents[1] / "scripts" / "reset_development_data.py")
    reset = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reset)
    report = reset.reset_development_database(workspace)
    assert report["after"]["pending_commands"] == 0
    assert report["evidence_counts"][str(database.resolve())]["pending_commands"] == 3


def _stored_definition_save_row(resolution, *, owner_kind, owner_id, document=None):
    request = {"document": {"workflow_definition_id": str(uuid4()), "revision": 1, "nodes": []}
               if document is None else document,
               "expected_revision": 0, "idempotency_key": str(uuid4())}
    return (resolution, {
        "operation": "definition.save", "idempotency_key": request["idempotency_key"],
        "owner_kind": owner_kind, "owner_id": owner_id, "request": request,
        "request_digest": application_command_digest("definition.save", request),
    })


def _insert_evidence_rows(database, records):
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        for resolution, record in records:
            connection.execute(
                "INSERT INTO pending_commands VALUES(?,?,?,?,?,?,?)",
                (record["operation"], record["idempotency_key"], record["owner_kind"],
                 record["owner_id"], canonical_bytes(record["request"]).decode("utf-8"),
                 record["request_digest"], resolution),
            )


def test_stored_definition_save_evidence_decodes_after_receipt_contract_retirement(tmp_path):
    path = tmp_path / "legacy-definition-save-evidence.sqlite"
    service = RequestEvidenceService(path)
    assert service.read_evidence("workbench", "mixed-owner") == []
    mixed_owner = {"owner_kind": "workbench", "owner_id": "mixed-owner"}
    legacy = [
        _stored_definition_save_row("verified", **mixed_owner),
        _stored_definition_save_row("archived", **mixed_owner),
    ]
    _insert_evidence_rows(path, legacy)
    # A current, writable evidence record shares the same owner with the frozen
    # definition.save history; the mixed read must decode both.
    current_request = resource_request()
    current = service.write_evidence(
        "resource.save", current_request["idempotency_key"], "workbench", "mixed-owner",
        current_request)
    stored = service.read_evidence(**mixed_owner)
    assert [row["resolution"] for row in stored] == ["verified", "archived", "pending"]
    assert stored[0]["request"] == legacy[0][1]["request"]
    assert stored[1]["request"] == legacy[1][1]["request"]
    assert stored[2] == current

    legacy_pending = _stored_definition_save_row(
        "pending", owner_kind="workbench", owner_id="legacy-pending-owner")
    _insert_evidence_rows(path, [legacy_pending])
    pending = service.read_evidence("workbench", "legacy-pending-owner")
    assert len(pending) == 1 and pending[0]["resolution"] == "pending"
    assert pending[0]["request"] == legacy_pending[1]["request"]

    # New executable evidence for the retired operation stays rejected while the
    # stored decode above keeps working.
    with pytest.raises(ContractValidationError) as error:
        service.write_evidence(
            "definition.save", str(uuid4()), "workbench", "new-owner",
            {"document": {"workflow_definition_id": str(uuid4()), "revision": 1, "nodes": []},
             "expected_revision": 0, "idempotency_key": str(uuid4())})
    assert error.value.reason_code == "invalid_request"

    # Genuine corruption is reported, not skipped as an unreadable extra row.
    corrupt = deepcopy(legacy[1][1]["request"])
    corrupt["expected_revision"] = 1
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute(
            "UPDATE pending_commands SET request_json=? WHERE operation='definition.save' AND idempotency_key=?",
            (canonical_bytes(corrupt).decode("utf-8"), legacy[1][1]["idempotency_key"]),
        )
    with pytest.raises(ContractValidationError) as corrupt_error:
        service.read_evidence(**mixed_owner)
    assert corrupt_error.value.reason_code == "storage_contract_violation"
