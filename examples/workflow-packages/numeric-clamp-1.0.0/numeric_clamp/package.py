"""Bounded numeric computation with no session objects or external services."""

from copy import deepcopy
from math import isfinite

from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.content_contracts import (
    default_presentation, json_content, object_schema, presentation_schema, validate_presentation,
)
from phase1_agent.contract_errors import require
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.prompt_lifecycle import (
    PROMPT_MATERIALS_TYPE, lifecycle_prompt_content, make_lifecycle_prompt_item,
)
from phase1_agent.tool_binding_contract import PureToolBindingAdapter


PACKAGE_ID = "example.numeric-clamp"
VERSION = "1.0.0"
TARGET = "example.numeric-clamp"
LOADER = "example.numeric-clamp-tools"


def normalize(config):
    return [{
        "name": "numeric_clamp",
        "description": "Clamp a finite number to an inclusive minimum and maximum.",
        "parameters": object_schema({
            "value": {"type": "number", "description": "Finite number to clamp."},
            "minimum": {"type": "number", "description": "Inclusive lower bound."},
            "maximum": {"type": "number", "description": "Inclusive upper bound."},
        }),
        "target": {"component_id": TARGET, "component_version": "1"},
        "config": {},
    }]


def _validate_numbers(arguments):
    require(type(arguments) is dict and set(arguments) == {"value", "minimum", "maximum"},
            "numeric_clamp_arguments_invalid", "Clamp requires exactly value, minimum and maximum")
    require(all(type(value) in (int, float) and isfinite(value) for value in arguments.values()),
            "numeric_clamp_arguments_invalid", "Clamp arguments must be finite numbers")
    require(arguments["minimum"] <= arguments["maximum"],
            "numeric_clamp_bounds_invalid", "Clamp minimum must not exceed maximum")


def call(spec, arguments):
    _validate_numbers(arguments)
    return deepcopy(spec["config"]), {"input": json_content(deepcopy(arguments))}


def execute(config, inputs, context):
    arguments = inputs["input"]["value"]
    _validate_numbers(arguments)
    value = arguments["value"]
    bounded = max(arguments["minimum"], min(value, arguments["maximum"]))
    return {"output": json_content({"value": bounded, "clamped": bounded != value})}


def result(spec, outputs):
    return deepcopy(outputs["output"]["value"])


def prompt(config, inputs, context):
    items = []
    for spec in context.bound_tool_prompt(config["agent_node_id"]):
        for part, text, presentation in (
            ("description", spec["description"], config["description_presentation"]),
            ("schema", canonical_bytes(spec["parameters"]).decode("utf-8"),
             config["schema_presentation"]),
        ):
            identity = {"agent_node_id": config["agent_node_id"], "tool_name": spec["name"],
                        "binding_id": spec["binding_id"], "part": part}
            items.append(make_lifecycle_prompt_item(
                context.node_binding_id, spec["binding_id"] + ":" + part, text, presentation,
                purpose="tool-description" if part == "description" else "tool-schema",
                source={"kind": "bound_tool", **identity}, metadata=identity))
    return {"materials": lifecycle_prompt_content(items)}


def validate_loader(config):
    validate_presentation(config["description_presentation"])
    validate_presentation(config["schema_presentation"])


def register(host):
    host.register_node(NodeDefinition(
        TARGET, "1", "Numeric clamp", "Examples", {}, object_schema({}),
        inputs=(NodePort("input", "JSON", data_schema_version=2),),
        outputs=(NodePort("output", "JSON", data_schema_version=2),),
    ), execute)
    host.register_node(NodeDefinition(
        LOADER, "1", "Numeric clamp tools", "Examples",
        {"agent_node_id": "", "description_presentation": default_presentation(),
         "schema_presentation": {**default_presentation(), "placement": "after"}},
        object_schema({
            "agent_node_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "description_presentation": presentation_schema(),
            "schema_presentation": presentation_schema(),
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
