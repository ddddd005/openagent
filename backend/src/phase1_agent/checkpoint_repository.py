"""Uninstalled checkpoint persistence on one caller-owned SQLite transaction.

The constructor performs no SQL or DDL. Typed bodies, artifact proof, lifecycle
permissions and command receipts remain the caller's trusted service boundary.
No production store, registry, storage version or legacy Head is changed here.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import sqlite3

from .contract_errors import require, uuid_value
from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .graph_records import validate_graph_record
from .runtime_checkpoints import materialize as materialize_state, validate_checkpoint


_CHECKPOINT_COLUMNS = (
    "schema_version", "checkpoint_id", "workflow_definition_id", "workflow_session_id",
    "fixed_definition_revision", "kind", "parent_checkpoint_id", "round_id",
    "chain_run_id", "assistant_entry_id", "state_storage_json",
)
_MEMBERSHIP_COLUMNS = (
    "schema_version", "workflow_session_id", "chain_run_id",
    "workflow_definition_id", "fixed_definition_revision",
)
_RECORD_COLUMNS = {"record_type", "record_id", "payload", "immutable", "created_at"}
_OWNER_FIELDS = ("workflow_definition_id", "workflow_session_id", "fixed_definition_revision")
_HEAD_FIELDS = {
    "schema_version", "execution_model", "workflow_ref_id", "workflow_session_id",
    "checkpoint_id", "revision",
}
_MAX_INTEGER = 2**53 - 1
_MAX_CHECKPOINT_BYTES = 4_000_000


def _integer(value, minimum=1):
    return type(value) is int and minimum <= value <= _MAX_INTEGER


def _connection(connection):
    require(isinstance(connection, sqlite3.Connection) and connection.row_factory is sqlite3.Row,
            "checkpoint_connection_invalid", "Checkpoint reads require a caller-owned Row connection", 500)


def _transaction(connection):
    require(connection.in_transaction, "checkpoint_transaction_required",
            "Checkpoint operations require one caller-owned transaction", 500)


def initialize_checkpoint_tables(connection):
    """Explicit new-table initialization; never called by a read or constructor."""
    _connection(connection)
    _transaction(connection)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workflow_checkpoints ("
        "schema_version INTEGER NOT NULL CHECK(schema_version=1),"
        "checkpoint_id TEXT PRIMARY KEY NOT NULL,workflow_definition_id TEXT NOT NULL,"
        "workflow_session_id TEXT NOT NULL,fixed_definition_revision INTEGER NOT NULL,"
        "kind TEXT NOT NULL CHECK(kind IN ('initial','successful_round')),"
        "parent_checkpoint_id TEXT,round_id TEXT,chain_run_id TEXT,assistant_entry_id TEXT,"
        "state_storage_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS workflow_checkpoints_initial "
        "ON workflow_checkpoints(workflow_session_id) WHERE kind='initial'"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workflow_history_membership ("
        "schema_version INTEGER NOT NULL CHECK(schema_version=1),"
        "workflow_session_id TEXT NOT NULL,chain_run_id TEXT NOT NULL,"
        "workflow_definition_id TEXT NOT NULL,fixed_definition_revision INTEGER NOT NULL,"
        "PRIMARY KEY(workflow_session_id,chain_run_id))"
    )


def validate_checkpoint_head(value):
    """Exact local workflow_ref@3 contract; no dispatch through old Head@2."""
    validate_json_value(value)
    require(type(value) is dict and set(value) in (_HEAD_FIELDS, _HEAD_FIELDS | {"created_at"})
            and type(value["schema_version"]) is int and value["schema_version"] == 3
            and value["execution_model"] == "graph"
            and all(uuid_value(value[key]) for key in (
                "workflow_ref_id", "workflow_session_id", "checkpoint_id"))
            and _integer(value["revision"]),
            "checkpoint_head_invalid", "Checkpoint Head fields or identity are invalid", 500)
    if "created_at" in value:
        require(type(value["created_at"]) is str and bool(value["created_at"]),
                "checkpoint_head_invalid", "Checkpoint Head time must remain its original text", 500)
    return deepcopy(value)


class CheckpointRepository:
    """Trusted session-scoped storage primitive, not a public UUID read/grant API."""

    def __init__(self, connection, *, workflow_definition_id, workflow_session_id,
                 fixed_definition_revision):
        _connection(connection)
        require(uuid_value(workflow_definition_id) and uuid_value(workflow_session_id)
                and _integer(fixed_definition_revision),
                "checkpoint_owner_invalid", "Checkpoint owner requires exact workflow/session/revision")
        self.connection = connection
        self._owner = {
            "workflow_definition_id": workflow_definition_id,
            "workflow_session_id": workflow_session_id,
            "fixed_definition_revision": fixed_definition_revision,
        }

    @property
    def owner(self):
        return deepcopy(self._owner)

    def _record(self, kind, identity, *, required=True):
        row = self.connection.execute(
            "SELECT * FROM records WHERE record_type=? AND record_id=?", (kind, identity),
        ).fetchone()
        if row is None:
            require(not required, "checkpoint_record_not_found",
                    "Checkpoint owner or original execution record is missing", 404)
            return None
        require(set(row.keys()) == _RECORD_COLUMNS and row["record_type"] == kind
                and row["record_id"] == identity,
                "checkpoint_storage_invalid", "Original record SQL identity differs", 500)
        value = validate_graph_record(kind, loads_strict(row["payload"]))
        identity_field = "workflow_session_id" if kind == "workflow_session" else "chain_run_id"
        require(value[identity_field] == identity and row["immutable"] == 0,
                "checkpoint_storage_invalid", "Original record body or mutability differs", 500)
        return value

    def _session(self):
        _transaction(self.connection)
        session = self._record("workflow_session", self._owner["workflow_session_id"])
        require((session["workflow_definition_id"], session["definition_revision"]) == (
                    self._owner["workflow_definition_id"], self._owner["fixed_definition_revision"]),
                "checkpoint_owner_denied", "Target session has another workflow or fixed revision", 403)
        return session

    def _chain(self, chain_run_id):
        require(uuid_value(chain_run_id), "checkpoint_chain_invalid",
                "Historical chain identity must be a canonical UUID4")
        chain = self._record("chain_run", chain_run_id)
        require((chain["workflow_definition_id"], chain["definition_revision"]) == (
                    self._owner["workflow_definition_id"], self._owner["fixed_definition_revision"]),
                "checkpoint_history_denied", "Original chain has another workflow or fixed revision", 403)
        # The chain is the immutable origin; its source session may have been deleted.
        source = self._record("workflow_session", chain["workflow_session_id"], required=False)
        if source is not None:
            require((source["workflow_definition_id"], source["definition_revision"]) == (
                        chain["workflow_definition_id"], chain["definition_revision"]),
                    "checkpoint_storage_invalid", "Existing source session differs from the original chain", 500)
        return chain

    def _decode(self, row):
        require(set(row.keys()) == set(_CHECKPOINT_COLUMNS),
                "checkpoint_storage_invalid", "Checkpoint SQL columns differ", 500)
        value = {key: row[key] for key in _CHECKPOINT_COLUMNS if key != "state_storage_json"}
        value["state_storage"] = loads_strict(row["state_storage_json"])
        checked = validate_checkpoint(value)
        require(row["state_storage_json"] == canonical_bytes(checked["state_storage"]).decode("utf-8"),
                "checkpoint_storage_invalid", "Checkpoint storage is not its exact canonical body", 500)
        require(all(checked[key] == self._owner[key] for key in _OWNER_FIELDS),
                "checkpoint_owner_denied", "Checkpoint is not owned by the target session", 403)
        return checked

    def get(self, checkpoint_id):
        self._session()
        require(uuid_value(checkpoint_id), "runtime_checkpoint_invalid",
                "Checkpoint identity must be a canonical UUID4")
        row = self.connection.execute(
            "SELECT * FROM workflow_checkpoints WHERE checkpoint_id=?", (checkpoint_id,),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def prefix(self, checkpoint_id):
        """Return only the selected initial-to-target parent path, fully materialized."""
        path = []

        def load(identity):
            value = self.get(identity)
            if value is not None:
                path.append(value)
            return value

        materialize_state(checkpoint_id, load)
        return list(reversed(path))

    def materialize(self, checkpoint_id):
        return materialize_state(checkpoint_id, self.get)

    def insert(self, checkpoint):
        """Insert an immutable point after validating its actual owned parent path."""
        self._session()
        value = validate_checkpoint(checkpoint)
        require(all(value[key] == self._owner[key] for key in _OWNER_FIELDS),
                "checkpoint_owner_denied", "Cannot write another session's checkpoint", 403)
        encoded = canonical_bytes(value["state_storage"]).decode("utf-8")
        require(len(canonical_bytes(value)) <= _MAX_CHECKPOINT_BYTES,
                "runtime_checkpoint_too_large", "Checkpoint exceeds its persistence byte limit", 413)
        existing = self.get(value["checkpoint_id"])
        if existing is not None:
            require(canonical_bytes(existing) == canonical_bytes(value),
                    "checkpoint_immutable_conflict", "Checkpoint identity has a different body", 409)
            self.materialize(value["checkpoint_id"])
            return existing
        if value["kind"] == "initial":
            prior = self.connection.execute(
                "SELECT checkpoint_id FROM workflow_checkpoints WHERE workflow_session_id=? AND kind='initial'",
                (self._owner["workflow_session_id"],),
            ).fetchone()
            require(prior is None, "checkpoint_initial_conflict",
                    "Target session already has an initial checkpoint", 409)
        else:
            self._chain(value["chain_run_id"])
        materialize_state(
            value["checkpoint_id"],
            lambda identity: value if identity == value["checkpoint_id"] else self.get(identity),
        )
        columns = ",".join(_CHECKPOINT_COLUMNS)
        self.connection.execute(
            "INSERT INTO workflow_checkpoints (" + columns + ") VALUES("
            + ",".join("?" for _ in _CHECKPOINT_COLUMNS) + ")",
            tuple(encoded if key == "state_storage_json" else value[key] for key in _CHECKPOINT_COLUMNS),
        )
        return deepcopy(value)

    def get_membership(self, chain_run_id):
        self._session()
        require(uuid_value(chain_run_id), "checkpoint_chain_invalid",
                "Historical chain identity must be a canonical UUID4")
        row = self.connection.execute(
            "SELECT * FROM workflow_history_membership WHERE workflow_session_id=? AND chain_run_id=?",
            (self._owner["workflow_session_id"], chain_run_id),
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        require(set(value) == set(_MEMBERSHIP_COLUMNS)
                and type(value["schema_version"]) is int and value["schema_version"] == 1
                and value["chain_run_id"] == chain_run_id
                and all(value[key] == self._owner[key] for key in _OWNER_FIELDS),
                "checkpoint_storage_invalid", "Historical membership body or owner differs", 500)
        self._chain(chain_run_id)
        return deepcopy(value)

    def _retain_chain(self, chain_run_id):
        """Only trusted proof transactions use this for their derived artifact closure."""
        self._session()
        self._chain(chain_run_id)
        existing = self.get_membership(chain_run_id)
        if existing is not None:
            return existing
        value = {"schema_version": 1, "chain_run_id": chain_run_id, **self._owner}
        self.connection.execute(
            "INSERT INTO workflow_history_membership VALUES(?,?,?,?,?)",
            tuple(value[key] for key in _MEMBERSHIP_COLUMNS),
        )
        return deepcopy(value)

    def retain_checkpoint_history(self, checkpoint_id):
        """Retain chains derived from an owned persisted checkpoint prefix, not caller IDs."""
        return [self._retain_chain(point["chain_run_id"])
                for point in self.prefix(checkpoint_id) if point["chain_run_id"] is not None]

    def get_head(self):
        self._session()
        rows = self.connection.execute(
            "SELECT * FROM records WHERE record_type='workflow_ref' "
            "AND json_extract(payload,'$.workflow_session_id')=?",
            (self._owner["workflow_session_id"],),
        ).fetchall()
        require(len(rows) <= 1, "checkpoint_storage_invalid",
                "Target session has more than one Head", 500)
        if not rows:
            return None
        row = rows[0]
        require(set(row.keys()) == _RECORD_COLUMNS and row["immutable"] == 0,
                "checkpoint_storage_invalid", "Head SQL columns or mutability differ", 500)
        value = validate_checkpoint_head(loads_strict(row["payload"]))
        require(row["record_type"] == "workflow_ref" and row["record_id"] == value["workflow_ref_id"]
                and value["workflow_session_id"] == self._owner["workflow_session_id"]
                and row["payload"] == canonical_bytes(value).decode("utf-8"),
                "checkpoint_storage_invalid", "Head SQL/body identity differs", 500)
        require(self.get(value["checkpoint_id"]) is not None,
                "checkpoint_head_invalid", "Head refers to a missing owned checkpoint", 500)
        return value

    def initialize_head(self, checkpoint_id, *, workflow_ref_id):
        self._session()
        require(uuid_value(workflow_ref_id), "checkpoint_head_invalid",
                "Head identity must be a canonical UUID4")
        point = self.get(checkpoint_id)
        require(point is not None and point["kind"] == "initial",
                "checkpoint_head_invalid", "Head initialization requires the owned initial checkpoint", 409)
        value = validate_checkpoint_head({
            "schema_version": 3, "execution_model": "graph", "workflow_ref_id": workflow_ref_id,
            "workflow_session_id": self._owner["workflow_session_id"], "checkpoint_id": checkpoint_id,
            "revision": 1,
        })
        prior = self.get_head()
        if prior is not None:
            require(canonical_bytes(prior) == canonical_bytes(value),
                    "checkpoint_head_conflict", "Session Head was already initialized differently", 409)
            return prior
        self.connection.execute(
            "INSERT INTO records (record_type,record_id,payload,immutable,created_at) VALUES(?,?,?,?,?)",
            ("workflow_ref", workflow_ref_id, canonical_bytes(value).decode("utf-8"), 0,
             datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")),
        )
        return deepcopy(value)

    def move_head(self, checkpoint_id, *, expected_revision, expected_checkpoint_id):
        self._session()
        require(_integer(expected_revision) and uuid_value(expected_checkpoint_id),
                "checkpoint_head_invalid", "Head move requires its exact current CAS basis")
        current = self.get_head()
        require(current is not None, "checkpoint_head_invalid", "Session has no initialized Head", 409)
        require(current["revision"] == expected_revision
                and current["checkpoint_id"] == expected_checkpoint_id,
                "stale_head_revision", "Session Head changed since it was read", 409)
        require(expected_revision < _MAX_INTEGER, "checkpoint_head_revision_exhausted",
                "Head revision exceeds its protocol limit", 409)
        self.materialize(checkpoint_id)
        value = validate_checkpoint_head({
            **current, "checkpoint_id": checkpoint_id, "revision": expected_revision + 1,
        })
        updated = self.connection.execute(
            "UPDATE records SET payload=? WHERE record_type='workflow_ref' AND record_id=? AND payload=?",
            (canonical_bytes(value).decode("utf-8"), current["workflow_ref_id"],
             canonical_bytes(current).decode("utf-8")),
        )
        require(updated.rowcount == 1, "stale_head_revision",
                "Session Head did not match its exact persisted CAS basis", 409)
        return deepcopy(value)
