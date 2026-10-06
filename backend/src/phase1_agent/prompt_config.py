"""Immutable prompt definitions and exact-reference configuration expansion.

This boundary only resolves configuration. It does not interpolate text,
assemble messages, read live variables, or modify a workflow's frozen input.
Group members are direct item references in version 1; nested groups are not
accepted. Disabled references are still resolved and validated.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator, validators

from .contract_errors import ContractValidationError
from .contract_json import validate_json_value


_UUID = {
    "type": "string",
    "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    "minLength": 36,
    "maxLength": 36,
}
_REVISION = {"type": "integer", "minimum": 1}
_NAME = {"type": "string", "minLength": 1}
_DEPTH = {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]}
_IDENTITY_FIELDS = {"item": "item_id", "group": "group_id", "config": "config_id"}
_EFFECTIVE_FIELDS = (
    "name", "text", "role", "enabled", "placement", "depth", "order", "interpolation",
)


def _object(required: tuple[str, ...], **properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "required": list(required),
        "properties": properties,
        "additionalProperties": False,
    }


def _record(kind: str, **properties: Any) -> dict[str, Any]:
    return _object(
        ("schema_version", "kind", *properties),
        schema_version={"type": "integer", "const": 1},
        kind={"const": kind},
        **properties,
    )


_ITEM_PROPERTIES = {
    "name": _NAME,
    "text": {"type": "string"},
    "role": {"enum": ["system", "user", "assistant"]},
    "enabled": {"type": "boolean"},
    "placement": {"enum": ["before", "middle", "after"]},
    "depth": _DEPTH,
    "order": {"type": "integer"},
    "interpolation": {"enum": ["variables", "literal"]},
}
_OVERRIDES = _object((), **_ITEM_PROPERTIES)
_MEMBER = _object(
    ("item_instance_id", "item_id", "revision", "overrides"),
    item_instance_id=_UUID,
    item_id=_UUID,
    revision=_REVISION,
    overrides=_OVERRIDES,
)
_MEMBER_OVERRIDE = _object(
    ("item_instance_id", "overrides"),
    item_instance_id=_UUID,
    overrides=_OVERRIDES,
)
_ITEM_INPUT = _object(
    ("name", "kind", "item_instance_id", "item_id", "revision", "overrides"),
    name=_NAME,
    kind={"const": "item"},
    item_instance_id=_UUID,
    item_id=_UUID,
    revision=_REVISION,
    overrides=_OVERRIDES,
)
_GROUP_INPUT = _object(
    ("name", "kind", "group_instance_id", "group_id", "revision", "enabled", "member_overrides"),
    name=_NAME,
    kind={"const": "group"},
    group_instance_id=_UUID,
    group_id=_UUID,
    revision=_REVISION,
    enabled={"type": "boolean"},
    member_overrides={"type": "array", "items": _MEMBER_OVERRIDE},
)

PROMPT_CONFIG_SCHEMAS: dict[str, dict[str, Any]] = {
    "item": _record(
        "item",
        item_id=_UUID,
        revision=_REVISION,
        **_ITEM_PROPERTIES,
        source=_object(("kind",), kind={"const": "configuration"}),
    ),
    "group": _record(
        "group",
        group_id=_UUID,
        revision=_REVISION,
        name=_NAME,
        members={"type": "array", "items": _MEMBER},
    ),
    "config": _record(
        "config",
        config_id=_UUID,
        revision=_REVISION,
        name=_NAME,
        inputs={"type": "array", "items": {"oneOf": [_ITEM_INPUT, _GROUP_INPUT]}},
    ),
}
PROMPT_CONFIG_SCHEMAS["item"]["allOf"] = [{
    "if": {"properties": {"placement": {"const": "middle"}}},
    "then": {"properties": {"depth": {"type": "integer", "minimum": 0}}},
    "else": {"properties": {"depth": {"type": "null"}}},
}]

_STRICT_TYPES = Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", lambda _checker, value: type(value) is int,
)
_StrictValidator = validators.extend(Draft202012Validator, type_checker=_STRICT_TYPES)
_VALIDATORS = {
    kind: _StrictValidator(copy.deepcopy(schema))
    for kind, schema in PROMPT_CONFIG_SCHEMAS.items()
}
_V2_CONFIG_SCHEMA = copy.deepcopy(PROMPT_CONFIG_SCHEMAS["config"])
_V2_CONFIG_SCHEMA["properties"]["schema_version"] = {"type": "integer", "const": 2}
_V2_CONFIG_SCHEMA["properties"]["preparation"] = _object(
    ("schema_version", "kind", "nodes", "outputs"),
    schema_version={"type": "integer", "const": 1},
    kind={"const": "prompt_preparation_program"},
    nodes={
        "type": "array", "maxItems": 128,
        "items": _object(
            ("node_id", "kind", "inputs", "config"),
            node_id={"type": "string", "minLength": 1, "maxLength": 128},
            kind={"enum": [
                "text", "prompt-source", "context-source", "prompt-collector",
                "text-to-prompt", "prompt-to-text", "regex", "variable-register",
                "variable-assign", "variable-replace",
                "global-source", "session-data-read", "session-data-write", "json-to-text",
            ]},
            inputs={"type": "object", "additionalProperties": {"type": "string"}},
            config={"type": "object"},
            output_ports={"type": "array", "minItems": 1, "maxItems": 4, "uniqueItems": True,
                          "items": {"type": "string", "maxLength": 64}},
            public_outputs={"type": "array", "maxItems": 4, "uniqueItems": True,
                            "items": {"type": "string", "maxLength": 64}},
        ),
    },
    outputs=_object(
        ("prompt", "context"), prompt={"type": ["string", "null"]},
        context={"type": ["string", "null"]},
    ),
)
_V2_CONFIG_SCHEMA["required"].append("preparation")
PROMPT_CONFIG_V2_SCHEMA = _V2_CONFIG_SCHEMA
_V2_CONFIG_VALIDATOR = _StrictValidator(copy.deepcopy(_V2_CONFIG_SCHEMA))

PromptResolver = Callable[[str, int], Mapping[str, Any]]


def _error_path(path: Any) -> str:
    parts = []
    for key in path:
        if type(key) is int:
            parts.append(f"[{key}]")
        elif type(key) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            parts.append("." + key)
        else:
            parts.append("[field]")
    return "".join(parts)


def _require_unique(values: list[str], description: str) -> None:
    if len(values) != len(set(values)):
        raise ContractValidationError(f"Duplicate prompt {description}")


def validate_prompt_record(kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a version-1 definition/config and return detached JSON values.

    Reference existence is checked by expansion or the persistent catalog.
    Schema and same-scope identity checks apply to disabled entries as well.
    """
    if type(kind) is not str or kind not in _VALIDATORS:
        raise ContractValidationError("Unknown prompt record kind")
    validate_json_value(value)
    if type(value) is not dict:
        raise ContractValidationError("A prompt record must be a JSON object")
    validator = (_V2_CONFIG_VALIDATOR if kind == "config" and value.get("schema_version") == 2
                 else _VALIDATORS[kind])
    error = next(validator.iter_errors(value), None)
    if error is not None:
        raise ContractValidationError(
            f"prompt {kind}{_error_path(error.absolute_path)}: {error.validator}"
        ) from None
    if kind == "group":
        _require_unique(
            [member["item_instance_id"] for member in value["members"]],
            "group member instance",
        )
    elif kind == "config":
        if value["schema_version"] == 2:
            from .preparation_program import validate_preparation_program
            validate_preparation_program(value["preparation"])
        _require_unique([entry["name"] for entry in value["inputs"]], "input name")
        _require_unique(
            [
                entry["item_instance_id"] if entry["kind"] == "item" else entry["group_instance_id"]
                for entry in value["inputs"]
            ],
            "top-level instance",
        )
        for entry in value["inputs"]:
            if entry["kind"] == "group":
                _require_unique(
                    [override["item_instance_id"] for override in entry["member_overrides"]],
                    "member override target",
                )
    return copy.deepcopy(value)


