"""Two-node default context chain and transaction settlement adapter."""

from copy import deepcopy

from .context_contract import EFFECTIVE_CONTEXT_TYPE, artifact_ref, context_view_references
from .context_host_service import service_requirement
from .context_package import _exact_input
from .context_prompt import (
    assemble_bound_context_prompt, context_prompt_references, validate_bound_context_prompt,
)
from .context_v2 import (
    agent_context_references, merge_bound_context, prepare_bound_context_adoption, read_bound_context,
    validate_agent_context, validate_bound_context_view, validate_bound_context_write,
    window_bound_context,
)
from .content_contracts import object_schema
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import DataTypeDefinition


def _validate(context, operation, view, packet=None):
    payload = {"operation": operation, "view": deepcopy(view), "view_ref": _exact_input(context, "view")}
    if packet is not None:
        payload.update(context=deepcopy(packet), context_ref=_exact_input(context, "context"))
    context.host_call("context:validate", "validate-context", payload)
    return payload


def _output(config, inputs, context):
    record = context.object_read(config["object_key"])
    retained = context.host_call("context:read", "read-context", {"object_key": config["object_key"]})
    require(retained["object"] == record, "context_invalid_read", "Read requires its exact frozen object")
    return {"output": read_bound_context(record, context.workflow_session_id, config["object_key"],
                                        config["agent_node_id"], retained["view"])}


def _window(config, inputs, context):
    _validate(context, "window", inputs["view"])
    return {"output": window_bound_context(inputs["view"], _exact_input(context, "view"),
                                          last_units=config["last_units"])}


def _assembly(config, inputs, context):
    _validate(context, "assembly", inputs["view"])
    return {"output": assemble_bound_context_prompt(
        inputs.get("materials", []), inputs["view"], inputs["current_input"],
        context_ref=_exact_input(context, "view"), current_input_ref=_exact_input(context, "current_input"),
        source_output_refs=context.input_artifact_refs("materials"))}


def _merge(config, inputs, context):
    from .agent_executor import resolve_input, validate_execution_projection
    from .context_prompt import validate_bound_context_prompt
    view, packet = validate_bound_context_view(inputs["view"]), validate_agent_context(inputs["context"])
    require(view["owner"] == {"workflow_session_id": context.workflow_session_id,
                             "object_key": config["object_key"], "agent_node_id": config["agent_node_id"]},
            "context_agent_binding_mismatch", "Merge must target its bound Agent and context object")
    producer = resolve_input(context, "context", packet)["producer"]
    require(producer == packet["owner"], "context_agent_binding_mismatch",
            "Agent context must belong to its accepted producer")
    payload = _validate(context, "merge", view, packet)
    prompt = context.host_call("artifacts:read", "resolve-artifact",
                              {"reference": packet["receipts"]["frozen_prompt_ref"]})["value"]
    prompt = validate_bound_context_prompt(prompt)
    facts = context.host_call("facts:read", "read-executor-facts", {
        "port": "context", "producer": producer, "executor_ref": packet["receipts"]["executor_ref"],
        "fact_ids": packet["receipts"]["fact_ids"]})
    validate_execution_projection(packet["unit"], packet["receipts"], facts, prompt)
    return {"output": merge_bound_context(view, payload["view_ref"], packet, payload["context_ref"])}


def validate_context_receipt(value):
    from .contract_json import validate_json_value
    validate_json_value(value)
    require(type(value) is dict and set(value) == {
        "schema_version", "kind", "owner", "view_ref", "adopted_delta_ids", "operation_key", "receipt"},
        "context_invalid_commit", "Actual context receipt fields are invalid")
    require(type(value["schema_version"]) is int and value["schema_version"] == 2
            and value["kind"] == "workflow.context-commit",
            "context_invalid_commit", "Actual context receipt requires CONTEXT_COMMIT@2")
    artifact_ref(value["view_ref"])
    from .context_contract import _fields, _ids
    from .graph_contracts import uuid4_string
    _fields(value["owner"], ("workflow_session_id", "object_key", "agent_node_id"))
    uuid4_string(value["owner"]["workflow_session_id"])
    uuid4_string(value["owner"]["agent_node_id"])
    require(type(value["owner"]["object_key"]) is str and 0 < len(value["owner"]["object_key"]) <= 128,
            "context_invalid_commit", "Commit requires its bound object key")
    _ids(value["adopted_delta_ids"])
    receipt = value["receipt"]
    require(type(receipt) is dict and set(receipt) == {"object_key", "revision", "revision_id", "deleted"}
            and receipt["object_key"] == value["owner"]["object_key"] and receipt["deleted"] is False
            and type(receipt["revision"]) is int and 2 <= receipt["revision"] <= 2**53 - 1
            and type(value["operation_key"]) is str and value["operation_key"].startswith("context-merge:"),
            "context_invalid_commit", "Commit requires an actual nondeleted object receipt")
    uuid4_string(receipt["revision_id"])
    return deepcopy(value)


