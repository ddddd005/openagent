"""Non-secret immutable request evidence; this authority never dispatches requests."""

from contextlib import closing
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from re import fullmatch
import sqlite3
from threading import RLock

from .contract_errors import DiagnosticError
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .graph_application_identity import application_command_digest, persist_application_command_identity
from .graph_records import require, uuid_value


_SECRET_FIELDS = {
    "secret", "api_key", "apikey", "access_token", "authorization", "private_key",
    "secret_key", "password", "credential_value",
}
_FIELDS = {
    "schema_version", "kind", "operation", "idempotency_key", "owner_kind", "owner_id",
    "request", "request_digest", "resolution",
}
_WORKFLOW_OWNERS = frozenset({
    "workflow-configuration", "workflow-configuration-copy", "workflow-management",
})
_PENDING_COORDINATES = ("owner_kind", "owner_id", "operation", "idempotency_key")


def _valid_pending_coordinates(value):
    return (type(value) is dict and set(value) == set(_PENDING_COORDINATES)
            and type(value["owner_kind"]) is str and value["owner_kind"] in _WORKFLOW_OWNERS
            and uuid_value(value["owner_id"])
            and _text(value["operation"], 128) and _text(value["idempotency_key"], 128))


def validate_pending_page(limit=50, cursor=None):
    require(type(limit) is int and 1 <= limit <= 100, "invalid_request",
            "Pending request page limit is invalid")
    require(cursor is None or _valid_pending_coordinates(cursor), "invalid_request",
            "Pending request cursor is invalid")


def read_workflow_pending(database_path, *, limit=50, cursor=None):
    """Discover workflow request coordinates without reading bodies or opening a writer."""
    validate_pending_page(limit, cursor)
    page = {"schema_version": 1, "kind": "workflow.request-evidence-page",
            "items": [], "next_cursor": None}
    path = Path(database_path).resolve()
    try:
        if not path.exists():
            return page
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                     isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                  "AND name='pending_commands'").fetchone() is None:
                return page
            coordinates = ",".join(_PENDING_COORDINATES)
            parameters = sorted(_WORKFLOW_OWNERS)
            after = ""
            if cursor is not None:
                after = f" AND ({coordinates}) > (?,?,?,?)"
                parameters.extend(cursor[key] for key in _PENDING_COORDINATES)
            rows = connection.execute(
                f"SELECT {coordinates},request_digest,resolution FROM pending_commands "
                "WHERE resolution='pending' AND owner_kind IN (?,?,?)" + after
                + f" ORDER BY {coordinates} LIMIT ?", [*parameters, limit + 1]).fetchall()
            for row in rows:
                item = dict(row)
                coordinate = {key: item[key] for key in _PENDING_COORDINATES}
                require(_valid_pending_coordinates(coordinate)
                        and type(item["request_digest"]) is str
                        and fullmatch("[0-9a-f]{64}", item["request_digest"]) is not None,
                        "storage_contract_violation", "Pending request summary is invalid", 500)
            page["items"] = [dict(row) for row in rows[:limit]]
            if len(rows) > limit:
                page["next_cursor"] = {key: rows[limit - 1][key] for key in _PENDING_COORDINATES}
            return page
    except (sqlite3.Error, OSError) as exc:
        raise DiagnosticError("storage_unavailable", "Pending requests cannot be read", status=503) from exc


