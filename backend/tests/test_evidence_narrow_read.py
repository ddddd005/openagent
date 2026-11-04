"""Q07 narrow request-evidence reads and the layered pending summary.

The full owner-history read keeps working.  The two narrow reads return complete
records (owner/operation/key/digest/resolution plus the full original body) for
exactly one condition, and a damaged or non-unique stored state raises instead of
looking like "no pending request".  Nothing is deleted, migrated or rewritten.
"""

from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_identity import application_command_digest
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.request_evidence import RequestEvidenceService
from test_configuration_application import configuration_document, prompt_registry


OPERATION = "definition.update"
OWNER_KIND = "workflow-configuration"


def evidence_request(document, *, key=None):
    return {"workflow_definition_id": document["workflow_definition_id"],
            "document": deepcopy(document), "expected_edit_sequence": 0,
            "idempotency_key": key or str(uuid4())}


def write_parameters(request):
    return {"operation": OPERATION, "idempotency_key": request["idempotency_key"],
            "owner_kind": OWNER_KIND, "owner_id": request["workflow_definition_id"],
            "request": deepcopy(request)}


def read_parameters(record, **extra):
    return {"owner_kind": record["owner_kind"], "owner_id": record["owner_id"], **extra}


def open_application(path, registry):
    service = GraphWorkflowService(path, registry=registry)
    return service, GraphApplication(service)


def assert_reason(call, code, status=None):
    with pytest.raises(ContractValidationError) as error:
        call()
    assert error.value.reason_code == code
    if status is not None:
        assert error.value.status_code == status


def owner_rows(path, owner_id):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT operation,idempotency_key,request_json,request_digest,resolution "
            "FROM pending_commands WHERE owner_id=? ORDER BY rowid", (owner_id,)).fetchall()


def test_point_read_returns_the_complete_record_and_is_owner_scoped(tmp_path):
    path, registry = tmp_path / "narrow-point.sqlite", prompt_registry()
    service, app = open_application(path, registry)
    try:
        first = evidence_request(configuration_document(registry))
        second = evidence_request(configuration_document(registry))
        written = app.command("request-evidence.write", write_parameters(first))["result"]
        other = app.command("request-evidence.write", write_parameters(second))["result"]

        point = app.query("request-evidence.read", read_parameters(
            written, operation=OPERATION, idempotency_key=written["idempotency_key"]))
        assert point == [written]
        assert point[0]["request"] == first
        assert point[0]["request_digest"] == written["request_digest"]

        resolved = app.command("request-evidence.resolve", {
            "operation": OPERATION, "idempotency_key": written["idempotency_key"],
            "request_digest": written["request_digest"], "resolution": "verified"})["result"]
        assert app.query("request-evidence.read", read_parameters(
            written, operation=OPERATION, idempotency_key=written["idempotency_key"])) == [resolved]

        missing = app.query("request-evidence.read", read_parameters(
            written, operation=OPERATION, idempotency_key=str(uuid4())))
        assert missing == []
        assert app.query("request-evidence.read", read_parameters(
            other, operation=OPERATION, idempotency_key=other["idempotency_key"])) == [other]

        # The key exists globally, so reading it under the other owner is a conflict,
        # never an empty "no request" answer.
        assert_reason(lambda: app.query("request-evidence.read", read_parameters(
            other, operation=OPERATION, idempotency_key=written["idempotency_key"])),
            "request_evidence_conflict", 409)
        assert owner_rows(path, written["owner_id"])[0][4] == "verified"
        assert owner_rows(path, other["owner_id"])[0][4] == "pending"
    finally:
        service.close()


def test_pending_narrow_read_returns_only_the_unresolved_request_with_full_body(tmp_path):
    path, registry = tmp_path / "narrow-pending.sqlite", prompt_registry()
    service, app = open_application(path, registry)
    try:
        first = evidence_request(configuration_document(registry))
        written = app.command("request-evidence.write", write_parameters(first))["result"]
        unrelated = evidence_request(configuration_document(registry))
        app.command("request-evidence.write", write_parameters(unrelated))

        pending = app.query("request-evidence.read", read_parameters(written, resolution="pending"))
        assert pending == [written]
        assert pending[0]["request"] == first

        app.command("request-evidence.resolve", {
            "operation": OPERATION, "idempotency_key": written["idempotency_key"],
            "request_digest": written["request_digest"], "resolution": "verified"})
        assert app.query("request-evidence.read", read_parameters(written, resolution="pending")) == []

        follow_up = {**first, "idempotency_key": str(uuid4())}
        pending_again = app.command("request-evidence.write", write_parameters(follow_up))["result"]
        rows = app.query("request-evidence.read", read_parameters(written, resolution="pending"))
        assert rows == [pending_again]
        # The verified historical row is still readable through the full owner read.
        full = app.query("request-evidence.read", read_parameters(written))
        assert [row["resolution"] for row in full] == ["verified", "pending"]
        assert [row["idempotency_key"] for row in full] == [
            written["idempotency_key"], pending_again["idempotency_key"]]
    finally:
        service.close()


