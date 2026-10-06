"""Transactional SQLite contracts using public v2 example records."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.storage import SqliteStore


TEST_DIR = Path(__file__).resolve().parent
EXAMPLE = TEST_DIR.parent / "examples" / "contracts_v2" / "success.json"
IDENTITY = {
    "visible_message": "visible_message_id",
    "node_input": "input_id",
    "input_snapshot": "snapshot_id",
    "run_record": "run_id",
    "turn": "turn_id",
    "candidate_group": "candidate_group_id",
    "candidate_selection": "candidate_group_id",
}


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def identity(kind, record):
    field = IDENTITY[kind]
    return {field: record[field]}


@pytest.fixture
def db_path():
    with TemporaryDirectory(prefix="storage-contract-", dir=TEST_DIR) as directory:
        yield Path(directory) / "records.sqlite3"


@pytest.fixture
def example():
    bundle = loads_strict(EXAMPLE.read_text(encoding="utf-8"))
    return {
        kind: [validate_record(kind, row) for row in rows]
        for kind, rows in bundle.items()
    }


def pre_dispatch(example):
    run = deepcopy(example["run_record"][0])
    run.update(status="prepared", revision=1, result_turn_id=None)
    return [
        (kind, validate_record(kind, row))
        for kind, row in (
            ("visible_message", example["visible_message"][0]),
            ("node_input", example["node_input"][0]),
            ("input_snapshot", example["input_snapshot"][0]),
            ("run_record", run),
        )
    ]


def successful_archive(example):
    return [
        (kind, deepcopy(example[kind][0]))
        for kind in ("run_record", "turn", "candidate_group")
    ]


def second_candidate(example):
    run_id, turn_id = uid(0x110), uid(0x111)
    succeeded = deepcopy(example["run_record"][0])
    succeeded.update(run_id=run_id, result_turn_id=turn_id)
    prepared = deepcopy(succeeded)
    prepared.update(status="prepared", revision=1, result_turn_id=None)

    turn = deepcopy(example["turn"][0])
    changed = {}

    def distinct_ids(value):
        if isinstance(value, list):
            return [distinct_ids(item) for item in value]
        if isinstance(value, dict):
            for key in ("message_id", "request_id", "tool_call_id", "tool_execution_id"):
                if key in value:
                    changed.setdefault(value[key], uid(0x120 + len(changed)))
            return {key: distinct_ids(item) for key, item in value.items()}
        return value

    distinct_ids(turn)

    def replace_ids(value):
        if isinstance(value, list):
            return [replace_ids(item) for item in value]
        if isinstance(value, dict):
            return {key: replace_ids(item) for key, item in value.items()}
        return changed.get(value, value) if isinstance(value, str) else value

    turn = replace_ids(turn)
    turn.update(turn_id=turn_id, run_id=run_id)
    group = deepcopy(example["candidate_group"][0])
    group["candidate_refs"].append({"run_id": run_id, "turn_id": turn_id})
    for kind, row in (("run_record", prepared), ("run_record", succeeded),
                      ("turn", turn), ("candidate_group", group)):
        validate_record(kind, row)
    return prepared, [
        ("run_record", succeeded), ("turn", turn), ("candidate_group", group),
    ]


def test_pre_dispatch_fault_rolls_back_every_record(db_path, example):
    points = []

    def fail_after_write(point):
        points.append(point)
        if point == "after_first_write":
            raise RuntimeError("injected write failure")

    records = pre_dispatch(example)
    with closing(SqliteStore(db_path, fault_injector=fail_after_write)) as store:
        with pytest.raises(RuntimeError, match="injected write failure"):
            store.save_pre_dispatch(records, "dispatch-fault")
        assert "after_first_write" in points
        assert store.read_bundle() == {}
    with closing(SqliteStore(db_path)) as reopened:
        assert reopened.read_bundle() == {}


def test_pre_dispatch_replay_returns_same_records_and_rejects_changed_body(db_path, example):
    records = pre_dispatch(example)
    with closing(SqliteStore(db_path)) as store:
        first = store.save_pre_dispatch(records, "dispatch-1")
        assert first == [row for _, row in records]
        assert store.save_pre_dispatch(deepcopy(records), "dispatch-1") == first
        for kind, row in records:
            assert store.list_records(kind) == [row]

        changed = deepcopy(records)
        changed[-1][1]["initial_budget"]["max_model_requests"] += 1
        with pytest.raises(ContractValidationError):
            store.save_pre_dispatch(changed, "dispatch-1")
        assert store.get_record("run_record", identity(*records[-1])) == records[-1][1]


def test_reopen_reads_ui_user_input_snapshot_and_prepared_run(db_path, example):
    records = pre_dispatch(example)
    with closing(SqliteStore(db_path)) as store:
        store.save_pre_dispatch(records, "dispatch-1")
    with closing(SqliteStore(db_path)) as reopened:
        bundle = reopened.read_bundle()
        for kind, row in records:
            assert reopened.get_record(kind, identity(kind, row)) == row
            assert reopened.list_records(kind) == [row]
            assert canonical_bytes(bundle[kind]) == canonical_bytes([row])
        assert bundle["visible_message"][0]["role"] == "user"
        assert bundle["run_record"][0]["status"] == "prepared"


def test_archive_is_atomic_idempotent_and_does_not_select(db_path, example):
    archive = successful_archive(example)
    with closing(SqliteStore(db_path)) as store:
        store.save_pre_dispatch(pre_dispatch(example), "dispatch-1")
        first = store.archive_success(archive, "archive-1")
        assert first == [row for _, row in archive]
        assert store.archive_success(deepcopy(archive), "archive-1") == first
        for kind, row in archive:
            assert store.get_record(kind, identity(kind, row)) == row
        assert store.list_records("candidate_selection") == []

        changed = deepcopy(archive)
        changed[0][1]["revision"] += 1
        with pytest.raises(ContractValidationError):
            store.archive_success(changed, "archive-1")
        assert store.get_record("run_record", identity(*archive[0])) == archive[0][1]

    with closing(SqliteStore(db_path)) as reopened:
        for kind, row in archive:
            assert reopened.get_record(kind, identity(kind, row)) == row
        assert reopened.list_records("candidate_selection") == []


def test_immutable_input_cannot_be_overwritten_under_new_key(db_path, example):
    records = pre_dispatch(example)
    with closing(SqliteStore(db_path)) as store:
        store.save_pre_dispatch(records, "dispatch-1")
        changed = deepcopy(example["visible_message"][0])
        changed["payload"]["text"] = "Different UI request."
        validate_record("visible_message", changed)
        with pytest.raises(ContractValidationError):
            store.save_pre_dispatch([("visible_message", changed)], "dispatch-2")
        assert store.get_record("visible_message", identity("visible_message", changed)) == records[0][1]


def test_selection_initial_revision_and_cas_conflict(db_path, example):
    with closing(SqliteStore(db_path)) as store:
        store.save_pre_dispatch(pre_dispatch(example), "dispatch-1")
        store.archive_success(successful_archive(example), "archive-1")
        first = deepcopy(example["candidate_selection"][0])
        assert store.select_candidate(first, expected_revision=0) == first
        next_selection = deepcopy(first)
        next_selection.update(selected_turn_id=None, revision=2)
        assert store.select_candidate(next_selection, expected_revision=1) == next_selection

        stale = deepcopy(first)
        stale["revision"] = 2
        with pytest.raises(ContractValidationError):
            store.select_candidate(stale, expected_revision=1)
        assert store.get_record("candidate_selection", identity("candidate_selection", first)) == next_selection


def test_candidate_group_preserves_existing_member_when_appending(db_path, example):
    prepared, second = second_candidate(example)
    with closing(SqliteStore(db_path)) as store:
        store.save_pre_dispatch(pre_dispatch(example), "dispatch-1")
        store.archive_success(successful_archive(example), "archive-1")
        store.save_pre_dispatch([("run_record", prepared)], "dispatch-2")

        dropped_old = deepcopy(second)
        dropped_old[-1][1]["candidate_refs"] = dropped_old[-1][1]["candidate_refs"][1:]
        with pytest.raises(ContractValidationError):
            store.archive_success(dropped_old, "archive-drop-old")
        assert store.list_records("candidate_group") == example["candidate_group"]
        assert store.get_record("run_record", identity("run_record", prepared)) == prepared
        assert store.list_records("turn") == example["turn"]

        store.archive_success(second, "archive-2")
        assert store.list_records("candidate_group") == [second[-1][1]]
        assert store.list_records("turn") == [example["turn"][0], second[1][1]]


def test_cross_run_closure_rejects_without_partial_success(db_path, example):
    prepared, second = second_candidate(example)
    second[1][1]["run_id"] = example["run_record"][0]["run_id"]
    validate_record("turn", second[1][1])
    with closing(SqliteStore(db_path)) as store:
        store.save_pre_dispatch(pre_dispatch(example), "dispatch-1")
        store.archive_success(successful_archive(example), "archive-1")
        store.save_pre_dispatch([("run_record", prepared)], "dispatch-2")
        with pytest.raises(ContractValidationError):
            store.archive_success(second, "archive-cross-run")
        assert store.get_record("run_record", identity("run_record", prepared)) == prepared
        assert store.list_records("turn") == example["turn"]
        assert store.list_records("candidate_group") == example["candidate_group"]


def test_archive_fault_after_first_write_leaves_no_partial_success(db_path, example):
    points = []
    armed = False

    def fail_after_write(point):
        if armed:
            points.append(point)
            if point == "after_first_write":
                raise RuntimeError("injected archive failure")

    prepared = pre_dispatch(example)
    with closing(SqliteStore(db_path, fault_injector=fail_after_write)) as store:
        store.save_pre_dispatch(prepared, "dispatch-1")
        armed = True
        with pytest.raises(RuntimeError, match="injected archive failure"):
            store.archive_success(successful_archive(example), "archive-fault")
        assert "after_first_write" in points
        assert store.list_records("turn") == []
        assert store.list_records("candidate_group") == []
        assert store.get_record("run_record", identity(*prepared[-1])) == prepared[-1][1]
    with closing(SqliteStore(db_path)) as reopened:
        assert reopened.list_records("turn") == []
        assert reopened.list_records("candidate_group") == []
        assert reopened.get_record("run_record", identity(*prepared[-1])) == prepared[-1][1]
