"""Summary-aware lifecycle nodes reuse public object settlement and Agent facts."""

from copy import deepcopy

from .content_contracts import object_schema
from .context_contract import (
    EFFECTIVE_CONTEXT_TYPE, effective_context_references, preserve_artifact_references, validate_effective_context,
)
from .context_host_service import summary_service_requirement
from .context_package import _exact_input
from .context_prompt import (
    assemble_summary_context_prompt, context_prompt_references, validate_summary_context_prompt,
)
from .context_summary import (
    compression_plan_references, context_summary_references, validate_compression_plan, validate_context_summary,
)
from .context_v2_nodes import validate_context_receipt
from .context_v3 import (
    effective_view_references, merge_effective_view, prepare_effective_view_adoption,
    read_effective_view, validate_effective_view, validate_effective_view_write, window_effective_view,
)
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import DataTypeDefinition


def validate_view(context, view, packet=None):
    payload = {"operation": "merge" if packet is not None else "validate",
               "view": deepcopy(view), "view_ref": _exact_input(context, "view")}
    if packet is not None:
        payload.update(context=deepcopy(packet), context_ref=_exact_input(context, "context"))
    context.host_call("context:validate", "validate-context", payload)
    return payload


def _read(config, inputs, context):
    record = context.object_read(config["object_key"])
    retained = context.host_call("context:read", "read-context", {"object_key": config["object_key"]})
    require(retained["object"] == record, "context_invalid_read", "Read must use its exact authorized object")
    return {"output": read_effective_view(record, context.workflow_session_id, config["object_key"],
                                         config["agent_node_id"], retained["view"])}


def _window(config, inputs, context):
    validate_view(context, inputs["view"])
    return {"output": window_effective_view(inputs["view"], _exact_input(context, "view"),
                                           last_units=config["last_units"])}


def _assembly(config, inputs, context):
    validate_view(context, inputs["view"])
    return {"output": assemble_summary_context_prompt(
        inputs.get("materials", []), inputs["view"], inputs["current_input"],
        context_ref=_exact_input(context, "view"), current_input_ref=_exact_input(context, "current_input"),
        source_output_refs=context.input_artifact_refs("materials"), character_budget=config["character_budget"])}


def _merge(config, inputs, context):
    from .agent_executor import resolve_input, validate_execution_projection
    view, packet = validate_effective_view(inputs["view"]), inputs["context"]
    require(view["owner"] == {"workflow_session_id": context.workflow_session_id,
                             "object_key": config["object_key"], "agent_node_id": config["agent_node_id"]},
            "context_agent_binding_mismatch", "Merge must use its explicitly bound Agent and object")
    producer = resolve_input(context, "context", packet)["producer"]
    require(producer == packet["owner"], "context_agent_binding_mismatch", "Round requires its actual producer")
    payload = validate_view(context, view, packet)
    prompt = context.host_call("artifacts:read", "resolve-artifact",
                              {"reference": packet["receipts"]["frozen_prompt_ref"]})["value"]
    prompt = validate_summary_context_prompt(prompt)
    facts = context.host_call("facts:read", "read-executor-facts", {
        "port": "context", "producer": producer, "executor_ref": packet["receipts"]["executor_ref"],
        "fact_ids": packet["receipts"]["fact_ids"]})
    validate_execution_projection(packet["unit"], packet["receipts"], facts, prompt)
    return {"output": merge_effective_view(view, payload["view_ref"], packet, payload["context_ref"])}


def settle_effective_merge(context, outputs, output_refs):
    view = validate_effective_view(outputs["output"])
    key = view["owner"]["object_key"]
    reference = {"scope": "artifact", "output_id": output_refs["output"]}
    prepared = prepare_effective_view_adoption(
        workflow_session_id=context.workflow_session_id, object_key=key,
        object_record=context.object_read(key), view_ref=reference, view=view)
    require(not prepared["already_committed"], "context_settlement_replay_invalid",
            "Transaction retries reuse their original candidate and operation identity")

    def finalize(staged, refs, receipts):
        require(type(receipts) is list and len(receipts) == 1, "context_receipt_unavailable",
                "Merge requires the actual accepted object receipt")
        return {"output": view, "commit": validate_context_receipt({
            "schema_version": 2, "kind": "workflow.context-commit", "owner": deepcopy(view["owner"]),
            "view_ref": reference, "adopted_delta_ids": prepared["desired_value"]["accepted_delta_ids"],
            "operation_key": prepared["operation_key"], "receipt": receipts[0]})}

    return {"outputs": {"output": view}, "object_writes": [prepared["write_intent"]], "finalize": finalize}


