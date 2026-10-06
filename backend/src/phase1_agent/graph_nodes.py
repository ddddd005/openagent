"""First ordinary graph nodes, all installed through the public registry API."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_json import loads_strict
from .graph_contracts import (
    MAX_CONTENT_CHARS, GraphDiagnosticError, NodeDefinition, NodePort, NodeRegistry, json_value, prompt_value, require,
    text_value, validate_content_value,
)
from .graph_execution import NodeExecutionContext
from .preparation_program import validate_typed_value, validate_variable_name
from .prompt_regex import RegexLimits, regex_replace_many, regex_rule_from_dict
from .workbench_resources import CORE_SESSION_NOTE, validate_data_definition, validate_data_value


_UUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}
_TEXT = {"type": "string", "maxLength": 1_000_000}
_SCALAR = {"type": ["string", "integer", "number", "boolean"]}
_VARIABLE_TYPES = ["string", "integer", "number", "boolean"]
_PRESENTATION = {
    "type": "object", "additionalProperties": False,
    "required": ["role", "placement", "depth", "order", "enabled"],
    "properties": {
        "role": {"enum": ["system", "user", "assistant"]},
        "placement": {"enum": ["before", "middle", "after"]},
        "depth": {"type": ["integer", "null"], "minimum": 0},
        "order": {"type": "integer"}, "enabled": {"type": "boolean"},
    },
}
_DEFAULT_PRESENTATION = {"role": "system", "placement": "before", "depth": None, "order": 0, "enabled": True}
_DEFINITION_ID = "48c1b734-716b-4e1f-8bda-af158ab11bb2"


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


def _mode_ports() -> dict:
    return {mode: ((NodePort("input", family),), (NodePort("output", family),))
            for mode, family in (("text", "TEXT"), ("prompt", "PROMPT"))}


def _instance(node_id: str, member_id: str) -> str:
    return str(UUID(bytes=hashlib.sha256((node_id + ":" + member_id).encode("utf-8")).digest()[:16], version=4))


def _presentation(config: dict) -> None:
    value = config["presentation"]
    require(type(value["depth"]) is int and value["depth"] >= 0
            if value["placement"] == "middle" else value["depth"] is None,
            "graph_invalid_config", "Depth only applies to middle placement")


def _prompt_item(config: dict, context: NodeExecutionContext, *, member_id: str | None = None,
                 group: dict | None = None) -> dict:
    presentation = config["presentation"]
    return {
        "item_instance_id": _instance(context.node_binding_id, member_id or config["itemId"]),
        "text": config["text"], **copy.deepcopy(presentation), "purpose": "prompt",
        "source": {"kind": "configuration", "node_binding_id": context.node_binding_id,
                   "item_id": config["itemId"], "revision": config["revision"],
                   **({"group_id": group["groupId"], "group_revision": group["revision"]} if group else {})},
        "protected": False,
        "metadata": {"name": config.get("name", ""),
                     **({"group_instance_id": context.node_binding_id} if group else {})},
    }


def _execute_text(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    return {"output": text_value(config["text"])}


def _execute_current_input(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    value = context.external_input(config["input_name"])
    return {"output": text_value(value) if type(value) is str else validate_content_value(value, "TEXT")}


def _validate_external_input(config: dict, external_inputs: dict) -> None:
    name = config["input_name"]
    require(name in external_inputs, "graph_external_input_missing", "Required external input is missing", port_id=name)
    value = external_inputs[name]
    text_value(value) if type(value) is str else validate_content_value(value, "TEXT")


def _declare_external_input(config: dict) -> list[dict]:
    return [{"name": config["input_name"], "data_type": "TEXT", "required": True}]


def _execute_prompt_item(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    return {"output": prompt_value([_prompt_item(config, context)] if config["presentation"]["enabled"] else [])}


def _validate_group(config: dict) -> None:
    identities = [member["id"] for member in config["members"]]
    require(len(identities) == len(set(identities)), "graph_invalid_config", "Group member identities repeat")
    for member in config["members"]:
        _presentation(member)


def _execute_prompt_group(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    items = [_prompt_item(member, context, member_id=member["id"], group=config)
             for member in config["members"] if member["presentation"]["enabled"]] if config["enabled"] else []
    return {"output": prompt_value(items)}


def _execute_prompt_source(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    return {"output": validate_content_value(config["value"], "PROMPT")}


def _execute_summary(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    return {"output": prompt_value([copy.deepcopy(item)
                                    for value in inputs.get("input", []) for item in value["items"]])}


def _execute_text_to_prompt(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    item_config = {"itemId": context.node_binding_id, "revision": 1,
                   "text": inputs["input"]["text"], "presentation": config["presentation"]}
    return _execute_prompt_item(item_config, {}, context)


def _execute_prompt_to_text(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    items = inputs["input"]["items"]
    require(not any(item["protected"] for item in items), "graph_protected_content",
            "Protected protocol facts require an explicit safe projection", node_id=context.node_binding_id)
    return {"output": text_value(config["separator"].join(item["text"] for item in items if item["enabled"]))}


def _transform(config: dict, inputs: dict, context: NodeExecutionContext,
               operation: Callable[[list[str]], list[str]]) -> dict:
    value = copy.deepcopy(inputs["input"])
    if config["mode"] == "text":
        return {"output": text_value(operation([value["text"]])[0])}
    editable = [item for item in value["items"] if not item["protected"] and item["enabled"]]
    for item, text in zip(editable, operation([item["text"] for item in editable])):
        item["text"] = text
    value["stage"], value["assembly"] = "materials", None
    return {"output": validate_content_value(value, "PROMPT")}


def _rule(config: dict):
    return regex_rule_from_dict({"pattern": config["pattern"], "replacement": config["replacement"],
                                 "flags": "".join(config["flags"]), "mode": config["replaceMode"]})


def _execute_regex(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    return _transform(config, inputs, context, lambda texts: regex_replace_many(
        texts, _rule(config), limits=RegexLimits(), node_id=context.node_binding_id,
    ))


def _execute_variable_replace(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    def replace_batch(texts: list[str]) -> list[str]:
        processed, total = [], 0
        for text in texts:
            replaced = context.replace_variables(text)
            total += len(replaced)
            require(total <= MAX_CONTENT_CHARS, "macro_output_limit", "Macro batch exceeds its output limit",
                    node_id=context.node_binding_id)
            processed.append(replaced)
        return processed

    return _transform(config, inputs, context, replace_batch)


def _validate_register(config: dict) -> None:
    validate_variable_name(config["name"])
    if config["hasInitialValue"]:
        validate_typed_value(config["valueType"], config["initialValue"])


def _execute_register(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    initial, has_initial = config["initialValue"], config["hasInitialValue"]
    if "text" in inputs:
        require(config["valueType"] == "string", "variable_type_mismatch",
                "Text registration requires a string variable", node_id=context.node_binding_id)
        initial, has_initial = inputs["text"]["text"], True
    entry = context.register_variable(config["name"], config["valueType"], initial=initial, has_initial=has_initial)
    return {"text": text_value(context.variable_value_text(config["name"]))} if "value" in entry else {}


def _validate_assign(config: dict) -> None:
    validate_variable_name(config["name"])
    validate_typed_value(config["valueType"], config["value"])
    require(config["operation"] == "set" or config["valueType"] in ("integer", "number"),
            "variable_type_mismatch", "Numeric operation requires a numeric type")


def _execute_assign(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    value = inputs["text"]["text"] if "text" in inputs else config["value"]
    context.assign_variable(config["name"], value, operation=config["operation"])
    return {"text": text_value(context.variable_value_text(config["name"]))}


def _validate_shared_write(config: dict) -> None:
    definition = validate_data_definition(config["definition"])
    require(definition["writable"], "session_data_read_only", "Shared data declaration is read-only")
    # A connected text value is validated at execution; literal configuration is
    # still required to be valid so changing a connection cannot hide bad data.
    validate_data_value(definition, config["value"])


def _execute_shared_read(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    return {"json": json_value(context.read_shared(config["definition"]))}


def _execute_shared_write(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    definition, value = config["definition"], config["value"]
    if "text" in inputs:
        text = inputs["text"]["text"]
        if definition["schema"].get("type") == "string":
            value = text
        else:
            try:
                value = loads_strict(text)
            except (ValueError, ContractValidationError) as exc:
                raise GraphDiagnosticError("session_data_type_mismatch", "Input is not valid JSON") from exc
    context.write_shared(definition, value)
    return {"json": json_value(value)}


def _execute_json_to_text(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    value = inputs["input"]["value"]
    return {"output": text_value(value if type(value) is str else json.dumps(value, ensure_ascii=False, allow_nan=False))}


def _execute_output(config: dict, inputs: dict, context: NodeExecutionContext) -> dict:
    return {"output": copy.deepcopy(inputs["input"])}


def create_default_registry() -> NodeRegistry:
    registry = NodeRegistry()
    from .global_resources import register_global_content_type, register_global_resource_ref_type
    register_global_content_type(registry.data_types)
    register_global_resource_ref_type(registry.data_types)

    def install(name: str, display: str, category: str, default: dict, schema: dict,
                executor: Callable | None, *, inputs: tuple = (), outputs: tuple = (),
                is_output: bool = False, capabilities: tuple = (), validator: Callable | None = None,
                port_modes: dict | None = None, external_validator: Callable | None = None,
                external_declaration: Callable | None = None) -> None:
        registry.register(NodeDefinition(
            "workflow." + name, "1", display, category, default, schema,
            inputs, outputs, is_output, capabilities, port_modes=port_modes or {},
        ), executor, validator, external_inputs_validator=external_validator,
            external_inputs_declaration=external_declaration)

    text_port, prompt_port = NodePort("output", "TEXT"), NodePort("output", "PROMPT")
    install("text", "文本", "内容", {"text": ""}, _schema({"text": _TEXT}),
            _execute_text, outputs=(text_port,))
    install("current-input", "当前输入", "内容", {"input_name": "text"},
            _schema({"input_name": {"type": "string", "minLength": 1, "maxLength": 128}}),
            _execute_current_input, outputs=(text_port,), capabilities=("external:read",),
            external_validator=_validate_external_input, external_declaration=_declare_external_input)
    item_properties = {"itemId": _UUID, "revision": {"type": "integer", "minimum": 1},
                       "text": _TEXT, "presentation": _PRESENTATION}
    install("prompt-item", "提示词条目", "提示词", {
        "itemId": _DEFINITION_ID, "revision": 1, "text": "", "presentation": _DEFAULT_PRESENTATION,
    }, _schema(item_properties), _execute_prompt_item, outputs=(prompt_port,), validator=_presentation)
    group_member = _schema({"id": _UUID, "name": {"type": "string", "maxLength": 128}, **item_properties})
    install("prompt-group", "提示词条目组", "提示词", {
        "groupId": _DEFINITION_ID, "revision": 1, "enabled": True, "members": [],
    }, _schema({"groupId": _UUID, "revision": {"type": "integer", "minimum": 1},
                "enabled": {"type": "boolean"}, "members": {"type": "array", "maxItems": 128, "items": group_member}}),
        _execute_prompt_group, outputs=(prompt_port,), validator=_validate_group)
    install("prompt-source", "结构提示词材料", "提示词", {"value": prompt_value([])},
            _schema({"value": {"type": "object"}}), _execute_prompt_source,
            outputs=(prompt_port,), validator=lambda config: validate_content_value(config["value"], "PROMPT"))
    install("prompt-summary", "提示词汇总", "提示词", {}, _schema({}), _execute_summary,
            inputs=(NodePort("input", "PROMPT", required=False, multiple=True),), outputs=(prompt_port,))
    install("text-to-prompt", "文本转提示词", "转换", {"presentation": _DEFAULT_PRESENTATION},
            _schema({"presentation": _PRESENTATION}), _execute_text_to_prompt,
            inputs=(NodePort("input", "TEXT"),), outputs=(prompt_port,), validator=_presentation)
    install("prompt-to-text", "提示词转文本", "转换", {"separator": "\n"},
            _schema({"separator": _TEXT}), _execute_prompt_to_text,
            inputs=(NodePort("input", "PROMPT"),), outputs=(text_port,))
    mode = {"enum": ["text", "prompt"]}
    install("regex", "正则", "处理", {
        "mode": "text", "pattern": "", "replacement": "", "flags": [], "replaceMode": "all",
    }, _schema({"mode": mode, "pattern": _TEXT, "replacement": _TEXT,
                "flags": {"type": "array", "uniqueItems": True, "items": {"enum": list("imsxa")}},
                "replaceMode": {"enum": ["first", "all"]}}), _execute_regex,
        inputs=(NodePort("input", "TEXT"),), outputs=(text_port,), validator=_rule, port_modes=_mode_ports())
    variable_name = {"type": "string", "minLength": 1, "maxLength": 128}
    variable_type = {"enum": _VARIABLE_TYPES}
    install("variable-register", "变量注册", "变量", {
        "name": "variable", "valueType": "string", "hasInitialValue": False, "initialValue": None,
    }, _schema({"name": variable_name, "valueType": variable_type, "hasInitialValue": {"type": "boolean"},
                "initialValue": {"type": ["string", "integer", "number", "boolean", "null"]}}),
        _execute_register, inputs=(NodePort("text", "TEXT", required=False),),
        outputs=(NodePort("text", "TEXT", required=False),),
        capabilities=("variables:read", "variables:write"), validator=_validate_register)
    install("variable-assign", "变量赋值", "变量", {
        "name": "variable", "valueType": "string", "operation": "set", "value": "",
    }, _schema({"name": variable_name, "valueType": variable_type,
                "operation": {"enum": ["set", "add", "subtract"]}, "value": _SCALAR}),
        _execute_assign, inputs=(NodePort("text", "TEXT", required=False),),
        outputs=(NodePort("text", "TEXT"),), capabilities=("variables:read", "variables:write"),
        validator=_validate_assign)
    install("variable-replace", "变量替换", "变量", {"mode": "text"}, _schema({"mode": mode}),
            _execute_variable_replace, inputs=(NodePort("input", "TEXT"),), outputs=(text_port,),
            capabilities=("variables:read",), port_modes=_mode_ports())
    install("session-data-read", "会话数据读取", "会话", {"definition": CORE_SESSION_NOTE},
            _schema({"definition": {"type": "object"}}), _execute_shared_read,
            outputs=(NodePort("json", "JSON"),), capabilities=("shared:read",),
            validator=lambda config: validate_data_definition(config["definition"]))
    install("session-data-write", "会话数据写入", "会话", {"definition": CORE_SESSION_NOTE, "value": ""},
            _schema({"definition": {"type": "object"}, "value": {}}), _execute_shared_write,
            inputs=(NodePort("text", "TEXT", required=False),), outputs=(NodePort("json", "JSON"),),
            capabilities=("shared:write",), validator=_validate_shared_write)
    install("json-to-text", "JSON 转文本", "转换", {}, _schema({}), _execute_json_to_text,
            inputs=(NodePort("input", "JSON"),), outputs=(text_port,))
    install("output", "输出", "输出", {"mode": "text"}, _schema({"mode": mode}),
            _execute_output, inputs=(NodePort("input", "TEXT"),), outputs=(text_port,),
            is_output=True, port_modes=_mode_ports())
    install("agent", "Agent 内核（待接入）", "Agent", {}, {"type": "object"}, None,
            inputs=(NodePort("prompt", "PROMPT"), NodePort("model", "MODEL_RESOURCE")), outputs=(prompt_port,))
    install("model-provider", "模型提供（待接入）", "资源", {}, {"type": "object"}, None,
            outputs=(NodePort("model", "MODEL_RESOURCE"),))
    from .graph_prompt_nodes import register_prompt_nodes
    register_prompt_nodes(registry)
    from .graph_agent_nodes import register_graph_agent_nodes
    register_graph_agent_nodes(registry)
    from .graph_object_nodes import register_object_nodes
    register_object_nodes(registry)
    return registry


def create_package_registry(*, packages=(), enabled: dict[str, str] | None = None):
    """Load the existing compatibility entry and explicitly enabled project packages."""
    from .capability_packages import CapabilityPackageLoader, create_compatibility_package

    compatibility = create_compatibility_package(create_default_registry())
    from .builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
    selected = dict(DEFAULT_PACKAGES) if enabled is None else {"workflow.compat": "1.0.0"}
    if enabled is not None:
        selected.update(enabled)
    supplied = tuple(packages)
    identities = {(package.manifest.package_id, package.manifest.version) for package in supplied}
    installed = tuple(package for package in builtin_capability_packages()
                      if (package.manifest.package_id, package.manifest.version) not in identities)
    return CapabilityPackageLoader((compatibility, *installed, *supplied)).load(selected)
