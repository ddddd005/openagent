"""Offline tests for the 2.2 logical checkpoint state and full/diff rules."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.runtime_checkpoints import (
    make_diff, materialize, validate_checkpoint, validate_checkpoint_state, validate_state_storage,
)


def identity():
    return str(uuid4())


WORKFLOW, SESSION, NODE, OBJECT = (identity() for _ in range(4))
OWNER = {
    "workflow_definition_id": WORKFLOW,
    "workflow_session_id": SESSION,
    "fixed_definition_revision": 3,
}


def agent_value(generation=0):
    return {"messages": [], "layout": [], "accepted_delta_ids": [], "generation": generation}


def agent_entry(generation=0):
    return {"value": agent_value(generation)}


def state():
    return {
        "configuration": {
            "workflow_definition_id": WORKFLOW,
            "fixed_definition_revision": 3,
            "nodes": {
                NODE: {
                    "component_id": "test.counter",
                    "component_version": "1",
                    "config": {"count": 1},
                },
            },
        },
        "agent_contexts": {OBJECT: agent_entry()},
        "variables": {},
        "chat_contexts": {},
    }


def root(value=None, owner=None):
    owner = OWNER if owner is None else owner
    return {
        "schema_version": 4,
        "checkpoint_id": identity(),
        **owner,
        "parent_checkpoint_id": None,
        "state_storage": {"mode": "full", "state": deepcopy(state() if value is None else value)},
    }


def child(root_point, parent, before, after, owner=None):
    owner = OWNER if owner is None else owner
    return {
        "schema_version": 4,
        "checkpoint_id": identity(),
        **owner,
        "parent_checkpoint_id": parent["checkpoint_id"],
        "state_storage": {
            "mode": "diff",
            "base_checkpoint_id": root_point["checkpoint_id"],
            **make_diff(before, after),
        },
    }


def materialize_records(target, records):
    return materialize(target["checkpoint_id"],
                       {point["checkpoint_id"]: point for point in records}.get)


def test_checkpoint_state_accepts_empty_branches_and_returns_a_detached_value():
    original = {
        "configuration": {
            "workflow_definition_id": WORKFLOW,
            "fixed_definition_revision": 3,
            "nodes": {},
        },
        "agent_contexts": {},
        "variables": {},
        "chat_contexts": {},
    }
    checked = validate_checkpoint_state(original)
    assert checked == original and checked is not original
    checked["configuration"]["nodes"][NODE] = {
        "component_id": "test.node", "component_version": "1", "config": {},
    }
    assert original["configuration"]["nodes"] == {}


@pytest.mark.parametrize("obsolete_schema", [1, 2, 3])
def test_checkpoint_record_requires_schema_four(obsolete_schema):
    value = root()
    value["schema_version"] = obsolete_schema
    with pytest.raises(ContractValidationError):
        validate_checkpoint(value)


@pytest.mark.parametrize("mutation", [
    "legacy_envelope", "extra_state_field", "bad_config_owner", "invalid_node_id",
    "agent_owner_field", "agent_source_pointer",
])
def test_checkpoint_state_rejects_legacy_or_invalid_state(mutation):
    value = state()
    if mutation == "legacy_envelope":
        value["node_states"] = {}
    elif mutation == "extra_state_field":
        value["history"] = {}
    elif mutation == "bad_config_owner":
        value["configuration"]["fixed_definition_revision"] = True
    elif mutation == "invalid_node_id":
        value["configuration"]["nodes"]["not-a-uuid"] = value["configuration"]["nodes"].pop(NODE)
    elif mutation == "agent_owner_field":
        value["agent_contexts"][OBJECT]["agent_node_id"] = identity()
    else:
        value["agent_contexts"][OBJECT]["value"]["view_ref"] = None
    with pytest.raises(ContractValidationError):
        validate_checkpoint_state(value)


def test_state_rejects_unprojected_agent_source_and_preserves_empty_agent_state():
    value = state()
    value["agent_contexts"][OBJECT]["value"]["messages"] = [{
        "message_id": identity(), "role": "user", "blocks": [{"type": "text", "text": "x"}],
        "source": {"kind": "request"},
    }]
    value["agent_contexts"][OBJECT]["value"]["layout"] = [{
        "kind": "history", "round_id": identity(), "compaction": "never",
    }]
    with pytest.raises(ContractValidationError):
        validate_checkpoint_state(value)
    value = state()
    value["agent_contexts"][OBJECT]["value"] = agent_value()
    assert validate_checkpoint_state(value)["agent_contexts"][OBJECT]["value"] == agent_value()


def test_diff_materializes_configuration_replacement_and_agent_key_changes():
    before = state()
    removed = identity()
    before["agent_contexts"][removed] = agent_entry()
    after = deepcopy(before)
    after["configuration"]["nodes"][NODE]["config"]["count"] = 2
    after["agent_contexts"][OBJECT] = agent_entry(1)
    added = identity()
    after["agent_contexts"][added] = agent_entry(0)
    del after["agent_contexts"][removed]
    changes = make_diff(before, after)
    assert changes["upsert"]["configuration"] == after["configuration"]
    assert changes["upsert"]["agent_contexts"] == {
        OBJECT: after["agent_contexts"][OBJECT], added: after["agent_contexts"][added],
    }
    assert changes["delete"]["agent_contexts"] == [removed]
    root_point = root(before)
    point = child(root_point, root_point, before, after)
    assert materialize_records(point, [root_point, point]) == after


def test_diff_is_empty_for_equivalent_mapping_order():
    before = state()
    reordered = {
        "agent_contexts": dict(reversed(list(before["agent_contexts"].items()))),
        "variables": {},
        "chat_contexts": {},
        "configuration": {
            **before["configuration"],
            "nodes": dict(reversed(list(before["configuration"]["nodes"].items()))),
        },
    }
    assert make_diff(before, reordered) == {
        "upsert": {"agent_contexts": {}, "variables": {}, "chat_contexts": {}},
        "delete": {"agent_contexts": [], "variables": [], "chat_contexts": []},
    }


def test_materialize_reads_only_the_selected_parent_prefix():
    before = state()
    root_point = root(before)
    left_state, right_state = deepcopy(before), deepcopy(before)
    left_state["agent_contexts"][OBJECT] = agent_entry(1)
    right_state["agent_contexts"][OBJECT] = agent_entry(2)
    left = child(root_point, root_point, before, left_state)
    right = child(root_point, root_point, before, right_state)
    tip_state = deepcopy(left_state)
    tip_state["configuration"]["nodes"][NODE]["config"]["count"] = 5
    tip = child(root_point, left, left_state, tip_state)
    records = [right, tip, root_point, left]
    visited = []
    by_id = {point["checkpoint_id"]: point for point in records}

    def load(checkpoint_id):
        visited.append(checkpoint_id)
        return by_id.get(checkpoint_id)

    assert materialize(tip["checkpoint_id"], load) == tip_state
    assert visited == [tip["checkpoint_id"], left["checkpoint_id"], root_point["checkpoint_id"]]
    assert materialize_records(right, records) == right_state


@pytest.mark.parametrize("field", [
    "workflow_session_id", "workflow_definition_id", "fixed_definition_revision",
])
def test_materialize_rejects_parent_owner_or_revision_change(field):
    before = state()
    root_point = root(before)
    after = deepcopy(before)
    after["agent_contexts"][OBJECT] = agent_entry(1)
    point = child(root_point, root_point, before, after)
    if field == "fixed_definition_revision":
        changed_owner = {**OWNER, field: 4}
    else:
        changed_owner = {**OWNER, field: identity()}
    changed_state = deepcopy(before)
    if field == "workflow_definition_id":
        changed_state["configuration"]["workflow_definition_id"] = changed_owner[field]
    elif field == "fixed_definition_revision":
        changed_state["configuration"]["fixed_definition_revision"] = changed_owner[field]
    changed_root = root(changed_state, changed_owner)
    point["parent_checkpoint_id"] = changed_root["checkpoint_id"]
    point["state_storage"]["base_checkpoint_id"] = changed_root["checkpoint_id"]
    with pytest.raises(ContractValidationError) as caught:
        materialize_records(point, [changed_root, point])
    assert caught.value.reason_code == "runtime_checkpoint_lineage_invalid"


def test_materialize_rejects_wrong_base_cycle_missing_delete_and_wrong_loaded_identity():
    before = state()
    root_point = root(before)
    point = child(root_point, root_point, before, before)
    point["state_storage"]["base_checkpoint_id"] = identity()
    with pytest.raises(ContractValidationError, match="baseline"):
        materialize_records(point, [root_point, point])
    point["state_storage"]["base_checkpoint_id"] = root_point["checkpoint_id"]
    point["parent_checkpoint_id"] = point["checkpoint_id"]
    with pytest.raises(ContractValidationError, match="cyclic"):
        materialize_records(point, [root_point, point])
    point["parent_checkpoint_id"] = root_point["checkpoint_id"]
    point["state_storage"]["delete"]["agent_contexts"] = [identity()]
    with pytest.raises(ContractValidationError, match="absent"):
        materialize_records(point, [root_point, point])
    with pytest.raises(ContractValidationError, match="another identity"):
        materialize(point["checkpoint_id"], lambda _: root_point)
    with pytest.raises(ContractValidationError):
        materialize(point["checkpoint_id"], lambda _: None)


@pytest.mark.parametrize("mutation", [
    "full_has_parent", "diff_has_no_parent", "diff_partition_extra",
    "diff_upsert_delete_overlap", "duplicate_delete", "config_owner_mismatch",
])
def test_checkpoint_and_diff_envelopes_are_exact(mutation):
    before = state()
    root_point = root(before)
    after = deepcopy(before)
    after["agent_contexts"][OBJECT] = agent_entry(1)
    point = child(root_point, root_point, before, after)
    if mutation == "full_has_parent":
        root_point["parent_checkpoint_id"] = identity()
    elif mutation == "diff_has_no_parent":
        point["parent_checkpoint_id"] = None
    elif mutation == "diff_partition_extra":
        point["state_storage"]["upsert"]["objects"] = {}
    elif mutation == "diff_upsert_delete_overlap":
        point["state_storage"]["delete"]["agent_contexts"] = [OBJECT]
    elif mutation == "duplicate_delete":
        point["state_storage"]["delete"]["agent_contexts"] = [OBJECT, OBJECT]
    elif mutation == "config_owner_mismatch":
        point["state_storage"]["upsert"]["configuration"] = deepcopy(before["configuration"])
        point["state_storage"]["upsert"]["configuration"]["workflow_definition_id"] = identity()
    with pytest.raises(ContractValidationError):
        validate_checkpoint(point if mutation != "full_has_parent" else root_point)


def test_materialize_rejects_missing_root_and_parent_delete_absence():
    before = state()
    root_point = root(before)
    after = deepcopy(before)
    after["agent_contexts"].clear()
    point = child(root_point, root_point, before, after)
    with pytest.raises(ContractValidationError) as caught:
        materialize(point["checkpoint_id"], lambda identity: point if identity == point["checkpoint_id"] else None)
    assert caught.value.reason_code == "runtime_checkpoint_not_found"
    point["state_storage"]["delete"]["agent_contexts"] = [identity()]
    with pytest.raises(ContractValidationError, match="absent"):
        materialize_records(point, [root_point, point])
