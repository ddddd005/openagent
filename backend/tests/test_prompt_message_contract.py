"""I08-A prompt provenance is input evidence, never generated history."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from phase1_agent.adapter import DeepSeekAdapter
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import (
    validate_bundle, validate_message_history, validate_turn_final,
)
from phase1_agent.contract_json import loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.execution_facts import (
    ExecutionFactHistory, validate_execution_fact, validate_execution_fact_history,
)
from phase1_agent.runtime import CanonicalModelAdapter, SnapshotKernel
from phase1_agent.tools import final_answer_tool


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def bundle(name="success"):
    return loads_strict((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def prompt(role="assistant", number=500, group_instance_id=None):
    return {
        "schema_version": 3, "message_id": uid(number), "role": role,
        "source": {
            "kind": "prompt", "prompt_id": uid(400), "revision": 2,
            "item_instance_id": uid(401), "group_instance_id": group_instance_id,
        },
        "blocks": [{"kind": "text", "text": f"{role} configured text"}],
    }


def fact(kind, payload, sequence=1):
    return {
        "schema_version": 1, "fact_id": uid(1000 + sequence),
        "run_id": uid(7), "chain_run_id": uid(8), "workflow_session_id": uid(4),
        "node_binding_id": uid(3), "snapshot_id": uid(6), "generation": uid(900),
        "sequence": sequence, "kind": kind,
        "created_at": "2026-09-30T00:00:00.000Z", "payload": copy.deepcopy(payload),
    }


def request(snapshot):
    return {
        "request_id": uid(600), "request_index": 1,
        "messages": copy.deepcopy(snapshot["s0"]),
        "tools": [{
            "type": "function", "function": {
                "name": item["name"], "parameters": copy.deepcopy(item["parameters_schema"]),
                **({"description": item["description"]} if "description" in item else {}),
            },
        } for item in snapshot["tool_definitions"]],
        "model_parameters": copy.deepcopy(snapshot["model_parameters"]),
    }


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
@pytest.mark.parametrize("group_instance_id", [None, uid(402)])
def test_v3_prompt_source_keeps_definition_and_instance_identities(role, group_instance_id):
    original = prompt(role, group_instance_id=group_instance_id)
    checked = validate_record("agent_message", original)
    assert checked == original
    checked["source"]["revision"] = 99
    checked["blocks"][0]["text"] = "changed"
    assert original["source"]["revision"] == 2
    assert original["blocks"][0]["text"] == f"{role} configured text"


@pytest.mark.parametrize("mutation", [
    lambda message: message.update(role="tool"),
    lambda message: message.update(schema_version=1),
    lambda message: message.update(schema_version=2),
    lambda message: message.update(schema_version=True),
    lambda message: message["source"].pop("item_instance_id"),
    lambda message: message["source"].pop("group_instance_id"),
    lambda message: message["source"].update(revision=0),
    lambda message: message["source"].update(revision=True),
    lambda message: message["source"].update(item_instance_id=uid(0xABCD).upper()),
    lambda message: message["source"].update(group_instance_id="not-a-uuid"),
    lambda message: message["source"].update(group_id=uid(403)),
    lambda message: message["source"].update(kind="model", request_id=uid(404)),
    lambda message: message.update(blocks=[]),
    lambda message: message.update(blocks=[{
        "kind": "tool_call", "tool_call_id": uid(405), "tool_name": "final_answer",
        "tool_definition_version": "1", "raw_arguments": "{}", "parsed_arguments": {},
    }]),
    lambda message: message.update(blocks=[{
        "kind": "tool_result", "status": "success", "is_error": False,
        "content": {}, "tool_call_id": uid(405), "tool_execution_id": uid(406),
        "model_visible_text": "{}",
    }]),
])
def test_v3_prompt_rejects_unknown_versions_sources_identity_and_tool_blocks(mutation):
    message = prompt()
    mutation(message)
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", message)


@pytest.mark.parametrize("role", ["system", "user"])
def test_legacy_prompt_messages_remain_valid_without_instance_fields(role):
    message = {
        "schema_version": 1, "message_id": uid(500), "role": role,
        "source": {"kind": "prompt", "prompt_id": uid(400), "revision": 1},
        "blocks": [{"kind": "text", "text": "legacy configured text"}],
    }
    assert validate_record("agent_message", message) == message
    validate_message_history([message])
    message["role"] = "assistant"
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", message)


@pytest.mark.parametrize("name", ["success", "failed_user", "user_fork"])
def test_older_saved_bundles_are_unchanged(name):
    original = bundle(name)
    assert validate_bundle(original) == original


def test_v2_runtime_execution_observation_retains_its_original_source_contract():
    message = {
        "schema_version": 2, "message_id": uid(500), "role": "system",
        "source": {
            "kind": "runtime_execution_observation", "closeout_id": uid(501),
            "source_run_id": uid(502),
        },
        "blocks": [{"kind": "text", "text": "The previous execution was interrupted."}],
    }
    assert validate_record("agent_message", message) == message
    validate_message_history([message])


def test_three_role_prompts_are_valid_frozen_input_and_model_request_evidence():
    original = bundle()
    snapshot = original["input_snapshot"][0]
    configured = [prompt(role, 500 + index) for index, role in enumerate(
        ("system", "user", "assistant")
    )]
    snapshot["s0"] = configured + snapshot["s0"]
    assert validate_record("input_snapshot", snapshot) == snapshot
    assert validate_bundle(original) == original
    value = fact("model_request", request(snapshot))
    assert validate_execution_fact(value) == value
    assert validate_execution_fact_history([value], snapshot=snapshot) == [value]


def test_same_group_member_can_be_referenced_from_two_distinct_group_instances():
    messages = [
        prompt(number=500, group_instance_id=uid(402)),
        prompt(number=501, group_instance_id=uid(403)),
    ]
    assert messages[0]["source"]["item_instance_id"] == messages[1]["source"]["item_instance_id"]
    validate_message_history(messages)
    messages[1]["message_id"] = messages[0]["message_id"]
    with pytest.raises(ContractValidationError, match="Duplicate message"):
        validate_message_history(messages)


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
def test_v3_prompts_cannot_be_generated_turn_delta_or_final_provenance(role):
    original = bundle()
    turn = original["turn"][0]
    message = prompt(role)
    turn["messages"].append(message)
    with pytest.raises(ContractValidationError):
        validate_record("turn", turn)
    with pytest.raises(ContractValidationError):
        validate_bundle(original)
    if role == "assistant":
        turn["final"] = {"message_id": message["message_id"], "value": {"text": "forged"}}
        message["blocks"][0]["text"] = '{"text":"forged"}'
        with pytest.raises(ContractValidationError):
            validate_turn_final(turn, original["input_snapshot"][0])


@pytest.mark.parametrize("role", ["system", "user"])
def test_legacy_prompt_copies_also_cannot_accumulate_in_generated_turns(role):
    turn = bundle()["turn"][0]
    message = prompt(role)
    message["schema_version"] = 1
    message["source"].pop("item_instance_id")
    message["source"].pop("group_instance_id")
    turn["messages"].append(message)
    with pytest.raises(ContractValidationError):
        validate_record("turn", turn)


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
def test_prompt_message_accepted_facts_fail_explicitly_without_request_id_key_error(role):
    value = fact("message_accepted", {"message": prompt(role)})
    with pytest.raises(ContractValidationError, match="kernel-produced increments"):
        validate_execution_fact(value)
    with pytest.raises(ContractValidationError, match="kernel-produced increments"):
        ExecutionFactHistory().append(value)


def test_prompt_insertion_cannot_split_tool_protocol_groups():
    assistant, tool_result = bundle()["turn"][0]["messages"][:2]
    with pytest.raises(ContractValidationError, match="settle"):
        validate_message_history([assistant, prompt(), tool_result])
    validate_message_history([assistant, tool_result, prompt()])


def test_canonical_adapter_projects_each_prompt_role_without_merging_equal_role_items():
    captured = []

    def transport(http_request):
        captured.append(json.loads(http_request.content))
        return httpx.Response(200, json={
            "id": "completion", "object": "chat.completion", "created": 1,
            "model": "offline-model", "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"text":"ok"}'},
            }],
        })

    messages = [
        prompt("system", 500), prompt("user", 501),
        prompt("assistant", 502), prompt("assistant", 503),
    ]
    messages[2]["blocks"].append({"kind": "text", "text": "second block"})
    original = copy.deepcopy(messages)
    adapter = CanonicalModelAdapter(DeepSeekAdapter(
        "offline-dummy-key", model="offline-model", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    ))
    try:
        result = adapter.generate(messages, [])
    finally:
        adapter.close()
    assert result.content == '{"text":"ok"}'
    assert captured[0]["messages"] == [{
        "role": message["role"],
        "content": "\n".join(block["text"] for block in message["blocks"]),
    } for message in original]
    assert messages == original


def test_kernel_keeps_assistant_prompt_in_s0_but_only_archives_model_generated_delta():
    snapshot = bundle()["input_snapshot"][0]
    configured = prompt()
    snapshot["s0"].insert(0, configured)
    tool = final_answer_tool()
    snapshot["tool_definitions"] = [{
        "name": tool.name, "version": "1",
        "description": tool.definition["function"]["description"],
        "parameters_schema": copy.deepcopy(tool.schema),
    }]
    captured = []
    observed = []

    class FinalAdapter:
        def generate(self, messages, tools):
            captured.append(copy.deepcopy(messages))
            return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                id="provider-final", name="final_answer",
                raw_arguments='{"answer":{"text":"done"}}',
            ),))

    result = SnapshotKernel().run(
        snapshot, [tool], FinalAdapter(), on_fact=observed.append,
    )
    assert captured == [snapshot["s0"]]
    assert [message["source"]["kind"] for message in result.messages] == ["model", "tool"]
    assert configured["message_id"] not in {message["message_id"] for message in result.messages}
    assert result.final["message_id"] == result.messages[0]["message_id"]
    accepted = [entry["payload"]["message"] for entry in observed
                if entry["kind"] == "message_accepted"]
    assert accepted == result.messages
