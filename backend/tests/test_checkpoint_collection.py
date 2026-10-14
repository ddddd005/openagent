from contextlib import closing, contextmanager
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from phase1_agent.context_contract import (
    EFFECTIVE_CONTEXT_TYPE, project_agent_context_message, validate_effective_context,
)
from phase1_agent.context_v4 import validate_native_context_write
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry
from phase1_agent.graph_records import graph_record
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding, TypeRegistry
from phase1_agent.provider_metadata import GEMINI_PROTOCOL
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore
from phase1_agent.type_contract_store import TypeContractStore

from phase1_agent.checkpoint_collection import (
    collect_agent_contexts_in_transaction,
    collect_checkpoint_configuration,
    collect_checkpoint_state,
)
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.provider_metadata import remap_provider_metadata
from test_context_native_integration import NativeTransport, native_graph
from test_gemini_history import metadata
from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture


EMPTY_CONTEXT = {
    "messages": [],
    "layout": [],
    "accepted_delta_ids": [],
    "generation": 0,
}


@contextmanager
def transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _node(component_id, version, config, *, policy="fixed", index=0):
    return {
        "node_binding_id": str(uuid4()),
        "component_id": component_id,
        "component_version": version,
        "title": component_id,
        "position": {"x": index, "y": 0},
        "config": deepcopy(config),
        "configuration_policy": {"schema_version": 1, "prompt_mode": policy},
    }


def _document(workflow_id, revision, nodes, object_bindings=()):
    return {
        "schema_version": 3,
        "workflow_definition_id": workflow_id,
        "revision": revision,
        "name": "Collector test",
        "nodes": deepcopy(nodes),
        "edges": [],
        "object_bindings": [binding.to_dict() for binding in object_bindings],
    }


def _save_definition(store, document, descriptors):
    store._write("workflow_definition_revision", graph_record(
        "workflow_definition_revision",
        workflow_definition_id=document["workflow_definition_id"],
        revision=document["revision"],
        bindings=[node["node_binding_id"] for node in document["nodes"]],
        edges=document["edges"],
        document=document,
    ))
    for node in document["nodes"]:
        store._write("node_binding", graph_record(
            "node_binding",
            workflow_definition_id=document["workflow_definition_id"],
            workflow_definition_revision=document["revision"],
            node_binding_id=node["node_binding_id"],
            component_id=node["component_id"],
            component_version=node["component_version"],
            config=node["config"],
        ))
        descriptor = descriptors.get((node["component_id"], node["component_version"]))
        if descriptor is not None:
            store._write("node_definition", graph_record(
                "node_definition",
                component_id=node["component_id"],
                component_version=node["component_version"],
                descriptor=descriptor,
            ))


def _descriptor(component_id, version, schema, paths, *, adjustable=None, default=None,
                provided=()):
    descriptor = {
        "component_id": component_id,
        "component_version": version,
        "config_schema": deepcopy(schema),
        "checkpoint_save": {"config_paths": deepcopy(paths)},
        "adjustable_configuration": deepcopy(adjustable),
        "provided_session_item_types": deepcopy(list(provided)),
    }
    if default is not None:
        descriptor["default_config"] = deepcopy(default)
    return descriptor


def _node_registry(descriptors, document=None, *, types=None):
    definitions = dict(descriptors)
    if document is not None:
        for node in document["nodes"]:
            definitions.setdefault((node["component_id"], node["component_version"]), {
                "config_schema": {"type": "object"},
                "checkpoint_save": {"config_paths": []},
            })
    registry = NodeRegistry(types)
    for (component_id, version), descriptor in definitions.items():
        registry.register(NodeDefinition(
            component_id, version, component_id, "Test",
            deepcopy(descriptor.get("default_config", {})),
            deepcopy(descriptor["config_schema"]),
            adjustable_configuration=deepcopy(descriptor.get("adjustable_configuration")),
            checkpoint_save=deepcopy(descriptor.get("checkpoint_save", {"config_paths": []})),
            provided_session_item_types=tuple(
                deepcopy(descriptor.get("provided_session_item_types", ()))),
        ), None)
    return registry


def _context_types():
    types = TypeRegistry()
    types.register(DataTypeDefinition(
        EFFECTIVE_CONTEXT_TYPE, 4, {"type": "object"},
        default_value=deepcopy(EMPTY_CONTEXT),
        validator=validate_effective_context,
        write_validator=validate_native_context_write,
    ))
    return types


