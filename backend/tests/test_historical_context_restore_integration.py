"""Historical native proof gates the uninstalled logical restore transaction."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.historical_context_reader import validate_historical_context_object
from phase1_agent.session_objects import SessionObjectStore
from historical_context_test_support import NativeHistoryFixture
from test_checkpoint_history_integration import transaction


@pytest.fixture
def native_history(tmp_path):
    fixture = NativeHistoryFixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()


def evidence(connection):
    return (
        [tuple(row) for row in connection.execute(
            "SELECT * FROM records WHERE record_type IN "
            "('chain_run','node_run','workflow_output') ORDER BY record_type,record_id")],
        [tuple(row) for row in connection.execute(
            "SELECT * FROM workflow_runtime_facts ORDER BY fact_id")],
        [tuple(row) for row in connection.execute(
            "SELECT * FROM workflow_current_configuration ORDER BY workflow_definition_id")],
    )


def restore(fixture, checkpoint, *, session_id=None):
    sid = fixture.sid if session_id is None else session_id
    reader = fixture.reader(fixture.connection, checkpoint=checkpoint, workflow_session_id=sid)
    state = reader.checkpoint_state
    return SessionObjectStore(fixture.store, fixture.types).restore_logical(
        sid, fixture.document, state["objects"], object_keys=["context"],
        history_authority=reader)


def test_logical_native_restore_proves_old_state_without_rewriting_execution(native_history):
    fixture = native_history
    objects = SessionObjectStore(fixture.store, fixture.types)
    before = objects.current(fixture.sid)["context"]
    original = evidence(fixture.connection)
    initial, earlier = fixture.checkpoints[:2]
    with transaction(fixture.connection):
        repo = fixture.repository(fixture.connection)
        head = repo.get_head()
        if head is None:
            head = repo.initialize_head(initial["checkpoint_id"], workflow_ref_id=str(uuid4()))
        reader = fixture.reader(fixture.connection, checkpoint=earlier)
        selected = reader.checkpoint_state["objects"]["context"]["value"]
        assert validate_historical_context_object(reader, selected, object_key="context") == selected
        receipt = restore(fixture, earlier)[0]
        head = repo.move_head(
            earlier["checkpoint_id"], expected_revision=head["revision"],
            expected_checkpoint_id=head["checkpoint_id"])
        assert receipt["revision"] == before["revision"] + 1
    assert objects.current(fixture.sid)["context"]["value"] == selected
    assert evidence(fixture.connection) == original
    with transaction(fixture.connection):
        assert restore(fixture, earlier) == [receipt]
        repo = fixture.repository(fixture.connection)
        assert repo.materialize(fixture.checkpoint["checkpoint_id"])["objects"]["context"]["value"] != selected
        restored = restore(fixture, initial)[0]
        repo.move_head(
            initial["checkpoint_id"], expected_revision=head["revision"],
            expected_checkpoint_id=earlier["checkpoint_id"])
        assert restored["revision"] == receipt["revision"] + 1
    assert objects.current(fixture.sid)["context"]["value"] == {
        "view_ref": None, "accepted_delta_ids": []}
    assert evidence(fixture.connection) == original


def test_missing_deep_original_fact_blocks_logical_restore_before_any_write(native_history):
    fixture = native_history
    objects = SessionObjectStore(fixture.store, fixture.types)
    with transaction(fixture.connection):
        restore(fixture, fixture.checkpoints[0])
        identity = fixture.rounds[0]["packet"]["receipts"]["fact_ids"][0]
        fixture.connection.execute(
            "DELETE FROM workflow_runtime_facts WHERE fact_id=?", (identity,))
    before = objects.current(fixture.sid)
    original = evidence(fixture.connection)
    statements = []
    fixture.connection.set_trace_callback(statements.append)
    try:
        with pytest.raises(ContractValidationError):
            with transaction(fixture.connection):
                restore(fixture, fixture.checkpoint)
    finally:
        fixture.connection.set_trace_callback(None)
    assert objects.current(fixture.sid) == before
    assert evidence(fixture.connection) == original
    assert not any(statement.lstrip().upper().startswith((
        "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for statement in statements)


def test_native_restore_late_head_conflict_rolls_back_new_object_revision(native_history):
    fixture = native_history
    objects = SessionObjectStore(fixture.store, fixture.types)
    with transaction(fixture.connection):
        restore(fixture, fixture.checkpoints[0])
        repo = fixture.repository(fixture.connection)
        if repo.get_head() is None:
            repo.initialize_head(
                fixture.checkpoints[0]["checkpoint_id"], workflow_ref_id=str(uuid4()))
    before = objects.current(fixture.sid)
    count = fixture.connection.execute("SELECT COUNT(*) FROM session_object_values").fetchone()[0]
    original = evidence(fixture.connection)
    with pytest.raises(ContractValidationError) as rejected:
        with transaction(fixture.connection):
            restore(fixture, fixture.checkpoint)
            fixture.repository(fixture.connection).move_head(
                fixture.checkpoint["checkpoint_id"], expected_revision=999,
                expected_checkpoint_id=fixture.checkpoints[0]["checkpoint_id"])
    assert rejected.value.reason_code == "stale_head_revision"
    assert objects.current(fixture.sid) == before
    assert fixture.connection.execute("SELECT COUNT(*) FROM session_object_values").fetchone()[0] == count
    assert evidence(fixture.connection) == original


def test_self_reported_consumption_cannot_borrow_a_valid_historical_context(native_history):
    fixture = native_history
    objects = SessionObjectStore(fixture.store, fixture.types)
    before = objects.current(fixture.sid)
    with pytest.raises(ContractValidationError):
        with transaction(fixture.connection):
            reader = fixture.reader(fixture.connection)
            selected = deepcopy(reader.checkpoint_state["objects"])
            selected["context"]["value"]["accepted_delta_ids"] = []
            objects.restore_logical(
                fixture.sid, fixture.document, selected, object_keys=["context"],
                history_authority=reader)
    assert objects.current(fixture.sid) == before
