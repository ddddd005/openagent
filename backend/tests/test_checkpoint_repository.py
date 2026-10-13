"""Uninstalled repository checks with isolated SQLite fixtures and no services."""

from copy import deepcopy
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_repository import (
    CheckpointRepository, initialize_checkpoint_tables, validate_checkpoint_head,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_records import graph_record
from phase1_agent.runtime_checkpoints import make_diff


def identity():
    return str(uuid4())


def encoded(value):
    return canonical_bytes(value).decode("utf-8")


def seed_record(connection, kind, value):
    field = "workflow_session_id" if kind == "workflow_session" else "chain_run_id"
    connection.execute(
        "INSERT INTO records VALUES(?,?,?,?,?)",
        (kind, value[field], encoded(value), 0, "2026-10-12T00:00:00.000Z"),
    )


def seed_session(connection, workflow, session, revision=1):
    value = graph_record(
        "workflow_session", workflow_session_id=session, workflow_definition_id=workflow,
        definition_revision=revision, revision=1, source={"kind": "new"}, active_chain_run_id=None,
    )
    seed_record(connection, "workflow_session", value)
    return value


def seed_chain(connection, workflow, session, revision=1):
    chain, node, run = identity(), identity(), identity()
    value = graph_record(
        "chain_run", chain_run_id=chain, workflow_session_id=session,
        workflow_definition_id=workflow, definition_revision=revision, status="succeeded",
        revision=2, targets=[node], ordered_nodes=[node], node_run_ids=[run],
        completed_nodes=[node], next_node_index=1, inputs={}, outputs={node: {}}, diagnostic=None,
    )
    seed_record(connection, "chain_run", value)
    return value


def runtime_state(count=0):
    return {
        "schema_version": 1, "kind": "workflow.runtime-state", "node_states": {},
        "program_state": {
            "values": {"count": {"type": "integer", "source": "assignment", "value": count}},
            "data": {},
        },
        "objects": {"counter": {"type_id": "test.counter", "schema_version": 1,
                                "deleted": False, "value": {"count": count}}},
        "history": {"selected_round_id": None, "visible_entry_ids": []},
    }


def initial(owner, state=None):
    return {
        "schema_version": 1, "checkpoint_id": identity(), **owner, "kind": "initial",
        "parent_checkpoint_id": None, "round_id": None, "chain_run_id": None,
        "assistant_entry_id": None,
        "state_storage": {"mode": "full", "state": runtime_state() if state is None else state},
    }


def successful(owner, root, parent, before, after, chain):
    return {
        "schema_version": 1, "checkpoint_id": identity(), **owner, "kind": "successful_round",
        "parent_checkpoint_id": parent["checkpoint_id"], "round_id": identity(),
        "chain_run_id": chain["chain_run_id"], "assistant_entry_id": identity(),
        "state_storage": {
            "mode": "diff", "base_checkpoint_id": root["checkpoint_id"], **make_diff(before, after),
        },
    }


@pytest.fixture
def connection(tmp_path):
    database = tmp_path / "checkpoint-repository.sqlite"
    connection = sqlite3.connect(database, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA user_version=19")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "CREATE TABLE records (record_type TEXT NOT NULL,record_id TEXT NOT NULL,"
        "payload TEXT NOT NULL,immutable INTEGER NOT NULL,created_at TEXT NOT NULL,"
        "PRIMARY KEY(record_type,record_id))"
    )
    initialize_checkpoint_tables(connection)
    connection.execute("COMMIT")
    try:
        yield connection
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


@pytest.fixture
def repository(connection):
    workflow, session = identity(), identity()
    connection.execute("BEGIN IMMEDIATE")
    seed_session(connection, workflow, session)
    return CheckpointRepository(
        connection, workflow_definition_id=workflow,
        workflow_session_id=session, fixed_definition_revision=1,
    )


def branch(repository, count=1, parent=None, root=None):
    root = root or initial(repository.owner)
    if repository.get(root["checkpoint_id"]) is None:
        repository.insert(root)
    parent = parent or root
    before = repository.materialize(parent["checkpoint_id"])
    after = runtime_state(count)
    chain = seed_chain(
        repository.connection, repository.owner["workflow_definition_id"],
        repository.owner["workflow_session_id"], repository.owner["fixed_definition_revision"],
    )
    child = successful(repository.owner, root, parent, before, after, chain)
    repository.insert(child)
    return root, child, after


def test_constructor_has_no_sql_and_does_not_initialize_schema():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    queries = []
    connection.set_trace_callback(queries.append)
    try:
        CheckpointRepository(
            connection, workflow_definition_id=identity(), workflow_session_id=identity(),
            fixed_definition_revision=1,
        )
        assert queries == []
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()


def test_explicit_initializer_requires_transaction_and_does_not_change_version(connection):
    with pytest.raises(ContractValidationError, match="transaction"):
        initialize_checkpoint_tables(connection)
    connection.execute("BEGIN IMMEDIATE")
    initialize_checkpoint_tables(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 19
    assert not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name IN ('workflow_heads','workflow_current_state')",
    ).fetchall()


def test_row_factory_and_owner_are_exact():
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ContractValidationError, match="Row connection"):
            CheckpointRepository(
                connection, workflow_definition_id=identity(), workflow_session_id=identity(),
                fixed_definition_revision=1,
            )
        connection.row_factory = sqlite3.Row
        with pytest.raises(ContractValidationError, match="exact workflow"):
            CheckpointRepository(
                connection, workflow_definition_id=identity(), workflow_session_id=identity(),
                fixed_definition_revision=True,
            )
    finally:
        connection.close()


def test_reads_and_writes_require_one_caller_transaction(repository):
    repository.connection.execute("COMMIT")
    point = initial(repository.owner)
    for action in (
        lambda: repository.get(point["checkpoint_id"]),
        lambda: repository.insert(point),
        repository.get_head,
        lambda: repository.get_membership(identity()),
    ):
        with pytest.raises(ContractValidationError, match="caller-owned transaction"):
            action()


def test_initial_is_immutable_idempotent_and_detached(repository):
    point = initial(repository.owner)
    assert repository.insert(point) == point
    assert repository.insert(deepcopy(point)) == point
    returned = repository.get(point["checkpoint_id"])
    returned["state_storage"]["state"]["objects"]["counter"]["value"]["count"] = 99
    assert repository.materialize(point["checkpoint_id"]) == runtime_state()
    changed = deepcopy(point)
    changed["state_storage"]["state"]["program_state"]["values"]["count"]["value"] = 1
    with pytest.raises(ContractValidationError) as caught:
        repository.insert(changed)
    assert caught.value.reason_code == "checkpoint_immutable_conflict"
    assert repository.get(point["checkpoint_id"]) == point
    with pytest.raises(ContractValidationError, match="already has an initial"):
        repository.insert(initial(repository.owner))


def test_selected_prefix_and_branches_materialize_independently(repository):
    root, left, _ = branch(repository, 10)
    _, right, right_state = branch(repository, 20, root=root)
    _, tip, tip_state = branch(repository, 11, parent=left, root=root)
    assert repository.prefix(tip["checkpoint_id"]) == [root, left, tip]
    assert repository.materialize(tip["checkpoint_id"]) == tip_state
    assert repository.materialize(right["checkpoint_id"]) == right_state
    assert repository.materialize(root["checkpoint_id"]) == runtime_state()


def test_delete_tombstone_and_null_remain_distinct(repository):
    root = initial(repository.owner)
    repository.insert(root)
    before, after = runtime_state(), runtime_state()
    after["objects"]["counter"].update(deleted=True, value=None)
    after["objects"]["nullable"] = {
        "type_id": "test.nullable", "schema_version": 1, "deleted": False, "value": None,
    }
    after["program_state"]["values"] = {}
    chain = seed_chain(
        repository.connection, repository.owner["workflow_definition_id"],
        repository.owner["workflow_session_id"],
    )
    child = successful(repository.owner, root, root, before, after, chain)
    repository.insert(child)
    assert repository.materialize(child["checkpoint_id"]) == after
    assert child["state_storage"]["delete"]["program_values"] == ["count"]


def test_missing_parent_or_wrong_base_does_not_persist_child(repository):
    root, child, _ = branch(repository)
    candidate = deepcopy(child)
    candidate["checkpoint_id"] = identity()
    candidate["parent_checkpoint_id"] = identity()
    with pytest.raises(ContractValidationError) as caught:
        repository.insert(candidate)
    assert caught.value.reason_code == "runtime_checkpoint_not_found"
    assert repository.get(candidate["checkpoint_id"]) is None
    candidate["parent_checkpoint_id"] = root["checkpoint_id"]
    candidate["state_storage"]["base_checkpoint_id"] = identity()
    with pytest.raises(ContractValidationError, match="baseline"):
        repository.insert(candidate)
    assert repository.get(candidate["checkpoint_id"]) is None


@pytest.mark.parametrize("difference", ["session", "workflow", "revision"])
def test_other_owner_or_fixed_revision_cannot_read_or_parent(repository, difference):
    owner = repository.owner
    workflow = identity() if difference == "workflow" else owner["workflow_definition_id"]
    session = identity()
    revision = 2 if difference == "revision" else owner["fixed_definition_revision"]
    seed_session(repository.connection, workflow, session, revision)
    other = CheckpointRepository(
        repository.connection, workflow_definition_id=workflow,
        workflow_session_id=session, fixed_definition_revision=revision,
    )
    foreign = initial(other.owner)
    other.insert(foreign)
    with pytest.raises(ContractValidationError) as caught:
        repository.get(foreign["checkpoint_id"])
    assert caught.value.status_code == 403
    root, candidate, _ = branch(repository)
    candidate["checkpoint_id"] = identity()
    candidate["parent_checkpoint_id"] = foreign["checkpoint_id"]
    candidate["state_storage"]["base_checkpoint_id"] = root["checkpoint_id"]
    with pytest.raises(ContractValidationError) as caught:
        repository.insert(candidate)
    assert caught.value.status_code == 403


def test_corrupt_parent_cycle_and_missing_delete_fail_materialization(repository):
    root, child, _ = branch(repository)
    repository.connection.execute(
        "UPDATE workflow_checkpoints SET parent_checkpoint_id=? WHERE checkpoint_id=?",
        (child["checkpoint_id"], child["checkpoint_id"]),
    )
    with pytest.raises(ContractValidationError, match="cyclic"):
        repository.materialize(child["checkpoint_id"])
    repository.connection.execute(
        "UPDATE workflow_checkpoints SET parent_checkpoint_id=? WHERE checkpoint_id=?",
        (root["checkpoint_id"], child["checkpoint_id"]),
    )
    storage = deepcopy(child["state_storage"])
    storage["delete"]["objects"] = ["absent"]
    repository.connection.execute(
        "UPDATE workflow_checkpoints SET state_storage_json=? WHERE checkpoint_id=?",
        (encoded(storage), child["checkpoint_id"]),
    )
    with pytest.raises(ContractValidationError, match="absent"):
        repository.materialize(child["checkpoint_id"])


@pytest.mark.parametrize("corruption", ["json_duplicate", "json_extra", "noncanonical", "sql_revision"])
def test_sql_and_json_contracts_are_not_silently_repaired(repository, corruption):
    root = initial(repository.owner)
    repository.insert(root)
    storage = deepcopy(root["state_storage"])
    if corruption == "json_duplicate":
        body = '{"mode":"full","mode":"full","state":' + encoded(storage["state"]) + "}"
    elif corruption == "json_extra":
        body = encoded({**storage, "revision": 1})
    elif corruption == "noncanonical":
        body = " " + encoded(storage)
    else:
        body = encoded(storage)
        repository.connection.execute(
            "UPDATE workflow_checkpoints SET fixed_definition_revision=2 WHERE checkpoint_id=?",
            (root["checkpoint_id"],),
        )
    repository.connection.execute(
        "UPDATE workflow_checkpoints SET state_storage_json=? WHERE checkpoint_id=?",
        (body, root["checkpoint_id"]),
    )
    with pytest.raises(ContractValidationError):
        repository.get(root["checkpoint_id"])


def test_unknown_sql_columns_are_rejected(repository):
    root = initial(repository.owner)
    repository.insert(root)
    repository.connection.execute("ALTER TABLE workflow_checkpoints ADD COLUMN fallback TEXT")
    with pytest.raises(ContractValidationError, match="columns differ"):
        repository.get(root["checkpoint_id"])


def test_single_head_uses_existing_records_and_monotonic_cas(repository):
    root, child, _ = branch(repository)
    head_id = identity()
    first = repository.initialize_head(root["checkpoint_id"], workflow_ref_id=head_id)
    original_time = repository.connection.execute(
        "SELECT created_at FROM records WHERE record_type='workflow_ref' AND record_id=?", (head_id,),
    ).fetchone()["created_at"]
    assert first == repository.initialize_head(root["checkpoint_id"], workflow_ref_id=head_id)
    assert first["schema_version"] == 3 and first["revision"] == 1
    moved = repository.move_head(
        child["checkpoint_id"], expected_revision=1, expected_checkpoint_id=root["checkpoint_id"],
    )
    assert moved["revision"] == 2 and moved["workflow_ref_id"] == head_id
    restored = repository.move_head(
        root["checkpoint_id"], expected_revision=2, expected_checkpoint_id=child["checkpoint_id"],
    )
    assert restored["revision"] == 3 and restored["checkpoint_id"] == root["checkpoint_id"]
    assert repository.get(child["checkpoint_id"]) == child
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM records WHERE record_type='workflow_ref'",
    ).fetchone()[0] == 1
    assert repository.connection.execute(
        "SELECT created_at FROM records WHERE record_type='workflow_ref' AND record_id=?", (head_id,),
    ).fetchone()["created_at"] == original_time