def _save_session(store, workflow_id, revision, document, sid):
    store._write("workflow_session", graph_record(
        "workflow_session",
        workflow_session_id=sid,
        workflow_definition_id=workflow_id,
        definition_revision=revision,
        revision=1,
        source={"kind": "new"},
        active_chain_run_id=None,
    ))


def _put_context_value(store, sid, key, value):
    connection = store._connection
    binding_row = connection.execute(
        "SELECT binding,revision FROM session_object_bindings WHERE session_id=? AND object_key=?",
        (sid, key),
    ).fetchone()
    assert binding_row is not None
    binding = ObjectBinding.from_dict(json.loads(binding_row["binding"]))
    revision_id = str(uuid4())
    connection.execute(
        "INSERT INTO session_object_values VALUES(?,?,?,?,?)",
        (revision_id, binding.type_id, binding.schema_version, 0, json.dumps(value)),
    )
    connection.execute(
        "INSERT INTO session_object_revision_membership VALUES(?,?,?)",
        (sid, key, revision_id),
    )
    connection.execute(
        "UPDATE session_object_bindings SET revision=?,revision_id=? WHERE session_id=? AND object_key=?",
        (binding_row["revision"] + 1, revision_id, sid, key),
    )


@pytest.fixture
def store(tmp_path):
    value = SqliteStore(tmp_path / "checkpoint-collection.sqlite")
    try:
        yield value
    finally:
        value.close()


def test_configuration_uses_fixed_revision_current_declarations_and_paths_only(store):
    workflow_id = str(uuid4())
    config_schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string"},
            "optional_note": {"type": ["string", "null"]},
            "settings": {
                "type": "object",
                "properties": {
                    "strategy": {"type": ["string", "null"]},
                    "fixed": {"type": "string"},
                },
                "required": ["strategy", "fixed"],
                "additionalProperties": False,
            },
            "selected": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["mode", "settings", "selected"],
        "additionalProperties": False,
    }
    fixed = _node("example.fixed", "1", {
        "mode": "fast",
        "settings": {"strategy": None, "fixed": "keep"},
        "selected": [],
    })
    prompt_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "text": {"type": "string"},
            "fixed_label": {"type": "string"},
        },
        "required": ["id", "text", "fixed_label"],
        "additionalProperties": False,
    }
    prompt = _node("prompts.item", "3", {
        "id": str(uuid4()), "text": "fixed document text", "fixed_label": "retain",
    }, policy="adjustable", index=1)
    document = _document(workflow_id, 1, [fixed, prompt])
    descriptors = {
        ("example.fixed", "1"): _descriptor(
            "example.fixed", "1", config_schema,
            [["mode"], ["optional_note"], ["selected"], ["settings", "strategy"]],
            default={"mode": "fallback", "optional_note": "from default"}),
        ("prompts.item", "3"): _descriptor(
            "prompts.item", "3", prompt_schema,
            [["fixed_label"], ["id"], ["text"]],
            adjustable={
                "schema_version": 1,
                "policy": "prompt",
                "fields": ["text", "presentation", "metadata", "lifecycle", "compaction"],
            },
            default={"text": "must not fill persisted config"}),
    }
    revision_two = deepcopy(document)
    revision_two["revision"] = 2
    revision_two["nodes"][0]["config"]["mode"] = "newer fixed revision"

    with transaction(store._connection):
        _save_definition(store, document, descriptors)
        _save_definition(store, revision_two, descriptors)
        repo = GraphRecordStore(store)
        registry = _node_registry(descriptors)
        repo.put_current_configuration({
            "workflow_definition_id": workflow_id,
            "edit_sequence": 7,
            "fixed_definition_revision": 1,
            "display_name": "Current overlay",
            "node_presentation": {},
            "adjustable_config": {prompt["node_binding_id"]: {"text": "latest overlay"}},
        })
        result = collect_checkpoint_configuration(repo, workflow_id, 1, registry=registry)
        newer = collect_checkpoint_configuration(repo, workflow_id, 2, registry=registry)

    assert result == {
        "workflow_definition_id": workflow_id,
        "fixed_definition_revision": 1,
        "nodes": {
            fixed["node_binding_id"]: {
                "component_id": "example.fixed",
                "component_version": "1",
                "config": {
                    "mode": "fast",
                    "settings": {"strategy": None},
                    "selected": [],
                },
            },
            prompt["node_binding_id"]: {
                "component_id": "prompts.item",
                "component_version": "3",
                "config": {"fixed_label": "retain", "id": prompt["config"]["id"]},
            },
        },
    }
    assert newer["fixed_definition_revision"] == 2
    assert newer["nodes"][fixed["node_binding_id"]]["config"]["mode"] == "newer fixed revision"


