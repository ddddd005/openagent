"""Structured graph materials and protocol-safe, reproducible kernel assembly."""

from copy import deepcopy
from uuid import uuid4

from .contract_graph import validate_message_history
from .contract_json import canonical_bytes
from .contracts_v2 import validate_record
from .graph_contracts import prompt_value, require, uuid4_string
from .prompt_assembly import PromptAssemblyLimits, assemble_prompt_collection, validate_prompt_assembly


def archive_materials(archives):
    """Expose editable text while retaining each closed root/delta and protocol block."""
    items = []
    for archive in archives:
        for floor_index, messages in enumerate((archive["root"], archive["turn"]["messages"])):
            for index, original in enumerate(messages):
                message = validate_record("agent_message", original)
                protected = (any(block["kind"] != "text" for block in message["blocks"])
                             or message["message_id"] == archive["turn"]["final"]["message_id"]
                             or message["source"]["kind"] in ("protocol_feedback", "runtime_tool_observation"))
                items.append({
                    "item_instance_id": message["message_id"],
                    "text": "\n".join(block["text"] for block in message["blocks"] if block["kind"] == "text"),
                    "role": message["role"], "placement": "middle", "depth": 0,
                    "order": index, "enabled": True, "purpose": "context", "protected": protected,
                    "source": {"kind": "closed_archive", "turn_id": archive["turn"]["turn_id"],
                               "run_id": archive["turn"]["run_id"]},
                    "metadata": {"context": {"floor": floor_index, "index": index, "message": message}},
                })
    return prompt_value(items)


def merge_materials(values):
    items, seen = [], {}
    for value in values:
        for item in value["items"]:
            key = item["item_instance_id"]
            require(key not in seen or canonical_bytes(seen[key]) == canonical_bytes(item),
                    "graph_context_conflict", "Repeated material has different edits")
            if key not in seen:
                items.append(deepcopy(item))
                seen[key] = item
    return prompt_value(items)


def assemble_materials(values, text, *, input_id, limits, resolve_archive, current_root=None, prompt_message_ids=None):
    materials = merge_materials(values)
    grouped, prompts = {}, []
    for item in materials["items"]:
        if item["purpose"] != "context":
            require(not item["protected"] and item["role"] != "tool", "graph_protected_content",
                    "Protocol material requires a closed archive")
            if item["enabled"]:
                prompts.append(item)
            continue
        source = item["source"]
        require(source.get("kind") == "closed_archive", "graph_context_invalid", "Context has no closed archive")
        key = uuid4_string(source.get("turn_id"))
        grouped.setdefault(key, []).append(item)

    history, floors = [], []
    seen_messages = set()
    context_basis = []
    for turn_id, items in grouped.items():
        archive = resolve_archive(turn_id)
        expected = archive_materials([archive])["items"]
        expected_by_id = {item["item_instance_id"]: item for item in expected}
        require(set(expected_by_id) == {item["item_instance_id"] for item in items},
                "graph_context_incomplete", "Context must retain each complete root/delta pair")
        supplied = {item["item_instance_id"]: item for item in items}
        derived = {0: [], 1: []}
        for canonical in expected:
            item = supplied[canonical["item_instance_id"]]
            require(all(item[field] == canonical[field] for field in canonical if field != "text"),
                    "graph_context_invalid", "Context metadata or protocol protection changed")
            require(not canonical["protected"] or item["text"] == canonical["text"],
                    "graph_protected_content", "Protected protocol text changed")
            message = deepcopy(canonical["metadata"]["context"]["message"])
            if item["text"] != canonical["text"]:
                require(len(message["blocks"]) == 1 and message["blocks"][0]["kind"] == "text",
                        "graph_context_invalid", "Only a plain text block can be edited")
                message["blocks"][0]["text"] = item["text"]
            require(message["message_id"] not in seen_messages,
                    "graph_context_conflict", "Archive floors overlap inconsistently")
            seen_messages.add(message["message_id"])
            derived[canonical["metadata"]["context"]["floor"]].append(message)
        for floor in (derived[0], derived[1]):
            require(bool(floor), "graph_context_incomplete", "Archive floor is empty")
            history.extend(floor)
            floors.append([message["message_id"] for message in floor])
        context_basis.append({"turn_id": turn_id, "run_id": archive["turn"]["run_id"]})

    root = current_root or {"schema_version": 1, "message_id": str(uuid4()), "role": "user",
            "source": {"kind": "upstream_node", "output_id": uuid4_string(input_id)},
            "blocks": [{"kind": "text", "text": text}]}
    history.append(root)
    floors.append([root["message_id"]])
    collection_items = []
    for index, item in enumerate(prompts):
        collection_items.append({
            "item_id": item["source"].get("item_id", item["item_instance_id"]),
            "revision": item["source"].get("revision", 1),
            "name": item["metadata"].get("name") or "Graph material",
            **{field: item[field] for field in ("text", "role", "enabled", "placement", "depth", "order")},
            "interpolation": "literal", "source": {"kind": "configuration"},
            "item_instance_id": item["item_instance_id"], "group_id": None,
            "group_revision": None, "group_instance_id": None,
            "input_name": "graph", "declaration_index": index,
        })
    assembly = assemble_prompt_collection(
        {"schema_version": 1, "kind": "prompt_collection", "items": collection_items}, history,
        logical_floors=floors, prompt_message_ids=prompt_message_ids or [str(uuid4()) for _ in prompts],
        limits=PromptAssemblyLimits(**limits),
    )
    return {**materials, "stage": "assembled", "assembly": {
        "schema_version": 1, "kind": "workflow.prompt-assembly", "prompt": assembly,
        "current_root": root, "context_basis": context_basis,
    }}


def validate_ready_prompt(value):
    evidence = value["assembly"]
    require(type(evidence) is dict and set(evidence) == {
        "schema_version", "kind", "prompt", "current_root", "context_basis",
    } and evidence["schema_version"] == 1 and evidence["kind"] == "workflow.prompt-assembly",
        "graph_prompt_not_ready", "Kernel prompt has no assembly evidence")
    assembly = validate_prompt_assembly(evidence["prompt"])
    root = validate_record("agent_message", evidence["current_root"])
    require(root["message_id"] == assembly["manifest"]["logical_floors"][-1][0]
            and root in assembly["messages"], "graph_prompt_not_ready", "Current root differs from assembly")
    validate_message_history(assembly["messages"])
    return deepcopy(value)
