"""Global lorebook content and exact, frozen-reference activation."""

from copy import deepcopy

from ..content_contracts import object_schema
from ..contract_json import canonical_bytes, validate_json_value
from ..global_resources import (
    MAX_GLOBAL_RESOURCE_BYTES, global_resource_reference, validate_global_resource_reference,
)
from ..graph_contracts import NodeDefinition, NodePort, require
from ..host_sdk import DataTypeDefinition, ResourceIdentity, bounded_name
from ..lorebook_engine import lorebook_entry_schema, validate_lorebook_entries
from ..lorebook_nodes import execute_lorebook
from ..prompt_contract import check_prompt_schema
from ..tool_package import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE


LOREBOOK_RESOURCE_TYPE = "workflow.tavern.lorebook"
GLOBAL_LOREBOOK_COMPONENTS = ("lorebook.global-reference", "lorebook.global-activate")
_DEFAULT_RESOURCE_ID = "877a4804-ac85-4ce0-b9ea-7855d77e9701"
OBJECT_KEYS_SCHEMA = {
    "type": "array", "maxItems": 1024, "uniqueItems": True,
    "items": {"type": "string", "minLength": 1, "maxLength": 128},
}


def lorebook_resource_schema():
    return object_schema({
        "name": {"type": "string", "maxLength": 128}, "enabled": {"type": "boolean"},
        "entries": {"type": "array", "maxItems": 1024, "items": lorebook_entry_schema()},
    })


def validate_lorebook_resource(value):
    validate_json_value(value)
    check_prompt_schema(value, lorebook_resource_schema(), "lorebook_resource_invalid",
                        "Global lorebooks contain only name, enabled and entries")
    validate_lorebook_entries(value["entries"])
    require(len(canonical_bytes(value)) <= MAX_GLOBAL_RESOURCE_BYTES,
            "lorebook_budget_exceeded", "Global lorebook exceeds its resource budget")
    return deepcopy(value)


def validate_lorebook_reference(config):
    reference = ResourceIdentity.from_dict(config["reference"]).to_dict()
    require(reference["type_id"] == LOREBOOK_RESOURCE_TYPE, "lorebook_resource_type_mismatch",
            "Lorebook references require the exact tavern lorebook resource type")


def preflight_lorebook(config, records):
    reference = config.get("reference")
    matching = records if reference is None else [record for record in records if all(
        record.get(key) == reference[key] for key in ("scope", "type_id", "resource_id"))]
    require(len(matching) == 1, "global_resource_missing",
            "Global lorebook activation requires one declared resource")
    record = matching[0]
    require(record["type_id"] == LOREBOOK_RESOURCE_TYPE and record["data_schema_version"] == 1,
            "lorebook_resource_type_mismatch", "Global lorebooks require resource schema one")
    require(validate_lorebook_resource(record["value"])["enabled"], "global_content_disabled",
            "Global lorebook is disabled")


def validate_activation_config(config):
    require(all(bounded_name(key) for key in config["object_keys"]),
            "lorebook_config_invalid", "Variable object keys must be explicit canonical identities")


def activate_lorebook(config, inputs, context):
    reference = validate_global_resource_reference(inputs["resource"])["reference"]
    validate_lorebook_reference({"reference": reference})
    record = context.host_call("resources:read", "current-global-resource", reference)
    preflight_lorebook({"reference": reference}, [record])
    context.reads.append({
        "kind": "global_resource_read", "reference": deepcopy(reference),
        "update_sequence": record["update_sequence"],
    })
    return execute_lorebook(
        {"entries": record["value"]["entries"], "object_keys": config["object_keys"]},
        inputs, context, grouped=True,
    )


def register_global_lorebook(host):
    host.register_data_type(DataTypeDefinition(
        LOREBOOK_RESOURCE_TYPE, 1, lorebook_resource_schema(),
        {"name": "", "enabled": True, "entries": []}, scope="global",
        validator=validate_lorebook_resource,
    ))
    reference = ResourceIdentity("workspace", LOREBOOK_RESOURCE_TYPE, _DEFAULT_RESOURCE_ID).to_dict()
    host.register_node(NodeDefinition(
        "lorebook.global-reference", "1", "全局 Lorebook 引用", "类酒馆",
        {"reference": reference}, object_schema({"reference": {"type": "object"}}),
        outputs=(NodePort("output", "GLOBAL_RESOURCE_REF"),),
        capabilities=("resources:read",), input_storage="references",
    ), lambda config, inputs, context: {"output": global_resource_reference(config["reference"])},
        config_validator=validate_lorebook_reference,
        resource_dependencies_declaration=lambda config: [
            {"kind": "global-resource", "reference": deepcopy(config["reference"])}],
        resource_preflight_validator=preflight_lorebook)
    host.register_node(NodeDefinition(
        "lorebook.global-activate", "1", "全局 Lorebook 触发", "类酒馆",
        {"object_keys": []}, object_schema({"object_keys": deepcopy(OBJECT_KEYS_SCHEMA)}),
        inputs=(NodePort("input", "PROMPT", data_schema_version=6),
                NodePort("resource", "GLOBAL_RESOURCE_REF")),
        outputs=(NodePort("output", "PROMPT_MATERIALS"),),
        capabilities=("resources:read", "objects:read", "artifacts:read"), input_storage="references",
        object_accesses=({
            "config_field": "object_keys", "multiple": True, "access": "read",
            "type_id": VARIABLE_TYPE, "schema_version": VARIABLE_SCHEMA_VERSION,
        },),
    ), activate_lorebook, config_validator=validate_activation_config,
        resource_input_ports=("resource",), resource_preflight_validator=preflight_lorebook)