def test_configuration_omits_missing_required_saved_values(store):
    workflow_id = str(uuid4())
    node = _node("example.optional", "1", {})
    schema = {
        "type": "object",
        "properties": {"optional": {"type": "string"}},
        "required": ["optional"],
        "additionalProperties": False,
    }
    document = _document(workflow_id, 1, [node])
    descriptors = {
        ("example.optional", "1"): _descriptor(
            "example.optional", "1", schema, [["optional"]]),
    }
    with transaction(store._connection):
        _save_definition(store, document, descriptors)
        result = collect_checkpoint_configuration(
            GraphRecordStore(store), workflow_id, 1,
            registry=_node_registry(descriptors, document))

    assert result["nodes"] == {}


def test_schema_two_saved_definition_uses_its_existing_fixed_configuration_rules(store):
    workflow_id = str(uuid4())
    node = _node("example.legacy", "1", {"mode": "saved", "optional": None})
    node.pop("configuration_policy")
    document = _document(workflow_id, 1, [node])
    document["schema_version"] = 2
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string"},
            "optional": {"type": ["string", "null"]},
        },
        "required": ["mode", "optional"],
        "additionalProperties": False,
    }
    descriptors = {
        ("example.legacy", "1"): _descriptor(
            "example.legacy", "1", schema, [["mode"], ["optional"]]),
    }

    with transaction(store._connection):
        _save_definition(store, document, descriptors)
        registry = _node_registry(descriptors, document)
        result = collect_checkpoint_configuration(
            GraphRecordStore(store), workflow_id, 1, registry=registry)

    assert result == {
        "workflow_definition_id": workflow_id,
        "fixed_definition_revision": 1,
        "nodes": {
            node["node_binding_id"]: {
                "component_id": "example.legacy",
                "component_version": "1",
                "config": {"mode": "saved", "optional": None},
            },
        },
    }


def test_wide_members_path_collects_the_fixed_identity_projection(store):
    workflow_id = str(uuid4())
    member_one, member_two = str(uuid4()), str(uuid4())
    node = _node("prompts.group", "3", {
        "members": [
            {"id": member_one, "text": "adjustable one", "review_tag": "fixed one"},
            {"id": member_two, "text": "adjustable two", "review_tag": "fixed two"},
        ],
    }, policy="adjustable")
    document = _document(workflow_id, 1, [node])
    member_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "text": {"type": "string"},
            "review_tag": {"type": "string"},
        },
        "required": ["id", "text", "review_tag"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "members": {"type": "array", "items": member_schema},
        },
        "required": ["members"],
        "additionalProperties": False,
    }
    descriptors = {
        ("prompts.group", "3"): _descriptor(
            "prompts.group", "3", schema, [["members"]],
            adjustable={
                "schema_version": 1,
                "policy": "prompt",
                "fields": ["members"],
                "member_identity_field": "id",
                "member_fields": ["text", "presentation", "metadata", "lifecycle", "compaction"],
            }),
    }

    with transaction(store._connection):
        _save_definition(store, document, descriptors)
        result = collect_checkpoint_configuration(
            GraphRecordStore(store), workflow_id, 1,
            registry=_node_registry(descriptors, document))

    assert result["nodes"][node["node_binding_id"]]["config"]["members"] == {
        member_one: {"review_tag": "fixed one"},
        member_two: {"review_tag": "fixed two"},
    }


def test_configuration_requires_the_exact_current_definition(store):
    workflow_id = str(uuid4())
    node = _node("example.missing", "1", {})
    document = _document(workflow_id, 1, [node])
    with transaction(store._connection):
        _save_definition(store, document, {})
        with pytest.raises(ContractValidationError) as caught:
            collect_checkpoint_configuration(
                GraphRecordStore(store), workflow_id, 1, registry=NodeRegistry())

    assert caught.value.reason_code == "graph_missing_node_type"


