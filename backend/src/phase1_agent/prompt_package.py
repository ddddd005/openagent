"""Independent prompt materials, assembly and current global resources.

This package consumes public content and host capabilities. It neither imports
an Agent executor nor reads history or a database. Assemblies in this version
have one optional current user input; history placement belongs to a later
context contract.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping

from jsonschema import Draft202012Validator

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import (
    default_presentation, make_prompt_item, presentation_schema, prompt_content, validate_presentation,
)
from .contract_json import canonical_bytes, validate_json_value
from .global_resources import global_resource_reference, validate_global_resource_reference
from .graph_contracts import NodeDefinition, NodePort, require, uuid4_string
from .host_sdk import DataTypeDefinition, ResourceIdentity
from .prompt_contract import (
    assemble_prompt, check_prompt_schema, merge_prompt_materials, validate_ready_prompt,
)


PROMPT_RESOURCE_TYPE = "workflow.prompt-resource"
PROMPT_PACKAGE_ID = "workflow.prompts"
_DEFAULT_MEMBER = "59e405d0-debd-4d7a-aa7f-ce6445d72665"

PROMPT_FRONTEND_EXTENSIONS = (
    {"extension_id": "workflow.prompts.workbench-panel", "kind": "workbench-panel",
     "entrypoint": "workflow.prompts.workbench.panel", "component_id": None, "component_version": None,
     "binding": {"surface": "workbench", "slot": "panel", "target": {}}},
    {"extension_id": "workflow.prompts.node-fields", "kind": "field-editor",
     "entrypoint": "workflow.prompts.workbench.node-fields",
     "component_id": "prompts.global-reference", "component_version": "1",
     "binding": {"surface": "workbench", "slot": "node-fields",
                 "target": {"component_id": "prompts.global-reference", "component_version": "1"}}},
)


def _schema(properties: dict, *, required: tuple[str, ...] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": deepcopy(properties),
            "required": list(properties if required is None else required)}


def _uuid_schema() -> dict:
    return {"type": "string", "pattern":
            "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}


def _member_schema() -> dict:
    return _schema({"id": _uuid_schema(), "text": {"type": "string"},
                    "presentation": presentation_schema(), "metadata": {"type": "object"}})


def _members_schema() -> dict:
    return {"type": "array", "maxItems": 1024, "items": _member_schema()}


def _validate_members(members: list[dict]) -> None:
    check_prompt_schema(members, _members_schema(), "graph_prompt_resource_invalid", "Prompt members are invalid")
    identities = [uuid4_string(member["id"]) for member in members]
    require(len(identities) == len(set(identities)), "graph_prompt_member_conflict",
            "A prompt group cannot contain repeated member identities")
    for member in members:
        validate_presentation(member["presentation"])


def validate_prompt_resource(value: object) -> dict:
    validate_json_value(value)
    check_prompt_schema(value, _schema({"enabled": {"type": "boolean"}, "members": _members_schema()}),
                        "graph_prompt_resource_invalid", "A prompt resource requires enabled and members")
    _validate_members(value["members"])
    return deepcopy(value)


def _fixed_members(config: dict, context, *, kind: str) -> dict:
    items = [make_prompt_item(
        context.node_binding_id, member["id"], member["text"], member["presentation"],
        source={"kind": kind, "node_id": context.node_binding_id, "member_id": member["id"]},
        metadata=member["metadata"],
    ) for member in config["members"]]
    return merge_prompt_materials([prompt_content(items)])


def _reference_config(config: dict) -> None:
    reference = ResourceIdentity.from_dict(config["reference"]).to_dict()
    require(reference["type_id"] == PROMPT_RESOURCE_TYPE, "graph_prompt_resource_type_mismatch",
            "Global prompt references require the prompt resource type")


def _reference_preflight(config: dict, records: list[dict]) -> None:
    reference = config["reference"]
    record = next((record for record in records if all(
        record.get(key) == reference[key] for key in ("scope", "type_id", "resource_id"))), None)
    require(record is not None, "global_resource_missing", "Global prompt resource is missing")
    require(record["data_schema_version"] == 1, "graph_prompt_resource_type_mismatch",
            "Global prompt resource schema is unsupported")
    resource = validate_prompt_resource(record["value"])
    require(resource["enabled"], "global_content_disabled", "Global prompt resource is disabled")


def _resolve(config: dict, inputs: dict, context) -> dict:
    envelope = validate_global_resource_reference(inputs["input"])
    reference = envelope["reference"]
    _reference_config({"reference": reference})
    record = context.host_call("resources:read", "current-global-resource", reference)
    _reference_preflight({"reference": reference}, [record])
    items = [make_prompt_item(
        context.node_binding_id, member["id"], member["text"], member["presentation"],
        source={"kind": "global_prompt", "reference": deepcopy(reference), "member_id": member["id"]},
        metadata=member["metadata"],
    ) for member in record["value"]["members"]]
    context.reads.append({"kind": "global_resource_read", "reference": deepcopy(reference)})
    return {"output": merge_prompt_materials([prompt_content(items)])}


def _tool_definitions(tool_catalog: Mapping | None) -> dict[tuple[str, str], dict]:
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


def create_prompt_package(*, tool_catalog: Mapping | None = None) -> CapabilityPackage:
    tools = _tool_definitions(tool_catalog)
    default_member = {"id": _DEFAULT_MEMBER, "text": "", "presentation": default_presentation(), "metadata": {}}
    prompt_port = lambda name="output", **kwargs: NodePort(name, "PROMPT", data_schema_version=2, **kwargs)
    text_port = lambda name="input", **kwargs: NodePort(name, "TEXT", data_schema_version=2, **kwargs)

    def register(host):
        for extension in PROMPT_FRONTEND_EXTENSIONS:
            host.register_frontend_extension(**extension, host_protocol_version=1)
        host.register_data_type(DataTypeDefinition(
            PROMPT_RESOURCE_TYPE, 1,
            _schema({"enabled": {"type": "boolean"}, "members": _members_schema()}),
            {"enabled": True, "members": []}, scope="global", validator=validate_prompt_resource,
        ))

        def node(name, title, config, schema, execute, *, inputs=(), outputs=None,
                 capabilities=(), validator=None, **hooks):
            host.register_node(NodeDefinition(
                "prompts." + name, "1", title, "提示词", config, schema, inputs=inputs,
                outputs=(prompt_port(),) if outputs is None else outputs, capabilities=capabilities,
                input_storage="references",
            ), execute, config_validator=validator, **hooks)

        node("item", "提示词条目", deepcopy(default_member), _member_schema(),
             lambda config, inputs, context: {
                 "output": _fixed_members({"members": [config]}, context, kind="prompt_item")},
             validator=lambda config: _validate_members([config]))
        node("group", "提示词内容组", {"members": [deepcopy(default_member)]},
             _schema({"members": _members_schema()}),
             lambda config, inputs, context: {"output": _fixed_members(config, context, kind="prompt_group")},
             validator=lambda config: _validate_members(config["members"]))

        def source_validate(config):
            value = config["value"]
            merge_prompt_materials([value])
            validate_presentation(config["presentation"])

        def source_execute(config, inputs, context):
            values = [deepcopy(config["value"])]
            if "input" in inputs:
                values.append(prompt_content([make_prompt_item(
                    context.node_binding_id, config["member_id"], inputs["input"]["text"], config["presentation"],
                    source={"kind": "prompt_increment", "node_id": context.node_binding_id,
                            "member_id": config["member_id"]},
                )]))
            return {"output": merge_prompt_materials(values)}

        node("source", "提示词材料与增量", {"value": prompt_content([]), "member_id": _DEFAULT_MEMBER,
                                           "presentation": default_presentation()},
             _schema({"value": {"type": "object"}, "member_id": _uuid_schema(),
                      "presentation": presentation_schema()}), source_execute,
             inputs=(text_port(required=False),), validator=source_validate)
        node("summary", "提示词汇总", {}, _schema({}),
             lambda config, inputs, context: {"output": merge_prompt_materials(inputs.get("input", []))},
             inputs=(prompt_port("input", required=False, multiple=True),))
        node("assembly", "提示词装配", {}, _schema({}),
             lambda config, inputs, context: {"output": assemble_prompt(
                 inputs.get("input", []), inputs.get("current_input"),
                 source_output_refs=context.input_artifact_refs("input"),
                 current_input_refs=context.input_artifact_refs("current_input"))},
             inputs=(prompt_port("input", required=False, multiple=True),
                     text_port("current_input", required=False)))

        reference = {"envelope_version": 1, "scope": "workspace", "type_id": PROMPT_RESOURCE_TYPE,
                     "resource_id": _DEFAULT_MEMBER}
        node("global-reference", "全局提示词引用", {"reference": reference},
             _schema({"reference": {"type": "object"}}),
             lambda config, inputs, context: {"output": global_resource_reference(config["reference"])},
             outputs=(NodePort("output", "GLOBAL_RESOURCE_REF"),), capabilities=("resources:read",),
             validator=_reference_config,
             resource_dependencies_declaration=lambda config: [
                 {"kind": "global-resource", "reference": deepcopy(config["reference"])}],
             resource_preflight_validator=_reference_preflight)
        node("global-resolve", "全局提示词解析", {}, _schema({}), _resolve,
             inputs=(NodePort("input", "GLOBAL_RESOURCE_REF"),), capabilities=("resources:read",),
             resource_input_ports=("input",))

        tool_ref = {"name": sorted(tools)[0][0] if tools else "inspect_text",
                    "version": sorted(tools)[0][1] if tools else "1"}
        tool_config = {"tool_ref": tool_ref, "description": deepcopy(default_presentation()),
                       "schema": deepcopy(default_presentation()),
                       "description_member_id": _DEFAULT_MEMBER, "schema_member_id": _DEFAULT_MEMBER}
        tool_schema = _schema({
            "tool_ref": _schema({"name": {"type": "string", "minLength": 1},
                                 "version": {"type": "string", "minLength": 1}}),
            "description": presentation_schema(), "schema": presentation_schema(),
            "description_member_id": _uuid_schema(), "schema_member_id": _uuid_schema(),
        })

        def tool_validate(config):
            ref = config["tool_ref"]
            require((ref["name"], ref["version"]) in tools, "graph_tool_not_registered",
                    "Tool material requires an installed exact tool version")
            for part in ("description", "schema"):
                validate_presentation(config[part])

        def tool_execute(config, inputs, context):
            ref = config["tool_ref"]
            definition = tools[(ref["name"], ref["version"])]
            result = {}
            for part, purpose, port in (
                ("description", "tool-description", "tool-descriptions"),
                ("schema", "tool-schema", "tool-schemas"),
            ):
                text = (definition["description"] if part == "description"
                        else canonical_bytes(definition["parameters"]).decode("utf-8"))
                item = make_prompt_item(
                    context.node_binding_id + ":" + part, config[part + "_member_id"], text, config[part],
                    source={"kind": "registered_tool", "tool_ref": deepcopy(ref), "part": part},
                    metadata={"tool_name": ref["name"]}, purpose=purpose,
                )
                result[port] = prompt_content([item])
            return result

        tool_ports = (prompt_port("tool-descriptions"), prompt_port("tool-schemas"))
        node("tool", "工具说明与参数 schema", tool_config, tool_schema, tool_execute,
             outputs=tool_ports, validator=tool_validate)
        node("tool-summary", "工具提示词汇总", {}, _schema({}),
             lambda config, inputs, context: {
                 port.port_id: merge_prompt_materials(inputs.get(port.port_id, [])) for port in tool_ports},
             inputs=tuple(prompt_port(port.port_id, required=False, multiple=True) for port in tool_ports),
             outputs=tool_ports)

    exports = {
        "data_types": [{"scope": "global", "type_id": PROMPT_RESOURCE_TYPE, "schema_version": 1}],
        "nodes": [{"component_id": "prompts." + name, "component_version": "1"} for name in (
            "item", "group", "source", "summary", "assembly", "global-reference", "global-resolve",
            "tool", "tool-summary",
        )],
        "frontend_extensions": [{"extension_id": row["extension_id"]} for row in PROMPT_FRONTEND_EXTENSIONS],
    }
    return CapabilityPackage(PackageManifest(
        PROMPT_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.content", "1.0.0"),), exports=exports,
    ), register)
