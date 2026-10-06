"""Versioned component resolution, context projection, and Agent input freezing.

This v2 boundary has no dependency on the phase-one runtime or persistence.
Callers must supply the selected successful history and persist a validated
snapshot before handing it to a model or tool executor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from inspect import signature
from typing import Any, Protocol
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history
from .contract_json import dumps_pretty, validate_json_value
from .contracts_v2 import validate_record
from .legacy_context_contracts import project_basic_turn
from .tools import RegisteredTool


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _uuid4(value: Any, field: str) -> None:
    _require(type(value) is str, f"{field} must be a UUID v4 string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ContractValidationError(f"{field} must be a UUID v4 string") from None
    _require(parsed.version == 4 and str(parsed) == value, f"{field} must be a UUID v4 string")


def _config_envelope(config: dict[str, Any], component_id: str) -> dict[str, Any]:
    _require(type(config) is dict, "Private config must be an object")
    validate_json_value(config)
    _require(set(config) == {"owner_component_id", "schema_version", "payload"},
             "Private config requires owner_component_id, schema_version and payload")
    _require(config["owner_component_id"] == component_id, "Private config owner mismatch")
    _require(type(config["schema_version"]) is int and config["schema_version"] == 1,
             "Unsupported private config schema version")
    return deepcopy(config)


def _reject_external_refs(schema: dict[str, Any]) -> None:
    stack: list[Any] = [schema]
    while stack:
        item = stack.pop()
        if type(item) is dict:
            for key, value in item.items():
                if key in ("$ref", "$dynamicRef"):
                    _require(type(value) is str and value.startswith("#"),
                             "Private config schema must use local references")
                stack.append(value)
        elif type(item) is list:
            stack.extend(item)


@dataclass(frozen=True)
class ResolvedComponent:
    implementation: Any
    descriptor: dict[str, Any]
    config: dict[str, Any] = field(default_factory=dict)

    def require_api(self) -> None:
        """Check the public v1 calling convention without invoking a component."""
        calls = {
            "context": {
                "prepare": (({}, []), {"config": {}}),
                "project_turn": (({}, {}, {}), {}),
            },
            "kernel": {"run": (({}, (), object()), {
                "max_model_requests": 1, "max_model_attempts": 4,
                "checkpoint": None, "on_progress": None, "on_boundary": None,
                "on_tool_event": None, "on_fact": None,
            })},
            "io": {"emit": (({},), {"config": {}})},
        }
        required = calls.get(self.descriptor["kind"], {})
        if (self.descriptor["kind"] == "context"
                and "prompt_preparation_v1" in self.descriptor["capabilities"]):
            required = {
                "prepare_context": (({}, []), {
                    "config": {}, "workflow_session_id": "", "node_binding_id": "",
                    "parent_turn_id": None, "logical_floors": [], "protected_blocks": [],
                }),
                "project_turn": (({}, {}, {}), {}),
            }
        if (self.descriptor["kind"] == "kernel"
                and "pause_resume" in self.descriptor["capabilities"]):
            required = {**required, "validate_checkpoint": ((object(), {}), {})}
        if "events" in self.descriptor["capabilities"]:
            required = {
                name: (args, {**kwargs, "on_event": None})
                if name in {"run", "emit"} else (args, kwargs)
                for name, (args, kwargs) in required.items()
            }
        for name, (args, kwargs) in required.items():
            method = getattr(self.implementation, name, None)
            _require(callable(method), f"Component requires public {name} API")
            try:
                signature(method).bind(*args, **kwargs)
            except (ValueError, TypeError):
                raise ContractValidationError(f"Component has incompatible public {name} API") from None


@dataclass(frozen=True)
class ComponentSelection:
    component_id: str
    version: str
    config: dict[str, Any]
    required_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowComponents:
    """Explicit replacements for the fixed workflow's public component ports."""

    registry: ComponentRegistry
    contexts: Mapping[str, ComponentSelection] = field(default_factory=dict)
    kernels: Mapping[str, ComponentSelection] = field(default_factory=dict)
    output: ComponentSelection | None = None
    workflow_definition_id: str | None = None
    event_registry: Any = None


class ContextComponent(Protocol):
    def prepare(self, node_input: dict, history: list[dict], *, config: dict) -> list[dict]: ...

    def project_turn(self, turn: dict, node_input: dict, snapshot: dict) -> list[dict]: ...


class PreparedContextComponent(Protocol):
    """Optional versioned preparation capability; legacy Context is unchanged."""

    def prepare_context(
        self, node_input: dict, history: list[dict], *, config: dict,
        workflow_session_id: str, node_binding_id: str, parent_turn_id: str | None,
        logical_floors: list[list[str]], protected_blocks: list[dict],
    ) -> dict: ...

    def project_turn(self, turn: dict, node_input: dict, snapshot: dict) -> list[dict]: ...


class KernelComponent(Protocol):
    def run(
        self, snapshot: dict, tools: Sequence[RegisteredTool], adapter: Any, *,
        max_model_requests: int, max_model_attempts: int, checkpoint: Any,
        on_progress: Any, on_boundary: Any, on_tool_event: Any, on_fact: Any,
    ) -> Any: ...


