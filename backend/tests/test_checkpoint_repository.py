"""Caller-transaction checkpoint repository tests with isolated SQLite files."""

from copy import deepcopy
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_repository import (
    CheckpointRepository, initialize_checkpoint_tables,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_records import graph_record
from phase1_agent.runtime_checkpoints import make_diff
from phase1_agent.storage import SqliteStore


def identity():
    return str(uuid4())


def encoded(value):
    return canonical_bytes(value).decode("utf-8")


def seed_session(connection, workflow, session, revision=1):
    value = graph_record(
        "workflow_session", workflow_session_id=session, workflow_definition_id=workflow,
        definition_revision=revision, revision=1, source={"kind": "new"}, active_chain_run_id=None,
    )
    connection.execute(
        "INSERT INTO records VALUES(?,?,?,?,?)",
        ("workflow_session", session, encoded(value), 0, "2026-10-13T00:00:00.000Z"),
    )
    return value


def state(owner):
    return {
        "configuration": {
            "workflow_definition_id": owner["workflow_definition_id"],
            "fixed_definition_revision": owner["fixed_definition_revision"],
            "nodes": {},
        },
        "agent_contexts": {},
        "variables": {},
        "chat_contexts": {},
    }


def root(owner):
    return {
        "schema_version": 4,
        "checkpoint_id": identity(),
        **owner,
        "parent_checkpoint_id": None,
        "state_storage": {"mode": "full", "state": state(owner)},
    }


def child(repository, parent, value):
    before = repository.materialize(parent["checkpoint_id"])
    after = deepcopy(before)
    after["agent_contexts"]["context"] = {
        "value": {"messages": [], "layout": [], "accepted_delta_ids": [], "generation": value},
    }
    baseline = repository.prefix(parent["checkpoint_id"])[0]
    return {
        "schema_version": 4,
        "checkpoint_id": identity(),
        **repository.owner,
        "parent_checkpoint_id": parent["checkpoint_id"],
        "state_storage": {
            "mode": "diff",
            "base_checkpoint_id": baseline["checkpoint_id"],
            **make_diff(before, after),
        },
    }


@pytest.fixture
def connection(tmp_path):
    database = tmp_path / "checkpoint-repository.sqlite"
    store = SqliteStore(database)
    connection = store._connection
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='workflow_checkpoints'",
    ).fetchone()["sql"]
    assert "CHECK(schema_version=4)" in table_sql
    try:
        yield connection
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        store.close()


@pytest.fixture
def repository(connection):
    workflow, session = identity(), identity()
    owner = {
        "workflow_definition_id": workflow,
        "workflow_session_id": session,
        "fixed_definition_revision": 1,
    }
    connection.execute("BEGIN IMMEDIATE")
    seed_session(connection, workflow, session)
    return CheckpointRepository(connection, **owner)


def test_constructor_has_no_sql_and_initializer_is_explicit_transactional_ddl():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    queries = []
    connection.set_trace_callback(queries.append)
    try:
        repo = CheckpointRepository(
            connection, workflow_definition_id=identity(),
            workflow_session_id=identity(), fixed_definition_revision=1,
        )
        assert queries == []
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        with pytest.raises(ContractValidationError, match="transaction"):
            initialize_checkpoint_tables(connection)
        connection.execute("BEGIN IMMEDIATE")
        initialize_checkpoint_tables(connection)
        names = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
        assert "workflow_checkpoints" in names
        assert "workflow_history_membership" not in names
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        connection.execute("ROLLBACK")
        assert repo.owner["fixed_definition_revision"] == 1
    finally:
        connection.close()


def test_row_factory_and_owner_contract_are_exact():
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ContractValidationError, match="Row connection"):
            CheckpointRepository(
                connection, workflow_definition_id=identity(),
                workflow_session_id=identity(), fixed_definition_revision=1,
            )
        connection.row_factory = sqlite3.Row
        with pytest.raises(ContractValidationError, match="exact workflow"):
            CheckpointRepository(
                connection, workflow_definition_id=identity(),
                workflow_session_id=identity(), fixed_definition_revision=True,
            )
    finally:
        connection.close()


def test_operations_require_the_caller_owned_transaction(repository):
    point = root(repository.owner)
    repository.connection.execute("COMMIT")
    for action in (
        lambda: repository.get(point["checkpoint_id"]),
        lambda: repository.insert(point),
        repository.latest,
        lambda: repository.materialize(point["checkpoint_id"]),
    ):
        with pytest.raises(ContractValidationError, match="caller-owned transaction"):
            action()


def test_root_and_children_are_immutable_and_materialize_equivalently(repository):
    root_point = root(repository.owner)
    assert repository.insert(root_point) == root_point
    assert repository.insert(deepcopy(root_point)) == root_point
    child_point = child(repository, root_point, 1)
    assert repository.insert(child_point) == child_point
    assert repository.latest() == child_point
    assert repository.prefix(child_point["checkpoint_id"]) == [root_point, child_point]
    result = repository.materialize(child_point["checkpoint_id"])
    assert result["agent_contexts"]["context"]["value"]["generation"] == 1
    result["agent_contexts"].clear()
    assert repository.materialize(child_point["checkpoint_id"])["agent_contexts"]
    changed = deepcopy(root_point)
    changed["state_storage"]["state"]["configuration"]["nodes"][identity()] = {
        "component_id": "test.counter", "component_version": "1", "config": {"count": 4},
    }
    with pytest.raises(ContractValidationError, match="different body"):
        repository.insert(changed)
    with pytest.raises(ContractValidationError, match="already has a full root"):
        repository.insert(root(repository.owner))


