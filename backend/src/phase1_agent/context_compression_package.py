"""Independent explicit prefix selection -> public Chat -> gated replacement."""

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .context_artifacts import projection_id, resolve_input
from .content_contracts import object_schema
from .context_contract import artifact_ref
from .context_host_service import summary_service_requirement
from .context_package import _exact_input
from .context_summary import (
    accept_summary_candidate, plan_compression, replace_context_prefix, summary_prompt,
)
from .context_v3_nodes import validate_view
from .graph_contracts import NodeDefinition, NodePort, require


def _plan(config, inputs, context):
    validate_view(context, inputs["view"])
    return {"output": plan_compression(inputs["view"], _exact_input(context, "view"), config,
                                       projection_id(context.node_run_id, "compression-plan"))}


def _prompt(config, inputs, context):
    resolve_input(context, "plan", inputs["plan"])
    return {"output": summary_prompt(inputs["plan"], _exact_input(context, "plan"),
                                     source_output_refs=context.input_artifact_refs("plan"))}


def _summary(config, inputs, context):
    for port in ("plan", "prompt", "result"):
        resolve_input(context, port, inputs[port])
    origin = context.host_call("artifacts:read", "describe-input-origin", {"port": "result"})
    require((origin["component_id"], origin["component_version"]) == ("models.chat", "1"),
            "context_summary_generation_unproven", "Summary requires an actual accepted public Chat output")
    refs = origin["input_refs"].get("prompt", [])
    require(len(refs) == 1 and refs[0]["output_id"] == _exact_input(context, "prompt")["output_id"],
            "context_summary_generation_unproven", "Chat must actually consume this plan's exact summary prompt")
    return {"output": accept_summary_candidate(
        inputs["plan"], _exact_input(context, "plan"), inputs["prompt"], _exact_input(context, "prompt"),
        inputs["result"], _exact_input(context, "result"), projection_id(context.node_run_id, "context-summary"))}


def _replace(config, inputs, context):
    validate_view(context, inputs["view"])
    for port in ("plan", "summary"):
        resolve_input(context, port, inputs[port])
    return {"output": replace_context_prefix(
        inputs["view"], _exact_input(context, "view"), inputs["plan"], _exact_input(context, "plan"),
        inputs["summary"], _exact_input(context, "summary"))}


def create_context_compression_package():
    def register(host):
        view = NodePort("view", "CONTEXT_VIEW", data_schema_version=3)
        plan, summary = NodePort("plan", "CONTEXT_PLAN"), NodePort("summary", "CONTEXT_SUMMARY")
        policy = {"prefix_units": 1, "min_chars": 1, "max_chars": 2000,
                  "max_ratio": 0.5, "required_terms": []}
        policy_schema = object_schema({
            "prefix_units": {"type": "integer", "minimum": 1, "maximum": 4096},
            "min_chars": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
            "max_ratio": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
            "required_terms": {"type": "array", "maxItems": 256,
                               "items": {"type": "string", "minLength": 1, "maxLength": 4096}},
        })
        host.register_node(NodeDefinition(
            "context.plan", "1", "Plan prefix compression", "Context", policy, policy_schema,
            inputs=(view,), outputs=(NodePort("output", "CONTEXT_PLAN"),),
            capabilities=("context:validate",), input_storage="references",
            service_requirements=(summary_service_requirement("context:validate", "validate-context"),)), _plan)
        host.register_node(NodeDefinition(
            "context.summary-prompt", "1", "Summary request prompt", "Context", {}, object_schema({}),
            inputs=(plan,), outputs=(NodePort("output", "PROMPT", data_schema_version=2),),
            capabilities=("artifacts:read",), input_storage="references"), _prompt)
        host.register_node(NodeDefinition(
            "context.summary", "1", "Validate summary candidate", "Context", {}, object_schema({}),
            inputs=(plan, NodePort("prompt", "PROMPT", data_schema_version=2),
                    NodePort("result", "MODEL_RESULT")),
            outputs=(NodePort("output", "CONTEXT_SUMMARY"),),
            capabilities=("artifacts:read",), input_storage="references"), _summary)
        host.register_node(NodeDefinition(
            "context.replace", "1", "Replace planned prefix", "Context", {}, object_schema({}),
            inputs=(view, plan, summary), outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=3),),
            capabilities=("context:validate", "artifacts:read"), input_storage="references",
            service_requirements=(summary_service_requirement("context:validate", "validate-context"),)), _replace)

    return CapabilityPackage(PackageManifest(
        "workflow.context-compression", "1.0.0", (
            PackageDependency("workflow.context", "1.0.0"), PackageDependency("workflow.models", "1.0.0")),
        exports={"nodes": [{"component_id": component, "component_version": "1"} for component in (
            "context.plan", "context.summary-prompt", "context.summary", "context.replace")]}), register)
