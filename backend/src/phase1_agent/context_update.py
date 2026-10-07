"""Ordered native context updates and their immutable input references."""

from copy import deepcopy

from .agent_receipt_contract import projection_id, validate_agent_receipts
from .context_contract import (
    EFFECTIVE_CONTEXT_TYPE, MAX_ACCEPTED_DELTAS, _fields, _ids, _unique_refs,
    artifact_ref, validate_effective_context,
)
from .context_compaction_policy import frame_checkpoint
from .context_compaction import is_compactable_text, validate_compaction_operation
from .contract_graph import validate_message_history, validate_pending_message_history
from .contract_json import content_digest, validate_json_value
from .contracts_v2 import validate_record
from .graph_contracts import require, uuid4_string
from .host_sdk import WriteIntent


_MAX_OPERATIONS = 4096
_COMPACT_FIELDS = (
    "kind", "compaction_id", "before_message_ids", "covered_message_ids",
    "checkpoint", "after_message_ids", "request", "result", "capacity",
)


def _message_ids(messages):
    return [message["message_id"] for message in messages]


def _identity_list(value, *, nonempty=False):
    require(type(value) is list and len(value) <= _MAX_OPERATIONS
            and (bool(value) or not nonempty), "context_update_invalid",
            "Message identities require a bounded ordered list")
    for identity in value:
        uuid4_string(identity)
    require(len(value) == len(set(value)), "context_update_duplicate_identity",
            "Message identities cannot repeat")


def _text_message(message):
    return message["role"] in ("user", "assistant") and all(
        block["kind"] == "text" for block in message["blocks"])


def _closed_or_pending(messages):
    pending = []
    for message in messages:
        for block in message["blocks"]:
            if block["kind"] == "tool_call":
                pending.append(block["tool_call_id"])
            elif block["kind"] == "tool_result":
                require(bool(pending) and pending[0] == block["tool_call_id"],
                        "context_update_tool_unclosed", "Tool results must retain their accepted pairing")
                pending.pop(0)
    if pending:
        validate_pending_message_history(messages, pending)
    else:
        validate_message_history(messages)
    return pending


def _measurement(value):
    _fields(value, ("input_tokens", "output_reserve_tokens", "total_tokens",
                    "context_window_tokens", "token_count_kind"))
    require(all(type(value[key]) is int and value[key] >= 0
                for key in ("input_tokens", "output_reserve_tokens", "total_tokens"))
            and type(value["context_window_tokens"]) is int and value["context_window_tokens"] > 0
            and value["total_tokens"] == value["input_tokens"] + value["output_reserve_tokens"]
            and value["token_count_kind"] in ("utf8_bytes_estimate", "provided"),
            "context_update_invalid_capacity", "Capacity evidence must retain its declared units and reserve")


