"""Validate actual committed native-context object receipts."""

from copy import deepcopy

from .context_contract import _fields, _ids, artifact_ref
from .contract_json import validate_json_value
from .graph_contracts import require, uuid4_string


def validate_context_receipt(value):
    validate_json_value(value)
    require(type(value) is dict and set(value) == {
        "schema_version", "kind", "owner", "view_ref", "adopted_delta_ids", "operation_key", "receipt"},
        "context_invalid_commit", "Actual context receipt fields are invalid")
    require(type(value["schema_version"]) is int and value["schema_version"] == 2
            and value["kind"] == "workflow.context-commit",
            "context_invalid_commit", "Actual context receipt requires CONTEXT_COMMIT@2")
    artifact_ref(value["view_ref"])
    _fields(value["owner"], ("workflow_session_id", "object_key", "agent_node_id"))
    uuid4_string(value["owner"]["workflow_session_id"])
    uuid4_string(value["owner"]["agent_node_id"])
    require(type(value["owner"]["object_key"]) is str and 0 < len(value["owner"]["object_key"]) <= 128,
            "context_invalid_commit", "Commit requires its bound object key")
    _ids(value["adopted_delta_ids"])
    receipt = value["receipt"]
    require(type(receipt) is dict and set(receipt) == {"object_key", "revision", "revision_id", "deleted"}
            and receipt["object_key"] == value["owner"]["object_key"] and receipt["deleted"] is False
            and type(receipt["revision"]) is int and 2 <= receipt["revision"] <= 2**53 - 1
            and type(value["operation_key"]) is str and value["operation_key"].startswith("context-merge:"),
            "context_invalid_commit", "Commit requires an actual nondeleted object receipt")
    uuid4_string(receipt["revision_id"])
    return deepcopy(value)
