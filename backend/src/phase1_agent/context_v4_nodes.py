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
from .context_receipt import validate_context_receipt
from .context_v4 import (
    merge_native_context_v5, native_context_references, native_context_references_v5,
    prepare_native_context_adoption_v5, read_native_context_v5, validate_native_context_view,
    validate_native_context_view_v5, validate_native_context_write,
)
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import DataTypeDefinition


def validate_legacy_graph_context_update(value):
    """Validate the retired update envelope for read-only historical decoding."""
    value = validate_context_update(value)
    require(value["next_view"]["schema_version"] == 4, "context_update_invalid",
            "Legacy Agent updates require their historical CONTEXT_VIEW@4 candidate")
    return value


def _validate(context, view):
    view = validate_native_context_view_v5(view)
    payload = {"operation": "validate", "view": deepcopy(view),
               "view_ref": _exact_input(context, "view")}
    origin = context.host_call("artifacts:read", "describe-input-origin", {"port": "view"})
    expected_producer = {
        "read": ("context.output", "5"),
        "merge": ("context.merge", "6"),
    }[view["derivation"]["operation"]]
    require(origin["output_id"] == payload["view_ref"]["output_id"]
            and (origin["component_id"], origin["component_version"]) == expected_producer,
            "context_view_producer_invalid",
            "Native context lineage requires its exact registered read or merge producer")
    context.host_call("context:validate", "validate-context", payload)
    return payload


def _output(config, inputs, context):
    record = context.object_read(config["object_key"])
    retained = context.host_call("context:read", "read-context", {"object_key": config["object_key"]})
    require(retained["object"] == record, "context_invalid_read", "Read must use its exact authorized frozen object")
    return {"output": read_native_context_v5(
        record, context.workflow_session_id, config["object_key"], retained["view"])}


def _assembly(config, inputs, context):
    from .context_prompt_v6 import assemble_native_context_prompt
    _validate(context, inputs["view"])
    return {"output": assemble_native_context_prompt(
        inputs.get("materials", []), inputs["view"], inputs["current_input"],
        context_ref=_exact_input(context, "view"), current_input_ref=_exact_input(context, "current_input"),
        source_output_refs=context.input_artifact_refs("materials"))}


def _merge_legacy(config, inputs, context):
    from .agent_executor import resolve_input
    from .agent_receipt_contract import validate_agent_receipts
    from .context_prompt_v6 import validate_native_context_prompt
    from .context_update import prove_agent_execution

    view = validate_native_context_view_v5(inputs["view"])
    receipts = validate_agent_receipts(inputs["receipts"])
    require(receipts["schema_version"] == 1
            and receipts["owner"]["workflow_session_id"] == context.workflow_session_id
            and receipts["owner"]["chain_run_id"] == context.chain_run_id
            and view["owner"] == {"workflow_session_id": context.workflow_session_id,
                                  "object_key": config["object_key"]},
            "context_execution_owner_mismatch",
            "Merge must target its selected item using execution evidence from this session and chain")
    producer = resolve_input(context, "receipts", receipts)["producer"]
    require(producer == receipts["owner"], "agent_receipt_owner_mismatch",
            "Execution evidence must belong to its exact accepted producer")
    payload = _validate(context, view)
    origin = context.host_call("artifacts:read", "describe-input-origin", {"port": "receipts"})
    require(origin["producer"] == producer
            and (origin["component_id"], origin["component_version"]) in (
                ("agents.execute", "5"), ("agents.execute", "9")),
            "agent_receipt_producer_invalid", "Receipts require their exact active Agent producer")

    def exact_origin_ref(port, receipt_ref=None):
        refs = origin["input_refs"].get(port, [])
        require(len(refs) == (0 if receipt_ref is None else 1)
                and (not refs or refs[0]["output_id"] == receipt_ref["output_id"]),
                "agent_input_origin_mismatch", "Receipts must retain each exact Agent input artifact")
        return refs

    exact_origin_ref("prompt", receipts["frozen_prompt_ref"])
    exact_origin_ref("model", receipts["model_ref"])
    prompt = context.host_call("artifacts:read", "resolve-artifact", {
        "reference": receipts["frozen_prompt_ref"]})["value"]
    prompt = validate_native_context_prompt(prompt)
    require(prompt["context_ref"] == payload["view_ref"] and prompt["context"] == view,
            "context_update_basis_mismatch", "Agent evidence must use this exact loaded context view")
    binding = context.host_call("artifacts:read", "resolve-artifact", {
        "reference": receipts["model_ref"]})["value"]
    policy_refs = origin["input_refs"].get("compaction_policy", [])
    require(len(policy_refs) in (0, 1),
            "context_policy_binding_mismatch", "Agent policy input must be singular when present")
    policy_ref = (None if not policy_refs else
                  {"scope": "artifact", "output_id": policy_refs[0]["output_id"]})
    facts = context.host_call("facts:read", "read-executor-facts", {
        "port": "receipts", "producer": producer, "executor_ref": receipts["executor_ref"],
        "fact_ids": receipts["fact_ids"]})
    require(type(facts) is list and bool(facts)
            and type(facts[-1].get("payload")) is dict
            and facts[-1]["payload"].get("kind") == "agent_result"
            and type(facts[-1]["payload"].get("payload")) is dict
            and type(facts[-1]["payload"]["payload"].get("snapshot")) is dict,
            "agent_result_unaccepted", "Receipts require their exact terminal execution fact")
    terminal = facts[-1]["payload"]["payload"]
    snapshot_payload = terminal["snapshot"]["config"]["payload"]
    policy = snapshot_payload["context_compaction"]["policy"]
    require(snapshot_payload["frozen_compaction_policy_ref"] == policy_ref,
            "context_policy_binding_mismatch",
            "Frozen policy evidence must name the exact accepted Agent policy input")
    execution = prove_agent_execution(receipts, facts, prompt, binding, policy=policy, policy_ref=policy_ref)
    receipts_ref = _exact_input(context, "receipts")
    return {"output": merge_native_context_v5(
        view, payload["view_ref"], receipts, receipts_ref, prompt, execution)}


