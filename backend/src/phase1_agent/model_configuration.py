"""Configuration-only Chat providers and typed model dependency graphs."""

from __future__ import annotations

import copy
import math
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_json import validate_json_value
from .frozen_model import FrozenModelParameters
from .gemini_capabilities import validate_provider_parameters


DEFAULT_PROVIDER_ID = "7be319b8-30bd-4674-b7bf-d1cf54a1a110"
MODEL_BINDINGS = frozenset((
    "7be319b8-30bd-4674-b7bf-d1cf54a1a108",
    "7be319b8-30bd-4674-b7bf-d1cf54a1a109",
))
MODEL_WORKFLOW_ID = "frontend:main-test"
MAX_REVISION = 2**53 - 1
CHAT_MAX_TOKENS = 8192


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def chat_parameter_diagnostic(parameters: dict[str, Any], protocol="chat") -> str | None:
    """Keep stored configurations readable while diagnosing the current adapter."""
    try:
        model = FrozenModelParameters.from_mapping(parameters)
        validate_provider_parameters(protocol, dict(model.as_mapping()))
    except ContractValidationError:
        return "model_parameters_unsupported"
    if model.max_tokens is not None and model.max_tokens > CHAT_MAX_TOKENS:
        return "model_parameters_unsupported"
    return None


def configuration_error(reason: str, status: int = 400) -> ContractValidationError:
    error = ContractValidationError("Model configuration request is invalid")
    error.reason_code, error.status_code = reason, status
    return error


def require(condition: bool) -> None:
    if not condition:
        raise configuration_error("invalid_request")


def identifier(value: Any) -> None:
    require(type(value) is str)
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise configuration_error("invalid_request") from None
    require(parsed.version == 4 and str(parsed) == value)


def revision(value: Any, *, zero: bool = False) -> None:
    require(type(value) is int and (0 if zero else 1) <= value <= MAX_REVISION)


def exact(value: Any, fields: set[str]) -> None:
    require(type(value) is dict and set(value) == fields)


def default_provider() -> dict[str, Any]:
    return {
        "schema_version": 1, "kind": "chat_provider",
        "provider_id": DEFAULT_PROVIDER_ID, "revision": 1,
        "name": "DeepSeek", "protocol": "chat",
        "base_url": "https://api.deepseek.com",
        "credential_ref": "env:DEEPSEEK_API_KEY", "enabled": True,
    }


def validate_provider(value: Any) -> dict[str, Any]:
    exact(value, {
        "schema_version", "kind", "provider_id", "revision", "name",
        "protocol", "base_url", "credential_ref", "enabled",
    })
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "chat_provider")
    identifier(value["provider_id"])
    revision(value["revision"])
    require(type(value["name"]) is str and bool(value["name"].strip()) and len(value["name"]) <= 128
            and not _has_control_characters(value["name"]))
    require(value["protocol"] in ("chat", "gemini"))
    address = value["base_url"]
    require(type(address) is str and 0 < len(address) <= 2048
            and address.lower().startswith(("http://", "https://"))
            and not any(character in address for character in "\\?#") and not _has_control_characters(address)
            and not any(character.isspace() for character in address))
    try:
        parts = urlsplit(address)
        port = parts.port
    except ValueError:
        raise configuration_error("invalid_request") from None
    require(parts.scheme in ("http", "https") and bool(parts.hostname)
            and parts.username is None and parts.password is None and not parts.query
            and not parts.fragment and (port is None or 1 <= port <= 65535))
    require(parts.scheme == "https" or parts.hostname in ("127.0.0.1", "localhost", "::1"))
    credential = "env:GEMINI_API_KEY" if value["protocol"] == "gemini" else "env:DEEPSEEK_API_KEY"
    require(value["credential_ref"] in (None, credential))
    require(type(value["enabled"]) is bool)
    validate_json_value(value)
    return copy.deepcopy(value)


def validate_model_configuration(value: Any) -> dict[str, Any]:
    exact(value, {
        "schema_version", "kind", "config_id", "revision", "workflow_id", "nodes", "edges",
    })
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow_model_configuration")
    from .resource_contracts import workflow_identity
    workflow_identity(value["workflow_id"])
    identifier(value["config_id"])
    revision(value["revision"])
    nodes, edges = value["nodes"], value["edges"]
    require(type(nodes) is list and len(nodes) <= 16 and type(edges) is list and len(edges) <= 2)
    node_ids, edge_ids, targets = set(), set(), set()
    for node in nodes:
        exact(node, {"id", "position", "provider_ref", "parameters"})
        identifier(node["id"])
        require(node["id"] not in node_ids)
        node_ids.add(node["id"])
        exact(node["position"], {"x", "y"})
        require(all(type(number) in (int, float) and math.isfinite(number)
                    for number in node["position"].values()))
        reference = node["provider_ref"]
        if reference is not None:
            exact(reference, {"provider_id", "revision"})
            identifier(reference["provider_id"])
            revision(reference["revision"])
        parameters = node["parameters"]
        require(type(parameters) is dict and set(parameters) <= {
            "model", "max_tokens", "temperature", "thinking", "stream"}
                and "model" in parameters and type(parameters["model"]) is str
                and len(parameters["model"]) <= 256 and not _has_control_characters(parameters["model"]))
        try:
            FrozenModelParameters.from_mapping(parameters)
        except ContractValidationError:
            raise configuration_error("invalid_request") from None
        if "max_tokens" in parameters:
            require(parameters["max_tokens"] <= MAX_REVISION)
    for edge in edges:
        exact(edge, {"id", "source", "target_binding_id"})
        identifier(edge["id"])
        require(type(edge["source"]) is str and type(edge["target_binding_id"]) is str
                and edge["id"] not in edge_ids and edge["source"] in node_ids
                and edge["target_binding_id"] in MODEL_BINDINGS
                and edge["target_binding_id"] not in targets)
        edge_ids.add(edge["id"])
        targets.add(edge["target_binding_id"])
    validate_json_value(value)
    return copy.deepcopy(value)


def validate_configuration(kind: str, value: Any) -> dict[str, Any]:
    require(kind in ("provider", "model"))
    return validate_provider(value) if kind == "provider" else validate_model_configuration(value)


def configuration_identity(kind: str, value: dict[str, Any]) -> str:
    return value["provider_id" if kind == "provider" else "config_id"]
