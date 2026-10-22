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
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def equal(left: Any, right: Any) -> bool:
        return canonical_bytes(left) == canonical_bytes(right)
