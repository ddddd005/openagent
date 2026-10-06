"""Opt-in Agent execution and explicit accepted-result delta projection."""

from copy import deepcopy

from .agent_executor import (
    AGENT_EXECUTOR_REF, PublicAgentExecutor, exact_input, projection_id, receipts_references,
    resolve_input, validate_agent_receipts, validate_execution_projection,
)
from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import object_schema
from .context_contract import validate_agent_delta, validate_context_unit
from .context_prompt import validate_context_ready_prompt
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import DataTypeDefinition, InformationSourceDefinition, InformationSourceReference
from .runtime_executor_contracts import ExecutorDefinition, PauseSupport
from .agent_failed_retry import validate_agent_failed_retry


AGENT_PACKAGE_ID = "workflow.agents"


def _delta(config, inputs, context):
    prompt = validate_context_ready_prompt(inputs["prompt"])
    unit = validate_context_unit(inputs["unit"])
    receipts = validate_agent_receipts(inputs["facts"])
    prompt_record = resolve_input(context, "prompt", prompt)
    unit_record = resolve_input(context, "unit", unit)
    facts_record = resolve_input(context, "facts", receipts)
    require(unit_record["producer"] == facts_record["producer"] == receipts["owner"]
            and receipts["frozen_prompt_ref"]["output_id"] == prompt_record["output_id"],
            "agent_projection_owner_mismatch", "Unit and receipts must belong to one accepted Agent invocation")
    facts = context.host_call("facts:read", "read-executor-facts", {
        "port": "facts", "producer": receipts["owner"], "executor_ref": receipts["executor_ref"],
        "fact_ids": receipts["fact_ids"]})
    validate_execution_projection(unit, receipts, facts, prompt)
    return {"output": validate_agent_delta({
        "schema_version": 1, "kind": "workflow.agent-delta",
        "delta_id": projection_id(context.node_run_id, "agent-delta"),
        "owner": {"workflow_session_id": context.workflow_session_id,
                  "chain_run_id": context.chain_run_id, "node_binding_id": context.node_binding_id,
                  "node_run_id": context.node_run_id},
        "frozen_prompt_ref": exact_input(context, "prompt"), "basis_view_ref": deepcopy(prompt["context_ref"]),
        "current_root_ref": deepcopy(prompt["current_input_ref"]), "unit_ref": exact_input(context, "unit"),
        "unit": unit, "fact_receipt_refs": [exact_input(context, "facts")]})}


def create_agent_package(*, tools=None, kernel=None, information_reader=None):
    """Registration never constructs a kernel, model adapter or live provider."""
    def register(host):
        from .model_host_service import model_service_requirement
        host.register_data_type(DataTypeDefinition(
            "AGENT_RECEIPTS", 1, {"type": "object"}, scope="content", validator=validate_agent_receipts,
            references=receipts_references, max_bytes=1_000_000))
        host.register_executor(ExecutorDefinition(
            AGENT_EXECUTOR_REF, continuation_mode="same_process",
            fact_schema=object_schema({
                "kind": {"enum": ["model_request", "model_attempt_started", "model_attempt_finished",
                                 "tool_dispatch", "tool_settled", "message_accepted", "execution_failed",
                                 "agent_result"]},
                "payload": {"type": "object"},
                "created_at": {"type": "string"},
            })), lambda config, inputs, context: PublicAgentExecutor(
                config, inputs, context, tools=tools, kernel=kernel,
                direct_context=inputs["prompt"].get("schema_version") in (4, 5)))
        host.register_pause_support(PauseSupport(AGENT_EXECUTOR_REF),
                                    lambda handle, checkpoint: handle.retains(checkpoint))
        host.register_node(NodeDefinition(
            "agents.execute", "1", "Agent execution", "Agent",
            {}, object_schema({}),
            inputs=(NodePort("prompt", "PROMPT", data_schema_version=3), NodePort("model", "MODEL_BINDING")),
            outputs=(NodePort("result", "TEXT", data_schema_version=2),
                     NodePort("unit", "CONTEXT_UNIT"), NodePort("facts", "AGENT_RECEIPTS")),
            capabilities=("models:call", "artifacts:read"), input_storage="references",
            service_requirements=(model_service_requirement("models:call", "kernel-model"),)),
            None, executor_ref=AGENT_EXECUTOR_REF)
        host.register_node(NodeDefinition(
            "agents.execute", "2", "Bound Agent execution", "Agent", {}, object_schema({}),
            inputs=(NodePort("prompt", "PROMPT", data_schema_version=4), NodePort("model", "MODEL_BINDING")),
            outputs=(NodePort("result", "TEXT", data_schema_version=2),
                     NodePort("context", "AGENT_CONTEXT")),
            capabilities=("models:call", "artifacts:read"), input_storage="references",
            service_requirements=(model_service_requirement("models:call", "kernel-model"),)),
            None, executor_ref=AGENT_EXECUTOR_REF)
        host.register_node(NodeDefinition(
            "agents.execute", "3", "Summary-aware Agent execution", "Agent", {}, object_schema({}),
            inputs=(NodePort("prompt", "PROMPT", data_schema_version=5), NodePort("model", "MODEL_BINDING")),
            outputs=(NodePort("result", "TEXT", data_schema_version=2), NodePort("context", "AGENT_CONTEXT")),
            capabilities=("models:call", "artifacts:read"), input_storage="references",
            service_requirements=(model_service_requirement("models:call", "kernel-model"),)),
            None, executor_ref=AGENT_EXECUTOR_REF, failed_retry_validator=validate_agent_failed_retry)
        host.register_node(NodeDefinition(
            "agents.delta", "1", "Agent delta", "Agent", {}, object_schema({}),
            inputs=(NodePort("prompt", "PROMPT", data_schema_version=3),
                    NodePort("unit", "CONTEXT_UNIT"), NodePort("facts", "AGENT_RECEIPTS")),
            outputs=(NodePort("output", "AGENT_DELTA"),),
            capabilities=("artifacts:read", "facts:read"), input_storage="references"), _delta)
        for version in ("1", "2", "3"):
            host.register_information_source(InformationSourceDefinition(
                InformationSourceReference("workflow.agents.execute-" + version + ".facts", "1"),
                "agents.execute", version, "execution-facts", "workflow.executor-facts",
                item_schema={"type": "object"}, source_scope="history", max_page_bytes=4_000_000),
                history_reader=information_reader)

    return CapabilityPackage(PackageManifest(
        AGENT_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.context", "1.0.0"),
                                  PackageDependency("workflow.models", "1.0.0")),
        exports={"data_types": [{"scope": "content", "type_id": "AGENT_RECEIPTS", "schema_version": 1}],
                 "nodes": [{"component_id": "agents.execute", "component_version": "1"},
                           {"component_id": "agents.execute", "component_version": "2"},
                           {"component_id": "agents.execute", "component_version": "3"},
                           {"component_id": "agents.delta", "component_version": "1"}],
                 "executors": [AGENT_EXECUTOR_REF.to_dict()],
                 "pause_support": [AGENT_EXECUTOR_REF.to_dict()],
                 "information_sources": [
                     InformationSourceReference("workflow.agents.execute-" + version + ".facts", "1").to_dict()
                     for version in ("1", "2", "3")]},
        schema_version=4), register)
