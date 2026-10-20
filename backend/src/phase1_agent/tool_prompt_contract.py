"""Structured tool prompt produced by one loading node.

The loading node owns the business projection of prepared tool declarations:
tool name, description, parameter schema and the presentation of the two prompt
parts.  The context assembly consumes this exact value; it never re-reads the
tool preparation frame and never turns the parameters into an opaque string.
"""

from __future__ import annotations

from copy import deepcopy

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .content_contracts import object_schema, presentation_schema, validate_presentation
from .contract_errors import DiagnosticError
from .contract_json import canonical_bytes
from .graph_contracts import require
from .host_sdk import DataTypeDefinition
from .prompt_contract import check_prompt_schema
from .prompt_lifecycle import lifecycle_prompt_content, make_lifecycle_prompt_item


TOOL_PROMPT_TYPE = "TOOL_PROMPT"
TOOL_PROMPT_SCHEMA_VERSION = 1
TOOL_PROMPT_KIND = "workflow.tool-prompt"
MAX_TOOL_PROMPT_ENTRIES = 120
_TOOL_DESCRIPTION = "tool-description"
_TOOL_SCHEMA = "tool-schema"
_UUID = {"type": "string", "pattern":
         "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}


def tool_prompt_schema() -> dict:
    entry = object_schema({
        "binding_id": _UUID,
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "operation": {"enum": ["read", "set", "add", "subtract"]},
        "description": {"type": "string"},
        "parameters": {"type": "object"},
    })
    return object_schema({
        "schema_version": {"const": TOOL_PROMPT_SCHEMA_VERSION},
        "kind": {"const": TOOL_PROMPT_KIND},
        "agent_node_id": _UUID,
        "source": object_schema({
            "anchor_node_id": _UUID,
            "component_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "component_version": {"type": "string", "minLength": 1, "maxLength": 64},
        }),
        "list_presentation": presentation_schema(),
        "parameters_presentation": presentation_schema(),
        "entries": {"type": "array", "minItems": 1, "maxItems": MAX_TOOL_PROMPT_ENTRIES,
                    "items": entry},
    })


def validate_tool_prompt(value) -> dict:
    """Validate the exact paired structure, not two positionally related arrays."""

    check_prompt_schema(value, tool_prompt_schema(), "tool_prompt_invalid",
                        "Structured tool prompt fields are invalid")
    for part in ("list_presentation", "parameters_presentation"):
        validate_presentation(value[part])
    names, identities = set(), set()
    for entry in value["entries"]:
        identity = entry["binding_id"], entry["name"]
        require(identity not in identities and entry["name"] not in names,
                "tool_prompt_invalid",
                "Tool prompt requires unique tool names and binding identities")
        identities.add(identity)
        names.add(entry["name"])
        require(type(entry["parameters"]) is dict, "tool_prompt_invalid",
                "Tool prompt parameters must remain a structured object schema")
        try:
            Draft202012Validator.check_schema(entry["parameters"])
        except SchemaError as exc:
            raise DiagnosticError("tool_prompt_invalid",
                                  "Tool prompt parameters are not a valid JSON schema") from exc
    return deepcopy(value)


def tool_prompt_definition() -> DataTypeDefinition:
    return DataTypeDefinition(
        TOOL_PROMPT_TYPE, TOOL_PROMPT_SCHEMA_VERSION, tool_prompt_schema(), scope="content",
        max_bytes=4_000_000, validator=validate_tool_prompt)


def tool_prompt_materials(value, *, node_binding_id: str) -> dict:
    """Project one paired structure into two lifetime-aware prompt parts."""

    value = validate_tool_prompt(value)
    items = []
    for entry in value["entries"]:
        for part, purpose, presentation, text in (
            ("description", _TOOL_DESCRIPTION, value["list_presentation"], entry["description"]),
            ("parameters", _TOOL_SCHEMA, value["parameters_presentation"],
             canonical_bytes(entry["parameters"]).decode("utf-8")),
        ):
            items.append(make_lifecycle_prompt_item(
                node_binding_id,
                "tool-prompt:" + entry["binding_id"] + ":" + entry["name"] + ":" + part,
                text, presentation, purpose=purpose,
                source={"kind": "bound_tool", "agent_node_id": value["agent_node_id"],
                        "tool_name": entry["name"], "binding_id": entry["binding_id"],
                        "part": part},
                metadata={"tool_name": entry["name"], "binding_id": entry["binding_id"],
                          "agent_node_id": value["agent_node_id"],
                          "operation": entry["operation"]},
            ))
    return lifecycle_prompt_content(items)
