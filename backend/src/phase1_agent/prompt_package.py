"""Current lifecycle materials and ordinary one-response prompt assembly."""

from copy import deepcopy
from collections.abc import Mapping

from jsonschema import Draft202012Validator

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import object_schema, prompt_content
from .graph_contracts import NodeDefinition, NodePort, require
from .prompt_contract import assemble_prompt
from .prompt_lifecycle import merge_lifecycle_prompt_materials, PROMPT_MATERIALS_TYPE
from .prompt_lifecycle_nodes import LIFECYCLE_PROMPT_COMPONENTS, register_lifecycle_prompt_nodes

PROMPT_RESOURCE_TYPE = "workflow.prompt-resource"
PROMPT_PACKAGE_ID = "workflow.prompts"
PROMPT_FRONTEND_EXTENSIONS = (
    {"extension_id": "workflow.prompts.workbench-panel", "kind": "workbench-panel",
     "entrypoint": "workflow.prompts.workbench.panel", "component_id": None, "component_version": None,
     "binding": {"surface": "workbench", "slot": "panel", "target": {}}},
    {"extension_id": "workflow.prompts.node-fields-v2", "kind": "field-editor",
     "entrypoint": "workflow.prompts.workbench.node-fields-v2",
     "component_id": "prompts.global-reference", "component_version": "2",
     "binding": {"surface": "workbench", "slot": "node-fields",
                 "target": {"component_id": "prompts.global-reference", "component_version": "2"}}},
)


def _tool_definitions(tool_catalog: Mapping | None) -> dict:
    result = {}
    for identity, value in (tool_catalog or {}).items():
        require(type(identity) is tuple and len(identity) == 2
                and all(type(part) is str and bool(part) for part in identity),
                "graph_tool_catalog_invalid", "Tool catalog identities require name and exact version")
        value = getattr(value, "definition", value)
        if isinstance(value, Mapping) and "function" in value:
            value = value["function"]
        require(isinstance(value, Mapping) and type(value.get("description")) is str
                and isinstance(value.get("parameters"), Mapping),
                "graph_tool_catalog_invalid", "Tool catalog requires description and JSON parameter schema")
        Draft202012Validator.check_schema(value["parameters"])
        result[identity] = {"description": value["description"], "parameters": deepcopy(value["parameters"])}
    return result


def _assembly(config, inputs, context):
    materials = merge_lifecycle_prompt_materials(inputs.get("input", []))
    items = [{key: deepcopy(value) for key, value in item.items()
              if key not in ("lifecycle", "compaction", "origin_item_ids")}
             for item in materials["items"]]
    # Chat has one response and no persistent context lifetime. Keep identity,
    # presentation and source evidence while projecting its current materials.
    return {"output": assemble_prompt(
        [prompt_content(items), *inputs.get("raw_prompt", [])], inputs.get("current_input"),
        source_output_refs=[*context.input_artifact_refs("input"), *context.input_artifact_refs("raw_prompt")],
        current_input_refs=context.input_artifact_refs("current_input"))}


def create_prompt_package(*, tool_catalog: Mapping | None = None) -> CapabilityPackage:
    tools = _tool_definitions(tool_catalog)

    def register(host):
        for extension in PROMPT_FRONTEND_EXTENSIONS:
            host.register_frontend_extension(**extension, host_protocol_version=1)
        register_lifecycle_prompt_nodes(host, tools=tools)
        host.register_node(NodeDefinition(
            "prompts.assembly", "2", "Prompt assembly", "Prompt", {}, object_schema({}),
            inputs=(NodePort("input", PROMPT_MATERIALS_TYPE, required=False, multiple=True),
                    NodePort("raw_prompt", "PROMPT", data_schema_version=2, required=False, multiple=True),
                    NodePort("current_input", "TEXT", data_schema_version=2, required=False)),
            outputs=(NodePort("output", "PROMPT", data_schema_version=2),),
            input_storage="references"), _assembly)

    return CapabilityPackage(PackageManifest(
        PROMPT_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.content", "1.0.0"),),
        exports={
            "data_types": [{"scope": "global", "type_id": PROMPT_RESOURCE_TYPE, "schema_version": 2}],
            "nodes": [{"component_id": "prompts." + name, "component_version": "2"}
                      for name in (*LIFECYCLE_PROMPT_COMPONENTS, "assembly")],
            "frontend_extensions": [{"extension_id": row["extension_id"]} for row in PROMPT_FRONTEND_EXTENSIONS],
        }), register)