@pytest.mark.parametrize("wrong", ["revision", "point"])
def test_stale_head_move_has_no_write(repository, wrong):
    root, child, _ = branch(repository)
    before = repository.initialize_head(root["checkpoint_id"], workflow_ref_id=identity())
    with pytest.raises(ContractValidationError) as caught:
        repository.move_head(
            child["checkpoint_id"], expected_revision=2 if wrong == "revision" else 1,
            expected_checkpoint_id=identity() if wrong == "point" else root["checkpoint_id"],
        )
    assert caught.value.reason_code == "stale_head_revision"
    assert repository.get_head() == before


def test_head_cannot_initialize_at_success_or_reinitialize_identity(repository):
    root, child, _ = branch(repository)
    with pytest.raises(ContractValidationError, match="initial checkpoint"):
        repository.initialize_head(child["checkpoint_id"], workflow_ref_id=identity())
    repository.initialize_head(root["checkpoint_id"], workflow_ref_id=identity())
    with pytest.raises(ContractValidationError, match="initialized differently"):
        repository.initialize_head(root["checkpoint_id"], workflow_ref_id=identity())


def test_duplicate_head_and_legacy_shape_are_rejected(repository):
    root = initial(repository.owner)
    repository.insert(root)
    head = repository.initialize_head(root["checkpoint_id"], workflow_ref_id=identity())
    duplicate = {**head, "workflow_ref_id": identity()}
    repository.connection.execute(
        "INSERT INTO records VALUES(?,?,?,?,?)",
        ("workflow_ref", duplicate["workflow_ref_id"], encoded(duplicate), 0, "original"),
    )
    with pytest.raises(ContractValidationError, match="more than one"):
        repository.get_head()
    repository.connection.execute(
        "DELETE FROM records WHERE record_id=?", (duplicate["workflow_ref_id"],),
    )
    legacy = {**head, "schema_version": 2, "head_commit_id": head["checkpoint_id"]}
    del legacy["checkpoint_id"]
    repository.connection.execute(
        "UPDATE records SET payload=? WHERE record_id=?", (encoded(legacy), head["workflow_ref_id"]),
    )
    with pytest.raises(ContractValidationError, match="Head fields"):
        repository.get_head()


