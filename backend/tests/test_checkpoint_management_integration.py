"""Management-host checkpoint fork wiring without execution ownership."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

from phase1_agent import graph_service as graph_service_module
from phase1_agent.checkpoint_repository import CheckpointRepository
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_records import uuid_value
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow_host import WorkflowHost


RESULT_FIELDS = {
    "target_session_id", "target_checkpoint_id", "target_commit_id", "target_workflow_ref_id",
}
DETAIL_FIELDS = {
    "schema_version", "checkpoint_id", "workflow_definition_id",
    "workflow_session_id", "fixed_definition_revision", "parent_checkpoint_id",
}


def identity():
    return str(uuid4())


def _save_empty_definition(application):
    workflow_id = identity()
    platform = application.query("platform")
    document = {
        "schema_version": 3,
        "workflow_definition_id": workflow_id,
        "revision": 1,
        "name": "Management checkpoint integration",
        "nodes": [],
        "edges": [],
        "object_bindings": [],
        "package_lock": deepcopy(platform["execution_package_lock"]),
        "execution_roots": [],
        "control_edges": [],
        "event_bindings": [],
    }
    accepted = application.command("definition.update", {
        "workflow_definition_id": workflow_id,
        "document": document,
        "expected_edit_sequence": 0,
        "idempotency_key": identity(),
    })["result"]
    assert accepted["outcome"] == "saved"
    assert accepted["definition_revision"] == 1
    return workflow_id


def _insert_empty_context_checkpoint(database, session):
    checkpoint_id = identity()
    state = {
        "configuration": {
            "workflow_definition_id": session["workflow_definition_id"],
            "fixed_definition_revision": session["definition_revision"],
            "nodes": {},
        },
        "agent_contexts": {},
    }
    store = SqliteStore(database)
    connection = store._connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        repository = CheckpointRepository(
            connection,
            workflow_definition_id=session["workflow_definition_id"],
            workflow_session_id=session["workflow_session_id"],
            fixed_definition_revision=session["definition_revision"],
        )
        repository.insert({
            "schema_version": 2,
            "checkpoint_id": checkpoint_id,
            "workflow_definition_id": session["workflow_definition_id"],
            "workflow_session_id": session["workflow_session_id"],
            "fixed_definition_revision": session["definition_revision"],
            "parent_checkpoint_id": None,
            "state_storage": {"mode": "full", "state": state},
        })
        connection.execute("COMMIT")
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        store.close()
    return checkpoint_id


def test_management_host_forks_a_checkpoint_with_existing_receipt_and_queries(tmp_path, monkeypatch):
    database = tmp_path / "checkpoint-management-integration.sqlite"

    def reject_execution_initialization(*args, **kwargs):
        raise AssertionError("management checkpoint flow must not initialize execution ownership")

    monkeypatch.setattr(GraphWorkflowService, "__init__", reject_execution_initialization)
    monkeypatch.setattr(graph_service_module, "_GraphLease", reject_execution_initialization)
    monkeypatch.setattr(graph_service_module, "ThreadPoolExecutor", reject_execution_initialization)

    host = WorkflowHost(database)
    try:
        application = GraphApplication(host)
        workflow_id = _save_empty_definition(application)
        source = application.command("session.create", {
            "workflow_definition_id": workflow_id,
            "definition_revision": 1,
            "idempotency_key": identity(),
        })["result"]
        session_id = source["workflow_session_id"]
        checkpoint_id = _insert_empty_context_checkpoint(database, source)

        source_before = application.query("session.read", {"session_id": session_id})
        definition_before = application.query("definition.read", {"identity": workflow_id})
        request = {
            "session_id": session_id,
            "checkpoint_id": checkpoint_id,
            "expected_session_revision": source_before["revision"],
            "expected_data_revision": source_before["data_revision"],
            "expected_head_revision": source_before["head_revision"],
            "idempotency_key": identity(),
        }
        packet = application.command("checkpoint.fork", request)
        result = packet["result"]
        assert set(result) == RESULT_FIELDS
        assert all(uuid_value(result[field]) for field in RESULT_FIELDS)
        assert result["target_session_id"] != session_id
        assert packet["receipt"]["operation"] == "checkpoint.fork"
        assert packet["receipt"]["operation_scope"] == "management"
        assert packet["receipt"]["idempotency_key"] == request["idempotency_key"]
        assert packet["receipt"]["target"] == {"session_id": session_id}
        assert packet["receipt"]["accepted"] == result

        matched = application.query("receipt.read", {
            "operation": "checkpoint.fork",
            "parameters": request,
        })
        assert matched["outcome"] == "matched"
        assert matched["reason_code"] == "receipt_matched"
        assert matched["result"] == result
        assert matched["receipt"]["accepted"] == result

        target_checkpoints = application.query("checkpoint.list", {
            "session_id": result["target_session_id"],
        })
        assert target_checkpoints == [{
            "checkpoint_id": result["target_checkpoint_id"],
            "parent_checkpoint_id": None,
        }]
        target_detail = application.query("checkpoint.read", {
            "session_id": result["target_session_id"],
            "checkpoint_id": result["target_checkpoint_id"],
        })
        assert set(target_detail) == DETAIL_FIELDS
        assert target_detail == {
            "schema_version": 2,
            "checkpoint_id": result["target_checkpoint_id"],
            "workflow_definition_id": workflow_id,
            "workflow_session_id": result["target_session_id"],
            "fixed_definition_revision": 1,
            "parent_checkpoint_id": None,
        }

        assert application.query("session.read", {"session_id": session_id}) == source_before
        assert application.query("definition.read", {"identity": workflow_id}) == definition_before
        sessions_before_replay = application.query("definition.sessions", {
            "workflow_definition_id": workflow_id,
        })
        assert len(sessions_before_replay) == 2
        assert sum(row["workflow_session_id"] == result["target_session_id"]
                   for row in sessions_before_replay) == 1

        replay = application.command("checkpoint.fork", deepcopy(request))
        assert replay == packet
        sessions_after_replay = application.query("definition.sessions", {
            "workflow_definition_id": workflow_id,
        })
        assert len(sessions_after_replay) == 2
        assert sum(row["workflow_session_id"] == result["target_session_id"]
                   for row in sessions_after_replay) == 1
        assert host._graph is None
    finally:
        host.close()


def test_run_history_query_is_available_from_the_management_host_after_execution_closes(tmp_path, monkeypatch):
    from test_graph_service import create, run, text_graph

    database = tmp_path / "management-run-history.sqlite"
    with closing(GraphWorkflowService(database)) as execution:
        document = text_graph(execution.registry)
        source = create(execution, document)
        completed = run(execution, source)
        chain_id = completed["chains"][-1]["chain_run_id"]
        session_id = completed["workflow_session_id"]
    # History is persisted data; reopen the manager after the live execution owner exits.
    host = WorkflowHost(database)

    def reject_execution_initialization(*args, **kwargs):
        raise AssertionError("run.read must not construct an execution owner")

    monkeypatch.setattr(GraphWorkflowService, "__init__", reject_execution_initialization)
    monkeypatch.setattr(graph_service_module, "_GraphLease", reject_execution_initialization)
    monkeypatch.setattr(graph_service_module, "ThreadPoolExecutor", reject_execution_initialization)
    try:
        history = GraphApplication(host).query("run.read", {
            "session_id": session_id,
            "chain_id": chain_id,
        })
        assert history["chain"]["chain_run_id"] == chain_id
        assert history["chain"]["status"] == "succeeded"
        assert history["node_runs"]
        assert host._graph is None
    finally:
        host.close()
