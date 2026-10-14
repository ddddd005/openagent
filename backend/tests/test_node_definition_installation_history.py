"""Local descriptor checks without the retired historical membership platform."""

from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort, NodeRegistry
from phase1_agent.graph_records import graph_record
from phase1_agent.historical_artifact_reader import (
    HistoryArtifactAuthority, readonly_history_connection,
)
from phase1_agent.storage import SqliteStore


@pytest.fixture
def history(tmp_path):
    definition = NodeDefinition(
        "test.history-source", "1", "Source", "Test", {"value": "original", "other": "later"},
        {"type": "object", "properties": {
            "value": {"type": "string"}, "other": {"type": "string"},
        }, "required": ["value", "other"], "additionalProperties": False},
        outputs=(NodePort("output", "TEXT"),),
        checkpoint_save={"config_paths": [["value"]]},
    )
    registry = NodeRegistry()
    registry.register(definition, None)
    workflow_id, session_id, node_id = (str(uuid4()) for _ in range(3))
    node = {
        "node_binding_id": node_id, "component_id": definition.component_id,
        "component_version": definition.component_version, "title": "Source",
        "position": {"x": 0, "y": 0}, "config": deepcopy(definition.default_config),
        "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
    }
    document = {
        "schema_version": 3, "workflow_definition_id": workflow_id, "revision": 1,
        "name": "Historical declaration", "nodes": [node], "edges": [], "object_bindings": [],
    }
    path = tmp_path / "history-declarations.sqlite"
    with closing(SqliteStore(path)) as store:
        store._write("workflow_definition_revision", graph_record(
            "workflow_definition_revision", workflow_definition_id=workflow_id,
            revision=1, bindings=[node_id], edges=[], document=document,
        ))
        store._write("node_binding", graph_record(
            "node_binding", workflow_definition_id=workflow_id, workflow_definition_revision=1,
            node_binding_id=node_id, component_id=definition.component_id,
            component_version=definition.component_version, config=node["config"],
        ))
        store._write("workflow_session", graph_record(
            "workflow_session", workflow_session_id=session_id, workflow_definition_id=workflow_id,
            definition_revision=1, revision=1, source={"kind": "new"}, active_chain_run_id=None,
        ))
        yield SimpleNamespace(
            path=path, store=store, definition=definition, registry=registry,
            workflow_id=workflow_id, session_id=session_id, node=node, document=document,
        )


def _save_descriptor(history, descriptor):
    history.store._write("node_definition", graph_record(
        "node_definition", component_id=history.definition.component_id,
        component_version=history.definition.component_version, descriptor=descriptor,
    ))


@contextmanager
def _reader(history):
    with readonly_history_connection(history.path) as connection:
        yield HistoryArtifactAuthority(
            connection, history.registry.data_types, workflow_definition_id=history.workflow_id,
            workflow_session_id=history.session_id, fixed_definition_revision=1,
            registry=history.registry,
        )


def _producer(reader, history, *, config=None):
    return reader._node(
        {"workflow_definition_id": history.workflow_id, "definition_revision": 1},
        {"node_binding_id": history.node["node_binding_id"],
         "config": deepcopy(history.node["config"] if config is None else config)},
    )


def test_declaration_updates_keep_historical_contract_and_consumed_values(history):
    historical = history.definition.to_dict(executable=False)
    historical.pop("execution_class")
    _save_descriptor(history, historical)
    history.registry.update_declarations(replace(
        history.definition, checkpoint_save={"config_paths": [["other"]]},
        execution_class="non_working",
    ))
    with _reader(history) as reader:
        statements = []
        reader.connection.set_trace_callback(statements.append)
        node, descriptor, document = _producer(reader, history)
        assert descriptor == historical
        assert node["config"] == {"value": "original", "other": "later"}
        assert document == history.document
        assert reader._fixed_document(document) == reader._fixed_document(history.document)
        descriptor["config_schema"]["properties"]["value"]["type"] = "integer"
        assert _producer(reader, history)[1] == historical
        assert not any(statement.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"),
        ) for statement in statements)


@pytest.mark.parametrize("field", ["config_schema", "outputs", "capabilities"])
def test_historical_contract_changes_still_reject(history, field):
    _save_descriptor(history, history.definition.to_dict())
    changes = {
        "config_schema": {**history.definition.config_schema, "required": ["value"]},
        "outputs": (NodePort("another-output", "TEXT"),),
        "capabilities": ("external:read",),
    }
    replacement = NodeRegistry()
    replacement.register(replace(history.definition, **{field: changes[field]}), None)
    history.registry = replacement
    with _reader(history) as reader, pytest.raises(ContractValidationError) as caught:
        _producer(reader, history)
    assert caught.value.reason_code == "history_node_redefined"


@pytest.mark.parametrize("has_old_descriptor", [False, True])
def test_historical_read_requires_exact_current_type_even_with_an_old_descriptor(history, has_old_descriptor):
    if has_old_descriptor:
        _save_descriptor(history, history.definition.to_dict())
    replacement = NodeRegistry()
    replacement.register(replace(history.definition, component_version="2"), None)
    history.registry = replacement
    with _reader(history) as reader, pytest.raises(ContractValidationError) as caught:
        _producer(reader, history)
    assert caught.value.reason_code == "history_node_unavailable"


def test_new_run_without_old_descriptor_reads_registered_contract_without_writes(history):
    before = history.store._connection.total_changes
    with _reader(history) as reader:
        statements = []
        reader.connection.set_trace_callback(statements.append)
        assert _producer(reader, history)[1] == history.definition.to_dict()
        reader._fixed_document(history.document)
        assert reader.connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='node_definition'",
        ).fetchone()[0] == 0
        assert not any(statement.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"),
        ) for statement in statements)
    assert history.store._connection.total_changes == before


def test_historical_descriptor_with_another_identity_is_rejected(history):
    _save_descriptor(history, {**history.definition.to_dict(), "component_id": "test.another"})
    with _reader(history) as reader, pytest.raises(ContractValidationError) as caught:
        _producer(reader, history)
    assert caught.value.reason_code == "history_storage_invalid"


def test_historical_consumed_configuration_stays_fixed_after_declaration_update(history):
    _save_descriptor(history, history.definition.to_dict())
    history.registry.update_declarations(replace(
        history.definition, checkpoint_save={"config_paths": []}, execution_class="non_working",
    ))
    with _reader(history) as reader, pytest.raises(ContractValidationError) as caught:
        _producer(reader, history, config={**history.node["config"], "value": "tampered"})
    assert caught.value.reason_code == "storage_contract_violation"