class OutputComponent(Protocol):
    def emit(self, node_input: dict, *, config: dict) -> Any: ...


class ComponentRegistry:
    """Resolve an exact implementation version after private config validation."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], tuple[Any, dict[str, Any], dict[str, Any]]] = {}
        self._event_refs: dict[tuple[str, str, str], tuple[dict[str, Any], ...]] = {}

    def register(
        self,
        kind: str,
        component_id: str,
        version: str,
        implementation: Any,
        *,
        capabilities: frozenset[str],
        config_schema: dict,
        contract_version: int = 1,
        event_refs: Sequence[dict[str, Any]] = (),
    ) -> None:
        _require(type(kind) is str and bool(kind), "Component kind must be nonempty")
        _uuid4(component_id, "component_id")
        _require(type(version) is str and bool(version), "Component version must be nonempty")
        _require(type(contract_version) is int and contract_version == 1,
                 "Unsupported component contract version")
        _require(type(capabilities) is frozenset and all(
            type(item) is str and bool(item) for item in capabilities
        ), "Capabilities must be a frozenset of nonempty strings")
        _require(type(config_schema) is dict, "Private config schema must be an object")
        _require(isinstance(event_refs, (tuple, list)), "Event references must be a sequence")
        validate_json_value(list(event_refs))
        _require(not event_refs or "events" in capabilities,
                 "Behavior declarations require the events capability")
        validate_json_value(config_schema)
        try:
            Draft202012Validator.check_schema(config_schema)
        except SchemaError:
            raise ContractValidationError("Invalid private config schema") from None
        _reject_external_refs(config_schema)
        key = (kind, component_id, version)
        _require(key not in self._entries, "Duplicate component kind/id/version")
        descriptor = {
            "kind": kind,
            "component_id": component_id,
            "component_version": version,
            "contract_version": 1,
            "capabilities": sorted(capabilities),
        }
        self._entries[key] = (implementation, descriptor, deepcopy(config_schema))
        self._event_refs[key] = tuple(deepcopy(list(event_refs)))

    def resolve(
        self,
        kind: str,
        component_id: str,
        version: str,
        *,
        required_capabilities: Sequence[str] = (),
        config: dict,
    ) -> ResolvedComponent:
        _require(type(kind) is str and bool(kind), "Component kind must be nonempty")
        _uuid4(component_id, "component_id")
        _require(type(version) is str and bool(version), "Component version must be nonempty")
        _require(isinstance(required_capabilities, (tuple, list, set, frozenset)) and all(
            type(item) is str and bool(item) for item in required_capabilities
        ), "Required capabilities must be nonempty strings")
        entry = self._entries.get((kind, component_id, version))
        _require(entry is not None, "Component version is not registered")
        implementation, descriptor, config_schema = entry
        envelope = _config_envelope(config, component_id)
        missing = set(required_capabilities) - set(descriptor["capabilities"])
        _require(not missing, f"Missing component capabilities: {', '.join(sorted(missing))}")
        try:
            Draft202012Validator(config_schema).validate(envelope["payload"])
        except (ValidationError, RecursionError):
            raise ContractValidationError("Private config payload does not match component schema") from None
        return ResolvedComponent(implementation, deepcopy(descriptor), envelope)

    def detached(self) -> ComponentRegistry:
        """Freeze registrations for a coordinator; retain only implementation handles."""
        registry = ComponentRegistry()
        registry._entries = {
            key: (implementation, deepcopy(descriptor), deepcopy(schema))
            for key, (implementation, descriptor, schema) in self._entries.items()
        }
        registry._event_refs = deepcopy(self._event_refs)
        return registry

    def event_refs(self, kind: str, component_id: str, version: str) -> tuple[dict[str, Any], ...]:
        """Return declared exact payload references, never a latest-version lookup."""
        key = (kind, component_id, version)
        _require(key in self._entries, "Component version is not registered")
        return deepcopy(self._event_refs.get(key, ()))

    def is_registered(self, kind: str, component_id: str, version: str) -> bool:
        """Check an exact registration without exposing implementation handles."""
        return (kind, component_id, version) in self._entries


class BasicContext:
    """Project caller-selected successful v2 messages into a detached S0."""

    def prepare(
        self,
        node_input: dict,
        history: list[dict],
        *,
        config: dict | None = None,
        prompt_id: str | None = None,
        prompt_revision: int | None = None,
        system_prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        if config is not None:
            _require(type(config) is dict and set(config) == {
                "prompt_id", "prompt_revision", "system_prompt",
            }, "BasicContext requires the public prompt config")
            _require(prompt_id is None and prompt_revision is None and system_prompt is None,
                     "Context config must not be combined with prompt keywords")
            prompt_id, prompt_revision, system_prompt = (
                config["prompt_id"], config["prompt_revision"], config["system_prompt"],
            )
        node_input = validate_record("node_input", node_input)
        _uuid4(prompt_id, "prompt_id")
        _require(type(prompt_revision) is int and prompt_revision > 0,
                 "Prompt revision must be positive")
        _require(type(system_prompt) is str, "System prompt must be text")
        _require(type(history) is list, "Successful history must be a list")
        projected: list[dict[str, Any]] = []
        for item in history:
            _require(type(item) is dict, "History entries must be v2 records")
            if "messages" in item:
                turn = validate_record("turn", item)
                validate_message_history(turn["messages"])
                final_id = turn["final"]["message_id"]
                last_assistant = next(
                    (message for message in reversed(turn["messages"]) if message["role"] == "assistant"), None
                )
                _require(last_assistant is not None and last_assistant["message_id"] == final_id,
                         "History Turn has no last assistant final")
                projected.extend(turn["messages"])
            else:
                projected.append(validate_record("agent_message", item))
        _require(all(message["role"] != "system" for message in projected),
                 "History must not replace the frozen system prompt")
        validate_message_history(projected)

        source = node_input["source"]
        if source["kind"] == "visible_message":
            user_source = {"kind": "human", "visible_message_id": source["visible_message_id"]}
        elif source["kind"] == "upstream_output":
            user_source = {"kind": "upstream_node", "output_id": source["output_id"]}
        else:
            raise ContractValidationError("External input requires an explicit v2 source projection")
        system = {
            "schema_version": 1, "message_id": str(uuid4()), "role": "system",
            "source": {"kind": "prompt", "prompt_id": prompt_id, "revision": prompt_revision},
            "blocks": [{"kind": "text", "text": system_prompt}],
        }
        user = {
            "schema_version": 1, "message_id": str(uuid4()), "role": "user",
            "source": user_source,
            "blocks": [{"kind": "text", "text": dumps_pretty(node_input["payload"])}],
        }
        result = [system, *projected, user]
        validate_message_history(result)
        return result

    def project_turn(
        self,
        turn: dict,
        node_input: dict,
        snapshot: dict,
    ) -> list[dict[str, Any]]:
        """Project one archived turn's own input and accepted message delta."""
        return project_basic_turn(turn, node_input, snapshot)


