"""Load checkpoint-owned state into a freshly initialized fork target."""

from __future__ import annotations

from .checkpoint_collection import (
    checkpoint_chat_bindings, checkpoint_context_bindings, checkpoint_variable_bindings,
    checkpoint_preloaded_bindings,
)
from .contract_json import canonical_bytes
from .graph_contracts import NodeRegistry
from .graph_records import require, uuid_value
from .host_sdk import TypeRegistry
from .runtime_checkpoints import validate_checkpoint_state
from .session_objects import SessionObjectStore
from .workflow_configuration import fixed_configuration_projection, validate_node_configuration


_FRESH_TARGET_RECORDS = (
    "state_snapshot", "workflow_commit", "workflow_ref",
    "chain_run", "node_run", "workflow_output",
)


def _require_transaction(repo) -> None:
    require(repo.store._connection.in_transaction, "checkpoint_transaction_required",
            "Checkpoint loading requires the caller's graph transaction", 500)


def _require_fresh_target(repo, target_session: dict) -> dict:
    require(type(target_session) is dict and set(target_session) == {
        "schema_version", "execution_model", "workflow_session_id",
        "workflow_definition_id", "definition_revision", "revision", "source",
        "active_chain_run_id",
    } and uuid_value(target_session["workflow_session_id"])
        and uuid_value(target_session["workflow_definition_id"])
        and type(target_session["definition_revision"]) is int
        and target_session["definition_revision"] >= 1
        and type(target_session["revision"]) is int
        and target_session["revision"] == 1
        and target_session["source"] == {"kind": "new"}
        and target_session["active_chain_run_id"] is None,
        "checkpoint_target_invalid", "Checkpoint target must be a fresh initialized session", 409)
    sid = target_session["workflow_session_id"]
    persisted = repo.get("workflow_session", workflow_session_id=sid)
    require(repo.equal(persisted, target_session), "checkpoint_target_invalid",
            "Checkpoint target must be the exact persisted initialization result", 409)
    for record_type in _FRESH_TARGET_RECORDS:
        require(not repo.rows(record_type, workflow_session_id=sid),
                "checkpoint_target_not_fresh",
                "Checkpoint target already has committed or execution state", 409)

    connection = repo.store._connection
    require(connection.execute(
        "SELECT 1 FROM session_object_receipts WHERE session_id=? LIMIT 1", (sid,),
    ).fetchone() is None,
        "checkpoint_target_not_fresh",
        "Checkpoint target already has session-object write receipts", 409)
    require(connection.execute(
        "SELECT 1 FROM graph_state_manifests "
        "WHERE json_extract(payload,'$.workflow_session_id')=? LIMIT 1", (sid,),
    ).fetchone() is None,
        "checkpoint_target_not_fresh",
        "Checkpoint target already has a saved session-state manifest", 409)
    for table in ("program_variable_heads", "program_variable_states", "program_variable_bindings"):
        require(connection.execute(
            f"SELECT 1 FROM {table} WHERE session_id=? LIMIT 1", (sid,),
        ).fetchone() is None,
            "checkpoint_target_not_fresh",
            "Checkpoint target already has program-variable state", 409)
    require(connection.execute(
        "SELECT 1 FROM program_variable_receipts "
        "WHERE json_extract(payload,'$.session_id')=? LIMIT 1", (sid,),
    ).fetchone() is None,
        "checkpoint_target_not_fresh",
        "Checkpoint target already has program-variable write receipts", 409)
    revisions = connection.execute(
        "SELECT revision FROM session_object_bindings WHERE session_id=?", (sid,),
    ).fetchall()
    require(all(row["revision"] == 1 for row in revisions),
            "checkpoint_target_not_fresh",
            "Checkpoint target session objects must remain at their initialized revisions", 409)
    heads = {
        row["object_key"]: row["revision_id"]
        for row in connection.execute(
            "SELECT object_key,revision_id FROM session_object_bindings WHERE session_id=?", (sid,),
        ).fetchall()
    }
    memberships = {
        (row["object_key"], row["revision_id"])
        for row in connection.execute(
            "SELECT object_key,revision_id FROM session_object_revision_membership WHERE session_id=?",
            (sid,),
        ).fetchall()
    }
    require(memberships == set(heads.items()),
            "checkpoint_target_not_fresh",
            "Checkpoint target objects must have only their initial revision membership", 409)
    return persisted


def _fixed_document(repo, session: dict) -> dict:
    record = repo.get(
        "workflow_definition_revision",
        workflow_definition_id=session["workflow_definition_id"],
        revision=session["definition_revision"],
    )
    require((record["workflow_definition_id"], record["revision"]) == (
        session["workflow_definition_id"], session["definition_revision"]),
        "checkpoint_definition_mismatch", "Target fixed definition identity differs", 500)
    return record["document"]


def _is_recursive_projection(projected, source) -> bool:
    if type(projected) is dict:
        return type(source) is dict and all(
            key in source and _is_recursive_projection(value, source[key])
            for key, value in projected.items()
        )
    return canonical_bytes(projected) == canonical_bytes(source)


