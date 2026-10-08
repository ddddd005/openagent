"""SQLite infrastructure for current graph records, objects and receipts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, loads_strict
from .graph_records import GRAPH_VERSIONS, is_graph_record, require, validate_graph_transition


STORAGE_VERSION = 15
_IDENTITIES = {
    "node_definition": ("component_id", "component_version"),
    "node_binding": ("workflow_definition_id", "workflow_definition_revision", "node_binding_id"),
    "workflow_definition_revision": ("workflow_definition_id", "revision"),
    "workflow_session": ("workflow_session_id",),
    "node_session": ("workflow_session_id", "node_binding_id"),
    "node_run": ("run_id",), "chain_run": ("chain_run_id",),
    "workflow_output": ("output_id",), "state_snapshot": ("state_snapshot_id",),
    "workflow_commit": ("commit_id",), "workflow_ref": ("workflow_ref_id",),
}
_MUTABLE = {"workflow_session", "node_session", "node_run", "chain_run", "workflow_ref"}


def _fields(record_type: str) -> tuple[str, ...]:
    require(record_type in _IDENTITIES, "storage_contract_violation", "Unknown graph record", 500)
    return _IDENTITIES[record_type]


def _record_id(record_type: str, record: Mapping[str, Any]) -> str:
    fields = _fields(record_type)
    return (record[fields[0]] if len(fields) == 1 else
            canonical_bytes([record[field] for field in fields]).decode("utf-8"))


class SqliteStore:
    """Shared storage; graph repositories own validation and mutation transactions."""

    def __init__(self, path: str | Path, fault_injector=None) -> None:
        self._fault_injector = fault_injector
        try:
            self._connection = sqlite3.connect(Path(path), isolation_level=None, timeout=30)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("BEGIN IMMEDIATE")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            require(version in range(STORAGE_VERSION + 1), "storage_contract_violation",
                    "Unsupported SQLite storage version", 500)
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "record_type TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL, "
                "immutable INTEGER NOT NULL, created_at TEXT NOT NULL, "
                "PRIMARY KEY(record_type,record_id))")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS idempotency ("
                "operation TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, "
                "result_refs TEXT NOT NULL, result_payload TEXT NOT NULL, request_digest TEXT, "
                "PRIMARY KEY(operation,key))")
            columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(idempotency)")}
            if "request_digest" not in columns:
                self._connection.execute("ALTER TABLE idempotency ADD COLUMN request_digest TEXT")
            for statement in (
                "CREATE TABLE IF NOT EXISTS program_variable_states ("
                "session_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(session_id,revision))",
                "CREATE TABLE IF NOT EXISTS program_variable_heads ("
                "session_id TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS program_variable_receipts ("
                "idempotency_key TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS program_variable_bindings ("
                "owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, session_id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, PRIMARY KEY(owner_kind,owner_id))",
            ):
                self._connection.execute(statement)
            from .session_objects import initialize_object_tables
            from .global_resources import initialize_global_resource_tables
            from .type_contract_store import initialize_type_contract_tables
            initialize_object_tables(self._connection)
            initialize_global_resource_tables(self._connection)
            initialize_type_contract_tables(self._connection)
            if 0 < version < STORAGE_VERSION:
                from .storage_retirement import remove_retired_test_data
                remove_retired_test_data(self._connection)
            self._connection.execute(f"PRAGMA user_version = {STORAGE_VERSION}")
            self._connection.execute("COMMIT")
        except BaseException as exc:
            if hasattr(self, "_connection"):
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                self._connection.close()
            if isinstance(exc, ContractValidationError):
                raise
            raise ContractValidationError("SQLite storage initialization failed") from exc

    def close(self) -> None:
        self._connection.close()

    def _inject(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def _get(self, record_type: str, record_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT payload FROM records WHERE record_type=? AND record_id=?", (record_type, record_id),
        ).fetchone()
        return loads_strict(row["payload"]) if row else None

    def _write(self, record_type: str, record: dict, *, update=False) -> bool:
        identity = _record_id(record_type, record)
        existing = self._get(record_type, identity)
        if existing is not None:
            if canonical_bytes(existing) == canonical_bytes(record):
                return False
            require(update, "storage_contract_violation", "Graph update is not permitted", 500)
            validate_graph_transition(record_type, existing, record)
            self._connection.execute(
                "UPDATE records SET payload=? WHERE record_type=? AND record_id=?",
                (canonical_bytes(record).decode("utf-8"), record_type, identity))
            return True
        self._connection.execute(
            "INSERT INTO records VALUES(?,?,?,?,?)",
            (record_type, identity, canonical_bytes(record).decode("utf-8"),
             int(record_type not in _MUTABLE),
             record.get("created_at", datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))),
        )
        return True

    def list_records(self, record_type: str, *, include_graph=False) -> list[dict]:
        require(record_type in GRAPH_VERSIONS, "storage_contract_violation", "Unknown graph record", 500)
        rows = self._connection.execute(
            "SELECT payload FROM records WHERE record_type=? ORDER BY rowid", (record_type,))
        values = [loads_strict(row["payload"]) for row in rows]
        return values if include_graph else [row for row in values if not is_graph_record(record_type, row)]

    def read_bundle(self, *, include_graph=False) -> dict[str, list[dict]]:
        bundle = {}
        for row in self._connection.execute("SELECT record_type,payload FROM records ORDER BY rowid"):
            if row["record_type"] not in GRAPH_VERSIONS:
                continue
            value = loads_strict(row["payload"])
            if include_graph or not is_graph_record(row["record_type"], value):
                bundle.setdefault(row["record_type"], []).append(value)
        return bundle

    def read_receipt_with_digest(self, operation: str, key: str):
        row = self._connection.execute(
            "SELECT digest,request_digest,result_payload FROM idempotency WHERE operation=? AND key=?",
            (operation, key)).fetchone()
        return (row["request_digest"] or row["digest"], loads_strict(row["result_payload"])) if row else None

    def read_receipt(self, operation: str, key: str):
        receipt = self.read_receipt_with_digest(operation, key)
        return receipt[1] if receipt is not None else None