def test_head_sql_body_identity_and_missing_point_fail_without_repair(repository):
    root = initial(repository.owner)
    repository.insert(root)
    head = repository.initialize_head(root["checkpoint_id"], workflow_ref_id=identity())
    changed = {**head, "workflow_ref_id": identity()}
    repository.connection.execute(
        "UPDATE records SET payload=? WHERE record_id=?", (encoded(changed), head["workflow_ref_id"]),
    )
    with pytest.raises(ContractValidationError, match="SQL/body identity"):
        repository.get_head()
    repository.connection.execute(
        "UPDATE records SET payload=? WHERE record_id=?", (encoded(head), head["workflow_ref_id"]),
    )
    repository.connection.execute(
        "DELETE FROM workflow_checkpoints WHERE checkpoint_id=?", (root["checkpoint_id"],),
    )
    with pytest.raises(ContractValidationError, match="missing owned checkpoint"):
        repository.get_head()
    assert repository.connection.execute(
        "SELECT payload FROM records WHERE record_id=?", (head["workflow_ref_id"],),
    ).fetchone()["payload"] == encoded(head)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("revision", True), ("revision", 0),
    ("checkpoint_id", "not-a-uuid"), ("execution_model", "fallback"),
])
def test_head_contract_is_exact(field, value):
    head = {
        "schema_version": 3, "execution_model": "graph", "workflow_ref_id": identity(),
        "workflow_session_id": identity(), "checkpoint_id": identity(), "revision": 1,
    }
    head[field] = value
    with pytest.raises(ContractValidationError):
        validate_checkpoint_head(head)


