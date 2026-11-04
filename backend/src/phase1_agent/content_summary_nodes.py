"""Explicit aggregation and one final Agent input assembly."""

from copy import deepcopy

from .content_contracts import object_schema
from .content_summary import CONTENT_SUMMARY_TYPE, summarize_content, validate_content_summary
from .context_host_service import summary_context_service_requirement
from .context_package import _exact_input
from .context_prompt_v6 import assemble_native_context_prompt
from .context_v4_nodes import _validate
from .graph_contracts import NodeDefinition, NodePort, require
from .tool_prompt_contract import tool_prompt_definition, tool_prompt_materials, validate_tool_prompt


def _summary_input(inputs, context):
    if "summary" not in inputs:
        return None
    value = validate_content_summary(inputs["summary"])
    accepted = context.host_call("artifacts:read", "resolve-input", {"port": "summary"})
    origin = context.host_call("artifacts:read", "describe-input-origin", {"port": "summary"})
    require(accepted["output_id"] == _exact_input(context, "summary")["output_id"]
            and accepted["value"] == value
            and (origin["component_id"], origin["component_version"]) == ("prompts.summary", "3"),
            "content_summary_producer_invalid",
            "Aggregation requires its exact accepted structured summary")
    if "context" in value:
        context.host_call("context:validate", "validate-context", {
            "operation": "validate", "view": value["context"], "view_ref": value["context_ref"],
            "summary": value, "summary_ref": _exact_input(context, "summary"),
        })
    if "current_input" in value:
        current = context.host_call("artifacts:read", "resolve-artifact",
                                    {"reference": value["current_input_ref"]})
        require(current["value"] == value["current_input"], "context_input_mismatch",
                "Summary current input differs from its original accepted text")
    return value


def _content(config, inputs, context):
    summary = _summary_input(inputs, context)
    if "view" in inputs:
        _validate(context, inputs["view"])
    return summarize_content(
        inputs.get("input", []) if "input" in inputs else inputs.get("materials", []),
        summary=summary, view=inputs.get("view"),
        context_ref=_exact_input(context, "view") if "view" in inputs else None,
        current_input=inputs.get("current_input"),
        current_input_ref=_exact_input(context, "current_input") if "current_input" in inputs else None,
        source_output_refs=[*context.input_artifact_refs("summary"),
                            *context.input_artifact_refs("input"),
                            *context.input_artifact_refs("materials")],
    )


def _summary(config, inputs, context):
    return {"output": _content(config, inputs, context)}


def _assembly(config, inputs, context):
    content = _content(config, inputs, context)
    require("context" in content and "current_input" in content,
            "input_assembly_incomplete", "Agent input assembly requires context and current input")
    materials = [content["materials"]]
    if "tool_prompt" in inputs:
        materials.append(tool_prompt_materials(
            validate_tool_prompt(inputs["tool_prompt"]), node_binding_id=context.node_binding_id))
    return {"output": assemble_native_context_prompt(
        materials, content["context"], content["current_input"],
        context_ref=content["context_ref"], current_input_ref=content["current_input_ref"],
        source_output_refs=[*content["source_output_refs"],
                            *context.input_artifact_refs("tool_prompt")],
    )}


def register_content_summary_nodes(host):
    host.register_shared_data_type(tool_prompt_definition())
    common = (
        NodePort("summary", CONTENT_SUMMARY_TYPE, required=False),
        NodePort("view", "CONTEXT_VIEW", data_schema_version=5, required=False),
        NodePort("current_input", "TEXT", data_schema_version=2, required=False),
    )
    hooks = {
        "capabilities": ("context:validate", "artifacts:read"), "input_storage": "references",
        "service_requirements": (summary_context_service_requirement(),),
    }
    host.register_node(NodeDefinition(
        "prompts.summary", "3", "提示词/上下文汇总", "提示词", {}, object_schema({}),
        inputs=(*common, NodePort("input", "PROMPT_MATERIALS", required=False, multiple=True,
                                  accepted_types=(("PROMPT", 2),))),
        outputs=(NodePort("output", CONTENT_SUMMARY_TYPE),), **deepcopy(hooks)), _summary)
    host.register_node(NodeDefinition(
        "context.assembly", "7", "输入装配", "上下文", {}, object_schema({}),
        inputs=(*common,
                NodePort("materials", "PROMPT_MATERIALS", required=False, multiple=True,
                         accepted_types=(("PROMPT", 2),)),
                NodePort("tool_prompt", "TOOL_PROMPT", required=False)),
        outputs=(NodePort("output", "PROMPT", data_schema_version=6),), **hooks), _assembly)