def validate_context_operations(operations):
    """Validate shape only; replay and accepted-fact proof are separate gates."""
    validate_json_value(operations)
    require(type(operations) is list and len(operations) <= _MAX_OPERATIONS,
            "context_update_invalid", "Context operations require a bounded ordered timeline")
    for operation in operations:
        require(type(operation) is dict, "context_update_invalid", "A context operation must be an object")
        if operation.get("kind") == "append":
            _fields(operation, ("kind", "message"))
            validate_record("agent_message", operation["message"])
            require(operation["message"]["source"]["kind"] not in (
                "context_checkpoint", "context_compaction_instruction"),
                "context_update_invalid_append", "Maintenance messages are not ordinary accepted appends")
            continue
        _fields(operation, _COMPACT_FIELDS)
        require(operation["kind"] == "compact", "context_update_invalid", "Context operation is unsupported")
        identity = uuid4_string(operation["compaction_id"])
        for field in ("before_message_ids", "covered_message_ids", "after_message_ids"):
            _identity_list(operation[field], nonempty=True)
        checkpoint = validate_record("agent_message", operation["checkpoint"])
        require(checkpoint["schema_version"] == 4 and checkpoint["role"] == "user"
                and checkpoint["source"] == {"kind": "context_checkpoint", "compaction_id": identity}
                and len(checkpoint["blocks"]) == 1 and _text_message(checkpoint),
                "context_update_invalid_checkpoint", "A checkpoint is derived context, not a human round")
        request = operation["request"]
        _fields(request, ("messages", "tools", "model_parameters"))
        require(type(request["messages"]) is list and bool(request["messages"])
                and type(request["tools"]) is list and type(request["model_parameters"]) is dict,
                "context_update_invalid_proof", "Maintenance requires its exact request projection")
        for message in request["messages"]:
            validate_record("agent_message", message)
        instruction = request["messages"][-1]
        require(instruction["schema_version"] == 4 and instruction["role"] == "user"
                and instruction["source"] == {"kind": "context_compaction_instruction", "compaction_id": identity}
                and _text_message(instruction) and len(instruction["blocks"]) == 1
                and bool(instruction["blocks"][0]["text"].strip()),
                "context_update_invalid_proof", "Maintenance instructions must be appended at the request tail")
        result = operation["result"]
        _fields(result, ("text", "finish_reason", "tool_calls", "usage", "response_id", "model"))
        require(type(result["text"]) is str and bool(result["text"].strip())
                and result["finish_reason"] == "stop" and result["tool_calls"] == []
                and (result["usage"] is None or type(result["usage"]) is dict)
                and all(value is None or type(value) is str
                        for value in (result["response_id"], result["model"])),
                "context_update_invalid_summary", "Accepted summaries require nonempty text without tool calls")
        require(checkpoint["blocks"] == [{"kind": "text", "text": frame_checkpoint(result["text"])}],
                "context_update_invalid_checkpoint", "Checkpoint text must frame its exact accepted summary")
        _fields(operation["capacity"], ("before", "after"))
        _measurement(operation["capacity"]["before"])
        _measurement(operation["capacity"]["after"])
        require(operation["capacity"]["before"]["context_window_tokens"]
                == operation["capacity"]["after"]["context_window_tokens"]
                and operation["capacity"]["before"]["output_reserve_tokens"]
                == operation["capacity"]["after"]["output_reserve_tokens"]
                and operation["capacity"]["before"]["token_count_kind"]
                == operation["capacity"]["after"]["token_count_kind"]
                and operation["capacity"]["after"]["input_tokens"]
                < operation["capacity"]["before"]["input_tokens"]
                and operation["capacity"]["after"]["total_tokens"] * 10
                < operation["capacity"]["after"]["context_window_tokens"] * 9,
                "context_update_invalid_capacity", "A maintenance measurement cannot change its declared window")
    return deepcopy(operations)


def replay_context_operations(initial_messages, operations, *, eligible_message_ids,
                              expected_append_messages=None, expected_compactions=None,
                              expected_tools=None, expected_model_parameters=None):
    """Reproduce the effective view, keeping raw accepted facts append-only."""
    operations = validate_context_operations(operations)
    require(type(initial_messages) is list and len(initial_messages) <= _MAX_OPERATIONS,
            "context_update_invalid", "An initial context must be a bounded canonical message list")
    messages = [validate_record("agent_message", message) for message in initial_messages]
    validate_message_history(messages)
    _identity_list(eligible_message_ids)
    eligible = set(eligible_message_ids)
    require(eligible <= set(_message_ids(messages)), "context_update_invalid_range",
            "Eligibility must name exact initial view messages")
    require(all(is_compactable_text(message)
        for message in messages if message["message_id"] in eligible),
        "context_update_ineligible", "Only explicitly eligible persistent text can be compacted")
    seen_messages = set(_message_ids(messages))
    seen_compactions = {message["source"]["compaction_id"] for message in messages
                        if message["source"]["kind"] == "context_checkpoint"}
    appended, compactions, pending = [], [], []
    for operation in operations:
        if operation["kind"] == "append":
            message = operation["message"]
            identity = message["message_id"]
            require(identity not in seen_messages, "context_update_duplicate_identity",
                    "Compaction cannot make an old message eligible for a second append")
            seen_messages.add(identity)
            messages.append(deepcopy(message))
            require(len(messages) <= _MAX_OPERATIONS, "context_update_invalid",
                    "An effective view cannot exceed its declared message budget")
            appended.append(deepcopy(message))
            if message["role"] == "assistant" and message["source"]["kind"] == "model" and is_compactable_text(message):
                eligible.add(identity)
            pending = _closed_or_pending(messages)
            continue
        require(not pending, "context_update_tool_unclosed",
                "Compaction must wait until every accepted tool batch has closed")
        validate_message_history(messages)
        identity = operation["compaction_id"]
        require(identity not in seen_compactions, "context_update_duplicate_identity",
                "An accepted compaction cannot be applied twice")
        seen_compactions.add(identity)
        before = _message_ids(messages)
        covered = operation["covered_message_ids"]
        require(operation["before_message_ids"] == before, "context_update_order_mismatch",
                "Each compaction must use the exact preceding effective view")
        require(set(covered) <= eligible and covered == [item for item in before if item in set(covered)],
                "context_update_ineligible", "Compaction must retain the selected eligible source order")
        require(operation["request"]["messages"][:-1] == messages,
                "context_update_invalid_proof", "Maintenance must reuse the exact current request prefix")
        validate_message_history(operation["request"]["messages"])
        if expected_tools is not None:
            require(operation["request"]["tools"] == expected_tools, "context_update_invalid_proof",
                    "Maintenance must preserve the frozen tool projection")
        if expected_model_parameters is not None:
            require(operation["request"]["model_parameters"] == expected_model_parameters,
                    "context_update_invalid_proof", "Maintenance must retain the frozen model settings")
        checkpoint = operation["checkpoint"]
        require(checkpoint["message_id"] not in seen_messages
                and checkpoint["message_id"] != operation["request"]["messages"][-1]["message_id"],
                "context_update_duplicate_identity",
                "Checkpoint identities must be fresh even across later replacements")
        seen_messages.add(checkpoint["message_id"])
        first, removed = before.index(covered[0]), set(covered)
        messages = [
            *deepcopy(messages[:first]), deepcopy(checkpoint),
            *(deepcopy(message) for message in messages[first:] if message["message_id"] not in removed),
        ]
        require(operation["after_message_ids"] == _message_ids(messages),
                "context_update_order_mismatch", "Compaction must replace selected text without moving retained messages")
        validate_compaction_operation(operation)
        validate_message_history(messages)
        eligible.difference_update(removed)
        eligible.add(checkpoint["message_id"])
        compactions.append(deepcopy(operation))
    require(not pending, "context_update_tool_unclosed", "Only a closed effective view may be adopted")
    validate_message_history(messages)
    if expected_append_messages is not None:
        require(appended == expected_append_messages, "context_update_append_unproven",
                "Append operations must equal the accepted canonical messages, not a full round appended twice")
    if expected_compactions is not None:
        require(compactions == expected_compactions, "context_update_compaction_unproven",
                "Compaction operations must equal the accepted ordered maintenance facts")
    return {"messages": deepcopy(messages), "generation_delta": len(compactions),
            "appended_message_ids": _message_ids(appended),
            "compaction_ids": [operation["compaction_id"] for operation in compactions]}


