"""Strict node-result ports released only after durable success and output save.

The fixed-workflow owner supplies the allowed A/B bindings and explicit routes.
This boundary does not run a kernel, select candidates, update Head, or commit
transactions. Its journal is a delivery mapping, never a second history store.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import sqlite3
from typing import Any, Mapping
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history, validate_turn_final
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .contracts_v2 import validate_record
from .storage import SqliteStore


_PACKAGE_FIELDS = {"schema_version", "kind", "turn", "group", "run", "output"}
_ROUTE_FIELDS = {
    "schema_version", "kind", "node_binding_id", "archive_route", "final_route", "context_route",
}
_SUBMISSION_FIELDS = {
    "schema_version", "kind", "identity", "archive_key", "package_digest", "routes", "package",
}
_RECEIPT_FIELDS = {
    "schema_version", "kind", "identity", "archive_key", "package_digest",
    "submission_digest", "stored_record_refs",
}
_RELEASE_FIELDS = {
    "schema_version", "kind", "identity", "archive_key", "package_digest",
    "archive_receipt_digest", "ports",
}
_TABLES = frozenset({"result_port_submissions", "result_port_releases"})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _fields(value: Any, fields: set[str], description: str) -> None:
    _require(type(value) is dict and set(value) == fields, f"{description} fields must be exact")


def _uuid(value: Any, description: str) -> None:
    _require(type(value) is str, f"{description} must be a canonical UUID4")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ContractValidationError(f"{description} must be a canonical UUID4") from None
    _require(parsed.version == 4 and str(parsed) == value,
             f"{description} must be a canonical UUID4")


def _bindings(allowed_bindings: tuple[str, str]) -> None:
    _require(type(allowed_bindings) is tuple and len(allowed_bindings) == 2,
             "Result ports require the two explicit fixed Agent bindings")
    for binding in allowed_bindings:
        _uuid(binding, "Allowed result binding")
    _require(allowed_bindings[0] != allowed_bindings[1], "Result bindings must be distinct")


def make_result_port_routes(
    node_binding_id: str, *, allowed_bindings: tuple[str, str],
) -> dict[str, Any]:
    """Declare all required fixed routes; absence is never filled by validation."""
    _bindings(allowed_bindings)
    _uuid(node_binding_id, "Result route owner")
    _require(node_binding_id in allowed_bindings, "Result route owner is not a fixed Agent binding")
    return {
        "schema_version": 1, "kind": "result_port_routes", "node_binding_id": node_binding_id,
        "archive_route": {
            "kind": "node_archive", "route_id": "fixed-node-archive:" + node_binding_id,
        },
        "final_route": {
            "kind": "workflow_final", "route_id": "fixed-node-final:" + node_binding_id,
        },
        "context_route": {
            "kind": "node_context_delta", "route_id": "fixed-node-context:" + node_binding_id,
        },
    }


def validate_result_port_routes(
    value: Mapping[str, Any], *, allowed_bindings: tuple[str, str],
) -> dict[str, Any]:
    validate_json_value(value)
    _fields(value, _ROUTE_FIELDS, "Result routes")
    expected = make_result_port_routes(value["node_binding_id"], allowed_bindings=allowed_bindings)
    _require(canonical_bytes(value) == canonical_bytes(expected), "Result routes are missing or incompatible")
    return expected


def make_result_package(
    turn: Mapping[str, Any], group: Mapping[str, Any],
    run: Mapping[str, Any], output: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture one already accepted result; snapshot/fact checks remain owner gates."""
    turn = validate_record("turn", turn)
    group = validate_record("candidate_group", group)
    run = validate_record("run_record", run)
    output = validate_record("workflow_output", output)
    _require(
        run["status"] == "succeeded" and run["result_turn_id"] == turn["turn_id"]
        and run["superseded_by_run_id"] is None, "Result package needs a successful run closure",
    )
    _require(
        turn["run_id"] == run["run_id"] and turn["input_id"] == run["input_id"]
        and turn["snapshot_id"] == run["snapshot_id"],
        "Result Turn and run identities differ",
    )
    _require(
        group["workflow_session_id"] == run["workflow_session_id"]
        and group["node_binding_id"] == run["node_binding_id"]
        and group["logical_input_id"] == run["input_id"]
        and group["frozen_snapshot_id"] == run["snapshot_id"]
        and group["parent_turn_id"] == turn["parent_turn_id"]
        and group["candidate_refs"] == [{"run_id": run["run_id"], "turn_id": turn["turn_id"]}],
        "Result candidate group does not identify this one accepted result",
    )
    _require(
        output["workflow_session_id"] == run["workflow_session_id"]
        and output["chain_run_id"] == run["chain_run_id"]
        and output["source"] == {
            "kind": "node_run", "node_binding_id": run["node_binding_id"],
            "run_id": run["run_id"], "turn_id": turn["turn_id"],
        }
        and output["port_id"] == "reply"
        and canonical_bytes(output["payload"]) == canonical_bytes(turn["final"]["value"]),
        "Result output differs from its accepted final or owner",
    )
    validate_message_history(turn["messages"])
    final = next(
        (message for message in turn["messages"]
         if message["message_id"] == turn["final"]["message_id"]), None,
    )
    _require(
        final is not None and final["role"] == "assistant" and final["source"]["kind"] == "model",
        "Result final must refer to an accepted model message",
    )
    _require(
        next(message for message in reversed(turn["messages"]) if message["role"] == "assistant")[
            "message_id"
        ] == final["message_id"],
        "Result final must be the last accepted assistant message",
    )
    return {
        "schema_version": 1, "kind": "node_result_package",
        "turn": turn, "group": group, "run": run, "output": output,
    }


