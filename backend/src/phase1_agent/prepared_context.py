"""Public prepared Context implementation with detached, frozen evidence."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid4
from typing import Any

from .bindings import BasicContext, ComponentRegistry
from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history, validate_turn_final
from .contract_json import canonical_bytes, dumps_pretty
from .contracts_v2 import validate_record
from .prompt_preparation import (
    prepare_prompt_context, validate_context_preparation, validate_prompt_context_config,
)
from .prompt_variables import validate_variable_snapshot
from .prompt_errors import PromptProcessingError
from .variable_preparation import (
    validate_frozen_variable_preparation, validate_root_variable_rederivation,
)


PREPARED_CONTEXT_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a10e"
PREPARED_CONTEXT_VERSION = "1.0.0"
PREPARATION_CAPABILITY = "prompt_preparation_v1"
PREPARED_PROJECTION_VERSION = 2
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


def _input_source(node_input: dict[str, Any]) -> dict[str, Any]:
    source = node_input["source"]
    if source["kind"] == "visible_message":
        return {"kind": "human", "visible_message_id": source["visible_message_id"]}
    if source["kind"] == "upstream_output":
        return {"kind": "upstream_node", "output_id": source["output_id"]}
    raise ContractValidationError("Prepared Context requires an explicit input source")


def validate_frozen_preparation(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Validate saved preparation without rerunning a rule or reading live values."""
    snapshot = validate_record("input_snapshot", snapshot)
    payload = snapshot["config"]["payload"]
    descriptor = payload.get("resolved", {}).get("context", {})
    prepared = PREPARATION_CAPABILITY in descriptor.get("capabilities", [])
    evidence = payload.get("context_preparation")
    if not prepared:
        _require(evidence is None, "Legacy Context cannot carry prepared projection evidence")
        return None
    _require(evidence is not None, "Prepared Context requires frozen projection evidence")
    evidence = validate_context_preparation(evidence)
    _require(snapshot["projection_version"] == PREPARED_PROJECTION_VERSION,
             "Prepared Context projection version is incompatible")
    base = evidence["s0"]
    _require(canonical_bytes(snapshot["s0"][:len(base)]) == canonical_bytes(base),
             "Frozen S0 differs from its preparation evidence")
    for observation in snapshot["s0"][len(base):]:
        _require(observation["schema_version"] == 2
                 and observation["source"]["kind"] == "runtime_execution_observation",
                 "Only saved continuation observations may extend prepared S0")
    validate_message_history(snapshot["s0"])
    _require(canonical_bytes(payload["context"]) == canonical_bytes(evidence["config"]),
             "Frozen Context config differs from its preparation evidence")
    _require(snapshot["parent_turn_id"] == evidence["send_view"]["parent_turn_id"],
             "Frozen parent differs from its preparation evidence")
    _require(snapshot["node_binding_id"] == evidence["send_view"]["node_binding_id"],
             "Frozen node binding differs from its preparation evidence")
    variables = payload.get("variable_preparation")
    if payload["context"]["schema_version"] == 2:
        _require(type(variables) is dict and set(variables) == {
            "schema_version", "kind", "state_ref", "receipt_key", "frozen", "rederivation", "seed",
        }, "Variable Context requires exact frozen variable evidence")
        _require(type(variables["schema_version"]) is int and variables["schema_version"] == 1
                 and variables["kind"] == "workflow_variable_preparation",
                 "Frozen variable evidence version or kind is invalid")
        frozen = validate_frozen_variable_preparation(variables["frozen"])
        rederived = variables["rederivation"]
        if rederived is not None:
            rederived = validate_root_variable_rederivation(rederived, frozen)
        expected = rederived["snapshot"] if rederived is not None else frozen["snapshot"]
        _require(canonical_bytes(expected) == canonical_bytes(payload["context"]["variables"]),
                 "Frozen variable outcome differs from prepared Context")
        _require(canonical_bytes(frozen["plan"]) == canonical_bytes(payload["context"]["variable_plan"]),
                 "Frozen variable rules differ from prepared Context")
        reference = variables["state_ref"]
        _require(type(reference) is dict and set(reference) == {
            "workflow_session_id", "workflow_id", "registry_revision", "revision",
        } and all(type(reference[field]) is int and reference[field] >= 1
                  for field in ("registry_revision", "revision")),
                 "Frozen variable state reference is invalid")
        for field in ("workflow_session_id", "workflow_id"):
            try:
                identity = UUID(reference[field])
                _require(identity.version == 4 and str(identity) == reference[field],
                         "Frozen variable owner must be a canonical UUID4")
            except (ValueError, TypeError, AttributeError):
                raise ContractValidationError("Frozen variable owner must be a canonical UUID4") from None
        _require(type(variables["receipt_key"]) is str and bool(variables["receipt_key"].strip())
                 and len(variables["receipt_key"]) <= 128,
                 "Frozen variable receipt identity is invalid")
        seed = variables["seed"]
        _require(seed is None or type(seed) is dict and set(seed) == {"commit_id", "state_ref"},
                 "Frozen variable seed fields are invalid")
        if seed is not None:
            try:
                commit_identity = UUID(seed["commit_id"])
                _require(commit_identity.version == 4 and str(commit_identity) == seed["commit_id"],
                         "Frozen variable seed commit identity is invalid")
            except (ValueError, TypeError, AttributeError):
                raise ContractValidationError("Frozen variable seed commit identity is invalid") from None
            seed_ref = seed["state_ref"]
            _require(type(seed_ref) is dict and set(seed_ref) == set(reference)
                     and all(seed_ref[field] == reference[field] for field in (
                         "workflow_session_id", "workflow_id", "registry_revision",
                     )) and type(seed_ref["registry_revision"]) is int
                     and type(seed_ref["revision"]) is int
                     and 1 <= seed_ref["revision"] < reference["revision"],
                     "Frozen variable seed reference is inconsistent")
        _require(reference["workflow_id"] == expected["registry"]["workflow_id"]
                 and reference["registry_revision"] == expected["registry"]["revision"]
                 and reference["workflow_session_id"] == expected["workflow_session_id"],
                 "Frozen variable state reference differs from its variable owner")
        original_input = rederived["node_input"] if rederived is not None else frozen["node_input"]
        _require(all(canonical_bytes(original_input[field]) == canonical_bytes(evidence["node_input"][field])
                     for field in ("schema_version", "port_id", "payload_schema_ref", "source", "payload")),
                 "Frozen variable dependency input differs from Context preparation")
    else:
        _require(variables is None, "Static variable Context cannot carry transaction evidence")
    program_variables = payload.get("program_variable_preparation")
    if payload["context"]["schema_version"] == 3:
        _require(type(program_variables) is dict and set(program_variables) == {
            "schema_version", "kind", "session_id", "revision", "receipt_key",
        } and program_variables["schema_version"] == 1
                 and program_variables["kind"] == "program_variable_preparation"
                 and type(program_variables["revision"]) is int and program_variables["revision"] >= 1,
                 "Preparation program requires a durable variable result")
    else:
        _require(program_variables is None, "Legacy context cannot carry program variable evidence")
    # An original frozen start may be cloned for reroll/fork, retaining origin
    # identities in the evidence. Its semantic input is checked by the owner.
    return evidence


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


