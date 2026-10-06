"""SQLite durability and transaction tests without model or executor dependencies."""

import copy
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from types import MappingProxyType

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import content_digest, loads_strict
from phase1_agent.storage import SqliteStore


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


@pytest.mark.parametrize("field,value", [
    ("content", {"error": "execution did not return answer"}),
    ("model_visible_text", '{"text":"another answer"}'),
])
def test_archive_rejects_inconsistent_final_control_result(database, field, value):
    before, after, _ = batches()
    after[0][1]["messages"][-1]["blocks"][0][field] = value
    store = SqliteStore(database)
    try:
        store.save_pre_dispatch(before, "start")
        with pytest.raises(ContractValidationError, match="Final control"):
            store.archive_success(after, "bad-final")
        assert store.list_records("turn") == []
        assert store.get_record("run_record", {"run_id": uid(7)})["status"] == "prepared"
    finally:
        store.close()


def test_pre_dispatch_rejects_input_borrowed_from_another_session(database):
    before, _, _ = batches()
    store = SqliteStore(database)
    try:
        store.save_pre_dispatch(before, "start")
        snapshot = copy.deepcopy(before[2][1])
        snapshot.update(snapshot_id=uid(610), workflow_session_id=uid(600))
        run = copy.deepcopy(before[3][1])
        run.update(run_id=uid(611), workflow_session_id=uid(600), snapshot_id=uid(610))
        with pytest.raises(ContractValidationError, match="another workflow session"):
            store.save_pre_dispatch([("input_snapshot", snapshot), ("run_record", run)], "cross-session")
        assert len(store.list_records("input_snapshot")) == 1
        assert len(store.list_records("run_record")) == 1
    finally:
        store.close()


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


@pytest.fixture
def database():
    with TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
        yield Path(directory) / "records.sqlite"


def batches():
    bundle = loads_strict(EXAMPLE.read_text(encoding="utf-8"))
    prepared = copy.deepcopy(bundle["run_record"][0])
    prepared.update(status="prepared", revision=1, result_turn_id=None)
    before = [
        ("visible_message", bundle["visible_message"][0]),
        ("node_input", bundle["node_input"][0]),
        ("input_snapshot", bundle["input_snapshot"][0]),
        ("run_record", prepared),
    ]
    after = [
        ("turn", bundle["turn"][0]),
        ("candidate_group", bundle["candidate_group"][0]),
        ("run_record", bundle["run_record"][0]),
    ]
    return before, after, bundle["candidate_selection"][0]


def sibling(before, after):
    second_input = copy.deepcopy(before[1][1])
    second_input["input_id"] = uid(101)
    second_snapshot = copy.deepcopy(before[2][1])
    second_snapshot.update(snapshot_id=uid(102), input_id=uid(101))
    second_prepared = copy.deepcopy(before[3][1])
    second_prepared.update(run_id=uid(103), input_id=uid(101), snapshot_id=uid(102),
                           source_run_id=None)
    second_success = copy.deepcopy(after[2][1])
    second_success.update(run_id=uid(103), input_id=uid(101), snapshot_id=uid(102),
                          source_run_id=None, result_turn_id=uid(104))
    second_turn = copy.deepcopy(after[0][1])
    second_turn.update(turn_id=uid(104), run_id=uid(103), input_id=uid(101), snapshot_id=uid(102))
    second_group = copy.deepcopy(after[1][1])
    second_group["candidate_refs"].append({"run_id": uid(103), "turn_id": uid(104)})
    return (
        [("node_input", second_input), ("input_snapshot", second_snapshot),
         ("run_record", second_prepared)],
        [("turn", second_turn), ("candidate_group", second_group),
         ("run_record", second_success)],
    )