def register_summary_context(host):
    host.register_data_type(DataTypeDefinition(
        EFFECTIVE_CONTEXT_TYPE, 3, object_schema({
            "view_ref": {"type": ["object", "null"]}, "accepted_delta_ids": {"type": "array"}}),
        {"view_ref": None, "accepted_delta_ids": []}, validator=validate_effective_context,
        references=effective_context_references, reference_mapper=preserve_artifact_references,
        write_validator=validate_effective_view_write))
    for identity, version, validator, refs in (
        ("CONTEXT_VIEW", 3, validate_effective_view, effective_view_references),
        ("CONTEXT_PLAN", 1, validate_compression_plan, compression_plan_references),
        ("CONTEXT_SUMMARY", 1, validate_context_summary, context_summary_references),
        ("PROMPT", 5, validate_summary_context_prompt, context_prompt_references),
    ):
        host.register_data_type(DataTypeDefinition(
            identity, version, {"type": "object"}, scope="content", validator=validator,
            references=refs, reference_mapper=preserve_artifact_references, max_bytes=4_000_000,
            artifact_bindings=(lambda value: value["source_output_refs"]) if identity == "PROMPT" else None))
    config = {"object_key": "context", "agent_node_id": "00000000-0000-4000-8000-000000000000"}
    schema = object_schema({"object_key": {"type": "string", "minLength": 1, "maxLength": 128},
                            "agent_node_id": {"type": "string"}})
    binding = ({"config_field": "agent_node_id", "multiple": False,
                "target_types": [{"component_id": "agents.execute", "component_version": version}
                                 for version in ("3", "7")]},)
    view = NodePort("view", "CONTEXT_VIEW", data_schema_version=3)
    host.register_node(NodeDefinition(
        "context.output", "2", "Summary-aware context output", "Context", config, schema,
        outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=3),),
        capabilities=("objects:read", "context:read"), input_storage="references",
        object_accesses=({"config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                          "schema_version": 3, "access": "read", "multiple": False},),
        service_requirements=(summary_service_requirement("context:read", "read-context"),),
        config_node_references=binding), _read)
    host.register_node(NodeDefinition(
        "context.window", "3", "Effective context window", "Context", {"last_units": 16},
        object_schema({"last_units": {"type": "integer", "minimum": 0, "maximum": 4096}}),
        inputs=(view,), outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=3),),
        capabilities=("context:validate",), input_storage="references",
        service_requirements=(summary_service_requirement("context:validate", "validate-context"),)), _window)
    host.register_node(NodeDefinition(
        "context.assembly", "3", "Effective context assembly", "Context", {"character_budget": 100_000},
        object_schema({"character_budget": {"type": "integer", "minimum": 1, "maximum": 4_000_000}}),
        inputs=(view, NodePort("materials", "PROMPT", data_schema_version=2, multiple=True, required=False),
                NodePort("current_input", "TEXT", data_schema_version=2)),
        outputs=(NodePort("output", "PROMPT", data_schema_version=5),),
        capabilities=("context:validate",), input_storage="references",
        service_requirements=(summary_service_requirement("context:validate", "validate-context"),)), _assembly)
    host.register_node(NodeDefinition(
        "context.merge", "2", "Merge effective Agent context", "Context", config, schema,
        inputs=(view, NodePort("context", "AGENT_CONTEXT")),
        outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=3),
                 NodePort("commit", "CONTEXT_COMMIT", data_schema_version=2)),
        capabilities=("objects:read", "objects:write", "context:validate", "artifacts:read", "facts:read"),
        input_storage="references",
        object_accesses=({"config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                          "schema_version": 3, "access": "read_write", "multiple": False},),
        service_requirements=(summary_service_requirement("context:validate", "validate-context"),),
        config_node_references=binding), _merge, settlement_builder=settle_effective_merge)
