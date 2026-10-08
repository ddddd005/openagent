"""Read frozen application receipts without constructing a runtime or store."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .graph_application_contracts import COMMANDS, validate_parameters
from .graph_application_identity import (
    APPLICATION_IDENTITY_OPERATION, application_command_identity, application_command_receipt,
    application_identity_key,
)
from .graph_records import uuid_value, validate_graph_record
from .host_sdk import ObjectBinding, ResourceIdentity, WriteIntent
from .storage import _record_id


_COMMANDS = {spec.name: spec for spec in COMMANDS}
_SESSION_FIELDS = {
    "schema_version", "execution_model", "workflow_session_id", "workflow_definition_id",
    "definition_revision", "revision", "source", "active_chain_run_id",
}
_SESSION_STATUSES = {
    "idle", "prepared", "running", "paused", "budget_exhausted", "archive_failed",
    "failed", "succeeded", "recovery_unavailable", "closed",
}


class _ReceiptEvidenceError(Exception):
    def __init__(self, reason):
        self.reason = reason


def _check(condition, reason="receipt_invalid"):
    if not condition:
        raise _ReceiptEvidenceError(reason)


def _unresolved(reason):
    return {"schema_version": 1, "kind": "workflow.application-receipt-read",
            "outcome": "unresolved", "reason_code": reason}


def _same(left, right):
    return canonical_bytes(left) == canonical_bytes(right)


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**53 - 1


def _columns(connection, table):
    return {row["name"] for row in connection.execute("PRAGMA table_info(" + table + ")")}


def _read_origin(connection, identity):
    columns = _columns(connection, "idempotency")
    _check({"operation", "key", "digest", "result_refs", "result_payload"} <= columns,
           "receipt_not_found")
    _check("request_digest" in columns, "insufficient_request_evidence")
    row = connection.execute(
        "SELECT digest,request_digest,result_refs,result_payload FROM idempotency "
        "WHERE operation=? AND key=?",
        (APPLICATION_IDENTITY_OPERATION,
         application_identity_key(identity["operation"], identity["operation_scope"],
                                  identity["idempotency_key"])),
    ).fetchone()
    _check(row is not None, "application_identity_missing")
    digest = "workflow-op-v1:" + identity["request_sha256"]
    _check(row["digest"] == digest and row["request_digest"] == digest, "request_mismatch")
    _check(loads_strict(row["result_refs"]) == [])
    payload = loads_strict(row["result_payload"])
    _check(type(payload) is list and len(payload) == 1 and type(payload[0]) is dict
           and set(payload[0]) == {"application_identity"})
    _check(_same(payload[0]["application_identity"], identity), "request_mismatch")


def _read_native(connection, identity):
    native = identity["native_receipt"]
    if native["table"] == "idempotency":
        row = connection.execute(
            "SELECT digest,request_digest,result_refs,result_payload FROM idempotency "
            "WHERE operation=? AND key=?", (native["operation"], native["key"]),
        ).fetchone()
        _check(row is not None, "native_receipt_missing")
        _check(row["request_digest"] is not None, "insufficient_request_evidence")
        _check(row["request_digest"] == native["request_digest"]
               and row["digest"] == native["request_digest"], "request_mismatch")
        _check(loads_strict(row["result_refs"]) == [])
        payload = loads_strict(row["result_payload"])
        _check(type(payload) is list and len(payload) == 1 and type(payload[0]) is dict
               and set(payload[0]) == {"graph_result"})
        result = payload[0]["graph_result"]
    else:
        _check({"idempotency_key", "digest", "payload"}
               <= _columns(connection, "global_resource_receipts"), "native_receipt_missing")
        row = connection.execute(
            "SELECT digest,payload FROM global_resource_receipts WHERE idempotency_key=?",
            (native["key"],),
        ).fetchone()
        _check(row is not None, "native_receipt_missing")
        _check(row["digest"] == native["request_digest"], "request_mismatch")
        result = loads_strict(row["payload"])
    _check(type(result) is dict)
    return result


def _session_result(result):
    source = result.get("session", result)
    _check(type(source) is dict and _SESSION_FIELDS <= set(source))
    record = {field: source[field] for field in _SESSION_FIELDS}
    if "created_at" in source:
        record["created_at"] = source["created_at"]
    validate_graph_record("workflow_session", record)
    _check(_integer(source.get("data_revision")) and _integer(source.get("head_revision"), 1)
           and uuid_value(source.get("head_commit_id")) and source.get("status") in _SESSION_STATUSES
           and (source.get("selected_chain_run_id") is None
                or uuid_value(source["selected_chain_run_id"])))
    _check(type(source.get("chains")) is list)
    identities = set()
    for chain in source["chains"]:
        validate_graph_record("chain_run", chain)
        _check(chain["workflow_session_id"] == source["workflow_session_id"]
               and chain["chain_run_id"] not in identities)
        identities.add(chain["chain_run_id"])
    if source["active_chain_run_id"] is not None:
        _check(source["active_chain_run_id"] in identities)
    return source


def _resource_result(operation, parameters, result):
    required = {"reference", "update_sequence", "deleted"}
    _check(set(result) == required)
    reference = ResourceIdentity.from_dict(result["reference"]).to_dict()
    if operation == "resource.save":
        record = parameters["record"]
        expected = ResourceIdentity.from_dict({field: record[field] for field in (
            "envelope_version", "scope", "type_id", "resource_id",
        )}).to_dict()
        _check(_integer(parameters["expected_sequence"])
               and record["update_sequence"] == parameters["expected_sequence"] + 1)
        sequence = record["update_sequence"]
    elif operation == "resource.delete":
        expected = ResourceIdentity.from_dict(parameters["identity"]).to_dict()
        _check(_integer(parameters["expected_sequence"], 1))
        sequence = parameters["expected_sequence"] + 1
    else:
        raise _ReceiptEvidenceError("unsupported_operation")
    _check(_same(reference, expected) and _integer(result["update_sequence"], 1)
           and result["update_sequence"] == sequence
           and type(result["deleted"]) is bool and result["deleted"] == (operation == "resource.delete"))


def _object_receipt(saved):
    _check(type(saved) is dict and set(saved) == {
        "object_key", "revision", "revision_id", "deleted",
    } and type(saved["object_key"]) is str and bool(saved["object_key"])
        and _integer(saved["revision"], 1) and uuid_value(saved["revision_id"])
        and type(saved["deleted"]) is bool)


def _adopted_object(source, parameters, key):
    objects = source.get("objects")
    _check(type(objects) is dict and key in objects)
    value = objects[key]
    _check(type(value) is dict and type(value.get("value")) is dict
           and value.get("deleted") is False and _integer(value.get("revision"), 1)
           and uuid_value(value.get("revision_id")))
    binding = ObjectBinding.from_dict(value["binding"])
    _check(binding.object_key == key and parameters["node_id"] in binding.writers
           and _same(value["value"].get("view_ref"), {
               "scope": "artifact", "output_id": parameters["view_output_id"],
           }))
    return value


def _validate_result(operation, parameters, result):
    if operation.startswith("resource."):
        _resource_result(operation, parameters, result)
        return
    if operation == "definition.save":
        _check(_same(result, parameters["document"]) and uuid_value(result.get("workflow_definition_id"))
               and _integer(result.get("revision"), 1)
               and result["revision"] == parameters["expected_revision"] + 1)
        return
    source = _session_result(result)
    name = operation.removeprefix("consumer.")
    sid = parameters.get("session_id")
    if name in ("session.create", "session.copy", "candidate.fork"):
        _check(source["revision"] == 1 and source["active_chain_run_id"] is None)
        if sid is not None:
            _check(source["workflow_session_id"] != sid)
        if name == "session.create":
            _check(source["workflow_definition_id"] == parameters["workflow_definition_id"]
                   and source["definition_revision"] == parameters["definition_revision"]
                   and source["source"] == {"kind": "new"})
        elif name == "candidate.fork":
            _check(source["source"].get("kind") == "fork_candidate"
                   and source["source"].get("workflow_session_id") == sid
                   and source["source"].get("candidate_commit_id") == parameters["candidate_id"])
        else:
            document = parameters["document"]
            _check(source["workflow_definition_id"] == document["workflow_definition_id"]
                   and source["definition_revision"] == document["revision"])
            _check(source["source"].get("kind") == "copy_current"
                   and source["source"].get("workflow_session_id") == sid
                   and source["source"].get("definition_revision")
                   == parameters["expected_definition_revision"])
        return
    _check(source["workflow_session_id"] == sid
           and source["revision"] == parameters["expected_revision"] + 1)
    if name == "session.rebind":
        _check(source["definition_revision"] == parameters["definition_revision"]
               and source["head_revision"] == parameters["expected_head_revision"] + 1)
    if name == "candidate.select":
        _check(source["head_revision"] == parameters["expected_head_revision"] + 1)
    if name in ("run.start", "event.submit"):
        chain_id = source["active_chain_run_id"]
        _check(uuid_value(chain_id) and source["status"] == "prepared")
        chain = next(row for row in source["chains"] if row["chain_run_id"] == chain_id)
        _check(chain["status"] == "prepared" and chain["revision"] == 1
               and chain["workflow_definition_id"] == source["workflow_definition_id"]
               and chain["definition_revision"] == source["definition_revision"])
        if name == "event.submit":
            _check(source["workflow_definition_id"] == parameters["workflow_definition_id"]
                   and source["definition_revision"] == parameters["definition_revision"]
                   and chain["execution_kind"] == "event" and type(chain["event"]) is dict)
            event = chain["event"]
            _check(event.get("event_id") == parameters["event_id"]
                   and event.get("schema_version") == parameters["event_schema_version"]
                   and event.get("idempotency_key") == parameters["idempotency_key"]
                   and (not operation.startswith("consumer.") or event.get("audience") == "consumer"))
    if name == "run.control":
        _check((source["active_chain_run_id"] is None) == (parameters["action"] == "close"))
    if operation in ("object.write", "context.adopt"):
        _check(set(result) == {"results", "session"} and type(result["results"]) is list)
        if operation == "object.write":
            intents = [WriteIntent.from_dict(item) for item in parameters["writes"]]
            _check(len(result["results"]) == len(intents))
            for intent, saved in zip(intents, result["results"]):
                _object_receipt(saved)
                _check(saved["object_key"] == intent.object_key
                    and saved["revision"] == intent.expected_revision + 1
                    and saved["deleted"] == (intent.operation == "delete"))
        else:
            _check(len(result["results"]) <= 1)
            if result["results"]:
                saved = result["results"][0]
                _object_receipt(saved)
                current = _adopted_object(source, parameters, saved["object_key"])
                _check(saved["deleted"] is False and saved["revision"] == current["revision"]
                       and saved["revision_id"] == current["revision_id"])
            else:
                _check(type(source.get("objects")) is dict)
                candidates = [key for key, value in source["objects"].items()
                              if type(value) is dict and type(value.get("binding")) is dict
                              and type(value.get("value")) is dict
                              and parameters["node_id"] in value["binding"].get("writers", [])]
                _check(bool(candidates))
                _check(any(_same(source["objects"][key].get("value", {}).get("view_ref"),
                                {"scope": "artifact", "output_id": parameters["view_output_id"]})
                           for key in candidates))
                for key in candidates:
                    if _same(source["objects"][key].get("value", {}).get("view_ref"),
                             {"scope": "artifact", "output_id": parameters["view_output_id"]}):
                        _adopted_object(source, parameters, key)
                        break


def _receipt_graph_record(connection, kind, **identity):
    row = connection.execute(
        "SELECT payload FROM records WHERE record_type=? AND record_id=?",
        (kind, _record_id(kind, identity)),
    ).fetchone()
    _check(row is not None)
    return validate_graph_record(kind, loads_strict(row["payload"]))


def _consumer_fork_source(connection, parameters, source):
    """Prove the frozen child seed against its immutable completed source."""
    origin = source["source"]
    _check(type(origin) is dict and set(origin) == {
        "kind", "workflow_session_id", "object_source_session_id", "workflow_definition_id",
        "definition_revision", "head_commit_id", "candidate_commit_id", "state_mappings",
    })
    candidate = _receipt_graph_record(
        connection, "workflow_commit", commit_id=parameters["candidate_id"])
    _check(candidate["source"].get("kind") == "completed_execution")
    chain = _receipt_graph_record(
        connection, "chain_run", chain_run_id=candidate["source"]["chain_run_id"])
    snapshot = _receipt_graph_record(
        connection, "state_snapshot", state_snapshot_id=candidate["state_snapshot_id"])
    _check(chain["status"] == "succeeded" and chain.get("execution_kind", "round") == "round"
           and candidate["workflow_session_id"] == chain["workflow_session_id"]
           and snapshot["workflow_session_id"] == chain["workflow_session_id"]
           and (snapshot["workflow_definition_id"], snapshot["definition_revision"])
           == (chain["workflow_definition_id"], chain["definition_revision"])
           and chain["chain_run_id"] in snapshot["history_refs"])
    expected_origin = {
        "kind": "fork_candidate", "workflow_session_id": parameters["session_id"],
        "object_source_session_id": snapshot["workflow_session_id"],
        "workflow_definition_id": snapshot["workflow_definition_id"],
        "definition_revision": snapshot["definition_revision"],
        "head_commit_id": parameters["candidate_id"], "candidate_commit_id": parameters["candidate_id"],
        "state_mappings": [],
    }
    _check(_same(origin, expected_origin)
           and (source["workflow_definition_id"], source["definition_revision"])
           == (snapshot["workflow_definition_id"], snapshot["definition_revision"])
           and source["head_revision"] == 1 and source["status"] == "succeeded"
           and source["selected_chain_run_id"] == chain["chain_run_id"]
           and _same(source.get("history_refs"), snapshot["history_refs"]))
    child = _receipt_graph_record(
        connection, "workflow_session", workflow_session_id=source["workflow_session_id"])
    seed = _receipt_graph_record(
        connection, "workflow_commit", commit_id=source["head_commit_id"])
    child_snapshot = _receipt_graph_record(
        connection, "state_snapshot", state_snapshot_id=seed["state_snapshot_id"])
    _check(_same(child["source"], expected_origin)
           and seed["workflow_session_id"] == child["workflow_session_id"]
           and seed["parent_commit_id"] is None
           and _same(seed["source"], {"kind": "session_seed", "source": expected_origin})
           and child_snapshot["workflow_session_id"] == child["workflow_session_id"]
           and (child_snapshot["workflow_definition_id"], child_snapshot["definition_revision"])
           == (snapshot["workflow_definition_id"], snapshot["definition_revision"])
           and child_snapshot["data_revision"] == source["data_revision"]
           and _same(child_snapshot["node_states"], snapshot["node_states"])
           and _same(child_snapshot["history_refs"], snapshot["history_refs"]))
    return {
        "workflow_session_id": parameters["session_id"], "candidate_id": parameters["candidate_id"],
        "chain_run_id": chain["chain_run_id"],
        "workflow_definition_id": chain["workflow_definition_id"],
        "definition_revision": chain["definition_revision"],
    }


def _consumer_result(operation, parameters, result, connection=None):
    source = _session_result(result)
    verbs = {"consumer.session.create": "create", "consumer.run.start": "start",
             "consumer.run.control": "control", "consumer.event.submit": "event",
             "consumer.candidate.fork": "fork"}
    receipt = {
        "workflow_definition_id": source["workflow_definition_id"],
        "definition_revision": source["definition_revision"],
        "workflow_session_id": source["workflow_session_id"],
        "session_revision": source["revision"], "status": source["status"],
        "chain_run_id": source["active_chain_run_id"],
        "operation": verbs[operation], "idempotency_key": parameters["idempotency_key"],
    }
    if operation == "consumer.candidate.fork":
        receipt["fork_source"] = _consumer_fork_source(connection, parameters, source)
    return {"receipt": receipt}


def read_graph_application_receipt(database_path, operation, parameters, scope="management"):
    """Return only persisted acceptance; absence never proves nonacceptance."""
    spec = _COMMANDS.get(operation) if type(operation) is str else None
    if spec is None or "idempotency_key" not in spec.required:
        return _unresolved("unsupported_operation")
    if scope not in ("management", "consumer"):
        return _unresolved("invalid_request")
    if scope == "consumer" and not spec.consumer:
        return _unresolved("application_scope_denied")
    try:
        request = deepcopy(validate_parameters(spec, parameters))
        validate_json_value(request)
        operation_scope = "consumer" if spec.consumer else "management"
        identity = application_command_identity(operation, operation_scope, request)
    except (ContractValidationError, KeyError, TypeError, ValueError):
        return _unresolved("invalid_request")
    try:
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            _read_origin(connection, identity)
            result = _read_native(connection, identity)
            _validate_result(operation, request, result)
            if spec.consumer:
                result = _consumer_result(operation, request, result, connection)
            receipt = application_command_receipt(operation, operation_scope, request, result,
                                                  authority="service_receipt")
            return {"schema_version": 1, "kind": "workflow.application-receipt-read",
                    "outcome": "matched", "reason_code": "receipt_matched",
                    "receipt": receipt, "result": deepcopy(result)}
    except _ReceiptEvidenceError as exc:
        return _unresolved(exc.reason)
    except (sqlite3.Error, OSError):
        return _unresolved("storage_unavailable")
    except (ContractValidationError, KeyError, TypeError, ValueError, StopIteration):
        return _unresolved("receipt_invalid")