def test_pre_dispatch_failure_rolls_back_all_writes_and_can_retry(database):
    before, _, _ = batches()
    fired = []

    def fail_once(point):
        if point == "after_first_write" and not fired:
            fired.append(point)
            raise RuntimeError("injected")

    store = SqliteStore(database, fault_injector=fail_once)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            store.save_pre_dispatch(before, "start-1")
        assert store.read_bundle() == {}
        assert store.save_pre_dispatch(before, "start-1") == [value for _, value in before]
    finally:
        store.close()

    reopened = SqliteStore(database)
    try:
        assert reopened.get_record("run_record", {"run_id": uid(7)})["status"] == "prepared"
        assert reopened.save_pre_dispatch(before, "start-1") == [value for _, value in before]
        assert len(reopened.list_records("input_snapshot")) == 1
    finally:
        reopened.close()


def test_before_commit_failure_does_not_leave_receipt_or_partial_archive(database):
    before, after, _ = batches()
    stages = []

    def fail_archive(point):
        stages.append(point)
        if point == "before_commit":
            raise RuntimeError("commit interrupted")

    store = SqliteStore(database)
    try:
        store.save_pre_dispatch(before, "start")
    finally:
        store.close()
    failing = SqliteStore(database, fault_injector=fail_archive)
    try:
        with pytest.raises(RuntimeError, match="commit interrupted"):
            failing.archive_success(after, "finish")
        assert stages == ["after_first_write", "before_commit"]
        assert failing.get_record("run_record", {"run_id": uid(7)})["status"] == "prepared"
        assert failing.list_records("turn") == []
        assert failing.list_records("candidate_group") == []
    finally:
        failing.close()
    retry = SqliteStore(database)
    try:
        assert retry.archive_success(after, "finish") == [value for _, value in after]
        assert retry.get_record("run_record", {"run_id": uid(7)})["status"] == "succeeded"
        assert retry.get_record("turn", {"turn_id": uid(9)}) == after[0][1]
    finally:
        retry.close()


def test_idempotency_is_durable_and_scoped_by_operation(database):
    before, after, _ = batches()
    store = SqliteStore(database)
    try:
        original = store.save_pre_dispatch(before, "shared")
        changed = copy.deepcopy(before)
        changed[1][1]["payload"]["text"] = "different"
        with pytest.raises(ContractValidationError, match="Idempotency"):
            store.save_pre_dispatch(changed, "shared")
        assert store.archive_success(after, "shared") == [value for _, value in after]
        # The pre-dispatch receipt must return the original prepared run, not its
        # subsequently archived state.
        assert store.save_pre_dispatch(before, "shared") == original
        assert store.archive_success(after, "shared") == [value for _, value in after]
        changed_archive = copy.deepcopy(after)
        changed_archive[0][1]["final"]["value"] = {"text": "different"}
        with pytest.raises(ContractValidationError, match="Idempotency"):
            store.archive_success(changed_archive, "shared")
        assert len(store.list_records("turn")) == 1
    finally:
        store.close()


def test_immutable_conflicts_and_invalid_archive_are_atomic(database):
    before, after, _ = batches()
    store = SqliteStore(database)
    try:
        store.save_pre_dispatch(before, "start")
        changed = copy.deepcopy(before)
        changed[2][1]["model_parameters"]["temperature"] = 0.7
        with pytest.raises(ContractValidationError, match="Immutable"):
            store.save_pre_dispatch(changed, "another-start")
        assert store.get_record("input_snapshot", {"snapshot_id": uid(6)}) == before[2][1]

        wrong = copy.deepcopy(after)
        wrong[2][1]["result_turn_id"] = uid(200)
        with pytest.raises(ContractValidationError):
            store.archive_success(wrong, "wrong-result")
        wrong = copy.deepcopy(after)
        wrong[2][1]["status"] = "failed"
        with pytest.raises(ContractValidationError):
            store.archive_success(wrong, "wrong-status")
        wrong = copy.deepcopy(after)
        wrong[0][1]["final"]["message_id"] = uid(201)
        with pytest.raises(ContractValidationError, match="Final"):
            store.archive_success(wrong, "wrong-final")
        assert store.list_records("turn") == []
        store.archive_success(after, "finish")
        altered = copy.deepcopy(after)
        altered[0][1]["final"]["value"] = {"text": "forged"}
        with pytest.raises(ContractValidationError):
            store.archive_success(altered, "new-key")
        assert store.get_record("turn", {"turn_id": uid(9)}) == after[0][1]
    finally:
        store.close()