def _merge(config, inputs, context):
    from .agent_executor import resolve_input
    from .agent_receipt_contract import validate_agent_receipts
    from .context_prompt_v6 import validate_native_context_prompt
    from .context_update_v2 import prove_context_merge_execution_v2
    from .model_request_transport import resolve_model_request_transport
    from .runtime import CanonicalModelAdapter
    from .model_request_contracts import validate_compaction_policy

    view = validate_native_context_view_v5(inputs["view"])
    receipts = validate_agent_receipts(inputs["receipts"])
    require(receipts["schema_version"] == 2
            and receipts["owner"]["workflow_session_id"] == context.workflow_session_id
            and receipts["owner"]["chain_run_id"] == context.chain_run_id
            and view["owner"] == {"workflow_session_id": context.workflow_session_id,
                                  "object_key": config["object_key"]},
            "context_execution_owner_mismatch",
            "Merge must target its selected item using schema 2 execution evidence")
    producer = resolve_input(context, "receipts", receipts)["producer"]
    require(producer == receipts["owner"], "agent_receipt_owner_mismatch",
            "Execution evidence must belong to its exact accepted producer")
    payload = _validate(context, view)
    origin = context.host_call("artifacts:read", "describe-input-origin", {"port": "receipts"})
    require(origin["producer"] == producer
            and (origin["component_id"], origin["component_version"]) == ("agents.execute", "10"),
            "agent_receipt_producer_invalid", "Receipts require their exact Agent 10 producer")

    def exact_origin_ref(port, receipt_ref=None):
        refs = origin["input_refs"].get(port, [])
        require(len(refs) == (0 if receipt_ref is None else 1)
                and (not refs or refs[0]["output_id"] == receipt_ref["output_id"]),
                "agent_input_origin_mismatch", "Receipts must retain each exact Agent input artifact")
        return refs

    exact_origin_ref("prompt", receipts["frozen_prompt_ref"])
    exact_origin_ref("model", receipts["model_ref"])
    prompt = context.host_call("artifacts:read", "resolve-artifact", {
        "reference": receipts["frozen_prompt_ref"]})["value"]
    prompt = validate_native_context_prompt(prompt)
    require(prompt["context_ref"] == payload["view_ref"] and prompt["context"] == view,
            "context_update_basis_mismatch", "Agent evidence must use this exact loaded context view")
    binding = context.host_call("artifacts:read", "resolve-artifact", {
        "reference": receipts["model_ref"]})["value"]
    policy_refs = origin["input_refs"].get("compaction_policy", [])
    policy_ref = deepcopy(receipts["compaction_policy_ref"])
    require(len(policy_refs) in (0, 1),
            "context_policy_binding_mismatch", "Agent policy input must be singular when present")
    require(len(policy_refs) == (0 if policy_ref is None else 1)
            and (not policy_refs or policy_refs[0]["output_id"] == policy_ref["output_id"]),
            "context_policy_binding_mismatch",
            "Receipt policy reference must equal the exact accepted Agent input")
    exact_origin_ref("compaction_policy", policy_ref)
    policy = (None if policy_ref is None else validate_compaction_policy(
        context.host_call("artifacts:read", "resolve-artifact", {"reference": policy_ref})["value"]))
    executor_facts = context.host_call("facts:read", "read-executor-facts", {
        "port": "receipts", "producer": producer, "executor_ref": receipts["executor_ref"],
        "fact_ids": receipts["fact_ids"]})
    model_facts = context.host_call("model-facts:read", "read-model-request-facts", {
        "port": "receipts", "producer": producer, "binding_ref": receipts["model_ref"],
        "requests": receipts["model_requests"]})
    execution = prove_context_merge_execution_v2(
        prompt, binding, receipts, executor_facts, model_facts=model_facts,
        policy=policy, policy_ref=policy_ref, project_messages=CanonicalModelAdapter._project,
        resolve_transport=resolve_model_request_transport)
    receipts_ref = _exact_input(context, "receipts")
    return {"output": merge_native_context_v5(
        view, payload["view_ref"], receipts, receipts_ref, prompt, execution)}


