"""Versioned public declarations; no arbitrary field selectors or private records."""

from __future__ import annotations

import copy
import sqlite3

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .model_configuration import configuration_error, identifier, require, revision


AGENT_BINDINGS = {
    "A": "7be319b8-30bd-4674-b7bf-d1cf54a1a108",
    "B": "7be319b8-30bd-4674-b7bf-d1cf54a1a109",
}


def validate_registration(value):
    require(type(value) is dict and set(value) == {
        "schemaVersion", "id", "workflowId", "stage", "nodeBindingId",
        "publicName", "kind", "type", "fields",
    })
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1)
    identifier(value["id"])
    from .resource_contracts import workflow_identity
    workflow_identity(value["workflowId"])
    require(value["stage"] in AGENT_BINDINGS)
    require(value["nodeBindingId"] == AGENT_BINDINGS[value["stage"]])
    require(type(value["publicName"]) is str and 0 < len(value["publicName"]) <= 128
            and value["publicName"] == value["publicName"].strip())
    if value["kind"] == "state" and value["type"] == "public_agent_state":
        allowed = {"status", "run_id", "revision", "budget"}
    elif value["kind"] == "result" and value["type"] in ("delivered_workflow_result", "public_node_result"):
        allowed = {"delivered_text"}
        if value["stage"] == "A":
            allowed.add("result_text")
    else:
        raise configuration_error("invalid_request")
    require(type(value["fields"]) is list and 0 < len(value["fields"]) <= len(allowed))
    require(all(type(field) is str and field in allowed for field in value["fields"]))
    require(len(set(value["fields"])) == len(value["fields"]))
    return copy.deepcopy(value)


def validate_exposure_configuration(value):
    try:
        validate_json_value(value)
        require(type(value) is dict and set(value) == {
            "schema_version", "kind", "config_id", "workflow_id", "revision", "registrations",
        })
        require(type(value["schema_version"]) is int and value["schema_version"] == 1
                and value["kind"] == "workflow_exposure_configuration")
        identifier(value["config_id"])
        revision(value["revision"])
        from .resource_contracts import workflow_identity
        workflow_identity(value["workflow_id"])
        require(type(value["registrations"]) is list and len(value["registrations"]) <= 128)
        registrations = [validate_registration(row) for row in value["registrations"]]
        require(len({row["id"] for row in registrations}) == len(registrations)
                and len({row["publicName"] for row in registrations}) == len(registrations))
        require(len(canonical_bytes(value)) <= 60 * 1024)
        return copy.deepcopy(value)
    except (KeyError, TypeError, ContractValidationError):
        raise configuration_error("invalid_request") from None


class ExposureConfigurationStore:
    def __init__(self, store):
        self._store, self._connection = store, store._connection

    def _decode(self, payload, identity, version):
        try:
            value = validate_exposure_configuration(loads_strict(payload))
            require(value["config_id"] == identity and value["revision"] == version)
            return value
        except ContractValidationError:
            raise configuration_error("storage_contract_violation", 500) from None

    def get(self, identity, version=None):
        identifier(identity)
        if version is not None:
            revision(version)
        try:
            row = self._connection.execute(
                "SELECT revision, payload FROM exposure_configuration_revisions WHERE config_id = ?"
                + (" AND revision = ?" if version is not None else " ORDER BY revision DESC LIMIT 1"),
                (identity, version) if version is not None else (identity,),
            ).fetchone()
            return self._decode(row["payload"], identity, row["revision"]) if row else None
        except sqlite3.Error:
            raise configuration_error("storage_error", 500) from None

    def write(self, record, *, expected_revision, idempotency_key):
        record = validate_exposure_configuration(record)
        revision(expected_revision, zero=True)
        require(type(idempotency_key) is str and 0 < len(idempotency_key.strip()) <= 128)
        validate_json_value(idempotency_key)
        require(record["revision"] == expected_revision + 1 and not self._connection.in_transaction)
        digest = content_digest({"record": record, "expected_revision": expected_revision})
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            receipt = self._connection.execute(
                "SELECT digest, payload FROM exposure_configuration_mutations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if receipt:
                if receipt["digest"] != digest:
                    raise configuration_error("idempotency_conflict", 409)
                saved = self._decode(receipt["payload"], record["config_id"], record["revision"])
                if saved != record or self.get(record["config_id"], record["revision"]) != saved:
                    raise configuration_error("storage_contract_violation", 500)
                self._connection.execute("COMMIT")
                return saved
            current = self.get(record["config_id"])
            if (current["revision"] if current else 0) != expected_revision:
                raise configuration_error("stale_revision", 409)
            payload = canonical_bytes(record).decode("utf-8")
            self._connection.execute(
                "INSERT INTO exposure_configuration_revisions VALUES (?, ?, ?)",
                (record["config_id"], record["revision"], payload),
            )
            self._store._inject("exposure_configuration_before_commit")
            self._connection.execute(
                "INSERT INTO exposure_configuration_mutations VALUES (?, ?, ?)",
                (idempotency_key, digest, payload),
            )
            self._connection.execute("COMMIT")
            return record
        except Exception as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise configuration_error("storage_error", 500) from None
            raise


def read_registered_exposures(record, current, view):
    observation = view["observation"]
    available = current is not None and current["revision"] == record["revision"]
    result = {
        "schema_version": 1, "kind": "workflow_exposure_read",
        "config_id": record["config_id"], "revision": record["revision"],
        "workflow_session_id": view["workflow_session_id"], "session_revision": view["revision"],
        "head_commit_id": view["head_commit_id"],
        "availability": "available" if available else "unavailable",
        "reason_code": None if available else "declaration_stale",
        "registrations": copy.deepcopy(record["registrations"]) if available else [],
        "observations": [],
    }
    for registration in result["registrations"]:
        node = next(row for row in observation["nodes"]
                    if row["node_binding_id"] == registration["nodeBindingId"])
        item = {
            "registrationId": registration["id"], "workflowId": registration["workflowId"],
            "workflowSessionId": view["workflow_session_id"],
            "nodeBindingId": node["node_binding_id"], "runId": node["run_id"],
            "schemaVersion": 1, "availability": "unavailable", "reason": "no_run", "value": {},
        }
        if registration["kind"] == "state" and node["run_id"] is not None:
            item["value"] = {field: copy.deepcopy(node[field]) for field in registration["fields"] if field in node}
            item["availability"] = "available"
            del item["reason"]
        elif registration["kind"] == "result":
            if node["result"]["availability"] == "available":
                item["value"] = {field: node["result"]["text"] for field in registration["fields"]}
                item["availability"] = "available"
                del item["reason"]
            else:
                item["reason"] = node["result"]["reason_code"]
        result["observations"].append(item)
    return result
