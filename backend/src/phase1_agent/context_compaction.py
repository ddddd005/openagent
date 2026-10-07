"""Native context maintenance without changing append-only execution history."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Sequence
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history
from .contract_json import canonical_bytes, validate_json_value
from .contracts_v2 import validate_record
from .context_compaction_policy import frame_checkpoint


TokenCounter = Callable[[Sequence[dict[str, Any]], Sequence[dict[str, Any]]], int]
_LAYOUT_KINDS = frozenset(("fixed", "history", "summary", "once", "current"))
_RESULT_FIELDS = frozenset((
    "text", "finish_reason", "tool_calls", "usage", "response_id", "model",
))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _uuid(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def validate_compaction_settings(value: Any, messages: list[dict]) -> dict | None:
    if value is None:
        return None
    from .context_compaction_policy import validate_compaction_policy
    validate_json_value(value)
    required = {
        "policy", "context_window_tokens", "output_reserve_tokens", "layout",
    }
    optional = {"summary_max_tokens", "max_cold_input_tokens"}
    _require(type(value) is dict and required <= set(value) <= required | optional,
             "Native compaction settings require an exact frozen descriptor")
    _require(type(value["context_window_tokens"]) is int
             and value["context_window_tokens"] > 0
             and type(value["output_reserve_tokens"]) is int
             and 0 <= value["output_reserve_tokens"] < value["context_window_tokens"],
             "Native compaction requires a known window and a valid output reserve")
    if value["policy"] is not None:
        validate_compaction_policy(value["policy"])
    for field in optional & set(value):
        _require(type(value[field]) is int and value[field] > 0,
                 "Native maintenance budgets must be positive")
    if "summary_max_tokens" in value:
        _require(value["summary_max_tokens"] <= value["output_reserve_tokens"],
                 "Summary output exceeds the frozen output reserve")
    layout = value["layout"]
    _require(type(layout) is list and len(layout) == len(messages),
             "Compaction layout must identify each frozen input message")
    for item in layout:
        _require(type(item) is dict and set(item) in (
            {"kind", "round_id"}, {"kind", "round_id", "compaction"},
        )
                 and type(item["kind"]) is str and item["kind"] in _LAYOUT_KINDS
                 and item.get("compaction", "allowed") in ("allowed", "never")
                 and (item["round_id"] is None
                      or type(item["round_id"]) is str and bool(item["round_id"])),
                 "Invalid frozen compaction layout")
    return deepcopy(value)


def policy_config(settings: dict | None) -> dict | None:
    if settings is None or settings["policy"] is None:
        return None
    from .context_compaction_policy import validate_compaction_policy
    policy = validate_compaction_policy(settings["policy"])
    return policy


def compaction_parameters(parameters: dict, settings: dict | None) -> dict:
    result = deepcopy(parameters)
    if settings is not None and "summary_max_tokens" in settings:
        result["max_tokens"] = settings["summary_max_tokens"]
    return result


def measure_context_capacity(
    messages: Sequence[dict], tools: Sequence[dict], settings: dict,
    token_counter: TokenCounter | None = None,
) -> dict:
    # UTF-8 bytes deliberately overestimate common text tokenization; this is
    # an identified estimate, not a claim to a provider's exact tokenizer.
    tokens = (len(canonical_bytes({"messages": list(messages), "tools": list(tools)}))
              if token_counter is None else token_counter(deepcopy(messages), deepcopy(tools)))
    _require(type(tokens) is int and tokens >= 0, "Token counter returned an invalid capacity")
    reserve = settings["output_reserve_tokens"]
    return {
        "input_tokens": tokens, "output_reserve_tokens": reserve,
        "total_tokens": tokens + reserve,
        "context_window_tokens": settings["context_window_tokens"],
        "token_count_kind": "utf8_bytes_estimate" if token_counter is None else "provided",
    }


def validate_capacity(value: Any) -> dict:
    _require(type(value) is dict and set(value) == {
        "input_tokens", "output_reserve_tokens", "total_tokens",
        "context_window_tokens", "token_count_kind",
    }, "Compaction capacity evidence fields differ")
    _require(all(type(value[key]) is int and value[key] >= 0 for key in
                 ("input_tokens", "output_reserve_tokens", "total_tokens"))
             and type(value["context_window_tokens"]) is int
             and value["context_window_tokens"] > 0
             and value["total_tokens"] == value["input_tokens"] + value["output_reserve_tokens"]
             and type(value["token_count_kind"]) is str
             and value["token_count_kind"] in ("utf8_bytes_estimate", "provided"),
             "Invalid compaction capacity evidence")
    return deepcopy(value)


def at_safety_watermark(capacity: dict) -> bool:
    return capacity["total_tokens"] * 10 >= capacity["context_window_tokens"] * 9


def should_compact(capacity: dict, policy: dict) -> bool:
    return at_safety_watermark(capacity) or (
        policy["trigger_tokens"] > 0 and capacity["input_tokens"] >= policy["trigger_tokens"]
    )


def is_compactable_text(message: dict) -> bool:
    return (message["role"] in ("user", "assistant")
            and all(block["kind"] == "text" for block in message["blocks"])
            and message["source"]["kind"] not in (
                "protocol_feedback", "context_compaction_instruction", "upstream_node",
            ))


def eligible_compaction_ids(
    initial_messages: list[dict], effective_messages: list[dict], settings: dict,
    accepted_messages: Sequence[dict] = (),
) -> list[str]:
    config = policy_config(settings)
    if config is None:
        return []
    layout = {message["message_id"]: item for message, item in
              zip(initial_messages, settings["layout"])}
    rounds = []
    for message in effective_messages:
        item = layout.get(message["message_id"])
        if item and item["kind"] == "history" and item["round_id"] is not None:
            if item["round_id"] not in rounds:
                rounds.append(item["round_id"])
    depth = config["keep_depth"]
    retained = set(rounds[-depth:]) if depth else set()
    generated = {message["message_id"] for message in accepted_messages
                 if message["role"] == "assistant" and message["source"]["kind"] == "model"
                 and is_compactable_text(message)}
    selected = []
    for message in effective_messages:
        if not is_compactable_text(message):
            continue
        identity = message["message_id"]
        item = layout.get(identity)
        checkpoint = message["source"]["kind"] == "context_checkpoint"
        if checkpoint or identity in generated or (
            item and item["kind"] in ("history", "summary", "once")
            and item.get("compaction", "allowed") == "allowed"
            and item["round_id"] not in retained
        ):
            selected.append(identity)
    return selected


def checkpoint_message(compaction_id: str, message_id: str, text: str) -> dict:
    return validate_record("agent_message", {
        "schema_version": 4, "message_id": message_id, "role": "user",
        "source": {"kind": "context_checkpoint", "compaction_id": compaction_id},
        "blocks": [{"kind": "text", "text": frame_checkpoint(text)}],
    })


def compaction_instruction(compaction_id: str, message_id: str, prompt: str,
                           covered_message_ids: list[str], messages: list[dict]) -> dict:
    positions = [index + 1 for index, message in enumerate(messages)
                 if message["message_id"] in covered_message_ids]
    text = (
        prompt + "\n\n"
        "Only summarize the permitted conversation messages at these 1-based "
        "message positions: " + ", ".join(map(str, positions)) + ". "
        "The other messages are reference material and remain unchanged. "
        "Return checkpoint text only. Do not call tools or perform the task."
    )
    return validate_record("agent_message", {
        "schema_version": 4, "message_id": message_id, "role": "user",
        "source": {"kind": "context_compaction_instruction", "compaction_id": compaction_id},
        "blocks": [{"kind": "text", "text": text}],
    })


def replace_compacted_messages(messages: list[dict], covered: list[str],
                               checkpoint: dict) -> list[dict]:
    _require(type(covered) is list and bool(covered)
             and all(type(identity) is str for identity in covered)
             and len(set(covered)) == len(covered),
             "Compaction must cover a nonempty unique ordered message set")
    selected = set(covered)
    _require([message["message_id"] for message in messages
              if message["message_id"] in selected] == covered,
             "Compaction coverage differs from the current view order")
    _require(all(is_compactable_text(message) for message in messages
                 if message["message_id"] in selected),
             "Compaction cannot replace current input, protocol feedback or tool messages")
    result, inserted = [], False
    for message in messages:
        if message["message_id"] in selected:
            if not inserted:
                result.append(deepcopy(checkpoint))
                inserted = True
        else:
            result.append(deepcopy(message))
    validate_message_history(result)
    return result


def response_evidence(response: Any) -> dict:
    return {
        "text": response.content, "finish_reason": response.finish_reason,
        "tool_calls": [{"id": call.id, "name": call.name,
                        "raw_arguments": call.raw_arguments, "type": call.type}
                       for call in response.tool_calls],
        "usage": deepcopy(response.usage), "response_id": response.response_id,
        "model": response.model,
    }


def validate_summary_result(value: Any, *, successful: bool = False) -> dict:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == _RESULT_FIELDS
             and (value["text"] is None or type(value["text"]) is str)
             and type(value["finish_reason"]) is str
             and type(value["tool_calls"]) is list
             and all(type(call) is dict for call in value["tool_calls"])
             and (value["usage"] is None or type(value["usage"]) is dict)
             and all(value[key] is None or type(value[key]) is str
                     for key in ("response_id", "model")),
             "Invalid context summary response evidence")
    if successful:
        _require(value["finish_reason"] == "stop" and not value["tool_calls"]
                 and type(value["text"]) is str and bool(value["text"].strip()),
                 "Context compaction only accepts nonempty plain text without tool calls")
    return deepcopy(value)


def validate_compaction_request(value: Any, compaction_id: str) -> dict:
    _require(type(value) is dict and set(value) == {"messages", "tools", "model_parameters"}
             and type(value["messages"]) is list and len(value["messages"]) >= 2
             and type(value["tools"]) is list and all(type(tool) is dict for tool in value["tools"])
             and type(value["model_parameters"]) is dict,
             "Invalid context compaction request evidence")
    validate_message_history(value["messages"])
    instruction = value["messages"][-1]
    _require(instruction["role"] == "user"
             and instruction["source"] == {
                 "kind": "context_compaction_instruction", "compaction_id": compaction_id,
             } and bool(instruction["blocks"][0]["text"].strip()),
             "Compaction requires its own final user maintenance instruction")
    return deepcopy(value)


def validate_compaction_operation(value: Any) -> dict:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {
        "kind", "compaction_id", "before_message_ids", "covered_message_ids",
        "checkpoint", "after_message_ids", "request", "result", "capacity",
    } and value["kind"] == "compact" and _uuid(value["compaction_id"]),
             "Invalid context compaction operation")
    for field in ("before_message_ids", "covered_message_ids", "after_message_ids"):
        _require(type(value[field]) is list and all(_uuid(item) for item in value[field])
                 and len(set(value[field])) == len(value[field]),
                 "Compaction message identities must be unique canonical UUID4")
    request = validate_compaction_request(value["request"], value["compaction_id"])
    result = validate_summary_result(value["result"], successful=True)
    before = request["messages"][:-1]
    _require(value["before_message_ids"] == [message["message_id"] for message in before],
             "Compaction request prefix differs from its before view")
    checkpoint = validate_record("agent_message", value["checkpoint"])
    _require(checkpoint["source"] == {
        "kind": "context_checkpoint", "compaction_id": value["compaction_id"],
    } and checkpoint["role"] == "user" and checkpoint["blocks"] == [
        {"kind": "text", "text": frame_checkpoint(result["text"])}
    ] and checkpoint["message_id"] not in value["before_message_ids"],
             "Compaction checkpoint differs from its accepted summary")
    after = replace_compacted_messages(before, value["covered_message_ids"], checkpoint)
    _require(value["after_message_ids"] == [message["message_id"] for message in after],
             "Compaction operation does not prove its after view")
    _require(type(value["capacity"]) is dict and set(value["capacity"]) == {"before", "after"},
             "Compaction capacity must prove both views")
    prior, current = (validate_capacity(value["capacity"][key]) for key in ("before", "after"))
    _require(prior["context_window_tokens"] == current["context_window_tokens"]
             and prior["output_reserve_tokens"] == current["output_reserve_tokens"]
             and prior["token_count_kind"] == current["token_count_kind"]
             and current["input_tokens"] < prior["input_tokens"]
             and not at_safety_watermark(current),
             "Compaction changed its capacity measurement basis")
    return deepcopy(value)
