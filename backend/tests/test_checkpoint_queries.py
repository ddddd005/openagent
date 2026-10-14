"""Management-only checkpoint query contracts over the existing repository."""

from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent import graph_service as graph_service_module
from phase1_agent.checkpoint_repository import CheckpointRepository
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_records import graph_record
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.runtime_checkpoints import make_diff
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow_host import WorkflowHost


def identity():
    return str(uuid4())


def _seed_session(connection, workflow_id, session_id):
    session = graph_record(
        "workflow_session",
        workflow_session_id=session_id,
        workflow_definition_id=workflow_id,
        definition_revision=1,
        revision=1,
        source={"kind": "new"},
        active_chain_run_id=None,
    )
    connection.execute(
        "INSERT INTO records VALUES(?,?,?,?,?)",
        ("workflow_session", session_id, canonical_bytes(session).decode("utf-8"),
         0, "2026-10-14T00:00:00.000Z"),
    )


@pytest.fixture
def checkpoint_database(tmp_path):
    database = tmp_path / "checkpoint-queries.sqlite"
    workflow_id, session_id = identity(), identity()
    other_workflow_id, other_session_id = identity(), identity()
    empty_workflow_id, empty_session_id = identity(), identity()
    owner = {
        "workflow_definition_id": workflow_id,
        "workflow_session_id": session_id,
        "fixed_definition_revision": 1,
    }
    other_owner = {
        "workflow_definition_id": other_workflow_id,
        "workflow_session_id": other_session_id,
        "fixed_definition_revision": 1,
    }
    store = SqliteStore(database)
    connection = store._connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_session(connection, workflow_id, session_id)
        _seed_session(connection, other_workflow_id, other_session_id)
        _seed_session(connection, empty_workflow_id, empty_session_id)
        root_id, child_id, other_root_id = identity(), identity(), identity()
        initial_state = {
            "configuration": {
                "workflow_definition_id": workflow_id,
                "fixed_definition_revision": 1,
                "nodes": {},
            },
            "agent_contexts": {},
        }
        repository = CheckpointRepository(
            connection,
            workflow_definition_id=workflow_id,
            workflow_session_id=session_id,
            fixed_definition_revision=1,
        )
        root = {
            "schema_version": 2,
            **owner,
            "checkpoint_id": root_id,
            "parent_checkpoint_id": None,
            "state_storage": {"mode": "full", "state": initial_state},
        }
        repository.insert(root)
        next_state = deepcopy(initial_state)
        next_state["configuration"]["nodes"][identity()] = {
            "component_id": "tools.text",
            "component_version": "1",
            "config": {"private_test_value": "not returned by checkpoint.read"},
        }
        child = {
            "schema_version": 2,
            **owner,
            "checkpoint_id": child_id,
            "parent_checkpoint_id": root_id,
            "state_storage": {
                "mode": "diff",
                "base_checkpoint_id": root_id,
                **make_diff(initial_state, next_state),
            },
        }
        repository.insert(child)
        other_repository = CheckpointRepository(
            connection,
            workflow_definition_id=other_workflow_id,
            workflow_session_id=other_session_id,
            fixed_definition_revision=1,
        )
        other_repository.insert({
            "schema_version": 2,
            **other_owner,
            "checkpoint_id": other_root_id,
            "parent_checkpoint_id": None,
            "state_storage": {
                "mode": "full",
                "state": {
                    "configuration": {
                        "workflow_definition_id": other_workflow_id,
                        "fixed_definition_revision": 1,
                        "nodes": {},
                    },
                    "agent_contexts": {},
                },
            },
        })
        connection.execute("COMMIT")
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        store.close()
    return {
        "path": database,
        "workflow_id": workflow_id,
        "session_id": session_id,
        "other_session_id": other_session_id,
        "empty_session_id": empty_session_id,
        "root_id": root_id,
        "child_id": child_id,
        "other_root_id": other_root_id,
    }


def _business_snapshot(database):
    connection = sqlite3.connect(database)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )]
        return {
            table: tuple(sorted(
                connection.execute(f'SELECT * FROM "{table}"').fetchall(),
                key=repr,
            ))
            for table in tables
        }
    finally:
        connection.close()


def _assert_error(action, reason, status):
    with pytest.raises(ContractValidationError) as caught:
        action()
    assert caught.value.reason_code == reason
    assert caught.value.status_code == status


