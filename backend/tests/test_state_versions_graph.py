"""Cross-record checks for the optional state-version layer."""

import copy
from pathlib import Path

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import loads_strict


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def operation(session_id, operation_id, key, revision):
    return {
        "schema_version": 1,
        "operation_id": operation_id,
        "kind": "submit",
        "scope": {"kind": "workflow_session", "id": session_id},
        "target": {"kind": "workflow_session", "id": session_id},
        "idempotency_key": key,
        "expected_revisions": [
            {"kind": "workflow_session", "id": session_id, "revision": revision},
        ],
        "payload": {},
    }


def versioned_bundle():
    bundle = loads_strict(EXAMPLE.read_text(encoding="utf-8"))
    session = bundle["workflow_session"][0]
    sid = session["workflow_session_id"]
    initial, completed = bundle["workflow_checkpoint"]

    def snapshot(checkpoint, snapshot_id, visible_refs):
        return {
            "schema_version": 1,
            "state_snapshot_id": snapshot_id,
            "workflow_session_id": sid,
            "workflow_definition_id": session["workflow_definition_id"],
            "definition_revision": session["definition_revision"],
            "node_states": [
                {
                    "node_binding_id": node["node_binding_id"],
                    "data_version": node["data_version"],
                    "private_data": copy.deepcopy(node["private_data"]),
                    "selected_result": (
                        {"kind": "agent_turn", "turn_id": node["selected_turn_id"]}
                        if node["selected_turn_id"] else None
                    ),
                }
                for node in checkpoint["nodes"]
            ],
            "selection_refs": copy.deepcopy(checkpoint["selection_refs"]),
            "visible_message_refs": [
                {key: copy.deepcopy(ref[key])
                 for key in ("visible_message_id", "sequence", "role", "boundary")}
                for ref in visible_refs
            ],
            "pending_input_id": None,
        }

    bundle["workflow_operation"] = [
        operation(sid, uid(301), "seed", 1),
        operation(sid, uid(302), "result", 2),
    ]
    bundle["state_snapshot"] = [
        snapshot(initial, uid(303), []),
        snapshot(completed, uid(304), bundle["visible_message_ref"]),
    ]
    bundle["workflow_commit"] = [
        {
            "schema_version": 1, "commit_id": uid(305),
            "workflow_session_id": sid, "state_snapshot_id": uid(303),
            "parent_commit_id": None, "operation_id": uid(301),
            "source": {"kind": "session_seed", "workflow_session_id": sid},
        },
        {
            "schema_version": 1, "commit_id": uid(306),
            "workflow_session_id": sid, "state_snapshot_id": uid(304),
            "parent_commit_id": uid(305), "operation_id": uid(302),
            "source": {"kind": "candidate", "candidate_id": uid(307)},
        },
    ]
    bundle["workflow_candidate"] = [{
        "schema_version": 1, "candidate_id": uid(307),
        "workflow_session_id": sid, "chain_run_id": bundle["chain_run"][0]["chain_run_id"],
        "base_commit_id": uid(305), "result_commit_id": uid(306),
        "output_id": completed["output_id"], "checkpoint_id": completed["checkpoint_id"],
    }]
    bundle["workflow_ref"] = [{
        "schema_version": 1, "workflow_ref_id": uid(308),
        "workflow_session_id": sid, "head_commit_id": uid(305), "revision": 1,
    }]
    return bundle


def reroll_bundle():
    bundle = versioned_bundle()
    source = bundle["chain_run"][0]
    original_run = bundle["run_record"][0]
    original_input = bundle["node_input"][0]
    original_snapshot = bundle["input_snapshot"][0]
    session_id = source["workflow_session_id"]
    chain_id, input_id, snapshot_id, run_id = (uid(number) for number in range(330, 334))
    new_input = copy.deepcopy(original_input)
    new_input["input_id"] = input_id
    new_snapshot = copy.deepcopy(original_snapshot)
    new_snapshot.update(snapshot_id=snapshot_id, input_id=input_id)
    new_run = copy.deepcopy(original_run)
    new_run.update(
        run_id=run_id, chain_run_id=chain_id, input_id=input_id,
        snapshot_id=snapshot_id, source_run_id=original_run["run_id"],
        status="prepared", revision=1, result_turn_id=None,
    )
    new_chain = {
        "schema_version": 2, "chain_run_id": chain_id,
        "workflow_session_id": session_id, "input_id": input_id,
        "status": "prepared", "node_run_ids": [run_id], "output_id": None,
        "base_commit_id": uid(305), "base_ref_revision": 1,
        "operation_id": uid(334),
    }
    bundle["node_input"].append(new_input)
    bundle["input_snapshot"].append(new_snapshot)
    bundle["run_record"].append(new_run)
    bundle["chain_run"].append(new_chain)
    bundle["workflow_operation"].append({
        "schema_version": 1, "operation_id": uid(334),
        "kind": "reroll",
        "scope": {"kind": "workflow_session", "id": session_id},
        "target": {"kind": "chain_run", "id": chain_id},
        "idempotency_key": "reroll-once",
        "expected_revisions": [
            {"kind": "workflow_session", "id": session_id, "revision": 1},
            {"kind": "workflow_ref", "id": uid(308), "revision": 1},
        ],
        "payload": {
            "chain_run_id": chain_id, "base_commit_id": uid(305),
            "base_ref_revision": 1,
        },
    })
    bundle["chain_input_origin"] = [{
        "schema_version": 1, "chain_run_id": chain_id,
        "workflow_session_id": session_id,
        "visible_message_id": bundle["visible_message_ref"][0]["visible_message_id"],
        "input_id": input_id, "source_chain_run_id": source["chain_run_id"],
    }]
    return bundle


