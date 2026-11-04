"""Transactional graph operations over the existing SQLite records and receipts."""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from typing import Any, Callable

from .contract_errors import DiagnosticError
from .contract_json import canonical_bytes, content_digest, loads_strict
from .graph_application_identity import persist_application_command_identity
from .graph_records import (is_graph_record, require, validate_graph_bundle,
                            validate_graph_record)
from .storage import SqliteStore, _record_id, is_storage_busy
from .workflow_request_abandon import abandoned_command


def replayed_result(database, operation: str, key: str, request: dict):
    """Return one exact persisted replay without opening a writer.

    Reading an accepted result is not a second real modification, so this probe
    runs before (and without) the modification entitlement: it uses a read-only
    SQLite connection that never blocks an in-flight writer.  A missing receipt
    returns ``None`` and the caller takes the normal transaction path; the same
    key with another request keeps the existing ``idempotency_conflict``.
    """
    digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
    try:
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT request_digest,digest,result_payload FROM idempotency "
                "WHERE operation=? AND key=?", (operation, key)).fetchone()
    except sqlite3.Error:
        # A probe that cannot read must not change command semantics; the normal
        # transaction path still replays or executes under its full protection.
        return None
    if row is None:
        return None
    stored = row["request_digest"] or row["digest"]
    require(stored == digest, "idempotency_conflict", "The key has a different request", 409)
    payload = loads_strict(row["result_payload"])
    require(type(payload) is list and len(payload) == 1 and type(payload[0]) is dict
            and set(payload[0]) == {"graph_result"},
            "storage_contract_violation", "Invalid graph receipt", 500)
    return deepcopy(payload[0]["graph_result"])


