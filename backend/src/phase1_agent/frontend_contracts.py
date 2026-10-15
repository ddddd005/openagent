"""Shared commit envelope retained by the logical-chat frontend node."""

from copy import deepcopy

from .contract_json import validate_json_value
from .graph_contracts import require, uuid4_string
from .host_sdk import bounded_name, validate_reference


FRONTEND_COMMIT_TYPE = "FRONTEND_COMMIT"


def preserve_frontend_references(value, mapping):
    return deepcopy(value)


def _fields(value, fields):
    require(type(value) is dict and set(value) == set(fields),
            "frontend_invalid_contract", "Frontend contract fields differ")


def _artifact(value):
    reference = validate_reference(value)
    require(reference["scope"] == "artifact", "frontend_exact_artifact_required",
            "Frontend commits require exact immutable artifacts")
    return reference


def validate_frontend_commit(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "view_ref", "operation_key", "receipt"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.frontend-commit", "frontend_invalid_contract",
            "Frontend commit envelope is invalid")
    _fields(value["owner"], ("workflow_session_id", "object_key"))
    uuid4_string(value["owner"]["workflow_session_id"])
    require(bounded_name(value["owner"]["object_key"]) and bounded_name(value["operation_key"]),
            "frontend_invalid_contract", "Frontend commit identities are invalid")
    _artifact(value["view_ref"])
    receipt = value["receipt"]
    _fields(receipt, ("object_key", "revision", "revision_id", "deleted"))
    uuid4_string(receipt["revision_id"])
    require(receipt["object_key"] == value["owner"]["object_key"]
            and type(receipt["revision"]) is int and 1 <= receipt["revision"] <= 2**53 - 1
            and receipt["deleted"] is False, "frontend_invalid_contract",
            "Frontend commit must contain its actual accepted object receipt")
    return deepcopy(value)
