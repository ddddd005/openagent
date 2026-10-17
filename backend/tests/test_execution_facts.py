"""Execution facts are immutable causal evidence, not reconstructed Turn history."""

from copy import deepcopy
from pathlib import Path

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.execution_facts import (
    validate_execution_fact, validate_execution_fact_history,
)


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


@pytest.fixture
def example():
    return loads_strict(EXAMPLE.read_text(encoding="utf-8"))


def fact(kind, payload, sequence=1, **changes):
    value = {
        "schema_version": 1, "fact_id": uid(1000 + sequence),
        "run_id": uid(7), "chain_run_id": uid(8), "workflow_session_id": uid(4),
        "node_binding_id": uid(3), "snapshot_id": uid(6), "generation": uid(900),
        "sequence": sequence, "kind": kind,
        "created_at": "2026-09-29T00:00:00.000Z", "payload": deepcopy(payload),
    }
    value.update(changes)
    return value


def request(snapshot, request_id=uid(10), request_index=1, messages=None):
    return {
        "request_id": request_id, "request_index": request_index,
        "messages": deepcopy(snapshot["s0"] if messages is None else messages),
        "tools": [{
            "type": "function", "function": {
                "name": item["name"], "parameters": deepcopy(item["parameters_schema"]),
                **({"description": item["description"]} if "description" in item else {}),
            },
        } for item in snapshot["tool_definitions"]],
        "model_parameters": deepcopy(snapshot["model_parameters"]),
    }


def attempt_start(request_id=uid(10), request_index=1, attempt_index=1, retry_index=0):
    return {
        "request_id": request_id, "attempt_id": uid(2000 + attempt_index),
        "request_index": request_index, "attempt_index": attempt_index, "retry_index": retry_index,
    }


def attempt_finish(request_id=uid(10), attempt_index=1, outcome="responded"):
    return {
        "request_id": request_id, "attempt_id": uid(2000 + attempt_index), "outcome": outcome,
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        "response_id": "provider-response", "model": "offline_fixture",
    }


def successful_facts(example):
    snapshot = example["input_snapshot"][0]
    messages = example["turn"][0]["messages"]
    facts = []
    accepted = []
    for index in range(2):
        assistant, result = messages[index * 2:index * 2 + 2]
        request_id = assistant["source"]["request_id"]
        block = result["blocks"][0]
        entries = [
            ("model_request", request(snapshot, request_id, index + 1, snapshot["s0"] + accepted)),
            ("model_attempt_started", attempt_start(request_id, index + 1, index + 1)),
            ("model_attempt_finished", attempt_finish(request_id, index + 1)),
            ("message_accepted", {"message": assistant}),
            ("tool_dispatch", {
                "tool_call_id": block["tool_call_id"],
                "tool_execution_id": block["tool_execution_id"],
            }),
            ("tool_settled", {
                "tool_call_id": block["tool_call_id"],
                "tool_execution_id": block["tool_execution_id"],
                "outcome": "success", "message": result,
            }),
            ("message_accepted", {"message": result}),
        ]
        for kind, payload in entries:
            facts.append(fact(kind, payload, len(facts) + 1))
        accepted.extend((assistant, result))
    return facts


def observation(call_id, execution_id, reason, message_id):
    content = {
        "reason_code": reason, "error": "Execution result is not available.",
        "retry_guidance": "Assess idempotence and verify external state before retrying.",
    }
    return {
        "schema_version": 2, "message_id": message_id, "role": "tool",
        "source": {
            "kind": "runtime_tool_observation", "tool_call_id": call_id,
            "tool_execution_id": execution_id, "reason_code": reason,
        },
        "blocks": [{
            "kind": "tool_result", "tool_call_id": call_id, "tool_execution_id": execution_id,
            "status": "error", "is_error": True, "content": content,
            "model_visible_text": canonical_bytes(content).decode("utf-8"),
        }],
    }


def unknown_batch_facts(example):
    prefix = successful_facts(example)[:4]
    assistant = prefix[3]["payload"]["message"]
    second_call = deepcopy(assistant["blocks"][0])
    second_call["tool_call_id"] = uid(80)
    assistant["blocks"].append(second_call)
    first_message = observation(uid(16), uid(17), "outcome_unknown", uid(81))
    second_message = observation(uid(80), None, "never_started", uid(82))
    for kind, payload in [
        ("tool_dispatch", {"tool_call_id": uid(16), "tool_execution_id": uid(17)}),
        ("tool_settled", {
            "tool_call_id": uid(16), "tool_execution_id": uid(17),
            "outcome": "outcome_unknown", "message": first_message,
        }),
        ("message_accepted", {"message": first_message}),
        ("tool_settled", {
            "tool_call_id": uid(80), "tool_execution_id": None,
            "outcome": "never_started", "message": second_message,
        }),
        ("message_accepted", {"message": second_message}),
        ("model_request", request(
            example["input_snapshot"][0], uid(90), 2,
            example["input_snapshot"][0]["s0"] + [assistant, first_message, second_message],
        )),
    ]:
        prefix.append(fact(kind, payload, len(prefix) + 1))
    return prefix


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("schema_version", True),
    ("fact_id", "not-a-uuid"), ("run_id", uid(15).upper()), ("generation", 1),
    ("sequence", True), ("sequence", 0), ("sequence", 1.0),
    ("created_at", "2026-02-30T00:00:00.000Z"),
    ("created_at", "2026-09-29T00:00:00Z"), ("kind", "private_checkpoint"),
    ("payload", {"code": "failed", "category": "contract", "model_requests": 0, "attempts": 0,
                 "private_checkpoint": {}}),
])
def test_fact_structural_contract_rejects_invalid_version_identity_and_payload(field, value):
    record = fact("execution_failed", {
        "code": "failed", "category": "contract", "model_requests": 0, "attempts": 0,
    })
    record[field] = value
    with pytest.raises(ContractValidationError):
        validate_execution_fact(record)


