from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.context_contract import project_agent_context_message
from phase1_agent.contract_graph import validate_context_message_history
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.contracts_v2 import validate_record


def paired_history(reason_code, *, final=False, model_visible_text=None):
    call_id = str(uuid4())
    execution_id = None if reason_code == "never_started" else str(uuid4())
    content = {
        "reason_code": reason_code,
        "error": "The tool outcome could not be confirmed.",
        "retry_guidance": "Confirm the outcome before retrying.",
    }
    result = {
        "kind": "tool_result", "tool_call_id": call_id,
        "tool_execution_id": execution_id, "status": "error", "is_error": True,
        "content": content,
        "model_visible_text": (canonical_bytes(content).decode("utf-8")
                               if model_visible_text is None else model_visible_text),
    }
    return [
        {
            "message_id": str(uuid4()), "role": "assistant",
            "blocks": [{
                "kind": "tool_call", "tool_call_id": call_id,
                "tool_name": "final_answer" if final else "lookup",
                "tool_definition_version": "1", "raw_arguments": "{}",
                "parsed_arguments": {},
            }],
        },
        {"message_id": str(uuid4()), "role": "tool", "blocks": [result]},
    ]


@pytest.mark.parametrize("reason_code", ["never_started", "interrupted", "outcome_unknown"])
def test_source_free_runtime_observations_preserve_closed_tool_pairing(reason_code):
    validate_context_message_history(paired_history(reason_code))


@pytest.mark.parametrize("reason_code", ["never_started", "interrupted", "outcome_unknown"])
def test_source_free_runtime_observation_text_must_encode_exact_content(reason_code):
    different = canonical_bytes({
        "reason_code": reason_code, "error": "A fabricated outcome.",
        "retry_guidance": "A fabricated retry rule.",
    }).decode("utf-8")
    messages = paired_history(reason_code, model_visible_text=different)
    with pytest.raises(ContractValidationError, match="projection differs"):
        validate_context_message_history(messages)


def test_source_free_observation_shaped_unknown_reason_cannot_fall_back_to_normal_result():
    messages = paired_history("outcome_unknown_forged")
    with pytest.raises(ContractValidationError, match="exact block contract"):
        validate_context_message_history(messages)


def test_source_free_history_cannot_observe_a_final_answer_call():
    with pytest.raises(ContractValidationError, match="Final control call"):
        validate_context_message_history(paired_history("outcome_unknown", final=True))


def test_source_free_runtime_observation_must_close_the_matching_call():
    messages = paired_history("interrupted")
    messages[1]["blocks"][0]["tool_call_id"] = str(uuid4())
    with pytest.raises(ContractValidationError, match="Tool result has no pending call"):
        validate_context_message_history(messages)


@pytest.mark.parametrize(("content", "status", "is_error"), [
    ({"reason_code": "interrupted"}, "success", False),
    ({"reason_code": "interrupted"}, "error", True),
    ({
        "reason_code": "interrupted",
        "error": "A tool's business-level error.",
        "retry_guidance": "Retry only after checking the operation state.",
    }, "success", False),
])
@pytest.mark.parametrize("final", [False, True])
def test_ordinary_tool_content_with_observation_fields_survives_projection(
    content, status, is_error, final,
):
    messages = paired_history("interrupted", final=final)
    call_id = messages[0]["blocks"][0]["tool_call_id"]
    execution_id = messages[1]["blocks"][0]["tool_execution_id"]
    live_message = validate_record("agent_message", {
        "schema_version": 1,
        "message_id": messages[1]["message_id"],
        "role": "tool",
        "source": {"kind": "tool", "tool_execution_id": execution_id},
        "blocks": [{
            "kind": "tool_result",
            "tool_call_id": call_id,
            "tool_execution_id": execution_id,
            "status": status,
            "is_error": is_error,
            "content": content,
            "model_visible_text": canonical_bytes(content).decode("utf-8"),
        }],
    })
    messages[1] = project_agent_context_message(live_message)

    validate_context_message_history(messages)
    assert messages[1]["blocks"][0]["content"] == content
