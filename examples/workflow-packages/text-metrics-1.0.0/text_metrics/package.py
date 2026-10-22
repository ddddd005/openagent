"""Pure computation tools supplied through a manually placed local package."""

from copy import deepcopy

from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.content_contracts import (
    default_presentation, json_content, object_schema, presentation_schema,
    text_content, validate_presentation,
)
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.prompt_lifecycle import (
    PROMPT_MATERIALS_TYPE, lifecycle_prompt_content, make_lifecycle_prompt_item,
)
from phase1_agent.tool_binding_contract import PureToolBindingAdapter


PACKAGE_ID = "example.text-metrics"
VERSION = "1.0.0"
TARGET = "example.text-metrics"
LOADER = "example.text-metrics-tools"


def normalize(config):
    return [{
        "name": "text_metrics",
        "description": "Count characters and lines in text, optionally excluding spaces.",
        "parameters": object_schema({
            "text": {"type": "string", "description": "Text to inspect."},
            "include_spaces": {"type": "boolean", "description": "Include space characters."},
        }),
        "target": {"component_id": TARGET, "component_version": "1"},
        "config": {"include_spaces": True},
    }]


def call(spec, arguments):
    return {"include_spaces": arguments["include_spaces"]}, {"input": text_content(arguments["text"])}


def result(spec, outputs):
    return deepcopy(outputs["output"]["value"])


def execute(config, inputs, context):
    text = inputs["input"]["text"]
    counted = text if config["include_spaces"] else text.replace(" ", "")
    return {"output": json_content({"characters": len(counted), "lines": len(text.splitlines())})}


def prompt(config, inputs, context):
    specs = context.bound_tool_prompt(config["agent_node_id"])
    items = []
    for spec in specs:
        for part, purpose, presentation, text in (
            ("description", "tool-description", config["list_presentation"], spec["description"]),
            ("parameters", "tool-schema", config["parameters_presentation"],
             canonical_bytes(spec["parameters"]).decode("utf-8")),
        ):
            items.append(make_lifecycle_prompt_item(
                context.node_binding_id, spec["binding_id"] + ":" + part, text, presentation,
                purpose=purpose,
                source={"kind": "bound_tool", "agent_node_id": config["agent_node_id"],
                        "tool_name": spec["name"], "binding_id": spec["binding_id"], "part": part},
                metadata={"agent_node_id": config["agent_node_id"], "tool_name": spec["name"],
                          "binding_id": spec["binding_id"], "part": part},
            ))
    return {"materials": lifecycle_prompt_content(items)}


def validate_loader(config):
    validate_presentation(config["list_presentation"])
    validate_presentation(config["parameters_presentation"])


def register(host):
    host.register_node(NodeDefinition(
        TARGET, "1", "Text metrics", "Examples", {"include_spaces": True},
        object_schema({"include_spaces": {"type": "boolean"}}),
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "JSON", data_schema_version=2),),
    ), execute)
    host.register_node(NodeDefinition(
        LOADER, "1", "Text metrics tools", "Examples",
        {"agent_node_id": "", "list_presentation": default_presentation(),
         "parameters_presentation": {**default_presentation(), "placement": "after"}},
        object_schema({
            "agent_node_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "list_presentation": presentation_schema(),
            "parameters_presentation": presentation_schema(),
        }),
        outputs=(NodePort("materials", PROMPT_MATERIALS_TYPE),),
        input_storage="references",
        config_node_references=({
            "config_field": "agent_node_id", "multiple": False,
            "target_types": [{"component_id": "agents.execute", "component_version": "10"}],
        },),
    ), prompt, config_validator=validate_loader, tool_binding=PureToolBindingAdapter(
        normalize=normalize, call=call, result=result,
        prompt_output=("materials", PROMPT_MATERIALS_TYPE, 1)))


def create_package():
    return CapabilityPackage(PackageManifest(
        PACKAGE_ID, VERSION,
        dependencies=(PackageDependency("workflow.content", "1.0.0"),
                      PackageDependency("workflow.agents", "1.0.0")),
        exports={"nodes": [
            {"component_id": TARGET, "component_version": "1"},
            {"component_id": LOADER, "component_version": "1"},
        ]},
    ), register)
