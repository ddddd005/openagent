"""Pure runtime-state diffs and checkpoint parent-chain materialization.

These helpers validate logical state and lineage only. Registered payload
schemas, artifact closure, permissions and current CAS writes belong to the
future transactional restore boundary.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from .contract_errors import require, uuid_value
from .contract_json import canonical_bytes, validate_json_value
from .preparation_program import validate_program_state


_PARTITIONS = ("node_states", "program_values", "program_data", "objects")
_CHECKPOINT_FIELDS = {
    "schema_version", "checkpoint_id", "workflow_definition_id", "workflow_session_id",
    "fixed_definition_revision", "kind", "parent_checkpoint_id", "round_id",
    "chain_run_id", "assistant_entry_id", "state_storage",
}


def _check(condition: bool, message: str) -> None:
    require(condition, "runtime_checkpoint_invalid", message)


def _fields(value: Any, required: set[str], *, optional: set[str] | None = None) -> None:
    _check(type(value) is dict and required <= set(value)
           and set(value) <= required | (optional or set()), "Runtime fields are invalid")


def _version(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 2**53 - 1


def _name(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 128 and value == value.strip()


def _validate_nodes(nodes: Any) -> None:
    _check(type(nodes) is dict, "Node states must be a mapping")
    for key, entry in nodes.items():
        _check(uuid_value(key), "Node state identity must be a canonical UUID4")
        _fields(entry, {"owner_component_id", "schema_version", "payload"})
        _check(_name(entry["owner_component_id"]) and type(entry["schema_version"]) is int
               and entry["schema_version"] == 1, "Node state owner or schema is invalid")


def _validate_objects(objects: Any) -> None:
    _check(type(objects) is dict, "Object states must be a mapping")
    for key, entry in objects.items():
        _check(_name(key), "Object key is invalid")
        _fields(entry, {"type_id", "schema_version", "deleted", "value"})
        _check(_name(entry["type_id"]) and _version(entry["schema_version"])
               and type(entry["deleted"]) is bool, "Object type, schema or tombstone is invalid")
        _check(not entry["deleted"] or entry["value"] is None,
               "Deleted object state must retain its type with a null value")


def _validate_history(history: Any) -> None:
    _fields(history, {"selected_round_id", "visible_entry_ids"})
    _check(history["selected_round_id"] is None or uuid_value(history["selected_round_id"]),
           "Selected round identity is invalid")
    entries = history["visible_entry_ids"]
    _check(type(entries) is list and all(uuid_value(entry) for entry in entries)
           and len(entries) == len(set(entries)), "Visible message identities are invalid")


def _validate_program(values: Any, data: Any) -> None:
    # The temporary revision is validation input, never part of logical state.
    validate_program_state({"revision": 0, "values": values, "data": data})


def validate_runtime_state(state: Any) -> dict:
    """Return detached logical state; reject storage/CAS envelopes."""
    validate_json_value(state)
    _fields(state, {"schema_version", "kind", "node_states", "program_state", "objects", "history"})
    _check(type(state["schema_version"]) is int and state["schema_version"] == 1
           and state["kind"] == "workflow.runtime-state", "Runtime state version or kind is invalid")
    _validate_nodes(state["node_states"])
    _fields(state["program_state"], {"values", "data"})
    _validate_program(state["program_state"]["values"], state["program_state"]["data"])
    _validate_objects(state["objects"])
    _validate_history(state["history"])
    return deepcopy(state)


def _validate_changes(changes: Any) -> None:
    _fields(changes, {"upsert", "delete"})
    _fields(changes["upsert"], set(_PARTITIONS), optional={"history"})
    _fields(changes["delete"], set(_PARTITIONS))
    upsert, deleted = changes["upsert"], changes["delete"]
    _validate_nodes(upsert["node_states"])
    _validate_objects(upsert["objects"])
    _validate_program(upsert["program_values"], upsert["program_data"])
    if "history" in upsert:
        _validate_history(upsert["history"])
    for partition in _PARTITIONS:
        keys = deleted[partition]
        _check(type(keys) is list and all(type(key) is str for key in keys),
               "Deleted partition keys must be a string list")
        _check(len(keys) == len(set(keys)) and not set(keys) & upsert[partition].keys(),
               "Deleted partition keys must be unique and disjoint from upsert")
        if partition == "node_states":
            _check(all(uuid_value(key) for key in keys), "Deleted node identity is invalid")
        else:
            _check(all(_name(key) for key in keys), "Deleted partition key is invalid")


def validate_state_storage(storage: Any) -> dict:
    """Validate exact full or diff storage; no legacy shape is accepted."""
    validate_json_value(storage)
    _check(type(storage) is dict, "Checkpoint storage must be an object")
    if storage.get("mode") == "full":
        _fields(storage, {"mode", "state"})
        validate_runtime_state(storage["state"])
    else:
        _fields(storage, {"mode", "base_checkpoint_id", "upsert", "delete"})
        _check(storage["mode"] == "diff" and uuid_value(storage["base_checkpoint_id"]),
               "Diff mode or full baseline identity is invalid")
        _validate_changes({key: storage[key] for key in ("upsert", "delete")})
    return deepcopy(storage)


def validate_checkpoint(checkpoint: Any) -> dict:
    """Validate the checkpoint envelope independently of its parent records."""
    validate_json_value(checkpoint)
    _fields(checkpoint, _CHECKPOINT_FIELDS)
    _check(type(checkpoint["schema_version"]) is int and checkpoint["schema_version"] == 1,
           "Checkpoint schema version is invalid")
    _check(all(uuid_value(checkpoint[field]) for field in (
        "checkpoint_id", "workflow_definition_id", "workflow_session_id")),
        "Checkpoint owner or identity is invalid")
    _check(_version(checkpoint["fixed_definition_revision"]), "Fixed definition revision is invalid")
    storage = validate_state_storage(checkpoint["state_storage"])
    association_fields = ("parent_checkpoint_id", "round_id", "chain_run_id", "assistant_entry_id")
    if checkpoint["kind"] == "initial":
        _check(storage["mode"] == "full"
               and all(checkpoint[field] is None for field in association_fields),
               "Initial checkpoint requires full state and no round associations")
    else:
        _check(checkpoint["kind"] == "successful_round" and storage["mode"] == "diff"
               and all(uuid_value(checkpoint[field]) for field in association_fields),
               "Successful checkpoint requires a diff and exact round associations")
    return deepcopy(checkpoint)


def _partitions(state: dict) -> dict:
    return {
        "node_states": state["node_states"],
        "program_values": state["program_state"]["values"],
        "program_data": state["program_state"]["data"],
        "objects": state["objects"],
    }


def make_diff(parent_state: Any, state: Any) -> dict:
    """Return upsert/delete partitions; the caller supplies the full baseline ID."""
    parent, current = validate_runtime_state(parent_state), validate_runtime_state(state)
    before, after = _partitions(parent), _partitions(current)
    changes = {"upsert": {}, "delete": {}}
    for partition in _PARTITIONS:
        changes["upsert"][partition] = {
            key: deepcopy(after[partition][key]) for key in sorted(after[partition])
            if key not in before[partition]
            or canonical_bytes(before[partition][key]) != canonical_bytes(after[partition][key])
        }
        changes["delete"][partition] = sorted(before[partition].keys() - after[partition].keys())
    if canonical_bytes(parent["history"]) != canonical_bytes(current["history"]):
        changes["upsert"]["history"] = deepcopy(current["history"])
    return changes


def materialize(checkpoint_id: str, load_checkpoint: Callable[[str], Any]) -> dict:
    """Apply only the selected parent chain from its session-owned full root.

    The loader supplies exact immutable checkpoint records or None for a missing
    identity. No repository, registry, permission or artifact lookup is performed.
    """
    _check(uuid_value(checkpoint_id), "Checkpoint target must be a canonical UUID4")
    path, visited, identity, owner = [], set(), checkpoint_id, None
    while identity is not None:
        require(identity not in visited, "runtime_checkpoint_lineage_invalid",
                "Checkpoint parent chain is cyclic")
        visited.add(identity)
        loaded = load_checkpoint(identity)
        require(loaded is not None, "runtime_checkpoint_not_found", "Checkpoint parent is missing", 404)
        checkpoint = validate_checkpoint(loaded)
        require(checkpoint["checkpoint_id"] == identity, "runtime_checkpoint_lineage_invalid",
                "Checkpoint loader returned another identity")
        current_owner = tuple(checkpoint[field] for field in (
            "workflow_definition_id", "workflow_session_id", "fixed_definition_revision"))
        owner = current_owner if owner is None else owner
        require(current_owner == owner, "runtime_checkpoint_lineage_invalid",
                "Checkpoint parent has another owner or fixed definition")
        path.append(checkpoint)
        identity = checkpoint["parent_checkpoint_id"]
    root = path[-1]
    require(root["kind"] == "initial", "runtime_checkpoint_lineage_invalid",
            "Checkpoint chain must end at an initial full baseline")
    state = deepcopy(root["state_storage"]["state"])
    for checkpoint in reversed(path[:-1]):
        storage = checkpoint["state_storage"]
        require(storage["base_checkpoint_id"] == root["checkpoint_id"],
                "runtime_checkpoint_lineage_invalid", "Diff baseline is outside its parent branch")
        partitions = _partitions(state)
        for partition in _PARTITIONS:
            for key in storage["delete"][partition]:
                require(key in partitions[partition], "runtime_checkpoint_lineage_invalid",
                        "Diff deletes a key absent from its parent state")
                del partitions[partition][key]
            partitions[partition].update(deepcopy(storage["upsert"][partition]))
        if "history" in storage["upsert"]:
            state["history"] = deepcopy(storage["upsert"]["history"])
        state = validate_runtime_state(state)
    return state
