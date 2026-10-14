"""Checkpoint records on one caller-owned SQLite transaction."""

from __future__ import annotations

from copy import deepcopy
import sqlite3

from .contract_errors import require, uuid_value
from .contract_json import canonical_bytes, loads_strict
from .graph_records import validate_graph_record
from .runtime_checkpoints import materialize as materialize_state, validate_checkpoint


_CHECKPOINT_COLUMNS = (
    "schema_version", "checkpoint_id", "workflow_definition_id", "workflow_session_id",
    "fixed_definition_revision", "parent_checkpoint_id", "state_storage_json",
)
_RECORD_COLUMNS = {"record_type", "record_id", "payload", "immutable", "created_at"}
_OWNER_FIELDS = ("workflow_definition_id", "workflow_session_id", "fixed_definition_revision")
_MAX_INTEGER = 2**53 - 1


def _integer(value, minimum=1):
    return type(value) is int and minimum <= value <= _MAX_INTEGER


def _connection(connection):
    require(isinstance(connection, sqlite3.Connection) and connection.row_factory is sqlite3.Row,
            "checkpoint_connection_invalid", "Checkpoint reads require a caller-owned Row connection", 500)


def _transaction(connection):
    require(connection.in_transaction, "checkpoint_transaction_required",
            "Checkpoint operations require one caller-owned transaction", 500)


def initialize_checkpoint_tables(connection):
    """Create the single checkpoint table explicitly inside the caller's transaction."""
    _connection(connection)
    _transaction(connection)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workflow_checkpoints ("
        "schema_version INTEGER NOT NULL CHECK(schema_version=2),"
        "checkpoint_id TEXT PRIMARY KEY NOT NULL,"
        "workflow_definition_id TEXT NOT NULL,"
        "workflow_session_id TEXT NOT NULL,"
        "fixed_definition_revision INTEGER NOT NULL CHECK(fixed_definition_revision>0),"
        "parent_checkpoint_id TEXT,"
        "state_storage_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS workflow_checkpoints_root "
        "ON workflow_checkpoints(workflow_session_id) WHERE parent_checkpoint_id IS NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS workflow_checkpoints_owner "
        "ON workflow_checkpoints(workflow_session_id, fixed_definition_revision)"
    )


class CheckpointRepository:
    """Session-scoped immutable storage; it never opens or commits a transaction."""

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

    def _session(self):
        _transaction(self.connection)
        row = self.connection.execute(
            "SELECT * FROM records WHERE record_type='workflow_session' AND record_id=?",
            (self._owner["workflow_session_id"],),
        ).fetchone()
        require(row is not None, "checkpoint_record_not_found",
                "Checkpoint owner session is missing", 404)
        require(set(row.keys()) == _RECORD_COLUMNS
                and row["record_type"] == "workflow_session"
                and row["record_id"] == self._owner["workflow_session_id"],
                "checkpoint_storage_invalid", "Session SQL identity differs", 500)
        session = validate_graph_record("workflow_session", loads_strict(row["payload"]))
        require(row["immutable"] == 0
                and session["workflow_session_id"] == row["record_id"],
                "checkpoint_storage_invalid", "Session body or mutability differs", 500)
        require((session["workflow_definition_id"], session["definition_revision"]) == (
            self._owner["workflow_definition_id"], self._owner["fixed_definition_revision"]),
            "checkpoint_owner_denied",
            "Checkpoint owner session has another workflow or fixed revision", 403)
        return session

    def _decode(self, row):
        require(set(row.keys()) == set(_CHECKPOINT_COLUMNS),
                "checkpoint_storage_invalid", "Checkpoint SQL columns differ", 500)
        value = {key: row[key] for key in _CHECKPOINT_COLUMNS if key != "state_storage_json"}
        value["state_storage"] = loads_strict(row["state_storage_json"])
        checked = validate_checkpoint(value)
        require(row["state_storage_json"] == canonical_bytes(checked["state_storage"]).decode("utf-8"),
                "checkpoint_storage_invalid", "Checkpoint storage is not its exact canonical body", 500)
        require(all(checked[key] == self._owner[key] for key in _OWNER_FIELDS),
                "checkpoint_owner_denied", "Checkpoint is not owned by this session", 403)
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
        """Return only the selected initial-to-target parent path."""
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

    def latest(self):
        """Return the sole leaf for this session/revision or report a real branch."""
        self._session()
        rows = self.connection.execute(
            "SELECT checkpoint_id FROM workflow_checkpoints "
            "WHERE workflow_definition_id=? AND workflow_session_id=? "
            "AND fixed_definition_revision=? ORDER BY rowid",
            tuple(self._owner[field] for field in _OWNER_FIELDS),
        ).fetchall()
        points = [self.get(row["checkpoint_id"]) for row in rows]
        if not points:
            return None
        parent_ids = {point["parent_checkpoint_id"] for point in points
                      if point["parent_checkpoint_id"] is not None}
        leaves = [point for point in points if point["checkpoint_id"] not in parent_ids]
        require(len(leaves) == 1, "checkpoint_lineage_ambiguous",
                "Checkpoint history does not have one current leaf", 409)
        prefix = self.prefix(leaves[0]["checkpoint_id"])
        require({point["checkpoint_id"] for point in prefix}
                == {point["checkpoint_id"] for point in points},
                "runtime_checkpoint_lineage_invalid",
                "Checkpoint history is not one complete parent chain")
        return leaves[0]

    def insert(self, checkpoint):
        """Insert one immutable point after validating its owned parent chain."""
        self._session()
        value = validate_checkpoint(checkpoint)
        require(all(value[key] == self._owner[key] for key in _OWNER_FIELDS),
                "checkpoint_owner_denied", "Cannot write another session's checkpoint", 403)
        encoded = canonical_bytes(value["state_storage"]).decode("utf-8")
        existing = self.get(value["checkpoint_id"])
        if existing is not None:
            require(canonical_bytes(existing) == canonical_bytes(value),
                    "checkpoint_immutable_conflict",
                    "Checkpoint identity has a different body", 409)
            self.materialize(value["checkpoint_id"])
            return existing
        if value["parent_checkpoint_id"] is None:
            prior = self.connection.execute(
                "SELECT checkpoint_id FROM workflow_checkpoints "
                "WHERE workflow_session_id=? AND parent_checkpoint_id IS NULL",
                (self._owner["workflow_session_id"],),
            ).fetchone()
            require(prior is None, "checkpoint_root_conflict",
                    "Checkpoint owner already has a full root", 409)
        materialize_state(
            value["checkpoint_id"],
            lambda identity: value if identity == value["checkpoint_id"] else self.get(identity),
        )
        self.connection.execute(
            "INSERT INTO workflow_checkpoints (" + ",".join(_CHECKPOINT_COLUMNS) + ") VALUES("
            + ",".join("?" for _ in _CHECKPOINT_COLUMNS) + ")",
            tuple(encoded if key == "state_storage_json" else value[key]
                  for key in _CHECKPOINT_COLUMNS),
        )
        return deepcopy(value)