def test_full_owner_read_is_unchanged_and_narrow_reads_write_nothing(tmp_path):
    path, registry = tmp_path / "narrow-compat.sqlite", prompt_registry()
    service, app = open_application(path, registry)
    try:
        request = evidence_request(configuration_document(registry))
        written = app.command("request-evidence.write", write_parameters(request))["result"]
        before = owner_rows(path, written["owner_id"])
        app.query("request-evidence.read", read_parameters(written))
        app.query("request-evidence.read", read_parameters(written, resolution="pending"))
        app.query("request-evidence.read", read_parameters(
            written, operation=OPERATION, idempotency_key=written["idempotency_key"]))
        assert owner_rows(path, written["owner_id"]) == before
        assert len(before) == 1 and before[0][4] == "pending"
        # The full-history shape keeps its insertion order and complete records.
        assert app.query("request-evidence.read", read_parameters(written)) == [written]
    finally:
        service.close()


def test_owner_scoped_pending_page_is_a_bodyless_summary(tmp_path):
    path, registry = tmp_path / "narrow-page.sqlite", prompt_registry()
    service, app = open_application(path, registry)
    try:
        first = evidence_request(configuration_document(registry))
        written = app.command("request-evidence.write", write_parameters(first))["result"]
        unrelated = evidence_request(configuration_document(registry))
        other = app.command("request-evidence.write", write_parameters(unrelated))["result"]

        page = app.query("request-evidence.pending", {
            "owner_kind": written["owner_kind"], "owner_id": written["owner_id"]})
        assert page["schema_version"] == 1 and page["kind"] == "workflow.request-evidence-page"
        assert page["next_cursor"] is None
        assert page["items"] == [{
            "owner_kind": written["owner_kind"], "owner_id": written["owner_id"],
            "operation": OPERATION, "idempotency_key": written["idempotency_key"],
            "request_digest": written["request_digest"], "resolution": "pending"}]
        # The summary never carries the original body.
        assert "request" not in page["items"][0]

        global_page = app.query("request-evidence.pending", {"limit": 50})
        coordinates = {(row["owner_kind"], row["owner_id"]) for row in global_page["items"]}
        assert coordinates == {(written["owner_kind"], written["owner_id"]),
                               (other["owner_kind"], other["owner_id"])}

        app.command("request-evidence.resolve", {
            "operation": OPERATION, "idempotency_key": written["idempotency_key"],
            "request_digest": written["request_digest"], "resolution": "verified"})
        assert app.query("request-evidence.pending", {
            "owner_kind": written["owner_kind"], "owner_id": written["owner_id"]})["items"] == []
    finally:
        service.close()


def _insert_raw_row(path, *, operation, key, owner_kind, owner_id, request, resolution,
                    digest=None):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO pending_commands VALUES(?,?,?,?,?,?,?)",
            (operation, key, owner_kind, owner_id,
             canonical_bytes(request).decode("utf-8"),
             digest if digest is not None else application_command_digest(operation, request),
             resolution))
        connection.commit()


class _FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _statement, _parameters):
        return self

    def fetchall(self):
        return list(self._rows)


class _FakeStore:
    """Read-only store stand-in for the narrow-read uniqueness guard."""

    def __init__(self, rows):
        self._connection = _FakeConnection(rows)

    def close(self):
        pass


def _stored_row(evidence, operation, request):
    return {"operation": operation, "idempotency_key": evidence["idempotency_key"],
            "owner_kind": evidence["owner_kind"], "owner_id": evidence["owner_id"],
            "request_digest": evidence["request_digest"], "resolution": evidence["resolution"],
            "request_json": canonical_bytes(request).decode("utf-8")}


def test_non_unique_pending_state_is_reported_not_treated_as_empty(tmp_path):
    path, registry = tmp_path / "narrow-nonunique.sqlite", prompt_registry()
    service, app = open_application(path, registry)
    try:
        first = evidence_request(configuration_document(registry))
        written = app.command("request-evidence.write", write_parameters(first))["result"]
    finally:
        service.close()
    # The owner-scoped summary reads the table without the storage initialization,
    # so an index-level anomaly (dropped index, two pending rows) is observable.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX pending_command_owner")
        connection.commit()
    second = {**first, "idempotency_key": str(uuid4())}
    _insert_raw_row(path, operation=OPERATION, key=second["idempotency_key"],
                    owner_kind=OWNER_KIND, owner_id=written["owner_id"],
                    request=second, resolution="pending")
    from phase1_agent.request_evidence import read_workflow_pending

    assert_reason(lambda: read_workflow_pending(
        path, owner_kind=OWNER_KIND, owner_id=written["owner_id"]),
        "storage_contract_violation", 500)
    # The service-level narrow read keeps the same explicit guard.  A store whose
    # rows violate the single-pending guarantee must not answer with one row.
    rows = [_stored_row(written, OPERATION, first),
            _stored_row({**written, "idempotency_key": second["idempotency_key"],
                         "request_digest": application_command_digest(OPERATION, second)},
                        OPERATION, second)]
    fake = RequestEvidenceService(path, store_factory=lambda: _FakeStore(rows))
    assert_reason(lambda: fake.read_evidence(OWNER_KIND, written["owner_id"],
                                             resolution="pending"),
                  "storage_contract_violation", 500)
    full = fake.read_evidence(OWNER_KIND, written["owner_id"])
    assert len(full) == 2 and {row["resolution"] for row in full} == {"pending"}