def context_merge_settlement(context, outputs, output_refs):
    view = validate_bound_context_view(outputs["output"])
    key = view["owner"]["object_key"]
    # The original dispatch context retains the exact object revision it read.
    record = context.object_read(key)
    view_ref = artifact_ref({"scope": "artifact", "output_id": output_refs["output"]})
    prepared = prepare_bound_context_adoption(
        workflow_session_id=context.workflow_session_id, object_key=key, object_record=record,
        view_ref=view_ref, view=view)
    require(not prepared["already_committed"], "context_settlement_replay_invalid",
            "Transaction retries settle the original candidate and original write identity")

    def finalize(staged, refs, receipts):
        require(type(receipts) is list and len(receipts) == 1, "context_receipt_unavailable",
                "Merge requires its actual accepted object receipt")
        return {"output": deepcopy(view), "commit": validate_context_receipt({
            "schema_version": 2, "kind": "workflow.context-commit", "owner": deepcopy(view["owner"]),
            "view_ref": view_ref, "adopted_delta_ids": prepared["desired_value"]["accepted_delta_ids"],
            "operation_key": prepared["operation_key"], "receipt": deepcopy(receipts[0])})}

    return {"outputs": {"output": view}, "object_writes": [prepared["write_intent"]], "finalize": finalize}


def register_bound_context(host):
    from .context_contract import (
        effective_context_references, preserve_artifact_references, validate_effective_context,
    )
    host.register_data_type(DataTypeDefinition(
        EFFECTIVE_CONTEXT_TYPE, 2, object_schema({
            "view_ref": {"type": ["object", "null"]}, "accepted_delta_ids": {"type": "array"}}),
        {"view_ref": None, "accepted_delta_ids": []}, validator=validate_effective_context,
        references=effective_context_references, reference_mapper=preserve_artifact_references,
        write_validator=validate_bound_context_write))
    for identity, version, validator, refs in (
        ("AGENT_CONTEXT", 1, validate_agent_context, agent_context_references),
        ("CONTEXT_VIEW", 2, validate_bound_context_view, context_view_references),
        ("PROMPT", 4, validate_bound_context_prompt, context_prompt_references),
        ("CONTEXT_COMMIT", 2, validate_context_receipt, lambda value: [value["view_ref"]]),
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
                                 for version in ("2", "6")]},)
    view = NodePort("view", "CONTEXT_VIEW", data_schema_version=2)
    host.register_node(NodeDefinition(
        "context.output", "1", "Bound context output", "Context", config, schema,
        outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=2),),
        capabilities=("objects:read", "context:read"), input_storage="references",
        object_accesses=({"config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                          "schema_version": 2, "access": "read", "multiple": False},),
        service_requirements=(service_requirement("context:read", "read-context"),),
        config_node_references=binding), _output)
    host.register_node(NodeDefinition(
        "context.window", "2", "Bound context window", "Context", {"last_units": 16},
        object_schema({"last_units": {"type": "integer", "minimum": 0, "maximum": 4096}}),
        inputs=(view,), outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=2),),
        capabilities=("context:validate",), input_storage="references",
        service_requirements=(service_requirement("context:validate", "validate-context"),)), _window)
    host.register_node(NodeDefinition(
        "context.assembly", "2", "Bound context assembly", "Context", {}, object_schema({}),
        inputs=(view, NodePort("materials", "PROMPT", data_schema_version=2, multiple=True, required=False),
                NodePort("current_input", "TEXT", data_schema_version=2)),
        outputs=(NodePort("output", "PROMPT", data_schema_version=4),),
        capabilities=("context:validate",), input_storage="references",
        service_requirements=(service_requirement("context:validate", "validate-context"),)), _assembly)
    host.register_node(NodeDefinition(
        "context.merge", "1", "Merge Agent context", "Context", config, schema,
        inputs=(view, NodePort("context", "AGENT_CONTEXT")),
        outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=2),
                 NodePort("commit", "CONTEXT_COMMIT", data_schema_version=2)),
        capabilities=("objects:read", "objects:write", "context:validate", "artifacts:read", "facts:read"),
        input_storage="references",
        object_accesses=({"config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                          "schema_version": 2, "access": "read_write", "multiple": False},),
        service_requirements=(service_requirement("context:validate", "validate-context"),),
        config_node_references=binding), _merge, settlement_builder=context_merge_settlement)