def _accepted_context():
    call_id = str(uuid4())
    provider = remap_provider_metadata(
        metadata(("provider-call", "inspect_text", {"text": "one"})),
        {"provider-call": call_id},
    )
    call = project_agent_context_message(validate_record("agent_message", {
        "schema_version": 5,
        "message_id": str(uuid4()),
        "role": "assistant",
        "source": {"kind": "model", "request_id": str(uuid4())},
        "blocks": [
            {"kind": "text", "text": "original"},
            {
                "kind": "tool_call",
                "tool_call_id": call_id,
                "tool_name": "inspect_text",
                "tool_definition_version": "1",
                "raw_arguments": "{\"text\":\"one\"}",
                "parsed_arguments": {"text": "one"},
            },
        ],
        "thinking_summary": "Visible summary",
        "provider_metadata": provider,
    }))
    call_id = next(block["tool_call_id"] for block in call["blocks"]
                   if block["kind"] == "tool_call")
    result = {
        "message_id": str(uuid4()),
        "role": "tool",
        "blocks": [{
            "kind": "tool_result",
            "status": "success",
            "is_error": False,
            "tool_call_id": call_id,
            "tool_execution_id": str(uuid4()),
            "model_visible_text": "{\"text\":\"one\"}",
            "content": {"text": "one"},
        }],
    }
    value = {
        "messages": [call, result],
        "layout": [
            {"kind": "history", "round_id": str(uuid4()), "compaction": "never"},
            {"kind": "history", "round_id": str(uuid4()), "compaction": "never"},
        ],
        "accepted_delta_ids": [str(uuid4()), str(uuid4())],
        "generation": 3,
    }
    return value


def test_checkpoint_collector_uses_registered_item_without_reader_or_writer_nodes(store):
    workflow_id, sid = str(uuid4()), str(uuid4())
    agent = _node("agents.execute", "8", {}, index=0)
    root_binding = ObjectBinding(
        "agent-context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
        registrar_node_id=agent["node_binding_id"],
    )
    document = _document(workflow_id, 1, [agent], [root_binding])
    value = _accepted_context()
    types = _context_types()
    descriptors = {
        ("agents.execute", "8"): _descriptor(
            "agents.execute", "8", {"type": "object"}, [],
            provided=[{"type_id": EFFECTIVE_CONTEXT_TYPE, "schema_version": 4}],
        ),
    }
    registry = _node_registry(descriptors, document, types=types)

    with transaction(store._connection):
        TypeContractStore(store).check_registry(types)
        _save_definition(store, document, descriptors)
        _save_session(store, workflow_id, 1, document, sid)
        objects = SessionObjectStore(store, types)
        objects.initialize(sid, document)
        _put_context_value(store, sid, "agent-context", value)
        result = collect_agent_contexts_in_transaction(
            GraphRecordStore(store), sid, document, types=types, registry=registry)

    assert result == {"agent-context": {"value": value}}
    assert result["agent-context"]["value"]["messages"][0]["provider_metadata"]["content"] == (
        value["messages"][0]["provider_metadata"]["content"])
    assert result["agent-context"]["value"]["messages"][0]["provider_metadata"]["protocol"] == GEMINI_PROTOCOL
    assert [block["tool_call_id"] for block in result["agent-context"]["value"]["messages"][0]["blocks"]
            if block["kind"] == "tool_call"] == [
                result["agent-context"]["value"]["messages"][1]["blocks"][0]["tool_call_id"]]
    assert result["agent-context"]["value"]["accepted_delta_ids"] == value["accepted_delta_ids"]
    assert all("source" not in message for message in result["agent-context"]["value"]["messages"])


def test_checkpoint_collector_omits_uninitialized_empty_and_tombstoned_items(store):
    workflow_id = str(uuid4())
    agent = _node("agents.execute", "4", {})
    binding = ObjectBinding(
        "agent-context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
        registrar_node_id=agent["node_binding_id"],
    )
    document = _document(workflow_id, 1, [agent], [binding])
    types = _context_types()
    descriptors = {
        ("agents.execute", "4"): _descriptor(
            "agents.execute", "4", {"type": "object"}, [],
            provided=[{"type_id": EFFECTIVE_CONTEXT_TYPE, "schema_version": 4}],
        ),
    }
    registry = _node_registry(descriptors, document, types=types)
    session_ids = [str(uuid4()) for _ in range(3)]

    with transaction(store._connection):
        TypeContractStore(store).check_registry(types)
        _save_definition(store, document, descriptors)
        for sid in session_ids:
            _save_session(store, workflow_id, 1, document, sid)
        objects = SessionObjectStore(store, types)
        objects.initialize(session_ids[1], document)
        objects.initialize(session_ids[2], document)
        deleted_revision = store._connection.execute(
            "SELECT revision_id FROM session_object_bindings WHERE session_id=? AND object_key=?",
            (session_ids[2], "agent-context"),
        ).fetchone()["revision_id"]
        store._connection.execute(
            "UPDATE session_object_values SET deleted=1 WHERE revision_id=?", (deleted_revision,))
        repo = GraphRecordStore(store)
        absent = collect_agent_contexts_in_transaction(
            repo, session_ids[0], document, types=types, registry=registry)
        empty = collect_agent_contexts_in_transaction(
            repo, session_ids[1], document, types=types, registry=registry)
        tombstone = collect_agent_contexts_in_transaction(
            repo, session_ids[2], document, types=types, registry=registry)

    assert absent == {}
    assert empty == {}
    assert tombstone == {}


