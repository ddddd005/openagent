"""Current context and output configuration can be saved before execution exists."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphManagementService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow_host import WorkflowHost

from test_graph_management_split import _reject_execution_initialization
from test_graph_service import node


def _assert_configuration_only(owner, database):
    if isinstance(owner, WorkflowHost):
        assert owner._graph is None
    assert not (database.parent / (database.name + ".graph.lock")).exists()
    assert not (database.parent / (database.name + ".credentials.sqlite")).exists()
    with closing(SqliteStore(database)) as store:
        for kind in ("workflow_session", "chain_run", "node_run", "workflow_output"):
            assert store._connection.execute(
                "SELECT COUNT(*) FROM records WHERE record_type=?", (kind,),
            ).fetchone()[0] == 0
        for table in ("session_object_bindings", "session_object_values", "session_object_receipts"):
            assert store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("explicit_manager", [False, True])
def test_schema3_context_agent_and_output_save_without_session_or_execution(
        tmp_path, monkeypatch, explicit_manager):
    _reject_execution_initialization(monkeypatch)
    database = tmp_path / "schema3-context-configuration.sqlite"
    owner = GraphManagementService(database) if explicit_manager else WorkflowHost(database)
    with closing(owner):
        manager = owner if explicit_manager else owner.graph_management_service
        app = GraphApplication(owner)
        agent = node(manager.registry, "agents.execute", 8101)
        read = node(manager.registry, "context.output", 8102)
        output = node(manager.registry, "tools.output", 8103)
        read["config"]["object_key"] = "context"
        output["public_outputs"] = ["output"]
        for entry in (agent, read, output):
            entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        document = {
            "schema_version": 3,
            "workflow_definition_id": str(uuid4()),
            "revision": 1,
            "name": "Configure context before execution",
            "nodes": [agent, read, output],
            "edges": [],
            "object_bindings": [ObjectBinding(
                "context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
                registrar_node_id=agent["node_binding_id"],
            ).to_dict()],
            "package_lock": list(manager.registry.execution_package_lock),
            "control_edges": [],
        }
        saved = app.command("definition.update", {
            "workflow_definition_id": document["workflow_definition_id"],
            "document": document,
            "expected_edit_sequence": 0,
            "idempotency_key": str(uuid4()),
        })["result"]

        assert saved["outcome"] == "saved"
        assert saved["session_id"] is None
        assert saved["document"]["schema_version"] == 3
        assert saved["document"]["object_bindings"] == document["object_bindings"]
        assert app.query("definition.read", {
            "identity": document["workflow_definition_id"],
        }) == saved["document"]
        _assert_configuration_only(owner, database)

        configured = deepcopy(saved["document"])
        configured["object_bindings"] = [ObjectBinding(
            "context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
            readers=(read["node_binding_id"],),
            registrar_node_id=agent["node_binding_id"],
        ).to_dict()]
        configured["control_edges"] = [{
            "edge_id": str(uuid4()),
            "source_node_id": agent["node_binding_id"],
            "target_node_id": output["node_binding_id"],
        }]
        configured["event_bindings"] = [{
            "event_id": "test.context-output",
            "schema_version": 1,
            "display_name": "Context output test",
            "audience": "consumer",
            "target_node_ids": [output["node_binding_id"]],
            "payload_schema": {"type": "object"},
        }]
        updated = app.command("definition.update", {
            "workflow_definition_id": document["workflow_definition_id"],
            "document": configured,
            "expected_edit_sequence": saved["edit_sequence"],
            "idempotency_key": str(uuid4()),
        })["result"]

        assert updated["outcome"] == "saved"
        assert updated["session_id"] is None
        assert updated["document"]["schema_version"] == 3
        for field in ("object_bindings", "control_edges", "event_bindings"):
            assert updated["document"][field] == configured[field]
        assert app.query("definition.read", {
            "identity": document["workflow_definition_id"],
        }) == updated["document"]
        assert app.query("definition.sessions", {
            "workflow_definition_id": document["workflow_definition_id"],
        }) == []
        _assert_configuration_only(owner, database)
