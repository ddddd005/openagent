"""Prompt/context resource nodes installed through the ordinary registry."""

from copy import deepcopy
import json
from uuid import uuid4

from .graph_contracts import NodeDefinition, NodePort, prompt_value, require
from .graph_nodes import _schema, _UUID, _PRESENTATION, _DEFAULT_PRESENTATION, _instance
from .graph_prompt import merge_materials


def _context(config, inputs, context):
    return {"output": context.host_call("history:read", "context", config)}


def _global(config, inputs, context):
    record = context.host_call("resources:read", "global-content", config)
    context.reads.append({"kind": "global_content_read", "resource_id": record["resource_id"],
                          "revision": record["revision"]})
    items = [{"item_instance_id": _instance(context.node_binding_id, member["id"]),
              **{key: deepcopy(member[key]) for key in ("text", "role", "placement", "depth", "order", "enabled")},
              "purpose": "prompt", "protected": False,
              "source": {"kind": "global_content", "resource_id": record["resource_id"],
                         "revision": record["revision"], "item_id": member["id"]},
              "metadata": {"name": member["name"]}}
             for member in record["members"] if member["enabled"]]
    return {"output": prompt_value(items)}


def _global_dependencies(config):
    return [{"kind": "global-content", "resource_id": config["resource_id"]}]


def _current_global_reference(config):
    from .global_resources import GLOBAL_CONTENT_TYPE
    return {"envelope_version": 1, "scope": "workspace", "type_id": GLOBAL_CONTENT_TYPE,
            "resource_id": config["resource_id"]}


def _global_current(config, inputs, context):
    from .global_resources import global_resource_reference
    reference = _current_global_reference(config)
    record = context.host_call("resources:read", "current-global-resource", reference)
    require(record["value"]["enabled"], "global_content_disabled", "Global content is disabled")
    context.reads.append({"kind": "global_resource_read", "reference": deepcopy(reference)})
    return {"output": global_resource_reference(reference)}


def _global_current_dependencies(config):
    return [{"kind": "global-resource", "reference": _current_global_reference(config)}]


def _global_current_preflight(config, records):
    reference = _current_global_reference(config)
    record = next((record for record in records if all(
        record[key] == reference[key] for key in ("scope", "type_id", "resource_id"))), None)
    require(record is not None, "global_content_missing", "Global content is missing")
    require(record["value"]["enabled"], "global_content_disabled", "Global content is disabled")


def _assemble(config, inputs, context):
    return {"output": context.host_call("history:read", "assemble", {"config": config, "inputs": inputs})}


def _tool(config, inputs, context):
    from .graph_agent_runtime import graph_agent_tools
    tools = {tool.name: tool for tool in graph_agent_tools()}
    ref = config["toolRef"]
    require(ref["version"] == "1" and ref["name"] in tools,
            "graph_tool_not_registered", "Tool must name an installed callable")
    definition = tools[ref["name"]].definition["function"]
    outputs = {}
    for part, purpose in (("description", "tool-description"), ("schema", "tool-schema")):
        presentation = config[part]
        text = definition["description"] if part == "description" else json.dumps(definition["parameters"], ensure_ascii=False)
        item = {"item_instance_id": _instance(context.node_binding_id + ":" + part, config[part + "InstanceId"]), "text": text,
                **deepcopy(presentation), "purpose": purpose, "protected": False,
                "source": {"kind": "registered_tool", "tool_ref": ref,
                           "item_id": config[part + "ItemId"], "revision": config["revision"]},
                "metadata": {"name": ref["name"]}}
        outputs["tool-descriptions" if part == "description" else "tool-schemas"] = prompt_value(
            [item] if presentation["enabled"] else [])
    return outputs


def _tool_validator(config):
    from .graph_agent_runtime import graph_agent_tools
    require(config["toolRef"]["version"] == "1"
            and config["toolRef"]["name"] in {tool.name for tool in graph_agent_tools()},
            "graph_tool_not_registered", "Tool must name an installed callable")
    from .graph_nodes import _presentation
    for part in ("description", "schema"):
        _presentation({"presentation": config[part]})


def register_prompt_nodes(registry):
    def register(name, display, config, schema, execute, *, inputs=(), capabilities=(), outputs=None, version="1",
                 validator=None, resource_dependencies=None, resource_preflight_validator=None):
        registry.register(NodeDefinition("workflow." + name, version, display, "提示词", config, schema,
                          inputs=inputs, outputs=outputs or (NodePort("output", "PROMPT"),),
                          capabilities=capabilities), execute, config_validator=validator,
                          resource_dependencies_declaration=resource_dependencies,
                          resource_preflight_validator=resource_preflight_validator)
    default_id = "48c1b734-716b-4e1f-8bda-af158ab11bb2"
    register("context", "上下文", {"source_node_id": default_id}, _schema({"source_node_id": _UUID}),
             _context, capabilities=("history:read",))
    register("global-content", "全局提示词引用（legacy v1）", {"resource_id": default_id}, _schema({"resource_id": _UUID}),
             _global, capabilities=("resources:read",), resource_dependencies=_global_dependencies)
    register("global-content", "全局提示词当前引用", {"resource_id": default_id},
             _schema({"resource_id": _UUID}), _global_current,
             capabilities=("resources:read",), outputs=(NodePort("output", "GLOBAL_RESOURCE_REF"),),
             resource_dependencies=_global_current_dependencies,
             resource_preflight_validator=_global_current_preflight, version="2")
    limits = {"max_messages": {"type": "integer", "minimum": 1, "maximum": 4096},
              "max_total_chars": {"type": "integer", "minimum": 1, "maximum": 4_000_000}}
    register("prompt-assembly", "提示词装配", {"max_messages": 4096, "max_total_chars": 4_000_000},
             _schema(limits), _assemble,
             inputs=(NodePort("input", "PROMPT", required=False, multiple=True), NodePort("current_input", "TEXT")),
             capabilities=("history:read",))
    register("prompt-summary", "提示词汇总", {}, _schema({}),
             lambda config, inputs, context: {"output": merge_materials(inputs.get("input", []))},
             inputs=(NodePort("input", "PROMPT", required=False, multiple=True),), version="2")
    tool_config = {"toolRef": {"name": "inspect_text", "version": "1"}, "revision": 1,
                   **{part + suffix: default_id for part in ("description", "schema") for suffix in ("ItemId", "InstanceId")},
                   "description": deepcopy(_DEFAULT_PRESENTATION), "schema": deepcopy(_DEFAULT_PRESENTATION)}
    properties = {"toolRef": _schema({"name": {"type": "string"}, "version": {"const": "1"}}),
                  "revision": {"type": "integer", "minimum": 1},
                  **{part + suffix: _UUID for part in ("description", "schema") for suffix in ("ItemId", "InstanceId")},
                  "description": _PRESENTATION, "schema": _PRESENTATION}
    ports = (NodePort("tool-descriptions", "PROMPT"), NodePort("tool-schemas", "PROMPT"))
    register("tool", "工具说明与参数 schema", tool_config, _schema(properties), _tool,
             outputs=ports, validator=_tool_validator)
    register("tool-summary", "工具汇总", {}, _schema({}),
             lambda config, inputs, context: {port.port_id: merge_materials(inputs.get(port.port_id, [])) for port in ports},
             inputs=tuple(NodePort(port.port_id, "PROMPT", required=False, multiple=True) for port in ports), outputs=ports)
