"""Transactional graph operations over the existing SQLite records and receipts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from .contract_json import canonical_bytes, content_digest, loads_strict
from .graph_application_identity import persist_application_command_identity
from .graph_records import (is_graph_record, require, validate_graph_bundle,
                            validate_graph_record)
from .storage import SqliteStore, _record_id


class GraphRecordStore:
    def __init__(self, store: SqliteStore):
        self.store = store

    def rows(self, kind: str) -> list[dict]:
        return [validate_graph_record(kind, row)
                for row in self.store.list_records(kind, include_graph=True)
                if is_graph_record(kind, row)]

    def get(self, kind: str, **identity: Any) -> dict:
        row = self.store._get(kind, _record_id(kind, identity))
        require(is_graph_record(kind, row), "not_found", "Graph record not found", 404)
        return validate_graph_record(kind, row)

    def maybe(self, kind: str, **identity: Any) -> dict | None:
        row = self.store._get(kind, _record_id(kind, identity))
        return validate_graph_record(kind, row) if is_graph_record(kind, row) else None

    def put(self, kind: str, value: dict) -> None:
        require(self.store._connection.in_transaction, "storage_contract_violation", "Graph write requires transaction", 500)
        value = validate_graph_record(kind, value)
        self.store._write(kind, value, update=True)
        self.store._inject("after_graph_write")

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
            result = change(self)
            complete = self.store.read_bundle(include_graph=True)
            validate_graph_bundle({kind: [row for row in rows if is_graph_record(kind, row)]
                                   for kind, rows in complete.items()})
            payload = canonical_bytes([{"graph_result": result}]).decode("utf-8")
            connection.execute(
                "INSERT INTO idempotency (operation,key,digest,result_refs,result_payload,request_digest) VALUES(?,?,?,?,?,?)",
                (operation, key, digest, "[]", payload, digest),
            )
            persist_application_command_identity(connection, table="idempotency", operation=operation,
                                                 key=key, request_digest=digest)
            self.store._inject("before_commit")
            connection.execute("COMMIT")
            return deepcopy(result)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def equal(left: Any, right: Any) -> bool:
        return canonical_bytes(left) == canonical_bytes(right)