def validate_working_context_view(value):
    """Unregistered native view; this is not a runnable graph CONTEXT_VIEW version."""
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "basis", "messages",
                    "accepted_delta_ids", "applied_delta_ids", "once_injected_item_ids", "generation"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.context-working-view", "context_update_invalid",
            "Native working context requires its exact envelope")
    _fields(value["owner"], ("workflow_session_id", "object_key", "agent_node_id"))
    uuid4_string(value["owner"]["workflow_session_id"])
    uuid4_string(value["owner"]["agent_node_id"])
    require(type(value["owner"]["object_key"]) is str and 0 < len(value["owner"]["object_key"]) <= 128,
            "context_update_invalid", "A working view must retain its bound object")
    _fields(value["basis"], ("revision_id", "head_revision"))
    uuid4_string(value["basis"]["revision_id"])
    require(type(value["basis"]["head_revision"]) is int and 1 <= value["basis"]["head_revision"] <= 2**53 - 1,
            "context_update_invalid", "A working view must retain an exact revision fence")
    require(type(value["messages"]) is list and len(value["messages"]) <= _MAX_OPERATIONS,
            "context_update_invalid", "Effective messages require a bounded sequence")
    validate_message_history(value["messages"])
    for field in ("accepted_delta_ids", "applied_delta_ids", "once_injected_item_ids"):
        _ids(value[field])
    require(not set(value["accepted_delta_ids"]) & set(value["applied_delta_ids"]),
            "context_update_already_consumed", "New updates cannot already be adopted")
    require(len(value["accepted_delta_ids"]) + len(value["applied_delta_ids"]) <= MAX_ACCEPTED_DELTAS,
            "context_delta_capacity_exceeded", "Working views cannot exceed the persistent consumption budget")
    require(type(value["generation"]) is int and 0 <= value["generation"] <= 2**53 - 1,
            "context_update_invalid", "Context generation must be monotonic")
    return deepcopy(value)