def test_optional_versions_keep_unselected_candidate_and_commit_readable():
    bundle = versioned_bundle()
    checked = validate_bundle(bundle)
    assert checked["workflow_ref"][0]["head_commit_id"] == uid(305)
    assert checked["workflow_candidate"][0]["result_commit_id"] == uid(306)
    assert validate_bundle(bundle) == checked


def test_candidate_result_requires_one_formal_reply_on_its_user_floor():
    bundle = versioned_bundle()
    bundle["state_snapshot"][1]["visible_message_refs"].pop()
    with pytest.raises(ContractValidationError, match="one reply on its user floor"):
        validate_bundle(bundle)


def test_reroll_chain_preserves_user_identity_with_immutable_origin():
    bundle = reroll_bundle()
    checked = validate_bundle(bundle)
    assert checked["chain_input_origin"][0]["visible_message_id"] == (
        checked["visible_message_ref"][0]["visible_message_id"]
    )
    assert checked["chain_run"][0]["input_id"] != checked["chain_run"][1]["input_id"]


@pytest.mark.parametrize("mutate", [
    lambda b: b.pop("chain_input_origin"),
    lambda b: b["chain_input_origin"][0].update(workflow_session_id=uid(999)),
    lambda b: b["chain_input_origin"][0].update(visible_message_id=uid(999)),
    lambda b: b["chain_input_origin"][0].update(input_id=uid(999)),
    lambda b: b["chain_input_origin"][0].update(source_chain_run_id=uid(999)),
    lambda b: b["chain_input_origin"][0].update(source_chain_run_id=uid(330)),
    lambda b: b["node_input"][-1]["payload"].update(text="changed"),
    lambda b: b["chain_run"][-1].update(base_commit_id=uid(306)),
])
def test_reroll_origin_rejects_missing_foreign_or_changed_input(mutate):
    bundle = reroll_bundle()
    mutate(bundle)
    with pytest.raises(ContractValidationError):
        validate_bundle(bundle)


def test_reroll_origin_rejects_reusing_source_input_identity():
    bundle = reroll_bundle()
    source_input_id = bundle["chain_run"][0]["input_id"]
    bundle["chain_run"][-1]["input_id"] = source_input_id
    bundle["chain_input_origin"][0]["input_id"] = source_input_id
    with pytest.raises(ContractValidationError, match="new input identity"):
        validate_bundle(bundle)


@pytest.mark.parametrize("mutate", [
    lambda b: b["workflow_commit"][0].update(parent_commit_id=uid(306)),
    lambda b: b["workflow_commit"][1].update(state_snapshot_id=uid(999)),
    lambda b: b["workflow_commit"][1].update(parent_commit_id=None),
    lambda b: b["workflow_commit"][1]["source"].update(candidate_id=uid(999)),
    lambda b: b["workflow_commit"][1].update(operation_id=uid(999)),
    lambda b: b["workflow_ref"][0].update(head_commit_id=uid(999)),
    lambda b: b["workflow_ref"].append({
        **b["workflow_ref"][0], "workflow_ref_id": uid(309),
    }),
    lambda b: b["workflow_candidate"][0].update(chain_run_id=uid(999)),
    lambda b: b["workflow_candidate"][0].update(base_commit_id=uid(306)),
    lambda b: b["state_snapshot"][1]["node_states"][0]["selected_result"].update(turn_id=uid(999)),
    lambda b: b["state_snapshot"][1]["selection_refs"][0].update(selected_turn_id=None),
    lambda b: b["state_snapshot"][1].update(pending_input_id=uid(999)),
    lambda b: b["state_snapshot"][1]["visible_message_refs"][0]["boundary"].update(input_id=uid(999)),
    lambda b: b["workflow_operation"][1]["target"].update(id=uid(999)),
])
def test_state_versions_reject_bad_references_and_parent_cycles(mutate):
    bundle = versioned_bundle()
    mutate(bundle)
    with pytest.raises(ContractValidationError):
        validate_bundle(bundle)


def test_pending_start_operation_cannot_target_another_sessions_input():
    bundle = loads_strict(
        (EXAMPLE.parent / "user_fork.json").read_text(encoding="utf-8")
    )
    source, child = bundle["workflow_session"]
    pending = next(
        ref["boundary"]["input_id"] for ref in bundle["visible_message_ref"]
        if ref["workflow_session_id"] == child["workflow_session_id"]
        and ref["boundary"]["input_status"] == "pending"
    )
    request = operation(source["workflow_session_id"], uid(320), "cross-session", 1)
    request["kind"] = "continue_pending_input"
    request["target"] = {"kind": "node_input", "id": pending}
    bundle["workflow_operation"] = [request]
    with pytest.raises(ContractValidationError, match="Operation input belongs"):
        validate_bundle(bundle)
