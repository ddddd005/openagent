"""Versioned public records for the second-stage workflow boundary.

These schemas validate structure only. Ownership, existence, parent chains,
message pairing, and state transitions belong to the relational validator.
Frozen output and tool-parameter JSON Schemas use a narrow v1 profile: only
fragment-only $ref/$dynamicRef values are accepted. Local target resolution
is a separate validation step.
Exported ID and timestamp patterns cover syntax for ordinary JSON Schema
clients; uuid4 and utc_millis custom formats enforce canonical identity and
real UTC calendar values when validated through this module.
"""

from __future__ import annotations

import copy
from datetime import datetime
import re
from typing import Any, Literal, Mapping, TypedDict
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker, SchemaError, validators

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .event_contracts import (
    CORE_EVENT_TYPES, core_envelope_schema_conditions, validate_core_payload,
)
from .workflow_control import validate_workflow_operation


Status = Literal[
    "prepared", "running", "pausing", "paused", "failed", "final_ready",
    "succeeded", "superseded", "recovery_unavailable", "closed",
]
NodeStatus = Literal["prepared", "running", "paused", "failed", "succeeded", "superseded", "recovery_unavailable", "closed"]
ChainStatus = Literal["prepared", "running", "paused", "failed", "succeeded", "superseded", "recovery_unavailable", "closed"]
Role = Literal["system", "user", "assistant", "tool"]
VisibleRole = Literal["user", "assistant"]


class SchemaRef(TypedDict):
    schema_id: str
    version: int


class InputPort(TypedDict):
    port_id: str
    direction: Literal["input"]
    schema_ref: SchemaRef
    required: bool


class OutputPort(TypedDict):
    port_id: str
    direction: Literal["output"]
    schema_ref: SchemaRef


class ToolDefinition(TypedDict):
    name: str
    version: str
    parameters_schema: dict[str, Any]


class PrivatePayload(TypedDict):
    owner_component_id: str
    schema_version: int
    payload: Any


class NodeDefinition(TypedDict):
    schema_version: int
    component_id: str
    component_version: str
    kind: Literal["agent", "io", "transform"]
    capabilities: list[str]
    ports: list[InputPort | OutputPort]


class NodeBinding(TypedDict):
    schema_version: int
    node_binding_id: str
    workflow_definition_id: str
    workflow_definition_revision: int
    component_id: str
    component_version: str
    config: PrivatePayload


class WorkflowDefinitionRevision(TypedDict):
    schema_version: int
    workflow_definition_id: str
    revision: int
    bindings: list[str]
    edges: list[dict[str, Any]]


class WorkflowSession(TypedDict):
    schema_version: int
    workflow_session_id: str
    workflow_definition_id: str
    definition_revision: int
    revision: int
    source: dict[str, Any]


class NodeSession(TypedDict):
    schema_version: int
    workflow_session_id: str
    node_binding_id: str
    data_version: int
    private_data: PrivatePayload


class NodeInput(TypedDict):
    schema_version: int
    input_id: str
    port_id: str
    payload_schema_ref: SchemaRef
    source: dict[str, Any]
    payload: Any


class InputSnapshot(TypedDict):
    schema_version: int
    snapshot_id: str
    workflow_session_id: str
    node_binding_id: str
    input_id: str
    parent_turn_id: str | None
    component_id: str
    component_version: str
    config: PrivatePayload
    s0: list[dict[str, Any]]
    tool_definitions: list[ToolDefinition]
    model_parameters: dict[str, Any]
    output_schema: dict[str, Any]
    projection_version: int


class _OptionalInitialBudget(TypedDict, total=False):
    max_model_attempts: int


class InitialBudget(_OptionalInitialBudget):
    max_model_requests: int
    max_automatic_retries: Literal[3]


class RunRecord(TypedDict):
    schema_version: int
    profile: Literal["agent"]
    run_id: str
    workflow_session_id: str
    node_binding_id: str
    chain_run_id: str
    input_id: str
    snapshot_id: str
    source_run_id: str | None
    status: Status
    revision: int
    initial_budget: InitialBudget
    result_turn_id: str | None
    superseded_by_run_id: str | None


class NodeRun(TypedDict):
    """Generic node lifecycle; Agent-only fields belong to RunRecord."""

    schema_version: int
    profile: Literal["node"]
    run_id: str
    workflow_session_id: str
    node_binding_id: str
    chain_run_id: str
    input_id: str
    input_snapshot_id: str | None
    source_run_id: str | None
    status: NodeStatus
    revision: int
    result_output_id: str | None
    superseded_by_run_id: str | None


class Turn(TypedDict):
    schema_version: int
    turn_id: str
    parent_turn_id: str | None
    run_id: str
    snapshot_id: str
    input_id: str
    messages: list[dict[str, Any]]
    final: dict[str, Any]
    projection_version: int


class CandidateGroup(TypedDict):
    schema_version: int
    candidate_group_id: str
    workflow_session_id: str
    node_binding_id: str
    logical_input_id: str
    parent_turn_id: str | None
    frozen_snapshot_id: str
    candidate_refs: list[dict[str, str]]


class CandidateSelection(TypedDict):
    schema_version: int
    candidate_group_id: str
    selected_turn_id: str | None
    revision: int


class _OptionalModelChannels(TypedDict, total=False):
    thinking_summary: str | None
    provider_metadata: dict[str, Any] | None


class AgentMessage(_OptionalModelChannels):
    schema_version: int
    message_id: str
    role: Role
    source: dict[str, Any]
    blocks: list[dict[str, Any]]


class WorkflowCheckpoint(TypedDict):
    schema_version: int
    checkpoint_id: str
    workflow_session_id: str
    workflow_definition_id: str
    definition_revision: int
    revision: int
    kind: Literal["initial", "before_input", "completed_output"]
    nodes: list[dict[str, Any]]
    selection_refs: list[dict[str, Any]]