def validate_result_package(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_json_value(value)
    _fields(value, _PACKAGE_FIELDS, "Result package")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["kind"] == "node_result_package", "Unknown result package version or kind")
    return make_result_package(value["turn"], value["group"], value["run"], value["output"])


def _identity(package: dict[str, Any]) -> dict[str, Any]:
    run, turn = package["run"], package["turn"]
    return {
        "workflow_session_id": run["workflow_session_id"], "node_binding_id": run["node_binding_id"],
        "chain_run_id": run["chain_run_id"], "run_id": run["run_id"], "turn_id": turn["turn_id"],
        "input_id": run["input_id"], "snapshot_id": run["snapshot_id"],
        "output_id": package["output"]["output_id"],
        "candidate_group_id": package["group"]["candidate_group_id"],
    }


def make_archive_submission(
    package: Mapping[str, Any], routes: Mapping[str, Any], *, allowed_bindings: tuple[str, str],
) -> dict[str, Any]:
    """Build an internal-only package; this is not either formal output port."""
    package = validate_result_package(package)
    routes = validate_result_port_routes(routes, allowed_bindings=allowed_bindings)
    _require(routes["node_binding_id"] == package["run"]["node_binding_id"],
             "Result routes belong to another Agent binding")
    identity = _identity(package)
    return {
        "schema_version": 1, "kind": "result_archive_submission", "identity": identity,
        "archive_key": "archive:" + identity["run_id"], "package_digest": content_digest(package),
        "routes": routes, "package": package,
    }


def validate_archive_submission(
    value: Mapping[str, Any], *, allowed_bindings: tuple[str, str],
) -> dict[str, Any]:
    validate_json_value(value)
    _fields(value, _SUBMISSION_FIELDS, "Archive submission")
    expected = make_archive_submission(value["package"], value["routes"], allowed_bindings=allowed_bindings)
    _require(canonical_bytes(value) == canonical_bytes(expected), "Archive submission correspondence is invalid")
    return expected


