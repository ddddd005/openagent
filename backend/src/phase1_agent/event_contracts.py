"""Typed in-process event payloads, separate from durable execution facts."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Literal, Mapping, TypedDict
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker, SchemaError, validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .contract_errors import ContractValidationError
from .contract_json import validate_json_value


UUID_SCHEMA = {
    "type": "string", "format": "uuid4",
    "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    "minLength": 36, "maxLength": 36,
}
_NULL_ID = {"anyOf": [UUID_SCHEMA, {"type": "null"}]}
_CODE = {
    "type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]*(?![\s\S])", "maxLength": 128,
}
_NULL_CODE = {"anyOf": [_CODE, {"type": "null"}]}
_POSITIVE = {"type": "integer", "minimum": 1}
_NONNEGATIVE = {"type": "integer", "minimum": 0}
_STATUS = {"enum": [
    "prepared", "running", "pausing", "paused", "failed", "final_ready",
    "succeeded", "superseded", "recovery_unavailable", "closed",
]}
_ACTIONS = {"enum": [
    "pause", "resume", "reroll", "retry_archive", "extend_budget",
    "close_execution", "continue_workflow",
]}
WORKFLOW_OPERATION_KINDS = frozenset({
    "initialize_session", "submit", "continue_pending_input", "retry_archive", "retry_publish",
    "interrupt", "resume", "extend_budget", "reroll", "close_execution", "continue_workflow",
    "select_candidate", "create_branch", "create_and_switch_branch", "switch_session",
})


def _object(**fields: Any) -> dict[str, Any]:
    return {
        "type": "object", "required": list(fields), "properties": fields,
        "additionalProperties": False,
    }


CORE_PAYLOAD_SCHEMAS = {
    "command_result": _object(
        command_id=UUID_SCHEMA,
        target=_object(
            workflow_session_id=UUID_SCHEMA, node_binding_id=UUID_SCHEMA, run_id=UUID_SCHEMA,
        ),
        status={"enum": ["accepted", "rejected"]}, reason_code=_NULL_CODE,
    ),
    "workflow_operation_result": _object(
        operation_id=UUID_SCHEMA, operation_kind={"enum": sorted(WORKFLOW_OPERATION_KINDS)},
        target=_object(
            workflow_session_id=UUID_SCHEMA, node_binding_id=UUID_SCHEMA, run_id=UUID_SCHEMA,
        ),
        status={"enum": ["accepted", "rejected"]}, reason_code=_NULL_CODE,
    ),
    "run_started": _object(
        chain_run_id=UUID_SCHEMA, input_id=UUID_SCHEMA, input_snapshot_id=_NULL_ID,
        parent_turn_id=_NULL_ID,
        source_chain_run_id=_NULL_ID, source_run_id=_NULL_ID, source_turn_id=_NULL_ID,
    ),
    "run_state": _object(
        previous_status={"anyOf": [_STATUS, {"type": "null"}]},
        status=_STATUS, phase=_NULL_CODE,
        available_actions={"type": "array", "items": _ACTIONS, "uniqueItems": True},
        action_rejections={
            "type": "object", "propertyNames": _ACTIONS, "additionalProperties": _CODE,
        },
        reason_code=_NULL_CODE,
    ),
    "request_attempt": _object(
        request_id=UUID_SCHEMA, request_index=_POSITIVE, attempt_id=UUID_SCHEMA,
        attempt_index=_POSITIVE,
        outcome={"enum": [
            "started", "responded", "model_error", "protocol_error", "adapter_contract_error",
            "execution_interrupted", "host_interrupted",
        ]},
        phase=_CODE,
    ),
    "tool_progress": _object(
        tool_call_id=UUID_SCHEMA, tool_execution_id=_NULL_ID, model_order=_POSITIVE,
        status={"enum": ["pending", "settled", "uncertain"]},
        outcome={"enum": [
            None, "started", "success", "error", "unknown", "outcome_unknown",
            "interrupted", "never_started",
        ]},
    ),
    "budget_changed": _object(
        budget_kind={"enum": ["model_requests", "model_attempts"]},
        additional=_NONNEGATIVE, limit=_POSITIVE,
        used={"anyOf": [_NONNEGATIVE, {"type": "null"}]}, reason_code=_CODE,
    ),
    "final_ready": _object(result_id=UUID_SCHEMA, turn_id=UUID_SCHEMA, snapshot_id=UUID_SCHEMA),
    "archive_result": _object(
        result_id=UUID_SCHEMA, status={"enum": ["succeeded", "failed"]},
        turn_id=_NULL_ID, reason_code=_NULL_CODE,
    ),
    "ports_released": _object(
        result_id=UUID_SCHEMA, turn_id=UUID_SCHEMA, output_id=_NULL_ID,
        status={"enum": ["succeeded", "failed"]},
        ports={
            "type": "array", "items": {"enum": ["final", "context_delta"]},
            "uniqueItems": True, "maxItems": 2,
        },
        reason_code=_NULL_CODE,
    ),
    "workflow_published": _object(
        chain_run_id=UUID_SCHEMA, output_id=UUID_SCHEMA, delivery_id=UUID_SCHEMA,
        visible_message_id=_NULL_ID,
        status={"enum": ["pending", "succeeded", "failed", "unknown"]},
        reason_code=_NULL_CODE,
    ),
    "preview_delta": _object(
        preview_id=UUID_SCHEMA, fragment_index=_POSITIVE, text={"type": "string"},
    ),
    "preview_cleared": _object(preview_id=UUID_SCHEMA, reason_code=_CODE),
    "diagnostic": _object(
        code=_CODE, category={"enum": ["model", "protocol", "contract", "interrupted", "unknown"]},
    ),
}
CORE_EVENT_TYPES = frozenset(CORE_PAYLOAD_SCHEMAS)
RESERVED_EVENT_TYPES = CORE_EVENT_TYPES | {
    "registered_behavior", "state_changed", "progress", "error", "control_result", "output_preview",
}
_FORMAT_CHECKER = FormatChecker()


class RunEventProjection(TypedDict):
    """Authorized view linked to its source, not a revalidated source payload."""

    schema_version: Literal[1]
    kind: Literal["run_event_projection"]
    source_schema_version: Literal[2]
    event_id: str
    workflow_session_id: str
    node_binding_id: str
    run_id: str
    sequence: int
    event_type: str
    source_payload_schema_ref: dict[str, Any]
    payload: dict[str, Any]
    visibility: Literal["private", "business_candidate"]
    generation: str
    projection_ref: str
    projection_revision: str


EVENT_STREAM_SCHEMAS = {
    **{name + "_payload": deepcopy(schema) for name, schema in CORE_PAYLOAD_SCHEMAS.items()},
    "run_event_projection": _object(
        schema_version={"type": "integer", "const": 1},
        kind={"const": "run_event_projection"},
        source_schema_version={"type": "integer", "const": 2},
        event_id=UUID_SCHEMA, workflow_session_id=UUID_SCHEMA, node_binding_id=UUID_SCHEMA,
        run_id=UUID_SCHEMA, sequence=_POSITIVE,
        event_type={
            "type": "string", "pattern": r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*(?![\s\S])",
        },
        source_payload_schema_ref=_object(
            schema_id={"type": "string", "minLength": 1}, version=_POSITIVE,
        ),
        payload={"type": "object"},
        visibility={"enum": ["private", "business_candidate"]}, generation=UUID_SCHEMA,
        projection_ref={"type": "string", "minLength": 1},
        projection_revision={"type": "string", "pattern": r"^[0-9a-f]{64}$", "minLength": 64, "maxLength": 64},
    ),
}


@_FORMAT_CHECKER.checks("uuid4")
def _uuid4(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


_StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _checker, value: type(value) is int,
    ),
)


def validate_schema_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_json_value(value)
    if (type(value) is not dict or set(value) != {"schema_id", "version"}
            or type(value["schema_id"]) is not str or not value["schema_id"].strip()
            or type(value["version"]) is not int or value["version"] < 1):
        raise ContractValidationError("Event schema reference requires an exact identity and version")
    return deepcopy(value)


def validate_event_type(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", value) is None:
        raise ContractValidationError("Invalid event type name")
    return value


def core_payload_schema_ref(event_type: str) -> dict[str, Any]:
    if type(event_type) is not str or event_type not in CORE_EVENT_TYPES:
        raise ContractValidationError("Unknown core event type")
    return {"schema_id": "run-event." + event_type, "version": 1}


def _schema_children(schema: dict[str, Any]) -> list[Any]:
    children: list[Any] = []
    for name in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
        value = schema.get(name)
        if type(value) is dict:
            children.extend(value.values())
    for name in ("allOf", "anyOf", "oneOf", "prefixItems"):
        value = schema.get(name)
        if type(value) is list:
            children.extend(value)
    for name in (
        "additionalProperties", "unevaluatedProperties", "items", "contains", "propertyNames",
        "not", "if", "then", "else", "unevaluatedItems", "contentSchema", "additionalItems",
    ):
        if name in schema:
            children.append(schema[name])
    dependencies = schema.get("dependencies")
    if type(dependencies) is dict:
        children.extend(item for item in dependencies.values() if type(item) in (dict, bool))
    return children


def validate_payload_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Validate local-only Draft 2020-12 without resolving remote resources."""
    validate_json_value(schema)
    if type(schema) is not dict:
        raise ContractValidationError("Event payload schema must be a JSON object")
    invalid = False
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, RecursionError):
        invalid = True
    if invalid:
        raise ContractValidationError("Invalid event payload schema") from None
    stack = [schema]
    while stack:
        current = stack.pop()
        if type(current) is not dict:
            continue
        if "$schema" in current and current["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise ContractValidationError("Unsupported event payload schema dialect")
        if "$id" in current and current["$id"] not in ("", "#"):
            raise ContractValidationError("Event payload schemas cannot change reference scope")
        for name in ("$ref", "$dynamicRef"):
            if name in current and not current[name].startswith("#"):
                raise ContractValidationError("Event payload schemas allow only local fragment references")
        stack.extend(_schema_children(current))
    # Resolve references before payloads can selectively skip a broken branch.
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    stack = [resource]
    anchors = set()
    references = []
    while stack:
        current_resource = stack.pop()
        current = current_resource.contents
        if type(current) is not dict:
            continue
        for name in ("$anchor", "$dynamicAnchor"):
            if name in current:
                if current[name] in anchors:
                    raise ContractValidationError("Event payload schema has duplicate local anchors")
                anchors.add(current[name])
        references.extend(current[name] for name in ("$ref", "$dynamicRef") if name in current)
        stack.extend(current_resource.subresources())
    resolver = Registry().with_resource("urn:phase1-agent:event-schema", resource).crawl().resolver(
        "urn:phase1-agent:event-schema",
    )
    invalid = False
    for reference in references:
        try:
            invalid = invalid or type(resolver.lookup(reference).contents) not in (dict, bool)
        except Exception:
            invalid = True
    if invalid:
        raise ContractValidationError("Event payload schema has an unresolved local reference") from None
    return deepcopy(schema)


def validate_payload(schema: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached typed payload; diagnostics never include payload text."""
    validate_json_value(payload)
    if type(payload) is not dict:
        raise ContractValidationError("Event payload must be a JSON object")
    invalid = False
    try:
        invalid = next(_StrictValidator(schema, format_checker=_FORMAT_CHECKER).iter_errors(payload), None) is not None
    except Exception:
        invalid = True
    if invalid:
        raise ContractValidationError("Event payload does not match its exact schema") from None
    return deepcopy(payload)


def validate_core_payload(
    event_type: str, payload_schema_ref: Mapping[str, Any], payload: Mapping[str, Any],
) -> dict[str, Any]:
    ref = validate_schema_ref(payload_schema_ref)
    if ref != core_payload_schema_ref(event_type):
        raise ContractValidationError("Core event payload schema version is unsupported")
    value = validate_payload(CORE_PAYLOAD_SCHEMAS[event_type], payload)
    return validate_core_payload_correspondence(event_type, value)


def validate_core_payload_correspondence(
    event_type: str, value: dict[str, Any],
) -> dict[str, Any]:
    """Apply fixed v1 stage relationships after exact schema validation."""
    if event_type not in CORE_EVENT_TYPES:
        raise ContractValidationError("Unknown core event type")
    if event_type == "tool_progress":
        outcome, execution = value["outcome"], value["tool_execution_id"]
        if outcome in (None, "never_started"):
            valid = execution is None and value["status"] == (
                "pending" if outcome is None else "settled"
            )
        else:
            valid = execution is not None and value["status"] == (
                "uncertain" if outcome in {"started", "unknown", "outcome_unknown", "interrupted"}
                else "settled"
            )
        if not valid:
            raise ContractValidationError("Tool event status differs from execution knowledge")
    elif event_type == "archive_result":
        if (value["status"] == "succeeded") != (value["turn_id"] is not None):
            raise ContractValidationError("Archive event cannot claim an unsaved successful Turn")
    elif event_type == "ports_released":
        expected = {"final", "context_delta"} if value["status"] == "succeeded" else set()
        if (set(value["ports"]) != expected
                or (value["status"] == "succeeded") != (value["output_id"] is not None)):
            raise ContractValidationError("Result ports event must preserve the actual release stage")
    elif event_type == "workflow_published":
        if value["status"] == "succeeded" and value["visible_message_id"] is None:
            raise ContractValidationError("Successful UI publication requires its visible message identity")
    return value


def core_envelope_schema_conditions() -> list[dict[str, Any]]:
    """Expose the same exact payload shapes to standalone schema consumers."""
    return [
        {
            "if": {"properties": {"event_type": {"const": name}}, "required": ["event_type"]},
            "then": {
                "properties": {
                    "payload_schema_ref": {"const": core_payload_schema_ref(name)},
                    "payload": deepcopy(schema),
                },
            },
        }
        for name, schema in sorted(CORE_PAYLOAD_SCHEMAS.items())
    ]


def validate_event_projection(value: Mapping[str, Any]) -> RunEventProjection:
    """Validate the view shape without claiming its clipped data matches source."""
    return validate_payload(EVENT_STREAM_SCHEMAS["run_event_projection"], value)