def test_damaged_stored_evidence_raises_instead_of_looking_missing(tmp_path):
    path, registry = tmp_path / "narrow-damaged.sqlite", prompt_registry()
    service, app = open_application(path, registry)
    try:
        request = evidence_request(configuration_document(registry))
        written = app.command("request-evidence.write", write_parameters(request))["result"]
    finally:
        service.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE pending_commands SET request_json=? WHERE operation=? AND idempotency_key=?",
            ('{"broken":', OPERATION, written["idempotency_key"]))
        connection.commit()
    service, app = open_application(path, registry)
    try:
        assert_reason(lambda: app.query("request-evidence.read", {
            "owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
            "operation": OPERATION, "idempotency_key": written["idempotency_key"]}),
            "storage_contract_violation", 500)
        assert_reason(lambda: app.query("request-evidence.read", {
            "owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
            "resolution": "pending"}), "storage_contract_violation", 500)
        assert_reason(lambda: app.query("request-evidence.read", {
            "owner_kind": OWNER_KIND, "owner_id": written["owner_id"]}),
            "storage_contract_violation", 500)
    finally:
        service.close()
    with sqlite3.connect(path) as connection:
        # Restore the body, then break only the stored digest.
        connection.execute(
            "UPDATE pending_commands SET request_json=? WHERE operation=? AND idempotency_key=?",
            (canonical_bytes(request).decode("utf-8"), OPERATION, written["idempotency_key"]))
        connection.execute(
            "UPDATE pending_commands SET request_digest=? WHERE operation=? AND idempotency_key=?",
            ("f" * 64, OPERATION, written["idempotency_key"]))
        connection.commit()
    service, app = open_application(path, registry)
    try:
        # A digest that no longer matches its stored body is reported by the full
        # record decoding, never accepted as a small inconsistency.
        assert_reason(lambda: app.query("request-evidence.read", {
            "owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
            "operation": OPERATION, "idempotency_key": written["idempotency_key"]}),
            "storage_contract_violation", 500)
        assert_reason(lambda: app.query("request-evidence.read", {
            "owner_kind": OWNER_KIND, "owner_id": written["owner_id"]}),
            "storage_contract_violation", 500)
    finally:
        service.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE pending_commands SET request_digest=? WHERE operation=? AND idempotency_key=?",
            ("not-a-hex-digest", OPERATION, written["idempotency_key"]))
        connection.commit()
    service, app = open_application(path, registry)
    try:
        # The summary reads field shapes only; a malformed field is an explicit
        # storage error rather than a "no pending request" answer.
        assert_reason(lambda: app.query("request-evidence.pending", {
            "owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
            "limit": 2}), "storage_contract_violation", 500)
    finally:
        service.close()


def test_narrow_read_parameters_are_validated(tmp_path):
    path, registry = tmp_path / "narrow-invalid.sqlite", prompt_registry()
    service, app = open_application(path, registry)
    try:
        request = evidence_request(configuration_document(registry))
        written = app.command("request-evidence.write", write_parameters(request))["result"]
        base = read_parameters(written)
        assert_reason(lambda: app.query("request-evidence.read",
                                        {**base, "operation": OPERATION}), "invalid_request", 400)
        assert_reason(lambda: app.query("request-evidence.read",
                                        {**base, "idempotency_key": str(uuid4())}), "invalid_request", 400)
        assert_reason(lambda: app.query("request-evidence.read",
                                        {**base, "resolution": "verified"}), "invalid_request", 400)
        assert_reason(lambda: app.query("request-evidence.read", {
            **base, "resolution": "pending", "operation": OPERATION,
            "idempotency_key": written["idempotency_key"]}), "invalid_request", 400)
        assert_reason(lambda: app.query("request-evidence.pending", {
            "owner_kind": OWNER_KIND}), "invalid_request", 400)
        assert_reason(lambda: app.query("request-evidence.pending", {
            "owner_kind": "foreign-owner", "owner_id": written["owner_id"]}),
            "invalid_request", 400)
        assert_reason(lambda: app.query("request-evidence.pending", {
            "owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
            "cursor": {"owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
                       "operation": OPERATION, "idempotency_key": written["idempotency_key"]}}),
            "invalid_request", 400)
        # The unchanged full read still accepts the original two-field query.
        assert app.query("request-evidence.read", base) == [written]
    finally:
        service.close()
