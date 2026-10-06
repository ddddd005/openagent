"""Transactional SQLite storage for the agent pre-dispatch and archive boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Any

from .contract_errors import ContractValidationError
from .contract_graph import validate_bundle, validate_message_history, validate_turn_final
from .contract_json import canonical_bytes, content_digest, loads_strict
from .contracts_v2 import CONTRACT_SCHEMAS, validate_record
from .execution_facts import ExecutionFactHistory, validate_execution_fact


_IDENTITIES = {
    "node_definition": "component_id",
    "workflow_session": "workflow_session_id",
    "visible_message": "visible_message_id",
    "node_input": "input_id",
    "input_snapshot": "snapshot_id",
    "run_record": "run_id",
    "turn": "turn_id",
    "candidate_group": "candidate_group_id",
    "candidate_selection": "candidate_group_id",
    "node_run": "run_id",
    "agent_message": "message_id",
    "workflow_checkpoint": "checkpoint_id",
    "chain_run": "chain_run_id",
    "chain_input_origin": "chain_run_id",
    "execution_closeout": "closeout_id",
    "execution_continuation": "chain_run_id",
    "workflow_output": "output_id",
    "output_delivery": "delivery_id",
    "fork_anchor": "fork_anchor_id",
    "control_command": "command_id",
    "run_event": "event_id",
    "state_snapshot": "state_snapshot_id",
    "workflow_commit": "commit_id",
    "workflow_ref": "workflow_ref_id",
    "session_selection": "session_selection_id",
    "workflow_candidate": "candidate_id",
    "workflow_operation": "operation_id",
}
_COMPOSITE_IDENTITIES = {
    "node_definition": ("component_id", "component_version"),
    "node_binding": (
        "workflow_definition_id", "workflow_definition_revision", "node_binding_id",
    ),
    "workflow_definition_revision": ("workflow_definition_id", "revision"),
    "node_session": ("workflow_session_id", "node_binding_id"),
    "visible_message_ref": ("workflow_session_id", "visible_message_id"),
}
_PRE_DISPATCH = frozenset(("visible_message", "node_input", "input_snapshot", "run_record"))
_ARCHIVE = frozenset(("turn", "candidate_group", "run_record"))
_MUTABLE = frozenset((
    "run_record", "candidate_group", "candidate_selection", "chain_run", "node_session",
    "workflow_session", "visible_message_ref", "output_delivery", "workflow_ref",
    "session_selection",
))
_BUNDLE_ALLOWED = frozenset(CONTRACT_SCHEMAS) - {"candidate_selection", "turn"}
_BUNDLE_MUTABLE = _MUTABLE - {"candidate_selection"}
_RUN_EDGES = {
    "prepared": {"running", "paused", "failed", "recovery_unavailable", "closed"},
    "running": {"pausing", "failed", "final_ready", "recovery_unavailable", "closed"},
    "pausing": {"paused", "failed", "recovery_unavailable", "closed"},
    "paused": {"running", "superseded", "recovery_unavailable", "closed"},
    "final_ready": {"recovery_unavailable"},
    "failed": {"recovery_unavailable", "closed"},
    "recovery_unavailable": {"closed"},
}
_CHAIN_EDGES = {
    "prepared": {"running", "paused", "failed", "recovery_unavailable", "closed"},
    "running": {"paused", "failed", "succeeded", "recovery_unavailable", "closed"},
    "paused": {"running", "superseded", "recovery_unavailable", "closed"},
    "failed": {"recovery_unavailable", "closed"},
    "recovery_unavailable": {"closed"},
}
_REQUEST_DIGEST = re.compile(r"workflow-op-v1:[0-9a-f]{64}\Z")
ACTIVE_SESSION_SELECTION_ID = "7be319b8-30bd-4674-b7bf-d1cf54a1a10d"


def _fields(record_type: str) -> tuple[str, ...]:
    if record_type in _COMPOSITE_IDENTITIES:
        return _COMPOSITE_IDENTITIES[record_type]
    return (_IDENTITIES[record_type],)


def _record_id(record_type: str, record: Mapping[str, Any]) -> str:
    fields = _fields(record_type)
    if len(fields) == 1:
        return record[fields[0]]
    return canonical_bytes([record[field] for field in fields]).decode("utf-8")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


class SqliteStore:
    """Store validated public records; callers own execution and external dispatch."""

    def __init__(
        self, path: str | Path, fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._fault_injector = fault_injector
        try:
            self._connection = sqlite3.connect(Path(path), isolation_level=None, timeout=30)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("BEGIN IMMEDIATE")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            _require(version in range(14), "Unsupported SQLite storage version")
            if version == 0:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS records ("
                    "record_type TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL, "
                    "immutable INTEGER NOT NULL, created_at TEXT NOT NULL, "
                    "PRIMARY KEY (record_type, record_id))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS idempotency ("
                    "operation TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, "
                    "result_refs TEXT NOT NULL, result_payload TEXT NOT NULL, "
                    "PRIMARY KEY (operation, key))"
                )
            if version in (0, 1):
                self._connection.execute(
                    "ALTER TABLE idempotency ADD COLUMN request_digest TEXT"
                )
                self._connection.execute("PRAGMA user_version = 2")
            if version in (0, 1, 2):
                # The polymorphic records table needs no new columns for the
                # version objects; the marker gates their contract/CAS semantics.
                self._connection.execute("PRAGMA user_version = 3")
            if version in (0, 1, 2, 3):
                # Existing chain/message records stay intact; the new optional
                # reroll origin record has its own immutable identity.
                self._connection.execute("PRAGMA user_version = 4")
            if version in (0, 1, 2, 3, 4):
                # Past runs have no invented dispatch/usage facts after migration.
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS execution_facts ("
                    "fact_id TEXT PRIMARY KEY NOT NULL, run_id TEXT NOT NULL, "
                    "sequence INTEGER NOT NULL CHECK(sequence >= 1), payload TEXT NOT NULL, "
                    "UNIQUE (run_id, sequence))"
                )
                self._connection.execute("PRAGMA user_version = 5")
            if version in (0, 1, 2, 3, 4, 5):
                # Prompt configuration has a separate schema and catalog CAS;
                # migration does not invent configuration for existing runs.
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS prompt_revisions ("
                    "kind TEXT NOT NULL CHECK(kind IN ('item', 'group', 'config')), "
                    "definition_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1), "
                    "payload TEXT NOT NULL, PRIMARY KEY (kind, definition_id, revision))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS prompt_heads ("
                    "kind TEXT NOT NULL CHECK(kind IN ('item', 'group', 'config')), "
                    "definition_id TEXT NOT NULL, "
                    "definition_revision INTEGER NOT NULL CHECK(definition_revision >= 1), "
                    "catalog_revision INTEGER NOT NULL CHECK(catalog_revision >= 1), "
                    "selectable INTEGER NOT NULL CHECK(selectable IN (0, 1)), "
                    "PRIMARY KEY (kind, definition_id), "
                    "FOREIGN KEY (kind, definition_id, definition_revision) "
                    "REFERENCES prompt_revisions(kind, definition_id, revision))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS prompt_mutations ("
                    "idempotency_key TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, "
                    "result_payload TEXT NOT NULL)"
                )
                self._connection.execute("PRAGMA user_version = 6")
            if version in (0, 1, 2, 3, 4, 5, 6):
                # Preparation state is opt-in; existing sessions acquire no
                # invented definitions, assignments, or frozen variable values.
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS variable_registries ("
                    "workflow_id TEXT NOT NULL, "
                    "registry_revision INTEGER NOT NULL CHECK(registry_revision >= 1), "
                    "payload TEXT NOT NULL, PRIMARY KEY (workflow_id, registry_revision))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS variable_states ("
                    "workflow_session_id TEXT NOT NULL, workflow_id TEXT NOT NULL, "
                    "registry_revision INTEGER NOT NULL CHECK(registry_revision >= 1), "
                    "revision INTEGER NOT NULL CHECK(revision >= 1), payload TEXT NOT NULL, "
                    "PRIMARY KEY (workflow_session_id, workflow_id, registry_revision, revision), "
                    "FOREIGN KEY (workflow_id, registry_revision) "
                    "REFERENCES variable_registries(workflow_id, registry_revision))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS variable_heads ("
                    "workflow_session_id TEXT NOT NULL, workflow_id TEXT NOT NULL, "
                    "registry_revision INTEGER NOT NULL CHECK(registry_revision >= 1), "
                    "revision INTEGER NOT NULL CHECK(revision >= 1), "
                    "PRIMARY KEY (workflow_session_id, workflow_id, registry_revision), "
                    "FOREIGN KEY (workflow_session_id, workflow_id, registry_revision, revision) "
                    "REFERENCES variable_states("
                    "workflow_session_id, workflow_id, registry_revision, revision))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS variable_preparations ("
                    "idempotency_key TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, "
                    "payload TEXT NOT NULL)"
                )
                self._connection.execute("PRAGMA user_version = 7")
            if version in (0, 1, 2, 3, 4, 5, 6, 7):
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS variable_bindings ("
                    "owner_kind TEXT NOT NULL CHECK(owner_kind IN ("
                    "'input_snapshot', 'state_snapshot', 'workflow_commit', 'fork_anchor')), "
                    "owner_id TEXT NOT NULL, workflow_session_id TEXT NOT NULL, "
                    "payload TEXT NOT NULL, PRIMARY KEY (owner_kind, owner_id))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS result_port_submissions ("
                    "run_id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL)"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS result_port_releases ("
                    "run_id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL)"
                )
                self._connection.execute("PRAGMA user_version = 8")
            if version < 9:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS model_configuration_revisions ("
                    "kind TEXT NOT NULL CHECK(kind IN ('provider', 'model')), "
                    "identity TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1), "
                    "payload TEXT NOT NULL, PRIMARY KEY(kind, identity, revision))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS model_configuration_mutations ("
                    "idempotency_key TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                self._connection.execute("PRAGMA user_version = 9")
            if version < 10:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS exposure_configuration_revisions ("
                    "config_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1), "
                    "payload TEXT NOT NULL, PRIMARY KEY(config_id, revision))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS exposure_configuration_mutations ("
                    "idempotency_key TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                self._connection.execute("PRAGMA user_version = 10")
            if version < 11:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS program_variable_states ("
                    "session_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, "
                    "PRIMARY KEY(session_id, revision))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS program_variable_heads ("
                    "session_id TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL)"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS program_variable_receipts ("
                    "idempotency_key TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS program_variable_bindings ("
                    "owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, session_id TEXT NOT NULL, "
                    "revision INTEGER NOT NULL, PRIMARY KEY(owner_kind, owner_id))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS program_node_outputs ("
                    "chain_id TEXT NOT NULL, node_id TEXT NOT NULL, payload TEXT NOT NULL, "
                    "PRIMARY KEY(chain_id, node_id))"
                )
                self._connection.execute("PRAGMA user_version = 11")
            if version < 12:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS workbench_session_owners ("
                    "session_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL)"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS workbench_resource_revisions ("
                    "kind TEXT NOT NULL, resource_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                    "payload TEXT NOT NULL, PRIMARY KEY(kind,resource_id,revision))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS workbench_resource_heads ("
                    "kind TEXT NOT NULL, resource_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                    "deleted INTEGER NOT NULL, PRIMARY KEY(kind,resource_id))"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS workbench_resource_receipts ("
                    "operation_id TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                self._connection.execute("PRAGMA user_version = 12")
            if version < 13:
                from .session_objects import initialize_object_tables
                from .global_resources import initialize_global_resource_tables
                from .type_contract_store import initialize_type_contract_tables
                initialize_object_tables(self._connection)
                initialize_global_resource_tables(self._connection)
                initialize_type_contract_tables(self._connection)
                self._connection.execute("PRAGMA user_version = 13")
            self._connection.execute("COMMIT")
        except Exception as exc:
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

    def _get(self, record_type: str, record_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload FROM records WHERE record_type = ? AND record_id = ?",
            (record_type, record_id),
        ).fetchone()
        return loads_strict(row["payload"]) if row is not None else None

    def _write(self, record_type: str, record: dict[str, Any], *, update: bool = False) -> bool:
        from .graph_records import is_graph_record, validate_graph_transition
        record_id = _record_id(record_type, record)
        existing = self._get(record_type, record_id)
        if existing is not None:
            if canonical_bytes(existing) == canonical_bytes(record):
                return False
            if is_graph_record(record_type, record):
                _require(update, "Graph record update is not permitted")
                validate_graph_transition(record_type, existing, record)
            else:
                _require(update and record_type in _MUTABLE, "Immutable record identity conflict")
            if record_type == "candidate_group":
                _require(all(record.get(field) == existing.get(field) for field in (
                    "schema_version", "candidate_group_id", "workflow_session_id",
                    "node_binding_id", "logical_input_id", "parent_turn_id",
                    "frozen_snapshot_id", "created_at",
                )), "Candidate group frozen basis changed")
                old_refs = existing["candidate_refs"]
                _require(len(record["candidate_refs"]) > len(old_refs)
                         and record["candidate_refs"][:len(old_refs)] == old_refs,
                         "Candidate members must be append-only")
            self._connection.execute(
                "UPDATE records SET payload = ? WHERE record_type = ? AND record_id = ?",
                (canonical_bytes(record).decode("utf-8"), record_type, record_id),
            )
            return True
        timestamp = record.get(
            "created_at", datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        self._connection.execute(
            "INSERT INTO records (record_type, record_id, payload, immutable, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (record_type, record_id, canonical_bytes(record).decode("utf-8"),
             int(record_type not in _MUTABLE), timestamp),
        )
        return True

    @staticmethod
    def _validated(
        records: Sequence[tuple[str, Mapping[str, Any]]], allowed: frozenset[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        _require(isinstance(records, Sequence) and not isinstance(records, (str, bytes))
                 and bool(records), "Records must be a nonempty sequence")
        result: list[tuple[str, dict[str, Any]]] = []
        seen: set[tuple[str, str]] = set()
        for item in records:
            _require(isinstance(item, tuple) and len(item) == 2, "Record entry must be a type and value")
            record_type, value = item
            _require(type(record_type) is str and record_type in allowed, "Record type is not allowed here")
            _require(isinstance(value, Mapping), "Record value must be a mapping")
            record = validate_record(record_type, dict(value))
            identity = record_type, _record_id(record_type, record)
            _require(identity not in seen, "Duplicate record identity in batch")
            seen.add(identity)
            result.append((record_type, record))
        return result

    def _resolve(
        self, pending: dict[tuple[str, str], dict[str, Any]], record_type: str, record_id: str,
    ) -> dict[str, Any]:
        record = pending.get((record_type, record_id))
        if record is None:
            record = self._get(record_type, record_id)
        _require(record is not None, f"Missing {record_type} reference")
        return record

    def _check_pre_dispatch(self, pending: dict[tuple[str, str], dict[str, Any]]) -> None:
        owners: dict[str, str] = {}
        for kind in ("visible_message", "input_snapshot", "run_record"):
            records = self.list_records(kind) + [row for (record_kind, _), row in pending.items()
                                                 if record_kind == kind]
            for record in records:
                if kind == "visible_message":
                    if record["role"] != "user":
                        continue
                    input_id = record["source"]["input_id"]
                    session_id = record["origin_workflow_session_id"]
                else:
                    input_id, session_id = record["input_id"], record["workflow_session_id"]
                owner = owners.setdefault(input_id, session_id)
                _require(owner == session_id, "Input identity belongs to another workflow session")
        for (kind, _), record in pending.items():
            if kind == "visible_message":
                _require(record["role"] == "user", "Pre-dispatch visible message must be a user message")
            elif kind == "run_record":
                _require(record["status"] == "prepared" and record["result_turn_id"] is None
                         and record["superseded_by_run_id"] is None,
                         "Pre-dispatch run must be prepared without a result")
                snapshot = self._resolve(pending, "input_snapshot", record["snapshot_id"])
                for field in ("workflow_session_id", "node_binding_id", "input_id"):
                    _require(record[field] == snapshot[field], "Run and snapshot identity differ")
                self._resolve(pending, "node_input", record["input_id"])
            elif kind == "input_snapshot":
                self._resolve(pending, "node_input", record["input_id"])
                validate_message_history(record["s0"])
            if kind == "node_input" and record["source"]["kind"] == "visible_message":
                visible_id = record["source"]["visible_message_id"]
                visible = self._resolve(
                    pending, "visible_message", visible_id,
                )
                _require(visible["role"] == "user"
                         and visible["payload_schema_ref"] == record["payload_schema_ref"]
                         and canonical_bytes(visible["payload"]) == canonical_bytes(record["payload"]),
                         "Visible message and input differ")
                if ("visible_message", visible_id) in pending:
                    _require(visible["source"]["input_id"] == record["input_id"],
                             "New visible message points to another input")

    def _check_archive(self, pending: dict[tuple[str, str], dict[str, Any]]) -> None:
        runs = [record for (kind, _), record in pending.items() if kind == "run_record"]
        turns = [record for (kind, _), record in pending.items() if kind == "turn"]
        groups = [record for (kind, _), record in pending.items() if kind == "candidate_group"]
        _require(bool(runs and turns and groups), "Archive requires run, turn, and candidate group")
        for run in runs:
            _require(run["status"] == "succeeded" and run["result_turn_id"] is not None
                     and run["superseded_by_run_id"] is None, "Archive run must have a successful result")
            previous = self._get("run_record", run["run_id"])
            _require(previous is not None and previous["status"] in ("prepared", "final_ready")
                     and run["revision"] > previous["revision"], "Run is not eligible for archive")
            for field in (
                "schema_version", "profile", "run_id", "workflow_session_id",
                "node_binding_id", "chain_run_id", "input_id", "snapshot_id",
                "source_run_id", "initial_budget", "created_at",
            ):
                _require(run.get(field) == previous.get(field), "Archive changed run identity or frozen facts")
            turn = self._resolve(pending, "turn", run["result_turn_id"])
            _require(turn in turns and turn["run_id"] == run["run_id"]
                     and turn["snapshot_id"] == run["snapshot_id"]
                     and turn["input_id"] == run["input_id"], "Turn and archived run differ")
            snapshot = self._resolve(pending, "input_snapshot", run["snapshot_id"])
            _require(turn["parent_turn_id"] == snapshot["parent_turn_id"]
                     and turn["projection_version"] == snapshot["projection_version"],
                     "Turn differs from frozen snapshot")
            validate_turn_final(turn, snapshot)
            _require(any(
                {"run_id": run["run_id"], "turn_id": turn["turn_id"]} in group["candidate_refs"]
                for group in groups
            ), "Archived result is not a candidate")
        _require({turn["run_id"] for turn in turns} == {run["run_id"] for run in runs},
                 "Archive contains a turn without its run")
        existing_groups = self._connection.execute(
            "SELECT payload FROM records WHERE record_type = 'candidate_group'",
        ).fetchall()
        turn_owners = {
            ref["turn_id"]: group["candidate_group_id"]
            for row in existing_groups
            for group in (loads_strict(row["payload"]),)
            for ref in group["candidate_refs"]
        }
        for group in groups:
            snapshot = self._resolve(pending, "input_snapshot", group["frozen_snapshot_id"])
            basis_input = self._resolve(pending, "node_input", snapshot["input_id"])
            for field, source_field in (
                ("workflow_session_id", "workflow_session_id"),
                ("node_binding_id", "node_binding_id"),
                ("logical_input_id", "input_id"),
                ("parent_turn_id", "parent_turn_id"),
            ):
                _require(group[field] == snapshot[source_field], "Candidate group changed frozen basis")
            _require(len(group["candidate_refs"]) == len({
                ref["turn_id"] for ref in group["candidate_refs"]
            }), "Duplicate candidate member")
            for ref in group["candidate_refs"]:
                owner = turn_owners.setdefault(ref["turn_id"], group["candidate_group_id"])
                _require(owner == group["candidate_group_id"],
                         "Turn already belongs to another candidate group")
                turn = self._resolve(pending, "turn", ref["turn_id"])
                run = self._resolve(pending, "run_record", ref["run_id"])
                _require(turn["run_id"] == run["run_id"] and run["result_turn_id"] == turn["turn_id"]
                         and run["status"] == "succeeded", "Candidate reference is not a successful result")
                member_snapshot = self._resolve(pending, "input_snapshot", run["snapshot_id"])
                _require(run["workflow_session_id"] == group["workflow_session_id"]
                         and run["node_binding_id"] == group["node_binding_id"]
                         and turn["parent_turn_id"] == group["parent_turn_id"],
                         "Candidate member changed owner or parent")
                for field in (
                    "parent_turn_id", "component_id", "component_version", "config", "s0",
                    "tool_definitions", "model_parameters", "output_schema", "projection_version",
                ):
                    _require(canonical_bytes(member_snapshot[field]) == canonical_bytes(snapshot[field]),
                             "Candidate member changed frozen basis")
                member_input = self._resolve(pending, "node_input", run["input_id"])
                for field in ("payload", "payload_schema_ref", "port_id", "source"):
                    _require(canonical_bytes(member_input[field]) == canonical_bytes(basis_input[field]),
                             "Candidate member changed input content")

    @staticmethod
    def _check_member(group: dict[str, Any], turn_id: str | None) -> None:
        _require(turn_id is None or turn_id in {ref["turn_id"] for ref in group["candidate_refs"]},
                 "Selection is not a candidate member")

    def _save(
        self, records: Sequence[tuple[str, Mapping[str, Any]]], key: str, *,
        allowed: frozenset[str], operation: str,
    ) -> list[dict[str, Any]]:
        _require(type(key) is str and bool(key), "Idempotency key must be a nonempty string")
        values = self._validated(records, allowed)
        digest = content_digest([[kind, value] for kind, value in values])
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            receipt = self._connection.execute(
                "SELECT digest, result_payload FROM idempotency WHERE operation = ? AND key = ?",
                (operation, key),
            ).fetchone()
            if receipt is not None:
                _require(receipt["digest"] == digest, "Idempotency key has different content")
                result = loads_strict(receipt["result_payload"])
                self._connection.execute("COMMIT")
                return result
            pending = {(kind, _record_id(kind, value)): value for kind, value in values}
            if operation == "pre_dispatch":
                self._check_pre_dispatch(pending)
            else:
                self._check_archive(pending)
                if self._get_full_graph_available():
                    self._validate_merged(pending)
            first_write = True
            for kind, value in values:
                changed = self._write(kind, value, update=operation == "archive")
                if changed and first_write:
                    first_write = False
                    self._inject("after_first_write")
            result = [value for _, value in values]
            refs = [[kind, _record_id(kind, value)] for kind, value in values]
            self._connection.execute(
                "INSERT INTO idempotency (operation, key, digest, result_refs, result_payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (operation, key, digest, canonical_bytes(refs).decode("utf-8"),
                 canonical_bytes(result).decode("utf-8")),
            )
            self._inject("before_commit")
            self._connection.execute("COMMIT")
            return result
        except Exception as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise ContractValidationError("SQLite storage operation failed") from exc
            raise

    def save_pre_dispatch(
        self, records: Sequence[tuple[str, Mapping[str, Any]]], idempotency_key: str,
    ) -> list[dict[str, Any]]:
        return self._save(records, idempotency_key, allowed=_PRE_DISPATCH, operation="pre_dispatch")

    def archive_success(
        self, records: Sequence[tuple[str, Mapping[str, Any]]], idempotency_key: str,
    ) -> list[dict[str, Any]]:
        return self._save(records, idempotency_key, allowed=_ARCHIVE, operation="archive")

    def _get_full_graph_available(self) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM records WHERE record_type = 'workflow_session' LIMIT 1"
        ).fetchone() is not None

    def _validate_merged(self, pending: dict[tuple[str, str], dict[str, Any]]) -> None:
        rows = self._connection.execute(
            "SELECT record_type, record_id, payload FROM records ORDER BY rowid"
        ).fetchall()
        merged = {(row["record_type"], row["record_id"]): loads_strict(row["payload"])
                  for row in rows}
        merged.update(pending)
        bundle: dict[str, list[dict[str, Any]]] = {}
        for (kind, _), value in merged.items():
            bundle.setdefault(kind, []).append(value)
        validate_bundle(bundle)

    @staticmethod
    def _frozen(previous: dict[str, Any], value: dict[str, Any], mutable: set[str]) -> None:
        _require(
            set(previous) - mutable == set(value) - mutable
            and all(canonical_bytes(previous[field]) == canonical_bytes(value[field])
                    for field in previous if field not in mutable),
            "Stored record identity or frozen facts changed",
        )

    def _check_selection_projections(
        self, pending: dict[tuple[str, str], dict[str, Any]],
        expected_ref_heads: dict[str, tuple[int, str | None]],
        expected_selection_revisions: dict[str, int],
    ) -> None:
        selections = {identity: row for (kind, identity), row in pending.items()
                      if kind == "candidate_selection"}
        for group_id, revision in expected_selection_revisions.items():
            _require(type(group_id) is str and type(revision) is int and revision >= 0,
                     "Invalid expected candidate selection revision")
        _require(set(selections) == set(expected_selection_revisions),
                 "Candidate selection expectations must match projected records")
        if not selections:
            return
        refs = [row for (kind, _), row in pending.items() if kind == "workflow_ref"]
        _require(len(refs) == 1 and set(expected_ref_heads) == {refs[0]["workflow_ref_id"]},
                 "Candidate selections require one guarded workflow head update")
        ref = refs[0]
        _require(ref["head_commit_id"] is not None,
                 "Candidate selections require a target Head commit")
        commit = self._resolve(pending, "workflow_commit", ref["head_commit_id"])
        snapshot = self._resolve(pending, "state_snapshot", commit["state_snapshot_id"])
        _require(commit["workflow_session_id"] == ref["workflow_session_id"]
                 and snapshot["workflow_session_id"] == ref["workflow_session_id"],
                 "Target Head snapshot belongs to another workflow session")
        frozen = {item["candidate_group_id"]: item["selected_turn_id"]
                  for item in snapshot["selection_refs"]}
        for group_id, value in selections.items():
            group = self._get("candidate_group", group_id)
            _require(group is not None, "Missing archived candidate group")
            self._check_member(group, value["selected_turn_id"])
            _require(group_id in frozen and frozen[group_id] == value["selected_turn_id"],
                     "Candidate selection differs from target Head snapshot")
            previous = self._get("candidate_selection", group_id)
            revision = expected_selection_revisions[group_id]
            actual = previous["revision"] if previous is not None else 0
            _require(actual == revision, "Candidate selection revision conflict")
            _require(value["revision"] == revision + 1,
                     "Candidate selection revision must advance by one")
            if previous is not None:
                self._frozen(previous, value, {"selected_turn_id", "revision"})

    def _check_run_control_changes(
        self, values: list[tuple[str, dict[str, Any]]],
        expected: dict[str, int], operation: str,
    ) -> tuple[set[str], set[str], set[str]]:
        extensions: set[str] = set()
        resumed_runs: set[str] = set()
        resumed_chains: set[str] = set()
        controls = [row for kind, row in values if kind == "workflow_operation"]
        extension_controls = [row for row in controls if row["kind"] == "extend_budget"]
        _require(not extension_controls or operation == "workflow.extend_budget",
                 "Budget extension requires its dedicated workflow operation")
        if operation == "workflow.extend_budget":
            _require(len(values) == 3
                     and {kind for kind, _ in values}
                     == {"run_record", "workflow_session", "workflow_operation"}
                     and len(extension_controls) == 1,
                     "Budget extension must contain exactly its stopped run, session and control")
            control = extension_controls[0]
            run = next(row for kind, row in values if kind == "run_record")
            previous = self._get("run_record", run["run_id"])
            _require(previous is not None and previous["status"] in ("paused", "failed")
                     and run["status"] == previous["status"],
                     "Budget can only be extended for a stopped run")
            self._check_run_control_identity(control, previous, expected, "extend_budget")
            payload = control["payload"]
            _require(set(payload) == {
                "chain_run_id", "additional_model_requests", "additional_model_attempts",
                "max_model_requests", "max_model_attempts",
            }, "Budget extension payload fields differ from contract")
            for field, maximum in (
                ("additional_model_requests", 64), ("additional_model_attempts", 256),
            ):
                _require(type(payload[field]) is int and 0 <= payload[field] <= maximum,
                         "Budget extension delta is invalid")
            _require(payload["additional_model_requests"] + payload["additional_model_attempts"] > 0,
                     "Budget extension must add requests or attempts")
            initial = previous["initial_budget"]
            requests = initial["max_model_requests"]
            attempts = initial.get("max_model_attempts", requests * 4)
            for prior in self.list_records("workflow_operation"):
                if (prior["kind"] == "extend_budget"
                        and prior["target"] == control["target"]):
                    requests += prior["payload"]["additional_model_requests"]
                    attempts += prior["payload"]["additional_model_attempts"]
            requests += payload["additional_model_requests"]
            attempts += payload["additional_model_attempts"]
            _require(type(payload["max_model_requests"]) is int
                     and type(payload["max_model_attempts"]) is int
                     and payload["max_model_requests"] == requests
                     and payload["max_model_attempts"] == attempts
                     and 1 <= requests <= 64 and 1 <= attempts <= 256,
                     "Budget extension totals differ from saved allowances or exceed limits")
            extensions.add(run["run_id"])

        failed_resumes = [
            row for kind, row in values if kind == "run_record"
            and row["status"] == "running"
            and (self._get("run_record", row["run_id"]) or {}).get("status") == "failed"
        ]
        for run in failed_resumes:
            matching = [row for row in controls if row["kind"] == "resume"
                        and row["target"] == {"kind": "run_record", "id": run["run_id"]}]
            _require(operation == "workflow.resume" and len(matching) == 1,
                     "Failed run resume requires an explicit matching workflow control")
            previous = self._get("run_record", run["run_id"])
            control = matching[0]
            self._check_run_control_identity(control, previous, expected, "resume")
            _require(set(control["payload"]) == {"chain_run_id", "recovery_code"},
                     "Failed run resume must identify its recorded recovery reason")
            facts = self._read_execution_fact_history(run["run_id"]).facts
            failures = [fact["payload"] for fact in facts if fact["kind"] == "execution_failed"]
            recoverable = {
                "model_retry_exhausted": "model",
                "protocol_error": "protocol",
                "invalid_final": "protocol",
                "model_request_budget_exhausted": "model",
                "model_attempt_budget_exhausted": "model",
            }
            _require(bool(failures)
                     and failures[-1]["code"] in recoverable
                     and failures[-1]["category"] == recoverable[failures[-1]["code"]]
                     and control["payload"]["recovery_code"] == failures[-1]["code"],
                     "Failed run has no recorded recoverable failure")
            started = {fact["payload"]["attempt_id"] for fact in facts
                       if fact["kind"] == "model_attempt_started"}
            finished = {fact["payload"]["attempt_id"] for fact in facts
                        if fact["kind"] == "model_attempt_finished"}
            calls = {
                block["tool_call_id"] for fact in facts if fact["kind"] == "message_accepted"
                for block in fact["payload"]["message"]["blocks"] if block["kind"] == "tool_call"
            }
            settled = {fact["payload"]["tool_call_id"] for fact in facts
                       if fact["kind"] == "tool_settled" and fact["payload"]["message"] is not None}
            _require(started == finished and calls <= settled,
                     "Failed run resume cannot bypass an unsettled dispatch")
            chains = [row for kind, row in values if kind == "chain_run"
                      and row["chain_run_id"] == run["chain_run_id"]]
            previous_chain = self._get("chain_run", run["chain_run_id"])
            _require(len(chains) == 1 and previous_chain is not None
                     and previous_chain["status"] == "failed" and chains[0]["status"] == "running",
                     "Failed run and chain must resume together")
            resumed_runs.add(run["run_id"])
            resumed_chains.add(run["chain_run_id"])
        return extensions, resumed_runs, resumed_chains

    @staticmethod
    def _check_run_control_identity(
        control: dict[str, Any], run: dict[str, Any],
        expected: dict[str, int], kind: str,
    ) -> None:
        session_id = run["workflow_session_id"]
        revisions = {row["kind"]: row for row in control["expected_revisions"]}
        _require(control["kind"] == kind
                 and control["scope"] == {"kind": "workflow_session", "id": session_id}
                 and control["target"] == {"kind": "run_record", "id": run["run_id"]}
                 and control["payload"].get("chain_run_id") == run["chain_run_id"]
                 and set(revisions) == {"workflow_session", "run_record"}
                 and revisions["workflow_session"]["id"] == session_id
                 and revisions["workflow_session"]["revision"] == expected.get(session_id)
                 and revisions["run_record"]["id"] == run["run_id"]
                 and revisions["run_record"]["revision"] == run["revision"],
                 "Run control identity or expected revisions differ from its transaction")

    def _check_bundle_changes(
        self, values: list[tuple[str, dict[str, Any]]],
        expected: dict[str, int],
        expected_ref_heads: dict[str, tuple[int, str | None]],
        expected_selection_revisions: dict[str, int],
        expected_session_selection_revisions: dict[str, int],
        operation: str,
    ) -> None:
        extensions, resumed_runs, resumed_chains = self._check_run_control_changes(
            values, expected, operation,
        )
        sessions = {value["workflow_session_id"]: value for kind, value in values
                    if kind == "workflow_session"}
        refs = {value["workflow_ref_id"]: value for kind, value in values
                if kind == "workflow_ref"}
        active_selections = {value["session_selection_id"]: value for kind, value in values
                             if kind == "session_selection"}
        _require(set(active_selections) == set(expected_session_selection_revisions),
                 "Session selection expectations must match projected records")
        for selection_id, revision in expected_session_selection_revisions.items():
            _require(selection_id == ACTIVE_SESSION_SELECTION_ID
                     and type(revision) is int and revision >= 0,
                     "Invalid expected session selection revision")
            current = self._get("session_selection", selection_id)
            actual = current["revision"] if current is not None else 0
            _require(actual == revision, "Session selection revision conflict")
            _require(active_selections[selection_id]["revision"] == revision + 1,
                     "Session selection revision must advance by one")
        for session_id, revision in expected.items():
            _require(type(session_id) is str and type(revision) is int and revision >= 0,
                     "Invalid expected session revision")
            current = self._get("workflow_session", session_id)
            actual = current["revision"] if current is not None else 0
            _require(actual == revision, "Workflow session revision conflict")
            _require(session_id in sessions and sessions[session_id]["revision"] == revision + 1,
                     "Workflow session revision must advance by one")
        for ref_id, expectation in expected_ref_heads.items():
            _require(type(ref_id) is str and type(expectation) is tuple
                     and len(expectation) == 2, "Invalid expected workflow ref head")
            revision, head_commit_id = expectation
            _require(type(revision) is int and revision >= 0
                     and (head_commit_id is None or type(head_commit_id) is str),
                     "Invalid expected workflow ref head")
            current = self._get("workflow_ref", ref_id)
            actual_revision = current["revision"] if current is not None else 0
            actual_head = current["head_commit_id"] if current is not None else None
            _require(actual_revision == revision and actual_head == head_commit_id,
                     "Workflow ref head conflict")
            _require((ref_id not in refs and operation in (
                "workflow.close_execution", "workflow.reroll", "workflow.continue_workflow",
            )) or (ref_id in refs and refs[ref_id]["revision"] == revision + 1),
                     "Workflow ref revision must advance by one")
        for kind, value in values:
            key = _record_id(kind, value)
            previous = self._get(kind, key)
            if kind == "workflow_session" and previous != value:
                _require(value["workflow_session_id"] in expected,
                         "Workflow session update requires an expected revision")
            if kind == "workflow_ref" and previous != value:
                _require(key in expected_ref_heads,
                         "Workflow ref update requires an expected head")
            if kind == "session_selection" and previous != value:
                _require(key in expected_session_selection_revisions,
                         "Session selection update requires an expected revision")
            if kind == "run_record" and previous is None:
                _require(value["status"] == "prepared" and value["revision"] == 1
                         and (value["source_run_id"] is None or operation == "workflow.reroll")
                         and value["result_turn_id"] is None
                         and value["superseded_by_run_id"] is None,
                         "New Agent run must be prepared without reroll or result")
            if previous is None or canonical_bytes(previous) == canonical_bytes(value):
                continue
            _require(kind in _BUNDLE_MUTABLE or
                     (kind == "candidate_selection" and key in expected_selection_revisions),
                     "Immutable record identity conflict")
            if kind == "run_record":
                self._frozen(previous, value, {
                    "status", "revision", "result_turn_id", "superseded_by_run_id",
                })
                _require((value["status"] in _RUN_EDGES.get(previous["status"], set())
                          or key in extensions or key in resumed_runs)
                         and value["revision"] == previous["revision"] + 1
                         and value["result_turn_id"] is None
                         and (value["superseded_by_run_id"] is None
                              if value["status"] != "superseded" else
                              operation == "workflow.reroll"
                              and value["superseded_by_run_id"] is not None),
                         "Illegal Agent run transition or revision")
                if value["status"] == "closed":
                    _require(operation in ("workflow.close_execution", "workflow.reroll"),
                             "Run closeout requires an explicit workflow control operation")
            elif kind == "workflow_session":
                self._frozen(previous, value, {"revision"})
            elif kind == "workflow_ref":
                self._frozen(previous, value, {"revision", "head_commit_id"})
            elif kind == "session_selection":
                self._frozen(previous, value, {"revision", "active_workflow_session_id"})
            elif kind == "candidate_selection":
                self._frozen(previous, value, {"revision", "selected_turn_id"})
            elif kind == "node_session":
                self._frozen(previous, value, {"data_version", "private_data"})
                _require(value["data_version"] == previous["data_version"] + 1,
                         "Node data version must advance by one")
            elif kind == "chain_run":
                self._frozen(previous, value, {"status", "node_run_ids", "output_id"})
                if value["status"] == "superseded":
                    _require(operation == "workflow.reroll"
                             and previous["status"] == "paused",
                             "Only a paused chain can be replaced by a reroll")
                if value["status"] == "closed":
                    _require(operation in ("workflow.close_execution", "workflow.reroll")
                             and value["node_run_ids"] == previous["node_run_ids"]
                             and value["output_id"] == previous["output_id"],
                             "Chain closeout requires an explicit workflow control operation")
                if previous["status"] == "failed":
                    _require((value["status"] in ("recovery_unavailable", "closed")
                              or key in resumed_chains)
                             and value["node_run_ids"] == previous["node_run_ids"]
                             and value["output_id"] == previous["output_id"],
                             "Failed chain can only become recovery unavailable")
                else:
                    _require(previous["status"] in ("prepared", "running", "paused")
                             or (previous["status"] == "recovery_unavailable"
                                 and value["status"] == "closed"),
                             "Completed chain cannot change")
                _require(value["node_run_ids"][:len(previous["node_run_ids"])] ==
                         previous["node_run_ids"], "Chain run members must be append-only")
                _require(value["status"] == previous["status"] or key in resumed_chains
                         or value["status"] in _CHAIN_EDGES.get(previous["status"], set()),
                         "Illegal chain run transition")
                _require(previous["output_id"] is None or
                         value["output_id"] == previous["output_id"],
                         "Chain output cannot be replaced")
                _require(value["status"] == "succeeded" or value["output_id"] is None,
                         "Unfinished chain cannot own a completed output")
            elif kind == "visible_message_ref":
                self._frozen(previous, value, {"boundary"})
                old, new = previous["boundary"], value["boundary"]
                if value["role"] == "user":
                    _require(all(old[field] == new[field] for field in
                                 ("before_checkpoint_id", "input_id", "chain_run_id")
                                 if field != "chain_run_id" or old[field] is not None),
                             "Visible input boundary changed its source")
                    _require(new["input_status"] in {
                        "pending": {"running", "failed"},
                        "running": {"completed", "failed"},
                        "failed": {"completed"},
                    }.get(old["input_status"], set()), "Illegal visible input transition")
                else:
                    _require(old["chain_run_id"] == new["chain_run_id"]
                             and old["output_id"] == new["output_id"]
                             and old["after_checkpoint_id"] is None
                             and new["after_checkpoint_id"] is not None,
                             "Visible output boundary changed its source")
            elif kind == "output_delivery":
                self._frozen(previous, value, {"status"})
                _require(value["status"] in {
                    "pending": {"succeeded", "failed", "unknown"},
                    "unknown": {"succeeded", "failed"},
                }.get(previous["status"], set()), "Illegal delivery transition")
            # Candidate-group frozen basis and append-only membership are checked by _write.

    def _check_execution_closeouts(
        self, pending: dict[tuple[str, str], dict[str, Any]], *,
        operation: str, expected_session_revisions: dict[str, int],
        expected_ref_heads: dict[str, tuple[int, str | None]],
    ) -> None:
        closeouts = [value for (kind, _), value in pending.items()
                     if kind == "execution_closeout"]
        new_closeouts = [row for row in closeouts
                        if self._get("execution_closeout", row["closeout_id"]) is None]
        closed_runs = [row for (kind, _), row in pending.items()
                       if kind == "run_record" and row["status"] == "closed"
                       and (self._get("run_record", row["run_id"]) or {}).get("status") != "closed"]
        closed_chains = [row for (kind, _), row in pending.items()
                         if kind == "chain_run" and row["status"] == "closed"
                         and (self._get("chain_run", row["chain_run_id"]) or {}).get("status") != "closed"]
        _require({row["chain_run_id"] for row in new_closeouts}
                 == {row["chain_run_id"] for row in closed_chains}
                 and {run_id for row in new_closeouts for run_id in row["run_ids"]}
                 == {row["run_id"] for row in closed_runs},
                 "Execution closeout and closed records must be committed together")
        for closeout in new_closeouts:
            session_id = closeout["workflow_session_id"]
            _require(operation in ("workflow.close_execution", "workflow.reroll")
                     and session_id in expected_session_revisions,
                     "Execution closeout requires an explicit session revision guard")
            heads = [row for row in self.list_records("workflow_ref")
                     if row["workflow_session_id"] == session_id]
            _require(len(heads) == 1 and heads[0]["workflow_ref_id"] in expected_ref_heads,
                     "Execution closeout requires a guarded stable Head")
            projected_ref = self._resolve(pending, "workflow_ref", heads[0]["workflow_ref_id"])
            _require(projected_ref["head_commit_id"] == heads[0]["head_commit_id"],
                     "Execution closeout cannot move stable Head")
            previous_chain = self._get("chain_run", closeout["chain_run_id"])
            expected_reason = {
                "failed": "execution_failed", "recovery_unavailable": "lost_runtime",
                "paused": "paused_reroll", "prepared": "execution_failed",
                "running": "execution_failed",
            }.get((previous_chain or {}).get("status"))
            if (previous_chain or {}).get("status") == "paused" and operation == "workflow.close_execution":
                expected_reason = "execution_failed"
                _require(closeout["diagnostic"] == {
                    "code": "HOST_INTERRUPTED", "category": "interrupted",
                }, "Explicit paused closeout requires an unrecoverable host interruption")
            _require(expected_reason is not None and closeout["reason"] == expected_reason,
                     "Execution closeout reason differs from the stopped execution")
            if (previous_chain or {}).get("status") in ("prepared", "running"):
                _require(closeout["diagnostic"]["category"] == "contract"
                         or closeout["diagnostic"] == {
                             "code": "HOST_INTERRUPTED", "category": "interrupted",
                         },
                         "Active-state closeout requires an explicit program failure")
            controls = [
                row for (kind, _), row in pending.items()
                if kind == "workflow_operation"
                and row["kind"] == ("reroll" if expected_reason == "paused_reroll" else "close_execution")
                and row["scope"] == {"kind": "workflow_session", "id": session_id}
                and row["target"] == {"kind": "chain_run", "id": closeout["chain_run_id"]}
                and row["payload"].get("closeout_id") == closeout["closeout_id"]
            ]
            _require(len(controls) == 1, "Execution closeout has no matching control operation")
            control = controls[0]
            expected_by_kind = {row["kind"]: row for row in control["expected_revisions"]}
            _require(expected_by_kind["workflow_session"]["revision"]
                     == expected_session_revisions[session_id]
                     and expected_by_kind["workflow_ref"]["id"] == heads[0]["workflow_ref_id"]
                     and expected_by_kind["workflow_ref"]["revision"] == heads[0]["revision"]
                     and control.get("expected_head_commit_id") == heads[0]["head_commit_id"],
                     "Execution closeout control expectations differ from its transaction")
            for ref in closeout["fact_refs"]:
                facts = self._read_execution_fact_history(ref["run_id"]).facts
                _require(ref["sequence_count"] == len(facts) and ref["digest"] == content_digest(facts),
                         "Execution closeout fact summary differs from persisted evidence")
            failures = [
                fact["payload"]
                for run_id in previous_chain["node_run_ids"]
                if self._get("run_record", run_id) is not None
                for fact in self._read_execution_fact_history(run_id).facts
                if fact["kind"] == "execution_failed"
            ]
            if failures:
                coordination_failure = (
                    (previous_chain or {}).get("status") in ("prepared", "running")
                    and closeout["diagnostic"] == {
                        "code": "PROGRAM_OR_STORAGE_FAILED", "category": "contract",
                    }
                )
                _require(coordination_failure or closeout["diagnostic"] == {
                    field: failures[-1][field] for field in ("code", "category")
                }, "Execution closeout diagnostic differs from the saved failure")
            else:
                _require(closeout["diagnostic"]["category"] in ("unknown", "interrupted", "contract"),
                         "Execution closeout cannot invent an unrecorded failure classification")

    def _check_execution_continuations(
        self, pending: dict[tuple[str, str], dict[str, Any]], *,
        operation: str, expected_session_revisions: dict[str, int],
        expected_ref_heads: dict[str, tuple[int, str | None]],
    ) -> None:
        from .execution_recovery import interruption_evidence

        for (kind, chain_id), continuation in pending.items():
            if kind != "execution_continuation" or self._get(kind, chain_id) is not None:
                continue
            session_id = continuation["workflow_session_id"]
            _require(operation == "workflow.continue_workflow"
                     and session_id in expected_session_revisions,
                     "Continuation requires an explicit guarded workflow operation")
            closeout = self._resolve(pending, "execution_closeout", continuation["closeout_id"])
            heads = [row for row in self.list_records("workflow_ref")
                     if row["workflow_session_id"] == session_id]
            _require(len(heads) == 1 and heads[0]["workflow_ref_id"] in expected_ref_heads,
                     "Continuation requires a guarded stable Head")
            ref_id = heads[0]["workflow_ref_id"]
            _require(self._resolve(pending, "workflow_ref", ref_id)["head_commit_id"]
                     == heads[0]["head_commit_id"], "Continuation cannot move stable Head before a result")
            chain = self._resolve(pending, "chain_run", chain_id)
            control = self._resolve(pending, "workflow_operation", chain["operation_id"])
            expected_by_kind = {row["kind"]: row for row in control["expected_revisions"]}
            _require(control["kind"] == "continue_workflow"
                     and expected_by_kind["workflow_session"]["revision"]
                     == expected_session_revisions[session_id]
                     and expected_by_kind["workflow_ref"]["id"] == ref_id
                     and expected_by_kind["workflow_ref"]["revision"] == heads[0]["revision"]
                     and control.get("expected_head_commit_id") == heads[0]["head_commit_id"],
                     "Continuation expectations differ from its transaction")
            histories = {
                ref["run_id"]: self._read_execution_fact_history(ref["run_id"]).facts
                for ref in closeout["fact_refs"]
            }
            expected_text = interruption_evidence(
                closeout, continuation["source_run_id"], histories,
            )
            _require(continuation["evidence_message"]["blocks"]
                     == [{"kind": "text", "text": expected_text}],
                     "Continuation text differs from the complete saved evidence")

    def save_bundle(
        self, records: Sequence[tuple[str, dict]], idempotency_key: str, *,
        operation: str = "bundle",
        expected_session_revisions: dict[str, int] | None = None,
        expected_ref_heads: dict[str, tuple[int, str | None]] | None = None,
        expected_selection_revisions: dict[str, int] | None = None,
        expected_session_selection_revisions: dict[str, int] | None = None,
        request_digest: str | None = None,
        additional_request_receipt: tuple[str, str, str, dict] | None = None,
    ) -> list[dict]:
        """Atomically save public records with their established receipt and CAS checks."""
        return self._save_bundle_transaction(
            records, idempotency_key, operation=operation,
            expected_session_revisions=expected_session_revisions,
            expected_ref_heads=expected_ref_heads,
            expected_selection_revisions=expected_selection_revisions,
            expected_session_selection_revisions=expected_session_selection_revisions,
            request_digest=request_digest, additional_request_receipt=additional_request_receipt,
        )

    def save_bundle_prepared(
        self, builder: Callable[[SqliteStore], Sequence[tuple[str, dict]]],
        idempotency_key: str, *, request_digest: str, operation: str = "bundle",
        expected_session_revisions: dict[str, int] | None = None,
        expected_ref_heads: dict[str, tuple[int, str | None]] | None = None,
        expected_selection_revisions: dict[str, int] | None = None,
        expected_session_selection_revisions: dict[str, int] | None = None,
        additional_request_receipt: tuple[str, str, str, dict] | None = None,
        after_save: Callable[[SqliteStore, list[dict]], None] | None = None,
    ) -> list[dict]:
        """Prepare variables and records under one durable, replay-safe boundary.

        The request fingerprint is checked before invoking either callback.
        ``builder`` may use transaction-composable stores and returns the public
        bundle. ``after_save`` may bind its newly saved stable references. Neither
        callback may dispatch models/tools or commit the caller-owned transaction.
        """
        _require(callable(builder), "Prepared bundle requires a record builder")
        _require(after_save is None or callable(after_save),
                 "Prepared bundle requires a callable post-save binder")
        _require(type(request_digest) is str
                 and _REQUEST_DIGEST.fullmatch(request_digest) is not None,
                 "Prepared bundle requires an explicit workflow request digest")
        return self._save_bundle_transaction(
            (), idempotency_key, operation=operation,
            expected_session_revisions=expected_session_revisions,
            expected_ref_heads=expected_ref_heads,
            expected_selection_revisions=expected_selection_revisions,
            expected_session_selection_revisions=expected_session_selection_revisions,
            request_digest=request_digest, additional_request_receipt=additional_request_receipt,
            builder=builder, after_save=after_save,
        )

    def _save_bundle_transaction(
        self, records: Sequence[tuple[str, dict]], idempotency_key: str, *,
        operation: str = "bundle",
        expected_session_revisions: dict[str, int] | None = None,
        expected_ref_heads: dict[str, tuple[int, str | None]] | None = None,
        expected_selection_revisions: dict[str, int] | None = None,
        expected_session_selection_revisions: dict[str, int] | None = None,
        request_digest: str | None = None,
        additional_request_receipt: tuple[str, str, str, dict] | None = None,
        builder: Callable[[SqliteStore], Sequence[tuple[str, dict]]] | None = None,
        after_save: Callable[[SqliteStore, list[dict]], None] | None = None,
    ) -> list[dict]:
        """Atomically persist a bundle with content or explicit request identity.

        A request digest identifies the caller's logical operation, so an exact
        replay returns the original result even if regenerated records differ.
        Without one, the established record-content identity remains in force.
        An optional additional request receipt shares this transaction and
        retains the public envelope separately from strict internal records.
        Candidate selections may accompany exactly one guarded Head movement;
        standalone node selections retain their separate legacy API.
        """
        _require(type(idempotency_key) is str and bool(idempotency_key),
                 "Idempotency key must be a nonempty string")
        _require(type(operation) is str and bool(operation)
                 and operation not in {"archive", "pre_dispatch"},
                 "Invalid bundle operation")
        _require(expected_session_revisions is None or type(expected_session_revisions) is dict,
                 "Expected session revisions must be a mapping")
        _require(expected_ref_heads is None or type(expected_ref_heads) is dict,
                 "Expected workflow ref heads must be a mapping")
        _require(expected_selection_revisions is None or
                 type(expected_selection_revisions) is dict,
                 "Expected candidate selection revisions must be a mapping")
        _require(expected_session_selection_revisions is None or
                 type(expected_session_selection_revisions) is dict,
                 "Expected session selection revisions must be a mapping")
        _require(request_digest is None or
                 (type(request_digest) is str and _REQUEST_DIGEST.fullmatch(request_digest) is not None),
                 "Invalid workflow operation request digest")
        if additional_request_receipt is not None:
            _require(
                type(additional_request_receipt) is tuple
                and len(additional_request_receipt) == 4
                and all(type(value) is str and bool(value) for value in additional_request_receipt[:3])
                and _REQUEST_DIGEST.fullmatch(additional_request_receipt[2]) is not None,
                "Invalid additional request receipt",
            )
            _require(type(additional_request_receipt[3]) is dict,
                     "Additional receipt requires a request object")
            _require(additional_request_receipt[2] ==
                     "workflow-op-v1:" + content_digest(additional_request_receipt[3]).rsplit(":", 1)[1],
                     "Additional receipt digest differs from its request")
            _require(
                additional_request_receipt[:2] != (operation, idempotency_key),
                "Additional receipt must have a separate identity",
            )
        _require(not self._connection.in_transaction, "Bundle requires ownership of its transaction")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if additional_request_receipt is not None:
                identity_operation, identity_key, identity_digest, identity_request = additional_request_receipt
                identity = self._connection.execute(
                    "SELECT request_digest, result_payload FROM idempotency "
                    "WHERE operation = ? AND key = ?",
                    (identity_operation, identity_key),
                ).fetchone()
                if identity is not None:
                    _require(identity["request_digest"] == identity_digest,
                             "Idempotency key has different workflow operation request")
                    result = loads_strict(identity["result_payload"])
                    self._connection.execute("COMMIT")
                    return result
            receipt = self._connection.execute(
                "SELECT digest, request_digest, result_payload FROM idempotency "
                "WHERE operation = ? AND key = ?",
                (operation, idempotency_key),
            ).fetchone()
            if builder is not None:
                if receipt is not None:
                    _require(additional_request_receipt is None,
                             "Idempotency key already belongs to another operation identity")
                    _require(receipt["request_digest"] == request_digest,
                             "Idempotency key has different content")
                    result = loads_strict(receipt["result_payload"])
                    self._connection.execute("COMMIT")
                    return result
                records = builder(self)
                _require(self._connection.in_transaction,
                         "Prepared bundle builder ended its caller-owned transaction")
            allowed = (_BUNDLE_ALLOWED | {"candidate_selection"}
                       if expected_selection_revisions is not None else _BUNDLE_ALLOWED)
            values = self._validated(records, allowed)
            digest = content_digest([[kind, value] for kind, value in values])
            if receipt is not None:
                _require(additional_request_receipt is None,
                         "Idempotency key already belongs to another operation identity")
                prior_digest = (receipt["request_digest"] if receipt["request_digest"] is not None
                                else receipt["digest"])
                _require(prior_digest ==
                         (request_digest or digest), "Idempotency key has different content")
                result = loads_strict(receipt["result_payload"])
                self._connection.execute("COMMIT")
                return result
            pending = {(kind, _record_id(kind, value)): value for kind, value in values}
            self._check_bundle_changes(values, expected_session_revisions or {},
                                       expected_ref_heads or {},
                                       expected_selection_revisions or {},
                                       expected_session_selection_revisions or {},
                                       operation)
            self._check_selection_projections(
                pending, expected_ref_heads or {}, expected_selection_revisions or {},
            )
            self._check_execution_closeouts(
                pending, operation=operation,
                expected_session_revisions=expected_session_revisions or {},
                expected_ref_heads=expected_ref_heads or {},
            )
            self._check_execution_continuations(
                pending, operation=operation,
                expected_session_revisions=expected_session_revisions or {},
                expected_ref_heads=expected_ref_heads or {},
            )
            self._validate_merged(pending)
            first_write = True
            for kind, value in values:
                changed = self._write(kind, value, update=True)
                if changed and first_write:
                    first_write = False
                    self._inject("after_first_write")
            result = [value for _, value in values]
            if after_save is not None:
                after_save(self, result)
                _require(self._connection.in_transaction,
                         "Prepared bundle binder ended its caller-owned transaction")
            refs = [[kind, _record_id(kind, value)] for kind, value in values]
            self._connection.execute(
                "INSERT INTO idempotency "
                "(operation, key, digest, result_refs, result_payload, request_digest) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (operation, idempotency_key, digest, canonical_bytes(refs).decode("utf-8"),
                 canonical_bytes(result).decode("utf-8"), request_digest),
            )
            if additional_request_receipt is not None:
                self._connection.execute(
                    "INSERT INTO idempotency "
                    "(operation, key, digest, result_refs, result_payload, request_digest) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (identity_operation, identity_key, digest,
                     canonical_bytes(refs).decode("utf-8"),
                     canonical_bytes([*result, {"dispatch_request": identity_request}]).decode("utf-8"),
                     identity_digest),
                )
            self._inject("before_commit")
            self._connection.execute("COMMIT")
            return result
        except BaseException as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise ContractValidationError("SQLite storage operation failed") from exc
            raise

    def read_receipt(self, operation: str, key: str) -> list[dict] | None:
        receipt = self.read_receipt_with_digest(operation, key)
        return receipt[1] if receipt is not None else None

    def read_receipt_with_digest(
        self, operation: str, key: str,
    ) -> tuple[str, list[dict]] | None:
        """Return the request fingerprint, or legacy content digest, and result."""
        _require(type(operation) is str and bool(operation)
                 and type(key) is str and bool(key), "Invalid receipt identity")
        try:
            row = self._connection.execute(
                "SELECT digest, request_digest, result_payload FROM idempotency "
                "WHERE operation = ? AND key = ?",
                (operation, key),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ContractValidationError("SQLite read failed") from exc
        if row is None:
            return None
        identity_digest = (row["request_digest"] if row["request_digest"] is not None
                           else row["digest"])
        return identity_digest, loads_strict(row["result_payload"])

    def select_candidate(self, selection: Mapping[str, Any], expected_revision: int) -> dict[str, Any]:
        _require(isinstance(selection, Mapping), "Selection must be a mapping")
        value = validate_record("candidate_selection", dict(selection))
        _require(type(expected_revision) is int and expected_revision >= 0,
                 "Expected revision must be a nonnegative integer")
        _require(value["revision"] == expected_revision + 1, "Selection revision must advance by one")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            group = self._get("candidate_group", value["candidate_group_id"])
            _require(group is not None, "Missing candidate group")
            self._check_member(group, value["selected_turn_id"])
            previous = self._get("candidate_selection", value["candidate_group_id"])
            current_revision = previous["revision"] if previous is not None else 0
            _require(current_revision == expected_revision, "Candidate selection revision conflict")
            if previous is not None:
                _require(value.get("created_at") == previous.get("created_at"),
                         "Selection creation timestamp cannot change")
            self._write("candidate_selection", value, update=True)
            self._connection.execute("COMMIT")
            return value
        except Exception as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise ContractValidationError("SQLite storage operation failed") from exc
            raise

    def get_record(self, record_type: str, identity: Mapping[str, Any]) -> dict[str, Any]:
        _require(type(record_type) is str and record_type in CONTRACT_SCHEMAS,
                 "Unknown stored record type")
        fields = _fields(record_type)
        _require(isinstance(identity, Mapping) and set(identity) == set(fields)
                 and all(type(identity[field]) is (
                     int if field in ("revision", "workflow_definition_revision") else str
                 )
                         for field in fields), "Invalid record identity")
        try:
            result = self._get(record_type, _record_id(record_type, identity))
        except sqlite3.Error as exc:
            raise ContractValidationError("SQLite read failed") from exc
        _require(result is not None, "Record not found")
        return result

    def list_records(self, record_type: str, *, include_graph: bool = False) -> list[dict[str, Any]]:
        _require(type(record_type) is str and record_type in CONTRACT_SCHEMAS,
                 "Unknown stored record type")
        try:
            rows = self._connection.execute(
                "SELECT payload FROM records WHERE record_type = ? ORDER BY rowid", (record_type,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ContractValidationError("SQLite read failed") from exc
        values = [loads_strict(row["payload"]) for row in rows]
        if include_graph:
            return values
        from .graph_records import is_graph_record
        return [value for value in values if not is_graph_record(record_type, value)]

    def read_bundle(self, *, include_graph: bool = False) -> dict[str, list[dict[str, Any]]]:
        try:
            rows = self._connection.execute(
                "SELECT record_type, payload FROM records ORDER BY rowid",
            ).fetchall()
        except sqlite3.Error as exc:
            raise ContractValidationError("SQLite read failed") from exc
        bundle: dict[str, list[dict[str, Any]]] = {}
        from .graph_records import is_graph_record
        for row in rows:
            value = loads_strict(row["payload"])
            if include_graph or not is_graph_record(row["record_type"], value):
                bundle.setdefault(row["record_type"], []).append(value)
        return bundle

    def _execution_fact_owner(self, fact: dict[str, Any]) -> dict[str, Any]:
        run = self._get("run_record", fact["run_id"])
        _require(run is not None, "Execution fact refers to a missing Agent run")
        run = validate_record("run_record", run)
        snapshot = self._get("input_snapshot", fact["snapshot_id"])
        _require(snapshot is not None, "Execution fact refers to a missing snapshot")
        snapshot = validate_record("input_snapshot", snapshot)
        chain = self._get("chain_run", fact["chain_run_id"])
        _require(chain is not None, "Execution fact refers to a missing chain")
        chain = validate_record("chain_run", chain)
        _require(all(run[field] == fact[field] for field in (
            "run_id", "chain_run_id", "workflow_session_id", "node_binding_id", "snapshot_id",
        )), "Execution fact differs from run ownership")
        _require(all(snapshot[field] == fact[field] for field in (
            "snapshot_id", "workflow_session_id", "node_binding_id",
        )) and snapshot["input_id"] == run["input_id"],
                 "Execution fact differs from frozen snapshot ownership")
        _require(chain["workflow_session_id"] == fact["workflow_session_id"]
                 and run["run_id"] in chain["node_run_ids"],
                 "Execution fact differs from chain membership")
        return snapshot

    def _read_execution_fact_history(
        self, run_id: str, *, owner: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> ExecutionFactHistory:
        rows = self._connection.execute(
            "SELECT fact_id, run_id, sequence, payload FROM execution_facts "
            "WHERE run_id = ? ORDER BY sequence", (run_id,),
        ).fetchall()
        if rows and owner is None:
            owner = validate_execution_fact(loads_strict(rows[0]["payload"]))
            snapshot = self._execution_fact_owner(owner)
        history = ExecutionFactHistory(snapshot, owner=owner)
        for row in rows:
            fact = history.append(loads_strict(row["payload"]))
            _require(fact["fact_id"] == row["fact_id"] and fact["run_id"] == row["run_id"]
                     and fact["sequence"] == row["sequence"],
                     "Execution fact columns differ from stored payload")
        return history

    def append_execution_fact(
        self, fact: dict[str, Any], *, expected_sequence: int,
    ) -> dict[str, Any]:
        """Commit one immutable fact before the caller crosses its next boundary."""
        value = validate_execution_fact(fact)
        _require(type(expected_sequence) is int and expected_sequence >= 0,
                 "Expected execution fact sequence must be nonnegative")
        _require(value["sequence"] == expected_sequence + 1,
                 "Execution fact sequence must advance by one")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            snapshot = self._execution_fact_owner(value)
            history = self._read_execution_fact_history(
                value["run_id"], owner=value, snapshot=snapshot,
            )
            facts = history.facts
            existing = self._connection.execute(
                "SELECT payload FROM execution_facts WHERE fact_id = ?", (value["fact_id"],),
            ).fetchone()
            if existing is not None:
                previous = validate_execution_fact(loads_strict(existing["payload"]))
                _require(canonical_bytes(previous) == canonical_bytes(value),
                         "Immutable execution fact identity conflict")
                _require(any(item["fact_id"] == value["fact_id"] for item in facts),
                         "Execution fact identity has inconsistent ownership")
                self._connection.execute("COMMIT")
                return previous
            run = self._get("run_record", value["run_id"])
            chain = self._get("chain_run", value["chain_run_id"])
            _require(run is not None and run["status"] not in ("closed", "superseded")
                     and chain is not None and chain["status"] not in ("closed", "superseded"),
                     "Closed execution cannot append new facts")
            _require(len(facts) == expected_sequence, "Execution fact sequence conflict")
            history.append(value)
            self._inject("fact.before_write")
            self._connection.execute(
                "INSERT INTO execution_facts (fact_id, run_id, sequence, payload) "
                "VALUES (?, ?, ?, ?)",
                (value["fact_id"], value["run_id"], value["sequence"],
                 canonical_bytes(value).decode("utf-8")),
            )
            self._inject("fact.after_write")
            self._inject("fact.before_commit")
            self._connection.execute("COMMIT")
            return value
        except Exception as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise ContractValidationError("SQLite execution fact write failed") from exc
            raise

    def read_execution_facts(self, run_id: str) -> list[dict[str, Any]]:
        """Read and revalidate facts; an absent ledger means no facts were saved."""
        _require(type(run_id) is str and bool(run_id), "Invalid execution fact run identity")
        if self._connection.in_transaction:
            try:
                return self._read_execution_fact_history(run_id).facts
            except sqlite3.Error as exc:
                raise ContractValidationError("SQLite execution fact read failed") from exc
        try:
            self._connection.execute("BEGIN")
            result = self._read_execution_fact_history(run_id).facts
            self._connection.execute("COMMIT")
            return result
        except Exception as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise ContractValidationError("SQLite execution fact read failed") from exc
            raise
