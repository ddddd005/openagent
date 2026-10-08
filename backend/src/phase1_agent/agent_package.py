"""Native-context Agent execution for the installed model protocols."""

from .agent_executor import AGENT_EXECUTOR_REF, PublicAgentExecutor
from .agent_receipt_contract import receipts_references, validate_agent_receipts
from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import object_schema
from .context_compaction_policy import compaction_policy_exports, register_compaction_policy
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import DataTypeDefinition, InformationSourceDefinition, InformationSourceReference
from .provider_metadata import validate_thinking_summary
from .runtime_executor_contracts import ExecutorDefinition, PauseSupport

AGENT_PACKAGE_ID = "workflow.agents"


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
        host.register_data_type(DataTypeDefinition(
            "AGENT_RECEIPTS", 1, {"type": "object"}, scope="content",
            validator=validate_agent_receipts, references=receipts_references, max_bytes=1_000_000))
        host.register_executor(ExecutorDefinition(
            AGENT_EXECUTOR_REF, continuation_mode="same_process",
            fact_schema=object_schema({
                "kind": {"enum": ["model_request", "model_attempt_started", "model_attempt_finished",
                                 "tool_dispatch", "tool_settled", "message_accepted", "execution_failed",
                                 "context_compaction_started", "context_compaction_finished",
                                 "context_compaction_applied", "agent_result"]},
                "payload": {"type": "object"}, "created_at": {"type": "string"},
            })), lambda config, inputs, context: PublicAgentExecutor(
                config, inputs, context, tools=tools, kernel=kernel))
        host.register_pause_support(PauseSupport(AGENT_EXECUTOR_REF),
                                    lambda handle, checkpoint: handle.retains(checkpoint))
        for version, binding_version, title in (
            ("4", 2, "Agent execution"), ("8", 4, "Gemini Agent execution"),
        ):
            host.register_node(NodeDefinition(
                "agents.execute", version, title, "Agent", {}, object_schema({}),
                inputs=(NodePort("prompt", "PROMPT", data_schema_version=6),
                        NodePort("model", "MODEL_BINDING", data_schema_version=binding_version),
                        NodePort("compaction_policy", "CONTEXT_COMPACTION_POLICY", required=False)),
                outputs=(NodePort("result", "TEXT", data_schema_version=2),
                         NodePort("context", "AGENT_CONTEXT_UPDATE")),
                capabilities=("models:call", "artifacts:read"), input_storage="references",
                service_requirements=(model_service_requirement(
                    "models:call", "kernel-model", "kernel-compaction"),)),
                None, executor_ref=AGENT_EXECUTOR_REF)
            host.register_information_source(InformationSourceDefinition(
                InformationSourceReference("workflow.agents.execute-" + version + ".facts", "1"),
                "agents.execute", version, "execution-facts", "workflow.executor-facts",
                item_schema={"type": "object"}, source_scope="history", max_page_bytes=4_000_000),
                history_reader=information_reader)
        host.register_information_source(InformationSourceDefinition(
            InformationSourceReference("workflow.agents.execute-8.thinking", "1"),
            "agents.execute", "8", "thinking-summaries", "workflow.thinking-summaries",
            item_schema=object_schema({
                "message_id": {"type": "string"}, "request_id": {"type": "string"},
                "thinking_summary": {"type": "string", "minLength": 1, "maxLength": 131072},
            }), source_scope="history", discover_public=True, read_public=True, max_page_bytes=4_000_000),
            history_reader=(lambda request: thinking_summary_page(information_reader, request))
            if information_reader is not None else None)

    return CapabilityPackage(PackageManifest(
        AGENT_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.context", "1.0.0"),
                                  PackageDependency("workflow.models", "1.0.0")),
        exports={"data_types": [{"scope": "content", "type_id": "AGENT_RECEIPTS", "schema_version": 1},
                                *policy_exports["data_types"]],
                 "nodes": [{"component_id": "agents.execute", "component_version": version}
                           for version in ("4", "8")] + policy_exports["nodes"],
                 "executors": [AGENT_EXECUTOR_REF.to_dict()],
                 "pause_support": [AGENT_EXECUTOR_REF.to_dict()],
                 "information_sources": [
                     InformationSourceReference("workflow.agents.execute-" + version + ".facts", "1").to_dict()
                     for version in ("4", "8")] + [
                     InformationSourceReference("workflow.agents.execute-8.thinking", "1").to_dict()]},
        schema_version=4), register)
