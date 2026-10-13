"""Schema 2 context candidates and accepted M2/kernel proof, without live I/O."""

from copy import deepcopy

from .agent_receipt_contract import projection_id
from .context_compaction import is_compactable_text, replace_compacted_messages
from .context_contract import _fields, artifact_ref
from .context_prompt_v6 import validate_native_context_prompt
from .context_v4 import _project_context_update_view, final_from_context_operations, validate_native_context_view
from .contract_graph import validate_message_history, validate_pending_message_history
from .contract_json import validate_json_value
from .contracts_v2 import validate_record
from .graph_contracts import require, uuid4_string
from .model_execution_contracts import (
    replay_model_context_operations, validate_agent_receipts_v2, validate_model_execution_request,
)
from .model_request_contracts import validate_model_result, validate_operation_payload


def validate_context_operations_v2(operations):
    """Validate/replay shape only; accepted model evidence is required separately."""
    validate_json_value(operations)
    require(type(operations) is list and 0 < len(operations) <= 4096,
            "context_update_invalid", "An update requires a bounded accepted operation timeline")
    for operation in operations:
        require(type(operation) is dict, "context_update_invalid", "Operation must be an object")
        if operation.get("kind") == "append":
            _fields(operation, ("kind", "message"))
            message = validate_record("agent_message", operation["message"])
            require((message["role"] == "assistant" and message["source"]["kind"] == "model")
                    or message["role"] == "tool"
                    or (message["role"] == "user" and message["source"]["kind"] == "protocol_feedback"),
                    "context_update_invalid", "Only kernel increments can enter an update")
            continue
        _fields(operation, ("kind", "compaction_id", "before_message_ids", "covered_message_ids",
                            "checkpoint", "after_message_ids", "request", "result", "capacity",
                            "trigger_decision"))
        require(operation["kind"] == "compact", "context_update_invalid", "Unknown context operation")
        uuid4_string(operation["compaction_id"])
        for field in ("before_message_ids", "covered_message_ids", "after_message_ids"):
            ids = operation[field]
            require(type(ids) is list and 0 < len(ids) <= 4096 and len(set(ids)) == len(ids),
                    "context_update_invalid", "Compaction retains ordered distinct message identities")
            for identity in ids:
                uuid4_string(identity)
        validate_model_execution_request(operation["request"])
        result = validate_model_result(operation["result"])
        checkpoint = validate_record("agent_message", operation["checkpoint"])
        require(checkpoint["source"] == {"kind": "context_checkpoint",
                                         "compaction_id": operation["compaction_id"]}
                and result["finish_reason"] == "stop" and result["tool_calls"] == [],
                "context_update_invalid", "Compaction requires its summary and checkpoint")
        _fields(operation["capacity"], ("before", "after"))
        for measurement in operation["capacity"].values():
            validate_operation_payload("measure-context", measurement, response=True)
        decision = operation["trigger_decision"]
        _fields(decision, ("decision_id", "preparation", "discard"))
        uuid4_string(decision["decision_id"])
        validate_operation_payload("prepare-request", decision["preparation"], response=True)
        validate_operation_payload("discard-prepared-request", decision["discard"], response=True)
    return deepcopy(operations)


def _candidate_replay(prompt, operations):
    messages, pending = deepcopy(prompt["messages"]), []
    seen = {message["message_id"] for message in messages}
    compactions = set()
    for operation in operations:
        if operation["kind"] == "append":
            message = operation["message"]
            require(message["message_id"] not in seen, "context_update_invalid", "Message identity was reused")
            seen.add(message["message_id"])
            messages.append(deepcopy(message))
            for block in message["blocks"]:
                if block["kind"] == "tool_call":
                    pending.append(block["tool_call_id"])
                elif block["kind"] == "tool_result":
                    require(pending and pending[0] == block["tool_call_id"],
                            "context_update_invalid", "Tool results must close the accepted call order")
                    pending.pop(0)
            if pending:
                validate_pending_message_history(messages, pending)
            else:
                validate_message_history(messages)
        else:
            require(not pending and operation["before_message_ids"] == [m["message_id"] for m in messages]
                    and operation["request"]["messages"][:-1] == messages
                    and operation["compaction_id"] not in compactions
                    and operation["checkpoint"]["message_id"] not in seen,
                    "context_update_invalid", "Compaction must retain its preceding closed context")
            covered = operation["covered_message_ids"]
            require([m["message_id"] for m in messages if m["message_id"] in covered] == covered
                    and all(is_compactable_text(m) for m in messages if m["message_id"] in covered),
                    "context_update_invalid", "Compaction requires an ordered plain-text range")
            messages = replace_compacted_messages(messages, covered, operation["checkpoint"])
            require(operation["after_message_ids"] == [m["message_id"] for m in messages],
                    "context_update_invalid", "Checkpoint replacement differs from the declared context")
            seen.add(operation["checkpoint"]["message_id"])
            compactions.add(operation["compaction_id"])
            validate_message_history(messages)
    require(not pending, "context_update_invalid", "Context cannot adopt unfinished tool calls")
    return {"messages": messages, "generation_delta": len(compactions)}