def test_candidate_group_append_only_does_not_change_selection(database):
    before, after, selection = batches()
    store = SqliteStore(database)
    try:
        store.save_pre_dispatch(before, "start")
        store.archive_success(after, "finish")
        assert store.list_records("candidate_selection") == []
        assert store.select_candidate(selection, 0) == selection

        second_before, second_after = sibling(before, after)
        second_turn = second_after[0][1]
        second_group = second_after[1][1]
        second_success = second_after[2][1]
        store.save_pre_dispatch(second_before, "start-2")
        changed = copy.deepcopy(second_group)
        changed["candidate_refs"] = changed["candidate_refs"][1:]
        with pytest.raises(ContractValidationError, match="append-only"):
            store.archive_success([
                ("turn", second_turn), ("candidate_group", changed),
                ("run_record", second_success),
            ], "invalid-append")
        assert store.get_record("run_record", {"run_id": uid(103)})["status"] == "prepared"
        assert store.list_records("turn") == [after[0][1]]
        store.archive_success(second_after, "finish-2")
        assert store.get_record("candidate_group", {"candidate_group_id": uid(15)}) == second_group
        assert store.get_record("candidate_selection", {"candidate_group_id": uid(15)}) == selection
    finally:
        store.close()


@pytest.mark.parametrize(("field", "forged"), [
    ("payload", {"text": "forged"}),
    ("payload_schema_ref", {"schema_id": "writing_text", "version": 2}),
    ("port_id", "different"),
    ("source", {"kind": "external", "source_ref": "forged"}),
])
def test_forged_candidate_input_is_rejected_even_without_source_run(database, field, forged):
    before, after, _ = batches()
    before[1][1]["source"] = {"kind": "external", "source_ref": "original"}
    second_before, second_after = sibling(before, after)
    second_before[0][1][field] = forged
    store = SqliteStore(database)
    try:
        store.save_pre_dispatch(before, "start")
        store.archive_success(after, "finish")
        store.save_pre_dispatch(second_before, "start-2")
        assert second_after[2][1]["source_run_id"] is None
        with pytest.raises(ContractValidationError, match="input content"):
            store.archive_success(second_after, "forged-candidate")
        assert store.get_record("run_record", {"run_id": uid(103)})["status"] == "prepared"
        assert store.get_record("candidate_group", {"candidate_group_id": uid(15)}) == after[1][1]
    finally:
        store.close()


def test_turn_cannot_join_a_second_candidate_group(database):
    before, after, _ = batches()
    second_before, second_after = sibling(before, after)
    second_after[1][1]["candidate_group_id"] = uid(201)
    store = SqliteStore(database)
    try:
        store.save_pre_dispatch(before, "start")
        store.archive_success(after, "finish")
        store.save_pre_dispatch(second_before, "start-2")
        with pytest.raises(ContractValidationError, match="another candidate group"):
            store.archive_success(second_after, "other-group")
        assert store.list_records("candidate_group") == [after[1][1]]
        assert store.list_records("turn") == [after[0][1]]
    finally:
        store.close()


def test_selection_cas_rejects_stale_revision_and_nonmembers(database):
    before, after, selection = batches()
    store = SqliteStore(database)
    try:
        store.save_pre_dispatch(before, "start")
        store.archive_success(after, "finish")
        with pytest.raises(ContractValidationError):
            store.archive_success([*after, ("candidate_selection", selection)], "with-selection")
        with pytest.raises(ContractValidationError, match="revision"):
            store.select_candidate(selection, 1)
        store.select_candidate(selection, 0)
        changed = {**selection, "selected_turn_id": None, "revision": 2}
        with pytest.raises(ContractValidationError, match="revision"):
            store.select_candidate(changed, 0)
        invalid = {**selection, "selected_turn_id": uid(888), "revision": 2}
        with pytest.raises(ContractValidationError, match="member"):
            store.select_candidate(invalid, 1)
        assert store.select_candidate(changed, 1) == changed
        assert store.get_record("candidate_selection", {"candidate_group_id": uid(15)}) == changed
        assert store.get_record("candidate_group", {"candidate_group_id": uid(15)}) == after[1][1]
    finally:
        store.close()