class StateSnapshot(TypedDict):
    schema_version: int
    state_snapshot_id: str
    workflow_session_id: str
    workflow_definition_id: str
    definition_revision: int
    node_states: list[dict[str, Any]]
    selection_refs: list[dict[str, Any]]
    visible_message_refs: list[dict[str, Any]]
    pending_input_id: str | None


class WorkflowCommit(TypedDict):
    schema_version: int
    commit_id: str
    workflow_session_id: str
    state_snapshot_id: str
    parent_commit_id: str | None
    operation_id: str
    source: dict[str, Any]


class WorkflowRef(TypedDict):
    schema_version: int
    workflow_ref_id: str
    workflow_session_id: str
    head_commit_id: str | None
    revision: int


class SessionSelection(TypedDict):
    schema_version: int
    session_selection_id: str
    active_workflow_session_id: str
    revision: int


class WorkflowCandidate(TypedDict):
    schema_version: int
    candidate_id: str
    workflow_session_id: str
    chain_run_id: str
    base_commit_id: str
    result_commit_id: str
    output_id: str
    checkpoint_id: str


class WorkflowOperation(TypedDict):
    schema_version: int
    operation_id: str
    kind: str
    scope: dict[str, str]
    target: dict[str, str]
    idempotency_key: str
    expected_revisions: list[dict[str, Any]]
    payload: dict[str, Any]


class ChainRun(TypedDict):
    schema_version: int
    chain_run_id: str
    workflow_session_id: str
    input_id: str
    status: ChainStatus
    node_run_ids: list[str]
    output_id: str | None


class ChainInputOrigin(TypedDict):
    schema_version: int
    chain_run_id: str
    workflow_session_id: str
    visible_message_id: str
    input_id: str
    source_chain_run_id: str


class ExecutionCloseout(TypedDict):
    schema_version: int
    closeout_id: str
    workflow_session_id: str
    chain_run_id: str
    reason: Literal["lost_runtime", "execution_failed", "paused_reroll"]
    run_ids: list[str]
    fact_refs: list[dict[str, Any]]
    diagnostic: dict[str, str]


class ExecutionContinuation(TypedDict):
    schema_version: int
    chain_run_id: str
    workflow_session_id: str
    source_chain_run_id: str
    closeout_id: str
    source_run_id: str
    reused_run_ids: list[str]
    evidence_message: dict[str, Any]


class WorkflowOutput(TypedDict):
    schema_version: int
    output_id: str
    workflow_session_id: str
    chain_run_id: str
    source: dict[str, Any]
    port_id: str
    payload_schema_ref: SchemaRef
    payload: Any


class OutputDelivery(TypedDict):
    schema_version: int
    delivery_id: str
    output_id: str
    target: dict[str, Any]
    status: Literal["pending", "succeeded", "failed", "unknown"]
    idempotency_key: str


class VisibleMessage(TypedDict):
    schema_version: int
    visible_message_id: str
    origin_workflow_session_id: str
    role: VisibleRole
    payload_schema_ref: SchemaRef
    payload: Any
    source: dict[str, Any]


class VisibleMessageRef(TypedDict):
    schema_version: int
    workflow_session_id: str
    visible_message_id: str
    sequence: int
    role: VisibleRole
    boundary: dict[str, Any]


class ForkAnchor(TypedDict):
    schema_version: int
    fork_anchor_id: str
    source_workflow_session_id: str
    visible_message_id: str
    role: VisibleRole
    checkpoint_id: str


class ForkCandidateFloor(TypedDict):
    user_visible_message_id: str
    candidate_ids: list[str]
    selected_candidate_id: str


class BudgetExtensionPayload(TypedDict):
    additional_model_requests: int
    max_total_model_requests: int


class _OptionalControlPayload(TypedDict, total=False):
    payload: BudgetExtensionPayload


class ControlCommand(_OptionalControlPayload):
    schema_version: int
    command_id: str
    target: dict[str, Any]
    action: Literal[
        "pause", "resume", "reroll", "retry_archive", "extend_budget",
        "create_branch", "create_and_switch_branch",
    ]
    expected_revision: int


class LegacyRunEvent(TypedDict):
    """Historical v1 structure; no inferred schema reference or generation."""

    schema_version: int
    event_id: str
    workflow_session_id: str
    node_binding_id: str
    run_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]


class RunEvent(LegacyRunEvent):
    """Live v2 envelope; exact payload validation also requires its declaration."""

    payload_schema_ref: SchemaRef
    visibility: Literal["private", "business_candidate"]
    generation: str


_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_UTC_MILLIS_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
_UUID = {
    "type": "string", "format": "uuid4", "pattern": _UUID_PATTERN,
    "minLength": 36, "maxLength": 36,
}
_UUID_OR_NULL = {"anyOf": [_UUID, {"type": "null"}]}
_UTC_MILLIS = {
    "type": "string", "pattern": _UTC_MILLIS_PATTERN,
    "minLength": 24, "maxLength": 24,
    "allOf": [{"format": "date-time"}, {"format": "utc_millis"}],
}
_POS = {"type": "integer", "minimum": 1}
_SYMBOL = {"type": "string", "minLength": 1}
_JSON = {}
_OBJECT_VALUE = {"type": "object"}
_STATUS = {"enum": list(Status.__args__)}


def _object(fields: tuple[str, ...], **properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "required": list(fields),
        "properties": properties,
        "additionalProperties": False,
    }


def _array(items: dict[str, Any], *, unique: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "array", "items": items}
    if unique:
        result["uniqueItems"] = True
    return result


def _variant(kind: str, **properties: Any) -> dict[str, Any]:
    return _object(("kind", *properties), kind={"const": kind}, **properties)


def _union(*variants: dict[str, Any]) -> dict[str, Any]:
    return {"oneOf": list(variants)}


def _record(required: tuple[str, ...], **properties: Any) -> dict[str, Any]:
    return _object(
        ("schema_version", *required),
        schema_version={"type": "integer", "const": 1},
        created_at=_UTC_MILLIS,
        **properties,
    )


