"""Durable application origins attached to the existing native transactions."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256

from .contract_json import canonical_bytes, content_digest
from .graph_records import require
from .host_sdk import ResourceIdentity


APPLICATION_IDENTITY_OPERATION = "graph.application.command-identity.v1"
APPLICATION_IDENTITY_KIND = "workflow.application-command-identity"
_CURRENT_APPLICATION_COMMAND = ContextVar("graph_application_command_identity", default=None)


def application_command_digest(operation, parameters):
    return sha256(canonical_bytes({"operation": operation, "parameters": parameters})).hexdigest()


def application_identity_key(operation, operation_scope, idempotency_key):
    return sha256(canonical_bytes({
        "operation": operation, "operation_scope": operation_scope,
        "idempotency_key": idempotency_key,
    })).hexdigest()


def native_command_request(operation, parameters):
    """Map only caller-owned fields and established service defaults."""
    request = {key: deepcopy(value) for key, value in parameters.items()
               if key != "idempotency_key"}
    aliases = {
        "consumer.session.create": "session.create",
        "consumer.run.start": "run.start",
        "consumer.run.control": "run.control",
    }
    name = aliases.get(operation, operation)
    native_operations = {
        "definition.save": "graph.definition.save",
        "session.create": "graph.session.create",
        "run.start": "graph.run.start",
        "event.submit": "graph.event.submit",
        "consumer.event.submit": "graph.consumer.event.submit",
        "run.control": "graph.run.control",
        "session.copy": "graph.session.copy",
        "session.rebind": "graph.session.rebind",
        "session.data.update": "graph.session.data",
        "candidate.select": "graph.candidate.select",
        "candidate.fork": "graph.candidate.fork",
        "object.write": "graph.objects.update",
        "context.adopt": "graph.context.adopt",
    }
    if name in native_operations:
        if "session_id" in request:
            request["source" if name == "session.copy" else "session"] = request.pop("session_id")
        if name in ("session.copy", "session.rebind"):
            request["mappings"] = request.get("mappings") if request.get("mappings") is not None else []
        if name == "session.data.update":
            request.setdefault("variables", None)
            request.setdefault("shared", None)
        if name == "run.start":
            request["inputs"] = request.get("inputs") if request.get("inputs") is not None else {}
        return {
            "table": "idempotency", "operation": native_operations[name],
            "key": parameters["idempotency_key"],
            "request_digest": "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1],
        }
    resource_operations = {
        "resource.save": "write", "resource.delete": "delete",
    }
    if name not in resource_operations:
        return None
    if name == "resource.save":
        request = {"operation": "write", "record": request["record"],
                   "expected_sequence": request["expected_sequence"]}
    elif name == "resource.delete":
        request = {"operation": "delete",
                   "reference": ResourceIdentity.from_dict(request["identity"]).to_dict(),
                   "expected_sequence": request["expected_sequence"]}
    return {
        "table": "global_resource_receipts", "operation": resource_operations[name],
        "key": parameters["idempotency_key"], "request_digest": content_digest(request),
    }


def application_command_identity(operation, operation_scope, parameters):
    native = native_command_request(operation, parameters)
    if native is None:
        return None
    return {
        "schema_version": 1, "kind": APPLICATION_IDENTITY_KIND,
        "operation": operation, "operation_scope": operation_scope,
        "idempotency_key": parameters["idempotency_key"],
        "request_sha256": application_command_digest(operation, parameters),
        "target": {"session_id": parameters["session_id"]} if "session_id" in parameters else {},
        "native_receipt": native,
    }


@contextmanager
def bind_application_command(operation, operation_scope, parameters):
    identity = application_command_identity(operation, operation_scope, parameters)
    token = _CURRENT_APPLICATION_COMMAND.set(identity)
    try:
        yield
    finally:
        _CURRENT_APPLICATION_COMMAND.reset(token)


def persist_application_command_identity(connection, *, table, operation, key, request_digest):
    """Attach an origin only at the first matching native receipt write."""
    identity = _CURRENT_APPLICATION_COMMAND.get()
    if identity is None:
        return
    native = identity["native_receipt"]
    if (native["table"], native["operation"], native["key"]) != (table, operation, key):
        return
    require(connection.in_transaction, "storage_contract_violation",
            "Application identity requires its native transaction", 500)
    require(native["request_digest"] == request_digest, "storage_contract_violation",
            "Application normalization differs from the native request", 500)
    digest = "workflow-op-v1:" + identity["request_sha256"]
    connection.execute(
        "INSERT INTO idempotency "
        "(operation,key,digest,result_refs,result_payload,request_digest) VALUES(?,?,?,?,?,?)",
        (APPLICATION_IDENTITY_OPERATION,
         application_identity_key(identity["operation"], identity["operation_scope"],
                                  identity["idempotency_key"]),
         digest, "[]", canonical_bytes([{"application_identity": identity}]).decode("utf-8"), digest),
    )


def application_accepted_result(result):
    if type(result) is not dict:
        return {}
    if type(result.get("receipt")) is dict:
        return deepcopy(result["receipt"])
    source = result.get("session", result)
    fields = ("workflow_definition_id", "definition_revision", "workflow_session_id",
              "revision", "data_revision", "head_revision", "head_commit_id", "status",
              "active_chain_run_id", "selected_chain_run_id", "reference", "update_sequence",
              "deleted", "package_lock")
    evidence = {field: deepcopy(source[field]) for field in fields if field in source}
    if "workflow_definition_id" in source and "workflow_session_id" not in source and "revision" in source:
        evidence["definition_revision"] = source["revision"]
    return evidence


def application_command_receipt(operation, operation_scope, parameters, result, *, authority):
    return {
        "schema_version": 1, "kind": "workflow.application-receipt",
        "operation": operation, "operation_scope": operation_scope,
        "idempotency_key": parameters.get("idempotency_key"),
        "request_sha256": application_command_digest(operation, parameters),
        "authority": authority,
        "target": {"session_id": parameters["session_id"]} if "session_id" in parameters else {},
        "accepted": application_accepted_result(result),
    }
