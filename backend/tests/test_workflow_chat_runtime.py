"""Frozen side records use the caller's SQLite transaction and exact owners."""

from copy import deepcopy
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE, empty_chat_context
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.workflow_chat_operations import begin_chat_round
from phase1_agent.workflow_chat_runtime import (
    copy_chat_round_inputs, delete_chat_session_records, initialize_chat_tables,
    read_chat_invocations, read_chat_round_inputs, save_chat_invocations, save_chat_round_inputs,
)


def _id():
    return str(uuid4())


@pytest.fixture
def repo(tmp_path):
    connection = sqlite3.connect(tmp_path / "side-records.sqlite")
    connection.row_factory = sqlite3.Row
    initialize_chat_tables(connection)
    connection.commit()
    connection.execute("BEGIN")
    try:
        yield SimpleNamespace(store=SimpleNamespace(_connection=connection))
    finally:
        connection.rollback()
        connection.close()


def _initial():
    return {
        "agent_contexts": {"context-a": {"value": {
            "messages": [], "layout": [], "accepted_delta_ids": [], "generation": 0,
        }}},
        "variables": {"variable-a": {
            "type_id": "workflow.variable", "schema_version": 1,
            "value": {"registered": True, "name": "count", "value_type": "integer",
                      "assigned": True, "value": 7},
        }},
    }


def _invocation(round_id=None):
    return {
        "round_id": round_id or _id(), "candidate_id": _id(),
        "base_checkpoint_id": None, "submitted_input": "same submitted input",
    }


def _section(value):
    return {"chat-a": {"type_id": CHAT_CONTEXT_TYPE, "schema_version": 1, "value": value}}


def test_round_inputs_and_first_round_initial_values_are_detached_and_immutable(repo):
    sid, round_id, node_id = _id(), _id(), _id()
    initial = _initial()
    inputs, overrides = {"text": {"text": "frozen"}}, {node_id: "submitted"}
    saved = save_chat_round_inputs(
        repo, sid, "chat-a", round_id, inputs=inputs,
        input_overrides=overrides, initial_compute_state=initial)
    inputs["text"]["text"] = "later"
    overrides[node_id] = "later"
    initial["variables"]["variable-a"]["value"]["value"] = 99
    expected = {
        "inputs": {"text": {"text": "frozen"}}, "input_overrides": {node_id: "submitted"},
        "initial_compute_state": _initial(),
    }
    assert saved == read_chat_round_inputs(repo, sid, "chat-a", round_id) == expected
    saved["inputs"].clear()
    assert read_chat_round_inputs(repo, sid, "chat-a", round_id) == expected
    assert save_chat_round_inputs(repo, sid, "chat-a", round_id, **expected) == expected
    with pytest.raises(ContractValidationError) as conflict:
        save_chat_round_inputs(repo, sid, "chat-a", round_id, **{**expected, "inputs": {"text": "other"}})
    assert conflict.value.reason_code == "chat_runtime_conflict"


def test_side_records_isolate_sessions_items_rounds_and_actual_invocations(repo):
    sid, other_sid, chain_id = _id(), _id(), _id()
    invocation = _invocation()
    save_chat_round_inputs(repo, sid, "chat-a", invocation["round_id"], inputs={"one": 1})
    save_chat_round_inputs(repo, sid, "chat-b", invocation["round_id"], inputs={"two": 2})
    save_chat_round_inputs(repo, other_sid, "chat-a", invocation["round_id"], inputs={"three": 3})
    invocations = {"chat-a": invocation, "chat-b": _invocation()}
    assert save_chat_invocations(repo, sid, chain_id, invocations) == invocations
    invocations["chat-a"]["submitted_input"] = "mutated"
    result = read_chat_invocations(repo, sid, chain_id)
    assert result["chat-a"]["submitted_input"] == "same submitted input"
    result.clear()
    assert len(read_chat_invocations(repo, sid, chain_id)) == 2
    assert read_chat_invocations(repo, other_sid, chain_id) == {}
    assert read_chat_round_inputs(repo, sid, "chat-a", _id()) is None
    assert read_chat_round_inputs(repo, sid, "chat-a", invocation["round_id"])["inputs"] == {"one": 1}
    assert read_chat_round_inputs(repo, sid, "chat-b", invocation["round_id"])["inputs"] == {"two": 2}
    assert read_chat_round_inputs(repo, other_sid, "chat-a", invocation["round_id"])["inputs"] == {"three": 3}


def test_invocation_replay_cannot_change_candidate_or_base(repo):
    sid, chain_id = _id(), _id()
    invocation = _invocation()
    assert save_chat_invocations(repo, sid, chain_id, {"chat-a": invocation}) == {"chat-a": invocation}
    assert save_chat_invocations(repo, sid, chain_id, {"chat-a": deepcopy(invocation)}) == {"chat-a": invocation}
    with pytest.raises(ContractValidationError) as conflict:
        save_chat_invocations(repo, sid, chain_id, {
            "chat-a": {**invocation, "base_checkpoint_id": _id()},
        })
    assert conflict.value.reason_code == "chat_runtime_conflict"
    assert read_chat_invocations(repo, sid, chain_id) == {"chat-a": invocation}