def _record_v2(required: tuple[str, ...], **properties: Any) -> dict[str, Any]:
    return _object(
        ("schema_version", *required),
        schema_version={"type": "integer", "const": 2},
        created_at=_UTC_MILLIS,
        **properties,
    )


def _record_v3(required: tuple[str, ...], **properties: Any) -> dict[str, Any]:
    return _object(
        ("schema_version", *required),
        schema_version={"type": "integer", "const": 3},
        created_at=_UTC_MILLIS,
        **properties,
    )


_SCHEMA_REF = _object(
    ("schema_id", "version"), schema_id=_SYMBOL, version=_POS,
)
_PRIVATE = _object(
    ("owner_component_id", "schema_version", "payload"),
    owner_component_id=_UUID,
    schema_version=_POS,
    payload=_JSON,
)
_INPUT_PORT = _object(
    ("port_id", "direction", "schema_ref", "required"),
    port_id=_SYMBOL, direction={"const": "input"}, schema_ref=_SCHEMA_REF,
    required={"type": "boolean"},
)
_OUTPUT_PORT = _object(
    ("port_id", "direction", "schema_ref"),
    port_id=_SYMBOL, direction={"const": "output"}, schema_ref=_SCHEMA_REF,
)
_EDGE = _object(
    ("from_node_binding_id", "from_port_id", "to_node_binding_id", "to_port_id"),
    from_node_binding_id=_UUID, from_port_id=_SYMBOL,
    to_node_binding_id=_UUID, to_port_id=_SYMBOL,
)
_INPUT_SOURCE = _union(
    _variant("visible_message", visible_message_id=_UUID),
    _variant("upstream_output", output_id=_UUID),
    _variant("external", source_ref=_SYMBOL),
)
_MESSAGE_SOURCE = {
    "system": _variant("prompt", prompt_id=_UUID, revision=_POS),
    "user": _union(
        _variant("human", visible_message_id=_UUID),
        _variant("upstream_node", output_id=_UUID),
        _variant("prompt", prompt_id=_UUID, revision=_POS),
        _variant("protocol_feedback", request_id=_UUID),
    ),
    "assistant": _variant("model", request_id=_UUID),
    "tool": _variant("tool", tool_execution_id=_UUID),
}
_TEXT = _variant("text", text={"type": "string"})
_TOOL_CALL = _variant(
    "tool_call", tool_call_id=_UUID, tool_name=_SYMBOL,
    tool_definition_version=_SYMBOL, raw_arguments={"type": "string"},
    parsed_arguments=_OBJECT_VALUE,
)
_RESULT_COMMON = {
    "tool_call_id": _UUID, "tool_execution_id": _UUID,
    "model_visible_text": {"type": "string"},
}
_TOOL_RESULT = _union(
    *(
        _object(
            ("kind", "status", "is_error", "content", *_RESULT_COMMON),
            kind={"const": "tool_result"}, status={"const": status},
            is_error={"const": status == "error"}, content=_JSON,
            **_RESULT_COMMON,
        )
        for status in ("success", "error")
    ),
)
_OBSERVATION_REASONS = ("never_started", "interrupted", "outcome_unknown")
_OBSERVATION_TEXT = {"type": "string", "minLength": 1}
_RUNTIME_TOOL_SOURCE = _union(
    *(
        _variant(
            "runtime_tool_observation",
            tool_call_id=_UUID,
            tool_execution_id={"type": "null"} if reason == "never_started" else _UUID,
            reason_code={"const": reason},
        )
        for reason in _OBSERVATION_REASONS
    )
)
_RUNTIME_TOOL_RESULT = _union(
    *(
        _object(
            ("kind", "status", "is_error", "content", *_RESULT_COMMON),
            kind={"const": "tool_result"},
            status={"const": "error"},
            is_error={"const": True},
            content={
                "type": "object",
                "required": ["reason_code", "error", "retry_guidance"],
                "properties": {
                    "reason_code": {"const": reason},
                    "error": _OBSERVATION_TEXT,
                    "retry_guidance": _OBSERVATION_TEXT,
                },
                "additionalProperties": True,
            },
            **{
                **_RESULT_COMMON,
                "tool_execution_id": (
                    {"type": "null"} if reason == "never_started" else _UUID
                ),
            },
        )
        for reason in _OBSERVATION_REASONS
    )
)
_RUNTIME_EXECUTION_MESSAGE = _record_v2(
    ("message_id", "role", "source", "blocks"),
    message_id=_UUID, role={"const": "system"},
    source=_variant(
        "runtime_execution_observation", closeout_id=_UUID, source_run_id=_UUID,
    ),
    blocks={**_array(_variant("text", text=_OBSERVATION_TEXT)), "minItems": 1},
)
_PROMPT_MESSAGE = _record_v3(
    ("message_id", "role", "source", "blocks"),
    message_id=_UUID,
    role={"enum": ["system", "user", "assistant"]},
    source=_variant(
        "prompt", prompt_id=_UUID, revision=_POS,
        item_instance_id=_UUID, group_instance_id=_UUID_OR_NULL,
    ),
    blocks={**_array(_TEXT), "minItems": 1},
)
_CONTEXT_MAINTENANCE_MESSAGE = _object(
    ("schema_version", "message_id", "role", "source", "blocks"),
    schema_version={"type": "integer", "const": 4},
    message_id=_UUID, role={"const": "user"},
    source=_union(
        _variant("context_checkpoint", compaction_id=_UUID),
        _variant("context_compaction_instruction", compaction_id=_UUID),
    ),
    blocks={**_array(_TEXT), "minItems": 1, "maxItems": 1},
)
_PROVIDER_MODEL_MESSAGE = _object(
    ("schema_version", "message_id", "role", "source", "blocks"),
    schema_version={"type": "integer", "const": 5},
    created_at=_UTC_MILLIS, message_id=_UUID, role={"const": "assistant"},
    source=_MESSAGE_SOURCE["assistant"],
    blocks={**_array(_union(_TEXT, _TOOL_CALL)), "minItems": 1},
    thinking_summary={"type": ["string", "null"], "maxLength": 131072},
    provider_metadata={"type": ["object", "null"]},
)
_AGENT_MESSAGE = _union(
    *(
        _record(
            ("message_id", "role", "source", "blocks"),
            message_id=_UUID, role={"const": role}, source=_MESSAGE_SOURCE[role],
            blocks={**_array(_union(_TEXT, _TOOL_CALL) if role == "assistant"
                             else _TOOL_RESULT if role == "tool" else _TEXT),
                    "minItems": 1},
        )
        for role in ("system", "user", "assistant", "tool")
    ),
    _record_v2(
        ("message_id", "role", "source", "blocks"),
        message_id=_UUID,
        role={"const": "tool"},
        source=_RUNTIME_TOOL_SOURCE,
        blocks={**_array(_RUNTIME_TOOL_RESULT), "minItems": 1, "maxItems": 1},
    ),
    _RUNTIME_EXECUTION_MESSAGE,
    _PROMPT_MESSAGE,
    _CONTEXT_MAINTENANCE_MESSAGE,
    _PROVIDER_MODEL_MESSAGE,
)
_GENERATED_MESSAGE = {
    "allOf": [
        _AGENT_MESSAGE,
        {"properties": {"source": {"properties": {"kind": {
            "enum": ["model", "tool", "runtime_tool_observation", "protocol_feedback"],
        }}}}},
    ],
}
_NODE_STATE = _object(
    ("node_binding_id", "data_version", "private_data", "selected_turn_id"),
    node_binding_id=_UUID, data_version=_POS, private_data=_PRIVATE,
    selected_turn_id=_UUID_OR_NULL,
)
_SELECTION_REF = _object(
    ("candidate_group_id", "selected_turn_id"),
    candidate_group_id=_UUID, selected_turn_id=_UUID_OR_NULL,
)
_PENDING_INPUT = _object(
    ("input_id", "source_visible_message_id", "port_id", "payload_schema_ref", "payload"),
    input_id=_UUID, source_visible_message_id=_UUID, port_id=_SYMBOL,
    payload_schema_ref=_SCHEMA_REF, payload=_JSON,
)
_FORK_CANDIDATE_FLOOR = _object(
    ("user_visible_message_id", "candidate_ids", "selected_candidate_id"),
    user_visible_message_id=_UUID, candidate_ids=_array(_UUID, unique=True),
    selected_candidate_id=_UUID,
)
_VISIBLE_USER_BOUNDARY = _object(
    ("before_checkpoint_id", "input_id", "input_status", "chain_run_id"),
    before_checkpoint_id=_UUID, input_id=_UUID,
    input_status={"enum": ["pending", "running", "failed", "completed"]},
    chain_run_id=_UUID_OR_NULL,
)
_VISIBLE_ASSISTANT_BOUNDARY = _object(
    ("chain_run_id", "output_id", "after_checkpoint_id"),
    chain_run_id=_UUID, output_id=_UUID, after_checkpoint_id=_UUID_OR_NULL,
)
_STATE_RESULT = _union(
    _variant("agent_turn", turn_id=_UUID),
    _variant("node_output", output_id=_UUID),
    {"type": "null"},
)
_STATE_NODE = _object(
    ("node_binding_id", "data_version", "private_data", "selected_result"),
    node_binding_id=_UUID, data_version=_POS, private_data=_PRIVATE,
    selected_result=_STATE_RESULT,
)
_STATE_VISIBLE_REF = _union(
    *(
        _object(
            ("visible_message_id", "sequence", "role", "boundary"),
            visible_message_id=_UUID, sequence=_POS, role={"const": role}, boundary=boundary,
        )
        for role, boundary in (
            ("user", _VISIBLE_USER_BOUNDARY),
            ("assistant", _VISIBLE_ASSISTANT_BOUNDARY),
        )
    )
)
_COMMIT_SOURCE = _union(
    _variant("session_seed", workflow_session_id=_UUID),
    _variant("chain_run", chain_run_id=_UUID),
    _variant("candidate", candidate_id=_UUID),
    _variant("control_operation", operation_id=_UUID),
)
_OPERATION_VARIANTS = {
    "initialize_session": ("workflow_session", "workflow_session"),
    "submit": ("workflow_session", "workflow_session"),
    "continue_pending_input": ("workflow_session", "node_input"),
    "retry_archive": ("workflow_session", "run_record"),
    "retry_publish": ("workflow_session", "run_record"),
    "interrupt": ("workflow_session", "run_record"),
    "resume": ("workflow_session", "run_record"),
    "extend_budget": ("workflow_session", "run_record"),
    "reroll": ("workflow_session", "chain_run"),
    "close_execution": ("workflow_session", "chain_run"),
    "continue_workflow": ("workflow_session", "chain_run"),
    "select_candidate": ("workflow_session", "candidate"),
    "create_branch": ("workflow_session", "visible_message"),
    "create_and_switch_branch": ("workflow_session", "visible_message"),
    "switch_session": ("session_selection", "workflow_session"),
}
_OPERATION_REVISION = _object(
    ("kind", "id", "revision"),
    kind={"enum": ["workflow_session", "run_record", "workflow_ref", "session_selection"]},
    id=_UUID, revision=_POS,
)