def initialize_request_evidence(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS pending_commands ("
        "operation TEXT NOT NULL,idempotency_key TEXT NOT NULL,owner_kind TEXT NOT NULL,"
        "owner_id TEXT NOT NULL,request_json TEXT NOT NULL,request_digest TEXT NOT NULL,"
        "resolution TEXT NOT NULL CHECK(resolution IN ('pending','verified','archived')),"
        "PRIMARY KEY(operation,idempotency_key))"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS pending_command_owner "
        "ON pending_commands(owner_kind,owner_id) WHERE resolution='pending'"
    )


def evidence_receipt_key(operation, idempotency_key):
    return sha256(canonical_bytes([operation, idempotency_key])).hexdigest()


def _text(value, maximum):
    return type(value) is str and 0 < len(value) <= maximum and value == value.strip() and not any(
        ord(character) < 32 or ord(character) == 127 for character in value)


def validate_evidence_owner(owner_kind, owner_id):
    require(_text(owner_kind, 128) and _text(owner_id, 512),
            "invalid_request", "Request evidence owner is invalid")


def validate_evidence_request(operation, idempotency_key, request, *, stored=False):
    from .graph_application_contracts import (
        COMMANDS, RETIRED_RECEIPT_COMMANDS, STORED_EVIDENCE_COMMANDS, validate_parameters,
    )
    from .frontend_node_evidence import (
        FRONTEND_NODE_EVIDENCE_OPERATIONS, validate_frontend_node_evidence_request,
    )
    # Old records retain their exact contract without reopening evidence writes.
    # Stored-only contracts keep decoding legacy evidence whose executable
    # receipt contract has been retired.
    declarations = ((*COMMANDS, *RETIRED_RECEIPT_COMMANDS, *STORED_EVIDENCE_COMMANDS)
                    if stored else COMMANDS)
    spec = next((item for item in declarations if item.name == operation), None)
    private = operation in FRONTEND_NODE_EVIDENCE_OPERATIONS.values()
    require(private or spec is not None and not operation.startswith("request-evidence.")
            and "idempotency_key" in spec.required, "invalid_request",
            "Evidence requires an existing business command or exact frontend node operation")
    if private:
        validate_frontend_node_evidence_request(operation, idempotency_key, request)
        request_key = request["parameters"]["idempotency_key"]
    elif operation == "provider.credential.save":
        # This operation's public receipt identifies the original secret-free request.
        from .graph_provider_host import validate_provider_key_request
        validate_provider_key_request(request)
        require(request["provider_seed"] is not None, "invalid_request",
                "Provider save evidence requires its complete configuration")
        request_key = request["idempotency_key"]
    else:
        validate_parameters(spec, request)
        request_key = request["idempotency_key"]
    validate_json_value(request)
    require(request_key == idempotency_key,
            "request_evidence_conflict", "Evidence identity differs from the original request", 409)
    require(len(canonical_bytes(request)) <= 3_900_000,
            "request_evidence_capacity_exceeded", "Request evidence exceeds its limit", 413)
    pending = [request]
    while pending:
        value = pending.pop()
        if type(value) is dict:
            for field, child in value.items():
                require(field.casefold().replace("-", "_") not in _SECRET_FIELDS
                        or child is None or child == "",
                        "request_evidence_secret_disallowed", "Secret fields cannot enter request evidence")
                pending.append(child)
        elif type(value) is list:
            pending.extend(value)
    return application_command_digest(operation, request)


def validate_evidence_record(record):
    require(type(record) is dict and set(record) == _FIELDS
            and type(record["schema_version"]) is int and record["schema_version"] == 1
            and record["kind"] == "workflow.request-evidence"
            and record["resolution"] in ("pending", "verified", "archived"),
            "storage_contract_violation", "Request evidence record is invalid", 500)
    validate_evidence_owner(record["owner_kind"], record["owner_id"])
    digest = validate_evidence_request(
        record["operation"], record["idempotency_key"], record["request"], stored=True)
    _validate_private_owner(record["operation"], record["owner_kind"], record["owner_id"], record["request"])
    require(record["request_digest"] == digest,
            "storage_contract_violation", "Request evidence digest differs", 500)
    return deepcopy(record)


def _validate_private_owner(operation, owner_kind, owner_id, request):
    from .frontend_node_evidence import FRONTEND_NODE_EVIDENCE_OPERATIONS, frontend_node_evidence_owner
    if operation in FRONTEND_NODE_EVIDENCE_OPERATIONS.values():
        require((owner_kind, owner_id) == frontend_node_evidence_owner(operation, request),
                "request_evidence_conflict", "Frontend evidence owner must match its exact node operation", 409)


class RequestEvidenceService:
    def __init__(self, database_path, *, store_factory=None):
        from .storage import SqliteStore
        self.database = Path(database_path).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._store_factory = store_factory or (lambda: SqliteStore(self.database))
        self._lock = RLock()

    @staticmethod
    def _record(row):
        return validate_evidence_record({
            "schema_version": 1, "kind": "workflow.request-evidence",
            **{field: row[field] for field in (
                "operation", "idempotency_key", "owner_kind", "owner_id", "request_digest", "resolution")},
            "request": loads_strict(row["request_json"]),
        })

    def read_evidence(self, owner_kind, owner_id):
        validate_evidence_owner(owner_kind, owner_id)
        with self._lock, closing(self._store_factory()) as store:
            rows = store._connection.execute(
                "SELECT * FROM pending_commands WHERE owner_kind=? AND owner_id=? ORDER BY rowid",
                (owner_kind, owner_id))
            return [self._record(row) for row in rows]

    def _mutate(self, native_operation, request, idempotency_key, update):
        digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
        with self._lock, closing(self._store_factory()) as store:
            connection = store._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                prior = store.read_receipt_with_digest(native_operation, idempotency_key)
                if prior is not None:
                    require(prior[0] == digest, "idempotency_conflict",
                            "Request evidence command identity differs", 409)
                    require(type(prior[1]) is list and len(prior[1]) == 1
                            and set(prior[1][0]) == {"graph_result"},
                            "storage_contract_violation", "Request evidence receipt is invalid", 500)
                    result = validate_evidence_record(prior[1][0]["graph_result"])
                else:
                    result = update(connection)
                    store._inject("after_request_evidence_write")
                    connection.execute(
                        "INSERT INTO idempotency "
                        "(operation,key,digest,result_refs,result_payload,request_digest) VALUES(?,?,?,?,?,?)",
                        (native_operation, idempotency_key, digest, "[]",
                         canonical_bytes([{"graph_result": result}]).decode("utf-8"), digest))
                    persist_application_command_identity(connection, table="idempotency",
                        operation=native_operation, key=idempotency_key, request_digest=digest)
                store._inject("before_commit")
                connection.execute("COMMIT")
                return deepcopy(result)
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def write_evidence(self, operation, idempotency_key, owner_kind, owner_id, request):
        validate_evidence_owner(owner_kind, owner_id)
        request_digest = validate_evidence_request(operation, idempotency_key, request)
        _validate_private_owner(operation, owner_kind, owner_id, request)
        record = {
            "schema_version": 1, "kind": "workflow.request-evidence", "operation": operation,
            "idempotency_key": idempotency_key, "owner_kind": owner_kind, "owner_id": owner_id,
            "request": deepcopy(request), "request_digest": request_digest, "resolution": "pending",
        }

        def update(connection):
            current = connection.execute(
                "SELECT * FROM pending_commands WHERE operation=? AND idempotency_key=?",
                (operation, idempotency_key)).fetchone()
            require(current is None, "request_evidence_conflict",
                    "Original request evidence already exists", 409)
            active = connection.execute(
                "SELECT 1 FROM pending_commands WHERE owner_kind=? AND owner_id=? AND resolution='pending'",
                (owner_kind, owner_id)).fetchone()
            require(active is None, "request_evidence_pending",
                    "This owner already has an unresolved request", 409)
            connection.execute("INSERT INTO pending_commands VALUES(?,?,?,?,?,?,?)", (
                operation, idempotency_key, owner_kind, owner_id,
                canonical_bytes(request).decode("utf-8"), request_digest, "pending"))
            return record

        parameters = {"operation": operation, "owner_kind": owner_kind, "owner_id": owner_id,
                      "request": deepcopy(request)}
        return self._mutate("graph.request-evidence.write", parameters,
                            evidence_receipt_key(operation, idempotency_key), update)

    def resolve_evidence(self, operation, idempotency_key, request_digest, resolution):
        require(resolution in ("verified", "archived"),
                "invalid_request", "Evidence can only be verified or archived")

        def update(connection):
            row = connection.execute(
                "SELECT * FROM pending_commands WHERE operation=? AND idempotency_key=?",
                (operation, idempotency_key)).fetchone()
            require(row is not None, "not_found", "Original request evidence was not found", 404)
            record = self._record(row)
            require(record["request_digest"] == request_digest,
                    "request_evidence_conflict", "Original request digest differs", 409)
            require(record["resolution"] in ("pending", resolution),
                    "request_evidence_conflict", "Original evidence has already been resolved differently", 409)
            connection.execute(
                "UPDATE pending_commands SET resolution=? WHERE operation=? AND idempotency_key=?",
                (resolution, operation, idempotency_key))
            return {**record, "resolution": resolution}

        parameters = {"operation": operation, "request_digest": request_digest, "resolution": resolution}
        return self._mutate("graph.request-evidence.resolve", parameters,
                            evidence_receipt_key(operation, idempotency_key), update)
