"""Canonical native-context assembly with explicit prompt lifetime semantics."""

from copy import deepcopy
from uuid import UUID

from .agent_receipt_contract import projection_id
from .content_contracts import text_content
from .context_contract import artifact_ref
from .contract_graph import validate_message_history
from .contract_json import canonical_bytes, validate_json_value
from .contracts_v2 import validate_record
from .graph_contracts import require
from .prompt_contract import _artifact_refs
from .prompt_depth import (
    LOGICAL_FLOOR_PLACEMENT, logical_floor_materials, uses_logical_floor_placement,
)
from .prompt_lifecycle import lifecycle_prompt_content, merge_lifecycle_prompt_materials


_FIELDS = frozenset({
    "schema_version", "kind", "stage", "items", "context", "context_ref", "current_input",
    "current_input_ref", "source_output_refs", "messages", "provenance", "layout", "once_pending_item_ids",
})


def _material_message(item):
    return validate_record("agent_message", {
        "schema_version": 3,
        "message_id": projection_id(item["item_instance_id"], "prompt-material:" + item["lifecycle"]),
        "role": item["role"],
        "source": {"kind": "prompt", "prompt_id": item["item_instance_id"], "revision": 1,
                   "item_instance_id": item["item_instance_id"], "group_instance_id": None},
        "blocks": [{"kind": "text", "text": item["text"]}],
    })


def _current_message(current_input, reference):
    require(type(current_input) is dict and set(current_input) == {"schema_version", "kind", "text"}
            and type(current_input["schema_version"]) is int and current_input["schema_version"] == 2
            and current_input["kind"] == "workflow.text" and type(current_input["text"]) is str,
            "context_invalid_current_input", "Native assembly requires one actual TEXT@2 input")
    identity = projection_id(reference["output_id"], "current-input")
    return validate_record("agent_message", {
        "schema_version": 3, "message_id": identity, "role": "user",
        "source": {"kind": "prompt", "prompt_id": reference["output_id"], "revision": 1,
                   "item_instance_id": identity, "group_instance_id": None},
        "blocks": [{"kind": "text", "text": current_input["text"]}],
    })


def native_history_floor_starts(view):
    """Keep each root and its complete generated delta as separate floors."""
    starts, previous = [], None
    for index, (message, entry) in enumerate(zip(view["messages"], view["layout"])):
        if entry["kind"] != "history":
            continue
        root = message["role"] == "user" and message["source"]["kind"] in (
            "human", "upstream_node", "prompt")
        key = (entry["round_id"], "root" if root else "delta")
        if key != previous:
            starts.append(index)
            previous = key
    starts.append(len(view["messages"]))
    for start, end in zip(starts, starts[1:]):
        validate_message_history(view["messages"][start:end])
    return starts


def _assemble(items, view, current, *, logical_floors=True):
    active = [item for item in items if item["enabled"]]
    consumed = set(view["once_injected_item_ids"])
    pending = sorted({
        identity for item in active if item["lifecycle"] == "context_once"
        for identity in item["origin_item_ids"] if identity not in consumed
    }, key=lambda identity: UUID(identity).int)
    inject = [item for item in active if item["lifecycle"] == "per_request"
              or not set(item["origin_item_ids"]) & consumed]
    if not logical_floors:
        rounds = []
        for entry in view["layout"]:
            if entry["kind"] == "history" and entry["round_id"] is not None and entry["round_id"] not in rounds:
                rounds.append(entry["round_id"])
        boundaries = {0: len(view["messages"])}
        for depth, round_id in enumerate(reversed(rounds), 1):
            boundaries[depth] = next(index for index, entry in enumerate(view["layout"])
                                     if entry["kind"] == "history" and entry["round_id"] == round_id)
        for item in inject:
            require(item["placement"] != "middle" or item["depth"] in boundaries,
                    "context_history_anchor_missing", "Middle placement exceeds complete historical rounds")
    messages, provenance, layout = [], [], []

    def material(item):
        messages.append(_material_message(item))
        provenance.append({
            "kind": "material", "item_instance_id": item["item_instance_id"],
            "origin_item_ids": deepcopy(item["origin_item_ids"]), "source": deepcopy(item["source"]),
            "lifecycle": item["lifecycle"], "compaction": item["compaction"],
        })
        layout.append({
            "kind": "once" if item["lifecycle"] == "context_once" else "fixed",
            "round_id": None, "compaction": item["compaction"],
        })

    if logical_floors:
        history = [*view["messages"], current]
        buckets = logical_floor_materials(inject, native_history_floor_starts(view), len(history))
        for index in range(len(history) + 1):
            for item in buckets.get(index, []):
                material(item)
            if index < len(view["messages"]):
                messages.append(deepcopy(history[index]))
                provenance.append({"kind": "history", "message_id": history[index]["message_id"]})
                layout.append(deepcopy(view["layout"][index]))
            elif index == len(view["messages"]):
                messages.append(deepcopy(current))
                provenance.append({"kind": "current_input"})
                layout.append({"kind": "current", "round_id": None, "compaction": "never"})
        validate_message_history(messages)
        return messages, provenance, layout, pending

    for item in inject:
        if item["placement"] == "before":
            material(item)
    for index in range(len(view["messages"]) + 1):
        for item in inject:
            if item["placement"] == "middle" and boundaries[item["depth"]] == index:
                material(item)
        if index < len(view["messages"]):
            message = view["messages"][index]
            messages.append(deepcopy(message))
            provenance.append({"kind": "history", "message_id": message["message_id"]})
            layout.append(deepcopy(view["layout"][index]))
    messages.append(deepcopy(current))
    provenance.append({"kind": "current_input"})
    layout.append({"kind": "current", "round_id": None, "compaction": "never"})
    for item in inject:
        if item["placement"] == "after":
            material(item)
    validate_message_history(messages)
    return messages, provenance, layout, pending