def test_copy_keeps_only_retained_round_inputs_and_is_independent_of_source_rows(repo):
    source, target, retained, later = _id(), _id(), _id(), _id()
    value = begin_chat_round(
        empty_chat_context(), round_id=retained, base_checkpoint_id=None, submitted_input="retained")
    expected = save_chat_round_inputs(
        repo, source, "chat-a", retained, inputs={"message": "retained"},
        initial_compute_state=_initial())
    save_chat_round_inputs(repo, source, "chat-a", later, inputs={"message": "later"})
    copy_chat_round_inputs(repo, source, target, _section(value))
    assert read_chat_round_inputs(repo, target, "chat-a", retained) == expected
    assert read_chat_round_inputs(repo, target, "chat-a", later) is None
    delete_chat_session_records(repo, source)
    assert read_chat_round_inputs(repo, target, "chat-a", retained) == expected


def test_missing_submitted_round_cannot_be_silently_copied(repo):
    source, target, round_id = _id(), _id(), _id()
    value = begin_chat_round(
        empty_chat_context(), round_id=round_id, base_checkpoint_id=None, submitted_input="accepted")
    with pytest.raises(ContractValidationError) as missing:
        copy_chat_round_inputs(repo, source, target, _section(value))
    assert missing.value.reason_code == "chat_round_inputs_unavailable"
    assert read_chat_round_inputs(repo, target, "chat-a", round_id) is None
    passive = begin_chat_round(
        empty_chat_context(), round_id=round_id, base_checkpoint_id=None, submitted_input=None)
    copy_chat_round_inputs(repo, source, target, _section(passive))
    assert read_chat_round_inputs(repo, target, "chat-a", round_id) is None


def test_caller_rollback_removes_both_side_records(repo):
    sid, chain_id, round_id = _id(), _id(), _id()
    save_chat_round_inputs(repo, sid, "chat-a", round_id, inputs={"text": "submitted"})
    save_chat_invocations(repo, sid, chain_id, {"chat-a": _invocation(round_id)})
    repo.store._connection.rollback()
    repo.store._connection.execute("BEGIN")
    assert read_chat_round_inputs(repo, sid, "chat-a", round_id) is None
    assert read_chat_invocations(repo, sid, chain_id) == {}


def test_delete_targets_only_one_session_and_keeps_other_session_evidence(repo):
    target, other, round_id, chain_id = _id(), _id(), _id(), _id()
    for sid in (target, other):
        save_chat_round_inputs(repo, sid, "chat-a", round_id)
        save_chat_invocations(repo, sid, chain_id, {"chat-a": _invocation(round_id)})
    delete_chat_session_records(repo, target)
    assert read_chat_round_inputs(repo, target, "chat-a", round_id) is None
    assert read_chat_invocations(repo, target, chain_id) == {}
    assert read_chat_round_inputs(repo, other, "chat-a", round_id) is not None
    assert len(read_chat_invocations(repo, other, chain_id)) == 1


@pytest.mark.parametrize("operation", [
    lambda repo, sid, round_id, chain_id: save_chat_round_inputs(repo, sid, "chat-a", round_id),
    lambda repo, sid, round_id, chain_id: read_chat_round_inputs(repo, sid, "chat-a", round_id),
    lambda repo, sid, round_id, chain_id: save_chat_invocations(repo, sid, chain_id, {}),
    lambda repo, sid, round_id, chain_id: read_chat_invocations(repo, sid, chain_id),
    lambda repo, sid, round_id, chain_id: copy_chat_round_inputs(repo, sid, _id(), {}),
    lambda repo, sid, round_id, chain_id: delete_chat_session_records(repo, sid),
])
def test_all_side_record_access_requires_caller_transaction(repo, operation):
    repo.store._connection.rollback()
    with pytest.raises(ContractValidationError) as outside:
        operation(repo, _id(), _id(), _id())
    assert outside.value.reason_code == "chat_transaction_required"


@pytest.mark.parametrize("arguments", [
    {"inputs": []},
    {"inputs": {"_workflow_frozen_resources": {}}},
    {"input_overrides": {"not-a-node": "text"}},
    {"input_overrides": {_id(): 3}},
    {"initial_compute_state": {"agent_contexts": {}, "variables": {}, "chat_contexts": {}}},
    {"initial_compute_state": {"agent_contexts": {}, "variables": {
        "wrong": {"type_id": "different", "schema_version": 1, "value": {}},
    }}},
])
def test_invalid_input_or_initial_computation_state_writes_nothing(repo, arguments):
    sid, round_id = _id(), _id()
    with pytest.raises(ContractValidationError):
        save_chat_round_inputs(repo, sid, "chat-a", round_id, **arguments)
    assert read_chat_round_inputs(repo, sid, "chat-a", round_id) is None
