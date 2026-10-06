"""Single-keyword Lorebook activation over an explicit detached context view.

Activation and placement are separate. This pure layer does not persist books,
read live history, dispatch a model or recursively scan activated prompt text.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, fields
import re
from typing import Any
import unicodedata

from .contract_errors import ContractValidationError
from .contract_json import content_digest, validate_json_value
from .prompt_config import PromptResolver, validate_prompt_record
from .prompt_errors import PromptProcessingError
from .prompt_values import context_view_messages, validate_context_view, validate_prompt_collection


LOREBOOK_MATCH_PROFILE = "unicode-casefold-substring-v1"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_SOURCE_ROLES = {"human": "user", "upstream_node": "user", "model": "assistant"}
_PROMPT_FIELDS = {
    "item_id", "revision", "name", "text", "role", "enabled", "placement",
    "depth", "order", "interpolation",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _object(value: Any, fields: set[str], description: str) -> None:
    _require(type(value) is dict and set(value) == fields, f"{description} fields must be exact")


def _uuid(value: Any) -> None:
    _require(
        type(value) is str and _UUID.fullmatch(value) is not None,
        "Lorebook identity must be a canonical UUID4",
    )


def _revision(value: Any) -> None:
    _require(type(value) is int and value >= 1, "Lorebook revision must be a positive integer")


def _name(value: Any) -> None:
    _require(type(value) is str and bool(value.strip()), "Lorebook name must be nonempty text")


@dataclass(frozen=True)
class LorebookLimits:
    max_entries: int = 1024
    max_scan_blocks: int = 1024
    max_scan_chars: int = 1_000_000
    max_keyword_chars: int = 4096
    max_output_chars: int = 1_000_000
    max_work_units: int = 64_000_000

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            _require(type(value) is int and value >= 0, "Lorebook limits must be nonnegative integers")


def validate_lorebook(value: dict[str, Any]) -> dict[str, Any]:
    """Validate ordered definitions; catalog existence is checked when activated."""
    validate_json_value(value)
    _object(value, {"schema_version", "kind", "book_id", "revision", "name", "entries"}, "Lorebook")
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "lorebook", "Unknown Lorebook version or kind",
    )
    _uuid(value["book_id"])
    _revision(value["revision"])
    _name(value["name"])
    _require(type(value["entries"]) is list, "Lorebook entries must be a JSON array")
    seen = set()
    for entry in value["entries"]:
        _object(entry, {"entry_id", "revision", "name", "enabled", "keyword", "prompt_ref"}, "Lorebook entry")
        _uuid(entry["entry_id"])
        _require(entry["entry_id"] not in seen, "Duplicate Lorebook entry")
        seen.add(entry["entry_id"])
        _revision(entry["revision"])
        _require(type(entry["name"]) is str, "Lorebook entry memo must be text")
        _require(type(entry["enabled"]) is bool, "Lorebook enablement must be boolean")
        _require(
            type(entry["keyword"]) is str and bool(entry["keyword"].strip()),
            "Lorebook keyword must be one nonempty literal string",
        )
        reference = entry["prompt_ref"]
        _object(reference, {"item_id", "revision"}, "Lorebook prompt reference")
        _uuid(reference["item_id"])
        _revision(reference["revision"])
    return copy.deepcopy(value)


def validate_lorebook_request(value: dict[str, Any]) -> dict[str, Any]:
    validate_json_value(value)
    _object(value, {
        "schema_version", "kind", "node_id", "book_instance_id",
        "input_name", "entry_instances", "scan",
    }, "Lorebook activation request")
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "lorebook_request", "Unknown Lorebook request version or kind",
    )
    _uuid(value["node_id"])
    _uuid(value["book_instance_id"])
    _name(value["input_name"])
    _require(type(value["entry_instances"]) is list, "Lorebook instances must be a JSON array")
    entries, instances = set(), set()
    for instance in value["entry_instances"]:
        _object(instance, {"entry_id", "item_instance_id"}, "Lorebook entry instance")
        _uuid(instance["entry_id"])
        _uuid(instance["item_instance_id"])
        _require(
            instance["entry_id"] not in entries and instance["item_instance_id"] not in instances,
            "Duplicate Lorebook entry reference or item instance",
        )
        entries.add(instance["entry_id"])
        instances.add(instance["item_instance_id"])
    scan = value["scan"]
    _object(scan, {"text_source", "sources"}, "Lorebook scan selection")
    _require(scan["text_source"] in ("original", "derived"), "Unknown Lorebook scan text source")
    sources = scan["sources"]
    _require(
        type(sources) is list and bool(sources)
        and all(type(source) is str and source in _SOURCE_ROLES for source in sources)
        and len(set(sources)) == len(sources), "Lorebook scan sources must be explicit and unique",
    )
    return copy.deepcopy(value)


def activate_lorebook(
    book: dict[str, Any], request: dict[str, Any], view: dict[str, Any],
    resolve_prompt: PromptResolver, *, limits: LorebookLimits = LorebookLimits(),
) -> dict[str, Any]:
    """Read one exact prompt per matched entry and return v2 typed provenance."""
    _require(type(limits) is LorebookLimits, "Lorebook activation requires explicit limits")
    book = validate_lorebook(book)
    request = validate_lorebook_request(request)
    view = validate_context_view(view)
    _require(view["purpose"] == "send", "Lorebook activation requires a send context view")
    _require(callable(resolve_prompt), "Lorebook prompt resolver must be callable")
    node_id = request["node_id"]

    def limit(condition: bool) -> None:
        if not condition:
            raise PromptProcessingError(
                "lorebook_resource_limit", "Lorebook activation exceeds its resource limits",
                node_id=node_id,
            )

    limit(len(book["entries"]) <= limits.max_entries)
    instances = {item["entry_id"]: item["item_instance_id"] for item in request["entry_instances"]}
    _require(
        set(instances) == {entry["entry_id"] for entry in book["entries"]},
        "Lorebook instance mapping must contain the complete entry set",
    )
    for entry in book["entries"]:
        limit(len(entry["keyword"]) <= limits.max_keyword_chars)
    messages = view["messages"] if request["scan"]["text_source"] == "original" else context_view_messages(view)
    scanned = []
    total_chars = 0
    for message in messages:
        source = message["source"]["kind"]
        if source not in request["scan"]["sources"] or message["role"] != _SOURCE_ROLES.get(source):
            continue
        if any(block["kind"] != "text" for block in message["blocks"]):
            continue
        for index, block in enumerate(message["blocks"]):
            limit(len(scanned) < limits.max_scan_blocks)
            limit(total_chars + len(block["text"]) <= limits.max_scan_chars)
            folded = block["text"].casefold()
            total_chars += len(folded)
            limit(total_chars <= limits.max_scan_chars)
            scanned.append({
                "message_id": message["message_id"], "block_index": index,
                "text_digest": content_digest(block["text"]), "folded": folded,
            })
    book_digest, view_digest = content_digest(book), content_digest(view)
    items, decisions = [], []
    cache = {}
    work_units, output_chars = 0, 0
    for index, entry in enumerate(book["entries"]):
        decision = {
            "entry_id": entry["entry_id"], "entry_revision": entry["revision"],
            "item_instance_id": instances[entry["entry_id"]], "status": "disabled", "match": None,
        }
        decisions.append(decision)
        if not entry["enabled"]:
            continue
        keyword = entry["keyword"].casefold()
        limit(len(keyword) <= limits.max_keyword_chars)
        decision["status"] = "not_matched"
        for candidate in scanned:
            work_units += len(candidate["folded"]) + len(keyword)
            limit(work_units <= limits.max_work_units)
            if keyword in candidate["folded"]:
                decision["match"] = {
                    key: candidate[key] for key in ("message_id", "block_index", "text_digest")
                }
                break
        if decision["match"] is None:
            continue
        reference = entry["prompt_ref"]
        key = reference["item_id"], reference["revision"]
        if key not in cache:
            prompt = validate_prompt_record("item", resolve_prompt(*key))
            _require(
                (prompt["item_id"], prompt["revision"]) == key,
                "Resolved Lorebook prompt differs from its exact reference",
            )
            cache[key] = prompt
        prompt = cache[key]
        if not prompt["enabled"]:
            decision["status"] = "prompt_disabled"
            continue
        output_chars += len(prompt["text"])
        limit(output_chars <= limits.max_output_chars)
        activation = {
            "algorithm": LOREBOOK_MATCH_PROFILE, "unicode_version": unicodedata.unidata_version,
            "node_id": node_id, "view_digest": view_digest,
            "text_source": request["scan"]["text_source"], **decision["match"],
            "keyword_digest": content_digest(entry["keyword"]),
        }
        items.append({
            "schema_version": 2, "kind": "prompt_item",
            **{field: prompt[field] for field in _PROMPT_FIELDS},
            "item_instance_id": instances[entry["entry_id"]], "group_id": None,
            "group_revision": None, "group_instance_id": None,
            "input_name": request["input_name"], "declaration_index": index,
            "source": {
                "kind": "lorebook", "book_id": book["book_id"], "book_revision": book["revision"],
                "book_instance_id": request["book_instance_id"], "entry_id": entry["entry_id"],
                "entry_revision": entry["revision"], "book_digest": book_digest, "activation": activation,
            },
        })
        decision["status"] = "activated"
    collection = validate_prompt_collection({
        "schema_version": 2, "kind": "prompt_collection", "items": items,
    })
    return {
        "schema_version": 1, "kind": "lorebook_activation_result", "collection": collection,
        "trace": {
            "request": request, "book_digest": book_digest, "view_digest": view_digest,
            "matching_profile": LOREBOOK_MATCH_PROFILE, "unicode_version": unicodedata.unidata_version,
            "limits": asdict(limits), "scan_blocks": len(scanned),
            "scan_chars": total_chars, "work_units": work_units, "decisions": decisions,
        },
    }