def build_snapshot(
    *,
    workflow_session_id: str,
    node_binding_id: str,
    node_input: dict,
    parent_turn_id: str | None,
    node_definition: dict,
    config: dict,
    s0: list[dict],
    tools: Sequence[RegisteredTool],
    model_parameters: dict,
    output_schema: dict,
    projection_version: int = 1,
) -> dict[str, Any]:
    """Freeze only public JSON values, never tool callables or model clients."""
    node_input = validate_record("node_input", node_input)
    node_definition = validate_record("node_definition", node_definition)
    _require(node_definition["kind"] == "agent", "Agent snapshot requires an Agent component")
    component_id = node_definition["component_id"]
    config = _config_envelope(config, component_id)
    _require(type(s0) is list, "S0 must be a list of v2 messages")
    _require(type(projection_version) is int and projection_version >= 1,
             "Projection version must be a positive integer")
    validate_message_history(s0)
    _require(isinstance(tools, (tuple, list)) and all(
        isinstance(tool, RegisteredTool) for tool in tools
    ), "Tools must be RegisteredTool objects")
    names = [tool.name for tool in tools]
    _require(len(names) == len(set(names)), "Duplicate frozen tool name")
    definitions = [
        {
            "name": tool.name,
            "version": "1",
            "description": tool.definition["function"]["description"],
            "parameters_schema": deepcopy(tool.schema),
        }
        for tool in tools
    ]
    _require(type(model_parameters) is dict, "Model parameters must be an object")
    _require("model" in model_parameters and set(model_parameters) <= {
        "model", "max_tokens", "temperature", "thinking", "stream"
    }, "Model parameters require an explicit model and allow only frozen execution settings")
    model = model_parameters["model"]
    _require(type(model) is str and bool(model.strip()), "Model name must be nonempty")
    if "max_tokens" in model_parameters:
        _require(type(model_parameters["max_tokens"]) is int and model_parameters["max_tokens"] > 0,
                 "max_tokens must be positive")
    if "temperature" in model_parameters:
        temperature = model_parameters["temperature"]
        _require(type(temperature) in (int, float) and 0 <= temperature <= 2,
                 "temperature must be between 0 and 2")
    if "thinking" in model_parameters:
        _require(model_parameters["thinking"] == "disabled", "Only disabled thinking is supported")
    if "stream" in model_parameters:
        _require(model_parameters["stream"] is False, "Only non-streaming snapshots are supported")
    snapshot = {
        "schema_version": 1,
        "snapshot_id": str(uuid4()),
        "workflow_session_id": workflow_session_id,
        "node_binding_id": node_binding_id,
        "input_id": node_input["input_id"],
        "parent_turn_id": parent_turn_id,
        "component_id": component_id,
        "component_version": node_definition["component_version"],
        "config": config,
        "s0": s0,
        "tool_definitions": definitions,
        "model_parameters": model_parameters,
        "output_schema": output_schema,
        "projection_version": projection_version,
    }
    return validate_record("input_snapshot", snapshot)