def test_checkpoint_collector_requires_exact_registrar_type_declaration(store):
    workflow_id, sid = str(uuid4()), str(uuid4())
    registrar = _node("agents.execute", "8", {})
    binding = ObjectBinding(
        "agent-context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
        registrar_node_id=registrar["node_binding_id"],
    )
    document = _document(workflow_id, 1, [registrar], [binding])
    types = _context_types()
    descriptors = {
        ("agents.execute", "8"): _descriptor(
            "agents.execute", "8", {"type": "object"}, [],
        ),
    }
    registry = _node_registry(descriptors, document, types=types)
    with transaction(store._connection):
        TypeContractStore(store).check_registry(types)
        _save_definition(store, document, descriptors)
        _save_session(store, workflow_id, 1, document, sid)
        SessionObjectStore(store, types).initialize(sid, document)
        with pytest.raises(ContractValidationError) as caught:
            collect_agent_contexts_in_transaction(
                GraphRecordStore(store), sid, document, types=types, registry=registry)

    assert caught.value.reason_code == "checkpoint_context_registration_invalid"


def test_empty_checkpoint_whitelist_does_not_require_context_type(store):
    workflow_id, sid = str(uuid4()), str(uuid4())
    document = _document(workflow_id, 1, [])
    types = TypeRegistry()
    registry = NodeRegistry(types)
    with transaction(store._connection):
        _save_definition(store, document, {})
        _save_session(store, workflow_id, 1, document, sid)
        result = collect_agent_contexts_in_transaction(
            GraphRecordStore(store), sid, document, types=types, registry=registry)

    assert result == {}


def test_collectors_require_the_caller_owned_transaction(store):
    workflow_id = str(uuid4())
    node = _node("example.empty", "1", {})
    document = _document(workflow_id, 1, [node])
    descriptors = {
        ("example.empty", "1"): _descriptor(
            "example.empty", "1", {"type": "object", "properties": {}, "additionalProperties": False}, []),
    }
    with transaction(store._connection):
        _save_definition(store, document, descriptors)
    repo = GraphRecordStore(store)

    with pytest.raises(ContractValidationError) as caught:
        collect_checkpoint_configuration(
            repo, workflow_id, 1, registry=_node_registry(descriptors, document))
    assert caught.value.reason_code == "storage_contract_violation"


def test_composite_collector_returns_the_approved_logical_envelope(store):
    workflow_id, sid = str(uuid4()), str(uuid4())
    node = _node("example.empty", "1", {})
    document = _document(workflow_id, 1, [node])
    descriptors = {
        ("example.empty", "1"): _descriptor(
            "example.empty", "1", {"type": "object", "properties": {}, "additionalProperties": False}, []),
    }
    types = _context_types()
    with transaction(store._connection):
        TypeContractStore(store).check_registry(types)
        _save_definition(store, document, descriptors)
        _save_session(store, workflow_id, 1, document, sid)
        registry = _node_registry(descriptors, document)
        actual = collect_checkpoint_state(
            GraphRecordStore(store), workflow_id, 1, sid, types=types, registry=registry)

    assert actual == {
        "configuration": {
            "workflow_definition_id": workflow_id,
            "fixed_definition_revision": 1,
            "nodes": {},
        },
        "agent_contexts": {},
    }


def test_stock_schema_two_native_context_graph_collects_without_model_execution(tmp_path):
    transport = NativeTransport()
    with closing(GraphWorkflowService(
            tmp_path / "native-context-collection.sqlite",
            public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = native_graph(service, policy=False, once=False)
        session = create(service, document)
        store = service._store()
        try:
            with transaction(store._connection):
                actual = collect_checkpoint_state(
                    GraphRecordStore(store),
                    document["workflow_definition_id"],
                    document["revision"],
                    session["workflow_session_id"],
                    types=service.registry.data_types,
                    registry=service.registry,
                )
        finally:
            store.close()

    assert document["schema_version"] == 2
    assert transport.calls == []
    assert actual == {
        "configuration": {
            "workflow_definition_id": document["workflow_definition_id"],
            "fixed_definition_revision": document["revision"],
            "nodes": {},
        },
        "agent_contexts": {},
    }
