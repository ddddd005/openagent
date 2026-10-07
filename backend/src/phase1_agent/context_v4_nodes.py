"""Native context read, prompt assembly, and atomic ordered update adoption."""

from copy import deepcopy

from .content_contracts import object_schema
from .context_contract import (
    EFFECTIVE_CONTEXT_TYPE, effective_context_references, preserve_artifact_references,
    validate_effective_context,
)
from .context_host_service import native_service_requirement
from .context_package import _exact_input
from .context_update import context_update_references, validate_context_update
from .context_v2_nodes import validate_context_receipt
from .context_v4 import (
    merge_native_context, native_context_references, prepare_native_context_adoption,
    read_native_context, validate_native_context_view, validate_native_context_write,
)
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import DataTypeDefinition


def validate_graph_context_update(value):
    value = validate_context_update(value)
    require(value["next_view"]["schema_version"] == 4, "context_update_invalid",
            "Graph Agent updates require an explicit persistent CONTEXT_VIEW@4 candidate")
    return value


def _validate(context, view, packet=None):
    payload = {"operation": "merge" if packet is not None else "validate",
               "view": deepcopy(view), "view_ref": _exact_input(context, "view")}
    if packet is not None:
        payload.update(context=deepcopy(packet), context_ref=_exact_input(context, "context"))
    context.host_call("context:validate", "validate-context", payload)
    return payload


def _output(config, inputs, context):
    record = context.object_read(config["object_key"])
    retained = context.host_call("context:read", "read-context", {"object_key": config["object_key"]})
    require(retained["object"] == record, "context_invalid_read", "Read must use its exact authorized frozen object")
    return {"output": read_native_context(
        record, context.workflow_session_id, config["object_key"], config["agent_node_id"], retained["view"])}


def _assembly(config, inputs, context):
    from .context_prompt_v6 import assemble_native_context_prompt
    _validate(context, inputs["view"])
    return {"output": assemble_native_context_prompt(
        inputs.get("materials", []), inputs["view"], inputs["current_input"],
        context_ref=_exact_input(context, "view"), current_input_ref=_exact_input(context, "current_input"),
        source_output_refs=context.input_artifact_refs("materials"))}


def _merge(config, inputs, context):
    from .agent_executor import resolve_input, validate_execution_update
    from .context_prompt_v6 import validate_native_context_prompt

    view = validate_native_context_view(inputs["view"])
    packet = validate_graph_context_update(inputs["context"])
    require(view["owner"] == {"workflow_session_id": context.workflow_session_id,
                             "object_key": config["object_key"], "agent_node_id": config["agent_node_id"]},
            "context_agent_binding_mismatch", "Merge must target its explicitly bound Agent and object")
    producer = resolve_input(context, "context", packet)["producer"]
    require(producer == packet["owner"], "context_agent_binding_mismatch",
            "An Agent update must belong to its exact accepted producer")
    payload = _validate(context, view, packet)
    prompt = context.host_call("artifacts:read", "resolve-artifact", {
        "reference": packet["receipts"]["frozen_prompt_ref"]})["value"]
    prompt = validate_native_context_prompt(prompt)
    binding = context.host_call("artifacts:read", "resolve-artifact", {
        "reference": packet["receipts"]["model_ref"]})["value"]
    origin = context.host_call("artifacts:read", "describe-input-origin", {"port": "context"})
    require(origin["producer"] == producer
            and (origin["component_id"], origin["component_version"]) == ("agents.execute", "4"),
            "context_agent_binding_mismatch", "Native updates require their actual official Agent producer")
    policy_refs = origin["input_refs"].get("compaction_policy", [])
    require(len(policy_refs) == (0 if packet["compaction_policy_ref"] is None else 1)
            and (not policy_refs or policy_refs[0]["output_id"] == packet["compaction_policy_ref"]["output_id"]),
            "context_policy_binding_mismatch", "Compaction must retain its exact accepted Agent policy input")
    policy = None
    if policy_refs:
        policy = context.host_call("artifacts:read", "resolve-artifact", {
            "reference": packet["compaction_policy_ref"]})["value"]
    facts = context.host_call("facts:read", "read-executor-facts", {
        "port": "context", "producer": producer, "executor_ref": packet["receipts"]["executor_ref"],
        "fact_ids": packet["receipts"]["fact_ids"]})
    validate_execution_update(packet, facts, prompt, binding,
                              policy=policy, policy_ref=packet["compaction_policy_ref"])
    return {"output": merge_native_context(view, payload["view_ref"], packet, payload["context_ref"], prompt)}


