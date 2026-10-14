"""Independent schema 2 kernel proof over accepted host and M2 fact streams."""

from copy import deepcopy

from jsonschema import Draft202012Validator

from .context_compaction import replace_compacted_messages
from .context_prompt_v6 import validate_native_context_prompt
from .context_v4 import final_from_context_operations
from .contract_graph import (
    validate_context_message_history, validate_pending_context_message_history,
)
from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .contracts_v2 import validate_record
from .execution_facts import _timestamp
from .graph_contracts import require, uuid4_string
from .model_execution_contracts import (
    AGENT_EXECUTOR_REF_V2, model_request_stream_id, prove_model_request, snapshot_wire_tools,
    validate_agent_receipts_v2, validate_compaction_proof, validate_input_snapshot_v2,
    validate_model_execution_request,
)
from .model_request_contracts import validate_compaction_policy, validate_model_binding
from .provider_metadata import remap_provider_metadata
from .runtime_hosting import InvocationOwner


_COMPONENT_ID = "9c5a4e10-c67c-4d3d-9df8-2c43cf9e19c4"
_OUTPUT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string", "minLength": 1}},
                  "required": ["text"], "additionalProperties": False}
_EVENT_FIELDS = {
    "model_request": {"request_id", "request_index", "messages", "tools", "settings_id",
                      "reserved_model_fact_ids"},
    "model_result": {"request_id", "settings_id", "result"},
    "message_accepted": {"message"},
    "tool_dispatch": {"tool_call_id", "tool_execution_id"},
    "tool_settled": {"tool_call_id", "tool_execution_id", "outcome", "message"},
    "context_compaction_decided": {"decision_id", "preparation"},
    "context_compaction_applied": {"kind", "compaction_id", "before_message_ids",
                                   "covered_message_ids", "checkpoint", "after_message_ids",
                                   "request", "result", "capacity", "trigger_decision"},
    "agent_result": {"snapshot_id", "unit_id", "final", "model_requests", "attempts", "snapshot"},
}


def _fields(value, fields, code="agent_receipt_fact_mismatch"):
    require(type(value) is dict and set(value) == set(fields), code,
            "Accepted execution evidence fields differ from the exact contract")


def _accepted_envelopes(facts, receipts):
    validate_json_value(facts)
    require(type(facts) is list and 0 < len(facts) <= 16384,
            "agent_receipt_fact_mismatch", "Execution requires its bounded accepted fact stream")
    require([fact.get("fact_id") for fact in facts if type(fact) is dict] == receipts["fact_ids"],
            "agent_receipt_fact_mismatch", "Receipts must retain all execution facts in accepted order")
    events, previous_generation = [], 0
    for sequence, envelope in enumerate(facts, 1):
        _fields(envelope, {"schema_version", "owner", "executor_ref", "generation",
                           "sequence", "fact_id", "payload"})
        uuid4_string(envelope["fact_id"])
        require(type(envelope["schema_version"]) is int and envelope["schema_version"] == 1
                and envelope["owner"] == receipts["owner"]
                and envelope["executor_ref"] == AGENT_EXECUTOR_REF_V2
                and type(envelope["sequence"]) is int and envelope["sequence"] == sequence
                and type(envelope["generation"]) is int
                and envelope["generation"] >= max(1, previous_generation),
                "agent_receipt_fact_mismatch", "Executor facts changed owner, order or generation")
        previous_generation = envelope["generation"]
        event = envelope["payload"]
        _fields(event, {"kind", "payload", "created_at"})
        require(_timestamp(event["created_at"]) and type(event["kind"]) is str
                and event["kind"] in _EVENT_FIELDS,
                "agent_receipt_fact_mismatch", "Unknown or malformed completed executor event")
        _fields(event["payload"], _EVENT_FIELDS[event["kind"]])
        events.append(event)
    require(events[-1]["kind"] == "agent_result"
            and all(event["kind"] != "agent_result" for event in events[:-1]),
            "agent_result_unaccepted", "A complete execution has exactly one terminal result")
    return events


