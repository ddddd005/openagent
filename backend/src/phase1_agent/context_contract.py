"""Shared native context ownership, artifact and consumption contracts."""

from copy import deepcopy

from .contract_json import validate_json_value
from .contract_graph import validate_context_message_history
from .contracts_v2 import validate_record
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


def validate_agent_checkpoint_value(value):
    """Validate the exact source-free logical state stored in an Agent root object."""
    validate_json_value(value)
    _fields(value, ("messages", "layout", "accepted_delta_ids", "generation"))
    require(type(value["messages"]) is list and len(value["messages"]) <= 4096
            and type(value["layout"]) is list and len(value["layout"]) == len(value["messages"]),
            "context_update_invalid", "Persistent messages require a bounded explicit layout")
    validate_context_message_history(value["messages"])
    for message, item in zip(value["messages"], value["layout"]):
        require("source" not in message and "schema_version" not in message,
                "context_update_invalid", "Persistent messages cannot retain execution sources")
        _fields(item, ("kind", "round_id", "compaction"))
        require(item["kind"] in ("history", "summary", "once")
                and item["compaction"] in ("allowed", "never"),
                "context_update_invalid_layout", "Persistent layout cannot contain request-only material")
        if item["kind"] == "summary":
            require(item["round_id"] is None and item["compaction"] == "allowed"
                    and message["role"] == "user",
                    "context_update_invalid_layout", "A summary is a derived user message")
        else:
            uuid4_string(item["round_id"])
        require(item["compaction"] == "never" or _is_compactable_text(message),
                "context_update_ineligible", "Only persistent plain text can declare compaction eligibility")
        require(message["role"] != "system" or (
            item["kind"] == "once" and item["compaction"] == "never"),
            "context_update_invalid_layout", "System material remains protected even with once lifecycle")
    _ids(value["accepted_delta_ids"])
    require(type(value["generation"]) is int and 0 <= value["generation"] <= 2**53 - 1,
            "context_update_invalid", "Context generation must be monotonic")
    return deepcopy(value)


def _is_compactable_text(message):
    from .context_compaction import is_compactable_text
    return is_compactable_text(message)


def project_agent_context_message(message):
    """Keep protocol content while dropping execution and prompt provenance."""
    if "source" in message:
        message = validate_record("agent_message", message)
    else:
        from .contracts_v2 import validate_context_history_message
        message = validate_context_history_message(message)
    return {
        **{field: deepcopy(message[field]) for field in ("message_id", "role", "blocks")},
        **{field: deepcopy(message[field]) for field in ("thinking_summary", "provider_metadata")
           if field in message},
    }


def project_agent_context_value(view):
    """Project an accepted context view to its durable source-free root value."""
    value = {
        "messages": [project_agent_context_message(message) for message in view["messages"]],
        "layout": deepcopy(view["layout"]),
        "accepted_delta_ids": deepcopy(view["accepted_delta_ids"] + view["applied_delta_ids"]),
        "generation": view["generation"],
    }
    return validate_agent_checkpoint_value(value)


def validate_effective_context(value):
    return validate_agent_checkpoint_value(value)


def effective_context_references(value):
    return []


def preserve_artifact_references(value, mapping):
    """Forks map ownership at read time; immutable producers stay original."""
    return deepcopy(value)
