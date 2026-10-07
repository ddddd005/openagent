"""Independent lorebook nodes with explicit prompt and variable boundaries."""

from copy import deepcopy

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import object_schema
from .contract_json import canonical_bytes
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import bounded_name
from .lorebook_engine import (
    default_lorebook_entry, lorebook_entry_schema, validate_lorebook_entries,
)
from .lorebook_nodes import execute_lorebook
from .tool_package import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE


TAVERN_PACKAGE_ID = "workflow.tavern"
TAVERN_PACKAGE_VERSION = "1.0.0"
TAVERN_COMPONENTS = ("lorebook.item", "lorebook.group")
TAVERN_FRONTEND_EXTENSIONS = tuple({
    "extension_id": f"workflow.tavern.{name}-fields",
    "kind": "field-editor",
    "entrypoint": f"workflow.tavern.workbench.{name}-fields",
    "component_id": f"lorebook.{name}",
    "component_version": "1",
    "binding": {
        "surface": "workbench", "slot": "node-fields",
        "target": {"component_id": f"lorebook.{name}", "component_version": "1"},
    },
} for name in ("item", "group"))


def lorebook_node_schema(*, grouped=False):
    entry = lorebook_entry_schema()
    return object_schema({
        "entries" if grouped else "entry": (
            {"type": "array", "maxItems": 1024, "items": entry} if grouped else entry),
        "object_keys": {
            "type": "array", "maxItems": 1024, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
    })


def validate_lorebook_node_config(config, *, grouped=False):
    validate_lorebook_entries(config["entries"] if grouped else [config["entry"]])
    require(all(bounded_name(key) for key in config["object_keys"]),
            "lorebook_config_invalid", "Variable object keys must be explicit canonical identities")
    require(len(canonical_bytes(config)) <= 4_000_000, "lorebook_budget_exceeded",
            "Lorebook configuration exceeds its transport budget")


def create_tavern_package():
    def register(host):
        for extension in TAVERN_FRONTEND_EXTENSIONS:
            host.register_frontend_extension(**extension, host_protocol_version=1)
        for name, title in (("item", "Lorebook 单条目"), ("group", "Lorebook 条目组")):
            grouped = name == "group"
            entry = default_lorebook_entry()
            default = {"entries": [entry], "object_keys": []} if grouped else {
                "entry": entry, "object_keys": []}
            host.register_node(NodeDefinition(
                "lorebook." + name, "1", title, "类酒馆", deepcopy(default),
                lorebook_node_schema(grouped=grouped),
                inputs=(NodePort("input", "PROMPT", data_schema_version=6),),
                outputs=(NodePort("output", "PROMPT_MATERIALS"),),
                capabilities=("objects:read", "artifacts:read"), input_storage="references",
                object_accesses=({
                    "config_field": "object_keys", "multiple": True, "access": "read",
                    "type_id": VARIABLE_TYPE, "schema_version": VARIABLE_SCHEMA_VERSION,
                },),
            ), lambda config, inputs, context, grouped=grouped: execute_lorebook(
                config, inputs, context, grouped=grouped),
                config_validator=lambda config, grouped=grouped: validate_lorebook_node_config(
                    config, grouped=grouped))

    return CapabilityPackage(PackageManifest(
        TAVERN_PACKAGE_ID, TAVERN_PACKAGE_VERSION,
        dependencies=tuple(PackageDependency(identity, "1.0.0") for identity in (
            "workflow.content", "workflow.context", "workflow.tools",
        )),
        exports={
            "nodes": [{"component_id": identity, "component_version": "1"}
                      for identity in TAVERN_COMPONENTS],
            "frontend_extensions": [{"extension_id": row["extension_id"]}
                                    for row in TAVERN_FRONTEND_EXTENSIONS],
        },
    ), register)