def test_membership_is_derived_from_selected_prefix_and_not_other_branch(repository):
    root, left, _ = branch(repository, 1)
    _, right, _ = branch(repository, 2, root=root)
    _, tip, _ = branch(repository, 3, root=root, parent=left)
    retained = repository.retain_checkpoint_history(tip["checkpoint_id"])
    assert [row["chain_run_id"] for row in retained] == [left["chain_run_id"], tip["chain_run_id"]]
    assert repository.get_membership(right["chain_run_id"]) is None
    assert repository.retain_checkpoint_history(tip["checkpoint_id"]) == retained


def test_copied_owned_prefix_reads_original_chain_after_source_session_deleted(repository):
    root, child, after = branch(repository)
    original_chain = repository._chain(child["chain_run_id"])
    workflow, target_session = repository.owner["workflow_definition_id"], identity()
    seed_session(repository.connection, workflow, target_session)
    target = CheckpointRepository(
        repository.connection, workflow_definition_id=workflow,
        workflow_session_id=target_session, fixed_definition_revision=1,
    )
    target_root = initial(target.owner)
    target.insert(target_root)
    copied = {**deepcopy(child), **target.owner, "checkpoint_id": identity(),
              "parent_checkpoint_id": target_root["checkpoint_id"]}
    copied["state_storage"]["base_checkpoint_id"] = target_root["checkpoint_id"]
    target.insert(copied)
    target.retain_checkpoint_history(copied["checkpoint_id"])
    repository.connection.execute(
        "DELETE FROM records WHERE record_type='workflow_session' AND record_id=?",
        (repository.owner["workflow_session_id"],),
    )
    assert target.materialize(copied["checkpoint_id"]) == after
    assert target.get_membership(child["chain_run_id"])["workflow_session_id"] == target_session
    assert target._chain(child["chain_run_id"]) == original_chain
    assert original_chain["workflow_session_id"] == repository.owner["workflow_session_id"]