def _archive_records(package: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [("turn", package["turn"]), ("candidate_group", package["group"]), ("run_record", package["run"])]


def make_archive_receipt(
    submission: Mapping[str, Any], saved_records: list[dict[str, Any]], *,
    allowed_bindings: tuple[str, str],
) -> dict[str, Any]:
    """Check the archive's exact Turn/group/run receipt, not a claimed success flag."""
    submission = validate_archive_submission(submission, allowed_bindings=allowed_bindings)
    validate_json_value(saved_records)
    _require(type(saved_records) is list and len(saved_records) == 3,
             "Archive receipt must contain the three accepted public records")
    records = {}
    for value in saved_records:
        _require(type(value) is dict, "Archived record must be a JSON object")
        kind = (
            "candidate_group" if "candidate_group_id" in value
            else "turn" if "turn_id" in value
            else "run_record" if value.get("profile") == "agent" else None
        )
        _require(kind is not None and kind not in records, "Archive receipt contains unknown or duplicate records")
        records[kind] = validate_record(kind, value)
    expected = _archive_records(submission["package"])
    _require(
        set(records) == {kind for kind, _value in expected}
        and all(canonical_bytes(records[kind]) == canonical_bytes(value) for kind, value in expected),
        "Archive receipt does not preserve this exact accepted package",
    )
    identity = submission["identity"]
    return {
        "schema_version": 1, "kind": "result_archive_receipt",
        "identity": copy.deepcopy(identity), "archive_key": submission["archive_key"],
        "package_digest": submission["package_digest"], "submission_digest": content_digest(submission),
        "stored_record_refs": [
            {"record_type": kind, "record_id": identity[key]}
            for kind, key in (
                ("turn", "turn_id"), ("candidate_group", "candidate_group_id"), ("run_record", "run_id"),
            )
        ],
    }


def validate_archive_receipt(
    value: Mapping[str, Any], submission: Mapping[str, Any], *, allowed_bindings: tuple[str, str],
) -> dict[str, Any]:
    validate_json_value(value)
    _fields(value, _RECEIPT_FIELDS, "Archive receipt")
    submission = validate_archive_submission(submission, allowed_bindings=allowed_bindings)
    expected = make_archive_receipt(
        submission, [record for _kind, record in _archive_records(submission["package"])],
        allowed_bindings=allowed_bindings,
    )
    _require(canonical_bytes(value) == canonical_bytes(expected), "Archive receipt correspondence is invalid")
    return expected


def release_result_ports(
    submission: Mapping[str, Any], receipt: Mapping[str, Any], *, allowed_bindings: tuple[str, str],
) -> dict[str, Any]:
    """Form both projections of one saved result; persistence authorizes consumption."""
    submission = validate_archive_submission(submission, allowed_bindings=allowed_bindings)
    receipt = validate_archive_receipt(receipt, submission, allowed_bindings=allowed_bindings)
    identity = submission["identity"]
    source = {"kind": "node_result", **identity}
    package, routes = submission["package"], submission["routes"]
    turn = package["turn"]
    return {
        "schema_version": 1, "kind": "result_port_release", "identity": copy.deepcopy(identity),
        "archive_key": submission["archive_key"], "package_digest": submission["package_digest"],
        "archive_receipt_digest": content_digest(receipt),
        "ports": {
            "final": {
                "schema_version": 1, "kind": "node_final_port",
                "route_id": routes["final_route"]["route_id"],
                "delivery_id": "result-final:" + identity["run_id"],
                "source": copy.deepcopy(source), "value": copy.deepcopy(turn["final"]["value"]),
            },
            "context_delta": {
                "schema_version": 1, "kind": "node_context_delta_port",
                "route_id": routes["context_route"]["route_id"],
                "delivery_id": "result-context:" + identity["run_id"],
                "source": copy.deepcopy(source), "parent_turn_id": turn["parent_turn_id"],
                "message_ids": [message["message_id"] for message in turn["messages"]],
                "messages": copy.deepcopy(turn["messages"]),
                "final_message_id": turn["final"]["message_id"],
            },
        },
    }


def validate_result_port_release(
    value: Mapping[str, Any], submission: Mapping[str, Any], receipt: Mapping[str, Any], *,
    allowed_bindings: tuple[str, str],
) -> dict[str, Any]:
    validate_json_value(value)
    _fields(value, _RELEASE_FIELDS, "Result release")
    expected = release_result_ports(submission, receipt, allowed_bindings=allowed_bindings)
    _require(canonical_bytes(value) == canonical_bytes(expected), "Result release correspondence is invalid")
    return expected


class ResultPortStore:
    """Immutable submission/release mapping composed into owner transactions."""

    def __init__(self, store: SqliteStore, *, allowed_bindings: tuple[str, str]) -> None:
        _require(isinstance(store, SqliteStore), "Result ports require the workflow's SQLite store")
        _bindings(allowed_bindings)
        self._store, self._connection, self._allowed = store, store._connection, allowed_bindings

    @contextmanager
    def _write(self):
        _require(self._connection.in_transaction, "Result port writes require the caller's transaction")
        self._connection.execute("SAVEPOINT result_port_write")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK TO SAVEPOINT result_port_write")
            self._connection.execute("RELEASE SAVEPOINT result_port_write")
            raise
        else:
            self._connection.execute("RELEASE SAVEPOINT result_port_write")

    def _read(self, table: str, run_id: str) -> dict[str, Any] | None:
        _require(table in _TABLES, "Unknown result port journal")
        _uuid(run_id, "Result run identity")
        try:
            row = self._connection.execute(
                f"SELECT payload, digest FROM {table} WHERE run_id = ?", (run_id,),
            ).fetchone()
        except sqlite3.Error:
            raise ContractValidationError("Result port storage operation failed") from None
        if row is None:
            return None
        value = loads_strict(row["payload"])
        _require(content_digest(value) == row["digest"], "Stored result port journal digest is invalid")
        _require(type(value) is dict and type(value.get("identity")) is dict
                 and value["identity"].get("run_id") == run_id,
                 "Stored result port journal identity is invalid")
        return value

    def _put(self, table: str, value: dict[str, Any]) -> dict[str, Any]:
        identity = value["identity"]["run_id"]
        prior = self._read(table, identity)
        if prior is not None:
            _require(canonical_bytes(prior) == canonical_bytes(value),
                     "Result identity was reused with different content")
            return prior
        self._connection.execute(
            f"INSERT INTO {table}(run_id, payload, digest) VALUES (?, ?, ?)",
            (identity, canonical_bytes(value).decode("utf-8"), content_digest(value)),
        )
        return copy.deepcopy(value)

    def get_submission(self, run_id: str) -> dict[str, Any] | None:
        value = self._read("result_port_submissions", run_id)
        if value is None:
            return None
        return validate_archive_submission(value, allowed_bindings=self._allowed)

    def _check_accepted_package(self, submission: dict[str, Any]) -> None:
        package = submission["package"]
        current = self._store.get_record("run_record", {"run_id": package["run"]["run_id"]})
        incoming = package["run"]
        if current["status"] == "final_ready":
            _require(
                incoming["revision"] == current["revision"] + 1
                and all(canonical_bytes(current[key]) == canonical_bytes(incoming[key])
                        for key in current if key not in ("status", "revision", "result_turn_id")),
                "Accepted package differs from its final-ready run",
            )
        else:
            _require(current == incoming and current["status"] == "succeeded",
                     "Only an accepted final-ready or archived run can submit result ports")
        snapshot = self._store.get_record("input_snapshot", {"snapshot_id": incoming["snapshot_id"]})
        _require(
            snapshot["workflow_session_id"] == incoming["workflow_session_id"]
            and snapshot["node_binding_id"] == incoming["node_binding_id"]
            and snapshot["input_id"] == incoming["input_id"]
            and snapshot["parent_turn_id"] == package["turn"]["parent_turn_id"]
            and snapshot["projection_version"] == package["turn"]["projection_version"],
            "Accepted package differs from its frozen input",
        )
        validate_turn_final(package["turn"], snapshot)
        validate_message_history(snapshot["s0"] + package["turn"]["messages"])
        facts = self._store.read_execution_facts(incoming["run_id"])
        accepted = [fact["payload"]["message"] for fact in facts if fact["kind"] == "message_accepted"]
        _require(canonical_bytes(accepted) == canonical_bytes(package["turn"]["messages"]),
                 "Accepted package differs from its saved execution facts")

    def put_submission_in_transaction(self, submission: Mapping[str, Any]) -> dict[str, Any]:
        submission = validate_archive_submission(submission, allowed_bindings=self._allowed)
        with self._write():
            prior = self.get_submission(submission["identity"]["run_id"])
            if prior is not None:
                _require(canonical_bytes(prior) == canonical_bytes(submission),
                         "Result identity was reused with different content")
                return prior
            self._check_accepted_package(submission)
            return self._put("result_port_submissions", submission)

    def _saved_receipt(self, submission: dict[str, Any]) -> dict[str, Any]:
        saved = self._store.read_receipt("archive", submission["archive_key"])
        _require(saved is not None, "Result ports require a saved archive receipt")
        receipt = make_archive_receipt(submission, saved, allowed_bindings=self._allowed)
        for kind, record in _archive_records(submission["package"]):
            key = {"turn": "turn_id", "candidate_group": "candidate_group_id", "run_record": "run_id"}[kind]
            actual = self._store.get_record(kind, {key: record[key]})
            _require(canonical_bytes(actual) == canonical_bytes(record), "Archived result record differs from receipt")
        output = submission["package"]["output"]
        actual_output = self._store.get_record("workflow_output", {"output_id": output["output_id"]})
        _require(canonical_bytes(actual_output) == canonical_bytes(output),
                 "Formal result ports require this exact saved business output")
        return receipt

    def release_in_transaction(
        self, submission: Mapping[str, Any], receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        submission = validate_archive_submission(submission, allowed_bindings=self._allowed)
        receipt = validate_archive_receipt(receipt, submission, allowed_bindings=self._allowed)
        with self._write():
            prior = self.get_submission(submission["identity"]["run_id"])
            _require(prior is not None and canonical_bytes(prior) == canonical_bytes(submission),
                     "Result release has no matching accepted submission")
            saved_receipt = self._saved_receipt(submission)
            _require(canonical_bytes(receipt) == canonical_bytes(saved_receipt),
                     "Result release differs from saved archive evidence")
            value = release_result_ports(submission, receipt, allowed_bindings=self._allowed)
            return self._put("result_port_releases", value)

    def get_release(self, run_id: str) -> dict[str, Any] | None:
        value = self._read("result_port_releases", run_id)
        if value is None:
            return None
        submission = self.get_submission(run_id)
        _require(submission is not None, "Stored result release has no accepted submission")
        return validate_result_port_release(
            value, submission, self._saved_receipt(submission), allowed_bindings=self._allowed,
        )

    def read_port(self, run_id: str, port: str) -> dict[str, Any] | None:
        _require(type(port) is str and port in ("final", "context_delta"), "Unknown formal result port")
        release = self.get_release(run_id)
        return None if release is None else copy.deepcopy(release["ports"][port])
