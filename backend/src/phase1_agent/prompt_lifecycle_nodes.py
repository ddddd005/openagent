"""Version-two prompt producers explicitly opt into lifetime-aware materials."""

from copy import deepcopy

from .content_contracts import default_presentation, object_schema, presentation_schema, validate_presentation
from .contract_json import canonical_bytes, validate_json_value
from .global_resources import global_resource_reference, validate_global_resource_reference
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import DataTypeDefinition, ResourceIdentity
from .prompt_contract import check_prompt_schema
from .prompt_lifecycle import (
    PROMPT_MATERIALS_TYPE, lifecycle_fields_schema, lifecycle_prompt_content,
    make_lifecycle_prompt_item, merge_lifecycle_prompt_materials, validate_prompt_lifecycle,
)


PROMPT_RESOURCE_TYPE = "workflow.prompt-resource"
_DEFAULT_MEMBER = "59e405d0-debd-4d7a-aa7f-ce6445d72665"
_UUID = {"type": "string", "pattern":
         "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}
LIFECYCLE_PROMPT_COMPONENTS = (
    "item", "group", "source", "summary", "global-reference", "global-resolve", "tool", "tool-summary",
)


def lifecycle_member_schema() -> dict:
    return object_schema({
        "id": _UUID, "text": {"type": "string"}, "presentation": presentation_schema(),
        "metadata": {"type": "object"}, **lifecycle_fields_schema(),
    })


def lifecycle_members_schema() -> dict:
    return {"type": "array", "maxItems": 1024, "items": lifecycle_member_schema()}


def validate_lifecycle_members(members) -> None:
    check_prompt_schema(members, lifecycle_members_schema(), "graph_prompt_resource_invalid",
                        "Lifetime-aware prompt members are invalid")
    require(len({member["id"] for member in members}) == len(members),
            "graph_prompt_member_conflict", "A prompt resource cannot repeat stable member identities")
    for member in members:
        validate_presentation(member["presentation"])
        validate_prompt_lifecycle(member["lifecycle"], member["compaction"], role=member["presentation"]["role"])


def validate_lifecycle_prompt_resource(value) -> dict:
    validate_json_value(value)
    validate_schema = object_schema({"enabled": {"type": "boolean"}, "members": lifecycle_members_schema()})
    check_prompt_schema(value, validate_schema, "graph_prompt_resource_invalid",
                        "Lifetime-aware prompt resources require enabled and members")
    validate_lifecycle_members(value["members"])
    return deepcopy(value)


def _reference_config(config):
    reference = ResourceIdentity.from_dict(config["reference"]).to_dict()
    require(reference["type_id"] == PROMPT_RESOURCE_TYPE, "graph_prompt_resource_type_mismatch",
            "Prompt references require the current prompt resource type")


def _reference_preflight(config, records):
    reference = config["reference"]
    record = next((record for record in records if all(
        record.get(key) == reference[key] for key in ("scope", "type_id", "resource_id"))), None)
    require(record is not None, "global_resource_missing", "Prompt resource is missing")
    require(record["data_schema_version"] == 2, "graph_prompt_resource_type_mismatch",
            "Version-two prompt producers require explicit resource schema two")
    require(validate_lifecycle_prompt_resource(record["value"])["enabled"],
            "global_content_disabled", "Prompt resource is disabled")


def _members(config, context, *, kind):
    items = [make_lifecycle_prompt_item(
        context.node_binding_id, member["id"], member["text"], member["presentation"],
        lifecycle=member["lifecycle"], compaction=member["compaction"],
        source={"kind": kind, "node_id": context.node_binding_id, "member_id": member["id"]},
        metadata=member["metadata"],
    ) for member in config["members"]]
    return merge_lifecycle_prompt_materials([lifecycle_prompt_content(items)])


def register_lifecycle_prompt_nodes(host, *, tools) -> None:
    host.register_data_type(DataTypeDefinition(
        PROMPT_RESOURCE_TYPE, 2,
        object_schema({"enabled": {"type": "boolean"}, "members": lifecycle_members_schema()}),
        {"enabled": True, "members": []}, scope="global", validator=validate_lifecycle_prompt_resource,
    ))
    material_port = lambda name="output", **kwargs: NodePort(name, PROMPT_MATERIALS_TYPE, **kwargs)
    member = {
        "id": _DEFAULT_MEMBER, "text": "", "presentation": default_presentation(),
        "metadata": {}, "lifecycle": "per_request", "compaction": "never",
    }

    def node(name, title, config, schema, execute, *, inputs=(), outputs=None,
             capabilities=(), validator=None, **hooks):
        host.register_node(NodeDefinition(
            "prompts." + name, "2", title, "提示词", config, schema,
            inputs=inputs, outputs=(material_port(),) if outputs is None else outputs,
            capabilities=capabilities, input_storage="references",
        ), execute, config_validator=validator, **hooks)

    node("item", "提示词条目 · 生命周期", deepcopy(member), lifecycle_member_schema(),
         lambda config, inputs, context: {"output": _members({"members": [config]}, context, kind="prompt_item")},
         validator=lambda config: validate_lifecycle_members([config]))
    node("group", "提示词内容组 · 生命周期", {"members": [deepcopy(member)]},
         object_schema({"members": lifecycle_members_schema()}),
         lambda config, inputs, context: {"output": _members(config, context, kind="prompt_group")},
         validator=lambda config: validate_lifecycle_members(config["members"]))

    def source_validate(config):
        merge_lifecycle_prompt_materials([config["value"]])
        validate_presentation(config["presentation"])
        validate_prompt_lifecycle(config["lifecycle"], config["compaction"], role=config["presentation"]["role"])

    def source_execute(config, inputs, context):
        values = [deepcopy(config["value"])]
        if "input" in inputs:
            values.append(lifecycle_prompt_content([make_lifecycle_prompt_item(
                context.node_binding_id, config["member_id"], inputs["input"]["text"], config["presentation"],
                lifecycle=config["lifecycle"], compaction=config["compaction"],
                source={"kind": "prompt_increment", "node_id": context.node_binding_id,
                        "member_id": config["member_id"]},
            )]))
        return {"output": merge_lifecycle_prompt_materials(values)}

    node("source", "提示词材料与增量 · 生命周期", {
        "value": lifecycle_prompt_content([]), "member_id": _DEFAULT_MEMBER,
        "presentation": default_presentation(), "lifecycle": "per_request", "compaction": "never",
    }, object_schema({
        "value": {"type": "object"}, "member_id": _UUID, "presentation": presentation_schema(),
        **lifecycle_fields_schema(),
    }), source_execute, inputs=(NodePort("input", "TEXT", data_schema_version=2, required=False),),
         validator=source_validate)
    node("summary", "提示词汇总 · 生命周期", {}, object_schema({}),
         lambda config, inputs, context: {"output": merge_lifecycle_prompt_materials(inputs.get("input", []))},
         inputs=(material_port("input", required=False, multiple=True),))

    reference = {"envelope_version": 1, "scope": "workspace", "type_id": PROMPT_RESOURCE_TYPE,
                 "resource_id": _DEFAULT_MEMBER}
    node("global-reference", "全局提示词引用 · 生命周期", {"reference": reference},
         object_schema({"reference": {"type": "object"}}),
         lambda config, inputs, context: {"output": global_resource_reference(config["reference"])},
         outputs=(NodePort("output", "GLOBAL_RESOURCE_REF"),), capabilities=("resources:read",),
         validator=_reference_config,
         resource_dependencies_declaration=lambda config: [
             {"kind": "global-resource", "reference": deepcopy(config["reference"])}],
         resource_preflight_validator=_reference_preflight)

    def resolve(config, inputs, context):
        reference = validate_global_resource_reference(inputs["input"])["reference"]
        _reference_config({"reference": reference})
        record = context.host_call("resources:read", "current-global-resource", reference)
        _reference_preflight({"reference": reference}, [record])
        items = [make_lifecycle_prompt_item(
            context.node_binding_id, item["id"], item["text"], item["presentation"],
            lifecycle=item["lifecycle"], compaction=item["compaction"],
            source={"kind": "global_prompt", "reference": deepcopy(reference), "member_id": item["id"]},
            metadata=item["metadata"],
        ) for item in record["value"]["members"]]
        context.reads.append({"kind": "global_resource_read", "reference": deepcopy(reference)})
        return {"output": merge_lifecycle_prompt_materials([lifecycle_prompt_content(items)])}

    node("global-resolve", "全局提示词解析 · 生命周期", {}, object_schema({}), resolve,
         inputs=(NodePort("input", "GLOBAL_RESOURCE_REF"),), capabilities=("resources:read",),
         resource_input_ports=("input",))

    tool_ref = {"name": sorted(tools)[0][0] if tools else "inspect_text",
                "version": sorted(tools)[0][1] if tools else "1"}
    tool_config = {"tool_ref": tool_ref, "description": default_presentation(), "schema": default_presentation(),
                   "description_member_id": _DEFAULT_MEMBER, "schema_member_id": _DEFAULT_MEMBER}

    def validate_tool(config):
        require((config["tool_ref"]["name"], config["tool_ref"]["version"]) in tools,
                "graph_tool_not_registered", "Tool material requires an installed exact tool version")
        for part in ("description", "schema"):
            validate_presentation(config[part])

    def tool_execute(config, inputs, context):
        definition = tools[(config["tool_ref"]["name"], config["tool_ref"]["version"])]
        result = {}
        for part, purpose, port in (
            ("description", "tool-description", "tool-descriptions"), ("schema", "tool-schema", "tool-schemas"),
        ):
            text = (definition["description"] if part == "description"
                    else canonical_bytes(definition["parameters"]).decode("utf-8"))
            item = make_lifecycle_prompt_item(
                context.node_binding_id + ":" + part, config[part + "_member_id"], text, config[part],
                source={"kind": "registered_tool", "tool_ref": deepcopy(config["tool_ref"]), "part": part},
                metadata={"tool_name": config["tool_ref"]["name"]}, purpose=purpose,
            )
            result[port] = lifecycle_prompt_content([item])
        return result

    tool_ports = (material_port("tool-descriptions"), material_port("tool-schemas"))
    node("tool", "工具说明与参数 schema · 生命周期", tool_config, object_schema({
        "tool_ref": object_schema({"name": {"type": "string", "minLength": 1},
                                  "version": {"type": "string", "minLength": 1}}),
        "description": presentation_schema(), "schema": presentation_schema(),
        "description_member_id": _UUID, "schema_member_id": _UUID,
    }), tool_execute, outputs=tool_ports, validator=validate_tool)
    node("tool-summary", "工具提示词汇总 · 生命周期", {}, object_schema({}),
         lambda config, inputs, context: {
             port.port_id: merge_lifecycle_prompt_materials(inputs.get(port.port_id, [])) for port in tool_ports},
         inputs=tuple(material_port(port.port_id, required=False, multiple=True) for port in tool_ports),
         outputs=tool_ports)