def test_membership_rejects_other_workflow_or_revision(repository):
    owner = repository.owner
    for workflow, revision in ((identity(), 1), (owner["workflow_definition_id"], 2)):
        chain = seed_chain(repository.connection, workflow, identity(), revision)
        with pytest.raises(ContractValidationError) as caught:
            repository._retain_chain(chain["chain_run_id"])
        assert caught.value.status_code == 403
        assert repository.get_membership(chain["chain_run_id"]) is None


def test_membership_corruption_or_deleted_target_is_not_authority(repository):
    _, child, _ = branch(repository)
    repository._retain_chain(child["chain_run_id"])
    repository.connection.execute(
        "UPDATE workflow_history_membership SET workflow_definition_id=? WHERE chain_run_id=?",
        (identity(), child["chain_run_id"]),
    )
    with pytest.raises(ContractValidationError, match="membership body or owner"):
        repository.get_membership(child["chain_run_id"])
    repository.connection.execute(
        "DELETE FROM records WHERE record_type='workflow_session' AND record_id=?",
        (repository.owner["workflow_session_id"],),
    )
    with pytest.raises(ContractValidationError) as caught:
        repository.get_membership(child["chain_run_id"])
    assert caught.value.status_code == 404


def test_missing_original_chain_cannot_be_hidden_by_membership(repository):
    _, child, _ = branch(repository)
    repository._retain_chain(child["chain_run_id"])
    repository.connection.execute(
        "DELETE FROM records WHERE record_type='chain_run' AND record_id=?", (child["chain_run_id"],),
    )
    with pytest.raises(ContractValidationError) as caught:
        repository.get_membership(child["chain_run_id"])
    assert caught.value.status_code == 404