def prompt_record_identity(kind: str, value: Mapping[str, Any]) -> tuple[str, int]:
    """Return the entity identity and immutable definition revision."""
    record = validate_prompt_record(kind, value)
    return record[_IDENTITY_FIELDS[kind]], record["revision"]


def expand_prompt_config(
    config: Mapping[str, Any],
    resolve_item: PromptResolver,
    resolve_group: PromptResolver,
) -> list[dict[str, Any]]:
    """Expand exact revisions in declared order into detached effective items.

    A group's member instance is scoped by its group instance. Repeated groups
    retain all members, even when their definition IDs or text are identical.
    Definition-member overrides apply before configuration-member overrides.
    Declaration indexes include disabled entries, so toggling enablement does
    not reassign the ordering identity of later entries.
    """
    config = validate_prompt_record("config", config)
    if not callable(resolve_item) or not callable(resolve_group):
        raise ContractValidationError("Prompt resolvers must be callable")
    item_cache: dict[tuple[str, int], dict[str, Any]] = {}
    group_cache: dict[tuple[str, int], dict[str, Any]] = {}
    effective: list[dict[str, Any]] = []
    declaration_index = 0

    def resolve(
        kind: str, entity_id: str, revision: int, resolver: PromptResolver,
        cache: dict[tuple[str, int], dict[str, Any]],
    ) -> dict[str, Any]:
        key = (entity_id, revision)
        if key not in cache:
            record = validate_prompt_record(kind, resolver(entity_id, revision))
            if record[_IDENTITY_FIELDS[kind]] != entity_id or record["revision"] != revision:
                raise ContractValidationError(f"Resolved prompt {kind} revision does not match its reference")
            cache[key] = record
        return cache[key]

    def append_item(
        entry: dict[str, Any], member: dict[str, Any],
        group: dict[str, Any] | None, group_override: dict[str, Any] | None,
    ) -> None:
        nonlocal declaration_index
        item = copy.deepcopy(resolve(
            "item", member["item_id"], member["revision"], resolve_item, item_cache,
        ))
        item.update(member["overrides"])
        if group_override is not None:
            item.update(group_override)
        item = validate_prompt_record("item", item)
        index = declaration_index
        declaration_index += 1
        if not item["enabled"] or (group is not None and not entry["enabled"]):
            return
        effective.append({
            "item_id": item["item_id"],
            "revision": item["revision"],
            "item_instance_id": member["item_instance_id"],
            "group_id": None if group is None else group["group_id"],
            "group_revision": None if group is None else group["revision"],
            "group_instance_id": None if group is None else entry["group_instance_id"],
            "input_name": entry["name"],
            "declaration_index": index,
            **{field: item[field] for field in _EFFECTIVE_FIELDS},
            "source": copy.deepcopy(item["source"]),
        })

    for entry in config["inputs"]:
        if entry["kind"] == "item":
            append_item(entry, entry, None, None)
            continue
        group = resolve("group", entry["group_id"], entry["revision"], resolve_group, group_cache)
        overrides = {
            override["item_instance_id"]: override["overrides"]
            for override in entry["member_overrides"]
        }
        targets = {member["item_instance_id"] for member in group["members"]}
        if not set(overrides).issubset(targets):
            raise ContractValidationError("Prompt member override target is not in the referenced group")
        for member in group["members"]:
            append_item(entry, member, group, overrides.get(member["item_instance_id"]))
    return effective
