"""Explicit variable tool bindings and a matching prompt-material producer.

The anchor is a real non-working graph node.  Its configuration names one Agent
instance and the exact session variables that Agent may use.  The package owns
the variable-specific tool names, schemas, real target identities and argument
projection; the execution layer owns registry resolution, ACL, invocation and
per-call acceptance.
"""

from __future__ import annotations

from copy import deepcopy

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import (
    default_presentation, json_content, object_schema, presentation_schema,
    validate_presentation,
)
from .contract_json import canonical_bytes
from .graph_contracts import NodeDefinition, NodePort, require
from .prompt_lifecycle import (
    PROMPT_MATERIALS_TYPE, lifecycle_prompt_content, make_lifecycle_prompt_item,
)
from .tool_binding_contract import ToolBindingAdapter
from .variable_contract import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE, VARIABLE_TYPES


PACKAGE_ID = "workflow.tool-bindings"
PACKAGE_VERSION = "1.0.0"
ANCHOR_COMPONENT = "tool-bindings.variable-tools"
ANCHOR_VERSION = "1"
PROMPT_COMPONENT = "tool-bindings.tool-prompt"
PROMPT_VERSION = "1"
_AGENT_TARGET = ({"component_id": "agents.execute", "component_version": "10"},)
_DEFAULT_MEMBER = "59e405d0-debd-4d7a-aa7f-ce6445d72665"
_UUID = {"type": "string", "pattern":
         "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}
_OPERATIONS = ("read", "set", "add", "subtract")
_ALIAS = {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,54}$"}
_OPERATION_VALUE = {
    "set": "Set the variable to a new value.",
    "add": "Add to the current numeric value.",
    "subtract": "Subtract from the current numeric value.",
}


def _normalize(config):
    variables = config.get("variables")
    require(type(variables) is list and 0 < len(variables) <= 64,
            "tool_binding_invalid", "Variable tool anchor requires a bounded variable list")
    result, aliases = [], set()
    for entry in variables:
        require(type(entry) is dict and set(entry) == {"alias", "object_key", "operations"},
                "tool_binding_invalid", "Variable tool declaration fields differ")
        alias, object_key, operations = entry["alias"], entry["object_key"], entry["operations"]
        require(type(alias) is str and alias and alias not in aliases and alias != "final_answer",
                "tool_binding_alias_conflict", "Variable tool aliases must be unique and usable")
        require(type(object_key) is str and 0 < len(object_key.strip()) == len(object_key) <= 128,
                "tool_binding_invalid", "Variable tool requires an exact object key")
        require(type(operations) is list and 0 < len(operations) <= 4
                and all(type(operation) is str and operation in _OPERATIONS for operation in operations)
                and len(set(operations)) == len(operations),
                "tool_binding_operation_unavailable",
                "Variable tool operations require a unique nonempty subset")
        aliases.add(alias)
        result.append({"alias": alias, "object_key": object_key,
                       "operations": list(operations)})
    return result


def _schema(value_type):
    return {"type": "object", "properties": {
        "value": {"type": value_type, "description": "The variable value."}},
        "required": ["value"], "additionalProperties": False}


def _bind(declaration, item):
    require(item.get("type_id") == VARIABLE_TYPE and item.get("schema_version") == VARIABLE_SCHEMA_VERSION,
            "tool_binding_type_mismatch", "Variable tools require workflow.variable@1 objects")
    value = item["value"]
    require(type(value) is dict and value.get("registered"),
            "tool_binding_type_mismatch", "Variable tool binding requires a registered variable")
    value_type = value["value_type"]
    require(value_type in VARIABLE_TYPES, "tool_binding_type_mismatch",
            "Registered variable type is unavailable")
    operations = declaration["operations"]
    numeric = value_type in ("integer", "number")
    if any(operation in ("add", "subtract") for operation in operations):
        require(numeric, "tool_binding_operation_unavailable",
                "Arithmetic tools require an integer or number variable")
    alias, object_key = declaration["alias"], declaration["object_key"]
    specs = []
    for operation in operations:
        if operation == "read":
            specs.append({
                "name": alias + "_read", "operation": "read", "access": "read",
                "description": f"Read the variable '{value['name']}' and return its current value.",
                "parameters": {"type": "object", "properties": {}, "required": [],
                               "additionalProperties": False},
                "target": {"component_id": "tools.variable-to-content",
                           "component_version": "1"},
                "config": {"object_key": object_key, "mode": "json"},
            })
            continue
        specs.append({
            "name": alias + "_" + operation, "operation": operation, "access": "write",
            "description": f"{_OPERATION_VALUE[operation]} Variable '{value['name']}'.",
            "parameters": _schema(value_type),
            "target": {"component_id": "tools.variable-assign", "component_version": "1"},
            "config": {"object_key": object_key, "operation": operation, "value": ""},
        })
    return specs


def _call(spec, arguments):
    if spec["access"] == "read":
        return deepcopy(spec["config"]), {}
    return deepcopy(spec["config"]), {"value": json_content(arguments["value"])}


def _result(spec, outputs, item):
    value = item["value"]
    require(item.get("type_id") == VARIABLE_TYPE and isinstance(value, dict)
            and value.get("registered") and value.get("assigned"),
            "tool_binding_type_mismatch", "Variable tool result requires an assigned variable")
    return {"variable": value["name"], "object_key": spec["object_key"],
            "value": value["value"], "revision": item["revision"]}


ADAPTER = ToolBindingAdapter(normalize=_normalize, bind=_bind, call=_call, result=_result)


def variable_tool_config() -> dict:
    return {"agent_node_id": "", "variables": []}


def variable_tool_schema() -> dict:
    return object_schema({
        "agent_node_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "variables": {"type": "array", "maxItems": 64, "items": object_schema({
            "alias": _ALIAS,
            "object_key": {"type": "string", "minLength": 1, "maxLength": 128},
            "operations": {"type": "array", "minItems": 1, "maxItems": 4,
                           "uniqueItems": True, "items": {"enum": list(_OPERATIONS)}},
        })},
    })