def check_prepared_request_capacity(
    snapshot: dict[str, Any], *, messages: list[dict[str, Any]] | None = None,
) -> None:
    """Bound a complete request, including accepted deltas and frozen tools."""
    from .prompt_assembly import message_content_chars

    evidence = validate_frozen_preparation(snapshot)
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
        turn = validate_record("turn", turn)
        node_input = validate_record("node_input", node_input)
        snapshot = validate_record("input_snapshot", snapshot)
        _require(turn["snapshot_id"] == snapshot["snapshot_id"]
                 and turn["input_id"] == snapshot["input_id"] == node_input["input_id"],
                 "Turn, snapshot and input identities differ")
        validate_turn_final(turn, snapshot)
        evidence = validate_frozen_preparation(snapshot)
        if evidence is None:
            return BasicContext().project_turn(turn, node_input, snapshot)
        root = next(
            message for message in evidence["canonical_messages"]
            if message["message_id"] == evidence["root_locator"]["message_id"]
        )
        _require(root["source"] == _input_source(node_input)
                 and root["blocks"] == [{"kind": "text", "text": dumps_pretty(node_input["payload"])}],
                 "Prepared history root differs from its canonical input")
        projected = [root, *turn["messages"]]
        validate_message_history(projected)
        return deepcopy(projected)


def register_prepared_context(registry: ComponentRegistry) -> None:
    registry.register(
        "context", PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION,
        PreparedPromptContext(), capabilities=PREPARED_CONTEXT_CAPABILITIES,
        config_schema=deepcopy(PREPARED_CONTEXT_CONFIG_SCHEMA),
    )
