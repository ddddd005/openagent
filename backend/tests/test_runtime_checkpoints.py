"""Synthetic, in-memory checkpoint contracts; no runtime or storage writes."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.runtime_checkpoints import (
    make_diff, materialize, validate_checkpoint, validate_runtime_state, validate_state_storage,
)


WORKFLOW, SESSION, NODE, ENTRY, ROUND = (str(uuid4()) for _ in range(5))


def state():
    return {
        "schema_version": 1, "kind": "workflow.runtime-state",
        "node_states": {
            NODE: {"owner_component_id": "test.counter", "schema_version": 1,
                   "payload": {"count": 3, "revision": 9}},
        },
        "program_state": {
            "values": {"name": {"type": "string", "source": "assignment", "value": "old"}},
            "data": {
                "test:value": {
                    "definition": {
                        "schema_version": 1, "definition_id": str(uuid4()), "revision": 2,
                        "key": "test:value", "name": "Value", "schema": {},
                        "writable": True, "public": True,
                    },
                    "value": {"items": [1, 2]},
                },
            },
        },
        "objects": {
            "context": {"type_id": "test.context", "schema_version": 4, "deleted": False,
                        "value": {"generation": 2, "accepted_delta_ids": [ENTRY]}},
            "nullable": {"type_id": "test.nullable", "schema_version": 1, "deleted": False,
                         "value": None},
        },
        "history": {"selected_round_id": None, "visible_entry_ids": []},
    }


def initial(value=None):
    return {
        "schema_version": 1, "checkpoint_id": str(uuid4()),
        "workflow_definition_id": WORKFLOW, "workflow_session_id": SESSION,
        "fixed_definition_revision": 3, "kind": "initial",
        "parent_checkpoint_id": None, "round_id": None, "chain_run_id": None,
        "assistant_entry_id": None,
        "state_storage": {"mode": "full", "state": value if value is not None else state()},
    }


def successful(root, parent, before, after):
    return {
        "schema_version": 1, "checkpoint_id": str(uuid4()),
        "workflow_definition_id": WORKFLOW, "workflow_session_id": SESSION,
        "fixed_definition_revision": 3, "kind": "successful_round",
        "parent_checkpoint_id": parent["checkpoint_id"], "round_id": ROUND,
        "chain_run_id": str(uuid4()), "assistant_entry_id": ENTRY,
        "state_storage": {
            "mode": "diff", "base_checkpoint_id": root["checkpoint_id"],
            **make_diff(before, after),
        },
    }


def materialize_records(target, records):
    return materialize(target["checkpoint_id"], {row["checkpoint_id"]: row for row in records}.get)


def test_state_preserves_business_envelopes_counters_and_detaches_values():
    original = state()
    checked = validate_runtime_state(original)
    assert checked == original and checked is not original
    checked["node_states"][NODE]["payload"]["count"] = 0
    checked["objects"]["context"]["value"]["accepted_delta_ids"].clear()
    checked["program_state"]["data"]["test:value"]["definition"]["revision"] = 4
    assert original["node_states"][NODE]["payload"] == {"count": 3, "revision": 9}
    assert original["objects"]["context"]["value"]["accepted_delta_ids"] == [ENTRY]
    assert original["program_state"]["data"]["test:value"]["definition"]["revision"] == 2


@pytest.mark.parametrize("path,field", [
    ((), "revision"), ((), "created_at"), ((), "context"), ((), "counters"),
    (("program_state",), "revision"), (("node_states", NODE), "data_version"),
    (("objects", "context"), "revision"), (("objects", "context"), "revision_id"),
    (("objects", "context"), "binding"),
])
def test_runtime_state_rejects_storage_and_duplicate_partitions(path, field):
    value = state()
    target = value
    for key in path:
        target = target[key]
    target[field] = 1
    with pytest.raises(ContractValidationError):
        validate_runtime_state(value)


@pytest.mark.parametrize("path,replacement", [
    (("schema_version",), True), (("schema_version",), 2), (("kind",), "other"),
    (("node_states", NODE, "owner_component_id"), ""),
    (("node_states", NODE, "schema_version"), True),
    (("objects", "context", "schema_version"), 0),
    (("objects", "context", "deleted"), 1),
    (("objects", "context", "type_id"), ""),
    (("history", "selected_round_id"), "unknown"),
    (("history", "visible_entry_ids"), [ENTRY, ENTRY]),
    (("program_state", "values", "name", "value"), 3),
    (("program_state", "data", "test:value", "definition", "revision"), 0),
])
def test_runtime_state_requires_exact_identity_type_and_schema(path, replacement):
    value = state()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    with pytest.raises(ContractValidationError):
        validate_runtime_state(value)


def test_runtime_state_rejects_missing_envelope_and_non_json_body():
    value = state()
    del value["node_states"][NODE]["owner_component_id"]
    with pytest.raises(ContractValidationError):
        validate_runtime_state(value)
    value = state()
    value["node_states"]["not-a-uuid"] = value["node_states"].pop(NODE)
    with pytest.raises(ContractValidationError):
        validate_runtime_state(value)
    value = state()
    value["objects"]["context"]["value"] = float("nan")
    with pytest.raises(ContractValidationError):
        validate_runtime_state(value)


def test_diff_materializes_all_partitions_with_null_tombstone_and_deletion():
    before = state()
    root = initial(before)
    after = deepcopy(before)
    del after["node_states"][NODE]
    added_node = str(uuid4())
    after["node_states"][added_node] = {
        "owner_component_id": "test.new", "schema_version": 1, "payload": {"count": 0},
    }
    after["program_state"]["values"] = {
        "ready": {"type": "boolean", "source": "assignment", "value": False},
    }
    after["program_state"]["data"]["test:value"]["value"] = None
    after["objects"]["context"].update(deleted=True, value=None)
    del after["objects"]["nullable"]
    after["history"] = {"selected_round_id": ROUND, "visible_entry_ids": [ENTRY]}
    changes = make_diff(before, after)
    assert changes["delete"] == {
        "node_states": [NODE], "program_values": ["name"], "program_data": [], "objects": ["nullable"],
    }
    assert changes["upsert"]["program_data"]["test:value"]["value"] is None
    assert changes["upsert"]["objects"]["context"] == {
        "type_id": "test.context", "schema_version": 4, "deleted": True, "value": None,
    }
    child = successful(root, root, before, after)
    assert materialize_records(child, [root, child]) == after
    assert root["state_storage"]["state"] == before
    del after["program_state"]["data"]["test:value"]
    grandchild = successful(root, child, materialize_records(child, [root, child]), after)
    assert grandchild["state_storage"]["delete"]["program_data"] == ["test:value"]
    assert materialize_records(grandchild, [root, child, grandchild]) == after


def test_diff_replaces_whole_entries_and_arrays_and_ignores_mapping_order():
    before = state()
    after = deepcopy(before)
    after["node_states"][NODE]["payload"] = {"count": 4}
    after["program_state"]["data"]["test:value"]["value"]["items"] = [2, 1]
    changes = make_diff(before, after)
    assert changes["upsert"]["node_states"][NODE] == after["node_states"][NODE]
    root = initial(before)
    child = successful(root, root, before, after)
    assert materialize_records(child, [root, child]) == after
    reordered = dict(reversed(list(before.items())))
    assert make_diff(before, reordered) == {
        "upsert": {key: {} for key in ("node_states", "program_values", "program_data", "objects")},
        "delete": {key: [] for key in ("node_states", "program_values", "program_data", "objects")},
    }


def test_history_is_whole_projection_optional_when_unchanged():
    before = state()
    before["history"] = {"selected_round_id": ROUND, "visible_entry_ids": [ENTRY]}
    after = deepcopy(before)
    after["history"] = {"selected_round_id": None, "visible_entry_ids": []}
    changes = make_diff(before, after)
    assert changes["upsert"]["history"] == after["history"]
    assert "history" not in changes["delete"]
    assert "history" not in make_diff(before, before)["upsert"]
    changes["upsert"]["history"]["visible_entry_ids"].append(ENTRY)
    assert after["history"]["visible_entry_ids"] == []


def test_unchanged_successful_checkpoint_and_full_root_are_detached():
    before = state()
    root = initial(before)
    child = successful(root, root, before, before)
    checked = validate_checkpoint(child)
    assert checked == child and checked is not child
    restored = materialize_records(child, [root, child])
    restored["objects"]["context"]["value"]["generation"] = 99
    assert materialize_records(root, [root]) == before
    assert materialize_records(child, [root, child]) == before


def test_materialize_reads_only_selected_parent_branch():
    before = state()
    root = initial(before)
    left_state, right_state = deepcopy(before), deepcopy(before)
    left_state["node_states"][NODE]["payload"]["count"] = 10
    right_state["node_states"][NODE]["payload"]["count"] = 20
    left = successful(root, root, before, left_state)
    right = successful(root, root, before, right_state)
    left_next = deepcopy(left_state)
    left_next["objects"]["context"]["value"]["generation"] = 7
    tip = successful(root, left, left_state, left_next)
    records = {row["checkpoint_id"]: row for row in (right, tip, root, left)}
    visited = []

    def load(identity):
        visited.append(identity)
        return records.get(identity)

    assert materialize(tip["checkpoint_id"], load) == left_next
    assert visited == [tip["checkpoint_id"], left["checkpoint_id"], root["checkpoint_id"]]


@pytest.mark.parametrize("partition", ["node_states", "program_values", "program_data", "objects"])
def test_diff_rejects_upsert_delete_overlap_and_duplicate_delete(partition):
    value = state()
    root = initial(value)
    child = successful(root, root, value, value)
    changes = child["state_storage"]
    logical_partition = {
        "node_states": value["node_states"], "program_values": value["program_state"]["values"],
        "program_data": value["program_state"]["data"], "objects": value["objects"],
    }[partition]
    key = next(iter(logical_partition))
    changes["upsert"][partition][key] = deepcopy(logical_partition[key])
    changes["delete"][partition] = [key]
    with pytest.raises(ContractValidationError):
        validate_state_storage(changes)
    changes["upsert"][partition] = {}
    changes["delete"][partition] = [key, key]
    with pytest.raises(ContractValidationError):
        validate_state_storage(changes)


@pytest.mark.parametrize("mutation", [
    "full_extra_base", "full_extra_diff", "full_not_initial", "diff_initial",
    "diff_missing_partition", "diff_history_null", "diff_history_delete",
    "initial_has_parent", "success_missing_assistant", "boolean_fixed_revision",
    "deleted_has_body",
])
def test_full_diff_and_checkpoint_envelopes_are_strict(mutation):
    value = state()
    root = initial(value)
    child = successful(root, root, value, value)
    target = child
    if mutation == "full_extra_base":
        target = root
        target["state_storage"]["base_checkpoint_id"] = root["checkpoint_id"]
    elif mutation == "full_extra_diff":
        target = root
        target["state_storage"]["upsert"] = {}
    elif mutation == "full_not_initial":
        child["state_storage"] = root["state_storage"]
    elif mutation == "diff_initial":
        target = root
        target["state_storage"] = child["state_storage"]
    elif mutation == "diff_missing_partition":
        del child["state_storage"]["upsert"]["objects"]
    elif mutation == "diff_history_null":
        child["state_storage"]["upsert"]["history"] = None
    elif mutation == "diff_history_delete":
        child["state_storage"]["delete"]["history"] = []
    elif mutation == "initial_has_parent":
        target = root
        root["parent_checkpoint_id"] = child["checkpoint_id"]
    elif mutation == "success_missing_assistant":
        child["assistant_entry_id"] = None
    elif mutation == "boolean_fixed_revision":
        child["fixed_definition_revision"] = True
    elif mutation == "deleted_has_body":
        target = root
        root["state_storage"]["state"]["objects"]["context"]["deleted"] = True
    with pytest.raises(ContractValidationError):
        validate_checkpoint(target)


@pytest.mark.parametrize("field", [
    "workflow_session_id", "workflow_definition_id", "fixed_definition_revision",
])
def test_materialize_rejects_parent_owner_or_fixed_revision_change(field):
    before = state()
    root = initial(before)
    child = successful(root, root, before, before)
    root[field] = 4 if field == "fixed_definition_revision" else str(uuid4())
    with pytest.raises(ContractValidationError) as caught:
        materialize_records(child, [root, child])
    assert caught.value.reason_code == "runtime_checkpoint_lineage_invalid"


def test_materialize_rejects_wrong_base_cycle_missing_or_wrong_loaded_identity():
    before = state()
    root = initial(before)
    child = successful(root, root, before, before)
    child["state_storage"]["base_checkpoint_id"] = str(uuid4())
    with pytest.raises(ContractValidationError, match="baseline"):
        materialize_records(child, [root, child])
    child["state_storage"]["base_checkpoint_id"] = root["checkpoint_id"]
    child["parent_checkpoint_id"] = child["checkpoint_id"]
    with pytest.raises(ContractValidationError, match="cyclic"):
        materialize_records(child, [root, child])
    child["parent_checkpoint_id"] = root["checkpoint_id"]
    with pytest.raises(ContractValidationError) as caught:
        materialize_records(child, [child])
    assert caught.value.reason_code == "runtime_checkpoint_not_found"
    assert caught.value.status_code == 404
    with pytest.raises(ContractValidationError, match="another identity"):
        materialize(child["checkpoint_id"], lambda _: root)


def test_materialize_rejects_delete_missing_from_parent_and_invalid_target_id():
    before = state()
    root = initial(before)
    child = successful(root, root, before, before)
    child["state_storage"]["delete"]["objects"] = ["absent"]
    with pytest.raises(ContractValidationError, match="absent"):
        materialize_records(child, [root, child])
    with pytest.raises(ContractValidationError):
        materialize("invalid", lambda _: pytest.fail("Invalid target must not load records"))