def settle_native_merge(context, outputs, output_refs):
    view = validate_native_context_view_v5(outputs["output"])
    key = view["owner"]["object_key"]
    reference = {"scope": "artifact", "output_id": output_refs["output"]}
    prepared = prepare_native_context_adoption_v5(
        workflow_session_id=context.workflow_session_id, object_key=key,
        object_record=context.object_read(key), view_ref=reference, view=view)
    require(not prepared["already_committed"], "context_settlement_replay_invalid",
            "Acceptance retries reuse their original candidate and operation identity")

    def finalize(staged, refs, receipts):
        require(type(receipts) is list and len(receipts) == 1, "context_receipt_unavailable",
                "Native merge requires its actual accepted object receipt")
        return {"output": deepcopy(view), "commit": validate_context_receipt({
            "schema_version": 3, "kind": "workflow.context-commit", "owner": deepcopy(view["owner"]),
            "view_ref": reference, "adopted_delta_ids": prepared["desired_value"]["accepted_delta_ids"],
            "operation_key": prepared["operation_key"], "receipt": deepcopy(receipts[0])})}

    return {"outputs": {"output": view}, "object_writes": [prepared["write_intent"]], "finalize": finalize}


def register_native_context(host):
    from .context_prompt_v6 import native_context_prompt_references, validate_native_context_prompt

    host.register_data_type(DataTypeDefinition(
        EFFECTIVE_CONTEXT_TYPE, 4, object_schema({
            "messages": {"type": "array"}, "layout": {"type": "array"},
            "accepted_delta_ids": {"type": "array"},
            "generation": {"type": "integer", "minimum": 0}}),
        {"messages": [], "layout": [], "accepted_delta_ids": [], "generation": 0},
        validator=validate_effective_context,
        references=effective_context_references, reference_mapper=preserve_artifact_references,
        write_validator=validate_native_context_write))
    for identity, version, validator, refs in (
        ("CONTEXT_VIEW", 4, validate_native_context_view, native_context_references),
        ("CONTEXT_VIEW", 5, validate_native_context_view_v5, native_context_references_v5),
        ("AGENT_CONTEXT_UPDATE", 1, validate_legacy_graph_context_update, context_update_references),
        ("PROMPT", 6, validate_native_context_prompt, native_context_prompt_references),
    ):
        host.register_data_type(DataTypeDefinition(
            identity, version, {"type": "object"}, scope="content", validator=validator,
            references=refs, reference_mapper=preserve_artifact_references, max_bytes=4_000_000,
            artifact_bindings=(lambda value: value["source_output_refs"]) if identity == "PROMPT" else None))
    config = {"object_key": "context"}
    schema = object_schema({"object_key": {"type": "string", "minLength": 1, "maxLength": 128}})
    view = NodePort("view", "CONTEXT_VIEW", data_schema_version=5)
    host.register_node(NodeDefinition(
        "context.output", "5", "Native context output", "Context", config, schema,
        outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=5),),
        capabilities=("objects:read", "context:read"), input_storage="references",
        object_accesses=({"config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                          "schema_version": 4, "access": "read", "multiple": False},),
        service_requirements=(native_service_requirement("context:read", "read-context"),)), _output)
    host.register_node(NodeDefinition(
        "context.assembly", "5", "Native context assembly", "Context", {}, object_schema({}),
        inputs=(view, NodePort("materials", "PROMPT_MATERIALS", multiple=True, required=False),
                NodePort("current_input", "TEXT", data_schema_version=2)),
        outputs=(NodePort("output", "PROMPT", data_schema_version=6),),
        capabilities=("context:validate", "artifacts:read"), input_storage="references",
        service_requirements=(native_service_requirement("context:validate", "validate-context"),)), _assembly)
    from .model_host_service import model_service_requirement
    host.register_node(NodeDefinition(
        "context.merge", "6", "Merge accepted Agent context", "Context", config, schema,
        inputs=(view, NodePort("receipts", "AGENT_RECEIPTS", data_schema_version=2)),
        outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=5),
                 NodePort("commit", "CONTEXT_COMMIT", data_schema_version=3)),
        capabilities=("objects:read", "objects:write", "context:validate", "artifacts:read", "facts:read",
                      "model-facts:read"),
        input_storage="references",
        object_accesses=({"config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                          "schema_version": 4, "access": "read_write", "multiple": False},),
        service_requirements=(native_service_requirement("context:validate", "validate-context"),
                              model_service_requirement(
                                  "model-facts:read", "read-model-request-facts"))),
        _merge, settlement_builder=settle_native_merge)
