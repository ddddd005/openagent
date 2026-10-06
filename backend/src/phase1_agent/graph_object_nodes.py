"""Generic object I/O nodes; registered types own all business semantics."""

from .graph_contracts import NodeDefinition, NodePort, json_value
from .host_sdk import DataTypeDefinition


def _read(config, inputs, context):
    return {"value": json_value(context.object_read(config["object_key"])["value"])}


def _write(config, inputs, context):
    basis = context.object_read(config["object_key"])
    value = inputs["value"]["value"] if "value" in inputs else config["value"]
    context.object_write(config["object_key"], value, expected_revision=basis["revision"])
    return {"value": json_value(value)}


def _delete(config, inputs, context):
    basis = context.object_read(config["object_key"])
    context.object_delete(config["object_key"], expected_revision=basis["revision"])
    return {"value": json_value({"object_key": config["object_key"], "deleted": True})}


def register_object_nodes(registry):
    registry.data_types.register(DataTypeDefinition("workflow.json-object", 1, {}, default_value={}))
    key = {"type": "string", "minLength": 1, "maxLength": 128}
    for name, display, executor, write in (
        ("object-read", "会话对象读取", _read, False),
        ("object-write", "会话对象更新", _write, True),
        ("object-delete", "会话对象删除", _delete, False),
    ):
        fields = {"object_key": key, **({"value": {}} if write else {})}
        registry.register(NodeDefinition(
            "workflow." + name, "1", display, "会话",
            {"object_key": "shared/main", **({"value": {}} if write else {})},
            {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False},
            inputs=(NodePort("value", "JSON", required=False),) if write else (),
            outputs=(NodePort("value", "JSON"),),
            capabilities=("objects:read", "objects:write") if name != "object-read" else ("objects:read",),
        ), executor)