def validate_checkpoint_configuration_target(configuration, document, *, registry: NodeRegistry) -> None:
    """Validate frozen checkpoint values against their fixed instance, not today's save paths."""
    require(isinstance(registry, NodeRegistry), "node_definition_management_unavailable",
            "Trusted node definition management is unavailable", 409)
    require(type(configuration) is dict and set(configuration) == {
        "workflow_definition_id", "fixed_definition_revision", "nodes",
    } and type(document) is dict,
        "runtime_checkpoint_invalid", "Checkpoint configuration or target definition is invalid", 409)
    require((configuration["workflow_definition_id"], configuration["fixed_definition_revision"])
            == (document.get("workflow_definition_id"), document.get("revision")),
            "checkpoint_target_definition_mismatch",
            "Checkpoint configuration and target must use the same fixed definition", 409)

    nodes = {node["node_binding_id"]: node for node in document["nodes"]}
    entries = configuration["nodes"]
    require(type(entries) is dict and set(entries) <= set(nodes),
            "checkpoint_configuration_mismatch",
            "Checkpoint configuration contains a node outside its fixed definition", 409)
    for node_id, entry in entries.items():
        node = nodes[node_id]
        require(type(entry) is dict and set(entry) == {"component_id", "component_version", "config"}
                and (entry["component_id"], entry["component_version"])
                    == (node["component_id"], node["component_version"])
                and type(entry["config"]) is dict,
                "checkpoint_configuration_mismatch",
                "Checkpoint configuration node identity differs from its fixed definition", 409,
                node_id=node_id)
        definition = registry.get_definition(node["component_id"], node["component_version"])
        require(definition is not None, "graph_missing_node_type",
                "Node declaration is unavailable", 409, node_id=node_id)
        require((definition.component_id, definition.component_version)
                == (node["component_id"], node["component_version"]),
                "graph_missing_node_type", "Node declaration has another exact identity", 409,
                node_id=node_id)
        descriptor = definition.to_dict()
        policy = node.get("configuration_policy")
        declaration = None if policy is None else validate_node_configuration(node, descriptor)
        fixed = fixed_configuration_projection(node["config"], declaration, policy)
        require(_is_recursive_projection(entry["config"], fixed),
                "checkpoint_configuration_mismatch",
                "Checkpoint values differ from the fixed instance configuration", 409,
                node_id=node_id)


def load_checkpoint_state_in_transaction(
        repo, target_session, logical_state, target_current_cas, *,
        types: TypeRegistry, registry: NodeRegistry):
    """Initialize registered inline checkpoint values into a fresh fork target.

    The caller owns the transaction and is responsible for committing the
    complete fork or rolling it back if any target write fails.
    """
    _require_transaction(repo)
    require(isinstance(types, TypeRegistry), "checkpoint_types_invalid",
            "Checkpoint loading requires the target session type registry", 500)
    session = _require_fresh_target(repo, target_session)
    state = validate_checkpoint_state(logical_state)
    owner = (
        session["workflow_definition_id"],
        session["workflow_session_id"],
        session["definition_revision"],
    )
    require((state["configuration"]["workflow_definition_id"],
             state["configuration"]["fixed_definition_revision"])
            == (owner[0], owner[2]),
            "checkpoint_target_definition_mismatch",
            "Checkpoint state and target must use the same fixed definition", 409)
    document = _fixed_document(repo, session)
    validate_checkpoint_configuration_target(state["configuration"], document, registry=registry)

    agent_contexts = state["agent_contexts"]
    target_bindings = checkpoint_context_bindings(document, types=types, registry=registry)
    require(set(agent_contexts) <= target_bindings.keys(),
            "checkpoint_target_context_binding_mismatch",
            "Checkpoint state names an item outside the registered whitelist", 409)
    variables = state["variables"]
    variable_bindings = checkpoint_variable_bindings(document, types=types, registry=registry)
    require(set(variables) == variable_bindings.keys(),
            "checkpoint_target_variable_binding_mismatch",
            "Checkpoint variables must match all admitted fixed target items", 409)
    for key, entry in variables.items():
        binding = variable_bindings[key]
        require((entry["type_id"], entry["schema_version"])
                == (binding.type_id, binding.schema_version),
                "checkpoint_target_variable_binding_mismatch",
                "Checkpoint variable has another exact target type", 409, object_key=key)
    chat_contexts = state["chat_contexts"]
    chat_bindings = checkpoint_chat_bindings(document, types=types, registry=registry)
    require(set(chat_contexts) == chat_bindings.keys(),
            "checkpoint_target_chat_binding_mismatch",
            "Checkpoint chats must match all admitted fixed target items", 409)
    for key, entry in chat_contexts.items():
        binding = chat_bindings[key]
        require((entry["type_id"], entry["schema_version"])
                == (binding.type_id, binding.schema_version),
                "checkpoint_target_chat_binding_mismatch",
                "Checkpoint chat has another exact target type", 409, object_key=key)
    preloaded_items = state.get("preloaded_items", {})
    preload_bindings = checkpoint_preloaded_bindings(document, types=types, registry=registry)
    require(set(preloaded_items) == preload_bindings.keys(),
            "checkpoint_target_preload_binding_mismatch",
            "Checkpoint preloads must match all exact fixed target items", 409)
    for key, entry in preloaded_items.items():
        binding = preload_bindings[key]
        require((entry["type_id"], entry["schema_version"]) == (binding.type_id, binding.schema_version),
                "checkpoint_target_preload_binding_mismatch",
                "Checkpoint preload has another exact target type", 409, object_key=key)
    values = {
        key: entry["value"]
        for section in (agent_contexts, variables, chat_contexts, preloaded_items)
        for key, entry in section.items()
    }
    require(type(target_current_cas) is dict
            and set(target_current_cas) == set(values)
            and all(type(head) is dict and set(head) == {"revision", "revision_id"}
                    and type(head["revision"]) is int and head["revision"] == 1
                    and uuid_value(head["revision_id"])
                    for head in target_current_cas.values()),
            "checkpoint_target_cas_invalid",
            "Target CAS map must contain the exact initialized checkpoint heads", 409)
    if not values:
        return {}

    objects = SessionObjectStore(repo.store, types)
    return objects._initialize_checkpoint_values(
        owner[1], document,
        values,
        target_current_cas,
    )
