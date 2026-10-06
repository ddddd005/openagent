"""Agent-owned first-request failure policy, separate from graph scheduling."""

from .graph_records import require
from .model_host_service import MODEL_SERVICE_REF


def validate_agent_failed_retry(config, evidence):
    owner, facts = evidence["owner"], evidence["facts"]
    require(any(item["component_id"] == "agents.execute" for item in evidence["completed"]),
            "failed_retry_unsupported", "This policy requires an accepted preceding Agent", 409)
    model = [fact for fact in facts if fact.get("kind") == "workflow.model-fact"
             and fact.get("node_run_id") == owner["node_run_id"]]
    executor = [fact for fact in facts if fact.get("owner") == owner]
    events = [fact["payload"] for fact in executor]
    require(len(model) == 3 and [fact["stage"] for fact in model] == ["request", "attempt", "outcome"]
            and [fact["sequence"] for fact in model] == [1, 2, 3]
            and len({fact["request_id"] for fact in model}) == 1
            and len({fact["binding_id"] for fact in model}) == 1
            and all(fact["session_id"] == owner["workflow_session_id"]
                    and fact["chain_run_id"] == owner["chain_run_id"]
                    and fact["node_binding_id"] == owner["node_binding_id"] for fact in model)
            and events and events[-1]["kind"] == "execution_failed"
            and events[-1]["payload"]["model_requests"] == 1
            and events[-1]["payload"]["attempts"] == 1
            and sum(event["kind"] == "model_request" for event in events) == 1
            and sum(event["kind"] == "model_attempt_started" for event in events) == 1
            and not any(event["kind"] in ("tool_dispatch", "tool_settled", "message_accepted", "agent_result")
                        for event in events),
            "failed_retry_evidence_incomplete", "Agent restart requires complete first-request and effect evidence", 409)
    outcome = model[-1]["details"]
    classification = outcome.get("classification")
    require(classification == "not_dispatched"
            or classification == "provider_error" and outcome.get("status_code") in (429, 503),
            "failed_retry_unsafe_outcome", "Unknown or unsupported model outcomes cannot be resent", 409)
    require(outcome["attempt_id"] == model[1]["details"]["attempt_id"],
            "failed_retry_evidence_incomplete", "Failure attempt evidence differs", 409)
    return {"classification": classification, "service_ref": MODEL_SERVICE_REF.to_dict(),
            "service_evidence": {"classification": classification, "request_id": model[0]["request_id"],
                                 "attempt_id": outcome["attempt_id"], "binding_id": model[0]["binding_id"],
                                 "fact_ids": [fact["fact_id"] for fact in model]}}
