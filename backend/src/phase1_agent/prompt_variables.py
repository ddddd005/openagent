"""Pure workflow variable definitions and detached session snapshots.

These records are preparation data, not a persistent variable catalog. Forking
a record here does not perform a real workflow fork or mutate saved history.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import validate_json_value
from .prompt_errors import PromptProcessingError


VARIABLE_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
VARIABLE_TYPES = frozenset({"string", "integer", "number", "boolean"})
PRESET_VARIABLE_NAMES = frozenset({"workflow_session_id", "node_binding_id"})
_NAME = re.compile(VARIABLE_NAME_PATTERN)


def _fail(message: str, *, node_id: str | None = None) -> None:
    raise PromptProcessingError("invalid_variable_data", message, node_id=node_id)


def _json(value: Any, *, node_id: str | None = None) -> None:
    try:
        validate_json_value(value)
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ContractValidationError, ValueError, OverflowError, RecursionError):
        _fail("Variable data must be serializable JSON", node_id=node_id)


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if type(value) is not dict:
        _fail("Variable record must be a JSON object")
    if not required <= value.keys() or value.keys() - required - (optional or set()):
        _fail("Variable record fields are invalid")


def _identifier(value: Any) -> None:
    if type(value) is not str or not value.strip():
        _fail("Variable identity must be nonempty text")


def _name(value: Any) -> None:
    if type(value) is not str or _NAME.fullmatch(value) is None:
        _fail("Variable name is invalid")


def _typed_value(variable_type: str, value: Any) -> None:
    accepted = {
        "string": type(value) is str,
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
    }
    if variable_type not in VARIABLE_TYPES or not accepted[variable_type]:
        _fail("Variable value does not match its declared type")
    _json(value)


def _definition_source(registry: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "workflow_id": registry["workflow_id"],
        "registry_revision": registry["revision"],
    }


def create_variable_registry(
    *, workflow_id: str, revision: int, definitions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Register exact workflow definitions; absent default means unassigned."""
    return validate_variable_registry({
        "schema_version": 1,
        "kind": "variable_registry",
        "workflow_id": workflow_id,
        "revision": revision,
        "definitions": definitions,
        "source": {"kind": "workflow_definition"},
    })