def test_source_chain_sql_body_identity_mismatch_cannot_grant_membership(repository):
    _, child, _ = branch(repository)
    chain = repository._chain(child["chain_run_id"])
    changed = {**chain, "chain_run_id": identity()}
    repository.connection.execute(
        "UPDATE records SET payload=? WHERE record_type='chain_run' AND record_id=?",
        (encoded(changed), child["chain_run_id"]),
    )
    with pytest.raises(ContractValidationError, match="body or mutability"):
        repository._retain_chain(child["chain_run_id"])
    assert repository.get_membership(child["chain_run_id"]) is None


def test_existing_source_session_must_match_immutable_chain_origin(repository):
    workflow, source_session = repository.owner["workflow_definition_id"], identity()
    source = seed_session(repository.connection, workflow, source_session)
    chain = seed_chain(repository.connection, workflow, source_session)
    source["definition_revision"] = 2
    repository.connection.execute(
        "UPDATE records SET payload=? WHERE record_type='workflow_session' AND record_id=?",
        (encoded(source), source_session),
    )
    with pytest.raises(ContractValidationError, match="source session differs"):
        repository._retain_chain(chain["chain_run_id"])
    assert repository.get_membership(chain["chain_run_id"]) is None


def test_caller_rollback_removes_checkpoint_head_and_membership(repository):
    root, child, _ = branch(repository)
    head_id = identity()
    repository.initialize_head(root["checkpoint_id"], workflow_ref_id=head_id)
    repository.retain_checkpoint_history(child["checkpoint_id"])
    repository.move_head(
        child["checkpoint_id"], expected_revision=1, expected_checkpoint_id=root["checkpoint_id"],
    )
    repository.connection.execute("ROLLBACK")
    assert repository.connection.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 0
    assert repository.connection.execute("SELECT COUNT(*) FROM workflow_history_membership").fetchone()[0] == 0
    assert repository.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == 19


def test_read_only_snapshot_never_performs_ddl_or_writes(connection):
    workflow, session = identity(), identity()
    connection.execute("BEGIN IMMEDIATE")
    seed_session(connection, workflow, session)
    repository = CheckpointRepository(
        connection, workflow_definition_id=workflow, workflow_session_id=session,
        fixed_definition_revision=1,
    )
    root = initial(repository.owner)
    repository.insert(root)
    repository.initialize_head(root["checkpoint_id"], workflow_ref_id=identity())
    connection.execute("COMMIT")
    path = connection.execute("PRAGMA database_list").fetchone()["file"]
    reader = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    reader.row_factory = sqlite3.Row
    queries = []
    try:
        reader.execute("PRAGMA query_only=ON")
        reader.execute("BEGIN")
        reader.set_trace_callback(queries.append)
        read_repository = CheckpointRepository(
            reader, workflow_definition_id=workflow, workflow_session_id=session,
            fixed_definition_revision=1,
        )
        assert read_repository.materialize(root["checkpoint_id"]) == runtime_state()
        assert read_repository.get_head()["checkpoint_id"] == root["checkpoint_id"]
        assert read_repository.get_membership(identity()) is None
        assert all(sql.lstrip().startswith("SELECT") for sql in queries)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_repository.move_head(
                root["checkpoint_id"], expected_revision=1, expected_checkpoint_id=root["checkpoint_id"],
            )
    finally:
        reader.close()