def test_structural_and_phase_restrictions_and_detached_reads(database):
    before, after, _ = batches()
    store = SqliteStore(database)
    try:
        bad = copy.deepcopy(before)
        bad[3][1]["status"] = "running"
        with pytest.raises(ContractValidationError):
            store.save_pre_dispatch(bad, "bad-status")
        bad = copy.deepcopy(before)
        bad[2][1]["config"]["payload"]["api_key"] = "not persisted"
        with pytest.raises(ContractValidationError):
            store.save_pre_dispatch(bad, "bad-structure")
        bad = copy.deepcopy(before)
        bad[3][1]["snapshot_id"] = uid(800)
        with pytest.raises(ContractValidationError):
            store.save_pre_dispatch(bad, "bad-reference")
        with pytest.raises(ContractValidationError):
            store.save_pre_dispatch([("turn", after[0][1])], "bad-phase")
        assert store.read_bundle() == {}
        store.save_pre_dispatch(before, "start")
        detached = store.read_bundle()
        detached["node_input"][0]["payload"]["text"] = "local change"
        assert store.get_record("node_input", {"input_id": uid(5)}) == before[1][1]
    finally:
        store.close()


def test_mapping_inputs_and_selection_survive_reopen(database):
    before, after, selection = batches()
    store = SqliteStore(database)
    try:
        store.save_pre_dispatch([
            (kind, MappingProxyType(value)) for kind, value in before
        ], "start")
        store.archive_success(after, "finish")
        store.select_candidate(MappingProxyType(selection), 0)
    finally:
        store.close()
    reopened = SqliteStore(database)
    try:
        assert reopened.read_bundle()["turn"] == [after[0][1]]
        assert reopened.list_records("candidate_group") == [after[1][1]]
        assert reopened.get_record("candidate_selection", {"candidate_group_id": uid(15)}) == selection
        assert reopened.archive_success(after, "finish") == [value for _, value in after]
    finally:
        reopened.close()


def test_durable_columns_keep_creation_time_and_versioned_receipts(database):
    before, after, _ = batches()
    store = SqliteStore(database)
    try:
        store.save_pre_dispatch(before, "shared")
        with closing(sqlite3.connect(database)) as connection:
            prepared_time, mutable = connection.execute(
                "SELECT created_at, immutable FROM records WHERE record_type = 'run_record'",
            ).fetchone()
            snapshot_immutable = connection.execute(
                "SELECT immutable FROM records WHERE record_type = 'input_snapshot'",
            ).fetchone()[0]
        store.archive_success(after, "shared")
    finally:
        store.close()
    with closing(sqlite3.connect(database)) as connection:
        archived_time = connection.execute(
            "SELECT created_at FROM records WHERE record_type = 'run_record'",
        ).fetchone()[0]
        receipts = connection.execute(
            "SELECT operation, digest, result_refs FROM idempotency WHERE key = ? "
            "ORDER BY operation", ("shared",),
        ).fetchall()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", prepared_time)
    assert archived_time == prepared_time
    assert mutable == 0 and snapshot_immutable == 1
    assert [row[0] for row in receipts] == ["archive", "pre_dispatch"]
    assert receipts[0][1] == content_digest([[kind, value] for kind, value in after])
    assert receipts[1][1] == content_digest([[kind, value] for kind, value in before])
    assert loads_strict(receipts[0][2]) == [
        ["turn", uid(9)], ["candidate_group", uid(15)], ["run_record", uid(7)],
    ]


def test_storage_imports_no_smolagents():
    source = (Path(__file__).resolve().parents[1] / "src" / "phase1_agent" / "storage.py").read_text(
        encoding="utf-8",
    )
    assert "import smolagents" not in source
    assert "from smolagents" not in source
