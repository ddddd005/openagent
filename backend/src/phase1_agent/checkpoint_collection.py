"""Collect fixed configuration and explicitly admitted session item values."""

from __future__ import annotations

from copy import deepcopy

from .chat_contracts import CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION
from .context_contract import EFFECTIVE_CONTEXT_TYPE, validate_agent_checkpoint_value
from .graph_contracts import NodeRegistry, require
from .host_sdk import ObjectBinding, TypeRegistry
from .session_objects import SessionObjectStore
from .variable_contract import VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION
from .workflow_configuration import fixed_configuration_projection, validate_node_configuration


_EMPTY_AGENT_CONTEXT = {
    "messages": [],
    "layout": [],
    "accepted_delta_ids": [],
    "generation": 0,
}


def _require_transaction(repo) -> None:
    require(repo.store._connection.in_transaction, "storage_contract_violation",
            "Checkpoint collection requires the caller's graph transaction", 500)


def _fixed_definition(repo, workflow_definition_id, fixed_definition_revision):
    definition = repo.get(
        "workflow_definition_revision",
        workflow_definition_id=workflow_definition_id,
        revision=fixed_definition_revision,
    )
    return definition, definition["document"]


def _path_projection(config, paths):
    projected = {}
    for path in paths:
        value = config
        for segment in path:
            if type(value) is not dict or segment not in value:
                break
            value = value[segment]
        else:
            target = projected
            for segment in path[:-1]:
                target = target.setdefault(segment, {})
            target[path[-1]] = deepcopy(value)
    return projected


def collect_checkpoint_configuration(
        repo, workflow_definition_id, fixed_definition_revision, *, registry: NodeRegistry):
    """Collect fixed node configuration using the current trusted declarations."""
    _require_transaction(repo)
    definition, document = _fixed_definition(repo, workflow_definition_id, fixed_definition_revision)
    require(definition["workflow_definition_id"] == workflow_definition_id
            and definition["revision"] == fixed_definition_revision,
            "checkpoint_definition_mismatch", "Fixed checkpoint definition identity differs", 500)
    require(isinstance(registry, NodeRegistry), "node_definition_management_unavailable",
            "Trusted node definition management is unavailable", 409)

    nodes = {}
    for node in document["nodes"]:
        node_id = node["node_binding_id"]
        node_definition = registry.get_definition(node["component_id"], node["component_version"])
        require(node_definition is not None, "graph_missing_node_type",
                "Node declaration is unavailable", 409, node_id=node_id)
        require((node_definition.component_id, node_definition.component_version)
                == (node["component_id"], node["component_version"]),
                "graph_missing_node_type", "Node declaration has another exact identity", 409,
                node_id=node_id)
        descriptor = node_definition.to_dict()

        policy = node.get("configuration_policy")
        declaration = None if policy is None else validate_node_configuration(node, descriptor)
        fixed = fixed_configuration_projection(node["config"], declaration, policy)
        config = _path_projection(fixed, node_definition.checkpoint_save["config_paths"])
        if config:
            nodes[node_id] = {
                "component_id": node["component_id"],
                "component_version": node["component_version"],
                "config": config,
            }

    return {
        "workflow_definition_id": workflow_definition_id,
        "fixed_definition_revision": fixed_definition_revision,
        "nodes": nodes,
    }


def _checkpoint_bindings(document, exact_type, *, types: TypeRegistry, registry: NodeRegistry):
    """Resolve admitted exact items without borrowing ordinary consumer permissions."""
    require(type(document) is dict and isinstance(types, TypeRegistry)
            and isinstance(registry, NodeRegistry),
            "checkpoint_context_registration_invalid",
            "Checkpoint context selection requires the fixed document and trusted registries", 409)
    nodes = {node["node_binding_id"]: node for node in document.get("nodes", [])}
    bindings, registered_instances = {}, set()
    for raw in document.get("object_bindings", []):
        binding = ObjectBinding.from_dict(raw)
        if (binding.type_id, binding.schema_version) != exact_type:
            continue
        definition_type = types.get(*exact_type, scope="session")
        require(definition_type is not None,
                "checkpoint_context_type_unavailable",
                "The exact checkpoint session type is unavailable", 409)
        if not definition_type.checkpoint_admission:
            continue
        registrar_id = binding.registrar_node_id
        registrar = nodes.get(registrar_id)
        require(registrar is not None,
                "checkpoint_context_registration_invalid",
                "Checkpoint context item requires its exact registered node", 409,
                object_key=binding.object_key)
        definition = registry.get_definition(registrar["component_id"], registrar["component_version"])
        require(definition is not None
                and (definition.component_id, definition.component_version)
                == (registrar["component_id"], registrar["component_version"]),
                "graph_missing_node_type", "Checkpoint item registrar declaration is unavailable", 409,
                node_id=registrar_id)
        provided_types = {
            (item["type_id"], item["schema_version"])
            for item in definition.provided_session_item_types
        }
        require((binding.type_id, binding.schema_version) in provided_types,
                "checkpoint_context_registration_invalid",
                "Checkpoint item registrar does not provide its exact type", 409,
                object_key=binding.object_key, node_id=registrar_id)
        registrar_identity = (registrar_id, binding.type_id, binding.schema_version)
        require(registrar_identity not in registered_instances,
                "checkpoint_context_registration_invalid",
                "A registrar instance cannot register the same exact item type twice", 409,
                object_key=binding.object_key, node_id=registrar_id)
        registered_instances.add(registrar_identity)
        require(binding.object_key not in bindings,
                "checkpoint_context_registration_invalid",
                "Checkpoint context item keys must be unique", 409,
                object_key=binding.object_key)
        bindings[binding.object_key] = binding
    return bindings


