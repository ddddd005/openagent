from contextlib import contextmanager
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_collection import collect_checkpoint_configuration
from phase1_agent.checkpoint_repository import CheckpointRepository
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry
from phase1_agent.graph_records import graph_record
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.storage import SqliteStore


@contextmanager
def transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _identity():
    return str(uuid4())


def _document(workflow_id, nodes):
    return {
        "schema_version": 2,
        "workflow_definition_id": workflow_id,
        "revision": 1,
        "name": "Definition retirement test",
        "nodes": nodes,
        "edges": [],
        "object_bindings": [],
    }


def _node():
    return {
        "node_binding_id": _identity(),
        "component_id": "test.legacy",
        "component_version": "1",
        "title": "Legacy node",
        "position": {"x": 0, "y": 0},
        "config": {"value": "must not be collected"},
    }


def _save_definition(store, document, *, old_descriptor=None):
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
    if old_descriptor is not None:
        store._write("node_definition", graph_record(
            "node_definition",
            component_id="test.legacy",
            component_version="1",
            descriptor=old_descriptor,
        ))


class _NoNodeDefinitionReads:
    def __init__(self, store):
        self.store = store
        self._repo = GraphRecordStore(store)
        self.reads = []

    def get(self, kind, **identity):
        self.reads.append(kind)
        assert kind != "node_definition"
        return self._repo.get(kind, **identity)


def _records_snapshot(connection):
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT record_type,record_id,payload,immutable,created_at "
            "FROM records ORDER BY record_type,record_id"
        )
    ]


@pytest.fixture
def store(tmp_path):
    value = SqliteStore(tmp_path / "definition-retirement-checkpoints.sqlite")
    try:
        yield value
    finally:
        if value._connection.in_transaction:
            value._connection.execute("ROLLBACK")
        value.close()


def test_collection_uses_current_registry_and_never_reads_legacy_descriptor(store):
    workflow_id = _identity()
    document = _document(workflow_id, [_node()])
    _save_definition(store, document, old_descriptor={
        "config_schema": {"type": "object"},
        "checkpoint_save": {"config_paths": []},
    })
    repo = _NoNodeDefinitionReads(store)
    registry = NodeRegistry()
    registry.register(NodeDefinition(
        "test.legacy", "1", "Legacy node", "Test", {},
        {"type": "object", "properties": {"value": {"type": "string"}}},
        checkpoint_save={"config_paths": [["value"]]},
    ), None)

    with transaction(store._connection):
        before = _records_snapshot(store._connection)
        changes_before = store._connection.total_changes
        result = collect_checkpoint_configuration(repo, workflow_id, 1, registry=registry)
        assert result["nodes"] == {
            document["nodes"][0]["node_binding_id"]: {
                "component_id": "test.legacy",
                "component_version": "1",
                "config": {"value": "must not be collected"},
            },
        }
        assert repo.reads == ["workflow_definition_revision"]
        assert store._connection.total_changes == changes_before
        assert _records_snapshot(store._connection) == before
        assert store._connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints"
        ).fetchone()[0] == 0


def test_empty_definition_collects_empty_projection_without_node_definition_reads(store):
    workflow_id = _identity()
    _save_definition(store, _document(workflow_id, []))
    repo = _NoNodeDefinitionReads(store)

    with transaction(store._connection):
        result = collect_checkpoint_configuration(repo, workflow_id, 1, registry=NodeRegistry())

    assert result == {
        "workflow_definition_id": workflow_id,
        "fixed_definition_revision": 1,
        "nodes": {},
    }
    assert repo.reads == ["workflow_definition_revision"]


def test_checkpoint_repository_materializes_old_point_without_definition_manager(store):
    workflow_id, session_id, node_id = _identity(), _identity(), _identity()
    owner = {
        "workflow_definition_id": workflow_id,
        "workflow_session_id": session_id,
        "fixed_definition_revision": 1,
    }
    state = {
        "configuration": {
            "workflow_definition_id": workflow_id,
            "fixed_definition_revision": 1,
            "nodes": {
                node_id: {
                    "component_id": "test.legacy",
                    "component_version": "1",
                    "config": {"value": "already saved"},
                },
            },
        },
        "agent_contexts": {},
        "variables": {},
        "chat_contexts": {},
    }
    point = {
        "schema_version": 4,
        "checkpoint_id": _identity(),
        **owner,
        "parent_checkpoint_id": None,
        "state_storage": {"mode": "full", "state": state},
    }

    with transaction(store._connection):
        store._write("workflow_session", graph_record(
            "workflow_session",
            workflow_session_id=session_id,
            workflow_definition_id=workflow_id,
            definition_revision=1,
            revision=1,
            source={"kind": "new"},
            active_chain_run_id=None,
        ))
        store._write("node_definition", graph_record(
            "node_definition",
            component_id="test.legacy",
            component_version="1",
            descriptor={"retired": True},
        ))
        repository = CheckpointRepository(store._connection, **owner)
        repository.insert(point)
        assert repository.get(point["checkpoint_id"]) == point
        materialized = repository.materialize(point["checkpoint_id"])

    assert materialized == state
    assert set(materialized) == {"configuration", "agent_contexts", "variables", "chat_contexts"}
    assert "messages" not in materialized
