"""One scalar-variable value contract consumed through explicit object access."""

from __future__ import annotations

from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, validate_json_value
from .host_sdk import DataTypeDefinition, ensure


VARIABLE_TYPE = "workflow.variable"
VARIABLE_SCHEMA_VERSION = 1
VARIABLE_TYPES = ("string", "integer", "number", "boolean")
VARIABLE_MAX_BYTES = 4_000_000


def empty_variable_value() -> dict:
    return {"registered": False, "name": None, "value_type": None,
            "assigned": False, "value": None}


def validate_variable_name(value: Any) -> None:
    ensure(type(value) is str and 0 < len(value) <= 128
           and (value[0].isalpha() or value[0] == "_")
           and all(character.isalnum() or character == "_" for character in value),
           "variable_invalid_name", "Variable name must be a Unicode identifier")


def validate_typed_value(value_type: str, value: Any) -> None:
    accepted = {"string": type(value) is str, "integer": type(value) is int,
                "number": type(value) in (int, float), "boolean": type(value) is bool}
    ensure(value_type in VARIABLE_TYPES and accepted[value_type],
           "variable_type_mismatch", "Variable value differs from its registered scalar type")
    if value_type == "integer":
        ensure(-(2**53 - 1) <= value <= 2**53 - 1,
               "variable_type_mismatch", "Integer exceeds the interoperable safe range")
    try:
        validate_json_value(value)
    except ContractValidationError as exc:
        ensure(False, "variable_type_mismatch", str(exc))


def validate_variable(value: Any) -> None:
    empty = empty_variable_value()
    ensure(type(value) is dict and set(value) == set(empty)
           and type(value["registered"]) is bool and type(value["assigned"]) is bool,
           "variable_invalid_state", "Variable object fields are invalid")
    if not value["registered"]:
        ensure(value == empty, "variable_invalid_state",
               "An unregistered variable must remain an empty object shell")
        return
    validate_variable_name(value["name"])
    ensure(value["value_type"] in VARIABLE_TYPES, "variable_invalid_state",
           "Registered variable type is unavailable")
    if value["assigned"]:
        validate_typed_value(value["value_type"], value["value"])
    else:
        ensure(value["value"] is None, "variable_invalid_state",
               "An unassigned variable cannot hold a value")


def scalar_text(value_type: str, value: Any) -> str:
    validate_typed_value(value_type, value)
    return value if value_type == "string" else canonical_bytes(value).decode("utf-8")


def read_variable(context, object_key: str) -> dict:
    """Read the exact authorized item; its data boundary determines ownership."""
    item = context.object_read(object_key)
    ensure(item["type_id"] == VARIABLE_TYPE and item["schema_version"] == VARIABLE_SCHEMA_VERSION,
           "variable_binding_type_mismatch", "Variable reads require a workflow.variable@1 object")
    validate_variable(item["value"])
    return item


def variable_data_type() -> DataTypeDefinition:
    fields = {
        "registered": {"type": "boolean"}, "name": {"type": ["string", "null"]},
        "value_type": {"enum": [None, *VARIABLE_TYPES]}, "assigned": {"type": "boolean"},
        "value": {"type": ["string", "integer", "number", "boolean", "null"]},
    }
    return DataTypeDefinition(
        VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION,
        {"type": "object", "properties": fields, "required": list(fields),
         "additionalProperties": False},
        default_value=empty_variable_value(), validator=validate_variable,
        max_bytes=VARIABLE_MAX_BYTES, checkpoint_admission=True,
    )