def _trusted_model_facts(value, receipts):
    _fields(value, {"requests"}, "model_execution_fact_mismatch")
    require(type(value["requests"]) is list and len(value["requests"]) == len(receipts["model_requests"]),
            "model_execution_fact_mismatch", "Reader must return every consumed model request exactly once")
    facts = {}
    for item, receipt in zip(value["requests"], receipts["model_requests"]):
        _fields(item, {"request_id", "settings_id", "facts"}, "model_execution_fact_mismatch")
        require(item["request_id"] == receipt["request_id"] and item["settings_id"] == receipt["settings_id"]
                and type(item["facts"]) is list
                and [fact.get("fact_id") for fact in item["facts"] if type(fact) is dict] == receipt["fact_ids"],
                "model_execution_fact_mismatch", "Reader order or original accepted identities differ")
        facts[item["request_id"]] = deepcopy(item["facts"])
    return facts


def _assistant_matches(message, result, snapshot, seen_upstream):
    require(message["role"] == "assistant"
            and message["source"] == {"kind": "model", "request_id": result["request_id"]},
            "agent_model_response_mismatch", "Accepted assistant must name its actual model request")
    blocks, mapping = [], {}
    if result["content"]:
        blocks.append({"kind": "text", "text": result["content"]})
    calls = [block for block in message["blocks"] if block["kind"] == "tool_call"]
    require(len(calls) == len(result["tool_calls"]), "agent_model_response_mismatch",
            "Accepted assistant calls differ from the original model result")
    definitions = {tool["name"]: tool for tool in snapshot["tool_definitions"]}
    for accepted, original in zip(calls, result["tool_calls"]):
        require(original["id"] not in seen_upstream and original["name"] in definitions,
                "agent_model_response_mismatch", "Provider call identity or frozen tool is invalid")
        seen_upstream.add(original["id"])
        arguments = loads_strict(original["raw_arguments"])
        require(type(arguments) is dict, "agent_model_response_mismatch", "Tool arguments must be an object")
        if original["name"] != "final_answer":
            require(Draft202012Validator(definitions[original["name"]]["parameters_schema"]).is_valid(arguments),
                    "agent_model_response_mismatch", "Tool arguments differ from the frozen schema")
        blocks.append({"kind": "tool_call", "tool_call_id": accepted["tool_call_id"],
                       "tool_name": original["name"],
                       "tool_definition_version": definitions[original["name"]]["version"],
                       "raw_arguments": original["raw_arguments"], "parsed_arguments": arguments})
        mapping[original["id"]] = accepted["tool_call_id"]
    require(message["blocks"] == blocks
            and result["finish_reason"] in ("stop", "tool_calls")
            and ((result["finish_reason"] == "stop" and not calls and bool(result["content"]))
                 or (result["finish_reason"] == "tool_calls" and bool(calls))),
            "agent_model_response_mismatch", "Canonical assistant must preserve original content and calls")
    require(not any(call["tool_name"] == "final_answer" for call in calls) or len(calls) == 1,
            "agent_model_response_mismatch", "Final answer must be the exclusive call")
    expected_metadata = {}
    if result["thinking_summary"] is not None:
        expected_metadata["thinking_summary"] = result["thinking_summary"]
    if result["provider_metadata"] is not None:
        expected_metadata["provider_metadata"] = remap_provider_metadata(
            result["provider_metadata"], mapping,
            calls=[{"id": call["tool_call_id"], "name": call["tool_name"],
                    "raw_arguments": call["raw_arguments"]} for call in calls])
    require(message["schema_version"] == (5 if expected_metadata else 1)
            and {key: value for key, value in message.items()
             if key in ("thinking_summary", "provider_metadata")} == expected_metadata,
            "agent_model_response_mismatch", "Signed provider metadata changed across the canonical boundary")
    return calls


