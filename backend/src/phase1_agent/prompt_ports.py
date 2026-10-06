"""Non-destructive type diagnostics for the future adaptive regex editor."""

from __future__ import annotations

import copy
import re
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import validate_json_value
from .prompt_errors import PromptProcessingError


REGEX_PORT_TYPES = ("text", "prompt_item", "prompt_collection", "context_view")
_PORT_VERSIONS = {
    "text": (1,), "prompt_item": (1, 2), "prompt_collection": (1, 2), "context_view": (1,),
}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def diagnose_regex_ports(
    node_id: str, mode: str | None, connections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check all port constraints without mutating a mode or any connection.

    Both directions participate equally. An unspecified mode is resolved only
    if every connected type agrees; conflicting drafts remain unresolved.
    """
    validate_json_value(connections)
    if type(node_id) is not str or _UUID.fullmatch(node_id) is None:
        raise ContractValidationError("Regex node identity must be a canonical UUID4")
    if mode is not None and (type(mode) is not str or mode not in REGEX_PORT_TYPES):
        raise ContractValidationError("Unknown explicit regex mode")
    if type(connections) is not list:
        raise ContractValidationError("Regex connections must be a JSON array")
    seen: set[str] = set()
    supported: set[tuple[str, int]] = set()
    for connection in connections:
        if type(connection) is not dict or set(connection) != {
            "connection_id", "direction", "port_id", "schema_ref",
        }:
            raise ContractValidationError("Regex connection fields must be exact")
        identity = connection["connection_id"]
        if type(identity) is not str or _UUID.fullmatch(identity) is None or identity in seen:
            raise ContractValidationError("Regex connection identity is invalid or repeated")
        seen.add(identity)
        if connection["direction"] not in ("input", "output"):
            raise ContractValidationError("Unknown regex port direction")
        if type(connection["port_id"]) is not str or not connection["port_id"]:
            raise ContractValidationError("Regex port identity must be nonempty text")
        schema = connection["schema_ref"]
        if (
            type(schema) is not dict or set(schema) != {"schema_id", "version"}
            or type(schema["schema_id"]) is not str or not schema["schema_id"]
            or type(schema["version"]) is not int or schema["version"] < 1
        ):
            raise ContractValidationError("Regex connection schema reference is invalid")
        if schema["version"] in _PORT_VERSIONS.get(schema["schema_id"], ()):
            supported.add((schema["schema_id"], schema["version"]))
    all_supported = all(
        item["schema_ref"]["version"] in _PORT_VERSIONS.get(item["schema_ref"]["schema_id"], ())
        for item in connections
    )
    resolved = mode
    kinds = {kind for kind, _ in supported}
    if mode is None and all_supported and len(kinds) == 1:
        resolved = next(iter(kinds))
    versions = {version for kind, version in supported if kind == resolved}
    schema_ref = None
    if resolved is not None and len(versions) <= 1:
        schema_ref = {"schema_id": resolved, "version": next(iter(versions), 1)}
    diagnostics = []
    for connection in connections:
        schema = connection["schema_ref"]
        if schema["version"] not in _PORT_VERSIONS.get(schema["schema_id"], ()):
            code = "regex_port_unsupported_type"
        elif resolved is None:
            code = "regex_port_type_conflict"
        elif schema["schema_id"] != resolved:
            code = "regex_port_type_mismatch"
        elif schema_ref is None:
            code = "regex_port_version_conflict"
        else:
            continue
        diagnostics.append({
            "code": code, "node_id": node_id,
            "connection_id": connection["connection_id"],
            "port_id": connection["port_id"], "direction": connection["direction"],
            "expected": copy.deepcopy(schema_ref),
            "actual": copy.deepcopy(schema),
        })
    if not connections and mode is None:
        diagnostics.append({
            "code": "regex_port_type_unresolved", "node_id": node_id,
            "connection_id": None, "port_id": None, "direction": None,
            "expected": None, "actual": None,
        })
    return {
        "schema_version": 1, "kind": "regex_port_diagnostics", "node_id": node_id,
        "mode": mode, "resolved_mode": resolved,
        "resolved_schema_ref": schema_ref,
        "executable": schema_ref is not None and not diagnostics,
        "diagnostics": diagnostics,
    }


def require_regex_port_mode(
    node_id: str, mode: str | None, connections: list[dict[str, Any]],
) -> str:
    """Refuse execution until the preserved draft has a consistent concrete type."""
    return require_regex_port_schema(node_id, mode, connections)["schema_id"]


def require_regex_port_schema(
    node_id: str, mode: str | None, connections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Require exact type and version; no automatic schema upgrade/downgrade."""
    result = diagnose_regex_ports(node_id, mode, connections)
    if not result["executable"]:
        raise PromptProcessingError(
            "regex_ports_invalid", "Regex port constraints must be resolved before execution",
            node_id=node_id,
        )
    return result["resolved_schema_ref"]