CONTRACT_SCHEMAS: dict[str, dict] = {
    "node_definition": _record(
        ("component_id", "component_version", "kind", "capabilities", "ports"),
        component_id=_UUID, component_version=_SYMBOL,
        kind={"enum": ["agent", "io", "transform"]},
        capabilities=_array(_SYMBOL, unique=True),
        ports=_array(_union(_INPUT_PORT, _OUTPUT_PORT)),
    ),
    "node_binding": _record(
        ("node_binding_id", "workflow_definition_id", "workflow_definition_revision",
         "component_id", "component_version", "config"),
        node_binding_id=_UUID, workflow_definition_id=_UUID,
        workflow_definition_revision=_POS, component_id=_UUID,
        component_version=_SYMBOL, config=_PRIVATE,
    ),
    "workflow_definition_revision": _record(
        ("workflow_definition_id", "revision", "bindings", "edges"),
        workflow_definition_id=_UUID, revision=_POS,
        bindings=_array(_UUID, unique=True), edges=_array(_EDGE),
    ),
    "workflow_session": _record(
        ("workflow_session_id", "workflow_definition_id", "definition_revision",
         "revision", "source"),
        workflow_session_id=_UUID, workflow_definition_id=_UUID,
        definition_revision=_POS, revision=_POS,
        source=_union(
            _variant("new"),
            _variant("fork", source_workflow_session_id=_UUID, visible_message_id=_UUID, fork_anchor_id=_UUID),
        ),
    ),
    "node_session": _record(
        ("workflow_session_id", "node_binding_id", "data_version", "private_data"),
        workflow_session_id=_UUID, node_binding_id=_UUID,
        data_version=_POS, private_data=_PRIVATE,
    ),
    "node_input": _record(
        ("input_id", "port_id", "payload_schema_ref", "source", "payload"),
        input_id=_UUID, port_id=_SYMBOL, payload_schema_ref=_SCHEMA_REF,
        source=_INPUT_SOURCE, payload=_JSON,
    ),
    "input_snapshot": _record(
        ("snapshot_id", "workflow_session_id", "node_binding_id", "input_id",
         "parent_turn_id", "component_id", "component_version", "config",
         "s0", "tool_definitions", "model_parameters", "output_schema",
         "projection_version"),
        snapshot_id=_UUID, workflow_session_id=_UUID, node_binding_id=_UUID,
        input_id=_UUID, parent_turn_id=_UUID_OR_NULL,
        component_id=_UUID, component_version=_SYMBOL, config=_PRIVATE,
        s0=_array(_AGENT_MESSAGE), tool_definitions=_array(
            _object(("name", "version", "parameters_schema"),
                    name=_SYMBOL, version=_SYMBOL,
                    parameters_schema=_OBJECT_VALUE,
                    description={"type": "string"}),
        ),
        model_parameters=_OBJECT_VALUE, output_schema=_OBJECT_VALUE,
        projection_version=_POS,
    ),
    "run_record": _record(
        ("profile", "run_id", "workflow_session_id", "node_binding_id", "chain_run_id",
         "input_id", "snapshot_id", "source_run_id", "status", "revision", "initial_budget",
         "result_turn_id", "superseded_by_run_id"),
        profile={"const": "agent"}, run_id=_UUID, workflow_session_id=_UUID, node_binding_id=_UUID,
        chain_run_id=_UUID, input_id=_UUID, snapshot_id=_UUID,
        source_run_id=_UUID_OR_NULL, status=_STATUS, revision=_POS,
        initial_budget=_object(
            ("max_model_requests", "max_automatic_retries"),
            max_model_requests={"type": "integer", "minimum": 1, "maximum": 64},
            max_model_attempts={"type": "integer", "minimum": 1, "maximum": 256},
            max_automatic_retries={"type": "integer", "const": 3},
        ),
        result_turn_id=_UUID_OR_NULL, superseded_by_run_id=_UUID_OR_NULL,
    ),
    "node_run": _record(
        ("profile", "run_id", "workflow_session_id", "node_binding_id", "chain_run_id",
         "input_id", "input_snapshot_id", "source_run_id", "status", "revision",
         "result_output_id", "superseded_by_run_id"),
        profile={"const": "node"}, run_id=_UUID, workflow_session_id=_UUID,
        node_binding_id=_UUID, chain_run_id=_UUID, input_id=_UUID,
        input_snapshot_id=_UUID_OR_NULL, source_run_id=_UUID_OR_NULL,
        status={"enum": list(NodeStatus.__args__)},
        revision=_POS, result_output_id=_UUID_OR_NULL,
        superseded_by_run_id=_UUID_OR_NULL,
    ),
    "turn": _record(
        ("turn_id", "parent_turn_id", "run_id", "snapshot_id", "input_id",
         "messages", "final", "projection_version"),
        turn_id=_UUID, parent_turn_id=_UUID_OR_NULL, run_id=_UUID,
        snapshot_id=_UUID, input_id=_UUID, messages=_array(_GENERATED_MESSAGE),
        final=_object(("message_id", "value"), message_id=_UUID, value=_JSON),
        projection_version=_POS,
    ),
    "candidate_group": _record(
        ("candidate_group_id", "workflow_session_id", "node_binding_id",
         "logical_input_id", "parent_turn_id", "frozen_snapshot_id", "candidate_refs"),
        candidate_group_id=_UUID, workflow_session_id=_UUID, node_binding_id=_UUID,
        logical_input_id=_UUID, parent_turn_id=_UUID_OR_NULL,
        frozen_snapshot_id=_UUID,
        candidate_refs=_array(_object(("run_id", "turn_id"), run_id=_UUID, turn_id=_UUID)),
    ),
    "candidate_selection": _record(
        ("candidate_group_id", "selected_turn_id", "revision"),
        candidate_group_id=_UUID, selected_turn_id=_UUID_OR_NULL, revision=_POS,
    ),
    "agent_message": _AGENT_MESSAGE,
    "workflow_checkpoint": _union(
        *(
            _record(
                ("checkpoint_id", "workflow_session_id", "workflow_definition_id",
                 "definition_revision", "revision", "kind", "nodes", "selection_refs",
                 *anchor),
                checkpoint_id=_UUID, workflow_session_id=_UUID,
                workflow_definition_id=_UUID, definition_revision=_POS,
                revision=_POS, kind={"const": kind}, nodes=_array(_NODE_STATE),
                selection_refs=_array(_SELECTION_REF), **extra,
            )
            for kind, anchor, extra in (
                ("initial", (), {}),
                ("before_input", ("input_id",), {"input_id": _UUID}),
                ("completed_output", ("output_id",), {"output_id": _UUID}),
            )
        )
    ),
    "state_snapshot": _record(
        ("state_snapshot_id", "workflow_session_id", "workflow_definition_id",
         "definition_revision", "node_states", "selection_refs",
         "visible_message_refs", "pending_input_id"),
        state_snapshot_id=_UUID, workflow_session_id=_UUID, workflow_definition_id=_UUID,
        definition_revision=_POS, node_states=_array(_STATE_NODE),
        selection_refs=_array(_SELECTION_REF),
        visible_message_refs=_array(_STATE_VISIBLE_REF),
        pending_input_id=_UUID_OR_NULL,
    ),
    "workflow_commit": _record(
        ("commit_id", "workflow_session_id", "state_snapshot_id",
         "parent_commit_id", "operation_id", "source"),
        commit_id=_UUID, workflow_session_id=_UUID, state_snapshot_id=_UUID,
        parent_commit_id=_UUID_OR_NULL, operation_id=_UUID, source=_COMMIT_SOURCE,
    ),
    "workflow_ref": _record(
        ("workflow_ref_id", "workflow_session_id", "head_commit_id", "revision"),
        workflow_ref_id=_UUID, workflow_session_id=_UUID, head_commit_id=_UUID_OR_NULL,
        revision=_POS,
    ),
    "session_selection": _record(
        ("session_selection_id", "active_workflow_session_id", "revision"),
        session_selection_id=_UUID, active_workflow_session_id=_UUID, revision=_POS,
    ),
    "workflow_candidate": _record(
        ("candidate_id", "workflow_session_id", "chain_run_id", "base_commit_id",
         "result_commit_id", "output_id", "checkpoint_id"),
        candidate_id=_UUID, workflow_session_id=_UUID, chain_run_id=_UUID,
        base_commit_id=_UUID, result_commit_id=_UUID,
        output_id=_UUID, checkpoint_id=_UUID,
    ),
    "workflow_operation": _union(
        *(
            _record(
                ("operation_id", "kind", "scope", "target",
                 "idempotency_key", "expected_revisions", "payload"),
                operation_id=_UUID, kind={"const": kind},
                scope=_object(("kind", "id"), kind={"const": scope_kind}, id=_UUID),
                target=_object(("kind", "id"), kind={"const": target_kind}, id=_UUID),
                idempotency_key={"type": "string", "minLength": 1, "maxLength": 128},
                expected_revisions=_array(_OPERATION_REVISION),
                expected_head_commit_id=_UUID_OR_NULL,
                payload=_OBJECT_VALUE,
            )
            for kind, (scope_kind, target_kind) in _OPERATION_VARIANTS.items()
        )
    ),
    "chain_run": _union(
        *(
            constructor(
                ("chain_run_id", "workflow_session_id", "input_id", "status",
                 "node_run_ids", "output_id", *extra),
                chain_run_id=_UUID, workflow_session_id=_UUID, input_id=_UUID,
                status={"enum": list(ChainStatus.__args__)},
                node_run_ids=_array(_UUID, unique=True), output_id=_UUID_OR_NULL,
                **additional,
            )
            for constructor, extra, additional in (
                (_record, (), {}),
                (_record_v2, ("base_commit_id", "base_ref_revision", "operation_id"),
                 {"base_commit_id": _UUID, "base_ref_revision": _POS, "operation_id": _UUID}),
            )
        )
    ),
    "chain_input_origin": _record(
        ("chain_run_id", "workflow_session_id", "visible_message_id",
         "input_id", "source_chain_run_id"),
        chain_run_id=_UUID, workflow_session_id=_UUID,
        visible_message_id=_UUID, input_id=_UUID, source_chain_run_id=_UUID,
    ),
    "execution_closeout": _record(
        ("closeout_id", "workflow_session_id", "chain_run_id", "reason",
         "run_ids", "fact_refs", "diagnostic"),
        closeout_id=_UUID, workflow_session_id=_UUID, chain_run_id=_UUID,
        reason={"enum": ["lost_runtime", "execution_failed", "paused_reroll"]},
        run_ids=_array(_UUID, unique=True),
        fact_refs={**_array(_object(
            ("run_id", "sequence_count", "digest"),
            run_id=_UUID, sequence_count={"type": "integer", "minimum": 0},
            digest={"type": "string", "pattern": r"^json-v1:sha256:[0-9a-f]{64}$"},
        ))},
        diagnostic=_object(
            ("code", "category"), code=_SYMBOL,
            category={"enum": ["model", "protocol", "contract", "interrupted", "unknown"]},
        ),
    ),
    "execution_continuation": _record(
        ("chain_run_id", "workflow_session_id", "source_chain_run_id", "closeout_id",
         "source_run_id", "reused_run_ids", "evidence_message"),
        chain_run_id=_UUID, workflow_session_id=_UUID, source_chain_run_id=_UUID,
        closeout_id=_UUID, source_run_id=_UUID,
        reused_run_ids=_array(_UUID, unique=True),
        evidence_message=_RUNTIME_EXECUTION_MESSAGE,
    ),
    "workflow_output": _record(
        ("output_id", "workflow_session_id", "chain_run_id", "source",
         "port_id", "payload_schema_ref", "payload"),
        output_id=_UUID, workflow_session_id=_UUID, chain_run_id=_UUID,
        source=_variant("node_run", node_binding_id=_UUID, run_id=_UUID,
                        turn_id=_UUID_OR_NULL),
        port_id=_SYMBOL, payload_schema_ref=_SCHEMA_REF, payload=_JSON,
    ),
    "output_delivery": _record(
        ("delivery_id", "output_id", "target", "status", "idempotency_key"),
        delivery_id=_UUID, output_id=_UUID,
        target=_union(
            _variant("ui", workflow_session_id=_UUID),
            _variant("external", destination_ref=_SYMBOL),
        ),
        status={"enum": ["pending", "succeeded", "failed", "unknown"]},
        idempotency_key=_SYMBOL,
    ),
    "visible_message": _union(
        _record(
            ("visible_message_id", "origin_workflow_session_id", "role",
             "payload_schema_ref", "payload", "source"),
            visible_message_id=_UUID, origin_workflow_session_id=_UUID,
            role={"const": "user"}, payload_schema_ref=_SCHEMA_REF, payload=_JSON,
            source=_variant("workflow_input", input_id=_UUID),
        ),
        _record(
            ("visible_message_id", "origin_workflow_session_id", "role",
             "payload_schema_ref", "payload", "source"),
            visible_message_id=_UUID, origin_workflow_session_id=_UUID,
            role={"const": "assistant"}, payload_schema_ref=_SCHEMA_REF, payload=_JSON,
            source=_variant("workflow_output", output_id=_UUID),
        ),
    ),
    "visible_message_ref": _union(
        *(
            _record(
                ("workflow_session_id", "visible_message_id", "sequence", "role", "boundary"),
                workflow_session_id=_UUID, visible_message_id=_UUID,
                sequence=_POS, role={"const": role}, boundary=boundary,
            )
            for role, boundary in (
                ("user", _VISIBLE_USER_BOUNDARY),
                ("assistant", _VISIBLE_ASSISTANT_BOUNDARY),
            )
        )
    ),
    "fork_anchor": _union(
        _record(
            ("fork_anchor_id", "source_workflow_session_id", "visible_message_id", "role",
             "checkpoint_id", "pending_input"),
            fork_anchor_id=_UUID, source_workflow_session_id=_UUID, visible_message_id=_UUID,
            role={"const": "user"}, checkpoint_id=_UUID,
            pending_input=_PENDING_INPUT,
            candidate_floors=_array(_FORK_CANDIDATE_FLOOR),
        ),
        _record(
            ("fork_anchor_id", "source_workflow_session_id", "visible_message_id", "role",
             "checkpoint_id", "chain_run_id", "output_id"),
            fork_anchor_id=_UUID, source_workflow_session_id=_UUID, visible_message_id=_UUID,
            role={"const": "assistant"}, checkpoint_id=_UUID,
            chain_run_id=_UUID, output_id=_UUID,
            candidate_floors=_array(_FORK_CANDIDATE_FLOOR),
        ),
    ),
    "control_command": _union(
        _record(
            ("command_id", "target", "action", "expected_revision"),
            command_id=_UUID,
            target=_variant("run", workflow_session_id=_UUID, run_id=_UUID),
            action={"enum": ["pause", "resume", "reroll", "retry_archive"]},
            expected_revision=_POS,
        ),
        _record(
            ("command_id", "target", "action", "expected_revision", "payload"),
            command_id=_UUID,
            target=_variant("run", workflow_session_id=_UUID, run_id=_UUID),
            action={"const": "extend_budget"}, expected_revision=_POS,
            payload=_object(
                ("additional_model_requests", "max_total_model_requests"),
                additional_model_requests=_POS, max_total_model_requests=_POS,
            ),
        ),
        _record(
            ("command_id", "target", "action", "expected_revision"),
            command_id=_UUID,
            target=_variant("fork", source_workflow_session_id=_UUID,
                            visible_message_id=_UUID),
            action={"enum": ["create_branch", "create_and_switch_branch"]},
            expected_revision=_POS,
        ),
    ),
    "run_event": _union(
        _record(
            ("event_id", "workflow_session_id", "node_binding_id", "run_id",
             "sequence", "event_type", "payload"),
            event_id=_UUID, workflow_session_id=_UUID, node_binding_id=_UUID,
            run_id=_UUID, sequence=_POS,
            event_type={"enum": ["state_changed", "progress", "error",
                                 "control_result", "output_preview"]},
            payload=_OBJECT_VALUE,
        ),
        {
            **_record_v2(
                ("event_id", "workflow_session_id", "node_binding_id", "run_id",
                 "sequence", "event_type", "payload_schema_ref", "payload",
                 "visibility", "generation"),
                event_id=_UUID, workflow_session_id=_UUID, node_binding_id=_UUID,
                run_id=_UUID, sequence=_POS,
                event_type={
                    "type": "string",
                    "pattern": r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*(?![\s\S])",
                    "not": {"enum": [
                        "registered_behavior", "state_changed", "progress", "error",
                        "control_result", "output_preview",
                    ]},
                },
                payload_schema_ref=_SCHEMA_REF, payload=_OBJECT_VALUE,
                visibility={"enum": ["private", "business_candidate"]},
                generation=_UUID,
            ),
            "allOf": core_envelope_schema_conditions(),
        },
    ),
}

