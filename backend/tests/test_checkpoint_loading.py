from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_collection import collect_checkpoint_configuration
from phase1_agent.checkpoint_loading import load_checkpoint_state_in_transaction
from phase1_agent.context_contract import (
    EFFECTIVE_CONTEXT_TYPE, validate_effective_context,
)
from phase1_agent.context_v4 import validate_native_context_write
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry
from phase1_agent.graph_records import graph_record
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding, TypeRegistry, WriteIntent
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore
from phase1_agent.type_contract_store import TypeContractStore
from phase1_agent.checkpoint_loading import validate_checkpoint_configuration_target


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
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _node(component_id, version, config, index):
    return {
        "node_binding_id": str(uuid4()),
        "component_id": component_id,
        "component_version": version,
        "title": component_id,
        "position": {"x": index, "y": 0},
        "config": deepcopy(config),
        "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
    }


def _document(workflow_id, nodes, object_bindings, *, schema_version=3):
    return {
        "schema_version": schema_version,
        "workflow_definition_id": workflow_id,
        "revision": 1,
        "name": "Checkpoint loader",
        "nodes": deepcopy(nodes),
        "edges": [],
        "object_bindings": [binding.to_dict() for binding in object_bindings],
    }


def _descriptor(component_id, version, schema, *, provided=()):
    return {
        "component_id": component_id,
        "component_version": version,
        "config_schema": deepcopy(schema),
        "checkpoint_save": {"config_paths": []},
        "adjustable_configuration": None,
        "provided_session_item_types": deepcopy(list(provided)),
    }


def _node_registry(descriptors, types):
    registry = NodeRegistry(types)
    for (component_id, version), descriptor in descriptors.items():
        registry.register(NodeDefinition(
            component_id, version, component_id, "Test", {},
            deepcopy(descriptor["config_schema"]),
            adjustable_configuration=deepcopy(descriptor.get("adjustable_configuration")),
            checkpoint_save=deepcopy(descriptor.get("checkpoint_save", {"config_paths": []})),
            provided_session_item_types=tuple(
                deepcopy(descriptor.get("provided_session_item_types", ()))),
        ), None)
    return registry


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
        descriptor = descriptors[(node["component_id"], node["component_version"])]
        store._write("node_definition", graph_record(
            "node_definition",
            component_id=node["component_id"],
            component_version=node["component_version"],
            descriptor=descriptor,
        ))


def _types(*, include_other=False):
    types = TypeRegistry()
    types.register(DataTypeDefinition(
        EFFECTIVE_CONTEXT_TYPE, 4, {"type": "object"},
        default_value=deepcopy(EMPTY_CONTEXT),
        validator=validate_effective_context,
        write_validator=validate_native_context_write,
        checkpoint_admission=True,
    ))
    if include_other:
        types.register(DataTypeDefinition(
            "test.other", 1, {"type": "object"}, default_value={"kept": True},
        ))
    return types


@pytest.fixture
def store(tmp_path):
    value = SqliteStore(tmp_path / "checkpoint-loading.sqlite")
    try:
        yield value
    finally:
        if value._connection.in_transaction:
            value._connection.execute("ROLLBACK")
        value.close()


