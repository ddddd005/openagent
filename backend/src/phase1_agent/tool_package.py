"""General content tools and explicit session-backed scalar variables.

The package only depends on the public host registration, execution context and
content contracts. Registration/assignment settle object writes without any
content output; graph roots and control edges determine when they run.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import (
    default_presentation, json_content, make_prompt_item, presentation_schema,
    prompt_content, text_content,
)
from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .graph_contracts import NodeDefinition, NodePort
from .host_sdk import DataTypeDefinition, ensure
from .prompt_regex import RegexLimits, regex_replace_many, regex_rule_from_dict


PACKAGE_ID = "workflow.tools"
PACKAGE_VERSION = "1.0.0"
VARIABLE_TYPE = "workflow.variable"
VARIABLE_SCHEMA_VERSION = 1
WIRE_BYTES = 4_000_000
VARIABLE_TYPES = ("string", "integer", "number", "boolean")
_CONTENT_TYPES = {"text": "TEXT", "prompt": "PROMPT", "json": "JSON"}
_TOKEN = re.compile(r"\{\{([^{}]+)\}\}")
_IDENTITY = {"type": "string", "minLength": 1, "maxLength": 128}
_STRING = {"type": "string"}
_SCALAR = {"type": ["string", "integer", "number", "boolean"]}
_OBJECT_KEYS = {"type": "array", "items": _IDENTITY, "uniqueItems": True, "maxItems": 1024}
_EMPTY_VARIABLE = {
    "registered": False, "name": None, "value_type": None,
    "assigned": False, "value": None,
}


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _port(name: str, family: str, *, required: bool = True) -> NodePort:
    return NodePort(name, family, required=required, data_schema_version=2)


def _mode_ports(*, source: bool = False) -> dict:
    return {mode: (() if source else (_port("input", family),), (_port("output", family),))
            for mode, family in _CONTENT_TYPES.items()}


def _variable_name(value: Any) -> None:
    ensure(type(value) is str and 0 < len(value) <= 128
           and (value[0].isalpha() or value[0] == "_")
           and all(character.isalnum() or character == "_" for character in value),
           "variable_invalid_name", "Variable name must be a Unicode identifier")


def _typed_value(value_type: str, value: Any) -> None:
    accepted = {"string": type(value) is str, "integer": type(value) is int,
                "number": type(value) in (int, float), "boolean": type(value) is bool}
    ensure(value_type in VARIABLE_TYPES and accepted[value_type],
           "variable_type_mismatch", "Variable value differs from its registered scalar type")
    if value_type == "integer":
        ensure(-(2**53 - 1) <= value <= 2**53 - 1,
               "variable_type_mismatch", "Integer exceeds the interoperable safe range")
    try:
        validate_json_value(value)
    except ContractValidationError as exc:
        ensure(False, "variable_type_mismatch", str(exc))


def _validate_variable(value: Any) -> None:
    ensure(type(value) is dict and set(value) == set(_EMPTY_VARIABLE)
           and type(value["registered"]) is bool and type(value["assigned"]) is bool,
           "variable_invalid_state", "Variable object fields are invalid")
    if not value["registered"]:
        ensure(value == _EMPTY_VARIABLE, "variable_invalid_state",
               "An unregistered variable must remain an empty object shell")
        return
    _variable_name(value["name"])
    ensure(value["value_type"] in VARIABLE_TYPES, "variable_invalid_state",
           "Registered variable type is unavailable")
    if value["assigned"]:
        _typed_value(value["value_type"], value["value"])
    else:
        ensure(value["value"] is None, "variable_invalid_state",
               "An unassigned variable cannot hold a value")


def _read_variable(context, object_key: str) -> dict:
    item = context.object_read(object_key)
    ensure(item["type_id"] == VARIABLE_TYPE and item["schema_version"] == VARIABLE_SCHEMA_VERSION,
           "variable_binding_type_mismatch", "Variable nodes require a workflow.variable@1 object")
    _validate_variable(item["value"])
    return item


def _presentation(config: dict) -> None:
    value = config["presentation"]
    ensure(type(value["depth"]) is int and value["depth"] >= 0
           if value["placement"] == "middle" else value["depth"] is None,
           "graph_invalid_config", "Depth only applies to middle placement")


def _text(config, inputs, context):
    return {"output": text_content(config["text"])}


def _external_text(value: Any) -> dict:
    if type(value) is str:
        return text_content(value)
    ensure(type(value) is dict and set(value) == {"schema_version", "kind", "text"}
           and type(value["schema_version"]) is int and value["schema_version"] == 2
           and value["kind"] == "workflow.text" and type(value["text"]) is str,
           "graph_invalid_external_input", "Current input requires plain text or TEXT@2")
    return text_content(value["text"])


def _current_input(config, inputs, context):
    return {"output": context.validate_content(
        _external_text(context.external_input(config["input_name"])), "TEXT", 2)}


def _validate_external(config, inputs):
    ensure(config["input_name"] in inputs, "graph_external_input_missing",
           "Required external input is missing")
    _external_text(inputs[config["input_name"]])


def _declare_external(config):
    return [{"name": config["input_name"], "data_type": "TEXT", "required": True}]


def _output(config, inputs, context):
    return {"output": context.validate_content(inputs["input"], _CONTENT_TYPES[config["mode"]], 2)}


def _text_to_prompt(config, inputs, context):
    item = make_prompt_item(
        context.node_binding_id, "content", inputs["input"]["text"], config["presentation"],
        source={"kind": "conversion", "node_binding_id": context.node_binding_id})
    return {"output": prompt_content([item] if item["enabled"] else [])}


def _prompt_to_text(config, inputs, context):
    value = inputs["input"]
    items = value["items"]
    ensure(not any(item["protected"] for item in items), "graph_protected_content",
           "Protected protocol facts require an explicit safe projection")
    texts = ([message["content"] for message in value["assembly"]["messages"]]
             if value["stage"] == "assembled"
             else [item["text"] for item in items if item["enabled"]])
    return {"output": text_content(config["separator"].join(texts))}


def _text_to_json(config, inputs, context):
    return {"output": json_content(loads_strict(inputs["input"]["text"]))}


def _json_to_text(config, inputs, context):
    return {"output": text_content(canonical_bytes(inputs["input"]["value"]).decode("utf-8"))}


def _json_to_prompt(config, inputs, context):
    return {"output": context.validate_content(inputs["input"]["value"], "PROMPT", 2)}


def _prompt_to_json(config, inputs, context):
    value = context.validate_content(inputs["input"], "PROMPT", 2)
    return {"output": json_content(value)}


def _regex_rule(config):
    return regex_rule_from_dict({
        "pattern": config["pattern"], "replacement": config["replacement"],
        "flags": "".join(config["flags"]), "mode": config["replaceMode"],
    })


def _regex(config, inputs, context):
    rule = _regex_rule(config)
    # Worker limits protect execution resources independently of variable data.
    limits = RegexLimits(max_input_chars=WIRE_BYTES, max_output_chars=WIRE_BYTES)
    operation = lambda texts: regex_replace_many(
        texts, rule, limits=limits, node_id=context.node_binding_id)
    return {"output": context.transform_content(
        inputs["input"], _CONTENT_TYPES[config["mode"]], 2, operation, scope=config["scope"])}


def _validate_register(config):
    _variable_name(config["name"])
    if config["has_initial"]:
        _typed_value(config["value_type"], config["initial_value"])


def _register(config, inputs, context):
    basis = _read_variable(context, config["object_key"])
    existing = basis["value"]
    if existing["registered"]:
        ensure(existing["name"] == config["name"] and existing["value_type"] == config["value_type"],
               "variable_registration_conflict", "Variable object is registered under another name or type")
        return {}
    value = {"registered": True, "name": config["name"], "value_type": config["value_type"],
             "assigned": config["has_initial"],
             "value": copy.deepcopy(config["initial_value"]) if config["has_initial"] else None}
    _validate_variable(value)
    context.object_write(config["object_key"], value, expected_revision=basis["revision"])
    return {}


def _assign(config, inputs, context):
    basis = _read_variable(context, config["object_key"])
    value = copy.deepcopy(basis["value"])
    ensure(value["registered"], "variable_not_registered", "Assignment needs a registered variable")
    incoming = inputs["value"]["value"] if "value" in inputs else config["value"]
    _typed_value(value["value_type"], incoming)
    if config["operation"] != "set":
        ensure(value["value_type"] in ("integer", "number") and value["assigned"],
               "variable_type_mismatch", "Arithmetic needs an assigned numeric variable")
        incoming = (value["value"] + incoming if config["operation"] == "add"
                    else value["value"] - incoming)
        _typed_value(value["value_type"], incoming)
    value.update(assigned=True, value=copy.deepcopy(incoming))
    context.object_write(config["object_key"], value, expected_revision=basis["revision"])
    return {}


def _scalar_text(value_type: str, value: Any) -> str:
    _typed_value(value_type, value)
    return value if value_type == "string" else canonical_bytes(value).decode("utf-8")


class _VariableResolver:
    """Resolve only explicitly authorized objects from the current run state."""

    def __init__(self, context, object_keys: list[str]):
        self._values: dict[str, dict] = {}
        for key in object_keys:
            entry = _read_variable(context, key)["value"]
            if not entry["registered"]:
                continue
            ensure(entry["name"] not in self._values, "variable_name_conflict",
                   "Authorized variable objects declare the same name")
            self._values[entry["name"]] = entry

    def text(self, name: str) -> str:
        ensure(name in self._values, "variable_not_registered",
               "Referenced variable is not registered in the authorized object set")
        entry = self._values[name]
        ensure(entry["assigned"], "variable_unassigned", "Referenced variable has not been assigned")
        return _scalar_text(entry["value_type"], entry["value"])


def _replace(config, inputs, context, *, selected: bool):
    resolver = _VariableResolver(context, config["object_keys"])
    names = set(config["names"]) if selected else None

    def operation(texts):
        def replace(match):
            name = match.group(1)
            return match.group(0) if names is not None and name not in names else resolver.text(name)
        return [_TOKEN.sub(replace, text) for text in texts]

    return {"output": context.transform_content(
        inputs["input"], _CONTENT_TYPES[config["mode"]], 2, operation, scope=config["scope"])}


def _replace_all(config, inputs, context):
    return _replace(config, inputs, context, selected=False)


def _replace_selected(config, inputs, context):
    return _replace(config, inputs, context, selected=True)


def _validate_selected(config):
    for name in config["names"]:
        _variable_name(name)


def _variable_to_content(config, inputs, context):
    value = _read_variable(context, config["object_key"])["value"]
    ensure(value["registered"], "variable_not_registered", "Content source needs a registered variable")
    ensure(value["assigned"], "variable_unassigned", "Content source needs an assigned variable")
    output = (text_content(_scalar_text(value["value_type"], value["value"]))
              if config["mode"] == "text" else json_content(value["value"]))
    return {"output": output}


def create_tool_package() -> CapabilityPackage:
    """Return the independently loadable general-tools capability package."""
    node_names = (
        "text", "current-input", "output", "regex", "text-to-prompt", "prompt-to-text",
        "text-to-json", "json-to-text", "json-to-prompt", "prompt-to-json",
        "variable-register", "variable-assign", "variable-replace-all",
        "variable-replace-selected", "variable-to-content",
    )

    def register(host):
        host.register_data_type(DataTypeDefinition(
            VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION,
            _schema({"registered": {"type": "boolean"}, "name": {"type": ["string", "null"]},
                     "value_type": {"enum": [None, *VARIABLE_TYPES]}, "assigned": {"type": "boolean"},
                     "value": {"type": ["string", "integer", "number", "boolean", "null"]}}),
            default_value=_EMPTY_VARIABLE, validator=_validate_variable, max_bytes=WIRE_BYTES,
        ))

        def install(name, display, category, config, fields, executor, *,
                    inputs=(), outputs=(), modes=None, capabilities=(), validator=None,
                    is_output=False, external_validator=None, external_declaration=None):
            accesses = ()
            if name in ("variable-register", "variable-assign", "variable-to-content",
                        "variable-replace-all", "variable-replace-selected"):
                collection = name.startswith("variable-replace")
                accesses = ({
                    "config_field": "object_keys" if collection else "object_key",
                    "multiple": collection,
                    "access": "read_write" if name in ("variable-register", "variable-assign") else "read",
                    "type_id": VARIABLE_TYPE, "schema_version": VARIABLE_SCHEMA_VERSION,
                },)
            host.register_node(NodeDefinition(
                "tools." + name, "1", display, category, config, _schema(fields),
                inputs=inputs, outputs=outputs, port_modes=modes or {},
                capabilities=capabilities, is_output=is_output, input_storage="references",
                object_accesses=accesses,
            ), executor, config_validator=validator, external_inputs_validator=external_validator,
                external_inputs_declaration=external_declaration)

        install("text", "文本", "通用工具", {"text": ""}, {"text": _STRING}, _text,
                outputs=(_port("output", "TEXT"),))
        install("current-input", "当前输入", "通用工具", {"input_name": "text"},
                {"input_name": _IDENTITY}, _current_input, outputs=(_port("output", "TEXT"),),
                capabilities=("external:read",), external_validator=_validate_external,
                external_declaration=_declare_external)
        install("output", "输出", "通用工具", {"mode": "text"},
                {"mode": {"enum": list(_CONTENT_TYPES)}}, _output,
                modes=_mode_ports(), is_output=True)
        install("regex", "正则", "通用工具", {
            "mode": "text", "scope": "body", "pattern": "", "replacement": "",
            "flags": [], "replaceMode": "all",
        }, {"mode": {"enum": list(_CONTENT_TYPES)}, "scope": {"enum": ["body", "all"]},
            "pattern": _STRING, "replacement": _STRING,
            "flags": {"type": "array", "uniqueItems": True, "items": {"enum": list("imsxa")}},
            "replaceMode": {"enum": ["first", "all"]}},
            _regex, modes=_mode_ports(), validator=_regex_rule)
        install("text-to-prompt", "文本转提示词", "格式转换",
                {"presentation": default_presentation()}, {"presentation": presentation_schema()},
                _text_to_prompt, inputs=(_port("input", "TEXT"),),
                outputs=(_port("output", "PROMPT"),), validator=_presentation)
        install("prompt-to-text", "提示词转文本", "格式转换",
                {"separator": "\n"}, {"separator": _STRING}, _prompt_to_text,
                inputs=(_port("input", "PROMPT"),), outputs=(_port("output", "TEXT"),))
        for name, display, executor, source, target in (
            ("text-to-json", "文本转 JSON", _text_to_json, "TEXT", "JSON"),
            ("json-to-text", "JSON 转文本", _json_to_text, "JSON", "TEXT"),
            ("json-to-prompt", "JSON 转提示词", _json_to_prompt, "JSON", "PROMPT"),
            ("prompt-to-json", "提示词转 JSON", _prompt_to_json, "PROMPT", "JSON"),
        ):
            install(name, display, "格式转换", {}, {}, executor,
                    inputs=(_port("input", source),), outputs=(_port("output", target),))
        install("variable-register", "变量注册", "变量", {
            "object_key": "variable/value", "name": "value", "value_type": "string",
            "has_initial": False, "initial_value": None,
        }, {"object_key": _IDENTITY, "name": _IDENTITY, "value_type": {"enum": list(VARIABLE_TYPES)},
            "has_initial": {"type": "boolean"},
            "initial_value": {"type": ["string", "integer", "number", "boolean", "null"]}},
            _register, capabilities=("objects:read", "objects:write"), validator=_validate_register)
        install("variable-assign", "变量赋值", "变量",
                {"object_key": "variable/value", "operation": "set", "value": ""},
                {"object_key": _IDENTITY, "operation": {"enum": ["set", "add", "subtract"]},
                 "value": _SCALAR}, _assign, inputs=(_port("value", "JSON", required=False),),
                capabilities=("objects:read", "objects:write"))
        replacement_config = {"mode": "text", "scope": "body", "object_keys": []}
        replacement_fields = {"mode": {"enum": list(_CONTENT_TYPES)},
                              "scope": {"enum": ["body", "all"]}, "object_keys": _OBJECT_KEYS}
        install("variable-replace-all", "全部变量转值", "变量", replacement_config,
                replacement_fields, _replace_all, modes=_mode_ports(), capabilities=("objects:read",))
        install("variable-replace-selected", "指定变量转值", "变量",
                {**replacement_config, "names": []},
                {**replacement_fields, "names": _OBJECT_KEYS}, _replace_selected,
                modes=_mode_ports(), capabilities=("objects:read",), validator=_validate_selected)
        install("variable-to-content", "变量转内容", "变量",
                {"object_key": "variable/value", "mode": "text"},
                {"object_key": _IDENTITY, "mode": {"enum": ["text", "json"]}},
                _variable_to_content,
                modes={mode: ((), (_port("output", family),))
                       for mode, family in _CONTENT_TYPES.items() if mode != "prompt"},
                capabilities=("objects:read",))

    return CapabilityPackage(PackageManifest(
        PACKAGE_ID, PACKAGE_VERSION, dependencies=(PackageDependency("workflow.content", "1.0.0"),),
        exports={
            "data_types": [{"scope": "session", "type_id": VARIABLE_TYPE,
                            "schema_version": VARIABLE_SCHEMA_VERSION}],
            "nodes": [{"component_id": "tools." + name, "component_version": "1"} for name in node_names],
        },
    ), register)