def settle_native_merge(context, outputs, output_refs):
    view = validate_native_context_view(outputs["output"])
    key = view["owner"]["object_key"]
    reference = {"scope": "artifact", "output_id": output_refs["output"]}
    prepared = prepare_native_context_adoption(
        workflow_session_id=context.workflow_session_id, object_key=key,
        object_record=context.object_read(key), view_ref=reference, view=view)
    require(not prepared["already_committed"], "context_settlement_replay_invalid",
            "Acceptance retries reuse their original candidate and operation identity")

    def finalize(staged, refs, receipts):
        require(type(receipts) is list and len(receipts) == 1, "context_receipt_unavailable",
                "Native merge requires its actual accepted object receipt")
        return {"output": deepcopy(view), "commit": validate_context_receipt({
            "schema_version": 2, "kind": "workflow.context-commit", "owner": deepcopy(view["owner"]),
            "view_ref": reference, "adopted_delta_ids": prepared["desired_value"]["accepted_delta_ids"],
            "operation_key": prepared["operation_key"], "receipt": deepcopy(receipts[0])})}

    return {"outputs": {"output": view}, "object_writes": [prepared["write_intent"]], "finalize": finalize}


def register_native_context(host):
    from .context_prompt_v6 import native_context_prompt_references, validate_native_context_prompt

    host.register_data_type(DataTypeDefinition(
        EFFECTIVE_CONTEXT_TYPE, 4, object_schema({
            "view_ref": {"type": ["object", "null"]}, "accepted_delta_ids": {"type": "array"}}),
        {"view_ref": None, "accepted_delta_ids": []}, validator=validate_effective_context,
        references=effective_context_references, reference_mapper=preserve_artifact_references,
        write_validator=validate_native_context_write))
    for identity, version, validator, refs in (
        ("CONTEXT_VIEW", 4, validate_native_context_view, native_context_references),
        ("AGENT_CONTEXT_UPDATE", 1, validate_graph_context_update, context_update_references),
        ("PROMPT", 6, validate_native_context_prompt, native_context_prompt_references),
    ):
        host.register_data_type(DataTypeDefinition(
            identity, version, {"type": "object"}, scope="content", validator=validator,
            references=refs, reference_mapper=preserve_artifact_references, max_bytes=4_000_000,
            artifact_bindings=(lambda value: value["source_output_refs"]) if identity == "PROMPT" else None))
    config = {"object_key": "context", "agent_node_id": "00000000-0000-4000-8000-000000000000"}
    schema = object_schema({"object_key": {"type": "string", "minLength": 1, "maxLength": 128},
                            "agent_node_id": {"type": "string"}})
    binding = ({"config_field": "agent_node_id", "multiple": False,
                "target_types": [{"component_id": "agents.execute", "component_version": "4"}]},)
    view = NodePort("view", "CONTEXT_VIEW", data_schema_version=4)
    host.register_node(NodeDefinition(
        "context.output", "4", "Native context output", "Context", config, schema,
        outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=4),),
        capabilities=("objects:read", "context:read"), input_storage="references",
        object_accesses=({"config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                          "schema_version": 4, "access": "read", "multiple": False},),
        service_requirements=(native_service_requirement("context:read", "read-context"),),
        config_node_references=binding), _output)
    host.register_node(NodeDefinition(
        "context.assembly", "4", "Native context assembly", "Context", {}, object_schema({}),
        inputs=(view, NodePort("materials", "PROMPT_MATERIALS", multiple=True, required=False),
                NodePort("current_input", "TEXT", data_schema_version=2)),
        outputs=(NodePort("output", "PROMPT", data_schema_version=6),),
        capabilities=("context:validate",), input_storage="references",
        service_requirements=(native_service_requirement("context:validate", "validate-context"),)), _assembly)
    host.register_node(NodeDefinition(
        "context.merge", "4", "Adopt ordered Agent context", "Context", config, schema,
        inputs=(view, NodePort("context", "AGENT_CONTEXT_UPDATE")),
        outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=4),
                 NodePort("commit", "CONTEXT_COMMIT", data_schema_version=2)),
        capabilities=("objects:read", "objects:write", "context:validate", "artifacts:read", "facts:read"),
        input_storage="references",
        object_accesses=({"config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                          "schema_version": 4, "access": "read_write", "multiple": False},),
        service_requirements=(native_service_requirement("context:validate", "validate-context"),),
        config_node_references=binding), _merge, settlement_builder=settle_native_merge)
