"""Pure resource identities and frozen content/session-data contracts."""

from __future__ import annotations

import copy
import re
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, validate_json_value


MAX_RESOURCE_BYTES = 60 * 1024
MAX_SESSION_VALUE_BYTES = 256 * 1024
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}:[A-Za-z][A-Za-z0-9_.-]{0,63}")


def resource_error(reason: str, message: str, status: int = 400) -> ContractValidationError:
    error = ContractValidationError(message)
    error.reason_code, error.status_code = reason, status
    return error


def require(condition: bool, message: str = "Invalid resource request") -> None:
    if not condition:
        raise resource_error("invalid_request", message)


def resource_id(value: Any) -> str:
    try:
        parsed = UUID(value) if type(value) is str else None
    except ValueError:
        parsed = None
    require(parsed is not None and parsed.version == 4 and str(parsed) == value)
    return value


def resource_revision(value: Any, *, zero: bool = False) -> int:
    require(type(value) is int and (0 if zero else 1) <= value <= 2**53 - 1)
    return value


def workflow_identity(value: Any) -> str:
    if value in ("frontend:main-test", "frontend:empty-test"):
        return value
    return resource_id(value)


def validate_global_content(value: Any) -> dict:
    validate_json_value(value)
    require(type(value) is dict and set(value) == {
        "schema_version", "kind", "resource_id", "revision", "name", "enabled", "members",
    })
    require(value["schema_version"] == 1 and type(value["schema_version"]) is int
            and value["kind"] in ("global_prompt", "role_card"))
    resource_id(value["resource_id"])
    resource_revision(value["revision"])
    require(type(value["name"]) is str and 0 < len(value["name"].strip()) <= 128
            and type(value["enabled"]) is bool)
    require(type(value["members"]) is list and 1 <= len(value["members"]) <= 128)
    identities = set()
    for member in value["members"]:
        require(type(member) is dict and set(member) == {
            "id", "name", "text", "role", "placement", "depth", "order", "enabled",
        })
        resource_id(member["id"])
        require(member["id"] not in identities)
        identities.add(member["id"])
        require(type(member["name"]) is str and len(member["name"]) <= 128
                and type(member["text"]) is str
                and member["role"] in ("system", "user", "assistant")
                and member["placement"] in ("before", "middle", "after")
                and type(member["order"]) is int and abs(member["order"]) <= 2**53 - 1
                and type(member["enabled"]) is bool)
        require(type(member["depth"]) is int and member["depth"] >= 0
                if member["placement"] == "middle" else member["depth"] is None)
    require(len(canonical_bytes(value)) <= MAX_RESOURCE_BYTES)
    return copy.deepcopy(value)


def validate_data_definition(value: Any) -> dict:
    validate_json_value(value)
    require(type(value) is dict and set(value) in (
        {"schema_version", "definition_id", "revision", "key", "name", "schema", "writable", "public"},
        {"schema_version", "definition_id", "revision", "key", "name", "schema", "writable", "public", "default"},
    ))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    resource_id(value["definition_id"])
    resource_revision(value["revision"])
    require(type(value["key"]) is str and _KEY.fullmatch(value["key"]) is not None
            and type(value["name"]) is str and 0 < len(value["name"].strip()) <= 128
            and type(value["writable"]) is bool and type(value["public"]) is bool
            and type(value["schema"]) is dict)
    # Registered data is JSON, never a loader for schemas, code or credentials.
    def local_schema(entry: Any) -> None:
        if type(entry) is dict:
            require(not ({"$ref", "$dynamicRef", "$recursiveRef"} & set(entry)))
            for child in entry.values():
                local_schema(child)
        elif type(entry) is list:
            for child in entry:
                local_schema(child)
    local_schema(value["schema"])
    try:
        Draft202012Validator.check_schema(value["schema"])
    except SchemaError as exc:
        raise resource_error("invalid_request", "Session data schema is invalid") from exc
    if "default" in value:
        validate_data_value(value, value["default"])
    require(len(canonical_bytes(value)) <= MAX_RESOURCE_BYTES)
    return copy.deepcopy(value)


def validate_data_value(definition: dict, value: Any) -> None:
    validate_json_value(value)
    require(len(canonical_bytes(value)) <= MAX_SESSION_VALUE_BYTES)
    try:
        Draft202012Validator(definition["schema"]).validate(value)
    except ValidationError as exc:
        raise resource_error("session_data_type_mismatch", "Value differs from registered schema") from exc


def session_data_entry(definition: dict, value: Any = None, *, assigned: bool = False) -> dict:
    definition = validate_data_definition(definition)
    if assigned:
        validate_data_value(definition, value)
    return {
        "definition": definition,
        **({"value": copy.deepcopy(value)} if assigned else
           {"value": copy.deepcopy(definition["default"])} if "default" in definition else {}),
    }


def validate_session_data(value: Any) -> dict:
    require(type(value) is dict and len(value) <= 128)
    result = {}
    for key, entry in value.items():
        require(type(entry) is dict and set(entry) in ({"definition"}, {"definition", "value"}))
        definition = validate_data_definition(entry["definition"])
        require(key == definition["key"])
        if "value" in entry:
            validate_data_value(definition, entry["value"])
        result[key] = copy.deepcopy(entry)
    require(len(canonical_bytes(result)) <= 1_000_000)
    return result


CORE_SESSION_NOTE = {
    "schema_version": 1, "definition_id": "7be319b8-30bd-4674-b7bf-d1cf54a1a121",
    "revision": 1, "key": "core:session_note", "name": "会话文本",
    "schema": {"type": "string", "maxLength": 65536},
    "writable": True, "public": True, "default": "",
}
