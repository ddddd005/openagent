"""Focused native update tests; no graph nodes or provider calls are involved."""

from copy import deepcopy

import pytest

from phase1_agent.agent_receipt_contract import AGENT_EXECUTOR_REF, projection_id
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.context_compaction_policy import frame_checkpoint
from phase1_agent.context_update import (
    prepare_context_update_adoption, prove_context_update, replay_context_operations,
    validate_context_update, validate_working_context_view,
)
from phase1_agent.graph_contracts import GraphDiagnosticError


def uid(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def ref(number):
    return {"scope": "artifact", "output_id": uid(number)}


def text(number, content=None, *, role="user", source=None):
    return {"schema_version": 1, "message_id": uid(number), "role": role,
            "source": source or ({"kind": "human", "visible_message_id": uid(number + 100)}
                                 if role == "user" else {"kind": "model", "request_id": uid(number + 100)}),
            "blocks": [{"kind": "text", "text": content or f"message {number}"}]}


def measurement(tokens):
    return {"input_tokens": tokens, "output_reserve_tokens": 10, "total_tokens": tokens + 10,
            "context_window_tokens": 1000, "token_count_kind": "provided"}


def compact(messages, covered, number):
    checkpoint = {"schema_version": 4, "message_id": uid(number + 1), "role": "user",
                  "source": {"kind": "context_checkpoint", "compaction_id": uid(number)},
                  "blocks": [{"kind": "text", "text": frame_checkpoint(f"checkpoint {number}")}]}
    instruction = {"schema_version": 4, "message_id": uid(number + 2), "role": "user",
                   "source": {"kind": "context_compaction_instruction", "compaction_id": uid(number)},
                   "blocks": [{"kind": "text", "text": "Summarize only the selected persistent text."}]}
    ids = [message["message_id"] for message in messages]
    start = ids.index(covered[0])
    result = [*messages[:start], checkpoint,
              *(message for message in messages[start:] if message["message_id"] not in covered)]
    return {"kind": "compact", "compaction_id": uid(number), "before_message_ids": ids,
            "covered_message_ids": covered, "checkpoint": checkpoint,
            "after_message_ids": [message["message_id"] for message in result],
            "request": {"messages": deepcopy(messages) + [instruction], "tools": [], "model_parameters": {}},
            "result": {"text": f"checkpoint {number}", "finish_reason": "stop", "tool_calls": [],
                       "usage": None, "response_id": None, "model": None},
            "capacity": {"before": measurement(200), "after": measurement(50)}}


def replay(initial, operations, **kwargs):
    return replay_context_operations(
        initial, operations, eligible_message_ids=[message["message_id"] for message in initial], **kwargs)


def append(message):
    return {"kind": "append", "message": message}


def test_compaction_then_append_does_not_restore_originals_or_append_a_full_unit():
    initial, new = [text(1), text(2, role="assistant")], text(3, role="assistant")
    operation = compact(initial, [uid(1), uid(2)], 20)
    result = replay(initial, [operation, append(new)], expected_append_messages=[new],
                    expected_compactions=[operation])
    assert result["messages"] == [operation["checkpoint"], new]
    assert result["generation_delta"] == 1
    assert result["appended_message_ids"] == [uid(3)]
    assert initial == [text(1), text(2, role="assistant")]


def test_append_then_compact_then_append_retains_only_post_checkpoint_tail():
    initial = [text(1)]
    first, last = text(2, role="assistant"), text(3, role="assistant")
    operation = compact([*initial, first], [uid(1), uid(2)], 20)
    result = replay(initial, [append(first), operation, append(last)])
    assert result["messages"] == [operation["checkpoint"], last]


def test_two_compactions_are_replayed_in_order_and_old_checkpoint_is_not_a_human_round():
    initial = [text(1)]
    first = compact(initial, [uid(1)], 20)
    middle, last = text(2, role="assistant"), text(3, role="assistant")
    second = compact([first["checkpoint"], middle], [uid(21), uid(2)], 30)
    result = replay(initial, [first, append(middle), second, append(last)])
    assert result["messages"] == [second["checkpoint"], last]
    assert result["compaction_ids"] == [uid(20), uid(30)]
    assert result["generation_delta"] == 2
    assert result["messages"][0]["source"]["kind"] == "context_checkpoint"


@pytest.mark.parametrize("change,reason", [
    (lambda operation: operation.update(before_message_ids=[uid(2), uid(1)]),
     "context_update_order_mismatch"),
    (lambda operation: operation.update(covered_message_ids=[uid(2), uid(1)]),
     "context_update_ineligible"),
    (lambda operation: operation.update(after_message_ids=[uid(21), uid(2)]),
     "context_update_order_mismatch"),
    (lambda operation: operation["request"]["messages"][0]["blocks"][0].update(text="forged original"),
     "context_update_invalid_proof"),
    (lambda operation: operation["result"].update(tool_calls=[{"name": "unexpected"}]),
     "context_update_invalid_summary"),
    (lambda operation: operation["capacity"]["after"].update(total_tokens=1),
     "context_update_invalid_capacity"),
])
def test_changed_order_or_maintenance_proof_is_rejected(change, reason):
    initial = [text(1), text(2, role="assistant")]
    operation = compact(initial, [uid(1), uid(2)], 20)
    change(operation)
    with pytest.raises(GraphDiagnosticError) as rejected:
        replay(initial, [operation])
    assert rejected.value.reason_code == reason


def test_duplicate_message_cannot_be_restored_after_it_was_compacted():
    initial = [text(1)]
    operation = compact(initial, [uid(1)], 20)
    with pytest.raises(GraphDiagnosticError) as rejected:
        replay(initial, [operation, append(initial[0])])
    assert rejected.value.reason_code == "context_update_duplicate_identity"


def test_duplicate_compaction_identity_is_rejected_even_if_new_view_is_forged():
    initial = [text(1)]
    first = compact(initial, [uid(1)], 20)
    second = compact([first["checkpoint"]], [uid(21)], 20)
    with pytest.raises(GraphDiagnosticError) as rejected:
        replay(initial, [first, second])
    assert rejected.value.reason_code == "context_update_duplicate_identity"


def test_a_new_update_cannot_reuse_the_previous_checkpoint_compaction_identity():
    original = compact([text(1)], [uid(1)], 20)["checkpoint"]
    reused = compact([original], [original["message_id"]], 20)
    reused["checkpoint"]["message_id"] = uid(24)
    reused["after_message_ids"] = [uid(24)]
    with pytest.raises(GraphDiagnosticError) as rejected:
        replay([original], [reused])
    assert rejected.value.reason_code == "context_update_duplicate_identity"


def tool_pair():
    call = {"schema_version": 1, "message_id": uid(50), "role": "assistant",
            "source": {"kind": "model", "request_id": uid(51)},
            "blocks": [{"kind": "text", "text": "Checking."}, {
                "kind": "tool_call", "tool_call_id": uid(52), "tool_name": "inspect_text",
                "tool_definition_version": "1", "raw_arguments": "{}", "parsed_arguments": {}}]}
    result = {"schema_version": 1, "message_id": uid(53), "role": "tool",
              "source": {"kind": "tool", "tool_execution_id": uid(54)},
              "blocks": [{"kind": "tool_result", "tool_call_id": uid(52), "tool_execution_id": uid(54),
                          "status": "success", "is_error": False, "content": "ok",
                          "model_visible_text": "ok"}]}
    return call, result


def test_compaction_waits_for_all_tools_to_close_and_keeps_their_protocol_unchanged():
    initial = [text(1)]
    call, tool_result = tool_pair()
    operation = compact([*initial, call], [uid(1)], 20)
    with pytest.raises(GraphDiagnosticError) as rejected:
        replay(initial, [append(call), operation])
    assert rejected.value.reason_code == "context_update_tool_unclosed"
    operation = compact([*initial, call, tool_result], [uid(1)], 20)
    result = replay(initial, [append(call), append(tool_result), operation])
    assert result["messages"] == [operation["checkpoint"], call, tool_result]


def test_tool_and_assistant_tool_call_are_not_eligible_even_with_forged_whitelist():
    call, result = tool_pair()
    initial = [text(1), call, result]
    with pytest.raises(GraphDiagnosticError) as rejected:
        replay(initial, [compact(initial, [uid(1)], 20)])
    assert rejected.value.reason_code == "context_update_ineligible"


def test_fixed_upstream_material_is_not_compactable_even_with_an_incorrect_whitelist():
    initial = [text(1, source={"kind": "upstream_node", "output_id": uid(200)})]
    with pytest.raises(GraphDiagnosticError) as rejected:
        replay(initial, [compact(initial, [uid(1)], 20)])
    assert rejected.value.reason_code == "context_update_ineligible"


def working_view(messages):
    return validate_working_context_view({
        "schema_version": 1, "kind": "workflow.context-working-view",
        "owner": {"workflow_session_id": uid(101), "object_key": "context", "agent_node_id": uid(102)},
        "basis": {"revision_id": uid(103), "head_revision": 7}, "messages": messages,
        "accepted_delta_ids": [uid(104)], "applied_delta_ids": [], "once_injected_item_ids": [uid(105)],
        "generation": 3})


def packet_fixture():
    initial = working_view([text(1)])
    new = text(2, role="assistant")
    operation = compact(initial["messages"], [uid(1)], 20)
    owner = {"workflow_session_id": uid(101), "node_binding_id": uid(102),
             "chain_run_id": uid(110), "node_run_id": uid(111)}
    update_id = projection_id(owner["node_run_id"], "context-update")
    next_view = {**deepcopy(initial), "messages": [operation["checkpoint"], new],
                 "applied_delta_ids": [update_id], "generation": 4}
    receipts = {"schema_version": 1, "kind": "workflow.agent-receipts", "owner": owner,
                "executor_ref": AGENT_EXECUTOR_REF.to_dict(), "snapshot_id": uid(112),
                "frozen_prompt_ref": ref(113), "model_ref": ref(114), "unit_id": uid(115),
                "fact_ids": [uid(116)]}
    packet = validate_context_update({
        "schema_version": 1, "kind": "workflow.agent-context-update", "owner": owner,
        "update_id": update_id, "basis_view_ref": ref(117), "basis_revision": initial["basis"],
        "compaction_policy_ref": None,
        "operations": [operation, append(new)], "next_view": next_view, "receipts": receipts})
    return initial, packet, new, operation


def prove(initial, packet, new, operation):
    return prove_context_update(
        initial, ref(117), packet, eligible_message_ids=[uid(1)],
        accepted_messages=[new], accepted_compactions=[operation], expected_tools=[], expected_model_parameters={})


def test_candidate_proof_preserves_consumed_ids_and_once_ledger_after_text_is_gone():
    initial, packet, new, operation = packet_fixture()
    assert prove(initial, packet, new, operation) == packet["next_view"]
    assert packet["next_view"]["once_injected_item_ids"] == initial["once_injected_item_ids"]
    assert packet["next_view"]["accepted_delta_ids"] == initial["accepted_delta_ids"]


@pytest.mark.parametrize("field", ["once_injected_item_ids", "accepted_delta_ids"])
def test_candidate_cannot_erase_consumption_or_once_injection_when_compacting(field):
    initial, packet, new, operation = packet_fixture()
    packet["next_view"][field] = []
    with pytest.raises(GraphDiagnosticError) as rejected:
        prove(initial, packet, new, operation)
    assert rejected.value.reason_code == "context_update_candidate_mismatch"


def test_candidate_rejects_wrong_agent_basis_unproven_append_and_unproven_compaction():
    initial, packet, new, operation = packet_fixture()
    forged = deepcopy(packet)
    forged["basis_view_ref"] = ref(118)
    with pytest.raises(GraphDiagnosticError) as rejected:
        prove(initial, forged, new, operation)
    assert rejected.value.reason_code == "context_update_basis_mismatch"
    with pytest.raises(GraphDiagnosticError) as rejected:
        prove(initial, packet, [], operation)
    assert rejected.value.reason_code == "context_update_append_unproven"
    with pytest.raises(GraphDiagnosticError) as rejected:
        prove(initial, packet, new, {})
    assert rejected.value.reason_code == "context_update_compaction_unproven"
    forged = deepcopy(packet)
    forged["next_view"]["owner"]["agent_node_id"] = uid(119)
    with pytest.raises(GraphDiagnosticError) as rejected:
        prove(initial, forged, new, operation)
    assert rejected.value.reason_code == "context_update_owner_mismatch"


def object_record(view):
    return {"type_id": EFFECTIVE_CONTEXT_TYPE, "schema_version": 4, "deleted": False,
            "binding": {"object_key": "context"}, "revision": 7, "revision_id": uid(103),
            "value": {"view_ref": ref(120), "accepted_delta_ids": deepcopy(view["accepted_delta_ids"])}}


def adoption(view, record):
    return prepare_context_update_adoption(
        workflow_session_id=uid(101), object_key="context", agent_node_id=uid(102),
        object_record=record, view_ref=ref(121), view=view)


def test_pure_adoption_is_cas_fenced_and_duplicate_submission_reuses_original_candidate():
    _, packet, _, _ = packet_fixture()
    view = packet["next_view"]
    record = object_record(view)
    prepared = adoption(view, record)
    assert prepared["write_intent"]["expected_revision"] == 7
    assert prepared["desired_value"]["accepted_delta_ids"] == view["accepted_delta_ids"] + view["applied_delta_ids"]
    assert record["value"] != prepared["desired_value"]
    committed = {**deepcopy(record), "revision": 8, "revision_id": uid(122),
                 "value": prepared["desired_value"]}
    repeated = adoption(view, committed)
    assert repeated["already_committed"] and repeated["write_intent"] is None
    assert repeated["operation_key"] == prepared["operation_key"]
    changed = {**deepcopy(record), "revision_id": uid(122)}
    with pytest.raises(GraphDiagnosticError) as rejected:
        adoption(view, changed)
    assert rejected.value.reason_code == "context_stale_basis"


def test_append_only_candidate_and_empty_maintenance_keep_same_generation():
    initial = working_view([text(1)])
    new = text(2, role="assistant")
    result = replay(initial["messages"], [append(new)], expected_compactions=[])
    assert result["messages"] == [*initial["messages"], new]
    assert result["generation_delta"] == 0


def test_native_kernel_two_compactions_replay_and_candidate_proof_do_not_reappend_raw_history():
    from phase1_agent.context_compaction_policy import default_compaction_policy
    from phase1_agent.contracts import ModelResponse
    from phase1_agent.runtime import SnapshotKernel
    from test_runtime import ScriptedAdapter, final, setup

    snapshot, tools = setup()
    snapshot["model_parameters"]["max_tokens"] = 16
    history = text(300, "HISTORY_MARKER " * 30)
    fixed = {"schema_version": 1, "message_id": uid(301), "role": "system",
             "source": {"kind": "prompt", "prompt_id": uid(302), "revision": 1},
             "blocks": [{"kind": "text", "text": "Stable system rules."}]}
    snapshot["s0"] = [fixed, history, deepcopy(snapshot["s0"][-1])]
    policy = {**default_compaction_policy(), "trigger_tokens": 100, "keep_depth": 0}
    snapshot["config"]["payload"]["context_compaction"] = {
        "policy": policy, "context_window_tokens": 1000, "output_reserve_tokens": 20,
        "layout": [{"kind": "fixed", "round_id": None},
                   {"kind": "history", "round_id": "old-round"},
                   {"kind": "current", "round_id": None}],
    }

    def count_tokens(messages, definitions):
        return 200 if any(
            "HISTORY_MARKER" in block.get("text", "") or "GENERATED_MARKER" in block.get("text", "")
            for message in messages for block in message["blocks"]) else 50

    adapter = ScriptedAdapter(
        ModelResponse("stop", content="first compact checkpoint"),
        ModelResponse("stop", content="GENERATED_MARKER " * 30),
        ModelResponse("stop", content="second compact checkpoint"),
        final({"text": "continued and complete"}),
    )
    facts = []
    result = SnapshotKernel().run(
        snapshot, tools, adapter, token_counter=count_tokens, on_fact=facts.append)
    accepted = [fact["payload"]["message"] for fact in facts if fact["kind"] == "message_accepted"]
    compactions = [fact["payload"] for fact in facts if fact["kind"] == "context_compaction_applied"]
    assert accepted == result.messages
    assert len(compactions) == 2
    assert [operation["kind"] for operation in result.context_operations] == [
        "compact", "append", "append", "compact", "append", "append"]
    assert any(message["message_id"] in compactions[1]["covered_message_ids"]
               for message in result.messages)
    replayed = replay_context_operations(
        snapshot["s0"], result.context_operations, eligible_message_ids=[history["message_id"]],
        expected_append_messages=accepted, expected_compactions=compactions,
        expected_tools=[tool.definition for tool in tools], expected_model_parameters=snapshot["model_parameters"])
    assert replayed["messages"] == result.effective_messages
    assert replayed["generation_delta"] == 2
    assert sum(operation["kind"] == "append" for operation in result.context_operations) == len(result.messages)
    assert "GENERATED_MARKER" not in str(replayed["messages"])
    assert "HISTORY_MARKER" not in str(replayed["messages"])

    initial = working_view(snapshot["s0"])
    initial["owner"] = {"workflow_session_id": snapshot["workflow_session_id"], "object_key": "context",
                        "agent_node_id": snapshot["node_binding_id"]}
    owner = {"workflow_session_id": snapshot["workflow_session_id"],
             "node_binding_id": snapshot["node_binding_id"], "chain_run_id": uid(310), "node_run_id": uid(311)}
    update_id = projection_id(owner["node_run_id"], "context-update")
    candidate = {**deepcopy(initial), "messages": replayed["messages"], "applied_delta_ids": [update_id],
                 "generation": initial["generation"] + replayed["generation_delta"]}
    receipts = {"schema_version": 1, "kind": "workflow.agent-receipts", "owner": owner,
                "executor_ref": AGENT_EXECUTOR_REF.to_dict(), "snapshot_id": snapshot["snapshot_id"],
                "frozen_prompt_ref": ref(312), "model_ref": ref(313), "unit_id": uid(314),
                "fact_ids": [uid(315 + index) for index in range(len(facts))]}
    packet = {"schema_version": 1, "kind": "workflow.agent-context-update", "owner": owner,
              "update_id": update_id, "basis_view_ref": ref(316), "basis_revision": initial["basis"],
              "compaction_policy_ref": None,
              "operations": result.context_operations, "next_view": candidate, "receipts": receipts}
    assert prove_context_update(
        initial, ref(316), packet, eligible_message_ids=[history["message_id"]],
        accepted_messages=accepted, accepted_compactions=compactions,
        expected_tools=[tool.definition for tool in tools],
        expected_model_parameters=snapshot["model_parameters"]) == candidate
