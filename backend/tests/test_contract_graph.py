"""Relational contract fixtures, not evidence of persistence or execution."""

import copy
from pathlib import Path

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle, validate_fork_request, validate_message_history
from phase1_agent.contract_json import dumps_pretty, loads_strict


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def load(name="success"):
    return loads_strict((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["success", "failed_user", "user_fork"])
def test_saved_examples_are_valid_and_roundtrip(name):
    original = load(name)
    checked = validate_bundle(original)
    assert checked == original
    assert checked is not original
    assert validate_bundle(loads_strict(dumps_pretty(checked))) == original


def test_failed_model_preserves_ui_user_without_success_history():
    bundle = validate_bundle(load("failed_user"))
    assert bundle["turn"] == []
    assert bundle["run_record"][0]["status"] == "failed"
    assert bundle["visible_message"][0]["role"] == "user"


def test_user_fork_keeps_identity_with_independent_pending_input():
    bundle = validate_bundle(load("user_fork"))
    original, _, fork = bundle["visible_message_ref"]
    assert original["visible_message_id"] == fork["visible_message_id"]
    assert original["workflow_session_id"] != fork["workflow_session_id"]
    assert original["boundary"]["input_id"] != fork["boundary"]["input_id"]
    assert fork["boundary"]["chain_run_id"] is None


@pytest.mark.parametrize("mutation", [
    lambda b: b["node_definition"].append(copy.deepcopy(b["node_definition"][0])),
    lambda b: b["run_record"][0].update(snapshot_id=uid(900)),
    lambda b: b["run_record"][0].update(input_id=uid(900)),
    lambda b: b["turn"][0].update(parent_turn_id=uid(9)),
    lambda b: b["turn"][0]["final"].update(value={"text": "forged"}),
    lambda b: b["turn"][0]["final"].update(message_id=uid(22)),
    lambda b: b["candidate_selection"][0].update(selected_turn_id=uid(900)),
    lambda b: b["visible_message_ref"][0]["boundary"].update(before_checkpoint_id=uid(19)),
    lambda b: b["visible_message_ref"][1]["boundary"].update(after_checkpoint_id=uid(12)),
    lambda b: b["visible_message"][1]["payload"].update(text="forged"),
    lambda b: b["workflow_checkpoint"][1]["nodes"][0]["private_data"].update(owner_component_id=uid(900)),
    lambda b: b["chain_run"][0].update(node_run_ids=[]),
    lambda b: b["run_record"][0].update(status="failed"),
    lambda b: b["visible_message_ref"][1].update(sequence=1),
])
def test_bad_references_and_business_facts_are_rejected(mutation):
    bundle = load()
    mutation(bundle)
    with pytest.raises(ContractValidationError):
        validate_bundle(bundle)


@pytest.mark.parametrize("mutation", [
    lambda b: b["visible_message_ref"][2]["boundary"].update(chain_run_id=uid(8)),
    lambda b: b["fork_anchor"][0]["pending_input"].update(input_id=uid(5)),
    lambda b: b["workflow_session"][1]["source"].update(source_workflow_session_id=uid(30)),
    lambda b: b["fork_anchor"][0].update(checkpoint_id=uid(19)),
])
def test_bad_fork_or_unfinished_source_rejected(mutation):
    bundle = load("user_fork")
    mutation(bundle)
    with pytest.raises(ContractValidationError):
        validate_bundle(bundle)


def test_historical_fork_survives_unknown_delivery_but_new_fork_is_blocked():
    bundle = load("user_fork")
    anchor = bundle["fork_anchor"][0]
    validate_fork_request(bundle, anchor)
    bundle["output_delivery"][0]["status"] = "unknown"
    validate_bundle(bundle)
    with pytest.raises(ContractValidationError, match="Unsettled delivery"):
        validate_fork_request(bundle, anchor)


def test_failed_source_blocks_fork_even_at_old_user_anchor():
    bundle = load("failed_user")
    anchor = load("user_fork")["fork_anchor"][0]
    with pytest.raises(ContractValidationError, match="Unfinished"):
        validate_fork_request(bundle, anchor)


def test_closed_tool_history_rejects_missing_duplicate_and_out_of_order_results():
    messages = load()["turn"][0]["messages"]
    validate_message_history(messages)
    invalid = [messages[:1], [messages[1]], [messages[0], messages[1], messages[1]]]
    for candidate in invalid:
        with pytest.raises(ContractValidationError):
            validate_message_history(candidate)
    batch = copy.deepcopy(messages[:2])
    extra = copy.deepcopy(batch[0]["blocks"][0])
    extra["tool_call_id"] = uid(90)
    batch[0]["blocks"].append(extra)
    batch[1]["blocks"][0]["tool_call_id"] = uid(90)
    with pytest.raises(ContractValidationError, match="order"):
        validate_message_history(batch)


def test_tool_error_is_valid_but_unknown_outcome_is_not_model_history():
    messages = load()["turn"][0]["messages"]
    result = messages[1]["blocks"][0]
    result.update(status="error", is_error=True, content={"reason": "tool failure"})
    validate_message_history(messages)
    result["status"] = "unknown"
    with pytest.raises(ContractValidationError):
        validate_message_history(messages)


def test_unselected_sibling_candidate_can_be_archived_without_reparenting():
    bundle = load()
    original_input = bundle["node_input"][0]
    new_input = {**copy.deepcopy(original_input), "input_id": uid(101)}
    snapshot = {**copy.deepcopy(bundle["input_snapshot"][0]), "snapshot_id": uid(102), "input_id": uid(101)}
    run = {**copy.deepcopy(bundle["run_record"][0]), "run_id": uid(103), "input_id": uid(101),
           "snapshot_id": uid(102), "source_run_id": uid(7), "result_turn_id": uid(104)}
    turn = {**copy.deepcopy(bundle["turn"][0]), "turn_id": uid(104), "run_id": uid(103),
            "input_id": uid(101), "snapshot_id": uid(102)}
    for offset, message in enumerate(turn["messages"]):
        message["message_id"] = uid(110 + offset)
        if message["role"] == "assistant":
            message["source"]["request_id"] = uid(120 + offset)
            message["blocks"][0]["tool_call_id"] = uid(130 + offset)
        else:
            message["source"]["tool_execution_id"] = uid(140 + offset)
            message["blocks"][0]["tool_execution_id"] = uid(140 + offset)
            message["blocks"][0]["tool_call_id"] = uid(130 + offset - 1)
    turn["final"]["message_id"] = uid(112)
    bundle["node_input"].append(new_input)
    bundle["input_snapshot"].append(snapshot)
    bundle["run_record"].append(run)
    bundle["turn"].append(turn)
    bundle["chain_run"][0]["node_run_ids"].append(uid(103))
    bundle["candidate_group"][0]["candidate_refs"].append({"run_id": uid(103), "turn_id": uid(104)})
    checked = validate_bundle(bundle)
    assert checked["candidate_selection"][0]["selected_turn_id"] == uid(9)
    assert len(checked["turn"]) == 2


def test_connection_requires_exact_versioned_schema():
    bundle = load()
    second = copy.deepcopy(bundle["node_binding"][0])
    second["node_binding_id"] = uid(200)
    bundle["node_binding"].append(second)
    definition = bundle["workflow_definition_revision"][0]
    definition["bindings"].append(uid(200))
    definition["edges"].append({"from_node_binding_id": uid(3), "from_port_id": "reply",
                                "to_node_binding_id": uid(200), "to_port_id": "request"})
    validate_bundle(bundle)
    bundle["node_definition"][0]["ports"][0]["schema_ref"]["version"] = 2
    with pytest.raises(ContractValidationError, match="schemas"):
        validate_bundle(bundle)


def test_assistant_fork_preserves_historical_execution_identity():
    bundle = load("user_fork")
    bundle["workflow_session"][1]["source"]["visible_message_id"] = uid(20)
    bundle["visible_message_ref"] = bundle["visible_message_ref"][:2]
    for original in list(bundle["visible_message_ref"]):
        shared = copy.deepcopy(original)
        shared["workflow_session_id"] = uid(30)
        bundle["visible_message_ref"].append(shared)
    bundle["node_input"] = bundle["node_input"][:1]
    bundle["fork_anchor"] = [{
        "schema_version": 1, "fork_anchor_id": uid(32), "source_workflow_session_id": uid(4),
        "visible_message_id": uid(20), "role": "assistant", "checkpoint_id": uid(19),
        "chain_run_id": uid(8), "output_id": uid(13),
    }]
    validate_bundle(bundle)
    validate_fork_request(bundle, bundle["fork_anchor"][0])
    assert bundle["visible_message_ref"][2]["boundary"]["chain_run_id"] == uid(8)
    assert len(bundle["chain_run"]) == 1


def test_user_fork_cannot_inherit_reply_after_anchor():
    bundle = load("user_fork")
    reply = copy.deepcopy(bundle["visible_message_ref"][1])
    reply["workflow_session_id"] = uid(30)
    bundle["visible_message_ref"].append(reply)
    with pytest.raises(ContractValidationError, match="beyond fork"):
        validate_bundle(bundle)


def test_successful_final_requires_successful_control_result():
    bundle = load()
    result = bundle["turn"][0]["messages"][-1]["blocks"][0]
    result.update(status="error", is_error=True)
    with pytest.raises(ContractValidationError, match="Final control"):
        validate_bundle(bundle)


def test_reroll_must_reuse_the_complete_frozen_s0():
    bundle = load()
    base = bundle["input_snapshot"][0]
    changed = copy.deepcopy(base)
    changed["snapshot_id"] = uid(301)
    changed["input_id"] = uid(302)
    extra_message = copy.deepcopy(changed["s0"][0])
    extra_message["message_id"] = uid(305)
    changed["s0"].append(extra_message)
    new_input = copy.deepcopy(bundle["node_input"][0])
    new_input["input_id"] = uid(302)
    run = copy.deepcopy(bundle["run_record"][0])
    run.update(run_id=uid(303), input_id=uid(302), snapshot_id=uid(301), source_run_id=uid(7),
               result_turn_id=uid(304))
    turn = copy.deepcopy(bundle["turn"][0])
    turn.update(turn_id=uid(304), run_id=uid(303), input_id=uid(302), snapshot_id=uid(301))
    for offset, message in enumerate(turn["messages"]):
        message["message_id"] = uid(310 + offset)
    turn["final"]["message_id"] = uid(312)
    bundle["input_snapshot"].append(changed)
    bundle["node_input"].append(new_input)
    bundle["run_record"].append(run)
    bundle["turn"].append(turn)
    bundle["chain_run"][0]["node_run_ids"].append(uid(303))
    bundle["candidate_group"][0]["candidate_refs"].append({"run_id": uid(303), "turn_id": uid(304)})
    with pytest.raises(ContractValidationError, match="frozen s0"):
        validate_bundle(bundle)


def test_candidate_members_must_share_the_group_frozen_basis():
    bundle = load()
    base = bundle["input_snapshot"][0]
    changed = copy.deepcopy(base)
    changed["snapshot_id"] = uid(321)
    changed["input_id"] = uid(322)
    changed["model_parameters"]["temperature"] = 0.7
    new_input = copy.deepcopy(bundle["node_input"][0])
    new_input["input_id"] = uid(322)
    run = copy.deepcopy(bundle["run_record"][0])
    run.update(run_id=uid(323), input_id=uid(322), snapshot_id=uid(321), source_run_id=None,
               result_turn_id=uid(324))
    turn = copy.deepcopy(bundle["turn"][0])
    turn.update(turn_id=uid(324), run_id=uid(323), input_id=uid(322), snapshot_id=uid(321))
    for offset, message in enumerate(turn["messages"]):
        message["message_id"] = uid(330 + offset)
        if message["role"] == "assistant":
            message["source"]["request_id"] = uid(340 + offset)
            message["blocks"][0]["tool_call_id"] = uid(350 + offset)
        else:
            message["source"]["tool_execution_id"] = uid(360 + offset)
            message["blocks"][0]["tool_execution_id"] = uid(360 + offset)
            message["blocks"][0]["tool_call_id"] = uid(350 + offset - 1)
    turn["final"]["message_id"] = uid(332)
    bundle["input_snapshot"].append(changed)
    bundle["node_input"].append(new_input)
    bundle["run_record"].append(run)
    bundle["turn"].append(turn)
    bundle["chain_run"][0]["node_run_ids"].append(uid(323))
    bundle["candidate_group"][0]["candidate_refs"].append({"run_id": uid(323), "turn_id": uid(324)})
    with pytest.raises(ContractValidationError, match="frozen basis"):
        validate_bundle(bundle)


def test_completed_checkpoint_selection_refs_must_match_node_heads():
    bundle = load()
    bundle["workflow_checkpoint"][1]["selection_refs"][0]["selected_turn_id"] = None
    with pytest.raises(ContractValidationError, match="disagrees"):
        validate_bundle(bundle)


def test_direct_upstream_input_cannot_rewrite_the_cited_output():
    bundle = load()
    upstream_input = copy.deepcopy(bundle["node_input"][0])
    upstream_input["input_id"] = uid(345)
    upstream_input["source"] = {"kind": "upstream_output", "output_id": uid(13)}
    bundle["node_input"].append(upstream_input)
    with pytest.raises(ContractValidationError, match="upstream input"):
        validate_bundle(bundle)


def test_one_output_cannot_publish_two_assistant_messages_in_one_session():
    bundle = load()
    duplicate = copy.deepcopy(bundle["visible_message"][1])
    duplicate["visible_message_id"] = uid(341)
    bundle["visible_message"].append(duplicate)
    with pytest.raises(ContractValidationError, match="two assistant"):
        validate_bundle(bundle)


def test_agent_final_answer_mode_rejects_text_only_final():
    bundle = load()
    final_message = bundle["turn"][0]["messages"][2]
    final_message["blocks"] = [{"kind": "text", "text": '{"text":"A saved example reply."}'}]
    bundle["turn"][0]["messages"].pop()
    bundle["turn"][0]["final"] = {"message_id": final_message["message_id"], "value": {"text": "A saved example reply."}}
    with pytest.raises(ContractValidationError, match="final_answer mode"):
        validate_bundle(bundle)


def test_fork_seed_and_child_pending_input_must_match():
    bundle = load("user_fork")
    child_input = copy.deepcopy(bundle["node_input"][0])
    child_input["input_id"] = uid(350)
    bundle["node_input"].append(child_input)
    bundle["visible_message_ref"][2]["boundary"]["input_id"] = uid(350)
    with pytest.raises(ContractValidationError, match="seed and child"):
        validate_bundle(bundle)


def test_two_branches_at_one_user_get_independent_seeds_and_inputs():
    bundle = load("user_fork")
    child = copy.deepcopy(bundle["workflow_session"][1])
    child["workflow_session_id"] = uid(400)
    child["source"]["fork_anchor_id"] = uid(401)
    anchor = copy.deepcopy(bundle["fork_anchor"][0])
    anchor["fork_anchor_id"] = uid(401)
    anchor["pending_input"]["input_id"] = uid(402)
    pending = copy.deepcopy(bundle["node_input"][1])
    pending["input_id"] = uid(402)
    ref = copy.deepcopy(bundle["visible_message_ref"][2])
    ref["workflow_session_id"] = uid(400)
    ref["boundary"]["input_id"] = uid(402)
    bundle["workflow_session"].append(child)
    bundle["fork_anchor"].append(anchor)
    bundle["node_input"].append(pending)
    bundle["visible_message_ref"].append(ref)
    validate_bundle(bundle)


def test_fork_seed_stays_valid_after_child_input_starts():
    bundle = load("user_fork")
    bundle["visible_message_ref"][2]["boundary"].update(input_status="running", chain_run_id=uid(410))
    chain = copy.deepcopy(bundle["chain_run"][0])
    chain.update(chain_run_id=uid(410), workflow_session_id=uid(30), input_id=uid(31),
                 status="running", node_run_ids=[], output_id=None)
    bundle["chain_run"].append(chain)
    validate_bundle(bundle)


@pytest.mark.parametrize("schema", [{"$ref": "#/$defs/absent"}, {"$ref": "#"}])
def test_unresolvable_or_cyclic_local_output_schema_raises_contract_error(schema):
    bundle = load()
    bundle["input_snapshot"][0]["output_schema"] = schema
    with pytest.raises(ContractValidationError, match="frozen output schema"):
        validate_bundle(bundle)


def test_final_control_can_have_text_but_text_cannot_replace_control_value():
    bundle = load()
    message = bundle["turn"][0]["messages"][2]
    message["blocks"].append({"kind": "text", "text": "Here is the completed reply."})
    validate_bundle(bundle)
    message["blocks"][-1]["text"] = '{"text":"forged"}'
    bundle["turn"][0]["final"]["value"] = {"text": "forged"}
    with pytest.raises(ContractValidationError, match="source message"):
        validate_bundle(bundle)


def generic_bundle():
    bundle = load()
    bundle["node_definition"][0]["kind"] = "io"
    agent = bundle.pop("run_record")[0]
    bundle["node_run"] = [{
        "schema_version": 1, "profile": "node", "run_id": agent["run_id"],
        "workflow_session_id": agent["workflow_session_id"], "node_binding_id": agent["node_binding_id"],
        "chain_run_id": agent["chain_run_id"], "input_id": agent["input_id"],
        "input_snapshot_id": None, "source_run_id": None, "status": "succeeded", "revision": 2,
        "result_output_id": uid(13), "superseded_by_run_id": None,
    }]
    for kind in ("input_snapshot", "turn", "candidate_group", "candidate_selection"):
        bundle.pop(kind)
    for checkpoint in bundle["workflow_checkpoint"]:
        checkpoint["selection_refs"] = []
        for node in checkpoint["nodes"]:
            node["selected_turn_id"] = None
    bundle["workflow_output"][0]["source"]["turn_id"] = None
    return bundle


def test_generic_io_node_closes_without_agent_snapshot_model_or_turn():
    bundle = generic_bundle()
    assert validate_bundle(bundle) == bundle


@pytest.mark.parametrize("mutation", [
    lambda b: b["node_definition"][0].update(kind="agent"),
    lambda b: b["node_run"][0].update(result_output_id=uid(900)),
    lambda b: b["node_run"][0].update(input_snapshot_id=uid(900)),
    lambda b: b["node_run"][0].update(source_run_id=uid(7)),
    lambda b: b["node_run"][0].update(status="failed"),
    lambda b: b["workflow_output"][0]["source"].update(turn_id=uid(9)),
    lambda b: b["chain_run"][0].update(node_run_ids=[]),
])
def test_generic_io_node_rejects_inconsistent_execution(mutation):
    bundle = generic_bundle()
    mutation(bundle)
    with pytest.raises(ContractValidationError):
        validate_bundle(bundle)


def test_agent_profile_cannot_run_non_agent_binding():
    bundle = load()
    bundle["node_definition"][0]["kind"] = "io"
    with pytest.raises(ContractValidationError, match="Agent binding"):
        validate_bundle(bundle)


def add_result(bundle, start=500):
    node_input = copy.deepcopy(bundle["node_input"][0])
    node_input["input_id"] = uid(start)
    snapshot = copy.deepcopy(bundle["input_snapshot"][0])
    snapshot.update(snapshot_id=uid(start + 1), input_id=uid(start))
    run = copy.deepcopy(bundle["run_record"][0])
    run.update(run_id=uid(start + 2), input_id=uid(start), snapshot_id=uid(start + 1),
               result_turn_id=uid(start + 3), source_run_id=None)
    turn = copy.deepcopy(bundle["turn"][0])
    turn.update(turn_id=uid(start + 3), input_id=uid(start), snapshot_id=uid(start + 1),
                run_id=uid(start + 2))
    for offset, message in enumerate(turn["messages"]):
        message["message_id"] = uid(start + 10 + offset)
        if message["role"] == "assistant":
            message["source"]["request_id"] = uid(start + 20 + offset)
            message["blocks"][0]["tool_call_id"] = uid(start + 30 + offset)
        else:
            message["source"]["tool_execution_id"] = uid(start + 40 + offset)
            message["blocks"][0]["tool_execution_id"] = uid(start + 40 + offset)
            message["blocks"][0]["tool_call_id"] = uid(start + 30 + offset - 1)
    turn["final"]["message_id"] = uid(start + 12)
    bundle["node_input"].append(node_input)
    bundle["input_snapshot"].append(snapshot)
    bundle["run_record"].append(run)
    bundle["turn"].append(turn)
    bundle["chain_run"][0]["node_run_ids"].append(run["run_id"])
    return node_input, snapshot, run, turn


def test_candidate_cannot_change_input_when_source_run_is_absent():
    bundle = load()
    node_input, _, run, turn = add_result(bundle)
    bundle["candidate_group"][0]["candidate_refs"].append({"run_id": run["run_id"], "turn_id": turn["turn_id"]})
    node_input["payload"]["text"] = "Not the frozen user input"
    with pytest.raises(ContractValidationError, match="changed input content"):
        validate_bundle(bundle)


def test_turn_cannot_be_registered_in_two_candidate_groups():
    bundle = load()
    group = copy.deepcopy(bundle["candidate_group"][0])
    group["candidate_group_id"] = uid(550)
    bundle["candidate_group"].append(group)
    with pytest.raises(ContractValidationError, match="multiple candidate groups"):
        validate_bundle(bundle)


def test_completed_checkpoint_keeps_historical_selections_along_parent_path():
    bundle = load()
    node_input, snapshot, run, turn = add_result(bundle)
    snapshot["parent_turn_id"] = turn["parent_turn_id"] = uid(9)
    group = copy.deepcopy(bundle["candidate_group"][0])
    group.update(candidate_group_id=uid(550), logical_input_id=node_input["input_id"],
                 parent_turn_id=uid(9), frozen_snapshot_id=snapshot["snapshot_id"],
                 candidate_refs=[{"run_id": run["run_id"], "turn_id": turn["turn_id"]}])
    bundle["candidate_group"].append(group)
    checkpoint = bundle["workflow_checkpoint"][1]
    checkpoint["nodes"][0]["selected_turn_id"] = turn["turn_id"]
    checkpoint["selection_refs"].append({"candidate_group_id": uid(550),
                                         "selected_turn_id": turn["turn_id"]})
    validate_bundle(bundle)


def test_checkpoint_cannot_select_sibling_of_its_output_source():
    bundle = load()
    _, _, run, turn = add_result(bundle)
    bundle["candidate_group"][0]["candidate_refs"].append({"run_id": run["run_id"], "turn_id": turn["turn_id"]})
    checkpoint = bundle["workflow_checkpoint"][1]
    checkpoint["nodes"][0]["selected_turn_id"] = turn["turn_id"]
    checkpoint["selection_refs"][0]["selected_turn_id"] = turn["turn_id"]
    with pytest.raises(ContractValidationError, match="output is outside"):
        validate_bundle(bundle)


@pytest.mark.parametrize("field,value", [
    ("content", {"error": "execution did not return answer"}),
    ("model_visible_text", '{"text":"another answer"}'),
    ("model_visible_text", "not JSON"),
])
def test_successful_final_control_must_return_the_actual_answer(field, value):
    bundle = load()
    bundle["turn"][0]["messages"][-1]["blocks"][0][field] = value
    with pytest.raises(ContractValidationError, match="Final control"):
        validate_bundle(bundle)


def test_two_user_branches_cannot_share_one_pending_input_identity():
    bundle = load("user_fork")
    child = copy.deepcopy(bundle["workflow_session"][1])
    child["workflow_session_id"] = uid(600)
    child["source"]["fork_anchor_id"] = uid(601)
    anchor = copy.deepcopy(bundle["fork_anchor"][0])
    anchor["fork_anchor_id"] = uid(601)
    ref = copy.deepcopy(bundle["visible_message_ref"][2])
    ref["workflow_session_id"] = uid(600)
    bundle["workflow_session"].append(child)
    bundle["fork_anchor"].append(anchor)
    bundle["visible_message_ref"].append(ref)
    with pytest.raises(ContractValidationError, match="independent input"):
        validate_bundle(bundle)


def test_two_sessions_cannot_execute_the_same_input_identity():
    bundle = load()
    session = copy.deepcopy(bundle["workflow_session"][0])
    session["workflow_session_id"] = uid(600)
    chain = copy.deepcopy(bundle["chain_run"][0])
    chain.update(chain_run_id=uid(601), workflow_session_id=uid(600),
                 status="running", node_run_ids=[], output_id=None)
    bundle["workflow_session"].append(session)
    bundle["chain_run"].append(chain)
    with pytest.raises(ContractValidationError, match="another workflow session"):
        validate_bundle(bundle)


def test_successful_result_must_be_registered_as_a_candidate():
    bundle = load()
    add_result(bundle)
    with pytest.raises(ContractValidationError, match="missing from candidate"):
        validate_bundle(bundle)
