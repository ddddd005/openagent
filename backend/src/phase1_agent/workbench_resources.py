"""Legacy v1 resource archives and typed, namespaced session-data declarations.

The explicit current-only global boundary is :mod:`global_resources`.  Existing
v1 callers retain their exact-revision semantics until they select the new API.
"""

from __future__ import annotations

import copy
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, loads_strict
from .resource_contracts import (
    CORE_SESSION_NOTE, MAX_RESOURCE_BYTES, MAX_SESSION_VALUE_BYTES, _KEY, require,
    resource_error, resource_id, resource_revision, session_data_entry,
    validate_data_definition, validate_data_value, validate_global_content,
    validate_session_data, workflow_identity,
)


class WorkbenchResourceStore:
    """Legacy immutable revisions; no silent reinterpretation as current-only."""

    def __init__(self, store: Any):
        self._store, self._connection = store, store._connection

    def session_owner(self, session_id):
        row = self._connection.execute(
            "SELECT workflow_id FROM workbench_session_owners WHERE session_id=?", (session_id,),
        ).fetchone()
        return row["workflow_id"] if row else "frontend:main-test"

    def bind_session_owner(self, session_id, workflow_id):
        resource_id(session_id)
        workflow_identity(workflow_id)
        old = self._connection.execute(
            "SELECT workflow_id FROM workbench_session_owners WHERE session_id=?", (session_id,),
        ).fetchone()
        require(old is None or old["workflow_id"] == workflow_id, "Session owner cannot change")
        self._connection.execute(
            "INSERT OR IGNORE INTO workbench_session_owners VALUES(?,?)", (session_id, workflow_id),
        )

    @staticmethod
    def _validate(kind: str, record: Any) -> dict:
        if kind == "content":
            return validate_global_content(record)
        if kind == "data-definition":
            return validate_data_definition(record)
        raise resource_error("invalid_request", "Unknown resource kind")

    @staticmethod
    def _identity(kind: str, record: dict) -> str:
        return record["resource_id" if kind == "content" else "definition_id"]

    def get(self, kind: str, identity: str, revision: int | None = None,
            *, include_deleted: bool = False) -> dict | None:
        resource_id(identity)
        if revision is not None:
            resource_revision(revision)
        row = self._connection.execute(
            "SELECT r.payload FROM workbench_resource_revisions r "
            + ("WHERE r.kind=? AND r.resource_id=? AND r.revision=?" if revision is not None else
               "JOIN workbench_resource_heads h ON h.kind=r.kind AND h.resource_id=r.resource_id "
               "AND h.revision=r.revision WHERE r.kind=? AND r.resource_id=?"
               + ("" if include_deleted else " AND h.deleted=0")),
            (kind, identity, revision) if revision is not None else (kind, identity),
        ).fetchone()
        if row is None:
            return None
        try:
            return self._validate(kind, loads_strict(row["payload"]))
        except ContractValidationError as exc:
            raise resource_error("storage_contract_violation", "Saved resource is invalid", 500) from exc

    def list(self, kind: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT resource_id FROM workbench_resource_heads WHERE kind=? AND deleted=0 "
            "ORDER BY resource_id", (kind,),
        ).fetchall()
        return [self.get(kind, row["resource_id"]) for row in rows]

    def resolve_content(self, identities: list[str]) -> list[dict]:
        require(type(identities) is list and len(identities) <= 128
                and all(type(identity) is str for identity in identities))
        require(len(set(identities)) == len(identities))
        result = []
        for identity in identities:
            record = self.get("content", identity)
            if record is None:
                raise resource_error("global_content_missing", "Global content is missing", 404)
            if not record["enabled"]:
                raise resource_error("global_content_disabled", "Global content is disabled", 409)
            result.append(record)
        return result

    def write(self, kind: str, record: dict, *, expected_revision: int,
              idempotency_key: str) -> dict:
        record = self._validate(kind, record)
        identity = self._identity(kind, record)
        resource_revision(expected_revision, zero=True)
        resource_id(idempotency_key)
        require(record["revision"] == expected_revision + 1
                and not self._connection.in_transaction)
        request = {"kind": kind, "record": record, "expected_revision": expected_revision}
        digest = content_digest(request)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            receipt = self._connection.execute(
                "SELECT digest,payload FROM workbench_resource_receipts WHERE operation_id=?",
                (idempotency_key,),
            ).fetchone()
            if receipt is not None:
                if receipt["digest"] != digest:
                    raise resource_error("idempotency_conflict", "Resource request differs", 409)
                result = loads_strict(receipt["payload"])
                self._connection.execute("COMMIT")
                return result
            head = self._connection.execute(
                "SELECT revision FROM workbench_resource_heads WHERE kind=? AND resource_id=?",
                (kind, identity),
            ).fetchone()
            if (head["revision"] if head else 0) != expected_revision:
                raise resource_error("stale_revision", "Resource revision changed", 409)
            if kind == "data-definition":
                existing = self._connection.execute(
                    "SELECT payload FROM workbench_resource_revisions WHERE kind=?",
                    (kind,),
                ).fetchall()
                require(all((other := loads_strict(row["payload"]))["key"] != record["key"]
                            or other["definition_id"] == identity for row in existing),
                        "Session data namespace is already registered")
            payload = canonical_bytes(record).decode("utf-8")
            self._connection.execute(
                "INSERT INTO workbench_resource_revisions VALUES(?,?,?,?)",
                (kind, identity, record["revision"], payload),
            )
            self._connection.execute(
                "INSERT INTO workbench_resource_heads VALUES(?,?,?,0) "
                "ON CONFLICT(kind,resource_id) DO UPDATE SET revision=excluded.revision,deleted=0",
                (kind, identity, record["revision"]),
            )
            self._connection.execute(
                "INSERT INTO workbench_resource_receipts VALUES(?,?,?)",
                (idempotency_key, digest, payload),
            )
            self._connection.execute("COMMIT")
            return copy.deepcopy(record)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def delete(self, identity: str, *, expected_revision: int) -> None:
        resource_id(identity)
        resource_revision(expected_revision)
        require(not self._connection.in_transaction)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                "UPDATE workbench_resource_heads SET deleted=1 "
                "WHERE kind='content' AND resource_id=? AND revision=?",
                (identity, expected_revision),
            )
            if cursor.rowcount != 1:
                raise resource_error("stale_revision", "Resource revision changed", 409)
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def bootstrap(self) -> None:
        if self.get("data-definition", CORE_SESSION_NOTE["definition_id"]) is None:
            self.write(
                "data-definition", CORE_SESSION_NOTE, expected_revision=0,
                idempotency_key="7be319b8-30bd-4674-b7bf-d1cf54a1a122",
            )