def validate_context_update_v2(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "update_id", "basis_view_ref",
                    "basis_revision", "compaction_policy_ref", "operations", "next_view", "receipts"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 2
            and value["kind"] == "workflow.agent-context-update",
            "context_update_invalid", "Schema 2 update requires its exact envelope")
    receipts = validate_agent_receipts_v2(value["receipts"])
    view = validate_native_context_view(value["next_view"])
    require(value["owner"] == receipts["owner"]
            and value["update_id"] == projection_id(receipts["owner"]["node_run_id"], "context-update")
            and value["basis_revision"] == view["basis"]
            and view["owner"]["workflow_session_id"] == receipts["owner"]["workflow_session_id"]
            and view["owner"]["agent_node_id"] == receipts["owner"]["node_binding_id"],
            "context_update_owner_mismatch", "Candidate cannot change its accepted owner or basis")
    artifact_ref(value["basis_view_ref"])
    if value["compaction_policy_ref"] is not None:
        artifact_ref(value["compaction_policy_ref"])
    validate_context_operations_v2(value["operations"])
    return deepcopy(value)


def make_context_update_v2(prompt, operations, receipts, final, *, effective_messages, policy_ref=None):
    """Construct a candidate without reading an as-yet-unaccepted producer output."""
    prompt = validate_native_context_prompt(prompt)
    receipts = validate_agent_receipts_v2(receipts)
    operations = validate_context_operations_v2(operations)
    replayed = _candidate_replay(prompt, operations)
    require(replayed["messages"] == effective_messages, "agent_projection_mismatch",
            "Candidate operations must reproduce the kernel's complete effective context")
    next_view = _project_context_update_view(prompt, operations, receipts, final, replayed)
    return validate_context_update_v2({
        "schema_version": 2, "kind": "workflow.agent-context-update", "owner": receipts["owner"],
        "update_id": projection_id(receipts["owner"]["node_run_id"], "context-update"),
        "basis_view_ref": prompt["context_ref"], "basis_revision": prompt["context"]["basis"],
        "compaction_policy_ref": deepcopy(policy_ref), "operations": operations,
        "next_view": next_view, "receipts": receipts,
    })


def build_execution_update_v2(prompt, binding, receipts, executor_facts, *, model_facts,
                              policy=None, policy_ref=None, project_messages, resolve_transport):
    from .execution_facts_v2 import prove_execution_facts_v2
    proof = prove_execution_facts_v2(
        executor_facts, prompt, binding, receipts, model_facts=model_facts,
        policy=policy, policy_ref=policy_ref,
        project_messages=project_messages, resolve_transport=resolve_transport)
    replayed = replay_model_context_operations(
        proof["snapshot"], proof["operations"], owner=receipts["owner"],
        model_facts=proof["model_facts"], project_messages=project_messages,
        resolve_transport=resolve_transport, accepted_messages=proof["messages"])
    return make_context_update_v2(
        prompt, proof["operations"], receipts, proof["final"],
        effective_messages=replayed["messages"], policy_ref=policy_ref)


def validate_execution_update_v2(packet, executor_facts, prompt, binding, *, model_facts,
                                 policy=None, policy_ref=None, project_messages, resolve_transport):
    packet = validate_context_update_v2(packet)
    expected = build_execution_update_v2(
        prompt, binding, packet["receipts"], executor_facts, model_facts=model_facts,
        policy=policy, policy_ref=policy_ref,
        project_messages=project_messages, resolve_transport=resolve_transport)
    require(packet == expected, "agent_projection_mismatch",
            "Context update differs from its complete accepted execution and model facts")
    return final_from_context_operations(expected["operations"])
