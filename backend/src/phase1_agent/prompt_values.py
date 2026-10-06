"""Detached typed values for pure prompt preparation and context projections.

Collections preserve configuration-instance identity. Context views retain the
original closed message sequence and apply text overrides only to a new copy.
Neither boundary reads live state, dispatches a model, or changes saved facts.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history
from .contract_json import validate_json_value
from .contracts_v2 import validate_record
from .prompt_config import PromptResolver, expand_prompt_config, validate_prompt_record
from .prompt_errors import PromptProcessingError


_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_ITEM_DEFINITION_FIELDS = {
    "item_id", "revision", "name", "text", "role", "enabled", "placement",
    "depth", "order", "interpolation", "source",
}
_ITEM_FIELDS = _ITEM_DEFINITION_FIELDS | {
    "item_instance_id", "group_id", "group_revision", "group_instance_id",
    "input_name", "declaration_index",
}
_COLLECTION_FIELDS = {"schema_version", "kind", "items"}
_VIEW_FIELDS = {
    "schema_version", "kind", "workflow_session_id", "node_binding_id",
    "parent_turn_id", "projection_version", "purpose", "messages", "overrides",
    "protected_blocks",
}
_OVERRIDE_FIELDS = {"message_id", "block_index", "text"}
_LOCATOR_FIELDS = {"message_id", "block_index"}
_LOREBOOK_SOURCE_FIELDS = {
    "kind", "book_id", "book_revision", "book_instance_id", "entry_id",
    "entry_revision", "book_digest", "activation",
}
_ACTIVATION_FIELDS = {
    "algorithm", "unicode_version", "node_id", "view_digest", "text_source",
    "message_id", "block_index", "text_digest", "keyword_digest",
}
_DIGEST = re.compile(r"json-v1:sha256:[0-9a-f]{64}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _check_fields(value: Any, fields: set[str], description: str) -> None:
    _require(type(value) is dict, f"{description} must be a JSON object")
    _require(set(value) == fields, f"{description} has missing or unknown fields")


def _check_uuid(value: Any, description: str, *, nullable: bool = False) -> None:
    _require(
        (nullable and value is None)
        or (type(value) is str and _UUID_PATTERN.fullmatch(value) is not None),
        f"{description} must be a canonical UUID4",
    )


def _check_nonnegative_int(value: Any, description: str) -> None:
    _require(type(value) is int and value >= 0, f"{description} must be a nonnegative integer")


def _item_identity(item: dict[str, Any]) -> tuple[str | None, str]:
    return item["group_instance_id"], item["item_instance_id"]


def validate_lorebook_source(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate explicit v2 provenance, without claiming to recheck activation."""
    validate_json_value(value)
    _check_fields(value, _LOREBOOK_SOURCE_FIELDS, "Lorebook prompt source")
    _require(value["kind"] == "lorebook", "Unknown Lorebook prompt source")
    for field in ("book_id", "book_instance_id", "entry_id"):
        _check_uuid(value[field], "Lorebook source identity")
    for field in ("book_revision", "entry_revision"):
        _require(
            type(value[field]) is int and value[field] >= 1,
            "Lorebook source revision must be a positive integer",
        )
    activation = value["activation"]
    _check_fields(activation, _ACTIVATION_FIELDS, "Lorebook activation evidence")
    _require(
        activation["algorithm"] == "unicode-casefold-substring-v1",
        "Unknown Lorebook activation algorithm",
    )
    _require(
        type(activation["unicode_version"]) is str
        and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", activation["unicode_version"]) is not None,
        "Lorebook Unicode data version is invalid",
    )
    _check_uuid(activation["node_id"], "Lorebook activation node")
    _check_uuid(activation["message_id"], "Lorebook activation message")
    _check_nonnegative_int(activation["block_index"], "Lorebook activation block")
    _require(
        activation["text_source"] in ("original", "derived"), "Unknown Lorebook text source",
    )
    for digest in (
        value["book_digest"], activation["view_digest"], activation["text_digest"],
        activation["keyword_digest"],
    ):
        _require(
            type(digest) is str and _DIGEST.fullmatch(digest) is not None,
            "Lorebook evidence digest is invalid",
        )
    return copy.deepcopy(value)