def validate_context_update(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "update_id", "basis_view_ref",
                    "basis_revision", "compaction_policy_ref", "operations", "next_view", "receipts"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.agent-context-update", "context_update_invalid",
            "An ordered context update requires its exact envelope")
    receipts = validate_agent_receipts(value["receipts"])
    require(value["owner"] == receipts["owner"]
            and value["update_id"] == projection_id(value["owner"]["node_run_id"], "context-update"),
            "context_update_owner_mismatch", "Update identity and receipts must name the actual Agent invocation")
    artifact_ref(value["basis_view_ref"])
    if value["compaction_policy_ref"] is not None:
        artifact_ref(value["compaction_policy_ref"])
    if value["next_view"].get("schema_version") == 4:
        from .context_v4 import validate_native_context_view
        next_view = validate_native_context_view(value["next_view"])
    else:
        next_view = validate_working_context_view(value["next_view"])
    require(next_view["owner"]["workflow_session_id"] == value["owner"]["workflow_session_id"]
            and next_view["owner"]["agent_node_id"] == value["owner"]["node_binding_id"]
            and value["basis_revision"] == next_view["basis"],
            "context_update_owner_mismatch", "An update cannot move ownership or revision basis")
    validate_context_operations(value["operations"])
    require(bool(value["operations"]), "context_update_invalid", "An Agent update must contain accepted operations")
    return deepcopy(value)


def context_update_references(value):
    references = [value["basis_view_ref"], value["receipts"]["frozen_prompt_ref"],
                  value["receipts"]["model_ref"]]
    if value["compaction_policy_ref"] is not None:
        references.append(value["compaction_policy_ref"])
    return _unique_refs(references)


def prove_context_update(initial_view, view_ref, update, *, eligible_message_ids,
                         accepted_messages, accepted_compactions, expected_tools,
                         expected_model_parameters):
    initial_view, update = validate_working_context_view(initial_view), validate_context_update(update)
    require(update["basis_view_ref"] == artifact_ref(view_ref)
            and update["basis_revision"] == initial_view["basis"]
            and update["next_view"]["owner"] == initial_view["owner"],
            "context_update_basis_mismatch", "Update proof must start from its exact frozen view and owner")
    identities = initial_view["accepted_delta_ids"] + initial_view["applied_delta_ids"]
    require(update["update_id"] not in identities, "context_update_already_consumed",
            "An adopted update cannot be replayed as new context")
    require(len(identities) < MAX_ACCEPTED_DELTAS, "context_delta_capacity_exceeded",
            "Consumed identities cannot be discarded during compaction")
    result = replay_context_operations(
        initial_view["messages"], update["operations"], eligible_message_ids=eligible_message_ids,
        expected_append_messages=accepted_messages, expected_compactions=accepted_compactions,
        expected_tools=expected_tools, expected_model_parameters=expected_model_parameters)
    expected = {**deepcopy(initial_view), "messages": result["messages"],
                "applied_delta_ids": initial_view["applied_delta_ids"] + [update["update_id"]],
                "generation": initial_view["generation"] + result["generation_delta"]}
    require(update["next_view"] == expected, "context_update_candidate_mismatch",
            "The candidate must derive from ordered facts without changing consumption or once-injection ledgers")
    return deepcopy(expected)


def prepare_context_update_adoption(*, workflow_session_id, object_key, agent_node_id,
                                    object_record, view_ref, view, object_schema_version=4):
    """Prepare CAS only; the caller still proves artifacts and performs settlement."""
    view = validate_working_context_view(view)
    require(view["owner"] == {"workflow_session_id": workflow_session_id, "object_key": object_key,
                             "agent_node_id": agent_node_id},
            "context_update_owner_mismatch", "An update must target its exact bound Agent and object")
    require((object_record["type_id"], object_record["schema_version"])
            == (EFFECTIVE_CONTEXT_TYPE, object_schema_version) and not object_record["deleted"]
            and object_record["binding"]["object_key"] == object_key,
            "context_object_type_mismatch", "Adoption requires the exact registered object version")
    current = validate_effective_context(object_record["value"])
    desired = {"view_ref": artifact_ref(view_ref),
               "accepted_delta_ids": view["accepted_delta_ids"] + view["applied_delta_ids"]}
    operation_key = "context-update:" + content_digest({
        "owner": view["owner"], "basis": view["basis"], "desired": desired})
    if current == desired:
        return {"desired_value": desired, "write_intent": None, "already_committed": True,
                "operation_key": operation_key}
    require(view["basis"] == {"revision_id": object_record["revision_id"],
                             "head_revision": object_record["revision"]},
            "context_stale_basis", "Adoption retains both exact revision identity and the CAS fence")
    require(current["accepted_delta_ids"] == view["accepted_delta_ids"],
            "context_consumption_mismatch", "Adoption cannot erase consumed updates")
    return {"desired_value": desired, "already_committed": False, "operation_key": operation_key,
            "write_intent": WriteIntent(object_key, object_record["revision"], operation_key, desired).to_dict()}