def checkpoint_context_bindings(document, *, types: TypeRegistry, registry: NodeRegistry):
    """Resolve explicitly admitted effective-context items from the fixed graph."""
    return _checkpoint_bindings(
        document, (EFFECTIVE_CONTEXT_TYPE, 4), types=types, registry=registry)


def checkpoint_variable_bindings(document, *, types: TypeRegistry, registry: NodeRegistry):
    """Resolve explicitly admitted independent variable items from the fixed graph."""
    return _checkpoint_bindings(
        document, (VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION), types=types, registry=registry)


def checkpoint_chat_bindings(document, *, types: TypeRegistry, registry: NodeRegistry):
    """Resolve admitted logical chat items without assuming a particular consumer node."""
    return _checkpoint_bindings(
        document, (CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION), types=types, registry=registry)


def collect_agent_contexts_in_transaction(
        repo, source_session, document, *, types: TypeRegistry, registry: NodeRegistry):
    """Collect accepted checkpoint values through the trusted internal object view."""
    _require_transaction(repo)
    require(isinstance(types, TypeRegistry), "storage_contract_violation",
            "Agent context collection requires the active session type registry", 500)
    require(type(document) is dict and type(source_session) is str,
            "invalid_request", "Agent context collection requires a session id and fixed document")

    workflow_definition_id = document.get("workflow_definition_id")
    fixed_definition_revision = document.get("revision")
    definition, persisted_document = _fixed_definition(
        repo, workflow_definition_id, fixed_definition_revision)
    require(repo.equal(document, persisted_document),
            "checkpoint_definition_mismatch",
            "Agent collection document must be the exact persisted fixed revision", 500)

    session = repo.get("workflow_session", workflow_session_id=source_session)
    require(session["workflow_definition_id"] == workflow_definition_id
            and session["definition_revision"] == fixed_definition_revision
            and definition["workflow_definition_id"] == session["workflow_definition_id"],
            "checkpoint_source_definition_mismatch",
            "Source session must use the collected fixed definition revision", 409)

    object_bindings = checkpoint_context_bindings(document, types=types, registry=registry)
    objects = SessionObjectStore(repo.store, types)
    contexts = {}
    for object_key, object_binding in sorted(object_bindings.items()):
        record = objects._checkpoint_current_value(source_session, object_key, object_binding)
        if record is None or record["deleted"]:
            continue
        value = validate_agent_checkpoint_value(record["value"])
        if value == _EMPTY_AGENT_CONTEXT:
            continue
        contexts[object_key] = {"value": value}
    return contexts


def collect_checkpoint_state(repo, workflow_definition_id, fixed_definition_revision,
                             source_session, *, types: TypeRegistry, registry: NodeRegistry):
    """Compose fixed configuration and the complete explicitly admitted item states."""
    _require_transaction(repo)
    _, document = _fixed_definition(repo, workflow_definition_id, fixed_definition_revision)
    contexts = collect_agent_contexts_in_transaction(
        repo, source_session, document, types=types, registry=registry)
    objects = SessionObjectStore(repo.store, types)
    variables = {}
    for key, binding in sorted(checkpoint_variable_bindings(
            document, types=types, registry=registry).items()):
        record = objects._checkpoint_current_value(source_session, key, binding)
        require(record is not None and not record["deleted"],
                "checkpoint_variable_state_unavailable",
                "An admitted variable item requires its current logical state", 409, object_key=key)
        value = types.validate(
            binding.type_id, binding.schema_version, record["value"], scope="session")
        variables[key] = {
            "type_id": binding.type_id, "schema_version": binding.schema_version,
            "value": value,
        }
    chat_contexts = {}
    for key, binding in sorted(checkpoint_chat_bindings(
            document, types=types, registry=registry).items()):
        record = objects._checkpoint_current_value(source_session, key, binding)
        require(record is not None and not record["deleted"],
                "checkpoint_chat_state_unavailable",
                "An admitted chat item requires its current logical state", 409, object_key=key)
        value = types.validate(
            binding.type_id, binding.schema_version, record["value"], scope="session")
        chat_contexts[key] = {
            "type_id": binding.type_id, "schema_version": binding.schema_version, "value": value,
        }
    return {
        "configuration": collect_checkpoint_configuration(
            repo, workflow_definition_id, fixed_definition_revision, registry=registry),
        "agent_contexts": contexts,
        "variables": variables,
        "chat_contexts": chat_contexts,
    }
