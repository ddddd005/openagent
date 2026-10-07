"""Registered global resources retain current values and lightweight receipts."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .graph_application_identity import persist_application_command_identity
from .resource_contracts import resource_error, resource_id, resource_revision


GLOBAL_RESOURCE_REF_TYPE = "GLOBAL_RESOURCE_REF"
MAX_GLOBAL_RESOURCE_BYTES = 1_000_000


def _require(condition: bool, reason: str, message: str, status: int = 400) -> None:
    if not condition:
        raise resource_error(reason, message, status)


def _identity(value: Any) -> dict:
    from .host_sdk import ResourceIdentity

    return ResourceIdentity.from_dict(value).to_dict()


def _coordinates(identity: dict) -> tuple[str, str, str]:
    return identity["scope"], identity["type_id"], identity["resource_id"]


def validate_global_resource_reference(value: Any) -> dict:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {"schema_version", "kind", "reference"}
             and type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["kind"] == "workflow.global-resource-ref",
             "graph_invalid_output", "Global resource output must contain only a stable identity")
    _identity(value["reference"])
    return deepcopy(value)


def global_resource_reference(identity: dict) -> dict:
    return validate_global_resource_reference({
        "schema_version": 1, "kind": "workflow.global-resource-ref", "reference": _identity(identity),
    })


def register_global_resource_ref_type(registry: Any) -> None:
    registry.register(global_resource_ref_definition())


def global_resource_ref_definition():
    from .host_sdk import DataTypeDefinition

    return DataTypeDefinition(
        type_id=GLOBAL_RESOURCE_REF_TYPE, schema_version=1, scope="content",
        schema={
            "type": "object", "additionalProperties": False,
            "required": ["schema_version", "kind", "reference"],
            "properties": {
                "schema_version": {"const": 1},
                "kind": {"const": "workflow.global-resource-ref"},
                "reference": {"type": "object"},
            },
        }, validator=validate_global_resource_reference,
        references=lambda value: [deepcopy(value["reference"])],
        reference_mapper=lambda value, mapping: deepcopy(value), max_bytes=4096,
    )


def create_global_type_registry():
    from .host_sdk import TypeRegistry

    registry = TypeRegistry()
    register_global_resource_ref_type(registry)
    return registry


def initialize_global_resource_tables(connection: Any) -> None:
    """Initialize current values and their request receipts."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS global_resource_current ("
        "scope TEXT NOT NULL, type_id TEXT NOT NULL, resource_id TEXT NOT NULL, "
        "update_sequence INTEGER NOT NULL CHECK(update_sequence>=1), "
        "deleted INTEGER NOT NULL CHECK(deleted IN (0,1)), payload TEXT, "
        "PRIMARY KEY(scope,type_id,resource_id), "
        "CHECK((deleted=0 AND payload IS NOT NULL) OR (deleted=1 AND payload IS NULL)))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS global_resource_receipts ("
        "idempotency_key TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)"
    )


