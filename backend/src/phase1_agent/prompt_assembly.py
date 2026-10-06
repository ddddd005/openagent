"""Pure, versioned prompt placement with detached message/source evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history
from .contract_json import canonical_bytes, validate_json_value
from .contracts_v2 import validate_record
from .prompt_errors import PromptProcessingError
from .prompt_values import validate_prompt_collection


@dataclass(frozen=True)
class PromptAssemblyLimits:
    max_messages: int = 4096
    max_total_chars: int = 4_000_000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (self.max_messages, self.max_total_chars)
        ):
            raise ContractValidationError("Prompt assembly limits must be nonnegative integers")

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


_ASSEMBLY_FIELDS = {"schema_version", "kind", "messages", "manifest"}
_MANIFEST_FIELDS = {
    "schema_version", "kind", "collection", "logical_floors", "input_message_ids",
    "prompt_message_ids", "entries", "limits", "usage",
}
_ENTRY_FIELDS = {"collection_index", "message_id", "message_index", "region", "floor_boundary"}
_DELTA_SOURCES = {"model", "tool", "protocol_feedback", "runtime_tool_observation"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _fields(value: Any, expected: set[str], description: str) -> None:
    _require(type(value) is dict, f"{description} must be a JSON object")
    _require(set(value) == expected, f"{description} has missing or unknown fields")


def _message_chars(message: dict[str, Any]) -> int:
    """Count semantic payload characters, including both tool JSON projections."""
    total = 0
    for block in message["blocks"]:
        if block["kind"] == "text":
            total += len(block["text"])
        elif block["kind"] == "tool_call":
            total += len(block["tool_name"]) + len(block["tool_definition_version"])
            total += len(block["raw_arguments"])
            total += len(canonical_bytes(block["parsed_arguments"]).decode("utf-8"))
        else:
            total += len(block["model_visible_text"])
            total += len(canonical_bytes(block["content"]).decode("utf-8"))
    return total


def message_content_chars(messages: list[dict[str, Any]]) -> int:
    """Count closed message payloads, excluding provenance and tool definitions.

    Raw and parsed arguments are counted separately, as are structured tool
    content and its model-visible text. Workflow preparation adds the serialized
    frozen tool definitions to this conservative, character-based count.
    """
    validate_json_value(messages)
    _require(type(messages) is list, "Prompt capacity messages must be a JSON array")
    messages = [validate_record("agent_message", message) for message in messages]
    validate_message_history(messages)
    return sum(_message_chars(message) for message in messages)


def _usage(messages: list[dict[str, Any]], limits: PromptAssemblyLimits) -> dict[str, int]:
    _require(type(limits) is PromptAssemblyLimits, "Prompt assembly requires explicit limits")
    usage = {"messages": len(messages), "total_chars": sum(_message_chars(m) for m in messages)}
    if usage["messages"] > limits.max_messages or usage["total_chars"] > limits.max_total_chars:
        raise PromptProcessingError(
            "prompt_assembly_limit", "Prompt assembly exceeds its message or total character limit",
        )
    return usage


def _validate_floors(
    messages: list[dict[str, Any]], logical_floors: list[list[str]],
) -> list[int]:
    validate_json_value(logical_floors)
    _require(
        type(logical_floors) is list and bool(logical_floors)
        and len(logical_floors) % 2 == 1,
        "Logical floors must be root/delta pairs followed by the current root",
    )
    _require(
        all(type(floor) is list and bool(floor) for floor in logical_floors),
        "Every logical floor must be a nonempty JSON array",
    )
    flattened = [identity for floor in logical_floors for identity in floor]
    _require(
        flattened == [message["message_id"] for message in messages],
        "Logical floors must partition the complete message sequence in order",
    )
    offsets = [0]
    for index, floor in enumerate(logical_floors):
        start = offsets[-1]
        sequence = messages[start:start + len(floor)]
        if index % 2 == 0:
            _require(
                len(sequence) == 1 and sequence[0]["role"] == "user"
                and sequence[0]["source"]["kind"] in ("human", "upstream_node"),
                "A root logical floor must contain one human or upstream user message",
            )
        else:
            _require(
                all(message["source"]["kind"] in _DELTA_SOURCES for message in sequence),
                "A delta logical floor must contain only accepted generated messages",
            )
            validate_message_history(sequence)
        offsets.append(start + len(floor))
    return offsets


def assemble_prompt_collection(
    collection: Mapping[str, Any], messages: list[dict[str, Any]], *,
    logical_floors: list[list[str]], prompt_message_ids: list[str],
    limits: PromptAssemblyLimits = PromptAssemblyLimits(),
) -> dict[str, Any]:
    """Insert effective items at complete logical-floor boundaries exactly once.

    Depth zero follows the newest root. Each depth step crosses one complete
    root/delta floor; excess depth clamps to the first middle boundary. Before
    and after regions stay outside middle items even at the same edge.
    """
    _require(type(limits) is PromptAssemblyLimits, "Prompt assembly requires explicit limits")
    collection = validate_prompt_collection(collection)
    validate_json_value(messages)
    _require(type(messages) is list, "Prompt assembly messages must be a JSON array")
    messages = [validate_record("agent_message", message) for message in messages]
    validate_message_history(messages)
    offsets = _validate_floors(messages, logical_floors)
    validate_json_value(prompt_message_ids)
    _require(
        type(prompt_message_ids) is list
        and len(prompt_message_ids) == len(collection["items"]),
        "Prompt message identities must be supplied once per effective item",
    )
    input_ids = [message["message_id"] for message in messages]
    _require(
        all(type(identity) is str for identity in prompt_message_ids),
        "Prompt message identities must be UUID4 text",
    )
    _require(
        len(set(prompt_message_ids)) == len(prompt_message_ids)
        and not set(prompt_message_ids).intersection(input_ids),
        "Prompt message identities must be unique and separate from input identities",
    )
    _usage(messages, limits)
    if len(messages) + len(collection["items"]) > limits.max_messages:
        raise PromptProcessingError(
            "prompt_assembly_limit", "Prompt assembly exceeds its message or total character limit",
        )

    placed: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    for index, item in enumerate(collection["items"]):
        message = validate_record("agent_message", {
            "schema_version": 3, "message_id": prompt_message_ids[index], "role": item["role"],
            "source": {
                "kind": "prompt", "prompt_id": item["item_id"], "revision": item["revision"],
                "item_instance_id": item["item_instance_id"],
                "group_instance_id": item["group_instance_id"],
            },
            "blocks": [{"kind": "text", "text": item["text"]}],
        })
        region = item["placement"]
        boundary = (
            max(0, len(logical_floors) - item["depth"]) if region == "middle"
            else 0 if region == "before" else len(logical_floors)
        )
        placed.append((boundary, region, {
            "collection_index": index, "message_id": message["message_id"],
            "message_index": 0, "region": region, "floor_boundary": boundary,
        }, message))

    def sort_key(value: tuple[int, str, dict[str, Any], dict[str, Any]]) -> tuple[Any, ...]:
        boundary, region, entry, _message = value
        index = entry["collection_index"]
        item = collection["items"][index]
        return (
            boundary, {"before": 0, "middle": 1, "after": 2}[region],
            item["order"], index, item["group_instance_id"] or "", item["item_instance_id"],
        )

    placed.sort(key=sort_key)
    assembled: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    cursor = 0
    for boundary, _region, entry, message in placed:
        position = offsets[boundary]
        assembled.extend(messages[cursor:position])
        cursor = position
        entry["message_index"] = len(assembled)
        entries.append(entry)
        assembled.append(message)
    assembled.extend(messages[cursor:])
    validate_message_history(assembled)
    usage = _usage(assembled, limits)
    result = {
        "schema_version": 1, "kind": "prompt_assembly", "messages": assembled,
        "manifest": {
            "schema_version": 1, "kind": "prompt_assembly_manifest",
            "collection": collection, "logical_floors": [list(floor) for floor in logical_floors],
            "input_message_ids": input_ids, "prompt_message_ids": list(prompt_message_ids),
            "entries": entries, "limits": limits.as_dict(), "usage": usage,
        },
    }
    return result


def validate_prompt_assembly(
    value: Mapping[str, Any], *, limits: PromptAssemblyLimits = PromptAssemblyLimits(),
) -> dict[str, Any]:
    """Rebuild an assembly from its evidence; reject changed messages or mapping."""
    validate_json_value(value)
    _fields(value, _ASSEMBLY_FIELDS, "Prompt assembly")
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "prompt_assembly", "Unknown prompt assembly version or kind",
    )
    manifest = value["manifest"]
    _fields(manifest, _MANIFEST_FIELDS, "Prompt assembly manifest")
    _require(
        type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
        and manifest["kind"] == "prompt_assembly_manifest",
        "Unknown prompt assembly manifest version or kind",
    )
    _fields(manifest["limits"], {"max_messages", "max_total_chars"}, "Prompt assembly limits")
    recorded_limits = PromptAssemblyLimits(**manifest["limits"])
    _fields(manifest["usage"], {"messages", "total_chars"}, "Prompt assembly usage")
    _require(type(manifest["entries"]) is list, "Prompt assembly entries must be a JSON array")
    for entry in manifest["entries"]:
        _fields(entry, _ENTRY_FIELDS, "Prompt assembly entry")
    _require(
        type(value["messages"]) is list, "Prompt assembly messages must be a JSON array",
    )
    messages = [validate_record("agent_message", message) for message in value["messages"]]
    validate_message_history(messages)
    _usage(messages, limits)
    input_ids = manifest["input_message_ids"]
    _require(
        type(input_ids) is list and all(type(identity) is str for identity in input_ids)
        and len(set(input_ids)) == len(input_ids),
        "Prompt assembly input identities must be a unique JSON array",
    )
    by_id = {message["message_id"]: message for message in messages}
    _require(set(input_ids) <= set(by_id), "Prompt assembly input message does not exist")
    expected = assemble_prompt_collection(
        manifest["collection"], [by_id[identity] for identity in input_ids],
        logical_floors=manifest["logical_floors"],
        prompt_message_ids=manifest["prompt_message_ids"], limits=recorded_limits,
    )
    _require(
        canonical_bytes(expected) == canonical_bytes(value),
        "Prompt assembly differs from its deterministic source evidence",
    )
    return expected
