"""Workflow pending discovery is bounded, body-free, and never initializes storage."""

from contextlib import closing
from hashlib import sha256
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.request_evidence import initialize_request_evidence
from phase1_agent.workflow_host import WorkflowHost
from test_graph_receipt_http import post, serve_receipts


KINDS = ("workflow-configuration", "workflow-configuration-copy", "workflow-management")
FIELDS = ("owner_kind", "owner_id", "operation", "idempotency_key", "request_digest", "resolution")


def seed(path, count=9):
    rows = []
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        initialize_request_evidence(connection)
        for index in range(count):
            row = {"owner_kind": KINDS[index % 3], "owner_id": str(uuid4()),
                   "operation": ("definition.update", "definition.copy", "session.create")[index % 3],
                   "idempotency_key": f"request-{index}", "request_digest": f"{index:064x}",
                   "resolution": "pending"}
            connection.execute("INSERT INTO pending_commands VALUES(?,?,?,?,?,?,?)", (
                row["operation"], row["idempotency_key"], row["owner_kind"], row["owner_id"],
                '{"private":"synthetic-body-not-for-discovery"}', row["request_digest"], "pending"))
            rows.append(row)
    return sorted(rows, key=lambda row: tuple(row[key] for key in FIELDS[:4]))


def query(path, **parameters):
    host = WorkflowHost(path)
    try:
        result = GraphApplication(host).query("request-evidence.pending", parameters)
        assert host._graph is None and host._management_graph is None
        return result
    finally:
        host.close()


def test_new_environment_and_old_empty_schema_are_not_created_or_migrated(tmp_path):
    missing = tmp_path / "uncreated" / "workflow.sqlite"
    assert query(missing) == {"schema_version": 1, "kind": "workflow.request-evidence-page",
                              "items": [], "next_cursor": None}
    assert not missing.parent.exists()
    old = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(old)) as connection:
        connection.execute("PRAGMA user_version=1")
    before = old.read_bytes()
    assert query(old)["items"] == []
    assert old.read_bytes() == before


def test_keyset_pages_keep_orphan_owners_and_exclude_other_domains_without_payload_reads(tmp_path, monkeypatch):
    path = tmp_path / "discovery.sqlite"
    expected = seed(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        for owner_kind, resolution in (("resource-channel", "pending"),
                                      ("frontend-node", "pending"), (KINDS[0], "verified")):
            connection.execute("INSERT INTO pending_commands VALUES(?,?,?,?,?,?,?)", (
                "resource.save", str(uuid4()), owner_kind, str(uuid4()), "not-json",
                "b" * 64, resolution))
    original_connect = sqlite3.connect
    observed = []

    def guarded_connect(*args, **kwargs):
        assert "mode=ro" in str(args[0]) and kwargs.get("uri") is True
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(observed.append)

        def authorize(action, first, second, *_):
            if action == sqlite3.SQLITE_READ and first == "pending_commands" and second == "request_json":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        return connection

    before = sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    items, cursor = [], None
    while True:
        page = query(path, limit=2, **({"cursor": cursor} if cursor else {}))
        assert page["kind"] == "workflow.request-evidence-page"
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert cursor == {key: items[-1][key] for key in FIELDS[:4]}
    assert items == expected
    assert "synthetic-body" not in repr(items)
    assert all(set(item) == set(FIELDS) for item in items)
    assert "PRAGMA query_only=ON" in observed and "BEGIN" in observed
    assert sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("parameters", [
    {"limit": 0}, {"limit": 101}, {"limit": True}, {"cursor": {}},
    {"cursor": {"owner_kind": [], "owner_id": str(uuid4()),
                "operation": "definition.update", "idempotency_key": "key"}},
    {"owner_kind": "resource-channel"}, {"cursor": {"owner_kind": "frontend-node",
     "owner_id": str(uuid4()), "operation": "definition.update", "idempotency_key": "key"}},
])
def test_bad_page_requests_are_400_without_touching_storage(tmp_path, parameters):
    with pytest.raises(ContractValidationError) as error:
        query(tmp_path / "missing.sqlite", **parameters)
    assert error.value.status_code == 400
    assert not (tmp_path / "missing.sqlite").exists()


def test_consumer_scope_denied(tmp_path):
    host = WorkflowHost(tmp_path / "missing.sqlite")
    try:
        with pytest.raises(ContractValidationError) as error:
            GraphApplication(host).for_consumer().query("request-evidence.pending")
        assert error.value.status_code == 403
    finally:
        host.close()


@pytest.mark.parametrize("field,value", [("request_digest", "synthetic-secret-bad-digest"),
                                        ("owner_id", "synthetic-secret-bad-owner")])
def test_bad_summary_metadata_is_safe_500_not_silently_dropped(tmp_path, field, value):
    path = tmp_path / "bad-summary.sqlite"
    seed(path, 1)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute(f"UPDATE pending_commands SET {field}=?", (value,))
    before = path.read_bytes()
    with pytest.raises(ContractValidationError) as error:
        query(path)
    assert error.value.status_code == 500
    assert error.value.reason_code == "storage_contract_violation"
    assert "synthetic-secret" not in str(error.value) + repr(error.value.diagnostics)
    assert path.read_bytes() == before


def test_http_discovery_and_denial_do_not_construct_graph_or_migrate(tmp_path, monkeypatch):
    path = tmp_path / "http.sqlite"
    rows = seed(path, 1)
    before = path.read_bytes()
    with serve_receipts(path, monkeypatch) as (_, port):
        status, result = post(port, "/api/graph/queries", {
            "operation": "request-evidence.pending", "parameters": {"limit": 1}})
        assert status == 200 and result["items"] == rows
        status, _ = post(port, "/api/graph/consumer/queries", {
            "operation": "request-evidence.pending", "parameters": {}})
        assert status == 403
    assert path.read_bytes() == before


def test_large_history_keeps_pending_page_bounded_and_unknown_operation_visible(tmp_path):
    path = tmp_path / "history.sqlite"
    expected = seed(path, 120)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        for index in range(1000):
            connection.execute("INSERT INTO pending_commands VALUES(?,?,?,?,?,?,?)", (
                "definition.save", f"history-{index}", KINDS[0], str(uuid4()),
                "synthetic-legacy-body", "c" * 64, "verified"))
        target = expected[0]
        connection.execute("UPDATE pending_commands SET operation='retired.unknown' "
                           "WHERE idempotency_key=?", (target["idempotency_key"],))
    first = query(path, limit=100)
    assert len(first["items"]) == 100 and first["next_cursor"] is not None
    second = query(path, limit=100, cursor=first["next_cursor"])
    assert len(second["items"]) == 20 and second["next_cursor"] is None
    assert any(item["operation"] == "retired.unknown" for item in first["items"] + second["items"])