class GlobalResourceStore:
    """CAS updates replace current values; receipts never contain body copies."""

    def __init__(self, store: Any, type_registry: Any = None):
        self._store = store
        self._connection = store._connection
        self.types = type_registry if type_registry is not None else create_global_type_registry()
        # The host migration owns table initialization. Keep an additive fallback
        # for standalone stores whose connection predates the host migration.
        initialize_global_resource_tables(self._connection)

    def _validate_record(self, record: Any, *, validate_type=True) -> dict:
        validate_json_value(record)
        _require(type(record) is dict and set(record) == {
            "envelope_version", "scope", "type_id", "resource_id", "data_schema_version",
            "update_sequence", "value",
        } and type(record["envelope_version"]) is int and record["envelope_version"] == 1,
                 "global_resource_invalid", "Global resource envelope is invalid")
        _identity({key: record[key] for key in ("envelope_version", "scope", "type_id", "resource_id")})
        resource_revision(record["update_sequence"])
        resource_revision(record["data_schema_version"])
        _require(len(canonical_bytes(record)) <= MAX_GLOBAL_RESOURCE_BYTES,
                 "global_resource_invalid", "Global resource exceeds its size limit")
        if validate_type:
            self.types.validate(record["type_id"], record["data_schema_version"], record["value"], scope="global")
            from .type_contract_store import TypeContractStore
            TypeContractStore(self._store).check(self.types, record["type_id"], record["data_schema_version"],
                                                "global", allow_legacy=True)
        return deepcopy(record)

    def _row(self, identity: dict):
        return self._connection.execute(
            "SELECT update_sequence,deleted,payload FROM global_resource_current "
            "WHERE scope=? AND type_id=? AND resource_id=?", _coordinates(identity),
        ).fetchone()

    @contextmanager
    def _read_boundary(self):
        """Reuse the caller's read transaction, otherwise freeze one SQLite view."""
        started = not self._connection.in_transaction
        if started:
            self._connection.execute("BEGIN")
        try:
            yield
            if started:
                self._connection.execute("COMMIT")
        except BaseException:
            if started and self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def head(self, identity: dict) -> dict | None:
        identity = _identity(identity)
        row = self._row(identity)
        if row is None:
            return None
        return {"reference": identity, "update_sequence": row["update_sequence"], "deleted": bool(row["deleted"])}

    def get(self, identity: dict) -> dict | None:
        identity = _identity(identity)
        row = self._row(identity)
        if row is None or row["deleted"]:
            return None
        try:
            record = self._validate_record(loads_strict(row["payload"]))
            _require(_coordinates(record) == _coordinates(identity)
                     and record["update_sequence"] == row["update_sequence"],
                     "storage_contract_violation", "Global resource identity differs from its row", 500)
            return record
        except ContractValidationError as exc:
            # A missing implementation must not erase the persisted object.
            if getattr(exc, "reason_code", "") == "host_unknown_type":
                raise
            raise resource_error("storage_contract_violation", "Saved global resource is invalid", 500) from exc

    def list(self, *, scope: str | None = None, type_id: str | None = None) -> list[dict]:
        for value in (scope, type_id):
            _require(value is None or (type(value) is str and 0 < len(value) <= 128),
                     "invalid_request", "Resource filter is invalid")
        clauses, arguments = ["deleted=0"], []
        for field, value in (("scope", scope), ("type_id", type_id)):
            if value is not None:
                clauses.append(field + "=?")
                arguments.append(value)
        with self._read_boundary():
            rows = self._connection.execute(
                "SELECT scope,type_id,resource_id FROM global_resource_current WHERE "
                + " AND ".join(clauses) + " ORDER BY scope,type_id,resource_id", arguments,
            ).fetchall()
            return [self.get({"envelope_version": 1, **dict(row)}) for row in rows]

    def read_many(self, identities: list[dict]) -> list[dict]:
        _require(type(identities) is list and len(identities) <= 128,
                 "invalid_request", "Global dependencies must be a bounded array")
        identities = [_identity(identity) for identity in identities]
        _require(len({_coordinates(identity) for identity in identities}) == len(identities),
                 "invalid_request", "Duplicate global resource identity")
        with self._read_boundary():
            records = []
            for identity in identities:
                record = self.get(identity)
                _require(record is not None, "global_resource_missing", "Global resource is missing", 404)
                records.append(record)
            return records

    def _receipt(self, key: str, digest: str) -> dict | None:
        row = self._connection.execute(
            "SELECT digest,payload FROM global_resource_receipts WHERE idempotency_key=?", (key,),
        ).fetchone()
        if row is None:
            return None
        _require(row["digest"] == digest, "idempotency_conflict", "Global resource request differs", 409)
        return loads_strict(row["payload"])

    def _mutate(self, request: dict, key: str, change):
        resource_id(key)
        _require(not self._connection.in_transaction, "transaction_in_progress",
                 "Global management must use its own short transaction", 409)
        digest = content_digest(request)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            result = self._receipt(key, digest)
            if result is None:
                result = change()
                self._connection.execute(
                    "INSERT INTO global_resource_receipts VALUES(?,?,?)",
                    (key, digest, canonical_bytes(result).decode("utf-8")),
                )
                persist_application_command_identity(
                    self._connection, table="global_resource_receipts", operation=request["operation"],
                    key=key, request_digest=digest,
                )
            self._connection.execute("COMMIT")
            return deepcopy(result)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def write(self, record: dict, *, expected_sequence: int, idempotency_key: str) -> dict:
        record = self._validate_record(record, validate_type=False)
        resource_revision(expected_sequence, zero=True)
        _require(record["update_sequence"] == expected_sequence + 1,
                 "invalid_request", "Update sequence must advance once")
        identity = _identity({key: record[key] for key in ("envelope_version", "scope", "type_id", "resource_id")})
        request = {"operation": "write", "record": record, "expected_sequence": expected_sequence}

        def change():
            self._validate_record(record)
            from .type_contract_store import TypeContractStore
            TypeContractStore(self._store).check(self.types, record["type_id"], record["data_schema_version"], "global")
            row = self._row(identity)
            _require((row["update_sequence"] if row else 0) == expected_sequence,
                     "stale_revision", "Global resource sequence changed", 409)
            self._connection.execute(
                "INSERT INTO global_resource_current VALUES(?,?,?,?,0,?) "
                "ON CONFLICT(scope,type_id,resource_id) DO UPDATE SET "
                "update_sequence=excluded.update_sequence,deleted=0,payload=excluded.payload",
                (*_coordinates(identity), record["update_sequence"], canonical_bytes(record).decode("utf-8")),
            )
            return {"reference": identity, "update_sequence": record["update_sequence"], "deleted": False}

        return self._mutate(request, idempotency_key, change)

    def delete(self, identity: dict, *, expected_sequence: int, idempotency_key: str) -> dict:
        identity = _identity(identity)
        resource_revision(expected_sequence)
        resource_revision(expected_sequence + 1)
        request = {"operation": "delete", "reference": identity, "expected_sequence": expected_sequence}

        def change():
            row = self._row(identity)
            _require(row is not None and not row["deleted"] and row["update_sequence"] == expected_sequence,
                     "stale_revision", "Global resource sequence changed", 409)
            record = loads_strict(row["payload"])
            from .type_contract_store import TypeContractStore
            TypeContractStore(self._store).check(
                self.types, identity["type_id"], record["data_schema_version"], "global")
            self._connection.execute(
                "UPDATE global_resource_current SET update_sequence=?,deleted=1,payload=NULL "
                "WHERE scope=? AND type_id=? AND resource_id=?",
                (expected_sequence + 1, *_coordinates(identity)),
            )
            return {"reference": identity, "update_sequence": expected_sequence + 1, "deleted": True}

        return self._mutate(request, idempotency_key, change)