def _validate_tool_bindings(config):
    require(type(config.get("agent_node_id")) is str and bool(config["agent_node_id"]),
            "tool_binding_agent_missing", "Variable tool anchor requires an explicit Agent node")
    _normalize(config)


def prompt_tool_config() -> dict:
    return {
        "agent_node_id": "", "tool_name": "",
        "description": default_presentation(), "schema": default_presentation(),
        "description_member_id": _DEFAULT_MEMBER, "schema_member_id": _DEFAULT_MEMBER,
    }


def prompt_tool_schema() -> dict:
    return object_schema({
        "agent_node_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "tool_name": {"type": "string", "minLength": 1, "maxLength": 128},
        "description": presentation_schema(), "schema": presentation_schema(),
        "description_member_id": _UUID, "schema_member_id": _UUID,
    })


def _validate_prompt_tool(config):
    require(type(config.get("agent_node_id")) is str and bool(config["agent_node_id"]),
            "tool_binding_agent_missing", "Bound tool prompt requires an explicit Agent node")
    require(type(config.get("tool_name")) is str and bool(config["tool_name"]),
            "tool_binding_not_found", "Bound tool prompt requires an exact tool name")
    validate_presentation(config["description"])
    validate_presentation(config["schema"])


def _prompt_tool(config, inputs, context):
    specs = context.bound_tool_specs(config["agent_node_id"])
    spec = next((item for item in specs if item.get("name") == config["tool_name"]), None)
    require(spec is not None, "tool_binding_not_found",
            "Bound tool prompt names no prepared tool for this Agent")
    result = {}
    for part, purpose, port in (
        ("description", "tool-description", "tool-descriptions"),
        ("schema", "tool-schema", "tool-schemas"),
    ):
        text = (spec["description"] if part == "description"
                else canonical_bytes(spec["parameters"]).decode("utf-8"))
        result[port] = lifecycle_prompt_content([make_lifecycle_prompt_item(
            context.node_binding_id + ":" + part, config[part + "_member_id"], text,
            config[part], purpose=purpose,
            source={"kind": "bound_tool", "agent_node_id": config["agent_node_id"],
                    "tool_name": config["tool_name"], "part": part},
            metadata={"tool_name": config["tool_name"]},
        )])
    return result


def create_tool_binding_package() -> CapabilityPackage:
    """Return the explicitly loadable variable tool binding package."""

    def register(host):
        host.register_node(NodeDefinition(
            ANCHOR_COMPONENT, ANCHOR_VERSION, "变量工具绑定", "变量",
            variable_tool_config(), variable_tool_schema(),
            execution_class="non_working",
            config_node_references=(
                {"config_field": "agent_node_id", "multiple": False,
                 "target_types": [deepcopy(item) for item in _AGENT_TARGET]},
            ),
        ), None, config_validator=_validate_tool_bindings, tool_binding=ADAPTER)
        host.register_node(NodeDefinition(
            PROMPT_COMPONENT, PROMPT_VERSION, "工具提示词 · 已绑定", "提示词",
            prompt_tool_config(), prompt_tool_schema(),
            outputs=(NodePort("tool-descriptions", PROMPT_MATERIALS_TYPE),
                     NodePort("tool-schemas", PROMPT_MATERIALS_TYPE)),
            input_storage="references",
            config_node_references=(
                {"config_field": "agent_node_id", "multiple": False,
                 "target_types": [deepcopy(item) for item in _AGENT_TARGET]},
            ),
        ), _prompt_tool, config_validator=_validate_prompt_tool)

    return CapabilityPackage(PackageManifest(
        PACKAGE_ID, PACKAGE_VERSION,
        dependencies=(PackageDependency("workflow.content", "1.0.0"),
                      PackageDependency("workflow.tools", "1.0.0"),
                      PackageDependency("workflow.agents", "1.0.0")),
        exports={"nodes": [
            {"component_id": ANCHOR_COMPONENT, "component_version": ANCHOR_VERSION},
            {"component_id": PROMPT_COMPONENT, "component_version": PROMPT_VERSION},
        ]},
    ), register)
