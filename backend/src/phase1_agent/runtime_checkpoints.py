"""Logical checkpoint state validation, diffs, and parent-chain materialization."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from .context_contract import validate_agent_checkpoint_value
from .contract_errors import require, uuid_value
from .contract_json import canonical_bytes, validate_json_value


_CHECKPOINT_FIELDS = {
    "schema_version", "checkpoint_id", "workflow_definition_id", "workflow_session_id",
    "fixed_definition_revision", "parent_checkpoint_id", "state_storage",
}
_MAX_INTEGER = 2**53 - 1


def _fields(value: Any, required: set[str], *, optional: set[str] | None = None) -> None:
    require(type(value) is dict and required <= set(value)
            and set(value) <= required | (optional or set()),
            "runtime_checkpoint_invalid", "Checkpoint fields are invalid")


def _revision(value: Any) -> bool:
    return type(value) is int and 1 <= value <= _MAX_INTEGER


def _name(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 128 and value == value.strip()


def _validate_configuration(configuration: Any) -> dict:
    _fields(configuration, {"workflow_definition_id", "fixed_definition_revision", "nodes"})
    require(uuid_value(configuration["workflow_definition_id"])
            and _revision(configuration["fixed_definition_revision"])
            and type(configuration["nodes"]) is dict,
            "runtime_checkpoint_invalid", "Configuration identity or nodes are invalid")
    for binding_id, entry in configuration["nodes"].items():
        require(uuid_value(binding_id), "runtime_checkpoint_invalid",
                "Configuration node identity is invalid")
        _fields(entry, {"component_id", "component_version", "config"})
        require(_name(entry["component_id"]) and _name(entry["component_version"])
                and type(entry["config"]) is dict,
                "runtime_checkpoint_invalid", "Configuration node descriptor is invalid")
    return configuration


def _validate_agent_contexts(agent_contexts: Any) -> dict:
    require(type(agent_contexts) is dict, "runtime_checkpoint_invalid",
            "Agent contexts must be a mapping")
    for object_key, entry in agent_contexts.items():
        require(_name(object_key), "runtime_checkpoint_invalid", "Agent object key is invalid")
        _fields(entry, {"value"})
        validate_agent_checkpoint_value(entry["value"])
    return agent_contexts


def validate_checkpoint_state(state: Any) -> dict:
    """Return the exact detached schema-2 logical state."""
    validate_json_value(state)
    _fields(state, {"configuration", "agent_contexts"})
    configuration = _validate_configuration(state["configuration"])
    _validate_agent_contexts(state["agent_contexts"])
    return deepcopy(state)


def _validate_changes(changes: Any) -> dict:
    _fields(changes, {"upsert", "delete"})
    _fields(changes["upsert"], {"agent_contexts"}, optional={"configuration"})
    _fields(changes["delete"], {"agent_contexts"})
    upsert, deleted = changes["upsert"], changes["delete"]
    if "configuration" in upsert:
        _validate_configuration(upsert["configuration"])
    _validate_agent_contexts(upsert["agent_contexts"])
    keys = deleted["agent_contexts"]
    require(type(keys) is list and all(_name(key) for key in keys)
            and keys == sorted(set(keys))
            and not set(keys) & upsert["agent_contexts"].keys(),
            "runtime_checkpoint_invalid", "Agent context deletes are invalid")
    return changes


def validate_state_storage(storage: Any) -> dict:
    """Validate exact full or diff storage for the checkpoint's logical state."""
    validate_json_value(storage)
    require(type(storage) is dict, "runtime_checkpoint_invalid",
            "Checkpoint storage must be an object")
    if storage.get("mode") == "full":
        _fields(storage, {"mode", "state"})
        validate_checkpoint_state(storage["state"])
    else:
        _fields(storage, {"mode", "base_checkpoint_id", "upsert", "delete"})
        require(storage["mode"] == "diff" and uuid_value(storage["base_checkpoint_id"]),
                "runtime_checkpoint_invalid", "Diff mode or full baseline identity is invalid")
        _validate_changes({"upsert": storage["upsert"], "delete": storage["delete"]})
    return deepcopy(storage)


def _require_configuration_owner(configuration: dict, checkpoint: dict) -> None:
    require((configuration["workflow_definition_id"], configuration["fixed_definition_revision"]) == (
        checkpoint["workflow_definition_id"], checkpoint["fixed_definition_revision"]),
        "runtime_checkpoint_invalid", "Configuration identity differs from checkpoint owner")