def seed_target(store, *, count=1, include_other=False, reader_count=0, writer_count=0,
                context_type=EFFECTIVE_CONTEXT_TYPE, schema_version=4, schema_version_document=3,
                add_overlay=False):
    workflow_id, session_id = str(uuid4()), str(uuid4())
    types = _types(include_other=include_other or context_type == "test.other")
    agent_nodes, access_nodes, bindings, descriptors = [], [], [], {}
    agent_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    access_schema = {
        "type": "object",
        "properties": {"object_key": {"type": "string"}},
        "required": ["object_key"],
        "additionalProperties": False,
    }
    descriptors[("agents.execute", "8")] = _descriptor(
        "agents.execute", "8", agent_schema,
        provided=[{"type_id": EFFECTIVE_CONTEXT_TYPE, "schema_version": 4}],
    )
    if reader_count or writer_count:
        descriptors[("test.object.access", "1")] = _descriptor(
            "test.object.access", "1", access_schema)
    registry = _node_registry(descriptors, types)

    for index in range(count):
        key = f"context-{index}"
        agent = _node("agents.execute", "8", {}, index * 2)
        agent_nodes.append(agent)
        local_access = []
        for access_index in range(max(reader_count, writer_count)):
            access = _node("test.object.access", "1", {"object_key": key}, index * 4 + access_index + 1)
            access_nodes.append(access)
            local_access.append(access)
        bindings.append(ObjectBinding(
            key, context_type, schema_version, "shared",
            readers=tuple(node["node_binding_id"] for node in local_access[:reader_count]),
            writers=tuple(node["node_binding_id"] for node in local_access[:writer_count]),
            registrar_node_id=agent["node_binding_id"],
        ))
    if include_other:
        bindings.append(ObjectBinding("other", "test.other", 1, "shared"))

    document = _document(
        workflow_id, [*agent_nodes, *access_nodes],
        bindings, schema_version=schema_version_document,
    )
    with transaction(store._connection):
        TypeContractStore(store).check_registry(types)
        _save_definition(store, document, descriptors)
        session = graph_record(
            "workflow_session",
            workflow_session_id=session_id,
            workflow_definition_id=workflow_id,
            definition_revision=1,
            revision=1,
            source={"kind": "new"},
            active_chain_run_id=None,
        )
        store._write("workflow_session", session)
        objects = SessionObjectStore(store, types)
        objects.initialize(session_id, document)
        repo = GraphRecordStore(store)
        if add_overlay:
            repo.put_current_configuration({
                "workflow_definition_id": workflow_id,
                "edit_sequence": 4,
                "fixed_definition_revision": 1,
                "display_name": "Current overlay",
                "node_presentation": {},
                "adjustable_config": {},
            })
        fixed_configuration = collect_checkpoint_configuration(
            repo, workflow_id, 1, registry=registry)
        current = objects.current(session_id)
        initial_cas = {
            key: {"revision": current[key]["revision"], "revision_id": current[key]["revision_id"]}
            for key in current
        }
        session = repo.get("workflow_session", workflow_session_id=session_id)
    return {
        "workflow_id": workflow_id,
        "session_id": session_id,
        "session": session,
        "document": document,
        "types": types,
        "registry": registry,
        "agent_ids": [node["node_binding_id"] for node in agent_nodes],
        "context_ids": [node["node_binding_id"] for node in access_nodes],
        "fixed_configuration": fixed_configuration,
        "initial_cas": initial_cas,
    }


def agent_value(text):
    message_id, round_id, delta_id = str(uuid4()), str(uuid4()), str(uuid4())
    return {
        "messages": [{
            "message_id": message_id,
            "role": "user",
            "blocks": [{"kind": "text", "text": text}],
        }],
        "layout": [{"kind": "history", "round_id": round_id, "compaction": "never"}],
        "accepted_delta_ids": [delta_id],
        "generation": 1,
    }


def checkpoint_state(target, contexts):
    return {
        "configuration": deepcopy(target["fixed_configuration"]),
        "agent_contexts": deepcopy(contexts),
        "variables": {},
        "chat_contexts": {},
    }


def cas_for(target, contexts):
    return {key: target["initial_cas"][key] for key in contexts}


def test_load_initializes_registered_values_without_node_permissions_or_receipts(store):
    target = seed_target(store, include_other=True, add_overlay=True)
    key = "context-0"
    value = agent_value("accepted context")
    contexts = {key: {"value": value}}
    expected_overlay = GraphRecordStore(store).current_configuration(target["workflow_id"])

    with transaction(store._connection):
        result = load_checkpoint_state_in_transaction(
            GraphRecordStore(store), target["session"], checkpoint_state(target, contexts),
            cas_for(target, contexts), types=target["types"], registry=target["registry"],
        )

    assert set(result) == {key}
    assert result[key]["object_key"] == key
    assert result[key]["revision"] == target["initial_cas"][key]["revision"] + 1
    with transaction(store._connection):
        current = SessionObjectStore(store, target["types"]).current(target["session_id"])
        assert current[key]["value"] == value
        assert current[key]["revision"] == target["initial_cas"][key]["revision"] + 1
        assert current["other"]["value"] == {"kept": True}
        assert current["other"]["revision"] == target["initial_cas"]["other"]["revision"]
        receipt = store._connection.execute(
            "SELECT operation_key,result FROM session_object_receipts WHERE session_id=?",
            (target["session_id"],),
        ).fetchall()
        assert receipt == []
        assert result[key]["revision_id"] != target["initial_cas"][key]["revision_id"]
        assert GraphRecordStore(store).current_configuration(target["workflow_id"]) == expected_overlay
        for kind in ("state_snapshot", "workflow_commit", "workflow_ref",
                     "chain_run", "node_run", "workflow_output"):
            assert GraphRecordStore(store).rows(kind, workflow_session_id=target["session_id"]) == []


