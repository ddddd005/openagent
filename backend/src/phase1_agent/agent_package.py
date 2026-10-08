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
from .context_compaction_policy import compaction_policy_exports, register_compaction_policy
from .provider_metadata import validate_thinking_summary


AGENT_PACKAGE_ID = "workflow.agents"


def thinking_summary_page(reader, request):
    """Publish only visible summaries, keeping original private fact pagination."""
    page = reader(request)
    require(type(page) is dict and set(page) == {"items", "next_cursor", "status"}
            and type(page["items"]) is list,
            "information_provider_invalid", "Agent fact reader returned an invalid page")
    summaries = []
    for fact in page["items"]:
        event = fact.get("payload", {}) if type(fact) is dict else {}
        if event.get("kind") != "message_accepted":
            continue
        message = event.get("payload", {}).get("message", {})
        if message.get("role") != "assistant" or message.get("source", {}).get("kind") != "model":
            continue
        summary = validate_thinking_summary(message.get("thinking_summary"))
        if summary:
            summaries.append({
                "message_id": message["message_id"],
                "request_id": message["source"]["request_id"],
                "thinking_summary": summary,
            })
    return {"items": summaries, "next_cursor": page["next_cursor"], "status": page["status"]}


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
    policy_exports = compaction_policy_exports()

    def register(host):
        from .model_host_service import model_service_requirement
        register_compaction_policy(host)
        host.register_data_type(DataTypeDefinition(
            "AGENT_RECEIPTS", 1, {"type": "object"}, scope="content", validator=validate_agent_receipts,
            references=receipts_references, max_bytes=1_000_000))
        host.register_executor(ExecutorDefinition(
            AGENT_EXECUTOR_REF, continuation_mode="same_process",
            fact_schema=object_schema({
                "kind": {"enum": ["model_request", "model_attempt_started", "model_attempt_finished",
                                 "tool_dispatch", "tool_settled", "message_accepted", "execution_failed",
                                 "context_compaction_started", "context_compaction_finished",
                                 "context_compaction_applied",
                                 "agent_result"]},
                "payload": {"type": "object"},
                "created_at": {"type": "string"},
            })), lambda config, inputs, context: PublicAgentExecutor(
                config, inputs, context, tools=tools, kernel=kernel,
                direct_context=inputs["prompt"].get("schema_version") in (4, 5, 6)))
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
        host.register_node(NodeDefinition(
            "agents.execute", "4", "原生上下文 Agent", "Agent", {}, object_schema({}),
            inputs=(NodePort("prompt", "PROMPT", data_schema_version=6),
                    NodePort("model", "MODEL_BINDING", data_schema_version=2),
                    NodePort("compaction_policy", "CONTEXT_COMPACTION_POLICY", required=False)),
            outputs=(NodePort("result", "TEXT", data_schema_version=2),
                     NodePort("context", "AGENT_CONTEXT_UPDATE")),
            capabilities=("models:call", "artifacts:read"), input_storage="references",
            service_requirements=(model_service_requirement(
                "models:call", "kernel-model", "kernel-compaction"),)),
            None, executor_ref=AGENT_EXECUTOR_REF)
        for version, prompt_version, binding_version, output, output_version in (
            ("5", 3, 3, "unit", 2),
            ("6", 4, 3, "context", 1),
            ("7", 5, 3, "context", 1),
            ("8", 6, 4, "context", 1),
        ):
            native = version == "8"
            host.register_node(NodeDefinition(
                "agents.execute", version, "Gemini Agent execution", "Agent", {}, object_schema({}),
                inputs=(NodePort("prompt", "PROMPT", data_schema_version=prompt_version),
                        NodePort("model", "MODEL_BINDING", data_schema_version=binding_version),
                        *((NodePort("compaction_policy", "CONTEXT_COMPACTION_POLICY", required=False),)
                          if native else ())),
                outputs=(NodePort("result", "TEXT", data_schema_version=2),
                         NodePort(output, "CONTEXT_UNIT" if output == "unit" else (
                             "AGENT_CONTEXT_UPDATE" if native else "AGENT_CONTEXT"),
                             data_schema_version=output_version),
                         *((NodePort("facts", "AGENT_RECEIPTS"),) if output == "unit" else ())),
                capabilities=("models:call", "artifacts:read"), input_storage="references",
                service_requirements=(model_service_requirement(
                    "models:call", "kernel-model", *(("kernel-compaction",) if native else ())),)),
                None, executor_ref=AGENT_EXECUTOR_REF,
                failed_retry_validator=validate_agent_failed_retry if version == "7" else None)
        host.register_node(NodeDefinition(
            "agents.delta", "2", "Protocol-aware Agent delta", "Agent", {}, object_schema({}),
            inputs=(NodePort("prompt", "PROMPT", data_schema_version=3),
                    NodePort("unit", "CONTEXT_UNIT", data_schema_version=2),
                    NodePort("facts", "AGENT_RECEIPTS")),
            outputs=(NodePort("output", "AGENT_DELTA"),),
            capabilities=("artifacts:read", "facts:read"), input_storage="references"), _delta)
        for version in ("1", "2", "3", "4", "5", "6", "7", "8"):
            host.register_information_source(InformationSourceDefinition(
                InformationSourceReference("workflow.agents.execute-" + version + ".facts", "1"),
                "agents.execute", version, "execution-facts", "workflow.executor-facts",
                item_schema={"type": "object"}, source_scope="history", max_page_bytes=4_000_000),
                history_reader=information_reader)
        for version in ("5", "6", "7", "8"):
            host.register_information_source(InformationSourceDefinition(
                InformationSourceReference("workflow.agents.execute-" + version + ".thinking", "1"),
                "agents.execute", version, "thinking-summaries", "workflow.thinking-summaries",
                item_schema=object_schema({
                    "message_id": {"type": "string"}, "request_id": {"type": "string"},
                    "thinking_summary": {"type": "string", "minLength": 1, "maxLength": 131072},
                }), source_scope="history", discover_public=True, read_public=True,
                max_page_bytes=4_000_000),
                history_reader=(lambda request: thinking_summary_page(information_reader, request))
                if information_reader is not None else None)

    return CapabilityPackage(PackageManifest(
        AGENT_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.context", "1.0.0"),
                                  PackageDependency("workflow.models", "1.0.0")),
        exports={"data_types": [{"scope": "content", "type_id": "AGENT_RECEIPTS", "schema_version": 1},
                                *policy_exports["data_types"]],
                 "nodes": [{"component_id": "agents.execute", "component_version": "1"},
                           {"component_id": "agents.execute", "component_version": "2"},
                           {"component_id": "agents.execute", "component_version": "3"},
                           {"component_id": "agents.execute", "component_version": "4"},
                           {"component_id": "agents.execute", "component_version": "5"},
                           {"component_id": "agents.execute", "component_version": "6"},
                           {"component_id": "agents.execute", "component_version": "7"},
                           {"component_id": "agents.execute", "component_version": "8"},
                           {"component_id": "agents.delta", "component_version": "1"},
                           {"component_id": "agents.delta", "component_version": "2"},
                           *policy_exports["nodes"]],
                 "executors": [AGENT_EXECUTOR_REF.to_dict()],
                 "pause_support": [AGENT_EXECUTOR_REF.to_dict()],
                 "information_sources": [
                     InformationSourceReference("workflow.agents.execute-" + version + ".facts", "1").to_dict()
                     for version in ("1", "2", "3", "4", "5", "6", "7", "8")] + [
                     InformationSourceReference("workflow.agents.execute-" + version + ".thinking", "1").to_dict()
                     for version in ("5", "6", "7", "8")]},
        schema_version=4), register)