def test_checkpoint_queries_use_the_approved_minimal_list_and_detail(checkpoint_database):
    host = WorkflowHost(checkpoint_database["path"])
    try:
        app = GraphApplication(host)
        session_id = checkpoint_database["session_id"]
        root_id, child_id = checkpoint_database["root_id"], checkpoint_database["child_id"]

        listed = app.query("checkpoint.list", {"session_id": session_id})
        assert listed == [
            {"checkpoint_id": root_id, "parent_checkpoint_id": None},
            {"checkpoint_id": child_id, "parent_checkpoint_id": root_id},
        ]
        details = app.query("checkpoint.read", {
            "session_id": session_id,
            "checkpoint_id": child_id,
        })
        assert details == {
            "schema_version": 2,
            "checkpoint_id": child_id,
            "workflow_definition_id": checkpoint_database["workflow_id"],
            "workflow_session_id": session_id,
            "fixed_definition_revision": 1,
            "parent_checkpoint_id": root_id,
        }
        assert set(details) == {
            "schema_version", "checkpoint_id", "workflow_definition_id",
            "workflow_session_id", "fixed_definition_revision", "parent_checkpoint_id",
        }
        assert "state_storage" not in details
        assert "private_test_value" not in repr(details)
        assert dispatch_graph(
            host, "GET", f"/api/graph/sessions/{session_id}/checkpoints",
        ) == (200, listed)
        assert dispatch_graph(
            host, "GET", f"/api/graph/sessions/{session_id}/checkpoints/{child_id}",
        ) == (200, details)
        assert dispatch_graph(host, "POST", "/api/graph/queries", {
            "operation": "checkpoint.list",
            "parameters": {"session_id": session_id},
        }) == (200, listed)
        assert app.query("checkpoint.list", {
            "session_id": checkpoint_database["empty_session_id"],
        }) == []
    finally:
        host.close()


def test_checkpoint_queries_bind_owner_and_preserve_existing_not_found_errors(checkpoint_database):
    host = WorkflowHost(checkpoint_database["path"])
    app = GraphApplication(host)
    try:
        session_id = checkpoint_database["session_id"]
        _assert_error(
            lambda: app.query("checkpoint.read", {
                "session_id": session_id,
                "checkpoint_id": checkpoint_database["other_root_id"],
            }),
            "checkpoint_owner_denied",
            403,
        )
        _assert_error(
            lambda: app.query("checkpoint.read", {
                "session_id": session_id,
                "checkpoint_id": identity(),
            }),
            "not_found",
            404,
        )
        _assert_error(
            lambda: app.query("checkpoint.list", {"session_id": identity()}),
            "not_found",
            404,
        )
        assert app.query("checkpoint.list", {
            "session_id": checkpoint_database["other_session_id"],
        }) == [{"checkpoint_id": checkpoint_database["other_root_id"],
                "parent_checkpoint_id": None}]
        consumer = GraphApplication(host, scope="consumer")
        _assert_error(
            lambda: consumer.query("checkpoint.list", {"session_id": session_id}),
            "application_scope_denied",
            403,
        )
        _assert_error(
            lambda: consumer.query("checkpoint.read", {
                "session_id": session_id,
                "checkpoint_id": checkpoint_database["child_id"],
            }),
            "application_scope_denied",
            403,
        )
    finally:
        host.close()


def test_checkpoint_reads_do_not_construct_execution_or_write_business_data(
    checkpoint_database, monkeypatch,
):
    database = checkpoint_database["path"]
    before = _business_snapshot(database)

    def reject_execution_construction(*args, **kwargs):
        raise AssertionError("checkpoint queries must not initialize graph execution")

    monkeypatch.setattr(GraphWorkflowService, "__init__", reject_execution_construction)
    monkeypatch.setattr(graph_service_module, "_GraphLease", reject_execution_construction)
    monkeypatch.setattr(graph_service_module, "ThreadPoolExecutor", reject_execution_construction)
    host = WorkflowHost(database)
    try:
        assert host._graph is None
        app = GraphApplication(host)
        app.query("checkpoint.list", {"session_id": checkpoint_database["session_id"]})
        app.query("checkpoint.read", {
            "session_id": checkpoint_database["session_id"],
            "checkpoint_id": checkpoint_database["root_id"],
        })
        assert dispatch_graph(
            host, "GET",
            f"/api/graph/sessions/{checkpoint_database['session_id']}/checkpoints",
        )[0] == 200
        assert host._graph is None
        assert _business_snapshot(database) == before
    finally:
        host.close()