def validate_variable_registry(value: Any) -> dict[str, Any]:
    """Validate a strict registry envelope and return a detached copy."""
    _json(value)
    _object(
        value,
        {"schema_version", "kind", "workflow_id", "revision", "definitions", "source"},
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _fail("Unsupported variable registry version")
    if value["kind"] != "variable_registry":
        _fail("Invalid variable registry kind")
    _identifier(value["workflow_id"])
    if type(value["revision"]) is not int or value["revision"] < 1:
        _fail("Variable registry revision must be a positive integer")
    if value["source"] != {"kind": "workflow_definition"}:
        _fail("Invalid variable definition source")
    if type(value["definitions"]) is not list:
        _fail("Variable definitions must be an ordered JSON array")
    seen: set[str] = set()
    for definition in value["definitions"]:
        _object(definition, {"name", "type"}, {"default"})
        _name(definition["name"])
        if definition["name"] in PRESET_VARIABLE_NAMES or definition["name"] in seen:
            _fail("Variable definition is repeated or reserved")
        seen.add(definition["name"])
        if type(definition["type"]) is not str or definition["type"] not in VARIABLE_TYPES:
            _fail("Unknown variable type")
        if "default" in definition:
            _typed_value(definition["type"], definition["default"])
    return copy.deepcopy(value)


def create_variable_snapshot(
    registry: dict[str, Any], *, workflow_session_id: str, node_binding_id: str,
) -> dict[str, Any]:
    """Initialize one session from exact definitions and explicit preset values."""
    registry = validate_variable_registry(registry)
    _json([workflow_session_id, node_binding_id])
    _identifier(workflow_session_id)
    _identifier(node_binding_id)
    values: dict[str, Any] = {}
    for definition in registry["definitions"]:
        has_default = "default" in definition
        entry = {
            "type": definition["type"],
            "source": _definition_source(registry, "default" if has_default else "unassigned"),
        }
        if has_default:
            entry["value"] = definition["default"]
        values[definition["name"]] = entry
    for name, value in (
        ("workflow_session_id", workflow_session_id), ("node_binding_id", node_binding_id),
    ):
        values[name] = {
            "type": "string", "value": value,
            "source": {"kind": "preset", "parameter": name},
        }
    return validate_variable_snapshot({
        "schema_version": 1,
        "kind": "variable_snapshot",
        "workflow_session_id": workflow_session_id,
        "node_binding_id": node_binding_id,
        "registry": registry,
        "values": values,
    })


def validate_variable_snapshot(value: Any) -> dict[str, Any]:
    """Validate values, provenance and registry references; return a deep copy."""
    _json(value)
    _object(
        value,
        {"schema_version", "kind", "workflow_session_id", "node_binding_id", "registry", "values"},
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _fail("Unsupported variable snapshot version")
    if value["kind"] != "variable_snapshot":
        _fail("Invalid variable snapshot kind")
    _identifier(value["workflow_session_id"])
    _identifier(value["node_binding_id"])
    registry = validate_variable_registry(value["registry"])
    definitions = {entry["name"]: entry for entry in registry["definitions"]}
    if type(value["values"]) is not dict or set(value["values"]) != definitions.keys() | PRESET_VARIABLE_NAMES:
        _fail("Variable snapshot names do not match the registry")
    for name, entry in value["values"].items():
        _object(entry, {"type", "source"}, {"value"})
        if name in PRESET_VARIABLE_NAMES:
            expected = {
                "type": "string", "value": value[name],
                "source": {"kind": "preset", "parameter": name},
            }
            if entry != expected:
                _fail("Variable preset does not match its explicit parameter")
            continue
        definition = definitions[name]
        if entry["type"] != definition["type"]:
            _fail("Variable snapshot type does not match its definition")
        source = entry["source"]
        if type(source) is not dict or type(source.get("kind")) is not str:
            _fail("Variable value source is invalid")
        source_kind = source["kind"]
        if source_kind not in {"default", "unassigned", "assignment"}:
            _fail("Unknown variable value source")
        if (
            type(source.get("workflow_id")) is not str
            or type(source.get("registry_revision")) is not int
        ):
            _fail("Variable value source reference types are invalid")
        expected_source = _definition_source(registry, source_kind)
        if source_kind == "assignment":
            _object(source, set(expected_source) | {"node_id"})
            _identifier(source["node_id"])
            expected_source["node_id"] = source["node_id"]
        if source != expected_source:
            _fail("Variable value source does not match the registry revision")
        if source_kind == "unassigned":
            if "value" in entry or "default" in definition:
                _fail("Unassigned variable record is inconsistent")
        else:
            if "value" not in entry:
                _fail("Assigned variable value is missing")
            _typed_value(definition["type"], entry["value"])
            if source_kind == "default" and (
                "default" not in definition
                or type(entry["value"]) is not type(definition["default"])
                or entry["value"] != definition["default"]
            ):
                _fail("Default variable value does not match its definition")
    return copy.deepcopy(value)


def assign_variable(
    snapshot: dict[str, Any], name: str, value: Any, *, node_id: str,
) -> dict[str, Any]:
    """Return a new session value; never modify a preset or an old snapshot."""
    try:
        result = validate_variable_snapshot(snapshot)
        _json(node_id)
        _identifier(node_id)
        _name(name)
        if name in PRESET_VARIABLE_NAMES:
            _fail("Preset variables are read-only")
        if name not in result["values"]:
            _fail("Assignment requires a registered variable")
        _typed_value(result["values"][name]["type"], value)
        source = _definition_source(result["registry"], "assignment")
        source["node_id"] = node_id
        result["values"][name] = {
            "type": result["values"][name]["type"],
            "value": value,
            "source": source,
        }
        return validate_variable_snapshot(result)
    except PromptProcessingError as exc:
        raise PromptProcessingError(
            exc.code, str(exc), node_id=node_id if type(node_id) is str else None,
        ) from None


def fork_variable_snapshot(
    source: dict[str, Any], *, workflow_session_id: str,
) -> dict[str, Any]:
    """Clone declared values; rebind only the explicitly supplied session preset."""
    result = validate_variable_snapshot(source)
    _json(workflow_session_id)
    _identifier(workflow_session_id)
    result["workflow_session_id"] = workflow_session_id
    result["values"]["workflow_session_id"]["value"] = workflow_session_id
    return validate_variable_snapshot(result)
