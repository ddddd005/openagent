"""Shared v2 content contracts, independent of Agent execution.

Transformations visit declared editable strings, never the serialized envelope.
Source evidence, platform identities and protected material remain opaque.
The four-megabyte envelope budget is a transport budget, not a variable or
model-context length policy.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from uuid import UUID

from .capability_packages import CapabilityPackage, PackageManifest
from .graph_contracts import require, uuid4_string
from .host_sdk import DataTypeDefinition


CONTENT_VERSION = 2
CONTENT_WIRE_BYTES = 4_000_000
_UUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}


def object_schema(properties, *, required=None):
    return {"type": "object", "properties": deepcopy(properties),
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def default_presentation():
    return {"role": "system", "placement": "before", "depth": None, "order": 0, "enabled": True}


def presentation_schema():
    return object_schema({
        "role": {"enum": ["system", "user", "assistant"]},
        "placement": {"enum": ["before", "middle", "after"]},
        "depth": {"type": ["integer", "null"], "minimum": 0},
        "order": {"type": "integer"}, "enabled": {"type": "boolean"},
    })


def validate_presentation(value):
    require(type(value["depth"]) is int and value["depth"] >= 0
            if value["placement"] == "middle" else value["depth"] is None,
            "content_invalid_placement", "Depth applies only to middle placement")


def stable_item_id(node_id, member_id):
    return str(UUID(bytes=sha256((node_id + ":" + member_id).encode("utf-8")).digest()[:16], version=4))


def make_prompt_item(node_id, member_id, text, presentation, *, source=None, metadata=None, purpose="prompt"):
    return {"item_instance_id": stable_item_id(node_id, member_id), "text": text,
            **deepcopy(presentation), "purpose": purpose, "protected": False,
            "source": deepcopy(source) if source is not None else {
                "kind": "configuration", "node_binding_id": node_id, "item_id": member_id},
            "metadata": deepcopy(metadata or {})}


def text_content(text):
    return {"schema_version": 2, "kind": "workflow.text", "text": text}


def json_content(value):
    return {"schema_version": 2, "kind": "workflow.json", "value": deepcopy(value)}


def prompt_content(items, assembly=None):
    return {"schema_version": 2, "kind": "workflow.prompt",
            "stage": "assembled" if assembly is not None else "materials",
            "items": deepcopy(items), "assembly": deepcopy(assembly)}


def _validate_prompt(value):
    identities = []
    for item in value["items"]:
        identities.append(uuid4_string(item["item_instance_id"]))
        validate_presentation(item)
        require(item["role"] != "tool" or item["protected"], "content_protected_protocol",
                "Tool protocol material must be protected")
    require(len(set(identities)) == len(identities), "content_duplicate_identity",
            "Prompt contains repeated item identities")
    require(value["assembly"] is None if value["stage"] == "materials" else type(value["assembly"]) is dict,
            "content_invalid_assembly", "Prompt stage differs from its assembly evidence")
    if value["assembly"] is not None:
        from .prompt_contract import validate_ready_prompt
        validate_ready_prompt(value)


def _prompt_references(value):
    if value["assembly"] is None:
        return []
    manifest = value["assembly"]["manifest"]
    identities = {ref["output_id"] for key in ("source_output_refs", "current_input_refs")
                  for ref in manifest[key]}
    return [{"scope": "artifact", "output_id": identity} for identity in sorted(identities)]


def _prompt_artifact_bindings(value):
    if value["assembly"] is None:
        return []
    manifest = value["assembly"]["manifest"]
    return deepcopy(manifest["source_output_refs"] + manifest["current_input_refs"])


def _transform(value, operation, scope, *, family):
    """Apply one batch, then rebuild declared fields without mutating the source."""
    slots = []

    def collect(item, *, keys=False):
        if type(item) is str:
            offset = len(slots)
            slots.append(item)
            return ("string", offset)
        if type(item) is list:
            return ("array", [collect(child, keys=keys) for child in item])
        if type(item) is dict:
            return ("object", [(collect(key) if keys else ("literal", key), collect(child, keys=keys))
                               for key, child in item.items()])
        return ("literal", deepcopy(item))

    derived = deepcopy(value)
    if family == "TEXT":
        plans = [(derived, "text", collect(derived["text"]))]
    elif family == "JSON":
        plans = [(derived, "value", collect(derived["value"], keys=scope == "all"))]
    else:
        plans = []
        for item in derived["items"]:
            if item["protected"]:
                continue
            plans.append((item, "text", collect(item["text"])))
            if scope == "all":
                for field in ("role", "placement", "purpose"):
                    plans.append((item, field, collect(item[field])))
                plans.append((item, "metadata", collect(item["metadata"], keys=True)))
        if derived["assembly"] is not None and derived["assembly"]["current_input"] is not None:
            current_input = derived["assembly"]["current_input"]
            plans.append((current_input, "content", collect(current_input["content"])))
    replaced = operation(slots)
    require(type(replaced) is list and len(replaced) == len(slots)
            and all(type(text) is str for text in replaced),
            "content_invalid_transform", "Transformation must return one string per editable input")

    def rebuild(plan):
        kind, item = plan
        if kind == "string":
            return replaced[item]
        if kind == "array":
            return [rebuild(child) for child in item]
        if kind == "object":
            result = {}
            for key, child in item:
                name = rebuild(key)
                require(name not in result, "content_field_collision",
                        "Transformation produced duplicate business field names")
                result[name] = rebuild(child)
            return result
        return deepcopy(item)

    for container, field, plan in plans:
        container[field] = rebuild(plan)
    if family == "PROMPT" and derived["assembly"] is not None:
        from .prompt_contract import assemble_prompt
        evidence = derived["assembly"]
        current_input = evidence["current_input"]
        return assemble_prompt(
            [prompt_content(derived["items"])],
            text_content(current_input["content"]) if current_input is not None else None,
            source_output_refs=evidence["manifest"]["source_output_refs"],
            current_input_refs=evidence["manifest"]["current_input_refs"],
        )
    return derived


def create_content_package():
    item_schema = object_schema({
        "item_instance_id": _UUID, "text": {"type": "string"},
        **presentation_schema()["properties"],
        "role": {"enum": ["system", "user", "assistant", "tool"]},
        "purpose": {"type": "string", "minLength": 1, "maxLength": 128},
        "source": {"type": "object", "minProperties": 1},
        "protected": {"type": "boolean"}, "metadata": {"type": "object"},
    })
    schemas = {
        "TEXT": object_schema({"schema_version": {"const": 2}, "kind": {"const": "workflow.text"},
                               "text": {"type": "string"}}),
        "JSON": object_schema({"schema_version": {"const": 2}, "kind": {"const": "workflow.json"}, "value": {}}),
        "PROMPT": object_schema({"schema_version": {"const": 2}, "kind": {"const": "workflow.prompt"},
            "stage": {"enum": ["materials", "assembled"]},
            "items": {"type": "array", "maxItems": 1024, "items": item_schema},
            "assembly": {"type": ["object", "null"]}}),
    }

    def register(host):
        for identity, schema in schemas.items():
            host.register_data_type(DataTypeDefinition(
                identity, 2, schema, scope="content", max_bytes=CONTENT_WIRE_BYTES,
                validator=_validate_prompt if identity == "PROMPT" else None,
                references=_prompt_references if identity == "PROMPT" else None,
                reference_mapper=(lambda value, mapping: deepcopy(value)) if identity == "PROMPT" else None,
                artifact_bindings=_prompt_artifact_bindings if identity == "PROMPT" else None,
                content_transformer=lambda value, op, scope, family=identity:
                    _transform(value, op, scope, family=family),
            ))
        from .global_resources import global_resource_ref_definition
        host.register_shared_data_type(global_resource_ref_definition())

    return CapabilityPackage(PackageManifest("workflow.content", "1.0.0"), register)
