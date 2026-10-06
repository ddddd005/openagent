"""Strict synthetic observations alongside unchanged v1 tool results."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import (
    validate_bundle,
    validate_message_history,
    validate_pending_message_history,
    validate_turn_final,
)
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.contracts_v2 import CONTRACT_SCHEMAS, validate_record


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def batch(*names):
    return {
        "schema_version": 1,
        "message_id": uid(300),
        "role": "assistant",
        "source": {"kind": "model", "request_id": uid(301)},
        "blocks": [
            {
                "kind": "tool_call",
                "tool_call_id": uid(310 + index),
                "tool_name": name,
                "tool_definition_version": "1",
                "raw_arguments": '{"text":"inspect"}',
                "parsed_arguments": {"text": "inspect"},
            }
            for index, name in enumerate(names)
        ],
    }


def observation(index=0, reason="outcome_unknown"):
    execution_id = None if reason == "never_started" else uid(330 + index)
    content = {
        "reason_code": reason,
        "error": "No confirmed successful result is available.",
        "retry_guidance": (
            "Check idempotency and verify external state by call ID. "
            "Ask the user before retrying when effects cannot be verified."
        ),
        "evidence": {"started": reason != "never_started"},
    }
    return {
        "schema_version": 2,
        "message_id": uid(320 + index),
        "role": "tool",
        "source": {
            "kind": "runtime_tool_observation",
            "tool_call_id": uid(310 + index),
            "tool_execution_id": execution_id,
            "reason_code": reason,
        },
        "blocks": [{
            "kind": "tool_result",
            "tool_call_id": uid(310 + index),
            "tool_execution_id": execution_id,
            "status": "error",
            "is_error": True,
            "content": content,
            "model_visible_text": canonical_bytes(content).decode("utf-8"),
        }],
    }


def load_bundle():
    return loads_strict(EXAMPLE.read_text(encoding="utf-8"))


def closed_batch():
    return [
        batch("inspect_text", "inspect_text", "inspect_text"),
        observation(),
        observation(1, "never_started"),
        observation(2, "never_started"),
    ]


def sync_projection(message):
    block = message["blocks"][0]
    block["model_visible_text"] = canonical_bytes(block["content"]).decode("utf-8")


def test_v1_results_and_saved_bundle_remain_unchanged_and_compatible():
    bundle = load_bundle()
    assert validate_bundle(bundle) == bundle
    messages = bundle["turn"][0]["messages"]
    validate_message_history(messages)
    for message in messages:
        assert validate_record("agent_message", message) == message
    legacy_error = copy.deepcopy(messages[1])
    legacy_error["blocks"][0].update(
        status="error",
        is_error=True,
        content={"error": "A known tool failure."},
        model_visible_text="A known tool failure.",
    )
    assert validate_record("agent_message", legacy_error) == legacy_error


@pytest.mark.parametrize("reason", ["never_started", "interrupted", "outcome_unknown"])
def test_v2_observation_round_trip_preserves_real_or_absent_execution_identity(reason):
    message = observation(reason=reason)
    checked = validate_record("agent_message", json.loads(json.dumps(message)))
    assert checked == message
    assert checked is not message
    assert Draft202012Validator(CONTRACT_SCHEMAS["agent_message"]).is_valid(message)
    validate_message_history([batch("inspect_text"), checked])
    checked["blocks"][0]["content"]["evidence"]["started"] = "changed"
    assert type(message["blocks"][0]["content"]["evidence"]["started"]) is bool


def test_multiple_never_started_results_close_in_order_without_fake_execution_ids():
    messages = closed_batch()
    validate_message_history(messages)
    assert [message["blocks"][0]["tool_execution_id"] for message in messages[1:]] == [
        uid(330), None, None,
    ]


def test_turn_and_snapshot_accept_mixed_v1_v2_messages_with_semantic_checks():
    bundle = load_bundle()
    turn = bundle["turn"][0]
    turn["messages"] = [*closed_batch(), *turn["messages"]]
    snapshot = bundle["input_snapshot"][0]
    assert validate_record("turn", turn) == turn
    validate_turn_final(turn, snapshot)
    assert validate_bundle(bundle) == bundle

    frozen = copy.deepcopy(snapshot)
    frozen["s0"] = [*snapshot["s0"], *closed_batch()]
    assert validate_record("input_snapshot", frozen) == frozen
    validate_message_history(frozen["s0"])

    for kind, record, messages_key in (
        ("turn", turn, "messages"),
        ("input_snapshot", frozen, "s0"),
    ):
        bad = copy.deepcopy(record)
        synthetic = next(message for message in bad[messages_key] if message["schema_version"] == 2)
        synthetic["blocks"][0]["content"]["retry_guidance"] = " "
        sync_projection(synthetic)
        with pytest.raises(ContractValidationError):
            validate_record(kind, bad)


@pytest.mark.parametrize("reason", ["never_started", "interrupted", "outcome_unknown"])
@pytest.mark.parametrize("invalid", ["", " ", None, "unrecognized", 1, True])
def test_unknown_or_invalid_reason_codes_are_rejected(reason, invalid):
    message = observation(reason=reason)
    message["source"]["reason_code"] = invalid
    message["blocks"][0]["content"]["reason_code"] = invalid
    sync_projection(message)
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", message)


@pytest.mark.parametrize("field", ["error", "retry_guidance"])
@pytest.mark.parametrize("value", ["", " \t\n", None, {}, 1])
def test_observation_explanation_and_retry_guidance_must_be_nonempty_text(field, value):
    message = observation()
    message["blocks"][0]["content"][field] = value
    sync_projection(message)
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", message)


@pytest.mark.parametrize("field", ["reason_code", "error", "retry_guidance"])
def test_observation_content_requires_all_classification_and_guidance_fields(field):
    message = observation()
    del message["blocks"][0]["content"][field]
    sync_projection(message)
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", message)


@pytest.mark.parametrize("reason", ["never_started", "interrupted", "outcome_unknown"])
def test_never_started_has_no_execution_and_uncertain_started_outcomes_require_uuid(reason):
    message = observation(reason=reason)
    wrong_id = uid(900) if reason == "never_started" else None
    message["source"]["tool_execution_id"] = wrong_id
    message["blocks"][0]["tool_execution_id"] = wrong_id
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", message)
    assert not Draft202012Validator(CONTRACT_SCHEMAS["agent_message"]).is_valid(message)


@pytest.mark.parametrize("mutation", [
    lambda message: message["source"].update(tool_call_id=uid(900)),
    lambda message: message["source"].update(tool_execution_id=uid(900)),
    lambda message: message["source"].update(reason_code="interrupted"),
    lambda message: message["blocks"][0].update(status="success", is_error=False),
    lambda message: message["blocks"][0].update(is_error=False),
    lambda message: message.update(role="assistant"),
    lambda message: message.update(schema_version=1),
    lambda message: message.update(schema_version=3),
    lambda message: message["source"].update(unexpected=True),
    lambda message: message["blocks"].append(copy.deepcopy(message["blocks"][0])),
])
def test_observation_source_status_version_and_block_shape_are_strict(mutation):
    message = observation()
    mutation(message)
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", message)


@pytest.mark.parametrize("projection", [
    "",
    "Outcome unknown.",
    "{}",
    '{"reason_code":"outcome_unknown","reason_code":"never_started"}',
    '{"reason_code":"outcome_unknown","error":"different","retry_guidance":"retry"}',
])
def test_observation_projection_cannot_omit_change_or_duplicate_content(projection):
    message = observation()
    message["blocks"][0]["model_visible_text"] = projection
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", message)


def test_observation_projection_can_use_different_json_formatting_without_changing_values():
    message = observation()
    message["blocks"][0]["model_visible_text"] = json.dumps(
        message["blocks"][0]["content"], indent=2, sort_keys=True,
    )
    assert validate_record("agent_message", message) == message


@pytest.mark.parametrize("mutation", [
    lambda messages: messages.pop(),
    lambda messages: messages.reverse(),
    lambda messages: messages.__setitem__(slice(1, 3), [messages[2], messages[1]]),
    lambda messages: messages[0]["blocks"][1].update(tool_call_id=uid(900)),
    lambda messages: messages.append(copy.deepcopy(messages[-1])),
])
def test_synthetic_results_cannot_bypass_pairing_order_or_identity_guards(mutation):
    messages = closed_batch()
    mutation(messages)
    with pytest.raises(ContractValidationError):
        validate_message_history(messages)


@pytest.mark.parametrize("reason", ["never_started", "interrupted", "outcome_unknown"])
def test_final_answer_cannot_receive_synthetic_observation_even_as_error(reason):
    with pytest.raises(ContractValidationError, match="Final control"):
        validate_message_history([batch("final_answer"), observation(reason=reason)])


def test_duplicate_actual_execution_identity_rejected_across_real_and_synthetic_results():
    message = observation()
    real = copy.deepcopy(load_bundle()["turn"][0]["messages"][1])
    real["message_id"] = uid(321)
    real["blocks"][0]["tool_call_id"] = uid(311)
    real["source"]["tool_execution_id"] = uid(330)
    real["blocks"][0]["tool_execution_id"] = uid(330)
    with pytest.raises(ContractValidationError, match="Duplicate tool execution"):
        validate_message_history([batch("inspect_text", "inspect_text"), message, real])


def test_duplicate_actual_execution_identity_rejected_between_synthetic_results():
    messages = [
        batch("inspect_text", "inspect_text"),
        observation(),
        observation(1, "interrupted"),
    ]
    messages[2]["source"]["tool_execution_id"] = uid(330)
    messages[2]["blocks"][0]["tool_execution_id"] = uid(330)
    with pytest.raises(ContractValidationError, match="Duplicate tool execution"):
        validate_message_history(messages)


def test_v1_error_cannot_use_synthetic_source_or_null_execution_identity():
    legacy = copy.deepcopy(load_bundle()["turn"][0]["messages"][1])
    legacy["source"] = copy.deepcopy(observation()["source"])
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", legacy)
    legacy = copy.deepcopy(load_bundle()["turn"][0]["messages"][1])
    legacy["source"]["tool_execution_id"] = None
    legacy["blocks"][0]["tool_execution_id"] = None
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", legacy)


def test_checkpoint_prefix_validates_actual_pending_order_without_synthetic_results():
    messages = closed_batch()[:2]
    validate_pending_message_history(messages, (uid(311), uid(312)))
    validate_pending_message_history(messages, [uid(311), uid(312)])
    with pytest.raises(ContractValidationError, match="Unsettled"):
        validate_message_history(messages)
    assert len(messages) == 2


def test_pending_final_control_call_needs_no_fake_execution_or_result():
    messages = [batch("final_answer")]
    validate_pending_message_history(messages, [uid(310)])
    assert messages[-1]["blocks"][0]["kind"] == "tool_call"
    with pytest.raises(ContractValidationError, match="Final control"):
        validate_pending_message_history(
            [messages[0], observation(reason="never_started")],
            [uid(310)],
        )


@pytest.mark.parametrize("pending", [
    (),
    [],
    None,
    {uid(311), uid(312)},
    uid(311),
    [None],
    [""],
    [True],
    [uid(311)],
    [uid(312), uid(311)],
    [uid(311), uid(312), uid(313)],
    [uid(311), uid(311)],
    [uid(900), uid(901)],
])
def test_pending_prefix_requires_exact_nonempty_unique_ordered_call_ids(pending):
    with pytest.raises(ContractValidationError):
        validate_pending_message_history(closed_batch()[:2], pending)


def test_pending_prefix_still_rejects_bad_result_order_and_duplicate_execution_identity():
    out_of_order = [closed_batch()[0], closed_batch()[2]]
    with pytest.raises(ContractValidationError, match="order"):
        validate_pending_message_history(out_of_order, [uid(310), uid(312)])
    prefix = [
        batch("inspect_text", "inspect_text", "inspect_text"),
        observation(),
        observation(1),
    ]
    prefix[-1]["source"]["tool_execution_id"] = uid(330)
    prefix[-1]["blocks"][0]["tool_execution_id"] = uid(330)
    with pytest.raises(ContractValidationError, match="Duplicate tool execution"):
        validate_pending_message_history(prefix, [uid(312)])


@pytest.mark.parametrize("kind,messages_key", [
    ("turn", "messages"),
    ("input_snapshot", "s0"),
])
def test_nested_observation_projection_is_checked_not_only_top_level(kind, messages_key):
    bundle = load_bundle()
    record = bundle[kind][0]
    record[messages_key] = [*closed_batch(), *record[messages_key]]
    record[messages_key][1]["blocks"][0]["model_visible_text"] = "{}"
    with pytest.raises(ContractValidationError, match="projection"):
        validate_record(kind, record)


def test_complete_bundle_rejects_execution_reuse_between_new_turns():
    bundle = load_bundle()
    first = bundle["turn"][0]
    first["messages"] = [*closed_batch(), *first["messages"]]

    node_input = copy.deepcopy(bundle["node_input"][0])
    node_input["input_id"] = uid(1001)
    snapshot = copy.deepcopy(bundle["input_snapshot"][0])
    snapshot.update(snapshot_id=uid(1002), input_id=uid(1001))
    run = copy.deepcopy(bundle["run_record"][0])
    run.update(
        run_id=uid(1003),
        input_id=uid(1001),
        snapshot_id=uid(1002),
        source_run_id=None,
        result_turn_id=uid(1004),
    )
    turn = copy.deepcopy(first)
    turn.update(turn_id=uid(1004), run_id=uid(1003), snapshot_id=uid(1002), input_id=uid(1001))
    call_ids = {}
    for offset, message in enumerate(turn["messages"]):
        message["message_id"] = uid(1100 + offset)
        if message["role"] == "assistant":
            message["source"]["request_id"] = uid(1200 + offset)
        for block_index, block in enumerate(message["blocks"]):
            if block["kind"] == "tool_call":
                call_ids[block["tool_call_id"]] = uid(1300 + offset * 10 + block_index)
                block["tool_call_id"] = call_ids[block["tool_call_id"]]
            elif block["kind"] == "tool_result":
                block["tool_call_id"] = call_ids[block["tool_call_id"]]
                if message["schema_version"] == 2:
                    message["source"]["tool_call_id"] = block["tool_call_id"]
                else:
                    execution_id = uid(1400 + offset)
                    block["tool_execution_id"] = execution_id
                    message["source"]["tool_execution_id"] = execution_id
    turn["final"]["message_id"] = next(
        message["message_id"]
        for message in reversed(turn["messages"])
        if message["role"] == "assistant"
    )
    bundle["node_input"].append(node_input)
    bundle["input_snapshot"].append(snapshot)
    bundle["run_record"].append(run)
    bundle["turn"].append(turn)
    bundle["chain_run"][0]["node_run_ids"].append(run["run_id"])
    bundle["candidate_group"][0]["candidate_refs"].append({
        "run_id": run["run_id"], "turn_id": turn["turn_id"],
    })
    with pytest.raises(ContractValidationError, match="New turns cannot reuse execution"):
        validate_bundle(bundle)