def test_old_configuration_projection_survives_checkpoint_declaration_update(store):
    target = seed_target(store, reader_count=1)
    definition = target["registry"].get_definition("test.object.access", "1")
    target["registry"].update_declarations(replace(
        definition, checkpoint_save={"config_paths": [["object_key"]]}))
    with transaction(store._connection):
        old_configuration = collect_checkpoint_configuration(
            GraphRecordStore(store), target["workflow_id"], 1, registry=target["registry"])
    context_id = target["context_ids"][0]
    assert old_configuration["nodes"][context_id]["config"] == {"object_key": "context-0"}

    updated = target["registry"].get_definition("test.object.access", "1")
    target["registry"].update_declarations(replace(
        updated, checkpoint_save={"config_paths": []}, execution_class="non_working"))
    validate_checkpoint_configuration_target(
        old_configuration, target["document"], registry=target["registry"])

    changed_value = deepcopy(old_configuration)
    changed_value["nodes"][context_id]["config"]["object_key"] = "forged"
    with pytest.raises(ContractValidationError) as mismatch:
        validate_checkpoint_configuration_target(
            changed_value, target["document"], registry=target["registry"])
    assert mismatch.value.reason_code == "checkpoint_configuration_mismatch"

    changed_identity = deepcopy(old_configuration)
    changed_identity["nodes"][context_id]["component_version"] = "5"
    with pytest.raises(ContractValidationError):
        with transaction(store._connection):
            load_checkpoint_state_in_transaction(
                GraphRecordStore(store), target["session"],
                {"configuration": changed_identity, "agent_contexts": {},
                 "variables": {}, "chat_contexts": {}}, {},
                types=target["types"], registry=target["registry"],
            )
    with transaction(store._connection):
        assert SessionObjectStore(store, target["types"]).current(target["session_id"])[
            "context-0"]["value"] == EMPTY_CONTEXT
        assert store._connection.execute(
            "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
            (target["session_id"],),
        ).fetchone()[0] == 0


