"""Historical ready PROMPT@3; PROMPT@2 remains an ordinary material contract."""

from copy import deepcopy

from .content_contracts import prompt_content
from .contract_json import canonical_bytes, validate_json_value
from .context_contract import artifact_ref, validate_context_view
from .graph_contracts import require
from .prompt_contract import _artifact_refs, _source_origins, merge_prompt_materials
from .prompt_depth import (
    LOGICAL_FLOOR_PLACEMENT, logical_floor_materials, uses_logical_floor_placement,
)


def _message(message):
    result = {key: deepcopy(message[key]) for key in
              ("role", "content", "tool_calls", "tool_call_id", "thinking_summary", "provider_metadata")
              if key in message}
    if "tool_calls" in result:
        result["tool_calls"] = [{"id": call["id"], "type": "function",
                                 "function": {"name": call["name"], "arguments": call["arguments"]}}
                                for call in result["tool_calls"]]
    return result


def _build(items, view, current_input, *, logical_floors=True):
    active = [item for item in items if item["enabled"]]
    for item in active:
        require(item["purpose"] not in ("role-card", "role_card", "rolecard")
                and all(source.get("kind") not in ("role-card", "role_card", "rolecard")
                        for source in _source_origins(item["source"])),
                "context_role_cards_excluded", "Role cards are outside this context package")
        require(logical_floors or item["placement"] != "middle" or item["depth"] <= len(view["units"]),
                "context_history_anchor_missing", "Middle depth exceeds complete historical units")
    messages, provenance = [], []

    def material(item):
        messages.append({"role": item["role"], "content": item["text"]})
        provenance.append({"kind": "material", "item_instance_id": item["item_instance_id"],
                           "source": deepcopy(item["source"])})

    if logical_floors:
        history, origins, floor_starts = [], [], []
        for entry in view["units"]:
            if "summary" in entry:
                summary = entry["summary"]
                history.append({"role": summary["role"], "content": summary["text"]})
                origins.append({"kind": "summary", "summary_ref": deepcopy(entry["summary_ref"]),
                                "summary_id": summary["summary_id"],
                                "covered_round_refs": deepcopy(summary["covered_round_refs"])})
                continue
            unit = entry["unit"]
            floor_starts.append(len(history))
            for index, message in enumerate([unit["root"], *unit["messages"]]):
                if index == 1:
                    floor_starts.append(len(history))
                history.append(_message(message))
                origins.append({"kind": "history", "unit_ref": deepcopy(entry["unit_ref"]),
                                "unit_id": unit["unit_id"], "message_id": message.get("message_id")})
        floor_starts.append(len(history))
        history.append(deepcopy(current_input))
        origins.append({"kind": "current_input"})
        buckets = logical_floor_materials(active, floor_starts, len(history))
        for index in range(len(history) + 1):
            for item in buckets.get(index, []):
                material(item)
            if index < len(history):
                messages.append(history[index])
                provenance.append(origins[index])
        return messages, provenance

    for item in active:
        if item["placement"] == "before":
            material(item)
    for index in range(len(view["units"]) + 1):
        depth = len(view["units"]) - index
        for item in active:
            if item["placement"] == "middle" and item["depth"] == depth:
                material(item)
        if index < len(view["units"]):
            entry = view["units"][index]
            if "summary" in entry:
                summary = entry["summary"]
                messages.append({"role": summary["role"], "content": summary["text"]})
                provenance.append({"kind": "summary", "summary_ref": deepcopy(entry["summary_ref"]),
                                   "summary_id": summary["summary_id"],
                                   "covered_round_refs": deepcopy(summary["covered_round_refs"])})
                continue
            unit = entry["unit"]
            for message in [unit["root"], *unit["messages"]]:
                messages.append(_message(message))
                provenance.append({"kind": "history", "unit_ref": deepcopy(entry["unit_ref"]),
                                   "unit_id": unit["unit_id"], "message_id": message.get("message_id")})
    messages.append(deepcopy(current_input))
    provenance.append({"kind": "current_input"})
    for item in active:
        if item["placement"] == "after":
            material(item)
    return messages, provenance


def assemble_context_prompt(materials, view, current_input, *, context_ref, current_input_ref,
                            source_output_refs=None):
    view = validate_context_view(view)
    require(type(current_input) is dict and set(current_input) == {"schema_version", "kind", "text"}
            and current_input["schema_version"] == 2 and current_input["kind"] == "workflow.text"
            and type(current_input["text"]) is str, "context_invalid_current_input",
            "Historical assembly requires one actual TEXT@2 input")
    items = merge_prompt_materials(materials)["items"]
    root = {"role": "user", "content": current_input["text"]}
    messages, provenance = _build(items, view, root)
    return {
        "schema_version": 3, "kind": "workflow.prompt", "stage": "assembled",
        "placement_profile": LOGICAL_FLOOR_PLACEMENT,
        "items": items, "context": deepcopy(view), "context_ref": artifact_ref(context_ref),
        "current_input": root, "current_input_ref": artifact_ref(current_input_ref),
        "source_output_refs": _artifact_refs(source_output_refs or []),
        "messages": messages, "provenance": provenance,
    }


