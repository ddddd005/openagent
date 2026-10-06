"""Pure prompt material and assembly contracts shared by content consumers.

This module does not load a node package or interpret Agent/history state.
Current assemblies require unprotected materials and one optional user input.
"""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from jsonschema import Draft202012Validator, ValidationError

from .content_contracts import presentation_schema, prompt_content
from .contract_json import canonical_bytes, validate_json_value
from .graph_contracts import require, uuid4_string


_BUSINESS_FIELDS = (
    "text", "role", "placement", "depth", "order", "enabled", "purpose", "metadata",
)


def check_prompt_schema(value: object, schema: dict, code: str, message: str) -> None:
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError:
        require(False, code, message)


def _source_origins(source: dict) -> list[dict]:
    if source.get("kind") == "merged":
        origins = source.get("origins")
        require(set(source) == {"kind", "origins"} and type(origins) is list and bool(origins)
                and all(type(origin) is dict and bool(origin)
                        and origin.get("kind") != "merged" for origin in origins),
                "graph_prompt_source_invalid", "Merged prompt provenance must contain original sources")
        return deepcopy(origins)
    return [deepcopy(source)]


def _item_order(item: dict) -> tuple:
    return ({"before": 0, "middle": 1, "after": 2}[item["placement"]],
            -item["depth"] if item["placement"] == "middle" else 0,
            item["order"], UUID(item["item_instance_id"]).int)


def _validate_material_item(item: dict) -> None:
    require(type(item) is dict and set(item) == {
        "item_instance_id", "text", "role", "placement", "depth", "order", "enabled",
        "purpose", "source", "protected", "metadata",
    }, "graph_prompt_material_invalid", "Prompt material fields are invalid")
    uuid4_string(item["item_instance_id"])
    require(type(item["text"]) is str and type(item["purpose"]) is str and bool(item["purpose"])
            and type(item["metadata"]) is dict and type(item["source"]) is dict and bool(item["source"]),
            "graph_prompt_material_invalid", "Prompt material content or provenance is invalid")
    check_prompt_schema({key: item[key] for key in ("role", "placement", "depth", "order", "enabled")},
                        presentation_schema(), "graph_prompt_material_invalid", "Prompt presentation is invalid")
    require(item["role"] in ("system", "user", "assistant") and item["protected"] is False
            and item["purpose"] != "context",
            "graph_prompt_protected_material", "History and protocol facts require their own context contract")
    require((item["placement"] == "middle" and type(item["depth"]) is int and item["depth"] >= 0)
            or (item["placement"] != "middle" and item["depth"] is None),
            "graph_prompt_material_invalid", "Prompt depth does not match its placement")
    _source_origins(item["source"])


