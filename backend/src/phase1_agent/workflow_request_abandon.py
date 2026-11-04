"""Durable arbitration of one unknown workflow request.

User-initiated termination of an unknown request is arbitrated inside one
SQLite write transaction: if the accepted business receipt already exists the
formal result is returned without re-executing anything; otherwise a durable
abandonment fact is written and every later execution of that exact native
request is refused by the existing atomic command entry.

The abandonment fact is one dedicated pseudo-receipt row in the existing
``idempotency`` table (operation ``graph.request.abandoned.v1:<native
operation>``, key and digest of the terminated native request).  That keeps the
project database table set unchanged, mirrors the existing
``graph.application.command-identity.v1`` origin rows and avoids a new
unclassified table for the development reset helper.

Only the caller-visible configuration save commands are arbitrable.  Evidence
must exist because the arbitration command must bind to the exact original
request contract; without it the outcome stays unknown instead of pretending a
successful cancellation.  Existing evidence resolution values are reused:
``verified`` for a committed request, ``archived`` for an abandoned one.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from .contract_errors import DiagnosticError, require
from .contract_json import canonical_bytes, content_digest, loads_strict
from .graph_application_identity import native_command_request
from .graph_records import uuid_value


ARBITRATION_OPERATION = "graph.request.abandon"
ABANDONED_OPERATION_PREFIX = "graph.request.abandoned.v1:"
TARGET_OPERATIONS = {
    "definition.update": "workflow-configuration",
    "definition.copy": "workflow-configuration-copy",
}


def _abandoned_operation(native_operation: str) -> str:
    return ABANDONED_OPERATION_PREFIX + native_operation


def abandoned_command(connection, operation: str, key: str):
    """Return the digested abandonment fact for one native command, if any."""
    row = connection.execute(
        "SELECT digest FROM idempotency WHERE operation=? AND key=?",
        (_abandoned_operation(operation), key)).fetchone()
    return None if row is None else row["digest"]


def _write_abandonment_fact(connection, native_operation: str, native_key: str,
                            native_digest: str) -> None:
    connection.execute(
        "INSERT INTO idempotency (operation,key,digest,result_refs,result_payload,request_digest) "
        "VALUES(?,?,?,?,?,?)",
        (_abandoned_operation(native_operation), native_key, native_digest, "[]",
         canonical_bytes([{"abandonment": {
             "operation": native_operation, "key": native_key,
             "request_digest": native_digest,
             "abandoned_at": datetime.now(timezone.utc).isoformat(
                 timespec="milliseconds").replace("+00:00", "Z")},
         }]).decode("utf-8"), native_digest))


def _unavailable(message: str, **details) -> DiagnosticError:
    return DiagnosticError("request_arbitration_unavailable", message, status=409, **details)


def _result_payload(connection, operation: str, key: str):
    row = connection.execute(
        "SELECT request_digest,digest,result_payload FROM idempotency "
        "WHERE operation=? AND key=?", (operation, key)).fetchone()
    if row is None:
        return None, None
    require(row["request_digest"] is not None, "request_arbitration_unavailable",
            "The stored receipt cannot identify its original request", 409)
    return row["request_digest"], loads_strict(row["result_payload"])


def _graph_result(payload) -> dict:
    require(type(payload) is list and len(payload) == 1 and type(payload[0]) is dict
            and set(payload[0]) == {"graph_result"}, "storage_contract_violation",
            "Invalid graph receipt", 500)
    result = payload[0]["graph_result"]
    require(type(result) is dict, "storage_contract_violation", "Invalid graph receipt", 500)
    return deepcopy(result)


def _arbitration_replay(connection, key: str, digest: str):
    prior = connection.execute(
        "SELECT request_digest,result_payload FROM idempotency WHERE operation=? AND key=?",
        (ARBITRATION_OPERATION, key)).fetchone()
    if prior is None:
        return None
    require(prior["request_digest"] == digest, "idempotency_conflict",
            "The arbitration key has a different request", 409)
    payload = loads_strict(prior["result_payload"])
    require(type(payload) is list and len(payload) == 1 and type(payload[0]) is dict
            and set(payload[0]) == {"graph_result"}, "storage_contract_violation",
            "Invalid arbitration receipt", 500)
    return deepcopy(payload[0]["graph_result"])


def _write_arbitration_result(connection, key: str, digest: str, result: dict) -> None:
    connection.execute(
        "INSERT INTO idempotency (operation,key,digest,result_refs,result_payload,request_digest) "
        "VALUES(?,?,?,?,?,?)",
        (ARBITRATION_OPERATION, key, digest, "[]",
         canonical_bytes([{"graph_result": result}]).decode("utf-8"), digest))


def abandon_request(service, workflow_definition_id, *, target_operation,
                    target_idempotency_key, target_request_digest, idempotency_key):
    """Arbitrate and durably terminate one unknown configuration save request."""
    from .storage import SqliteStore

    require(target_operation in TARGET_OPERATIONS, "invalid_request",
            "Abandon target operation is invalid")
    require(uuid_value(workflow_definition_id), "invalid_request",
            "Abandon workflow identity is invalid")
    request = {
        "workflow_definition_id": workflow_definition_id,
        "target_operation": target_operation,
        "target_idempotency_key": target_idempotency_key,
        "target_request_digest": target_request_digest,
    }
    digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
    store = SqliteStore(service.database)
    connection = store._connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            replayed = _arbitration_replay(connection, idempotency_key, digest)
            if replayed is not None:
                connection.execute("COMMIT")
                return replayed
            evidence = connection.execute(
                "SELECT owner_kind,owner_id,request_json,request_digest,resolution "
                "FROM pending_commands WHERE operation=? AND idempotency_key=?",
                (target_operation, target_idempotency_key)).fetchone()
            require(evidence is not None, "request_arbitration_unavailable",
                    "No original request evidence exists to arbitrate", 409)
            require(evidence["owner_kind"] == TARGET_OPERATIONS[target_operation]
                    and evidence["owner_id"] == workflow_definition_id,
                    "request_arbitration_unavailable",
                    "Original request evidence belongs to another workflow", 409)
            require(evidence["request_digest"] == target_request_digest,
                    "request_evidence_conflict", "Original request digest differs", 409)
            application_request = loads_strict(evidence["request_json"])
            native = native_command_request(target_operation, application_request)
            require(native is not None, "request_arbitration_unavailable",
                    "This target operation has no arbitrable native command", 409)
            native_operation, native_key = native["operation"], native["key"]
            native_digest = native["request_digest"]
            result = {
                "schema_version": 1, "kind": "workflow.request-arbitration",
                "operation": target_operation,
                "idempotency_key": target_idempotency_key,
                "request_digest": target_request_digest,
                "workflow_definition_id": workflow_definition_id,
            }
            receipt_digest, receipt_payload = _result_payload(
                connection, native_operation, native_key)
            if receipt_digest is not None:
                require(receipt_digest == native_digest, "request_arbitration_unavailable",
                        "Acceptance receipt belongs to another original request", 409)
                accepted = _graph_result(receipt_payload)
                resolution = evidence["resolution"]
                if resolution == "pending":
                    connection.execute(
                        "UPDATE pending_commands SET resolution='verified' "
                        "WHERE operation=? AND idempotency_key=?",
                        (target_operation, target_idempotency_key))
                    resolution = "verified"
                result.update(outcome="committed", result=accepted,
                              evidence_resolution=resolution or "verified")
                _write_arbitration_result(connection, idempotency_key, digest, result)
                connection.execute("COMMIT")
                return deepcopy(result)

            terminated = abandoned_command(connection, native_operation, native_key)
            require(terminated is None or terminated == native_digest, "idempotency_conflict",
                    "The request key was terminated with a different request", 409)
            require(evidence["resolution"] in ("pending", "archived"),
                    "request_arbitration_unavailable",
                    "Original request evidence cannot be archived by arbitration", 409)
            if terminated is None:
                _write_abandonment_fact(connection, native_operation, native_key, native_digest)
            if evidence["resolution"] == "pending":
                connection.execute(
                    "UPDATE pending_commands SET resolution='archived' "
                    "WHERE operation=? AND idempotency_key=?",
                    (target_operation, target_idempotency_key))
            result.update(outcome="abandoned", result=None, evidence_resolution="archived")
            _write_arbitration_result(connection, idempotency_key, digest, result)
            connection.execute("COMMIT")
            return deepcopy(result)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        store.close()