def validate_context_ready_prompt(value):
    validate_json_value(value)
    require(type(value) is dict and set(value) - {"placement_profile"} == {
        "schema_version", "kind", "stage", "items", "context", "context_ref",
        "current_input", "current_input_ref", "source_output_refs", "messages", "provenance",
    } and type(value["schema_version"]) is int and value["schema_version"] == 3
        and value["kind"] == "workflow.prompt" and value["stage"] == "assembled",
        "context_prompt_not_ready", "Historical prompt requires exact PROMPT@3")
    view = validate_context_view(value["context"])
    artifact_ref(value["context_ref"])
    artifact_ref(value["current_input_ref"])
    _artifact_refs(value["source_output_refs"])
    root = value["current_input"]
    require(type(root) is dict and set(root) == {"role", "content"} and root["role"] == "user"
            and type(root["content"]) is str, "context_invalid_current_input",
            "Ready prompt requires the actual frozen user input")
    items = merge_prompt_materials([prompt_content(value["items"])])["items"]
    messages, provenance = _build(items, view, root, logical_floors=uses_logical_floor_placement(value))
    require(canonical_bytes(items) == canonical_bytes(value["items"])
            and canonical_bytes(messages) == canonical_bytes(value["messages"])
            and canonical_bytes(provenance) == canonical_bytes(value["provenance"]),
            "context_prompt_not_ready", "Frozen messages, ordering or provenance were modified")
    return deepcopy(value)


def context_prompt_references(value):
    refs = [value["context_ref"], value["current_input_ref"]]
    refs += [{"scope": "artifact", "output_id": entry["output_id"]}
             for entry in value["source_output_refs"]]
    return [deepcopy(ref) for _, ref in sorted({ref["output_id"]: ref for ref in refs}.items())]


def assemble_bound_context_prompt(materials, view, current_input, **references):
    from .context_v2 import validate_bound_context_view
    view = validate_bound_context_view(view)
    # Reuse ordering and message projection without changing PROMPT@3.
    legacy = deepcopy(view)
    legacy["schema_version"] = 1
    legacy["owner"].pop("agent_node_id")
    if legacy["derivation"]["operation"] == "merge":
        legacy["derivation"]["operation"] = "advance"
    result = assemble_context_prompt(materials, legacy, current_input, **references)
    result.update(schema_version=4, context=view)
    return validate_bound_context_prompt(result)


def validate_bound_context_prompt(value):
    from .context_v2 import validate_bound_context_view
    require(type(value) is dict and type(value.get("schema_version")) is int and value["schema_version"] == 4,
            "context_prompt_not_ready", "Bound historical prompt requires PROMPT@4")
    view = validate_bound_context_view(value["context"])
    legacy = deepcopy(value)
    legacy["schema_version"] = 3
    legacy["context"]["schema_version"] = 1
    legacy["context"]["owner"].pop("agent_node_id")
    if legacy["context"]["derivation"]["operation"] == "merge":
        legacy["context"]["derivation"]["operation"] = "advance"
    validate_context_ready_prompt(legacy)
    return deepcopy(value)


def validate_any_context_prompt(value):
    if type(value) is dict and value.get("schema_version") == 6:
        from .context_prompt_v6 import validate_native_context_prompt
        return validate_native_context_prompt(value)
    if type(value) is dict and value.get("schema_version") == 5:
        return validate_summary_context_prompt(value)
    return (validate_bound_context_prompt(value) if type(value) is dict and value.get("schema_version") == 4
            else validate_context_ready_prompt(value))


def assemble_summary_context_prompt(materials, view, current_input, *, context_ref, current_input_ref,
                                    source_output_refs=None, character_budget=100_000):
    from .context_v3 import validate_effective_view
    view = validate_effective_view(view)
    require(type(current_input) is dict and set(current_input) == {"schema_version", "kind", "text"}
            and current_input["schema_version"] == 2 and current_input["kind"] == "workflow.text"
            and type(current_input["text"]) is str, "context_invalid_current_input",
            "Assembly requires one actual TEXT@2 current input")
    items = merge_prompt_materials(materials)["items"]
    root = {"role": "user", "content": current_input["text"]}
    messages, provenance = _build(items, view, root)
    return validate_summary_context_prompt({
        "schema_version": 5, "kind": "workflow.prompt", "stage": "assembled", "items": items,
        "placement_profile": LOGICAL_FLOOR_PLACEMENT,
        "context": view, "context_ref": artifact_ref(context_ref), "current_input": root,
        "current_input_ref": artifact_ref(current_input_ref),
        "source_output_refs": _artifact_refs(source_output_refs or []),
        "messages": messages, "provenance": provenance, "character_budget": character_budget})


def validate_summary_context_prompt(value):
    from .context_v3 import validate_effective_view
    validate_json_value(value)
    require(type(value) is dict and set(value) - {"placement_profile"} == {
        "schema_version", "kind", "stage", "items", "context", "context_ref", "current_input",
        "current_input_ref", "source_output_refs", "messages", "provenance", "character_budget",
    } and type(value["schema_version"]) is int and value["schema_version"] == 5
        and value["kind"] == "workflow.prompt" and value["stage"] == "assembled",
        "context_prompt_not_ready", "Summary-aware assembly requires exact PROMPT@5")
    view = validate_effective_view(value["context"])
    artifact_ref(value["context_ref"])
    artifact_ref(value["current_input_ref"])
    _artifact_refs(value["source_output_refs"])
    root = value["current_input"]
    require(type(root) is dict and set(root) == {"role", "content"} and root["role"] == "user"
            and type(root["content"]) is str, "context_invalid_current_input",
            "Assembly requires the exact frozen current input")
    items = merge_prompt_materials([prompt_content(value["items"])])["items"]
    messages, provenance = _build(items, view, root, logical_floors=uses_logical_floor_placement(value))
    require(items == value["items"] and messages == value["messages"] and provenance == value["provenance"],
            "context_prompt_not_ready", "Effective messages, ordering or replacement provenance were modified")
    budget = value["character_budget"]
    require(type(budget) is int and 1 <= budget <= 4_000_000
            and len(canonical_bytes(messages).decode("utf-8")) <= budget,
            "context_prompt_length_failed", "Actual canonical message JSON exceeds the declared character budget")
    return deepcopy(value)
