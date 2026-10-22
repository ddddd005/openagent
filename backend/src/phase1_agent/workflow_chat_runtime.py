"""Transactional side records for frozen chat inputs and actual invocations."""

from copy import deepcopy

from .chat_contracts import CHAT_CONTEXT_SCHEMA_VERSION, CHAT_CONTEXT_TYPE, validate_chat_context
from .context_contract import validate_agent_checkpoint_value
from .contract_errors import require, uuid_value
from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .host_sdk import bounded_name
from .variable_contract import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE, validate_variable
from .runtime_checkpoints import validate_preloaded_items


def initialize_chat_tables(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workflow_chat_round_inputs ("
        "sid TEXT NOT NULL,object_key TEXT NOT NULL,round_id TEXT NOT NULL,payload TEXT NOT NULL,"
        "PRIMARY KEY(sid,object_key,round_id))")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workflow_chat_invocations ("
        "sid TEXT NOT NULL,chain_id TEXT NOT NULL,object_key TEXT NOT NULL,payload TEXT NOT NULL,"
        "PRIMARY KEY(sid,chain_id,object_key))")


def _transaction(repo):
    require(repo.store._connection.in_transaction, "chat_transaction_required",
            "Chat side records require the caller's transaction", 500)
    return repo.store._connection


def _owner(sid, object_key=None):
    require(uuid_value(sid) and (object_key is None or bounded_name(object_key)),
            "chat_runtime_invalid", "Chat records require an exact session and item")


def _initial_state(value):
    if value is None:
        return None
    require(type(value) is dict and {"agent_contexts", "variables"} <= set(value)
            <= {"agent_contexts", "variables", "preloaded_items"}
            and type(value["agent_contexts"]) is dict and type(value["variables"]) is dict
            and not value["agent_contexts"].keys() & value["variables"].keys(),
            "chat_runtime_invalid", "Initial computation values require exact admitted sections")
    if "preloaded_items" in value:
        validate_preloaded_items(value["preloaded_items"])
        require(not (value["agent_contexts"].keys() | value["variables"].keys())
                & value["preloaded_items"].keys(),
                "chat_runtime_invalid", "Initial computation object keys must be disjoint")
    for key, entry in value["agent_contexts"].items():
        require(bounded_name(key) and type(entry) is dict and set(entry) == {"value"},
                "chat_runtime_invalid", "Initial context item is invalid")
        validate_agent_checkpoint_value(entry["value"])
    for key, entry in value["variables"].items():
        require(bounded_name(key) and type(entry) is dict
                and set(entry) == {"type_id", "schema_version", "value"}
                and entry["type_id"] == VARIABLE_TYPE
                and type(entry["schema_version"]) is int
                and entry["schema_version"] == VARIABLE_SCHEMA_VERSION,
                "chat_runtime_invalid", "Initial variable item requires its exact registered type")
        validate_variable(entry["value"])
    return deepcopy(value)


def _round_payload(value):
    validate_json_value(value)
    require(type(value) is dict
            and set(value) == {"inputs", "input_overrides", "initial_compute_state"}
            and type(value["inputs"]) is dict and type(value["input_overrides"]) is dict,
            "chat_runtime_invalid", "Frozen round input fields differ")
    require("_workflow_frozen_resources" not in value["inputs"],
            "chat_runtime_invalid", "Frozen inputs cannot supply reserved runtime resources")
    require(all(uuid_value(key) and type(text) is str
                for key, text in value["input_overrides"].items()),
            "chat_runtime_invalid", "Input overrides require exact node identities and text")
    _initial_state(value["initial_compute_state"])
    return deepcopy(value)


def _invocation(value):
    validate_json_value(value)
    require(type(value) is dict and set(value) == {
        "round_id", "base_checkpoint_id", "submitted_input", "candidate_id",
    } and uuid_value(value["round_id"]) and uuid_value(value["candidate_id"])
        and (value["base_checkpoint_id"] is None or uuid_value(value["base_checkpoint_id"]))
        and (value["submitted_input"] is None or type(value["submitted_input"]) is str),
        "chat_runtime_invalid", "Chat invocation requires exact frozen round identities")
    return deepcopy(value)


def _insert_frozen(connection, table, identity, payload):
    columns = tuple(identity)
    clause = " AND ".join(column + "=?" for column in columns)
    prior = connection.execute(
        "SELECT payload FROM " + table + " WHERE " + clause, tuple(identity.values())).fetchone()
    encoded = canonical_bytes(payload).decode("utf-8")
    if prior is not None:
        require(canonical_bytes(loads_strict(prior["payload"])) == canonical_bytes(payload),
                "chat_runtime_conflict", "The accepted chat record has a different frozen value", 409)
        return
    connection.execute(
        "INSERT INTO " + table + "(" + ",".join((*columns, "payload")) + ") VALUES("
        + ",".join("?" for _ in range(len(columns) + 1)) + ")",
        (*identity.values(), encoded))


def save_chat_round_inputs(
    repo, sid, object_key, round_id, *, inputs=None, input_overrides=None, initial_compute_state=None,
):
    connection = _transaction(repo)
    _owner(sid, object_key)
    require(uuid_value(round_id), "chat_runtime_invalid", "A frozen round requires an exact identity")
    payload = _round_payload({
        "inputs": {} if inputs is None else deepcopy(inputs),
        "input_overrides": {} if input_overrides is None else deepcopy(input_overrides),
        "initial_compute_state": deepcopy(initial_compute_state),
    })
    _insert_frozen(connection, "workflow_chat_round_inputs",
                   {"sid": sid, "object_key": object_key, "round_id": round_id}, payload)
    return payload


def read_chat_round_inputs(repo, sid, object_key, round_id):
    connection = _transaction(repo)
    _owner(sid, object_key)
    require(uuid_value(round_id), "chat_runtime_invalid", "A frozen round requires an exact identity")
    row = connection.execute(
        "SELECT payload FROM workflow_chat_round_inputs WHERE sid=? AND object_key=? AND round_id=?",
        (sid, object_key, round_id)).fetchone()
    return None if row is None else _round_payload(loads_strict(row["payload"]))


def save_chat_invocations(repo, sid, chain_id, invocations):
    connection = _transaction(repo)
    _owner(sid)
    require(uuid_value(chain_id) and type(invocations) is dict,
            "chat_runtime_invalid", "An invocation set requires an exact execution")
    checked = {}
    for key, value in invocations.items():
        _owner(sid, key)
        checked[key] = _invocation(value)
    for key, value in checked.items():
        _insert_frozen(connection, "workflow_chat_invocations",
                       {"sid": sid, "chain_id": chain_id, "object_key": key}, value)
    return deepcopy(checked)


def read_chat_invocations(repo, sid, chain_id):
    connection = _transaction(repo)
    _owner(sid)
    require(uuid_value(chain_id), "chat_runtime_invalid", "An exact execution is required")
    rows = connection.execute(
        "SELECT object_key,payload FROM workflow_chat_invocations WHERE sid=? AND chain_id=? ORDER BY object_key",
        (sid, chain_id)).fetchall()
    result = {}
    for row in rows:
        _owner(sid, row["object_key"])
        result[row["object_key"]] = _invocation(loads_strict(row["payload"]))
    return result


def copy_chat_round_inputs(repo, source_sid, target_sid, chat_contexts):
    _transaction(repo)
    _owner(source_sid)
    _owner(target_sid)
    require(type(chat_contexts) is dict, "chat_runtime_invalid",
            "Copied chat inputs require exact registered chat items")
    prepared = []
    for key, entry in chat_contexts.items():
        _owner(source_sid, key)
        require(type(entry) is dict and set(entry) == {"type_id", "schema_version", "value"}
                and entry["type_id"] == CHAT_CONTEXT_TYPE
                and type(entry["schema_version"]) is int
                and entry["schema_version"] == CHAT_CONTEXT_SCHEMA_VERSION,
                "chat_runtime_invalid", "Copied chat item has another exact type")
        for round_value in validate_chat_context(entry["value"])["rounds"]:
            payload = read_chat_round_inputs(repo, source_sid, key, round_value["round_id"])
            require(payload is not None or round_value["submitted_input"] is None,
                    "chat_round_inputs_unavailable",
                    "A submitted chat round has no accepted frozen inputs", 409)
            if payload is not None:
                prepared.append((key, round_value["round_id"], payload))
    for key, round_id, payload in prepared:
        save_chat_round_inputs(repo, target_sid, key, round_id, **payload)


def delete_chat_session_records(repo, sid):
    connection = _transaction(repo)
    _owner(sid)
    connection.execute("DELETE FROM workflow_chat_round_inputs WHERE sid=?", (sid,))
    connection.execute("DELETE FROM workflow_chat_invocations WHERE sid=?", (sid,))
