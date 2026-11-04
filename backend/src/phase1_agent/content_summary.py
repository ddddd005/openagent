"""Structured content aggregation before Agent input assembly."""

from copy import deepcopy

from .content_contracts import object_schema
from .context_contract import artifact_ref, preserve_artifact_references
from .contract_json import canonical_bytes, validate_json_value
from .graph_contracts import require
from .host_sdk import DataTypeDefinition
from .prompt_contract import _artifact_refs, merge_prompt_materials
from .prompt_lifecycle import (
    lifecycle_prompt_content, merge_lifecycle_prompt_materials,
    validate_lifecycle_prompt_materials,
)


CONTENT_SUMMARY_TYPE = "CONTENT_SUMMARY"
CONTENT_SUMMARY_VERSION = 1
CONTENT_SUMMARY_KIND = "workflow.content-summary"
_FIELDS = {"schema_version", "kind", "materials", "source_output_refs"}
_OPTIONAL_FIELDS = {"context", "context_ref", "current_input", "current_input_ref"}


def summary_prompt_materials(value):
    """Explicitly carry ordinary prompt materials into lifetime-aware aggregation."""
    if type(value) is dict and value.get("kind") == "workflow.prompt-materials":
        return validate_lifecycle_prompt_materials(value)
    require(type(value) is dict and value.get("stage") == "materials"
            and value.get("assembly") is None,
            "content_summary_materials_required",
            "Content aggregation accepts prompt materials, not assembled snapshots")
    ordinary = merge_prompt_materials([value])
    items = []
    for item in ordinary["items"]:
        identity = item["item_instance_id"]
        items.append({
            **deepcopy(item), "lifecycle": "per_request", "compaction": "never",
            "origin_item_ids": [identity],
            "source": {"kind": "prompt_projection", "origin_item_id": identity,
                       "original": deepcopy(item["source"])},
        })
    return merge_lifecycle_prompt_materials([lifecycle_prompt_content(items)])


def validate_content_summary(value):
    from .context_v4 import validate_native_context_view_v5

    validate_json_value(value)
    require(type(value) is dict and _FIELDS <= set(value)
            and set(value) <= _FIELDS | _OPTIONAL_FIELDS
            and type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == CONTENT_SUMMARY_KIND,
            "content_summary_invalid", "Content summary fields or version are invalid")
    validate_lifecycle_prompt_materials(value["materials"])
    require(_artifact_refs(value["source_output_refs"]) == value["source_output_refs"],
            "content_summary_invalid", "Content summary sources must be canonical artifact bindings")
    for content, reference in (("context", "context_ref"), ("current_input", "current_input_ref")):
        require((content in value) == (reference in value),
                "content_summary_reference_missing",
                "Content summary values must retain their exact original references")
        if content in value:
            artifact_ref(value[reference])
    if "context" in value:
        validate_native_context_view_v5(value["context"])
    if "current_input" in value:
        current = value["current_input"]
        require(type(current) is dict and set(current) == {"schema_version", "kind", "text"}
                and type(current["schema_version"]) is int and current["schema_version"] == 2
                and current["kind"] == "workflow.text" and type(current["text"]) is str,
                "context_invalid_current_input", "Current input must remain exact TEXT@2")
    require(len(canonical_bytes(value)) <= 4_000_000, "content_summary_length_failed",
            "Content summary exceeds its transport envelope budget")
    return deepcopy(value)


def summarize_content(materials, *, summary=None, view=None, context_ref=None,
                      current_input=None, current_input_ref=None, source_output_refs=None):
    inherited = validate_content_summary(summary) if summary is not None else None
    values = ([inherited["materials"]] if inherited is not None else [])
    values.extend(summary_prompt_materials(value) for value in materials)
    result = {
        "schema_version": 1, "kind": CONTENT_SUMMARY_KIND,
        "materials": merge_lifecycle_prompt_materials(values),
        "source_output_refs": _artifact_refs(
            [] if source_output_refs is None else source_output_refs),
    }
    for name, reference, content, ref in (
        ("context", "context_ref", view, context_ref),
        ("current_input", "current_input_ref", current_input, current_input_ref),
    ):
        require((content is None) == (ref is None), "content_summary_reference_missing",
                "Content summary inputs require exact references")
        if inherited is not None and name in inherited:
            require(content is None or (content == inherited[name] and ref == inherited[reference]),
                    "content_summary_input_conflict",
                    "A summary cannot merge different contexts or current inputs")
            result[name], result[reference] = deepcopy(inherited[name]), deepcopy(inherited[reference])
        elif content is not None:
            result[name], result[reference] = deepcopy(content), artifact_ref(ref)
    return validate_content_summary(result)


def content_summary_references(value):
    refs = [value[key] for key in ("context_ref", "current_input_ref") if key in value]
    refs.extend({"scope": "artifact", "output_id": ref["output_id"]}
                for ref in value["source_output_refs"])
    return [deepcopy(ref) for _, ref in sorted({ref["output_id"]: ref for ref in refs}.items())]


def content_summary_definition():
    return DataTypeDefinition(
        CONTENT_SUMMARY_TYPE, CONTENT_SUMMARY_VERSION,
        object_schema({
            "schema_version": {"type": "integer", "const": 1},
            "kind": {"const": CONTENT_SUMMARY_KIND},
            "materials": {"type": "object"}, "source_output_refs": {"type": "array"},
            "context": {"type": "object"}, "context_ref": {"type": "object"},
            "current_input": {"type": "object"}, "current_input_ref": {"type": "object"},
        }, required=sorted(_FIELDS)),
        scope="content", validator=validate_content_summary, max_bytes=4_000_000,
        references=content_summary_references, reference_mapper=preserve_artifact_references,
        artifact_bindings=lambda value: value["source_output_refs"],
    )