@pytest.mark.parametrize("value", [float("inf"), (1,), {1: "invalid"}])
def test_fact_requires_strict_json_without_python_or_nonfinite_values(value):
    record = fact("model_attempt_finished", attempt_finish())
    record["payload"]["usage"] = {"invalid": value}
    with pytest.raises(ContractValidationError):
        validate_execution_fact(record)


def test_accepted_assistant_requires_responded_attempt_and_preserves_raw_arguments(example):
    values = successful_facts(example)
    with pytest.raises(ContractValidationError, match="responded"):
        validate_execution_fact_history([fact("message_accepted", values[3]["payload"])])
    forged = deepcopy(values)
    forged[3]["payload"]["message"]["blocks"][0]["raw_arguments"] = '{"missing": 1}'
    with pytest.raises(ContractValidationError, match="raw_arguments"):
        validate_execution_fact_history(forged, snapshot=example["input_snapshot"][0])


def test_attempt_retry_is_separate_from_request_and_keeps_reported_or_unknown_usage(example):
    snapshot = example["input_snapshot"][0]
    entries = [
        ("model_request", request(snapshot)),
        ("model_attempt_started", attempt_start()),
        ("model_attempt_finished", {**attempt_finish(outcome="model_error"), "usage": None}),
        ("model_attempt_started", attempt_start(attempt_index=2, retry_index=1)),
        ("model_attempt_finished", attempt_finish(attempt_index=2)),
    ]
    values = [fact(kind, payload, index + 1) for index, (kind, payload) in enumerate(entries)]
    assert validate_execution_fact_history(values, snapshot=snapshot) == values
    assert values[2]["payload"]["usage"] is None
    assert values[4]["payload"]["usage"]["prompt_tokens"] == 12
    responded_retry = fact("model_attempt_started", attempt_start(attempt_index=3, retry_index=2), 6)
    with pytest.raises(ContractValidationError, match="Responded"):
        validate_execution_fact_history([*values, responded_retry], snapshot=snapshot)


def test_request_without_attempt_and_new_resume_generation_are_valid_but_never_regress(example):
    snapshot = example["input_snapshot"][0]
    first = fact("model_request", request(snapshot))
    resumed = fact("model_request", request(snapshot, uid(40), 2), 2, generation=uid(901))
    assert validate_execution_fact_history([first, resumed], snapshot=snapshot) == [first, resumed]
    reverted = fact("execution_failed", {
        "code": "model_error", "category": "model", "model_requests": 2, "attempts": 0,
    }, 3, generation=uid(900))
    with pytest.raises(ContractValidationError, match="retired"):
        validate_execution_fact_history([first, resumed, reverted], snapshot=snapshot)


@pytest.mark.parametrize("outcome", ["success", "error", "outcome_unknown", "interrupted", "unknown"])
def test_only_never_started_may_omit_tool_execution_identity(outcome):
    record = fact("tool_settled", {
        "tool_call_id": uid(16), "tool_execution_id": None, "outcome": outcome, "message": None,
    })
    with pytest.raises(ContractValidationError, match="Only never_started"):
        validate_execution_fact(record)


@pytest.mark.parametrize("role,source", [
    ("system", {"kind": "prompt", "prompt_id": uid(700), "revision": 1}),
    ("user", {"kind": "human", "visible_message_id": uid(14)}),
    ("user", {"kind": "upstream_node", "output_id": uid(701)}),
    ("user", {"kind": "prompt", "prompt_id": uid(700), "revision": 1}),
])
def test_message_facts_cannot_claim_frozen_or_invented_external_messages(role, source):
    message = {
        "schema_version": 1, "message_id": uid(702), "role": role, "source": source,
        "blocks": [{"kind": "text", "text": "This is not a kernel increment."}],
    }
    with pytest.raises(ContractValidationError, match="kernel-produced"):
        validate_execution_fact(fact("message_accepted", {"message": message}))


def test_tool_message_acceptance_cannot_reorder_settled_batch(example):
    values = unknown_batch_facts(example)
    wrong = fact("message_accepted", values[8]["payload"], 7)
    with pytest.raises(ContractValidationError, match="call order"):
        validate_execution_fact_history([*values[:6], wrong], snapshot=example["input_snapshot"][0])
