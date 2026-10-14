"""Atomic checkpoint forks create independent session-owned prefixes."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_repository import CheckpointRepository
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore
from test_context_native_integration import NativeTransport, native_graph
from test_graph_service import create, run as graph_run, service, text_graph
from test_models_service_integration import ModelDatabaseFixture
from test_success_checkpoints import checkpoint_history
from workflow_test_support import run as native_run


def _fork(service, view, checkpoint_id, *, key=None):
    return service.fork_checkpoint(
        view["workflow_session_id"],
        checkpoint_id=checkpoint_id,
        expected_session_revision=view["revision"],
        expected_data_revision=view["data_revision"],
        expected_head_revision=view["head_revision"],
        idempotency_key=key or str(uuid4()),
    )


def _commit_record(service, session_id):
    with closing(service._store()) as store:
        repo = GraphRecordStore(store)
        ref = next(row for row in repo.rows("workflow_ref")
                   if row["workflow_session_id"] == session_id)
        commit = repo.get("workflow_commit", commit_id=ref["head_commit_id"])
        snapshot = repo.get("state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
        return ref, commit, snapshot


def _source_business_state(service, session_id):
    view = deepcopy(service.get_session(session_id))
    objects = deepcopy(service.get_session_objects(session_id)["objects"])
    history = deepcopy(checkpoint_history(service, view))
    with closing(service._store()) as store:
        receipts = tuple(tuple(row) for row in store._connection.execute(
            "SELECT operation_key,digest,result FROM session_object_receipts "
            "WHERE session_id=? ORDER BY operation_key", (session_id,)))
    return view, objects, history, receipts


def _database_rows(service):
    with closing(service._store()) as store:
        connection = store._connection
        tables = (
            "records", "workflow_checkpoints", "session_object_bindings",
            "session_object_values", "session_object_revision_membership",
            "session_object_receipts", "graph_state_manifests",
            "program_variable_bindings", "program_variable_heads",
            "program_variable_states", "idempotency",
        )
        return {
            table: tuple(sorted((tuple(row) for row in connection.execute(
                f'SELECT * FROM "{table}"')), key=repr))
            for table in tables
        }


def _checkpoint_path(service, view):
    with closing(service._store()) as store:
        connection = store._connection
        connection.execute("BEGIN")
        try:
            repository = CheckpointRepository(
                connection,
                workflow_definition_id=view["workflow_definition_id"],
                workflow_session_id=view["workflow_session_id"],
                fixed_definition_revision=view["definition_revision"],
            )
            latest = repository.latest()
            return [] if latest is None else repository.prefix(latest["checkpoint_id"])
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")


def _materialize_checkpoint(service, view, checkpoint_id):
    with closing(service._store()) as store:
        connection = store._connection
        connection.execute("BEGIN")
        try:
            repository = CheckpointRepository(
                connection,
                workflow_definition_id=view["workflow_definition_id"],
                workflow_session_id=view["workflow_session_id"],
                fixed_definition_revision=view["definition_revision"],
            )
            return repository.materialize(checkpoint_id)
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")


def test_fork_copies_only_selected_prefix_replays_by_key_and_can_continue(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "empty-checkpoint-fork.sqlite")) as service:
        document = text_graph(service.registry)
        source = create(service, document)
        for _ in range(3):
            source = graph_run(service, source)
            assert source["status"] == "succeeded", source["chains"]

        source_before = _source_business_state(service, source["workflow_session_id"])
        source_path = source_before[2]["path"]
        assert len(source_path) == 3
        selected = source_path[1]
        key = str(uuid4())

        result = _fork(service, source, selected["checkpoint_id"], key=key)
        assert set(result) == {
            "target_session_id", "target_checkpoint_id", "target_commit_id",
            "target_workflow_ref_id",
        }
        assert result["target_checkpoint_id"] != selected["checkpoint_id"]
        target = service.get_session(result["target_session_id"])
        target_path = _checkpoint_path(service, target)
        assert len(target_path) == 2
        assert not ({point["checkpoint_id"] for point in target_path}
                    & {point["checkpoint_id"] for point in source_path})
        assert target_path[0]["parent_checkpoint_id"] is None
        assert target_path[0]["state_storage"] == source_path[0]["state_storage"]
        assert target_path[1]["parent_checkpoint_id"] == target_path[0]["checkpoint_id"]
        assert target_path[1]["checkpoint_id"] == result["target_checkpoint_id"]
        assert target_path[1]["state_storage"]["base_checkpoint_id"] == target_path[0]["checkpoint_id"]
        assert target_path[1]["state_storage"]["mode"] == "diff"
        assert target["source"] == {"kind": "new"}
        assert target["chains"] == [] and target["outputs"] == []
        assert target["history_refs"] == [] and target["inherited_history"] == []
        assert target["active_chain_run_id"] is None
        _, commit, snapshot = _commit_record(service, target["workflow_session_id"])
        assert commit["source"] == {"kind": "session_seed", "source": None}
        assert commit["parent_commit_id"] is None
        assert snapshot["history_refs"] == []
        assert result["target_commit_id"] == commit["commit_id"]
        assert result["target_workflow_ref_id"] == _commit_record(
            service, target["workflow_session_id"])[0]["workflow_ref_id"]
        assert _source_business_state(service, source["workflow_session_id"]) == source_before

        replay = _fork(service, source, selected["checkpoint_id"], key=key)
        assert replay == result
        assert len(service.list_sessions(document["workflow_definition_id"])) == 2
        with pytest.raises(ContractValidationError) as conflict:
            _fork(service, source, source_path[2]["checkpoint_id"], key=key)
        assert conflict.value.reason_code == "idempotency_conflict"

        continued = graph_run(service, target)
        assert continued["status"] == "succeeded", continued["chains"]
        continued_path = _checkpoint_path(service, continued)
        assert len(continued_path) == 3
        assert continued_path[-1]["parent_checkpoint_id"] == result["target_checkpoint_id"]
        assert continued_path[-1]["state_storage"]["base_checkpoint_id"] == continued_path[0]["checkpoint_id"]
        assert _source_business_state(service, source["workflow_session_id"]) == source_before


def test_native_context_fork_is_target_owned_and_survives_source_deletion(tmp_path):
    transport = NativeTransport()
    with closing(GraphWorkflowService(
            tmp_path / "native-checkpoint-fork.sqlite",
            public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = native_graph(service, policy=False, once=False)
        source = create(service, document)
        for index in range(3):
            source = native_run(service, source, f"Source round {index + 1}.")
            assert source["status"] == "succeeded", source["chains"]

        source_before = _source_business_state(service, source["workflow_session_id"])
        source_path = source_before[2]["path"]
        assert len(source_path) == 3
        selected_checkpoint = source_path[1]
        selected_state = _materialize_checkpoint(
            service, source, selected_checkpoint["checkpoint_id"])
        context_key = "context-a"
        assert context_key in selected_state["agent_contexts"]
        source_object = source["objects"][context_key]

        result = _fork(service, source, selected_checkpoint["checkpoint_id"])
        target = service.get_session(result["target_session_id"])
        target_path = _checkpoint_path(service, target)
        assert len(target_path) == 2
        assert target_path[-1]["checkpoint_id"] == result["target_checkpoint_id"]
        assert not ({point["checkpoint_id"] for point in target_path}
                    & {point["checkpoint_id"] for point in source_path})
        target_history = checkpoint_history(service, target)
        assert target_history["state"] == selected_state
        assert target_history["collected"] == selected_state
        target_object = target["objects"][context_key]
        assert target_object["value"] == selected_state["agent_contexts"][context_key]["value"]
        assert target_object["binding"] == source_object["binding"]
        assert target_object["revision_id"] != source_object["revision_id"]
        assert target_object["revision"] == 2
        object_view = service.get_session_objects(target["workflow_session_id"])["objects"]
        assert object_view[context_key]["revision_id"] == target_object["revision_id"]
        with closing(service._store()) as store:
            writes = store._connection.execute(
                "SELECT operation_key,result FROM session_object_receipts WHERE session_id=?",
                (target["workflow_session_id"],),
            ).fetchall()
        assert writes == []
        assert _source_business_state(service, source["workflow_session_id"]) == source_before

        target = native_run(service, target, "Continue from the forked context.")
        assert target["status"] == "succeeded", target["chains"]
        continued_history = checkpoint_history(service, target)
        assert len(continued_history["path"]) == 3
        assert continued_history["path"][-1]["parent_checkpoint_id"] == result["target_checkpoint_id"]
        assert continued_history["state"] == continued_history["collected"]
        assert len(transport.calls) == 4
        assert _source_business_state(service, source["workflow_session_id"]) == source_before

        deleted = service.delete_session(
            source["workflow_session_id"], expected_revision=source["revision"],
            idempotency_key=str(uuid4()),
        )
        assert deleted["deleted"]
        surviving = service.get_session(target["workflow_session_id"])
        surviving_history = checkpoint_history(service, surviving)
        assert surviving_history["state"] == surviving_history["collected"]
        assert surviving_history["state"]["agent_contexts"][context_key]["value"] == (
            target["objects"][context_key]["value"])

        reforked = _fork(
            service, surviving, surviving_history["latest"]["checkpoint_id"])
        refork_target = service.get_session(reforked["target_session_id"])
        reforked_history = checkpoint_history(service, refork_target)
        assert reforked_history["state"] == surviving_history["state"]
        assert reforked_history["collected"] == surviving_history["state"]
        assert reforked_history["path"][-1]["checkpoint_id"] == reforked["target_checkpoint_id"]


def test_fork_requires_all_three_cas_values_at_the_service_boundary(service):
    document = text_graph(service.registry)
    source = graph_run(service, create(service, document))
    assert source["status"] == "succeeded", source["chains"]
    checkpoint_id = checkpoint_history(service, source)["latest"]["checkpoint_id"]
    before = _database_rows(service)

    for field in ("expected_session_revision", "expected_data_revision", "expected_head_revision"):
        parameters = {
            "checkpoint_id": checkpoint_id,
            "expected_session_revision": source["revision"],
            "expected_data_revision": source["data_revision"],
            "expected_head_revision": source["head_revision"],
            "idempotency_key": str(uuid4()),
        }
        parameters[field] = None
        with pytest.raises(ContractValidationError) as invalid:
            service.fork_checkpoint(source["workflow_session_id"], **parameters)
        assert invalid.value.reason_code == "invalid_request"
        assert _database_rows(service) == before


def test_fork_rejects_checkpoint_owned_by_another_session_without_writes(service):
    document = text_graph(service.registry)
    source = create(service, document)
    sibling = service.create_session(
        document["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    sibling = graph_run(service, sibling)
    assert sibling["status"] == "succeeded", sibling["chains"]
    foreign_checkpoint = checkpoint_history(service, sibling)["latest"]["checkpoint_id"]
    before = _database_rows(service)

    with pytest.raises(ContractValidationError) as denied:
        _fork(service, source, foreign_checkpoint)

    assert denied.value.reason_code == "checkpoint_owner_denied"
    assert _database_rows(service) == before


def test_checkpoint_fork_before_commit_failure_rolls_back_complete_write_set(tmp_path):
    armed = []

    def fail_before_commit(point):
        if point == "before_commit" and armed:
            armed.pop()
            raise OSError("injected checkpoint fork transaction failure")

    with closing(GraphWorkflowService(
            tmp_path / "checkpoint-fork-rollback.sqlite",
            fault_injector=fail_before_commit)) as service:
        document = text_graph(service.registry)
        source = graph_run(service, create(service, document))
        assert source["status"] == "succeeded", source["chains"]
        source_before = _source_business_state(service, source["workflow_session_id"])
        before = _database_rows(service)
        armed.append(True)

        with pytest.raises(OSError, match="injected checkpoint fork transaction failure"):
            _fork(service, source, source_before[2]["latest"]["checkpoint_id"])

        assert not armed
        assert _database_rows(service) == before
        assert _source_business_state(service, source["workflow_session_id"]) == source_before
