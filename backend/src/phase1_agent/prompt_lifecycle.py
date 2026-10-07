"""Explicit lifetime-aware materials; PROMPT@2 keeps its original semantics."""

from copy import deepcopy
from uuid import UUID

from .content_contracts import make_prompt_item, object_schema, presentation_schema
from .contract_json import canonical_bytes, validate_json_value
from .graph_contracts import require, uuid4_string
from .host_sdk import DataTypeDefinition
from .prompt_contract import _item_order, _source_origins, _validate_material_item


PROMPT_MATERIALS_TYPE = "PROMPT_MATERIALS"
_ITEM_FIELDS = frozenset({
    "item_instance_id", "text", "role", "placement", "depth", "order", "enabled",
    "purpose", "source", "protected", "metadata", "lifecycle", "compaction", "origin_item_ids",
})
_BUSINESS_FIELDS = (
    "text", "role", "placement", "depth", "order", "enabled",
    "purpose", "metadata", "lifecycle", "compaction",
)
_UUID = {"type": "string", "pattern":
         "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}


def lifecycle_fields_schema() -> dict:
    return {
        "lifecycle": {"enum": ["per_request", "context_once"], "title": "注入生命周期"},
        "compaction": {"enum": ["never", "allowed"], "title": "上下文精简许可"},
    }


def validate_prompt_lifecycle(lifecycle, compaction, *, role, purpose="prompt") -> None:
    require(lifecycle in ("per_request", "context_once") and compaction in ("never", "allowed"),
            "prompt_lifecycle_invalid", "Prompt lifetime and compaction permission must be explicit")
    require(compaction == "never" or lifecycle == "context_once" and role in ("user", "assistant"),
            "prompt_compaction_protected",
            "Only persistent user or assistant background text may opt into compaction")
    require(purpose == "prompt" or lifecycle == "per_request" and compaction == "never",
            "prompt_lifecycle_protected", "Tool and other non-background materials remain per-request")


def lifecycle_prompt_content(items) -> dict:
    return {"schema_version": 1, "kind": "workflow.prompt-materials", "items": deepcopy(items)}


def make_lifecycle_prompt_item(node_id, member_id, text, presentation, *, lifecycle="per_request",
                               compaction="never", source=None, metadata=None, purpose="prompt") -> dict:
    item = make_prompt_item(node_id, member_id, text, presentation, source=source,
                            metadata=metadata, purpose=purpose)
    item["source"]["origin_item_id"] = item["item_instance_id"]
    item.update(lifecycle=lifecycle, compaction=compaction,
                origin_item_ids=[item["item_instance_id"]])
    return validate_lifecycle_prompt_item(item)


def validate_lifecycle_prompt_item(value) -> dict:
    require(type(value) is dict and set(value) == _ITEM_FIELDS,
            "prompt_lifecycle_material_invalid", "Lifetime-aware material fields are invalid")
    _validate_material_item({key: deepcopy(value[key]) for key in _ITEM_FIELDS
                             if key not in ("lifecycle", "compaction", "origin_item_ids")})
    validate_prompt_lifecycle(value["lifecycle"], value["compaction"],
                              role=value["role"], purpose=value["purpose"])
    origins = value["origin_item_ids"]
    require(type(origins) is list and 1 <= len(origins) <= 4096,
            "prompt_lifecycle_origins_invalid", "Material origin identities require a bounded nonempty list")
    for identity in origins:
        uuid4_string(identity)
    require(len(origins) == len(set(origins))
            and origins == sorted(origins, key=lambda identity: UUID(identity).int)
            and value["item_instance_id"] == origins[0],
            "prompt_lifecycle_origins_invalid",
            "Canonical material identity must retain its complete ordered origin identity set")
    sources = _source_origins(value["source"])
    require(all(type(source.get("origin_item_id")) is str for source in sources)
            and {source["origin_item_id"] for source in sources} == set(origins)
            and len(sources) == len(origins),
            "prompt_lifecycle_origins_invalid", "Every origin identity requires its unchanged source evidence")
    validate_json_value(value)
    return deepcopy(value)


def merge_lifecycle_prompt_materials(values) -> dict:
    require(type(values) is list, "prompt_lifecycle_materials_required",
            "Lifetime-aware aggregation requires a material list")
    groups, origins_seen, sources_seen = {}, {}, {}
    for value in values:
        require(type(value) is dict and set(value) == {"schema_version", "kind", "items"}
                and type(value["schema_version"]) is int and value["schema_version"] == 1
                and value["kind"] == "workflow.prompt-materials" and type(value["items"]) is list
                and len(value["items"]) <= 1024,
                "prompt_lifecycle_materials_required", "Aggregation requires explicit PROMPT_MATERIALS@1")
        for raw in value["items"]:
            item = validate_lifecycle_prompt_item(raw)
            key = canonical_bytes({field: item[field] for field in _BUSINESS_FIELDS})
            for identity in item["origin_item_ids"]:
                require(identity not in origins_seen or origins_seen[identity] == key,
                        "graph_prompt_identity_conflict", "A stable prompt identity has conflicting material")
                origins_seen[identity] = key
            for source in _source_origins(item["source"]):
                identity = source["origin_item_id"]
                require(identity not in sources_seen or sources_seen[identity] == canonical_bytes(source),
                        "graph_prompt_identity_conflict", "A stable prompt origin has conflicting source evidence")
                sources_seen[identity] = canonical_bytes(source)
            groups.setdefault(key, []).append(item)
    require(len(groups) <= 1024 and len(origins_seen) <= 4096,
            "prompt_lifecycle_capacity_exceeded", "Material and origin identity budgets cannot be discarded")
    result = []
    for group in groups.values():
        identities = sorted({identity for item in group for identity in item["origin_item_ids"]},
                            key=lambda identity: UUID(identity).int)
        kept = deepcopy(min(group, key=lambda item: UUID(item["item_instance_id"]).int))
        kept["item_instance_id"], kept["origin_item_ids"] = identities[0], identities
        origins = {canonical_bytes(origin): origin
                   for item in group for origin in _source_origins(item["source"])}
        ordered = [deepcopy(origins[key]) for key in sorted(origins)]
        kept["source"] = ordered[0] if len(ordered) == 1 else {"kind": "merged", "origins": ordered}
        result.append(kept)
    return lifecycle_prompt_content(sorted(result, key=_item_order))


def validate_lifecycle_prompt_materials(value) -> dict:
    merged = merge_lifecycle_prompt_materials([value])
    require(merged == value, "prompt_lifecycle_material_invalid",
            "Lifetime-aware material envelope must already be canonical")
    return deepcopy(value)


def lifecycle_prompt_schema() -> dict:
    item = object_schema({
        "item_instance_id": _UUID, "text": {"type": "string"},
        **presentation_schema()["properties"],
        "purpose": {"type": "string", "minLength": 1, "maxLength": 128},
        "source": {"type": "object", "minProperties": 1},
        "protected": {"const": False}, "metadata": {"type": "object"},
        **lifecycle_fields_schema(),
        "origin_item_ids": {"type": "array", "minItems": 1, "maxItems": 4096, "items": _UUID},
    })
    return object_schema({
        "schema_version": {"type": "integer", "const": 1},
        "kind": {"const": "workflow.prompt-materials"},
        "items": {"type": "array", "maxItems": 1024, "items": item},
    })


def lifecycle_prompt_definition() -> DataTypeDefinition:
    return DataTypeDefinition(
        PROMPT_MATERIALS_TYPE, 1, lifecycle_prompt_schema(), scope="content", max_bytes=4_000_000,
        validator=validate_lifecycle_prompt_materials,
        content_transformer=transform_lifecycle_prompt_materials,
    )


def transform_lifecycle_prompt_materials(value, operation, scope):
    from .content_contracts import _transform, prompt_content
    value = validate_lifecycle_prompt_materials(value)
    legacy_items = [{key: deepcopy(item[key]) for key in item
                     if key not in ("lifecycle", "compaction", "origin_item_ids")} for item in value["items"]]
    transformed = _transform(prompt_content(legacy_items), operation, scope, family="PROMPT")
    items = [{**item, **{key: deepcopy(original[key])
                       for key in ("lifecycle", "compaction", "origin_item_ids")}}
             for item, original in zip(transformed["items"], value["items"])]
    return merge_lifecycle_prompt_materials([lifecycle_prompt_content(items)])