def assemble_native_context_prompt(materials, view, current_input, *, context_ref,
                                   current_input_ref, source_output_refs=None):
    from .context_v4 import validate_native_context_view
    view = validate_native_context_view(view)
    items = merge_lifecycle_prompt_materials(materials)["items"]
    current_ref = artifact_ref(current_input_ref)
    current = _current_message(current_input, current_ref)
    messages, provenance, layout, pending = _assemble(items, view, current)
    return validate_native_context_prompt({
        "schema_version": 6, "kind": "workflow.prompt", "stage": "assembled", "items": items,
        "placement_profile": LOGICAL_FLOOR_PLACEMENT,
        "context": view, "context_ref": artifact_ref(context_ref), "current_input": current,
        "current_input_ref": current_ref,
        "source_output_refs": _artifact_refs([] if source_output_refs is None else source_output_refs),
        "messages": messages, "provenance": provenance, "layout": layout,
        "once_pending_item_ids": pending,
    })


def validate_native_context_prompt(value):
    from .context_v4 import validate_native_context_view
    validate_json_value(value)
    require(type(value) is dict and set(value) - {"placement_profile"} == _FIELDS
            and type(value["schema_version"]) is int and value["schema_version"] == 6
            and value["kind"] == "workflow.prompt" and value["stage"] == "assembled",
            "context_prompt_not_ready", "Native assembly requires exact PROMPT@6")
    view = validate_native_context_view(value["context"])
    artifact_ref(value["context_ref"])
    current_ref = artifact_ref(value["current_input_ref"])
    refs = _artifact_refs(value["source_output_refs"])
    current = validate_record("agent_message", value["current_input"])
    require(len(current["blocks"]) == 1 and current["blocks"][0]["kind"] == "text",
            "context_invalid_current_input", "Current input must retain its exact original text")
    expected_current = _current_message(text_content(current["blocks"][0]["text"]), current_ref)
    require(current == expected_current, "context_invalid_current_input",
            "Current input identity and source must match its exact accepted artifact")
    items = merge_lifecycle_prompt_materials([lifecycle_prompt_content(value["items"])])["items"]
    messages, provenance, layout, pending = _assemble(
        items, view, current, logical_floors=uses_logical_floor_placement(value))
    require(items == value["items"] and messages == value["messages"]
            and provenance == value["provenance"] and layout == value["layout"]
            and pending == value["once_pending_item_ids"] and refs == value["source_output_refs"],
            "context_prompt_not_ready", "Native projection or pending lifetime state was modified")
    require(len(canonical_bytes(value)) <= 4_000_000, "context_prompt_length_failed",
            "Native prompt exceeds its transport envelope budget")
    return deepcopy(value)


def native_context_prompt_references(value):
    refs = [value["context_ref"], value["current_input_ref"]]
    refs += [{"scope": "artifact", "output_id": entry["output_id"]}
             for entry in value["source_output_refs"]]
    return [deepcopy(ref) for _, ref in sorted({ref["output_id"]: ref for ref in refs}.items())]