def validate_prompt_item(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate legacy effective items or explicitly versioned Lorebook items."""
    validate_json_value(value)
    _require(type(value) is dict, "Prompt item must be a JSON object")
    versioned = "schema_version" in value
    _check_fields(
        value, _ITEM_FIELDS | ({"schema_version", "kind"} if versioned else set()), "Prompt item",
    )
    if versioned:
        _require(
            type(value["schema_version"]) is int and value["schema_version"] == 2
            and value["kind"] == "prompt_item", "Unknown typed prompt item version or kind",
        )
        validate_lorebook_source(value["source"])
    validate_prompt_record("item", {
        "schema_version": 1, "kind": "item",
        **{field: value[field] for field in _ITEM_DEFINITION_FIELDS - {"source"}},
        "source": {"kind": "configuration"} if versioned else value["source"],
    })
    _require(value["enabled"] is True, "An effective prompt item must be enabled")
    _check_uuid(value["item_instance_id"], "Prompt item instance")
    group_fields = ("group_id", "group_revision", "group_instance_id")
    if any(value[field] is not None for field in group_fields):
        _require(not versioned, "A Lorebook is not a configuration group")
        _check_uuid(value["group_id"], "Prompt group definition")
        _check_uuid(value["group_instance_id"], "Prompt group instance")
        _require(
            type(value["group_revision"]) is int and value["group_revision"] >= 1,
            "Prompt group revision must be a positive integer",
        )
    _require(
        type(value["input_name"]) is str and bool(value["input_name"]),
        "Prompt input name must be nonempty text",
    )
    _check_nonnegative_int(value["declaration_index"], "Prompt declaration index")
    return copy.deepcopy(value)


def validate_prompt_collection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate v1/v2 collections without admitting new sources through v1."""
    validate_json_value(value)
    _check_fields(value, _COLLECTION_FIELDS, "Prompt collection")
    _require(
        type(value["schema_version"]) is int and value["schema_version"] in (1, 2)
        and value["kind"] == "prompt_collection",
        "Unknown prompt collection version or kind",
    )
    _require(type(value["items"]) is list, "Prompt collection items must be a JSON array")
    items = [validate_prompt_item(item) for item in value["items"]]
    _require(
        value["schema_version"] == 2 or all("schema_version" not in item for item in items),
        "Prompt collection v1 cannot carry versioned Lorebook items",
    )
    identities = [_item_identity(item) for item in items]
    _require(len(set(identities)) == len(identities), "Duplicate scoped prompt item instance")
    return {"schema_version": value["schema_version"], "kind": "prompt_collection", "items": items}


def collection_from_config(
    config: Mapping[str, Any], resolve_item: PromptResolver, resolve_group: PromptResolver,
) -> dict[str, Any]:
    """Wrap exact-reference configuration expansion in the typed collection."""
    return validate_prompt_collection({
        "schema_version": 1, "kind": "prompt_collection",
        "items": expand_prompt_config(config, resolve_item, resolve_group),
    })


def collect_prompt_inputs(
    input_order: list[str], inputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Merge a complete named input set in declaration order, never arrival order."""
    validate_json_value(input_order)
    validate_json_value(inputs)
    _require(type(input_order) is list, "Prompt input order must be a JSON array")
    _require(
        all(type(name) is str and bool(name) for name in input_order),
        "Prompt input names must be nonempty text",
    )
    _require(len(set(input_order)) == len(input_order), "Duplicate prompt input name")
    _require(type(inputs) is dict, "Prompt inputs must be a JSON object")
    _require(set(inputs) == set(input_order), "Prompt inputs have missing or unknown names")
    items: list[dict[str, Any]] = []
    version = 1
    for name in input_order:
        value = inputs[name]
        _require(type(value) is dict, "Prompt input must be an item or collection")
        if value.get("kind") == "prompt_collection":
            collection = validate_prompt_collection(value)
            version = max(version, collection["schema_version"])
            items.extend(collection["items"])
        else:
            item = validate_prompt_item(value)
            version = max(version, item.get("schema_version", 1))
            items.append(item)
    return validate_prompt_collection({
        "schema_version": version, "kind": "prompt_collection", "items": items,
    })


def render_prompt_text(
    collection: Mapping[str, Any], *, separator: str, max_output_chars: int,
) -> str:
    """Explicitly discard item metadata and join text under a character limit."""
    collection = validate_prompt_collection(collection)
    validate_json_value(separator)
    _require(type(separator) is str, "Prompt text separator must be text")
    _check_nonnegative_int(max_output_chars, "Prompt output character limit")
    items = collection["items"]
    length = sum(len(item["text"]) for item in items)
    length += max(0, len(items) - 1) * len(separator)
    if length > max_output_chars:
        raise PromptProcessingError(
            "prompt_output_limit", "Prompt text rendering exceeds the output character limit",
        )
    return separator.join(item["text"] for item in items)


def make_context_view(
    messages: list[dict[str, Any]], *, workflow_session_id: str,
    node_binding_id: str, parent_turn_id: str | None, projection_version: int,
    purpose: str, protected_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Capture a closed canonical sequence without inventing text overrides."""
    return validate_context_view({
        "schema_version": 1, "kind": "context_view",
        "workflow_session_id": workflow_session_id, "node_binding_id": node_binding_id,
        "parent_turn_id": parent_turn_id, "projection_version": projection_version,
        "purpose": purpose, "messages": messages, "overrides": [],
        "protected_blocks": protected_blocks,
    })


def _editable_text(message: dict[str, Any], block: dict[str, Any]) -> bool:
    if block["kind"] != "text" or any(item["kind"] != "text" for item in message["blocks"]):
        return False
    if message["role"] == "user":
        return message["source"]["kind"] in ("human", "upstream_node")
    return message["role"] == "assistant" and message["source"]["kind"] == "model"


def validate_context_view(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate original messages and locatable, source-restricted text edits.

    The projection owner supplies protected locators using its Turn final
    references. This layer protects those blocks without guessing from text.
    """
    validate_json_value(value)
    _check_fields(value, _VIEW_FIELDS, "Context view")
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "context_view",
        "Unknown context view version or kind",
    )
    _check_uuid(value["workflow_session_id"], "Context workflow session")
    _check_uuid(value["node_binding_id"], "Context node binding")
    _check_uuid(value["parent_turn_id"], "Context parent turn", nullable=True)
    _require(
        type(value["projection_version"]) is int and value["projection_version"] >= 1,
        "Context projection version must be a positive integer",
    )
    _require(value["purpose"] in ("display", "send"), "Unknown context view purpose")
    _require(type(value["messages"]) is list, "Context messages must be a JSON array")
    messages = [validate_record("agent_message", message) for message in value["messages"]]
    validate_message_history(messages)
    _require(type(value["overrides"]) is list, "Context overrides must be a JSON array")
    _require(
        type(value["protected_blocks"]) is list,
        "Protected context blocks must be a JSON array",
    )
    by_id = {message["message_id"]: message for message in messages}
    protected: set[tuple[str, int]] = set()
    for locator in value["protected_blocks"]:
        _check_fields(locator, _LOCATOR_FIELDS, "Protected context block")
        _check_uuid(locator["message_id"], "Protected context message")
        _check_nonnegative_int(locator["block_index"], "Protected context block index")
        key = locator["message_id"], locator["block_index"]
        _require(key not in protected, "Duplicate protected context block")
        protected.add(key)
        message = by_id.get(locator["message_id"])
        _require(message is not None, "Protected context message does not exist")
        _require(
            locator["block_index"] < len(message["blocks"]),
            "Protected context block does not exist",
        )
        _require(
            message["blocks"][locator["block_index"]]["kind"] == "text",
            "Protected context locator must target a text block",
        )
    seen: set[tuple[str, int]] = set()
    for override in value["overrides"]:
        _check_fields(override, _OVERRIDE_FIELDS, "Context text override")
        _check_uuid(override["message_id"], "Context override message")
        _check_nonnegative_int(override["block_index"], "Context override block index")
        _require(type(override["text"]) is str, "Context override text must be text")
        key = override["message_id"], override["block_index"]
        _require(key not in seen, "Duplicate context text override target")
        _require(key not in protected, "Context text override targets protected content")
        seen.add(key)
        message = by_id.get(override["message_id"])
        _require(message is not None, "Context text override message does not exist")
        _require(
            override["block_index"] < len(message["blocks"]),
            "Context text override block does not exist",
        )
        _require(
            _editable_text(message, message["blocks"][override["block_index"]]),
            "Context text override targets protected content",
        )
    return copy.deepcopy(value)


def context_view_messages(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Materialize overrides into a detached derived sequence only."""
    view = validate_context_view(view)
    messages = view["messages"]
    by_id = {message["message_id"]: message for message in messages}
    for override in view["overrides"]:
        by_id[override["message_id"]]["blocks"][override["block_index"]]["text"] = override["text"]
    return messages
