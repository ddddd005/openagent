"""Public prepared Context implementation with detached, frozen evidence."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4
from typing import Any

from .bindings import ComponentRegistry
from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes
from .contracts_v2 import validate_record
from .legacy_context_contracts import (
    PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION, PREPARED_PROJECTION_VERSION,
    _validate_validated_frozen_preparation, project_prepared_turn,
)
from .prepared_request import PREPARATION_CAPABILITY, check_prepared_request_capacity
from .prompt_preparation import (
    prepare_prompt_context, validate_prompt_context_config,
)
from .prompt_variables import validate_variable_snapshot
from .prompt_errors import PromptProcessingError


PREPARED_CONTEXT_CAPABILITIES = frozenset({
    "sources", "paired_history", "frozen_projection", PREPARATION_CAPABILITY,
})

_PREPARED_V1_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "kind": {"const": "prompt_context_config"},
        "collection": {"type": "object"},
        "prompt_config": {"type": ["object", "null"]},
        "steps": {"type": "array"},
        "variables": {"type": ["object", "null"]},
        "lorebooks": {"type": "array"},
        "context_regex": {"type": "array"},
        "limits": {"type": "object"},
    },
    "required": [
        "schema_version", "kind", "collection", "prompt_config", "steps", "variables",
        "lorebooks", "context_regex", "limits",
    ],
    "additionalProperties": False,
}
PREPARED_CONTEXT_CONFIG_SCHEMA = {"oneOf": [
    _PREPARED_V1_CONFIG_SCHEMA,
    {
        **deepcopy(_PREPARED_V1_CONFIG_SCHEMA),
        "properties": {
            **deepcopy(_PREPARED_V1_CONFIG_SCHEMA["properties"]),
            "schema_version": {"type": "integer", "const": 2},
            "variable_plan": {"type": "object"},
        },
        "required": [*_PREPARED_V1_CONFIG_SCHEMA["required"], "variable_plan"],
    },
    {
        **deepcopy(_PREPARED_V1_CONFIG_SCHEMA),
        "properties": {
            **deepcopy(_PREPARED_V1_CONFIG_SCHEMA["properties"]),
            "schema_version": {"type": "integer", "const": 3},
            "preparation": {"type": "object"},
            "preparation_state": {"type": "object"},
            "preparation_cache": {"type": "object"},
            "context_sources": {"type": "object"},
        },
        "required": [*_PREPARED_V1_CONFIG_SCHEMA["required"], "preparation",
                     "preparation_state", "preparation_cache"],
    },
]}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def validate_frozen_preparation(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Validate saved preparation without rerunning a rule or reading live values."""
    snapshot = validate_record("input_snapshot", snapshot)
    return _validate_validated_frozen_preparation(snapshot)


def bind_prompt_config_scope(
    config: dict, *, workflow_session_id: str, node_binding_id: str,
) -> dict[str, Any]:
    """Bind explicit read-only presets, without refreshing declared values."""
    config = validate_prompt_context_config(config)
    variables = config["variables"]
    if variables is not None:
        variables["workflow_session_id"] = workflow_session_id
        variables["node_binding_id"] = node_binding_id
        for name, value in (
            ("workflow_session_id", workflow_session_id), ("node_binding_id", node_binding_id),
        ):
            variables["values"][name]["value"] = value
        config["variables"] = validate_variable_snapshot(variables)
    return config


def validate_prompt_registry_owner(
    config: dict, *, workflow_definition_id: str, definition_revision: int,
) -> None:
    variables = config["variables"]
    if variables is not None:
        registry = variables["registry"]
        _require(registry["workflow_id"] == workflow_definition_id
                 and registry["revision"] == definition_revision,
                 "Variable registry does not belong to the exact workflow definition")


def _check_validated_prepared_request_capacity(
    snapshot: dict[str, Any], *, messages: list[dict[str, Any]] | None = None,
) -> None:
    """Bound a complete request after the public gate validates its snapshot."""
    from .prompt_assembly import message_content_chars

    evidence = _validate_validated_frozen_preparation(snapshot)
    if evidence is None:
        return
    request_messages = snapshot["s0"] if messages is None else messages
    limits = evidence["config"]["limits"]["assembly"]
    tool_chars = len(canonical_bytes(snapshot["tool_definitions"]).decode("utf-8"))
    if (len(request_messages) > limits["max_messages"]
            or message_content_chars(request_messages) + tool_chars > limits["max_total_chars"]):
        raise PromptProcessingError(
            "prompt_capacity_exceeded", "Prepared model request exceeds its total capacity limit",
        )


class PreparedRequestAdapter:
    """Keep the dispatch gate even when the selected Kernel is replaced."""

    def __init__(self, implementation: Any, snapshot: dict[str, Any]):
        self.implementation = implementation
        self._snapshot = deepcopy(snapshot)
        self._wire_tools = [
            {"type": "function", "function": {
                "name": tool["name"],
                "parameters": deepcopy(tool["parameters_schema"]),
                **({"description": tool["description"]} if "description" in tool else {}),
            }}
            for tool in snapshot["tool_definitions"]
        ]

    def generate(self, messages, tools):
        _require(canonical_bytes(tools) == canonical_bytes(self._wire_tools),
                 "Prepared model request tools differ from frozen definitions")
        check_prepared_request_capacity(self._snapshot, messages=messages)
        return self.implementation.generate(messages, tools)

    def close(self) -> None:
        close = getattr(self.implementation, "close", None)
        if callable(close):
            close()


class PreparedPromptContext:
    """Use explicit public preparation data, never a catalog or mutable session."""

    def prepare_context(
        self, node_input: dict, history: list[dict], *, config: dict,
        workflow_session_id: str, node_binding_id: str, parent_turn_id: str | None,
        logical_floors: list[list[str]], protected_blocks: list[dict],
    ) -> dict[str, Any]:
        runtime_config = bind_prompt_config_scope(
            config, workflow_session_id=workflow_session_id, node_binding_id=node_binding_id,
        )
        root_id = str(uuid4())
        return prepare_prompt_context(
            deepcopy(node_input), deepcopy(history),
            config=runtime_config, workflow_session_id=workflow_session_id,
            node_binding_id=node_binding_id, parent_turn_id=parent_turn_id,
            logical_floors=deepcopy(logical_floors), protected_blocks=deepcopy(protected_blocks),
            root_message_id=root_id,
        )

    def project_turn(self, turn: dict, node_input: dict, snapshot: dict) -> list[dict[str, Any]]:
        return project_prepared_turn(turn, node_input, snapshot)


def register_prepared_context(registry: ComponentRegistry) -> None:
    registry.register(
        "context", PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION,
        PreparedPromptContext(), capabilities=PREPARED_CONTEXT_CAPABILITIES,
        config_schema=deepcopy(PREPARED_CONTEXT_CONFIG_SCHEMA),
    )