def validate_checkpoint(checkpoint: Any) -> dict:
    """Validate one immutable record without requiring execution or message identities."""
    validate_json_value(checkpoint)
    _fields(checkpoint, _CHECKPOINT_FIELDS)
    require(type(checkpoint["schema_version"]) is int and checkpoint["schema_version"] == 2
            and all(uuid_value(checkpoint[field]) for field in (
                "checkpoint_id", "workflow_definition_id", "workflow_session_id"))
            and _revision(checkpoint["fixed_definition_revision"]),
            "runtime_checkpoint_invalid", "Checkpoint identity or schema version is invalid")
    parent_id = checkpoint["parent_checkpoint_id"]
    require(parent_id is None or uuid_value(parent_id), "runtime_checkpoint_invalid",
            "Checkpoint parent identity is invalid")
    storage = validate_state_storage(checkpoint["state_storage"])
    if parent_id is None:
        require(storage["mode"] == "full", "runtime_checkpoint_invalid",
                "A root checkpoint must contain full state")
        _require_configuration_owner(storage["state"]["configuration"], checkpoint)
    else:
        require(storage["mode"] == "diff", "runtime_checkpoint_invalid",
                "A child checkpoint must contain a diff")
        if "configuration" in storage["upsert"]:
            _require_configuration_owner(storage["upsert"]["configuration"], checkpoint)
    return deepcopy(checkpoint)


def make_diff(parent_state: Any, state: Any) -> dict:
    """Return canonical configuration replacement and Agent-context key changes."""
    parent = validate_checkpoint_state(parent_state)
    current = validate_checkpoint_state(state)
    upsert = {
        "agent_contexts": {
            key: deepcopy(current["agent_contexts"][key])
            for key in sorted(current["agent_contexts"])
            if key not in parent["agent_contexts"]
            or canonical_bytes(parent["agent_contexts"][key])
            != canonical_bytes(current["agent_contexts"][key])
        },
    }
    if canonical_bytes(parent["configuration"]) != canonical_bytes(current["configuration"]):
        upsert["configuration"] = deepcopy(current["configuration"])
    return {
        "upsert": upsert,
        "delete": {
            "agent_contexts": sorted(parent["agent_contexts"].keys() - current["agent_contexts"].keys()),
        },
    }


def materialize(checkpoint_id: str, load_checkpoint: Callable[[str], Any]) -> dict:
    """Materialize only the requested same-owner parent path from its full root."""
    require(uuid_value(checkpoint_id), "runtime_checkpoint_invalid",
            "Checkpoint target must be a canonical UUID4")
    path, visited, identity, owner = [], set(), checkpoint_id, None
    while identity is not None:
        require(identity not in visited, "runtime_checkpoint_lineage_invalid",
                "Checkpoint parent chain is cyclic")
        visited.add(identity)
        loaded = load_checkpoint(identity)
        require(loaded is not None, "runtime_checkpoint_not_found",
                "Checkpoint parent is missing", 404)
        checkpoint = validate_checkpoint(loaded)
        require(checkpoint["checkpoint_id"] == identity,
                "runtime_checkpoint_lineage_invalid", "Checkpoint loader returned another identity")
        current_owner = tuple(checkpoint[field] for field in (
            "workflow_definition_id", "workflow_session_id", "fixed_definition_revision"))
        owner = current_owner if owner is None else owner
        require(current_owner == owner, "runtime_checkpoint_lineage_invalid",
                "Checkpoint parent has another owner or fixed definition")
        path.append(checkpoint)
        identity = checkpoint["parent_checkpoint_id"]

    root = path[-1]
    require(root["state_storage"]["mode"] == "full",
            "runtime_checkpoint_lineage_invalid", "Checkpoint chain must end at full state")
    state = deepcopy(root["state_storage"]["state"])
    for checkpoint in reversed(path[:-1]):
        storage = checkpoint["state_storage"]
        require(storage["base_checkpoint_id"] == root["checkpoint_id"],
                "runtime_checkpoint_lineage_invalid",
                "Diff baseline is outside its parent chain")
        if "configuration" in storage["upsert"]:
            state["configuration"] = deepcopy(storage["upsert"]["configuration"])
        contexts = state["agent_contexts"]
        for object_key in storage["delete"]["agent_contexts"]:
            require(object_key in contexts, "runtime_checkpoint_lineage_invalid",
                    "Diff deletes an Agent context absent from its parent state")
            del contexts[object_key]
        contexts.update(deepcopy(storage["upsert"]["agent_contexts"]))
        state = validate_checkpoint_state(state)
    return state
