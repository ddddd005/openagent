"""One visible tool loader, one paired structured prompt, one context input.

The second round keeps the first-round variable semantics and real execution
targets.  This package only adds the merged loading node and the context
assembly version that consumes its paired output; the existing
``workflow.tool-bindings@1.0.0`` package stays unchanged for saved projects.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import object_schema, presentation_schema, validate_presentation
from .context_v4_nodes import register_native_context_v6
from .graph_contracts import NodeDefinition, NodePort, require
from .tool_binding_package import ADAPTER, variable_tool_schema
from .tool_prompt_contract import (
    TOOL_PROMPT_KIND, TOOL_PROMPT_SCHEMA_VERSION, TOOL_PROMPT_TYPE,
    tool_prompt_definition, validate_tool_prompt,
)


PACKAGE_ID = "workflow.tool-prompts"
PACKAGE_VERSION = "1.0.0"
LOADER_COMPONENT = "tool-bindings.variable-tools"
LOADER_VERSION = "2"
_AGENT_TARGET = ({"component_id": "agents.execute", "component_version": "10"},)
_TOOL_PROMPT_OUTPUT = ("tool_prompt", TOOL_PROMPT_TYPE, TOOL_PROMPT_SCHEMA_VERSION)


def tool_loader_config() -> dict:
    return {
        "agent_node_id": "", "variables": [],
        "list_presentation": {"role": "system", "placement": "before",
                              "depth": None, "order": 0, "enabled": True},
        "parameters_presentation": {"role": "system", "placement": "after",
                                    "depth": None, "order": 0, "enabled": True},
    }


def tool_loader_schema() -> dict:
    properties = deepcopy(variable_tool_schema()["properties"])
    properties.update({"list_presentation": presentation_schema(),
                       "parameters_presentation": presentation_schema()})
    return object_schema(properties)


def _validate_tool_loader(config):
    require(type(config.get("agent_node_id")) is str and bool(config["agent_node_id"]),
            "tool_binding_agent_missing", "Tool loader requires an explicit Agent node")
    from .tool_binding_package import _normalize
    _normalize(config)
    validate_presentation(config["list_presentation"])
    validate_presentation(config["parameters_presentation"])


def _tool_prompt(config, inputs, context):
    specs = context.bound_tool_prompt(config["agent_node_id"])
    return {"tool_prompt": validate_tool_prompt({
        "schema_version": TOOL_PROMPT_SCHEMA_VERSION, "kind": TOOL_PROMPT_KIND,
        "agent_node_id": config["agent_node_id"],
        "source": {"anchor_node_id": context.node_binding_id,
                   "component_id": context.definition.component_id,
                   "component_version": context.definition.component_version},
        "list_presentation": config["list_presentation"],
        "parameters_presentation": config["parameters_presentation"],
        "entries": [{"binding_id": spec["binding_id"], "name": spec["name"],
                     "operation": spec["operation"], "description": spec["description"],
                     "parameters": spec["parameters"]} for spec in specs],
    })}


def create_tool_prompt_package() -> CapabilityPackage:
    """Return the merged loading node and its context assembly consumer."""

    adapter = replace(ADAPTER, prompt_output=_TOOL_PROMPT_OUTPUT)

    def register(host):
        host.register_data_type(tool_prompt_definition())
        register_native_context_v6(host)
        host.register_node(NodeDefinition(
            LOADER_COMPONENT, LOADER_VERSION, "变量工具加载", "变量",
            tool_loader_config(), tool_loader_schema(),
            outputs=(NodePort(_TOOL_PROMPT_OUTPUT[0], TOOL_PROMPT_TYPE,
                              data_schema_version=TOOL_PROMPT_SCHEMA_VERSION),),
            input_storage="references",
            config_node_references=(
                {"config_field": "agent_node_id", "multiple": False,
                 "target_types": [deepcopy(item) for item in _AGENT_TARGET]},
            ),
        ), _tool_prompt, config_validator=_validate_tool_loader, tool_binding=adapter)

    return CapabilityPackage(PackageManifest(
        PACKAGE_ID, PACKAGE_VERSION,
        dependencies=(PackageDependency("workflow.content", "1.0.0"),
                      PackageDependency("workflow.tools", "1.0.0"),
                      PackageDependency("workflow.context", "1.0.0"),
                      PackageDependency("workflow.agents", "1.0.0")),
        exports={
            "data_types": [{"scope": "content", "type_id": TOOL_PROMPT_TYPE,
                            "schema_version": TOOL_PROMPT_SCHEMA_VERSION}],
            "nodes": [{"component_id": LOADER_COMPONENT, "component_version": LOADER_VERSION},
                      {"component_id": "context.assembly", "component_version": "6"}],
        },
    ), register)
