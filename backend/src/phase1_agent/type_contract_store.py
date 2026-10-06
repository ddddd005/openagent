"""Persistent evidence that a registered data schema version has one meaning.

Only type declarations are recorded. Session values, global resource bodies and
their historical revisions never enter this repository. An absent package can
leave its evidence and raw data untouched; a present implementation must match
the previously accepted declaration or use a new schema version.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

from .contract_json import canonical_bytes, content_digest, loads_strict
from .host_sdk import TypeRegistry, bounded_name, ensure


def initialize_type_contract_tables(connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS registered_type_contracts ("
        "scope TEXT NOT NULL CHECK(scope IN ('content','session','global')), "
        "type_id TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version>=1), "
        "digest TEXT NOT NULL, declaration TEXT NOT NULL, "
        "PRIMARY KEY(scope,type_id,schema_version))"
    )


class TypeContractStore:
    def __init__(self, store):
        self.connection = store._connection

    @staticmethod
    def _coordinates(type_id, schema_version, scope):
        ensure(bounded_name(type_id) and type(schema_version) is int and 1 <= schema_version <= 2**53 - 1
               and scope in ("content", "session", "global"),
               "host_invalid_type", "Type contract requires an exact registered identity and scope")
        return scope, type_id, schema_version

    def get(self, type_id, schema_version, scope) -> dict | None:
        row = self.connection.execute(
            "SELECT digest,declaration FROM registered_type_contracts "
            "WHERE scope=? AND type_id=? AND schema_version=?",
            self._coordinates(type_id, schema_version, scope),
        ).fetchone()
        if row is None:
            return None
        declaration = loads_strict(row["declaration"])
        ensure(content_digest(declaration) == row["digest"], "type_contract_evidence_invalid",
               "Stored type contract evidence does not match its digest")
        return {"envelope_version": 1, "kind": "workflow.type-contract",
                "digest": row["digest"], "declaration": declaration}

    def check(self, types: TypeRegistry, type_id, schema_version, scope,
              *, allow_missing=False, allow_legacy=False) -> dict | None:
        """Check evidence, registering a first declaration only inside a transaction.

        Legacy reads explicitly pass ``allow_legacy=True`` to tolerate absent
        evidence without establishing a new baseline. Raw reads for a disabled
        package may pass ``allow_missing=True``. Neither option permits a known
        declaration to change an existing schema-version identity.
        """
        self._coordinates(type_id, schema_version, scope)
        definition = types.get(type_id, schema_version, scope=scope)
        ensure(definition is not None or allow_missing,
               "host_unknown_type", "Required data type version is unavailable")
        if definition is None:
            return None
        declaration = definition.to_dict()
        existing = self.get(type_id, schema_version, scope)
        if existing is not None:
            ensure(canonical_bytes(existing["declaration"]) == canonical_bytes(declaration),
                   "type_contract_redefined",
                   "Registered schema version differs from its persistent contract; use an explicit new version")
            return existing
        if allow_legacy:
            return None
        if not self.connection.in_transaction:
            ensure(False, "type_contract_evidence_missing",
                   "Type has no accepted contract evidence; register it in the owning save transaction")
        digest = content_digest(declaration)
        self.connection.execute(
            "INSERT INTO registered_type_contracts VALUES(?,?,?,?,?)",
            (scope, type_id, schema_version, digest, canonical_bytes(declaration).decode("utf-8")),
        )
        return {"envelope_version": 1, "kind": "workflow.type-contract",
                "digest": digest, "declaration": deepcopy(declaration)}

    @contextmanager
    def _atomic(self):
        """Own an atomic operation while preserving the caller's transaction."""
        nested = self.connection.in_transaction
        savepoint = "type_contract_" + uuid4().hex
        self.connection.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("RELEASE SAVEPOINT " + savepoint if nested else "COMMIT")
        except BaseException:
            if nested:
                self.connection.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                self.connection.execute("RELEASE SAVEPOINT " + savepoint)
            elif self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def register(self, types: TypeRegistry, type_id, schema_version, scope) -> dict:
        """Public atomic registration for management callers without a transaction."""
        with self._atomic():
            return self.check(types, type_id, schema_version, scope)

    def check_registry(self, types: TypeRegistry) -> list[dict]:
        """Persist one project's complete enabled declaration set atomically."""
        with self._atomic():
            return [
                self.check(types, definition.type_id, definition.schema_version, definition.scope)
                for definition in types.definitions()
            ]