def _canonical_items(values: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    groups: dict[bytes, list[dict]] = {}
    for value in values:
        require(type(value) is dict and value.get("schema_version") == 2
                and set(value) == {"schema_version", "kind", "stage", "items", "assembly"}
                and value.get("kind") == "workflow.prompt" and value.get("stage") == "materials"
                and value.get("assembly") is None and type(value.get("items")) is list,
                "graph_prompt_materials_required", "Prompt operations require unfrozen PROMPT@2 materials")
        for item in value["items"]:
            _validate_material_item(item)
            identity = item["item_instance_id"]
            require(identity not in seen or canonical_bytes(seen[identity]) == canonical_bytes(item),
                    "graph_prompt_identity_conflict", "The same item identity has different content or provenance")
            if identity in seen:
                continue
            seen[identity] = deepcopy(item)
            key = canonical_bytes({field: item[field] for field in _BUSINESS_FIELDS})
            groups.setdefault(key, []).append(item)
    result = []
    for group in groups.values():
        kept = deepcopy(min(group, key=lambda item: UUID(item["item_instance_id"]).int))
        origins = {canonical_bytes(origin): origin
                   for item in group for origin in _source_origins(item["source"])}
        ordered_origins = [deepcopy(origins[key]) for key in sorted(origins)]
        kept["source"] = (ordered_origins[0] if len(ordered_origins) == 1
                          else {"kind": "merged", "origins": ordered_origins})
        result.append(kept)
    return sorted(result, key=_item_order)


def merge_prompt_materials(values: list[dict]) -> dict:
    """Order by declared position/order/stable ID and retain merged provenance."""
    return prompt_content(_canonical_items(values))


def _artifact_refs(refs: list[dict]) -> list[dict]:
    require(type(refs) is list and len(refs) <= 4096, "graph_prompt_artifact_refs_invalid",
            "Assembly artifact bindings must be a bounded array")
    identities = []
    for ref in refs:
        require(type(ref) is dict and set(ref) == {"edge_id", "output_id", "order"}
                and type(ref["order"]) is int and 0 <= ref["order"] <= 2**53 - 1,
                "graph_prompt_artifact_refs_invalid", "Assembly artifact binding fields are invalid")
        identities.append(uuid4_string(ref["edge_id"]))
        uuid4_string(ref["output_id"])
    require(len(identities) == len(set(identities)), "graph_prompt_artifact_refs_invalid",
            "Assembly artifact bindings cannot repeat an edge")
    return sorted(deepcopy(refs), key=lambda ref: (ref["order"], UUID(ref["edge_id"]).int))


def _build_assembly(items: list[dict], current_input: dict | None, *,
                    source_output_refs: list[dict], current_input_refs: list[dict]) -> dict:
    active = [item for item in items if item["enabled"]]
    require(all(item["placement"] != "middle" or item["depth"] == 0 for item in active),
            "graph_prompt_history_anchor_required",
            "Middle depth greater than zero requires an explicit history contract")
    require(current_input is None or type(current_input) is dict
            and set(current_input) == {"role", "content"} and current_input["role"] == "user"
            and type(current_input["content"]) is str,
            "graph_prompt_current_input_invalid", "Current input must be one explicit user text")
    messages = [{"role": item["role"], "content": item["text"]}
                for item in active if item["placement"] != "after"]
    if current_input is not None:
        messages.append(deepcopy(current_input))
    messages.extend({"role": item["role"], "content": item["text"]}
                    for item in active if item["placement"] == "after")
    require(bool(messages), "graph_prompt_empty", "An assembly requires effective materials or current input")
    source_refs = _artifact_refs(source_output_refs)
    current_refs = _artifact_refs(current_input_refs)
    require(len(current_refs) <= 1 and (current_input is not None or not current_refs),
            "graph_prompt_artifact_refs_invalid", "Current input bindings do not match current input")
    require(not ({ref["edge_id"] for ref in source_refs} & {ref["edge_id"] for ref in current_refs}),
            "graph_prompt_artifact_refs_invalid", "An edge cannot bind both materials and current input")
    return {
        "schema_version": 2, "kind": "workflow.prompt-assembly", "messages": messages,
        "manifest": {"ordered_item_ids": [item["item_instance_id"] for item in active],
                     "source_output_refs": source_refs, "current_input_refs": current_refs},
        "current_input": deepcopy(current_input),
    }


def assemble_prompt(values: list[dict], current_input: dict | None = None, *,
                    source_output_refs: list[dict] | None = None,
                    current_input_refs: list[dict] | None = None) -> dict:
    items = _canonical_items(values)
    if current_input is not None:
        require(type(current_input) is dict and set(current_input) == {"schema_version", "kind", "text"}
                and current_input["schema_version"] == 2 and current_input["kind"] == "workflow.text"
                and type(current_input["text"]) is str,
                "graph_prompt_current_input_invalid", "Assembly current input requires TEXT@2")
        current_input = {"role": "user", "content": current_input["text"]}
    assembly = _build_assembly(
        items, current_input,
        source_output_refs=[] if source_output_refs is None else source_output_refs,
        current_input_refs=[] if current_input_refs is None else current_input_refs,
    )
    return prompt_content(items, assembly=assembly)


def validate_ready_prompt(value: dict) -> dict:
    """Recompute messages and ordering instead of trusting a frozen manifest."""
    validate_json_value(value)
    require(type(value) is dict and set(value) == {"schema_version", "kind", "stage", "items", "assembly"}
            and value["schema_version"] == 2 and value["kind"] == "workflow.prompt"
            and value["stage"] == "assembled" and type(value["items"]) is list,
            "graph_prompt_not_ready", "Ready prompt requires the explicit PROMPT@2 envelope")
    evidence = value["assembly"]
    require(type(evidence) is dict and set(evidence) == {
        "schema_version", "kind", "messages", "manifest", "current_input",
    } and evidence["schema_version"] == 2 and evidence["kind"] == "workflow.prompt-assembly",
            "graph_prompt_not_ready", "Prompt assembly evidence is invalid")
    manifest = evidence["manifest"]
    require(type(manifest) is dict and set(manifest) == {
        "ordered_item_ids", "source_output_refs", "current_input_refs",
    }, "graph_prompt_not_ready", "Prompt assembly manifest is invalid")
    items = _canonical_items([{**value, "stage": "materials", "assembly": None}])
    require(canonical_bytes(items) == canonical_bytes(value["items"]),
            "graph_prompt_not_ready", "Ready prompt materials are not canonical")
    expected = _build_assembly(items, evidence["current_input"],
                               source_output_refs=manifest["source_output_refs"],
                               current_input_refs=manifest["current_input_refs"])
    require(canonical_bytes(expected) == canonical_bytes(evidence), "graph_prompt_not_ready",
            "Frozen messages or assembly evidence differ from their materials")
    return deepcopy(value)
