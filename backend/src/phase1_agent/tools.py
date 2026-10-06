"""Native callable tools and an optional smolagents.Tool compatibility adapter."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator


_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})
_INPUT_KEYS = frozenset({"type", "description"})


class ToolExecutionError(Exception):
    """An implementation explicitly reports a tool-level business failure."""


class ToolOutcomeUnknown(Exception):
    """An external tool effect is uncertain while the host remains usable."""

    def __init__(self, message: str = "", *, reason_code: str = "outcome_unknown"):
        if reason_code not in {"outcome_unknown", "interrupted"}:
            raise ValueError("Unknown tool outcomes require a supported reason code")
        super().__init__(message)
        self.reason_code = reason_code


class _FrozenList(list):
    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("registered tool schema is immutable")

    __setitem__ = __delitem__ = __iadd__ = __imul__ = append = clear = extend = insert = pop = remove = reverse = sort = _immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        return [deepcopy(item, memo) for item in self]


class _FrozenDict(dict):
    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("registered tool schema is immutable")

    __setitem__ = __delitem__ = __ior__ = clear = pop = popitem = setdefault = update = _immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        return {key: deepcopy(value, memo) for key, value in self.items()}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class RegisteredTool:
    """A model-facing function definition and its callable implementation."""

    name: str
    schema: dict[str, Any]
    definition: dict[str, Any]
    _validator: Draft202012Validator = field(repr=False, compare=False)
    _implementation: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    def validate(self, arguments: dict[str, Any]) -> None:
        """Raise ``jsonschema.ValidationError`` when arguments violate the schema."""
        self._validator.validate(arguments)

    def execute(self, arguments: dict[str, Any]) -> Any:
        """Call the underlying tool with keyword arguments, or return a final answer."""
        if self._implementation is None:
            return arguments["answer"]
        return self._implementation(**arguments)


def _registered_tool(
    name: str,
    description: str,
    schema: dict[str, Any],
    implementation: Callable[..., Any] | None = None,
) -> RegisteredTool:
    Draft202012Validator.check_schema(schema)
    schema = _freeze(deepcopy(schema))
    definition = _FrozenDict({
        "type": "function",
        "function": _FrozenDict({
            "name": name,
            "description": description,
            "parameters": schema,
        }),
    })
    return RegisteredTool(
        name=name,
        schema=schema,
        definition=definition,
        _validator=Draft202012Validator(schema),
        _implementation=implementation,
    )


def _scalar_properties(inputs: dict[str, Any]) -> dict[str, dict[str, str]]:
    properties: dict[str, dict[str, str]] = {}
    for name, input_spec in inputs.items():
        if not isinstance(input_spec, dict):
            raise ValueError(f"Input '{name}' must be a dictionary")

        unknown_keys = set(input_spec) - _INPUT_KEYS
        missing_keys = _INPUT_KEYS - set(input_spec)
        if unknown_keys or missing_keys:
            raise ValueError(
                f"Input '{name}' must contain only 'type' and 'description'; "
                f"unknown keys: {sorted(unknown_keys)}, missing keys: {sorted(missing_keys)}"
            )

        input_type = input_spec["type"]
        if not isinstance(input_type, str) or input_type not in _SCALAR_TYPES:
            raise ValueError(
                f"Input '{name}' must use one required, non-nullable scalar type: "
                "'string', 'integer', 'number', or 'boolean'"
            )

        description = input_spec["description"]
        if not isinstance(description, str):
            raise ValueError(f"Input '{name}' description must be a string")
        properties[name] = {"type": input_type, "description": description}
    return properties


def register_callable(
    name: str,
    description: str,
    schema: dict[str, Any],
    implementation: Callable[..., Any],
) -> RegisteredTool:
    """Register a callable with a strict required, scalar-only JSON object schema."""
    if not isinstance(name, str) or not name or not isinstance(description, str):
        raise TypeError("name must be a nonempty string and description must be a string")
    if name == "final_answer":
        raise ValueError("'final_answer' is reserved for the control tool")
    if not callable(implementation):
        raise TypeError("implementation must be callable")
    Draft202012Validator.check_schema(schema)
    if (
        schema.get("type") != "object"
        or set(schema) != {"type", "properties", "required", "additionalProperties"}
        or not isinstance(schema.get("properties"), dict)
        or not isinstance(schema.get("required"), list)
        or schema.get("additionalProperties") is not False
    ):
        raise ValueError("Tool schema must be a strict object with properties, required, and additionalProperties=false")
    properties = _scalar_properties(schema["properties"])
    if len(schema["required"]) != len(properties) or set(schema["required"]) != set(properties):
        raise ValueError("Every tool input must be required exactly once")
    try:
        parameters = inspect.signature(implementation).parameters
    except (TypeError, ValueError) as exc:
        raise ValueError("Tool callable must have an inspectable keyword signature") from exc
    if set(parameters) != set(properties):
        raise ValueError("Tool callable parameters must match schema properties")
    for parameter in parameters.values():
        if parameter.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            raise ValueError("Tool callable parameters must accept keyword arguments")
        if parameter.default is not inspect.Parameter.empty:
            raise ValueError(f"Input '{parameter.name}' is optional; all registered inputs must be required")
    return _registered_tool(name, description, deepcopy(schema), implementation)


def register_tool(tool: Any) -> RegisteredTool:
    """Adapt a smolagents.Tool when that optional package is installed."""
    from smolagents import Tool

    if not isinstance(tool, Tool):
        raise TypeError("tool must be an instance of smolagents.Tool")
    if tool.name == "final_answer":
        raise ValueError("'final_answer' is reserved for the control tool")

    for name, parameter in inspect.signature(tool.forward).parameters.items():
        if parameter.default is not inspect.Parameter.empty:
            raise ValueError(f"Input '{name}' is optional; all registered inputs must be required")

    properties = _scalar_properties(tool.inputs)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    return _registered_tool(tool.name, tool.description, schema, tool)


def final_answer_tool() -> RegisteredTool:
    """Create the control tool; output-spec validation remains a kernel concern."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "answer": {
                "type": "object",
                "description": "The final JSON object, validated separately against output_spec.",
            }
        },
        "required": ["answer"],
        "additionalProperties": False,
    }
    return _registered_tool(
        "final_answer",
        "Return the final JSON answer.",
        schema,
    )
