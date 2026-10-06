"""Scoped graph Agent archive reads without store or coordinator construction."""

from contextlib import closing
from pathlib import Path
import sqlite3

from .contract_errors import ContractValidationError
from .contract_json import loads_strict
from .graph_records import graph_error, is_graph_record, require, uuid_value, validate_graph_record
from .legacy_archive import read_closed_legacy_archive


_IDENTITIES = {
    "workflow_session": "workflow_session_id",
    "workflow_ref": "workflow_ref_id",
    "workflow_commit": "commit_id",
    "state_snapshot": "state_snapshot_id",
    "chain_run": "chain_run_id",
    "node_run": "run_id",
    "run_record": "run_id",
    "turn": "turn_id",
    "input_snapshot": "snapshot_id",
    "node_input": "input_id",
}


def _referenced(repo, kind, **identity):
    try:
        return repo.get(kind, **identity)
    except ContractValidationError as error:
        if getattr(error, "reason_code", None) != "not_found":
            raise
        raise graph_error("storage_contract_violation", "Archive reference is missing", 500) from error


def _history(repo, sid):
    refs = [row for row in repo.rows("workflow_ref") if row["workflow_session_id"] == sid]
    require(len(refs) == 1, "storage_contract_violation", "Session must have one Head", 500)
    commit = _referenced(repo, "workflow_commit", commit_id=refs[0]["head_commit_id"])
    snapshot = _referenced(repo, "state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
    require(commit["workflow_session_id"] == snapshot["workflow_session_id"] == sid,
            "storage_contract_violation", "Archive Head or snapshot has another owner", 500)
    chains = {row["chain_run_id"]: row for row in repo.rows("chain_run")}
    require(all(identity in chains and chains[identity]["status"] in ("succeeded", "closed")
                for identity in snapshot["history_refs"]),
            "storage_contract_violation", "Archive history refers to unsettled or missing chains", 500)
    own = [identity for identity, row in chains.items() if row["workflow_session_id"] == sid]
    return set([*snapshot["history_refs"], *own]), chains


def _legacy_references(source):
    refs = source.get("legacy_archives", [])
    require(type(refs) is list and all(
        type(ref) is dict and set(ref) == {"source_node_id", "target_node_id", "turn_ids"}
        and uuid_value(ref["source_node_id"]) and uuid_value(ref["target_node_id"])
        and type(ref["turn_ids"]) is list and len(ref["turn_ids"]) <= 2048
        and all(uuid_value(identity) for identity in ref["turn_ids"])
        and len(set(ref["turn_ids"])) == len(ref["turn_ids"]) for ref in refs),
        "storage_contract_violation", "Frozen legacy archive references are invalid", 500)
    return refs


def _validate_native_owner(repo, run, chain):
    require(run["workflow_session_id"] == chain["workflow_session_id"]
            and run["node_binding_id"] in chain["ordered_nodes"],
            "storage_contract_violation", "Archive run has another chain owner", 500)
    index = chain["ordered_nodes"].index(run["node_binding_id"])
    require(chain["node_run_ids"][index] == run["run_id"],
            "storage_contract_violation", "Archive run is outside its accepted execution plan", 500)
    base = run["agent"]["accepted"]["identity"]["base_commit_id"]
    commit = _referenced(repo, "workflow_commit", commit_id=base)
    snapshot = _referenced(repo, "state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
    require(commit["workflow_session_id"] == snapshot["workflow_session_id"] == run["workflow_session_id"],
            "storage_contract_violation", "Archive baseline has another owner", 500)


def resolve_graph_archive(repo, sid, turn_id, *, legacy_reader):
    """Read the original accepted archive within the session's frozen references."""
    session = repo.get("workflow_session", workflow_session_id=sid)
    history, chains = _history(repo, sid)
    packages = {}
    for run in repo.rows("node_run"):
        if run.get("agent") and run["agent"]["accepted"] and run["status"] == "succeeded" \
                and run["chain_run_id"] in history:
            package = run["agent"]["accepted"]
            _validate_native_owner(repo, run, chains[run["chain_run_id"]])
            require(package["turn"]["turn_id"] not in packages,
                    "storage_contract_violation", "Accepted archive identity repeats", 500)
            packages[package["turn"]["turn_id"]] = package
    package = packages.get(turn_id)
    if package is not None:
        evidence = package["snapshot"]["config"]["payload"]["graph_preparation"]
        return {"turn": package["turn"], "root": [evidence["current_root"]], "snapshot": package["snapshot"]}
    refs = _legacy_references(session["source"])
    reference = next((ref for ref in refs if turn_id in ref["turn_ids"]), None)
    require(reference is not None, "graph_history_scope_mismatch", "Archive is outside this session's frozen history", 403)
    archive = read_closed_legacy_archive(legacy_reader, turn_id)
    require(archive["snapshot"]["node_binding_id"] == reference["source_node_id"],
            "graph_history_scope_mismatch", "Legacy archive has another node", 403)
    index = reference["turn_ids"].index(turn_id)
    parent = reference["turn_ids"][index + 1] if index + 1 < len(reference["turn_ids"]) else None
    require(archive["turn"]["parent_turn_id"] == parent,
            "storage_contract_violation", "Legacy archive ancestry differs from its frozen references", 500)
    return archive


class _ReadOnlyArchiveRecords:
    """The minimal existing graph/legacy record interfaces, with no write methods."""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _decode(kind, row):
        value = loads_strict(row["payload"])
        require(type(value) is dict and value.get(_IDENTITIES[kind]) == row["record_id"],
                "storage_contract_violation", "Archived record identity differs", 500)
        return value

    def get_record(self, kind, identity):
        field = _IDENTITIES[kind]
        require(type(identity) is dict and set(identity) == {field} and uuid_value(identity[field]),
                "storage_contract_violation", "Stored archive reference identity differs", 500)
        row = self.connection.execute(
            "SELECT record_id,payload FROM records WHERE record_type=? AND record_id=?",
            (kind, identity[field]),
        ).fetchone()
        return self._decode(kind, row) if row is not None else None

    def get(self, kind, **identity):
        value = self.get_record(kind, identity)
        require(is_graph_record(kind, value), "not_found", "Graph record not found", 404)
        return validate_graph_record(kind, value)

    def rows(self, kind):
        values = []
        for row in self.connection.execute(
            "SELECT record_id,payload FROM records WHERE record_type=? ORDER BY rowid", (kind,),
        ):
            value = self._decode(kind, row)
            if is_graph_record(kind, value):
                values.append(validate_graph_record(kind, value))
        return values


def read_graph_archive(database_path, session_id, archive_id):
    """Read a consistent SQLite snapshot; never initialize, migrate or recover."""
    require(uuid_value(session_id) and uuid_value(archive_id),
            "invalid_request", "Archive requires exact session and turn identities")
    try:
        database = Path(database_path).resolve()
        require(database.is_file(), "not_found", "Archive database not found", 404)
        uri = database.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            require(version in range(14), "graph_archive_schema_unsupported",
                    "Archive storage version is not supported", 409)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            require({"record_type", "record_id", "payload"} <= columns,
                    "graph_archive_storage_unavailable", "Archive records are not available", 503)
            repo = _ReadOnlyArchiveRecords(connection)
            return resolve_graph_archive(repo, session_id, archive_id, legacy_reader=repo)
    except (sqlite3.Error, OSError):
        raise graph_error("graph_archive_storage_unavailable", "Archive storage is not available", 503) from None
    except ContractValidationError as error:
        if hasattr(error, "status_code"):
            raise
        raise graph_error("storage_contract_violation", "Stored archive evidence is invalid", 500) from error
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise graph_error("storage_contract_violation", "Stored archive evidence is invalid", 500) from error
