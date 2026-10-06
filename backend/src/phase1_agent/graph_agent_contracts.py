"""Pure contracts for frozen legacy graph Agent identity and accepted evidence."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from .active_run import AcceptedProgress
from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history, validate_turn_final
from .contract_json import canonical_bytes, validate_json_value
from .contracts_v2 import validate_record
from .execution_facts import ExecutionFactHistory
from .model_selection import validate_model_binding
from .prompt_assembly import PromptAssemblyLimits, message_content_chars
from .prompt_errors import PromptProcessingError


GRAPH_AGENT_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a101"
GRAPH_AGENT_OUTPUT_SCHEMA = {
    "type": "object", "properties": {"text": {"type": "string", "minLength": 1}},
    "required": ["text"], "additionalProperties": False,
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _uuid(value: Any) -> None:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ContractValidationError("Agent identity must be a canonical UUID4") from None
    _require(type(value) is str and parsed.version == 4 and str(parsed) == value,
             "Agent identity must be a canonical UUID4")


@dataclass(frozen=True)
class GraphAgentIdentity:
    workflow_session_id: str
    node_binding_id: str
    chain_run_id: str
    node_run_id: str
    base_commit_id: str

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            _uuid(value)


def validate_graph_agent_accepted(value: Any) -> dict:
    """Validate archived evidence independently of any active kernel workspace."""
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {
        "schema_version", "kind", "identity", "snapshot", "turn", "facts", "limits", "progress",
    } and type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "workflow.agent-accepted", "Graph Agent accepted package fields differ")
    _require(type(value["identity"]) is dict and set(value["identity"]) == {
        "workflow_session_id", "node_binding_id", "chain_run_id", "node_run_id", "base_commit_id",
    } and type(value["progress"]) is dict and set(value["progress"]) == {
        "model_requests", "attempts", "accepted_messages",
    }, "Graph Agent accepted identity or progress fields differ")
    identity = GraphAgentIdentity(**value["identity"])
    snapshot = validate_record("input_snapshot", value["snapshot"])
    turn = validate_record("turn", value["turn"])
    binding = validate_model_binding(snapshot["config"]["payload"].get("model_binding"))
    _require(snapshot["component_id"] == GRAPH_AGENT_COMPONENT
             and snapshot["component_version"] == "graph-2"
             and snapshot["config"]["owner_component_id"] == GRAPH_AGENT_COMPONENT
             and snapshot["config"]["payload"].get("graph_identity") == value["identity"]
             and snapshot["model_parameters"] == binding["parameters"]
             and snapshot["output_schema"] == GRAPH_AGENT_OUTPUT_SCHEMA,
             "Graph Agent accepted snapshot changed its registered contract")
    _require(snapshot["workflow_session_id"] == identity.workflow_session_id
             and snapshot["node_binding_id"] == identity.node_binding_id
             and turn["run_id"] == identity.node_run_id
             and turn["snapshot_id"] == snapshot["snapshot_id"]
             and turn["input_id"] == snapshot["input_id"]
             and turn["parent_turn_id"] == snapshot["parent_turn_id"]
             and turn["projection_version"] == snapshot["projection_version"],
             "Graph Agent accepted package ownership differs")
    _validate_limits(value["limits"])
    progress = AcceptedProgress(**value["progress"])
    facts = _validated_facts(value["facts"], snapshot, identity)
    accepted = [fact["payload"]["message"] for fact in facts if fact["kind"] == "message_accepted"]
    requests = sum(fact["kind"] == "model_request" for fact in facts)
    started = {fact["payload"]["attempt_id"] for fact in facts if fact["kind"] == "model_attempt_started"}
    finished = {fact["payload"]["attempt_id"] for fact in facts if fact["kind"] == "model_attempt_finished"}
    _require(canonical_bytes(accepted) == canonical_bytes(turn["messages"])
             and progress == AcceptedProgress(requests, len(started), len(accepted))
             and started == finished and 1 <= requests <= value["limits"]["max_model_requests"]
             and requests <= len(started) <= value["limits"]["max_model_attempts"],
             "Graph Agent result differs from accepted execution facts")
    validate_message_history(snapshot["s0"] + turn["messages"])
    validate_turn_final(turn, snapshot)
    return copy.deepcopy(value)


def _validated_facts(facts: list[dict], snapshot: dict, identity: GraphAgentIdentity) -> list[dict]:
    _require(type(facts) is list, "Graph Agent facts must be an array")
    history = ExecutionFactHistory(snapshot, owner={
        "run_id": identity.node_run_id, "chain_run_id": identity.chain_run_id,
        "workflow_session_id": identity.workflow_session_id, "node_binding_id": identity.node_binding_id,
        "snapshot_id": snapshot["snapshot_id"],
    })
    for fact in facts:
        history.append(fact)
        if fact["kind"] == "model_request":
            _check_request_capacity(snapshot, fact["payload"]["messages"])
    return history.facts


def _check_request_capacity(snapshot: dict, messages: list[dict]) -> None:
    evidence = snapshot["config"]["payload"].get("graph_preparation", {})
    prompt = evidence.get("prompt")
    limits = (PromptAssemblyLimits(**prompt["manifest"]["limits"])
              if type(prompt) is dict else PromptAssemblyLimits())
    tool_chars = len(canonical_bytes(snapshot["tool_definitions"]).decode("utf-8"))
    if len(messages) > limits.max_messages or message_content_chars(messages) + tool_chars > limits.max_total_chars:
        raise PromptProcessingError("prompt_capacity_exceeded", "Graph model request exceeds its total capacity limit")


def _validate_limits(limits: dict) -> dict:
    _require(type(limits) is dict and set(limits) == {"max_model_requests", "max_model_attempts"}
             and type(limits["max_model_requests"]) is int and 1 <= limits["max_model_requests"] <= 64
             and type(limits["max_model_attempts"]) is int and 1 <= limits["max_model_attempts"] <= 256,
             "Graph Agent execution budget is invalid")
    return copy.deepcopy(limits)