def prove_execution_facts_v2(executor_facts, prompt, binding, receipts, *, model_facts,
                             policy=None, policy_ref=None, project_messages, resolve_transport):
    """Verify the complete accepted round without adapting schema 2 to legacy facts."""
    prompt, binding = validate_native_context_prompt(prompt), validate_model_binding(binding)
    receipts = validate_agent_receipts_v2(receipts)
    owner = InvocationOwner.from_dict(receipts["owner"]).to_dict()
    events = _accepted_envelopes(executor_facts, receipts)
    trusted = _trusted_model_facts(model_facts, receipts)
    terminal = events[-1]["payload"]
    snapshot = validate_input_snapshot_v2(terminal["snapshot"])
    config = snapshot["config"]["payload"]
    require(snapshot["component_id"] == _COMPONENT_ID
            and snapshot["snapshot_id"] == terminal["snapshot_id"] == receipts["snapshot_id"]
            and terminal["unit_id"] == receipts["unit_id"]
            and snapshot["workflow_session_id"] == owner["workflow_session_id"]
            and snapshot["node_binding_id"] == owner["node_binding_id"]
            and snapshot["input_id"] == prompt["current_input_ref"]["output_id"]
            and snapshot["s0"] == prompt["messages"] and snapshot["output_schema"] == _OUTPUT_SCHEMA
            and config["public_model_binding"] == binding
            and config["frozen_prompt_ref"] == receipts["frozen_prompt_ref"]
            and config["model_ref"] == receipts["model_ref"]
            and config["context_compaction"]["layout"] == prompt["layout"],
            "agent_snapshot_mismatch", "Snapshot differs from the actual frozen accepted inputs")
    if policy is not None:
        policy = validate_compaction_policy(policy)
    require(config["context_compaction"]["policy"] == policy
            and receipts["compaction_policy_ref"] == policy_ref
            and config["frozen_compaction_policy_ref"] == policy_ref
            and ((policy is None) == (policy_ref is None)),
            "agent_snapshot_mismatch", "Compaction differs from its exact accepted policy")
    if policy_ref is not None:
        from .context_contract import artifact_ref
        artifact_ref(policy_ref)

    effective, operations, accepted, requested = deepcopy(snapshot["s0"]), [], [], []
    seen_messages = {message["message_id"] for message in effective}
    seen_calls, seen_executions, seen_upstream = set(), set(), set()
    current, response, decision = None, None, None
    pending, dispatch, settled, feedback_request = [], None, None, None
    uncertain, completed_final, feedback_count, final_errors = False, False, 0, 0
    tools = snapshot_wire_tools(snapshot)
    for event in events[:-1]:
        kind, value = event["kind"], event["payload"]
        require(not completed_final, "agent_result_unaccepted", "No activity may follow successful final settlement")
        if kind == "context_compaction_decided":
            require(current is None and response is None and not pending and decision is None
                    and feedback_request is None,
                    "agent_execution_order_mismatch", "Compaction must start at a closed model boundary")
            uuid4_string(value["decision_id"])
            decision = deepcopy(value)
        elif kind == "model_request":
            require(current is None and response is None and not pending and feedback_request is None,
                    "agent_execution_order_mismatch", "Request bypassed an unresolved accepted response or tool batch")
            request = validate_model_execution_request(value)
            require(request["request_index"] == len(requested) + 1
                    and request["request_id"] in trusted
                    and request["request_id"] not in {item["request_id"] for item in requested},
                    "agent_execution_order_mismatch", "Request order differs from its accepted receipt list")
            proof = prove_model_request(
                request, trusted[request["request_id"]], owner=owner, binding=binding,
                stream_id=model_request_stream_id(request["request_id"]),
                project_messages=project_messages, resolve_transport=resolve_transport)
            purpose = proof["settings"]["purpose"]
            require(proof["result"] is not None
                    and ((decision is None and purpose == "normal" and request["messages"] == effective
                          and request["tools"] == tools)
                         or (decision is not None and purpose == "compaction"
                             and request["messages"][:-1] == effective
                             and request["tools"] == [])),
                    "agent_execution_order_mismatch", "Request does not use its exact preceding effective context")
            requested.append({"request_id": request["request_id"], "settings_id": request["settings_id"],
                              "fact_ids": [fact["fact_id"] for fact in proof["facts"]]})
            current = proof
        elif kind == "model_result":
            require(current is not None and response is None
                    and value["request_id"] == current["request"]["request_id"]
                    and value["settings_id"] == current["request"]["settings_id"]
                    and value["result"] == current["result"],
                    "agent_model_response_mismatch", "Kernel result differs from accepted original M2 response")
            response = deepcopy(value["result"])
        elif kind == "context_compaction_applied":
            require(decision is not None and current is not None and response is not None
                    and current["settings"]["purpose"] == "compaction"
                    and value["trigger_decision"] == {**decision, "discard": {
                        "request_id": decision["preparation"]["request_id"], "discarded": True}}
                    and value["request"] == current["request"] and value["result"] == response,
                    "agent_execution_order_mismatch", "Applied maintenance differs from its actual decision and result")
            operation = validate_compaction_proof(
                value, snapshot=snapshot, owner=owner, model_facts=trusted,
                project_messages=project_messages, resolve_transport=resolve_transport,
                accepted_messages=accepted)
            require(operation["before_message_ids"] == [message["message_id"] for message in effective],
                    "agent_execution_order_mismatch", "Maintenance replaced another context boundary")
            require(operation["checkpoint"]["message_id"] not in seen_messages
                    and operation["request"]["messages"][-1]["message_id"] not in seen_messages,
                    "agent_execution_order_mismatch", "Maintenance message identity was reused")
            seen_messages.update((operation["checkpoint"]["message_id"],
                                  operation["request"]["messages"][-1]["message_id"]))
            effective = replace_compacted_messages(
                effective, operation["covered_message_ids"], operation["checkpoint"])
            operations.append(operation)
            current, response, decision = None, None, None
        elif kind == "message_accepted":
            message = validate_record("agent_message", value["message"])
            require(message["message_id"] not in seen_messages,
                    "agent_execution_order_mismatch", "Accepted message identity was reused")
            if message["role"] == "assistant":
                require(current is not None and response is not None and decision is None and not pending,
                        "agent_execution_order_mismatch", "Assistant has no unresolved normal result")
                calls = _assistant_matches(message, response, snapshot, seen_upstream)
                for call in calls:
                    require(call["tool_call_id"] not in seen_calls,
                            "agent_execution_order_mismatch", "Canonical tool identity was reused")
                    seen_calls.add(call["tool_call_id"])
                pending = deepcopy(calls)
                feedback_request = response["request_id"] if response["finish_reason"] == "stop" else None
                current, response = None, None
                uncertain = False
            elif message["role"] == "tool":
                require(pending and settled is not None and settled["message"] == message,
                        "agent_execution_order_mismatch", "Tool message lacks its exact preceding settlement")
                completed_final = (pending[0]["tool_name"] == "final_answer"
                                   and settled["outcome"] == "success")
                pending.pop(0)
                dispatch, settled = None, None
            else:
                require(feedback_request is not None and message["role"] == "user"
                        and feedback_count == 0
                        and message["source"] == {"kind": "protocol_feedback", "request_id": feedback_request}
                        and message["blocks"] == [{"kind": "text",
                                                  "text": "Call final_answer with a valid JSON answer."}],
                        "agent_execution_order_mismatch", "Feedback does not derive from a plain normal response")
                feedback_request = None
                feedback_count += 1
            effective.append(deepcopy(message))
            seen_messages.add(message["message_id"])
            accepted.append(message)
            operations.append({"kind": "append", "message": message})
            if pending:
                validate_pending_context_message_history(effective, [call["tool_call_id"] for call in pending])
            else:
                validate_context_message_history(effective)
        elif kind == "tool_dispatch":
            uuid4_string(value["tool_execution_id"])
            require(pending and dispatch is None and settled is None and not uncertain
                    and value["tool_call_id"] == pending[0]["tool_call_id"]
                    and value["tool_execution_id"] not in seen_executions,
                    "agent_execution_order_mismatch", "Tool dispatch bypassed its accepted call order")
            dispatch = deepcopy(value)
            seen_executions.add(value["tool_execution_id"])
        elif kind == "tool_settled":
            require(pending and settled is None and value["tool_call_id"] == pending[0]["tool_call_id"],
                    "agent_execution_order_mismatch", "Tool settlement bypassed its accepted call")
            outcome = value["outcome"]
            require(outcome in ("success", "error", "outcome_unknown", "interrupted", "never_started"),
                    "agent_execution_order_mismatch", "Completed execution cannot claim an unknown tool outcome")
            if outcome == "never_started":
                require(uncertain and dispatch is None and value["tool_execution_id"] is None,
                        "agent_execution_order_mismatch", "Never-started tool cannot claim a dispatch")
            else:
                require(dispatch is not None
                        and value["tool_execution_id"] == dispatch["tool_execution_id"],
                        "agent_execution_order_mismatch", "Tool settlement has no exact dispatch")
            message = validate_record("agent_message", value["message"])
            require(message["role"] == "tool" and len(message["blocks"]) == 1,
                    "agent_execution_order_mismatch", "Settlement requires a complete tool result")
            block = message["blocks"][0]
            require(block["kind"] == "tool_result" and block["tool_call_id"] == value["tool_call_id"]
                    and block["tool_execution_id"] == value["tool_execution_id"]
                    and block["model_visible_text"] == canonical_bytes(block["content"]).decode("utf-8"),
                    "agent_execution_order_mismatch", "Settlement changed canonical result identity or text")
            if outcome in ("success", "error"):
                require(message["schema_version"] == 1
                        and message["source"] == {"kind": "tool", "tool_execution_id": value["tool_execution_id"]}
                        and block["status"] == outcome and block["is_error"] == (outcome == "error"),
                        "agent_execution_order_mismatch", "Settlement status differs from the actual result")
                if pending[0]["tool_name"] == "final_answer" and outcome == "success":
                    answer = pending[0]["parsed_arguments"].get("answer")
                    require(Draft202012Validator(_OUTPUT_SCHEMA).is_valid(answer) and block["content"] == answer
                            and Draft202012Validator(next(tool["parameters_schema"] for tool in
                                snapshot["tool_definitions"] if tool["name"] == "final_answer")).is_valid(
                                    pending[0]["parsed_arguments"]),
                            "agent_result_unaccepted", "Final settlement differs from the original answer call")
                elif pending[0]["tool_name"] == "final_answer":
                    arguments = pending[0]["parsed_arguments"]
                    argument_schema = next(tool["parameters_schema"] for tool in
                                           snapshot["tool_definitions"] if tool["name"] == "final_answer")
                    valid_answer = (Draft202012Validator(argument_schema).is_valid(arguments)
                                    and Draft202012Validator(_OUTPUT_SCHEMA).is_valid(arguments.get("answer")))
                    require(not valid_answer and final_errors == 0
                            and block["content"] == {"error": "Invalid final answer."},
                            "agent_result_unaccepted", "Final correction must derive from the actual invalid answer")
                    final_errors += 1
            else:
                require(message["schema_version"] == 2
                        and message["source"]["kind"] == "runtime_tool_observation"
                        and message["source"]["reason_code"] == outcome
                        and message["source"]["tool_execution_id"] == value["tool_execution_id"],
                        "agent_execution_order_mismatch", "Observation differs from its actual uncertain outcome")
                uncertain = True
            settled = deepcopy(value)
    require(completed_final and not pending and current is None and response is None and decision is None
            and feedback_request is None and requested == receipts["model_requests"]
            and type(terminal["model_requests"]) is int and terminal["model_requests"] == len(requested)
            and type(terminal["attempts"]) is int and terminal["attempts"] == len(requested),
            "agent_result_unaccepted", "Terminal result skipped a boundary or modified original request counters")
    final = final_from_context_operations(operations)
    require(terminal["final"] == final, "agent_result_unaccepted",
            "Terminal answer differs from its successful accepted control pair")
    return {"snapshot": snapshot, "operations": operations, "messages": accepted,
            "final": final, "model_facts": trusted, "effective_messages": effective}