_FORMATS = FormatChecker()


@_FORMATS.checks("uuid4")
def _is_uuid4(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


@_FORMATS.checks("utc_millis")
def _is_utc_millis(value: object) -> bool:
    if type(value) is not str or re.fullmatch(_UTC_MILLIS_PATTERN, value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True


_STRICT_TYPE_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", lambda _checker, value: type(value) is int,
)
_StrictDraft202012Validator = validators.extend(
    Draft202012Validator, type_checker=_STRICT_TYPE_CHECKER,
)
_VALIDATORS = {
    name: _StrictDraft202012Validator(schema, format_checker=_FORMATS)
    for name, schema in CONTRACT_SCHEMAS.items()
}


def _check_tool_arguments(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        if message["schema_version"] == 5:
            from .provider_metadata import canonical_message_metadata
            canonical_message_metadata(message)
        for block in message["blocks"]:
            if block["kind"] == "tool_result":
                if message["source"]["tool_execution_id"] != block["tool_execution_id"]:
                    raise ContractValidationError("Tool result execution id differs from message source")
                if message["schema_version"] == 2:
                    source = message["source"]
                    content = block["content"]
                    if source["tool_call_id"] != block["tool_call_id"]:
                        raise ContractValidationError("Tool observation call id differs from message source")
                    if source["reason_code"] != content["reason_code"]:
                        raise ContractValidationError("Tool observation reason differs from message source")
                    if not all(content[field].strip() for field in ("error", "retry_guidance")):
                        raise ContractValidationError("Tool observation explanation and guidance must be nonempty")
                    invalid_projection = False
                    try:
                        projection = loads_strict(block["model_visible_text"])
                    except ContractValidationError:
                        invalid_projection = True
                    if invalid_projection:
                        raise ContractValidationError("Tool observation projection must encode its content")
                    if canonical_bytes(projection) != canonical_bytes(content):
                        raise ContractValidationError("Tool observation projection differs from its content")
                continue
            if block["kind"] != "tool_call":
                continue
            invalid_raw = False
            try:
                parsed = loads_strict(block["raw_arguments"])
            except ContractValidationError:
                invalid_raw = True
            if invalid_raw:
                raise ContractValidationError("Tool call raw_arguments is not strict JSON")
            if type(parsed) is not dict or canonical_bytes(parsed) != canonical_bytes(block["parsed_arguments"]):
                raise ContractValidationError("Tool call parsed_arguments differs from raw_arguments")


def _check_snapshot_secrets(value: dict[str, Any]) -> None:
    prohibited = {"api_key", "secret", "password", "credential", "access_token", "refresh_token"}
    # This checks operational key names, not arbitrary prompt text or tool schema property names.
    stack: list[Any] = [
        value["config"]["payload"], value["model_parameters"],
    ]
    while stack:
        item = stack.pop()
        if type(item) is dict:
            if prohibited.intersection(key.lower() for key in item):
                raise ContractValidationError("InputSnapshot cannot contain credential fields")
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)


def _check_snapshot_schemas(value: dict[str, Any]) -> None:
    """Check Draft 2020-12 syntax and reject refs that could leave the document."""
    schemas = [value["output_schema"]]
    schemas.extend(tool["parameters_schema"] for tool in value["tool_definitions"])
    invalid_schema = False
    try:
        for schema in schemas:
            Draft202012Validator.check_schema(schema)
    except SchemaError:
        invalid_schema = True
    if invalid_schema:
        raise ContractValidationError("InputSnapshot contains an invalid JSON Schema")
    for schema in schemas:
        _reject_external_schema_refs(schema)


def _reject_external_schema_refs(schema: dict[str, Any]) -> None:
    schema_maps = ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas")
    schema_arrays = ("allOf", "anyOf", "oneOf", "prefixItems")
    schema_singles = (
        "additionalProperties", "unevaluatedProperties", "items", "contains",
        "propertyNames", "not", "if", "then", "else", "unevaluatedItems",
        "contentSchema", "additionalItems",
    )
    stack: list[Any] = [schema]
    while stack:
        current = stack.pop()
        if type(current) is not dict:
            continue  # Boolean subschemas have no references.
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in current and not current[keyword].startswith("#"):
                raise ContractValidationError(
                    "InputSnapshot JSON Schemas allow only local fragment references"
                )
        for keyword in schema_maps:
            children = current.get(keyword)
            if type(children) is dict:
                stack.extend(children.values())
        for keyword in schema_arrays:
            children = current.get(keyword)
            if type(children) is list:
                stack.extend(children)
        for keyword in schema_singles:
            if keyword in current:
                stack.append(current[keyword])
        dependencies = current.get("dependencies")
        if type(dependencies) is dict:
            stack.extend(child for child in dependencies.values() if type(child) in (dict, bool))


def _safe_error_path(parts: Any) -> str:
    path = ""
    for part in parts:
        if type(part) is int:
            path += f"[{part}]"
        elif type(part) is str and re.fullmatch(r"[a-z][a-z0-9_]*", part):
            path += f".{part}"
        else:
            path += ".<field>"
    return path


def validate_record(record_type: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a public record and return a detached JSON-value copy.

    This performs structural validation, not reference or lifecycle validation.
    """
    from .graph_records import is_graph_record, validate_graph_record
    if is_graph_record(record_type, value):
        return validate_graph_record(record_type, value)
    if type(record_type) is not str or record_type not in _VALIDATORS:
        raise ContractValidationError("Unknown public record type")
    invalid_json: str | None = None
    try:
        validate_json_value(value)
    except ContractValidationError as exc:
        invalid_json = str(exc)
    if invalid_json is not None:
        raise ContractValidationError(f"{record_type}: {invalid_json}") from None
    if type(value) is not dict:
        raise ContractValidationError("A public record must be a JSON object")
    error = next(_VALIDATORS[record_type].iter_errors(value), None)
    if error is not None:
        path = _safe_error_path(error.absolute_path)
        keyword = error.validator if type(error.validator) is str and re.fullmatch(
            r"[A-Za-z][A-Za-z0-9]*", error.validator,
        ) else "invalid"
        raise ContractValidationError(f"{record_type}{path}: {keyword}") from None
    if record_type == "agent_message":
        _check_tool_arguments([value])
    elif record_type == "turn":
        _check_tool_arguments(value["messages"])
    elif record_type == "input_snapshot":
        _check_tool_arguments(value["s0"])
        _check_snapshot_secrets(value)
        _check_snapshot_schemas(value)
    elif record_type == "workflow_operation":
        # created_at is optional record metadata, not part of the client envelope.
        validate_workflow_operation({key: item for key, item in value.items() if key != "created_at"})
    elif (record_type == "run_event" and value["schema_version"] == 2
          and value["event_type"] in CORE_EVENT_TYPES):
        validate_core_payload(value["event_type"], value["payload_schema_ref"], value["payload"])
    return copy.deepcopy(value)