def test_latest_returns_the_leaf_only_for_one_complete_linear_history(repository):
    root_point = root(repository.owner)
    repository.insert(root_point)
    left = child(repository, root_point, 1)
    repository.insert(left)
    tip = child(repository, left, 2)
    repository.insert(tip)
    assert repository.latest() == tip
    assert repository.prefix(tip["checkpoint_id"]) == [root_point, left, tip]


def test_latest_reports_an_actual_branch_instead_of_selecting_one(repository):
    root_point = root(repository.owner)
    repository.insert(root_point)
    repository.insert(child(repository, root_point, 1))
    repository.insert(child(repository, root_point, 2))
    with pytest.raises(ContractValidationError) as caught:
        repository.latest()
    assert caught.value.reason_code == "checkpoint_lineage_ambiguous"


def test_missing_parent_wrong_base_and_foreign_owner_do_not_persist(repository):
    root_point = root(repository.owner)
    repository.insert(root_point)
    candidate = child(repository, root_point, 1)
    candidate["checkpoint_id"] = identity()
    candidate["parent_checkpoint_id"] = identity()
    with pytest.raises(ContractValidationError) as caught:
        repository.insert(candidate)
    assert caught.value.reason_code == "runtime_checkpoint_not_found"
    assert repository.get(candidate["checkpoint_id"]) is None

    candidate = child(repository, root_point, 1)
    candidate["checkpoint_id"] = identity()
    candidate["state_storage"]["base_checkpoint_id"] = identity()
    with pytest.raises(ContractValidationError, match="baseline"):
        repository.insert(candidate)
    assert repository.get(candidate["checkpoint_id"]) is None

    other_workflow, other_session = identity(), identity()
    seed_session(repository.connection, other_workflow, other_session)
    foreign_owner = {
        "workflow_definition_id": other_workflow,
        "workflow_session_id": other_session,
        "fixed_definition_revision": 1,
    }
    other = CheckpointRepository(repository.connection, **foreign_owner)
    foreign_root = root(foreign_owner)
    other.insert(foreign_root)
    candidate = child(repository, root_point, 3)
    candidate["checkpoint_id"] = identity()
    candidate["parent_checkpoint_id"] = foreign_root["checkpoint_id"]
    with pytest.raises(ContractValidationError) as caught:
        repository.insert(candidate)
    assert caught.value.status_code == 403
    assert repository.get(candidate["checkpoint_id"]) is None


def test_owner_session_must_exist_and_match_workflow_revision(repository):
    repository.connection.execute(
        "DELETE FROM records WHERE record_type='workflow_session' AND record_id=?",
        (repository.owner["workflow_session_id"],),
    )
    with pytest.raises(ContractValidationError) as caught:
        repository.insert(root(repository.owner))
    assert caught.value.status_code == 404


@pytest.mark.parametrize("corruption", ["duplicate_json_key", "extra_json_field", "noncanonical", "sql_revision"])
def test_sql_and_json_contracts_are_not_silently_repaired(repository, corruption):
    point = root(repository.owner)
    repository.insert(point)
    storage = deepcopy(point["state_storage"])
    if corruption == "duplicate_json_key":
        body = '{"mode":"full","mode":"full","state":' + encoded(storage["state"]) + "}"
    elif corruption == "extra_json_field":
        body = encoded({**storage, "revision": 1})
    elif corruption == "noncanonical":
        body = " " + encoded(storage)
    else:
        body = encoded(storage)
        repository.connection.execute(
            "UPDATE workflow_checkpoints SET fixed_definition_revision=2 WHERE checkpoint_id=?",
            (point["checkpoint_id"],),
        )
    repository.connection.execute(
        "UPDATE workflow_checkpoints SET state_storage_json=? WHERE checkpoint_id=?",
        (body, point["checkpoint_id"]),
    )
    with pytest.raises(ContractValidationError):
        repository.get(point["checkpoint_id"])


def test_unknown_sql_columns_are_rejected(repository):
    point = root(repository.owner)
    repository.insert(point)
    repository.connection.execute("ALTER TABLE workflow_checkpoints ADD COLUMN fallback TEXT")
    with pytest.raises(ContractValidationError, match="columns differ"):
        repository.get(point["checkpoint_id"])


def test_caller_rollback_removes_session_and_all_points(repository):
    root_point = root(repository.owner)
    repository.insert(root_point)
    repository.insert(child(repository, root_point, 1))
    repository.connection.execute("ROLLBACK")
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM workflow_checkpoints",
    ).fetchone()[0] == 0
    assert repository.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == 20


def test_read_only_connection_can_materialize_without_repository_writes(connection):
    workflow, session = identity(), identity()
    owner = {
        "workflow_definition_id": workflow,
        "workflow_session_id": session,
        "fixed_definition_revision": 1,
    }
    connection.execute("BEGIN IMMEDIATE")
    seed_session(connection, workflow, session)
    writer = CheckpointRepository(connection, **owner)
    point = root(owner)
    writer.insert(point)
    connection.execute("COMMIT")
    database_path = connection.execute("PRAGMA database_list").fetchone()["file"]
    reader = sqlite3.connect(
        Path(database_path).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None,
    )
    reader.row_factory = sqlite3.Row
    try:
        reader.execute("PRAGMA query_only=ON")
        reader.execute("BEGIN")
        queries = []
        reader.set_trace_callback(queries.append)
        repo = CheckpointRepository(reader, **owner)
        assert repo.materialize(point["checkpoint_id"]) == state(owner)
        assert repo.latest() == point
        assert all(query.lstrip().startswith("SELECT") for query in queries)
    finally:
        reader.close()
