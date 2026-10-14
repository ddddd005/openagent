"""Native-context Agent execution for the installed model protocols."""

from .agent_executor import PublicAgentExecutor
from .agent_receipt_contract import (
    agent_receipts_data_type_definition, agent_receipts_data_type_definition_v2,
)
from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import object_schema
from .context_compaction_policy import compaction_policy_exports, register_compaction_policy
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import InformationSourceDefinition, InformationSourceReference
from .model_execution_contracts import AGENT_EXECUTOR_REF_V2
from .provider_metadata import validate_thinking_summary
from .runtime_executor_contracts import ExecutorDefinition, ExecutorReference, PauseSupport

AGENT_PACKAGE_ID = "workflow.agents"
AGENT_EXECUTOR_REF_V2_REGISTRATION = ExecutorReference(**AGENT_EXECUTOR_REF_V2)


def thinking_summary_page(reader, request):
    """Publish summaries without exposing private protocol metadata."""
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
            summaries.append({"message_id": message["message_id"],
                              "request_id": message["source"]["request_id"],
                              "thinking_summary": summary})
    return {"items": summaries, "next_cursor": page["next_cursor"], "status": page["status"]}


def create_agent_package(*, tools=None, kernel=None, information_reader=None):
    policy_exports = compaction_policy_exports()

    def register(host):
        from .model_host_service import model_service_requirement
        register_compaction_policy(host)
        host.register_shared_data_type(agent_receipts_data_type_definition())
        host.register_shared_data_type(agent_receipts_data_type_definition_v2())
        fact_schema = object_schema({
                "kind": {"enum": ["model_request", "model_result", "tool_dispatch", "tool_settled",
                                  "message_accepted", "execution_failed", "context_compaction_decided",
                                  "context_compaction_applied", "agent_result"]},
                "payload": {"type": "object"}, "created_at": {"type": "string"},
            })
        host.register_executor(
            ExecutorDefinition(AGENT_EXECUTOR_REF_V2_REGISTRATION, continuation_mode="same_process",
                               fact_schema=fact_schema),
            lambda config, inputs, context: PublicAgentExecutor(
                config, inputs, context, tools=tools, kernel=kernel, schema_version=2))
        host.register_pause_support(PauseSupport(AGENT_EXECUTOR_REF_V2_REGISTRATION),
                                    lambda handle, checkpoint: handle.retains(checkpoint))
        host.register_node(NodeDefinition(
            "agents.execute", "10", "Agent execution", "Agent", {}, object_schema({}),
            inputs=(NodePort("prompt", "PROMPT", data_schema_version=6),
                    NodePort("model", "MODEL_BINDING", data_schema_version=5),
                    NodePort("compaction_policy", "CONTEXT_COMPACTION_POLICY",
                             data_schema_version=2, required=False)),
            outputs=(NodePort("result", "TEXT", data_schema_version=2),
                     NodePort("receipts", "AGENT_RECEIPTS", data_schema_version=2)),
            capabilities=("models:call", "model-projection:read", "artifacts:read"),
            input_storage="references",
            service_requirements=(
                model_service_requirement(
                    "models:call", "prepare-request", "dispatch-request", "discard-prepared-request"),
                model_service_requirement("model-projection:read", "measure-context"),
            ),
            provided_session_item_types=({"type_id": "workflow.effective-context",
                                          "schema_version": 4},)),
            None,
            executor_ref=AGENT_EXECUTOR_REF_V2_REGISTRATION)
        host.register_information_source(InformationSourceDefinition(
            InformationSourceReference("workflow.agents.execute-10.facts", "1"),
            "agents.execute", "10", "execution-facts", "workflow.executor-facts",
            item_schema={"type": "object"}, source_scope="history", max_page_bytes=4_000_000),
            history_reader=information_reader)
        host.register_information_source(InformationSourceDefinition(
            InformationSourceReference("workflow.agents.execute-10.thinking", "1"),
            "agents.execute", "10", "thinking-summaries", "workflow.thinking-summaries",
            item_schema=object_schema({
                "message_id": {"type": "string"}, "request_id": {"type": "string"},
                "thinking_summary": {"type": "string", "minLength": 1, "maxLength": 131072},
            }), source_scope="history", discover_public=True, read_public=True, max_page_bytes=4_000_000),
            history_reader=(lambda request: thinking_summary_page(information_reader, request))
            if information_reader is not None else None)

    return CapabilityPackage(PackageManifest(
        AGENT_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.context", "1.0.0"),
                                  PackageDependency("workflow.models", "1.0.0")),
        exports={"data_types": [
                                {"scope": "content", "type_id": "AGENT_RECEIPTS", "schema_version": 1},
                                {"scope": "content", "type_id": "AGENT_RECEIPTS", "schema_version": 2},
                                *policy_exports["data_types"]],
                 "nodes": [{"component_id": "agents.execute", "component_version": "10"}]
                           + policy_exports["nodes"],
                 "executors": [AGENT_EXECUTOR_REF_V2_REGISTRATION.to_dict()],
                 "pause_support": [AGENT_EXECUTOR_REF_V2_REGISTRATION.to_dict()],
                 "information_sources": [
                     InformationSourceReference("workflow.agents.execute-10.facts", "1").to_dict(),
                     InformationSourceReference("workflow.agents.execute-10.thinking", "1").to_dict()]},
        schema_version=4), register)
