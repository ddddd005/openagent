"""Shared native context ownership, artifact and consumption contracts."""

from copy import deepcopy

from .contract_json import validate_json_value
from .graph_contracts import require, uuid4_string
from .host_sdk import validate_reference

EFFECTIVE_CONTEXT_TYPE = "workflow.effective-context"
MAX_ACCEPTED_DELTAS = 4096


def _fields(value, fields, *, optional=()):
    require(type(value) is dict and set(fields) <= set(value) <= set(fields) | set(optional),
            "context_invalid_contract", "Context contract fields are invalid")


def artifact_ref(value):
    checked = validate_reference(value)
    require(checked["scope"] == "artifact", "context_exact_artifact_required",
            "Context provenance requires an exact immutable artifact")
    return checked


def _ids(values):
    require(type(values) is list and len(values) <= MAX_ACCEPTED_DELTAS,
            "context_delta_capacity_exceeded", "Consumed update identities exceed the declared budget")
    for value in values:
        uuid4_string(value)
    require(len(values) == len(set(values)), "context_duplicate_delta",
            "Context cannot repeat a consumed update identity")


def _unique_refs(refs):
    return [deepcopy(ref) for _, ref in sorted({ref["output_id"]: ref for ref in refs}.items())]


def validate_effective_context(value):
    validate_json_value(value)
    _fields(value, ("view_ref", "accepted_delta_ids"))
    if value["view_ref"] is not None:
        artifact_ref(value["view_ref"])
    _ids(value["accepted_delta_ids"])
    require(value["view_ref"] is not None or not value["accepted_delta_ids"],
            "context_consumption_mismatch", "An uninitialized context has no consumed updates")
    return deepcopy(value)


def effective_context_references(value):
    return [] if value["view_ref"] is None else [deepcopy(value["view_ref"])]


def preserve_artifact_references(value, mapping):
    """Forks map ownership at read time; immutable producers stay original."""
    return deepcopy(value)