def test_empty_agent_state_returns_without_object_write(store, monkeypatch):
    target = seed_target(store)

    def unexpected_apply(*args, **kwargs):
        pytest.fail("Empty Agent state must not apply an object write")

    monkeypatch.setattr(SessionObjectStore, "apply", unexpected_apply)
    with transaction(store._connection):
        result = load_checkpoint_state_in_transaction(
            GraphRecordStore(store), target["session"],
            checkpoint_state(target, {}), {}, types=target["types"], registry=target["registry"],
        )
    assert result == {}
    with transaction(store._connection):
        current = SessionObjectStore(store, target["types"]).current(target["session_id"])
        assert current["context-0"]["value"] == EMPTY_CONTEXT
        assert current["context-0"]["revision"] == target["initial_cas"]["context-0"]["revision"]
        assert store._connection.execute(
            "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
            (target["session_id"],),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("problem", ["configuration", "cas", "entry_extra"])
def test_invalid_fixed_configuration_or_target_basis_writes_nothing(store, problem):
    target = seed_target(store)
    key = "context-0"
    contexts = {key: {"value": agent_value("state")}}
    state = checkpoint_state(target, contexts)
    cas = cas_for(target, contexts)
    if problem == "configuration":
        state["configuration"]["nodes"][str(uuid4())] = {
            "component_id": "agents.execute",
            "component_version": "8",
            "config": {},
        }
    elif problem == "cas":
        cas[key] = {**cas[key], "revision": cas[key]["revision"] + 1}
    else:
        state["agent_contexts"][key]["agent_node_id"] = str(uuid4())

    with pytest.raises(ContractValidationError):
        with transaction(store._connection):
            load_checkpoint_state_in_transaction(
                GraphRecordStore(store), target["session"], state, cas, types=target["types"],
                registry=target["registry"],
            )
    with transaction(store._connection):
        current = SessionObjectStore(store, target["types"]).current(target["session_id"])
        assert current[key]["value"] == EMPTY_CONTEXT
        assert current[key]["revision"] == target["initial_cas"][key]["revision"]
        assert store._connection.execute(
            "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
            (target["session_id"],),
        ).fetchone()[0] == 0


def test_target_rejects_existing_session_object_receipts(store):
    target = seed_target(store, writer_count=1)
    key = "context-0"
    value = agent_value("preexisting target write")
    with transaction(store._connection):
        SessionObjectStore(store, target["types"]).apply(
            target["session_id"],
            node_id=target["context_ids"][0],
            writes=[WriteIntent(
                key, target["initial_cas"][key]["revision"], "prior-write", value).to_dict()],
        )
        current = SessionObjectStore(store, target["types"]).current(target["session_id"])
        prior_value = current[key]["value"]
        prior_revision = current[key]["revision"]

    contexts = {key: {"value": agent_value("checkpoint")}}
    with pytest.raises(ContractValidationError) as caught:
        with transaction(store._connection):
            load_checkpoint_state_in_transaction(
                GraphRecordStore(store), target["session"], checkpoint_state(target, contexts),
                {key: {"revision": prior_revision, "revision_id": current[key]["revision_id"]}},
                types=target["types"], registry=target["registry"],
            )
    assert caught.value.reason_code == "checkpoint_target_not_fresh"
    with transaction(store._connection):
        current = SessionObjectStore(store, target["types"]).current(target["session_id"])
        assert current[key]["value"] == prior_value
        assert current[key]["revision"] == prior_revision
        assert store._connection.execute(
            "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
            (target["session_id"],),
        ).fetchone()[0] == 1


def test_target_rejects_noninitial_object_revision_without_receipt(store):
    target = seed_target(store)
    key = "context-0"
    with transaction(store._connection):
        store._connection.execute(
            "UPDATE session_object_bindings SET revision=2 "
            "WHERE session_id=? AND object_key=?",
            (target["session_id"], key),
        )

    contexts = {key: {"value": agent_value("checkpoint")}}
    cas = {key: {"revision": 2, "revision_id": target["initial_cas"][key]["revision_id"]}}
    with pytest.raises(ContractValidationError) as caught:
        with transaction(store._connection):
            load_checkpoint_state_in_transaction(
                GraphRecordStore(store), target["session"], checkpoint_state(target, contexts),
                cas, types=target["types"], registry=target["registry"],
            )
    assert caught.value.reason_code == "checkpoint_target_not_fresh"
    with transaction(store._connection):
        current = SessionObjectStore(store, target["types"]).current(target["session_id"])
        assert current[key]["value"] == EMPTY_CONTEXT
        assert current[key]["revision"] == 2
        assert store._connection.execute(
            "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
            (target["session_id"],),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(("reader_count", "writer_count"), [(0, 0), (1, 0), (0, 2)])
def test_load_does_not_require_a_reader_or_unique_writer_node(store, reader_count, writer_count):
    target = seed_target(store, reader_count=reader_count, writer_count=writer_count)
    key = "context-0"
    contexts = {key: {"value": agent_value("state")}}

    with transaction(store._connection):
        result = load_checkpoint_state_in_transaction(
            GraphRecordStore(store), target["session"], checkpoint_state(target, contexts),
            cas_for(target, contexts), types=target["types"], registry=target["registry"],
        )

    assert set(result) == {key}
    with transaction(store._connection):
        assert SessionObjectStore(store, target["types"]).current(target["session_id"])[
            key]["value"] == contexts[key]["value"]
        assert store._connection.execute(
            "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
            (target["session_id"],),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["workflow_ref", "chain_run"])
def test_target_must_have_no_commit_ref_or_run_before_loading(store, kind):
    target = seed_target(store)
    with transaction(store._connection):
        if kind == "workflow_ref":
            store._write("workflow_ref", graph_record(
                "workflow_ref",
                workflow_ref_id=str(uuid4()),
                workflow_session_id=target["session_id"],
                head_commit_id=str(uuid4()),
                revision=1,
            ))
        else:
            store._write("chain_run", graph_record(
                "chain_run",
                chain_run_id=str(uuid4()),
                workflow_session_id=target["session_id"],
                workflow_definition_id=target["workflow_id"],
                definition_revision=1,
                status="prepared",
                revision=1,
                targets=[],
                ordered_nodes=[],
                node_run_ids=[],
                completed_nodes=[],
                next_node_index=0,
                inputs={},
                outputs={},
                diagnostic=None,
                execution_kind="round",
                event=None,
                base_commit_id=None,
                node_run_attempts=[],
            ))

    with pytest.raises(ContractValidationError) as caught:
        with transaction(store._connection):
            load_checkpoint_state_in_transaction(
                GraphRecordStore(store), target["session"],
                checkpoint_state(target, {}), {}, types=target["types"], registry=target["registry"],
            )
    assert caught.value.reason_code == "checkpoint_target_not_fresh"


def test_internal_initializer_itself_rejects_a_committed_session(store):
    target = seed_target(store)
    key = "context-0"
    with transaction(store._connection):
        store._write("workflow_ref", graph_record(
            "workflow_ref",
            workflow_ref_id=str(uuid4()),
            workflow_session_id=target["session_id"],
            head_commit_id=str(uuid4()),
            revision=1,
        ))

    with pytest.raises(ContractValidationError) as caught:
        with transaction(store._connection):
            SessionObjectStore(store, target["types"])._initialize_checkpoint_values(
                target["session_id"], target["document"],
                {key: agent_value("must not overwrite a committed session")},
                cas_for(target, {key: None}),
            )

    assert caught.value.reason_code == "checkpoint_target_not_fresh"
    with transaction(store._connection):
        current = SessionObjectStore(store, target["types"]).current(target["session_id"])
        assert current[key]["value"] == EMPTY_CONTEXT
        assert current[key]["revision"] == target["initial_cas"][key]["revision"]
        assert store._connection.execute(
            "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
            (target["session_id"],),
        ).fetchone()[0] == 0


def test_target_binding_must_be_effective_context_v4(store):
    target = seed_target(store, context_type="test.other", schema_version=1)
    key = "context-0"
    contexts = {key: {"value": agent_value("state")}}
    with pytest.raises(ContractValidationError) as caught:
        with transaction(store._connection):
            load_checkpoint_state_in_transaction(
                GraphRecordStore(store), target["session"], checkpoint_state(target, contexts),
                cas_for(target, contexts), types=target["types"], registry=target["registry"],
            )
    assert caught.value.reason_code == "checkpoint_target_context_binding_mismatch"


def test_later_checkpoint_value_failure_rolls_back_earlier_initialization(store, monkeypatch):
    target = seed_target(store, count=2)
    contexts = {
        f"context-{index}": {"value": agent_value(f"context {index}")}
        for index in range(2)
    }
    original_put_value = SessionObjectStore._put_value
    calls = []

    def fail_second_value(objects, binding, value, **kwargs):
        calls.append(binding.object_key)
        if len(calls) == 2:
            raise OSError("injected target checkpoint initialization failure")
        return original_put_value(objects, binding, value, **kwargs)

    monkeypatch.setattr(SessionObjectStore, "_put_value", fail_second_value)
    with pytest.raises(OSError, match="injected target checkpoint initialization failure"):
        with transaction(store._connection):
            load_checkpoint_state_in_transaction(
                GraphRecordStore(store), target["session"],
                checkpoint_state(target, contexts), cas_for(target, contexts),
                types=target["types"], registry=target["registry"],
            )
    assert len(calls) == 2
    with transaction(store._connection):
        current = SessionObjectStore(store, target["types"]).current(target["session_id"])
        for key in contexts:
            assert current[key]["value"] == EMPTY_CONTEXT
            assert current[key]["revision"] == target["initial_cas"][key]["revision"]
        assert store._connection.execute(
            "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
            (target["session_id"],),
        ).fetchone()[0] == 0
