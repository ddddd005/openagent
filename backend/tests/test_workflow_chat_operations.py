"""Round rules retain every candidate and never use a page or node as authority."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.chat_contracts import (
    append_chat_candidate, bind_chat_candidate_checkpoint, empty_chat_context,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.workflow_chat_operations import (
    WorkflowChatOperations, begin_chat_round, plan_chat_continuation,
    plan_chat_floor_fork, plan_chat_reroll, select_chat_candidate,
)


def _id():
    return str(uuid4())


def _round(value=None, *, text="submitted", base=None, count=5):
    round_id = _id()
    value = begin_chat_round(
        empty_chat_context() if value is None else value, round_id=round_id,
        base_checkpoint_id=base, submitted_input=text)
    for number in range(count):
        candidate_id, checkpoint_id = _id(), _id()
        value = append_chat_candidate(
            value, round_id=round_id, base_checkpoint_id=base, submitted_input=text,
            candidate_id=candidate_id,
            messages=[{"message_id": _id(), "role": "assistant", "text": "reply-" + str(number)}])
        value = bind_chat_candidate_checkpoint(
            value, round_id=round_id, candidate_id=candidate_id, checkpoint_id=checkpoint_id)
    return value, round_id


def test_reroll_keeps_first_round_null_basis_and_the_same_input_without_mutation():
    value, round_id = _round(base=None)
    before = deepcopy(value)
    assert plan_chat_reroll(value, round_id) == {
        "round_id": round_id, "base_checkpoint_id": None, "submitted_input": "submitted",
    }
    assert value == before and len(value["rounds"]) == 1
    assert len(value["rounds"][0]["candidates"]) == 5


def test_selection_changes_only_current_choice_and_continuation_uses_that_result_point():
    value, round_id = _round(base=_id())
    before = deepcopy(value)
    first = value["rounds"][0]["candidates"][0]
    selected = select_chat_candidate(value, round_id=round_id, candidate_id=first["candidate_id"])
    assert value == before
    assert selected["rounds"][0]["candidates"] == before["rounds"][0]["candidates"]
    assert plan_chat_continuation(selected) == {
        "round_id": round_id, "candidate_id": first["candidate_id"],
        "result_checkpoint_id": first["result_checkpoint_id"],
    }


def test_new_round_uses_selected_point_and_locks_predecessor_for_reroll_and_selection():
    value, first_round = _round()
    first = value["rounds"][0]["candidates"][0]
    value = select_chat_candidate(value, round_id=first_round, candidate_id=first["candidate_id"])
    second_round = _id()
    with pytest.raises(ContractValidationError) as denied:
        begin_chat_round(
            value, round_id=second_round, base_checkpoint_id=_id(), submitted_input="next")
    assert denied.value.reason_code == "chat_round_basis_mismatch"
    next_value = begin_chat_round(
        value, round_id=second_round, base_checkpoint_id=first["result_checkpoint_id"], submitted_input="next")
    assert len(next_value["rounds"]) == 2
    for call in (
        lambda: plan_chat_reroll(next_value, first_round),
        lambda: select_chat_candidate(next_value, round_id=first_round, candidate_id=first["candidate_id"]),
    ):
        with pytest.raises(ContractValidationError) as locked:
            call()
        assert locked.value.reason_code == "chat_round_locked"


def test_historical_floor_fork_keeps_all_five_candidates_and_uses_last_point_for_copying():
    value, first_round = _round()
    first = value["rounds"][0]["candidates"][0]
    value = select_chat_candidate(value, round_id=first_round, candidate_id=first["candidate_id"])
    value, _ = _round(value, text="next", base=first["result_checkpoint_id"], count=1)
    before = deepcopy(value)
    plan = plan_chat_floor_fork(value, first_round)
    assert value == before and len(value["rounds"]) == 2
    assert plan["chat_context"]["rounds"] == [before["rounds"][0]]
    assert len(plan["chat_context"]["rounds"][0]["candidates"]) == 5
    assert plan["selected_checkpoint_id"] == first["result_checkpoint_id"]
    assert plan["copy_through_checkpoint_id"] == before["rounds"][0]["candidates"][-1]["result_checkpoint_id"]
    second = plan["chat_context"]["rounds"][0]["candidates"][1]
    reselected = select_chat_candidate(
        plan["chat_context"], round_id=first_round, candidate_id=second["candidate_id"])
    assert plan_chat_continuation(reselected)["result_checkpoint_id"] == second["result_checkpoint_id"]


def test_pending_candidate_cannot_be_selected_or_forked_but_round_basis_can_be_read():
    round_id, candidate_id = _id(), _id()
    value = append_chat_candidate(
        empty_chat_context(), round_id=round_id, base_checkpoint_id=None, submitted_input="first",
        candidate_id=candidate_id,
        messages=[{"message_id": _id(), "role": "assistant", "text": "pending"}])
    assert plan_chat_reroll(value, round_id)["base_checkpoint_id"] is None
    for call in (
        lambda: select_chat_candidate(value, round_id=round_id, candidate_id=candidate_id),
        lambda: plan_chat_continuation(value),
        lambda: plan_chat_floor_fork(value, round_id),
        lambda: begin_chat_round(value, round_id=_id(), base_checkpoint_id=None, submitted_input="next"),
    ):
        with pytest.raises(ContractValidationError) as denied:
            call()
        assert denied.value.reason_code == "chat_candidate_uncommitted"


@pytest.mark.parametrize("later_success", [False, True])
def test_floor_fork_retains_pending_candidates_and_copies_through_last_success(later_success):
    value, round_id = _round(count=2)
    first = value["rounds"][0]["candidates"][0]
    last_success = value["rounds"][0]["candidates"][-1]["result_checkpoint_id"]
    pending_id = _id()
    value = append_chat_candidate(
        value, round_id=round_id, base_checkpoint_id=None, submitted_input="submitted",
        candidate_id=pending_id,
        messages=[{"message_id": _id(), "role": "assistant", "text": "downstream failed"}])
    if later_success:
        candidate_id, last_success = _id(), _id()
        value = append_chat_candidate(
            value, round_id=round_id, base_checkpoint_id=None, submitted_input="submitted",
            candidate_id=candidate_id,
            messages=[{"message_id": _id(), "role": "assistant", "text": "accepted reroll"}])
        value = bind_chat_candidate_checkpoint(
            value, round_id=round_id, candidate_id=candidate_id, checkpoint_id=last_success)
    value = select_chat_candidate(value, round_id=round_id, candidate_id=first["candidate_id"])
    before = deepcopy(value)
    plan = plan_chat_floor_fork(value, round_id)
    assert value == before and plan["chat_context"] == before
    assert plan["copy_through_checkpoint_id"] == last_success
    assert plan["selected_checkpoint_id"] == first["result_checkpoint_id"]
    pending = next(candidate for candidate in plan["chat_context"]["rounds"][0]["candidates"]
                   if candidate["candidate_id"] == pending_id)
    assert pending["result_checkpoint_id"] is None
    with pytest.raises(ContractValidationError) as denied:
        select_chat_candidate(plan["chat_context"], round_id=round_id, candidate_id=pending_id)
    assert denied.value.reason_code == "chat_candidate_uncommitted"


def test_missing_round_or_foreign_candidate_is_rejected_without_mutating_value():
    value, round_id = _round()
    before = deepcopy(value)
    with pytest.raises(ContractValidationError) as missing:
        plan_chat_reroll(value, _id())
    assert missing.value.reason_code == "chat_round_not_found"
    with pytest.raises(ContractValidationError) as foreign:
        select_chat_candidate(value, round_id=round_id, candidate_id=_id())
    assert foreign.value.reason_code == "chat_candidate_not_found"
    with pytest.raises(ContractValidationError) as duplicate:
        begin_chat_round(value, round_id=round_id, base_checkpoint_id=None, submitted_input="again")
    assert duplicate.value.reason_code == "chat_round_exists"
    assert value == before and plan_chat_continuation(empty_chat_context()) is None


def test_independent_facade_forwards_exact_guards_and_copies_only_input_arguments():
    calls = []

    class Service:
        def __getattr__(self, name):
            def invoke(*args, **parameters):
                calls.append((name, args, deepcopy(parameters)))
                if "inputs" in parameters:
                    parameters["inputs"].clear()
                    parameters["input_overrides"].clear()
                return {"accepted": name}
            return invoke

    operations = WorkflowChatOperations(Service())
    session_id = _id()
    guards = {
        "object_key": "chat-one", "expected_session_revision": 3,
        "expected_data_revision": 0, "expected_head_revision": 2,
        "expected_object_revision": 4, "idempotency_key": "original",
    }
    inputs, overrides = {"text": "external"}, {_id(): "frozen"}
    operations.submit(session_id, submitted_input="user", inputs=inputs, input_overrides=overrides, **guards)
    round_id, candidate_id = _id(), _id()
    operations.reroll(session_id, round_id=round_id, **guards)
    operations.select(session_id, round_id=round_id, candidate_id=candidate_id, **guards)
    operations.fork_floor(session_id, round_id=round_id, **guards)
    assert inputs == {"text": "external"} and len(overrides) == 1
    assert [call[0] for call in calls] == ["chat_submit", "chat_reroll", "chat_select", "chat_fork_floor"]
    assert all(call[1] == (session_id,) and all(
        call[2][field] == expected for field, expected in guards.items()) for call in calls)
    assert calls[0][2]["submitted_input"] == "user"
    assert calls[1][2]["round_id"] == calls[3][2]["round_id"] == round_id
    assert calls[2][2]["candidate_id"] == candidate_id