class GraphRecordStore:
    def __init__(self, store: SqliteStore, *, definition_registry=None):
        self.store = store
        self.definition_registry = definition_registry
        self._changed_sessions = set()
        self._changed_definitions = set()

    def rows(self, kind: str, **owners) -> list[dict]:
        if owners:
            require(set(owners) <= {"workflow_definition_id", "workflow_session_id",
                                  "workflow_definition_revision", "revision", "chain_run_id"},
                    "storage_contract_violation", "Unknown graph owner filter", 500)
            clauses = " AND ".join(f"json_extract(payload,'$.{field}')=?" for field in owners)
            rows = self.store._connection.execute(
                "SELECT payload FROM records WHERE record_type=? AND json_valid(payload) AND "
                + clauses + " ORDER BY rowid",
                (kind, *owners.values()))
            values = [loads_strict(row["payload"]) for row in rows]
        else:
            values = self.store.list_records(kind, include_graph=True)
        return [validate_graph_record(kind, row)
                for row in values
                if is_graph_record(kind, row)]

    def get(self, kind: str, **identity: Any) -> dict:
        row = self.store._get(kind, _record_id(kind, identity))
        require(is_graph_record(kind, row), "not_found", "Graph record not found", 404)
        return validate_graph_record(kind, row)

    def maybe(self, kind: str, **identity: Any) -> dict | None:
        row = self.store._get(kind, _record_id(kind, identity))
        return validate_graph_record(kind, row) if is_graph_record(kind, row) else None

    def max_definition_revision(self, identity: str) -> int:
        """Return the highest stored fixed revision of one workflow (read-only).

        This is the SQL equivalent of the maximum over ``rows(
        "workflow_definition_revision", workflow_definition_id=identity)`` for a
        caller that has already read and validated the same identity's history
        inside the same transaction (Q08 second-pass optimisation).  The filter
        reproduces ``rows`` exactly: invalid JSON and non-graph payloads are
        skipped, and only records owned by this workflow contribute.  A record
        that ``rows`` would reject is rejected by the caller's unchanged first
        pass before this method runs; corrupt-but-skipped rows can never change
        the returned revision.
        """
        row = self.store._connection.execute(
            "SELECT MAX(json_extract(payload,'$.revision')) AS revision FROM records "
            "WHERE record_type='workflow_definition_revision' AND json_valid(payload) "
            "AND json_extract(payload,'$.execution_model')='graph' "
            "AND json_extract(payload,'$.workflow_definition_id')=?",
            (identity,)).fetchone()
        value = None if row is None else row["revision"]
        if value is None:
            return 0
        require(type(value) is int and value >= 1, "storage_contract_violation",
                "Invalid graph revision", 500)
        return value

    def current_configuration(self, identity: str) -> dict | None:
        row = self.store._connection.execute(
            "SELECT * FROM workflow_current_configuration WHERE workflow_definition_id=?", (identity,)).fetchone()
        if row is None:
            return None
        return {
            "workflow_definition_id": row["workflow_definition_id"], "edit_sequence": row["edit_sequence"],
            "fixed_definition_revision": row["fixed_definition_revision"], "display_name": row["display_name"],
            "node_presentation": loads_strict(row["node_presentation_json"]),
            "adjustable_config": loads_strict(row["adjustable_config_json"]),
        }

    def put_current_configuration(self, value: dict) -> None:
        require(self.store._connection.in_transaction, "storage_contract_violation",
                "Configuration write requires transaction", 500)
        self.store._connection.execute(
            "INSERT INTO workflow_current_configuration VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(workflow_definition_id) DO UPDATE SET "
            "edit_sequence=excluded.edit_sequence,fixed_definition_revision=excluded.fixed_definition_revision,"
            "display_name=excluded.display_name,node_presentation_json=excluded.node_presentation_json,"
            "adjustable_config_json=excluded.adjustable_config_json",
            (value["workflow_definition_id"], value["edit_sequence"], value["fixed_definition_revision"],
             value["display_name"], canonical_bytes(value["node_presentation"]).decode("utf-8"),
             canonical_bytes(value["adjustable_config"]).decode("utf-8")))
        self.store._inject("after_configuration_write")

    def put(self, kind: str, value: dict) -> None:
        require(self.store._connection.in_transaction, "storage_contract_violation", "Graph write requires transaction", 500)
        value = validate_graph_record(kind, value)
        self.store._write(kind, value, update=True)
        if "workflow_session_id" in value:
            self._changed_sessions.add(value["workflow_session_id"])
        if kind == "workflow_definition_revision":
            self._changed_definitions.add((value["workflow_definition_id"], value["revision"]))
        self.store._inject("after_graph_write")

    def _validate_changes(self):
        bundle = {}
        pending = set(self._changed_sessions)
        visited = set()
        definitions = set(self._changed_definitions)
        while pending:
            sid = pending.pop()
            if sid in visited:
                continue
            visited.add(sid)
            for kind in ("workflow_session", "node_session", "node_run", "chain_run",
                         "workflow_output", "state_snapshot", "workflow_commit", "workflow_ref"):
                rows = self.rows(kind, workflow_session_id=sid)
                bundle.setdefault(kind, []).extend(rows)
                for row in rows:
                    if "workflow_definition_id" in row:
                        definitions.add((row["workflow_definition_id"], row["definition_revision"]))
                    if kind == "state_snapshot":
                        for identity in row["history_refs"]:
                            chain = self.get("chain_run", chain_run_id=identity)
                            pending.add(chain["workflow_session_id"])
        descriptors = set()
        for identity, revision in definitions:
            bundle.setdefault("workflow_definition_revision", []).append(self.get(
                "workflow_definition_revision", workflow_definition_id=identity, revision=revision))
            bindings = self.rows("node_binding", workflow_definition_id=identity, workflow_definition_revision=revision)
            bundle.setdefault("node_binding", []).extend(bindings)
            for binding in bindings:
                key = binding["component_id"], binding["component_version"]
                if key in descriptors:
                    continue
                descriptors.add(key)
                definition = (self.definition_registry.get_definition(*key)
                              if self.definition_registry is not None else None)
                if definition is not None:
                    from .graph_records import graph_record
                    bundle.setdefault("node_definition", []).append(graph_record(
                        "node_definition", component_id=key[0], component_version=key[1],
                        descriptor=definition.to_dict()))
        validate_graph_bundle(bundle)

    def remove_node_state(self, sid: str, node_id: str) -> None:
        identity = _record_id("node_session", {"workflow_session_id": sid, "node_binding_id": node_id})
        row = self.store._get("node_session", identity)
        require(is_graph_record("node_session", row), "storage_contract_violation", "Cannot delete legacy node state", 500)
        self.store._connection.execute("DELETE FROM records WHERE record_type='node_session' AND record_id=?", (identity,))

    def atomic(self, operation: str, key: str, request: dict,
               change: Callable[["GraphRecordStore"], Any]) -> Any:
        require(type(key) is str and 1 <= len(key) <= 128 and bool(key.strip()),
                "invalid_request", "An idempotency key is required")
        digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
        connection = self.store._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = self.store.read_receipt_with_digest(operation, key)
            if prior is not None:
                require(prior[0] == digest, "idempotency_conflict", "The key has a different request", 409)
                require(len(prior[1]) == 1 and "graph_result" in prior[1][0],
                        "storage_contract_violation", "Invalid graph receipt", 500)
                connection.execute("COMMIT")
                return deepcopy(prior[1][0]["graph_result"])
            terminated = abandoned_command(connection, operation, key)
            if terminated is not None:
                require(terminated == digest, "idempotency_conflict",
                        "The key has a different request", 409)
                raise DiagnosticError(
                    "request_abandoned",
                    "This request was explicitly terminated and cannot execute", status=409)
            result = change(self)
            self._validate_changes()
            payload = canonical_bytes([{"graph_result": result}]).decode("utf-8")
            connection.execute(
                "INSERT INTO idempotency (operation,key,digest,result_refs,result_payload,request_digest) VALUES(?,?,?,?,?,?)",
                (operation, key, digest, "[]", payload, digest),
            )
            persist_application_command_identity(connection, table="idempotency", operation=operation,
                                                 key=key, request_digest=digest)
            if operation in ("graph.node.succeeded", "graph.run.finish"):
                from .result_acceptance import persistence_boundary
                stage = ("node_acceptance" if operation == "graph.node.succeeded"
                         else "round_acceptance")
                with persistence_boundary(stage):
                    self.store._inject("before_commit")
                    connection.execute("COMMIT")
            else:
                self.store._inject("before_commit")
                connection.execute("COMMIT")
            return deepcopy(result)
        except BaseException as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if is_storage_busy(exc):
                raise DiagnosticError(
                    "graph_storage_busy", "Storage is busy; the request was not processed",
                    status=409, phase="storage") from exc
            raise

    @staticmethod
    def equal(left: Any, right: Any) -> bool:
        return canonical_bytes(left) == canonical_bytes(right)
