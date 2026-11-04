"""Seed frozen legacy facts through native transactions, never active dispatch."""

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import sqlite3

from phase1_agent.contract_json import canonical_bytes, content_digest
from phase1_agent.graph_application_contracts import RETIRED_RECEIPT_COMMANDS, validate_parameters
from phase1_agent.graph_application_identity import (
    application_command_receipt, bind_application_command, persist_application_command_identity,
)


def seed_local_draft(service, operation, scope, request):
    """Write one accepted local-draft record and its frozen application receipt.

    ``LocalDraftStore.save`` retired with S4-05a because no production path wrote
    drafts any more; tests and legacy receipt fixtures still need an already
    accepted draft.  This seeds exactly the rows the retired writer produced in
    one native transaction: the ``local_editor_drafts`` row, the native
    ``idempotency`` receipt and the application-command identity row.
    """
    address = request["address"]
    value = request["value"]
    record = {
        "schema_version": 1, "kind": "workflow.local-draft", "address": deepcopy(address),
        "format_version": request["format_version"],
        "sequence": request["expected_sequence"] + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "value": deepcopy(value), "deleted": value is None,
    }
    native_request = {key: item for key, item in request.items() if key != "idempotency_key"}
    native_operation = "graph." + operation
    digest = "workflow-op-v1:" + content_digest(native_request).rsplit(":", 1)[1]
    with bind_application_command(operation, scope, request):
        with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO local_editor_drafts VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(address) DO UPDATE SET sequence=excluded.sequence,"
                    "payload=excluded.payload",
                    (canonical_bytes(address).decode("utf-8"), address["scope"], address["kind"],
                     address["workflow_definition_id"], address["workflow_session_id"],
                     record["sequence"], canonical_bytes(record).decode("utf-8")))
                connection.execute(
                    "INSERT INTO idempotency "
                    "(operation,key,digest,result_refs,result_payload,request_digest) "
                    "VALUES(?,?,?,?,?,?)",
                    (native_operation, request["idempotency_key"], digest, "[]",
                     canonical_bytes([{"graph_result": record}]).decode("utf-8"), digest))
                persist_application_command_identity(
                    connection, table="idempotency", operation=native_operation,
                    key=request["idempotency_key"], request_digest=digest)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
    return record


def seed_legacy_consumer_receipt(service, operation, parameters):
    spec = next(item for item in RETIRED_RECEIPT_COMMANDS if item.name == operation)
    request = deepcopy(validate_parameters(spec, parameters))
    arguments = {key: value for key, value in request.items() if key != "idempotency_key"}
    key = request["idempotency_key"]
    with bind_application_command(operation, "consumer", request):
        if operation == "consumer.draft.save":
            raw = seed_local_draft(service, operation, "consumer", request)
        elif operation == "consumer.session.create":
            raw = service.create_session(**arguments, idempotency_key=key)
        else:
            sid = arguments.pop("session_id")
            if operation == "consumer.run.start":
                raw = service.start(sid, **arguments, idempotency_key=key)
            elif operation == "consumer.run.control":
                raw = service.control(sid, **arguments, idempotency_key=key)
            else:
                assert operation == "consumer.event.submit"
                native = {"session": sid, **arguments}

                def change(data, basis):
                    _, document = service._event_scope(
                        data, sid, request["workflow_definition_id"], request["definition_revision"])
                    binding = service._event_binding(
                        document, request["event_id"], request["event_schema_version"], consumer=True)
                    event = {
                        "event_id": request["event_id"], "schema_version": request["event_schema_version"],
                        "audience": binding["audience"], "idempotency_key": key,
                    }
                    return service._prepare_run(
                        data, sid, request["expected_revision"], request["payload"],
                        event=event, basis=basis)

                raw = service._start_run(
                    sid, key, native, change, operation="graph.consumer.event.submit")
    if operation == "consumer.draft.save":
        result = raw
    else:
        verb = {
            "consumer.session.create": "create", "consumer.run.start": "start",
            "consumer.run.control": "control", "consumer.event.submit": "event",
        }[operation]
        result = {
            "receipt": {
                "workflow_definition_id": raw["workflow_definition_id"],
                "definition_revision": raw["definition_revision"],
                "workflow_session_id": raw["workflow_session_id"],
                "session_revision": raw["revision"], "status": raw["status"],
                "chain_run_id": raw["active_chain_run_id"], "operation": verb, "idempotency_key": key,
            },
            "consumer": service.get_consumer(raw["workflow_session_id"]),
        }
    return {
        "schema_version": 1, "kind": "workflow.application-command", "result": result,
        "receipt": application_command_receipt(
            operation, "consumer", request, result, authority="service_receipt"),
    }
