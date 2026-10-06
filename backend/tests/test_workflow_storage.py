"""Workflow storage transactions use public fixture records, never a model."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import canonical_bytes, content_digest, loads_strict
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def batch(bundle, *kinds):
    return [(kind, deepcopy(row)) for kind in kinds for row in bundle[kind]]


def revised(row, **changes):
    value = deepcopy(row)
    value.update(changes)
    return value


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="workflow-storage-", dir=Path(__file__).parent) as directory:
        yield Path(directory) / "workflow.sqlite"


@pytest.fixture
def example():
    return loads_strict(EXAMPLE.read_text(encoding="utf-8"))


def start_batch(example):
    session = example["workflow_session"][0]
    run = revised(example["run_record"][0], status="prepared", revision=1, result_turn_id=None)
    chain = revised(example["chain_run"][0], status="prepared", output_id=None)
    ref = revised(example["visible_message_ref"][0])
    ref["boundary"].update(input_status="pending", chain_run_id=None)
    return [
        *batch(example, "node_definition", "node_binding", "workflow_definition_revision"),
        ("workflow_session", session),
        *batch(example, "node_session", "node_input", "input_snapshot"),
        ("run_record", run), ("chain_run", chain),
        ("workflow_checkpoint", example["workflow_checkpoint"][0]),
        ("visible_message", example["visible_message"][0]),
        ("visible_message_ref", ref),
    ]


def start(store, example):
    return store.save_bundle(
        start_batch(example), "start", operation="session-start",
        expected_session_revisions={uid(4): 0},
    )


def advance(store, example):
    run = revised(example["run_record"][0], status="running", revision=2, result_turn_id=None)
    chain = revised(example["chain_run"][0], status="running", output_id=None)
    ref = revised(example["visible_message_ref"][0])
    ref["boundary"].update(input_status="running")
    records = [
        ("workflow_session", revised(example["workflow_session"][0], revision=2)),
        ("run_record", run), ("chain_run", chain), ("visible_message_ref", ref),
    ]
    store.save_bundle(records, "dispatch", expected_session_revisions={uid(4): 1})
    store.save_bundle([
        ("workflow_session", revised(example["workflow_session"][0], revision=3)),
        ("run_record", revised(run, status="final_ready", revision=3)),
    ], "final-ready", expected_session_revisions={uid(4): 2})


def archive_a(store, example):
    return store.archive_success([
        ("run_record", example["run_record"][0]),
        ("turn", example["turn"][0]),
        ("candidate_group", example["candidate_group"][0]),
    ], "archive-A")


def three_bindings(example):
    bundle = deepcopy(example)
    agent_binding = revised(bundle["node_binding"][0], node_binding_id=uid(301))
    io_definition = revised(bundle["node_definition"][0], component_id=uid(302),
                            kind="io", capabilities=[])
    io_binding = revised(bundle["node_binding"][0], node_binding_id=uid(303),
                         component_id=uid(302))
    io_binding["config"]["owner_component_id"] = uid(302)
    bundle["node_definition"].append(io_definition)
    bundle["node_binding"].extend([agent_binding, io_binding])
    bundle["workflow_definition_revision"][0]["bindings"].extend([uid(301), uid(303)])
    for binding_id, component_id in ((uid(301), uid(1)), (uid(303), uid(302))):
        state = revised(bundle["node_session"][0], node_binding_id=binding_id)
        state["private_data"]["owner_component_id"] = component_id
        bundle["node_session"].append(state)
        node = deepcopy(bundle["workflow_checkpoint"][0]["nodes"][0])
        node["node_binding_id"] = binding_id
        node["private_data"]["owner_component_id"] = component_id
        bundle["workflow_checkpoint"][0]["nodes"].append(node)
    return bundle


def second_agent(example):
    output_a = example["workflow_output"][0]
    input_b = revised(example["node_input"][0], input_id=uid(310),
                      source={"kind": "upstream_output", "output_id": output_a["output_id"]},
                      payload=deepcopy(output_a["payload"]))
    snapshot_b = revised(example["input_snapshot"][0], snapshot_id=uid(311),
                         node_binding_id=uid(301), input_id=uid(310),
                         parent_turn_id=None)
    run_b = revised(example["run_record"][0], run_id=uid(312),
                    node_binding_id=uid(301), input_id=uid(310),
                    snapshot_id=uid(311), status="prepared", revision=1,
                    result_turn_id=None)
    chain = revised(example["chain_run"][0], status="running", output_id=None,
                    node_run_ids=[uid(7), uid(312)])
    return input_b, snapshot_b, run_b, chain


def second_archive(example):
    mapping = {uid(number): uid(400 + index) for index, number in enumerate(
        (10, 16, 17, 21, 22, 23, 24, 25, 26, 27)
    )}

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return mapping.get(value, value) if isinstance(value, str) else value

    turn_b = replace(example["turn"][0])
    turn_b.update(turn_id=uid(313), parent_turn_id=None, run_id=uid(312),
                  snapshot_id=uid(311), input_id=uid(310))
    group_b = revised(example["candidate_group"][0], candidate_group_id=uid(314),
                      node_binding_id=uid(301), logical_input_id=uid(310),
                      parent_turn_id=None, frozen_snapshot_id=uid(311),
                      candidate_refs=[{"run_id": uid(312), "turn_id": uid(313)}])
    run_b = revised(example["run_record"][0], run_id=uid(312),
                    node_binding_id=uid(301), input_id=uid(310),
                    snapshot_id=uid(311), result_turn_id=uid(313))
    return run_b, turn_b, group_b


def version_seed(example):
    session_id = example["workflow_session"][0]["workflow_session_id"]
    operation = {
        "schema_version": 1, "operation_id": uid(1000), "kind": "submit",
        "scope": {"kind": "workflow_session", "id": session_id},
        "target": {"kind": "workflow_session", "id": session_id},
        "idempotency_key": "seed",
        "expected_revisions": [
            {"kind": "workflow_session", "id": session_id, "revision": 1},
        ],
        "payload": {},
    }
    node = example["node_session"][0]
    snapshot = {
        "schema_version": 1, "state_snapshot_id": uid(1001),
        "workflow_session_id": session_id,
        "workflow_definition_id": example["workflow_session"][0]["workflow_definition_id"],
        "definition_revision": 1,
        "node_states": [{
            "node_binding_id": node["node_binding_id"],
            "data_version": node["data_version"],
            "private_data": deepcopy(node["private_data"]),
            "selected_result": None,
        }],
        "selection_refs": [], "visible_message_refs": [], "pending_input_id": None,
    }
    commit = {
        "schema_version": 1, "commit_id": uid(1002),
        "workflow_session_id": session_id, "state_snapshot_id": snapshot["state_snapshot_id"],
        "parent_commit_id": None, "operation_id": operation["operation_id"],
        "source": {"kind": "session_seed", "workflow_session_id": session_id},
    }
    ref = {
        "schema_version": 1, "workflow_ref_id": uid(1003),
        "workflow_session_id": session_id, "head_commit_id": commit["commit_id"],
        "revision": 1,
    }
    return operation, snapshot, commit, ref


def save_version_seed(store, example):
    operation, snapshot, commit, ref = version_seed(example)
    records = [
        ("workflow_operation", operation), ("state_snapshot", snapshot),
        ("workflow_commit", commit), ("workflow_ref", ref),
    ]
    store.save_bundle(records, "seed", operation="version.seed",
                      expected_ref_heads={ref["workflow_ref_id"]: (0, None)})
    return operation, snapshot, commit, ref


def test_atomic_setup_receipts_composite_ids_and_reopen(database, example):
    records = start_batch(example)
    seen = []

    def fault(point):
        seen.append(point)
        if point == "after_first_write":
            raise RuntimeError("injected")

    with closing(SqliteStore(database, fault_injector=fault)) as store:
        with pytest.raises(RuntimeError, match="injected"):
            store.save_bundle(records, "start", operation="session-start",
                              expected_session_revisions={uid(4): 0})
        assert store.read_bundle() == {}
        assert store.read_receipt("session-start", "start") is None
    with closing(SqliteStore(database)) as store:
        assert start(store, example) == [row for _, row in records]
        assert store.read_receipt("session-start", "start") == [row for _, row in records]
        assert store.read_receipt("other-operation", "start") is None
        assert store.get_record("node_definition", {
            "component_version": "1.0.0", "component_id": uid(1),
        }) == example["node_definition"][0]
        assert store.get_record("node_binding", {
            "node_binding_id": uid(3), "workflow_definition_revision": 1,
            "workflow_definition_id": uid(2),
        }) == example["node_binding"][0]
        assert store.get_record("workflow_definition_revision", {
            "revision": 1, "workflow_definition_id": uid(2),
        }) == example["workflow_definition_revision"][0]
        assert store.get_record("node_session", {
            "node_binding_id": uid(3), "workflow_session_id": uid(4),
        }) == example["node_session"][0]
        assert store.get_record("visible_message_ref", {
            "visible_message_id": uid(14), "workflow_session_id": uid(4),
        })["boundary"]["input_status"] == "pending"
        assert store.get_record("run_record", {"run_id": uid(7)})["status"] == "prepared"
        assert store.save_bundle(records, "start", operation="session-start",
                                 expected_session_revisions={uid(4): 0}) == [row for _, row in records]
        changed = deepcopy(records)
        changed[-2][1]["payload"]["text"] = "forged"
        with pytest.raises(ContractValidationError, match="Idempotency"):
            store.save_bundle(changed, "start", operation="session-start")
        assert store.read_bundle() == validate_bundle(store.read_bundle())
    with closing(SqliteStore(database)) as reopened:
        assert reopened.read_receipt("session-start", "start") == [row for _, row in records]
        assert reopened.get_record("run_record", {"run_id": uid(7)})["status"] == "prepared"
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 13


def test_v1_upgrade_preserves_records_and_content_receipts(database, example):
    session = example["workflow_session"][0]
    records = [("workflow_session", session)]
    digest = content_digest([["workflow_session", session]])
    payload = canonical_bytes(session).decode("utf-8")
    result_payload = canonical_bytes([session]).decode("utf-8")
    with closing(sqlite3.connect(database)) as raw:
        raw.execute(
            "CREATE TABLE records (record_type TEXT NOT NULL, record_id TEXT NOT NULL, "
            "payload TEXT NOT NULL, immutable INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "PRIMARY KEY (record_type, record_id))"
        )
        raw.execute(
            "CREATE TABLE idempotency (operation TEXT NOT NULL, key TEXT NOT NULL, "
            "digest TEXT NOT NULL, result_refs TEXT NOT NULL, result_payload TEXT NOT NULL, "
            "PRIMARY KEY (operation, key))"
        )
        raw.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            ("workflow_session", uid(4), payload, 0, "2026-01-01T00:00:00.000Z"),
        )
        raw.execute(
            "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?)",
            ("legacy", "old-key", digest,
             canonical_bytes([["workflow_session", uid(4)]]).decode("utf-8"),
             result_payload),
        )
        raw.execute("PRAGMA user_version = 1")
        raw.commit()

    with closing(SqliteStore(database)) as store:
        assert store.get_record("workflow_session", {"workflow_session_id": uid(4)}) == session
        assert store.read_receipt_with_digest("legacy", "old-key") == (digest, [session])
        assert store.save_bundle(records, "old-key", operation="legacy") == [session]
        with pytest.raises(ContractValidationError, match="Idempotency"):
            store.save_bundle(records, "old-key", operation="legacy",
                              request_digest="workflow-op-v1:" + "a" * 64)
        assert store.read_receipt("legacy", "old-key") == [session]
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 13
        assert raw.execute(
            "SELECT digest, request_digest FROM idempotency WHERE key = 'old-key'"
        ).fetchone() == (digest, None)


def test_v2_upgrade_keeps_request_receipts_and_records(database, example):
    records = start_batch(example)
    fingerprint = "workflow-op-v1:" + "a" * 64
    with closing(SqliteStore(database)) as store:
        store.save_bundle(
            records, "start", operation="workflow.start",
            expected_session_revisions={uid(4): 0}, request_digest=fingerprint,
        )
    with closing(sqlite3.connect(database)) as raw:
        raw.execute("PRAGMA user_version = 2")
        raw.commit()
    with closing(SqliteStore(database)) as upgraded:
        assert upgraded.read_receipt_with_digest("workflow.start", "start") == (
            fingerprint, [row for _, row in records],
        )
        assert upgraded.get_record("workflow_session", {
            "workflow_session_id": uid(4),
        }) == example["workflow_session"][0]
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 13


def test_v3_upgrade_preserves_existing_chain_and_receipt_without_origin(database, example):
    with closing(SqliteStore(database)) as store:
        saved = start(store, example)
    with closing(sqlite3.connect(database)) as raw:
        raw.execute("PRAGMA user_version = 3")
        raw.commit()
    with closing(SqliteStore(database)) as upgraded:
        assert upgraded.read_receipt("session-start", "start") == saved
        assert upgraded.list_records("chain_input_origin") == []
        assert upgraded.read_bundle() == validate_bundle(upgraded.read_bundle())
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 13


def test_reroll_input_origin_is_atomic_immutable_and_reopens(database):
    with closing(WorkflowService(
        database, model_factory=lambda stage: OfflineAdapter(stage),
    )) as service:
        session_id = service.create_session()["workflow_session_id"]
        submitted = service.submit(session_id, "Original user text", "submit")
        service.wait_for_idle(session_id)

    with closing(SqliteStore(database)) as store:
        bundle = store.read_bundle()
        session = next(row for row in bundle["workflow_session"]
                       if row["workflow_session_id"] == session_id)
        head = next(row for row in bundle["workflow_ref"]
                    if row["workflow_session_id"] == session_id)
        source = next(row for row in bundle["chain_run"]
                      if row["chain_run_id"] == submitted["chain_run_id"])
        original_input = store.get_record("node_input", {"input_id": source["input_id"]})
        original_run = store.get_record("run_record", {
            "run_id": source["node_run_ids"][0],
        })
        original_snapshot = store.get_record("input_snapshot", {
            "snapshot_id": original_run["snapshot_id"],
        })
        chain_id, input_id, snapshot_id, run_id, operation_id = (
            uid(number) for number in range(2100, 2105)
        )
        new_input = revised(original_input, input_id=input_id)
        new_snapshot = revised(original_snapshot, snapshot_id=snapshot_id, input_id=input_id)
        new_run = revised(
            original_run, run_id=run_id, chain_run_id=chain_id, input_id=input_id,
            snapshot_id=snapshot_id, source_run_id=None, status="prepared",
            revision=1, result_turn_id=None,
        )
        operation = {
            "schema_version": 1, "operation_id": operation_id,
            "kind": "reroll",
            "scope": {"kind": "workflow_session", "id": session_id},
            "target": {"kind": "chain_run", "id": chain_id},
            "idempotency_key": "reroll-data",
            "expected_revisions": [
                {"kind": "workflow_session", "id": session_id,
                 "revision": session["revision"]},
                {"kind": "workflow_ref", "id": head["workflow_ref_id"],
                 "revision": head["revision"]},
            ],
            "expected_head_commit_id": head["head_commit_id"],
            "payload": {
                "chain_run_id": chain_id,
                "base_commit_id": source["base_commit_id"],
                "base_ref_revision": source["base_ref_revision"],
            },
        }
        chain = {
            "schema_version": 2, "chain_run_id": chain_id,
            "workflow_session_id": session_id, "input_id": input_id,
            "status": "prepared", "node_run_ids": [run_id], "output_id": None,
            "base_commit_id": source["base_commit_id"],
            "base_ref_revision": source["base_ref_revision"],
            "operation_id": operation_id,
        }
        origin = {
            "schema_version": 1, "chain_run_id": chain_id,
            "workflow_session_id": session_id,
            "visible_message_id": submitted["visible_message_id"],
            "input_id": input_id, "source_chain_run_id": source["chain_run_id"],
        }
        records = [
            ("workflow_session", revised(session, revision=session["revision"] + 1)),
            ("node_input", new_input), ("input_snapshot", new_snapshot),
            ("run_record", new_run), ("chain_run", chain),
            ("workflow_operation", operation), ("chain_input_origin", origin),
        ]
        saved = store.save_bundle(
            records, "reroll-data", operation="workflow.prepare_reroll",
            expected_session_revisions={session_id: session["revision"]},
        )
        assert store.save_bundle(
            records, "reroll-data", operation="workflow.prepare_reroll",
            expected_session_revisions={session_id: session["revision"]},
        ) == saved
        assert store.read_bundle() == validate_bundle(store.read_bundle())
        with pytest.raises(ContractValidationError, match="Immutable record identity"):
            store.save_bundle([
                ("chain_input_origin", {
                    **origin, "created_at": "2026-09-29T00:00:00.000Z",
                }),
            ], "changed-origin")
    with closing(SqliteStore(database)) as reopened:
        assert reopened.get_record("chain_input_origin", {
            "chain_run_id": chain_id,
        }) == origin
        assert reopened.read_bundle() == validate_bundle(reopened.read_bundle())


def test_version_records_ref_cas_and_immutable_history(database, example):
    with closing(SqliteStore(database)) as store:
        start(store, example)
        operation, snapshot, commit, ref = save_version_seed(store, example)
        assert store.get_record("workflow_commit", {"commit_id": commit["commit_id"]}) == commit
        assert store.get_record("state_snapshot", {
            "state_snapshot_id": snapshot["state_snapshot_id"],
        }) == snapshot
        assert store.get_record("workflow_operation", {
            "operation_id": operation["operation_id"],
        }) == operation
        assert store.get_record("workflow_ref", {
            "workflow_ref_id": ref["workflow_ref_id"],
        }) == ref

        next_operation = revised(operation, operation_id=uid(1004),
                                 idempotency_key="next", payload={"decision": "next"})
        next_snapshot = revised(snapshot, state_snapshot_id=uid(1005))
        next_commit = revised(
            commit, commit_id=uid(1006), state_snapshot_id=uid(1005),
            parent_commit_id=commit["commit_id"],
            operation_id=next_operation["operation_id"],
            source={"kind": "control_operation", "operation_id": next_operation["operation_id"]},
        )
        archived = [
            ("workflow_operation", next_operation),
            ("state_snapshot", next_snapshot), ("workflow_commit", next_commit),
        ]
        store.save_bundle(archived, "archive-next", operation="version.archive")
        advanced = revised(ref, head_commit_id=next_commit["commit_id"], revision=2)
        with pytest.raises(ContractValidationError, match="expected head"):
            store.save_bundle([("workflow_ref", advanced)], "unguarded-head",
                              operation="version.select")
        with pytest.raises(ContractValidationError, match="head conflict"):
            store.save_bundle(
                [("workflow_ref", advanced)], "stale-head", operation="version.select",
                expected_ref_heads={ref["workflow_ref_id"]: (1, uid(9999))},
            )
        with pytest.raises(ContractValidationError, match="revision must advance"):
            store.save_bundle(
                [("workflow_ref", revised(advanced, revision=3))], "skipped-head",
                operation="version.select",
                expected_ref_heads={ref["workflow_ref_id"]: (1, commit["commit_id"])},
            )
        assert store.get_record("workflow_ref", {
            "workflow_ref_id": ref["workflow_ref_id"],
        }) == ref
        assert store.get_record("workflow_commit", {
            "commit_id": next_commit["commit_id"],
        }) == next_commit
        assert store.save_bundle(
            [("workflow_ref", advanced)], "advance-head", operation="version.select",
            expected_ref_heads={ref["workflow_ref_id"]: (1, commit["commit_id"])},
        ) == [advanced]
        assert store.save_bundle(
            [("workflow_ref", advanced)], "advance-head", operation="version.select",
            expected_ref_heads={ref["workflow_ref_id"]: (1, commit["commit_id"])},
        ) == [advanced]
        for kind, value, identity in (
            ("state_snapshot", revised(snapshot, pending_input_id=uid(5)),
             {"state_snapshot_id": snapshot["state_snapshot_id"]}),
            ("workflow_commit", revised(commit, parent_commit_id=next_commit["commit_id"]),
             {"commit_id": commit["commit_id"]}),
            ("workflow_operation", revised(operation, payload={"decision": "forged"}),
             {"operation_id": operation["operation_id"]}),
        ):
            with pytest.raises(ContractValidationError, match="Immutable"):
                store.save_bundle([(kind, value)], "mutate-" + kind)
            assert store.get_record(kind, identity) != value
    with closing(SqliteStore(database)) as reopened:
        assert reopened.get_record("workflow_ref", {
            "workflow_ref_id": ref["workflow_ref_id"],
        }) == advanced
        assert reopened.get_record("workflow_commit", {
            "commit_id": commit["commit_id"],
        }) == commit


def test_ref_cas_fault_rolls_back_head_and_new_commit(database, example):
    with closing(SqliteStore(database)) as store:
        start(store, example)
        operation, snapshot, commit, ref = save_version_seed(store, example)

    def fault(point):
        if point == "before_commit":
            raise RuntimeError("interrupted")

    with closing(SqliteStore(database, fault_injector=fault)) as store:
        next_operation = revised(operation, operation_id=uid(1010),
                                 idempotency_key="fault")
        next_snapshot = revised(snapshot, state_snapshot_id=uid(1011))
        next_commit = revised(
            commit, commit_id=uid(1012), state_snapshot_id=uid(1011),
            parent_commit_id=commit["commit_id"],
            operation_id=next_operation["operation_id"],
            source={"kind": "control_operation", "operation_id": next_operation["operation_id"]},
        )
        advanced = revised(ref, head_commit_id=next_commit["commit_id"], revision=2)
        with pytest.raises(RuntimeError, match="interrupted"):
            store.save_bundle([
                ("workflow_operation", next_operation), ("state_snapshot", next_snapshot),
                ("workflow_commit", next_commit), ("workflow_ref", advanced),
            ], "fault-head", operation="version.select",
                expected_ref_heads={ref["workflow_ref_id"]: (1, commit["commit_id"])})
        assert store.read_receipt("version.select", "fault-head") is None
        assert store.get_record("workflow_ref", {"workflow_ref_id": ref["workflow_ref_id"]}) == ref
        assert len(store.list_records("workflow_commit")) == 1
    with closing(SqliteStore(database)) as reopened:
        assert reopened.get_record("workflow_ref", {"workflow_ref_id": ref["workflow_ref_id"]}) == ref
        assert reopened.read_receipt("version.select", "fault-head") is None


def test_workflow_candidate_survives_head_conflict(database, example):
    with closing(SqliteStore(database)) as store:
        start(store, example)
        _, _, base, ref = save_version_seed(store, example)
        advance(store, example)
        archive_a(store, example)
        store.save_bundle([
            ("workflow_session", revised(example["workflow_session"][0], revision=4)),
            ("chain_run", example["chain_run"][0]),
            ("workflow_output", example["workflow_output"][0]),
            ("output_delivery", example["output_delivery"][0]),
            ("workflow_checkpoint", example["workflow_checkpoint"][1]),
            ("visible_message_ref", example["visible_message_ref"][0]),
            ("visible_message", example["visible_message"][1]),
            ("visible_message_ref", example["visible_message_ref"][1]),
        ], "finish", operation="workflow.finish",
            expected_session_revisions={uid(4): 3})

        operation = revised(
            version_seed(example)[0], operation_id=uid(1020),
            idempotency_key="complete", payload={"input_id": uid(5)},
        )
        snapshot = revised(
            version_seed(example)[1], state_snapshot_id=uid(1021),
            selection_refs=deepcopy(example["workflow_checkpoint"][1]["selection_refs"]),
            visible_message_refs=[
                {field: deepcopy(ref_row[field])
                 for field in ("visible_message_id", "sequence", "role", "boundary")}
                for ref_row in example["visible_message_ref"]
            ],
        )
        snapshot["node_states"][0]["selected_result"] = {
            "kind": "agent_turn", "turn_id": uid(9),
        }
        result = {
            "schema_version": 1, "commit_id": uid(1022),
            "workflow_session_id": uid(4),
            "state_snapshot_id": snapshot["state_snapshot_id"],
            "parent_commit_id": base["commit_id"],
            "operation_id": operation["operation_id"],
            "source": {"kind": "candidate", "candidate_id": uid(1023)},
        }
        candidate = {
            "schema_version": 1, "candidate_id": uid(1023),
            "workflow_session_id": uid(4), "chain_run_id": uid(8),
            "base_commit_id": base["commit_id"], "result_commit_id": result["commit_id"],
            "output_id": uid(13), "checkpoint_id": uid(19),
        }
        store.save_bundle([
            ("workflow_operation", operation), ("state_snapshot", snapshot),
            ("workflow_commit", result), ("workflow_candidate", candidate),
        ], "candidate", operation="version.archive")
        selected = revised(ref, head_commit_id=result["commit_id"], revision=2)
        projection = example["candidate_selection"][0]
        projection_records = [("workflow_ref", selected),
                              ("candidate_selection", projection)]
        selection_revisions = {projection["candidate_group_id"]: 0}
        with pytest.raises(ContractValidationError, match="head conflict"):
            store.save_bundle(
                projection_records, "stale-select",
                operation="version.select",
                expected_ref_heads={ref["workflow_ref_id"]: (1, uid(9999))},
                expected_selection_revisions=selection_revisions,
            )
        with pytest.raises(ContractValidationError, match="target Head snapshot"):
            store.save_bundle(
                [("workflow_ref", selected),
                 ("candidate_selection", revised(projection, selected_turn_id=None))],
                "wrong-member", operation="version.select",
                expected_ref_heads={ref["workflow_ref_id"]: (1, base["commit_id"])},
                expected_selection_revisions=selection_revisions,
            )
        with pytest.raises(ContractValidationError, match="revision must advance"):
            store.save_bundle(
                [("workflow_ref", selected),
                 ("candidate_selection", revised(projection, revision=2))],
                "skipped-projection", operation="version.select",
                expected_ref_heads={ref["workflow_ref_id"]: (1, base["commit_id"])},
                expected_selection_revisions=selection_revisions,
            )
        with pytest.raises(ContractValidationError, match="revision conflict"):
            store.save_bundle(
                projection_records, "stale-projection", operation="version.select",
                expected_ref_heads={ref["workflow_ref_id"]: (1, base["commit_id"])},
                expected_selection_revisions={projection["candidate_group_id"]: 1},
            )
        with pytest.raises(ContractValidationError, match="guarded workflow head"):
            store.save_bundle(
                [("candidate_selection", projection)], "orphan-projection",
                operation="version.select",
                expected_selection_revisions=selection_revisions,
            )
        assert store.get_record("workflow_ref", {"workflow_ref_id": ref["workflow_ref_id"]}) == ref
        assert store.list_records("candidate_selection") == []
        assert store.get_record("workflow_candidate", {
            "candidate_id": candidate["candidate_id"],
        }) == candidate
        fingerprint = "workflow-op-v1:" + "d" * 64
        assert store.save_bundle(
            projection_records, "select", operation="version.select",
            expected_ref_heads={ref["workflow_ref_id"]: (1, base["commit_id"])},
            expected_selection_revisions=selection_revisions,
            request_digest=fingerprint,
        ) == [selected, projection]
        assert store.save_bundle(
            projection_records, "select", operation="version.select",
            expected_ref_heads={ref["workflow_ref_id"]: (1, base["commit_id"])},
            expected_selection_revisions=selection_revisions,
            request_digest=fingerprint,
        ) == [selected, projection]
        assert store.read_receipt_with_digest("version.select", "select") == (
            fingerprint, [selected, projection],
        )
    with closing(SqliteStore(database)) as reopened:
        assert reopened.get_record("workflow_candidate", {
            "candidate_id": candidate["candidate_id"],
        }) == candidate
        assert reopened.get_record("workflow_commit", {"commit_id": result["commit_id"]}) == result
        assert reopened.get_record("workflow_ref", {"workflow_ref_id": ref["workflow_ref_id"]}) == selected
        assert reopened.get_record("candidate_selection", {
            "candidate_group_id": projection["candidate_group_id"],
        }) == projection


def test_head_projection_rejects_candidate_from_unrelated_session(database, example):
    with closing(SqliteStore(database)) as store:
        start(store, example)
        advance(store, example)
        archive_a(store, example)
        operation, snapshot, commit, ref = version_seed(example)
        other_session_id = uid(1100)
        other_session = revised(example["workflow_session"][0],
                                workflow_session_id=other_session_id)
        other_node = revised(example["node_session"][0],
                             workflow_session_id=other_session_id)
        other_operation = revised(operation, operation_id=uid(1101),
                                  idempotency_key="other-seed")
        other_operation["scope"]["id"] = other_session_id
        other_operation["target"]["id"] = other_session_id
        other_operation["expected_revisions"][0]["id"] = other_session_id
        other_snapshot = revised(snapshot, state_snapshot_id=uid(1102),
                                 workflow_session_id=other_session_id)
        other_commit = revised(
            commit, commit_id=uid(1103), workflow_session_id=other_session_id,
            state_snapshot_id=other_snapshot["state_snapshot_id"],
            operation_id=other_operation["operation_id"],
            source={"kind": "session_seed", "workflow_session_id": other_session_id},
        )
        other_ref = revised(
            ref, workflow_ref_id=uid(1104), workflow_session_id=other_session_id,
            head_commit_id=other_commit["commit_id"],
        )
        store.save_bundle([
            ("workflow_session", other_session), ("node_session", other_node),
            ("workflow_operation", other_operation), ("state_snapshot", other_snapshot),
            ("workflow_commit", other_commit), ("workflow_ref", other_ref),
        ], "other-seed", operation="version.seed",
            expected_session_revisions={other_session_id: 0},
            expected_ref_heads={other_ref["workflow_ref_id"]: (0, None)})

        foreign_snapshot = revised(
            other_snapshot, state_snapshot_id=uid(1105),
            selection_refs=deepcopy(example["workflow_checkpoint"][1]["selection_refs"]),
        )
        foreign_operation = revised(other_operation, operation_id=uid(1106),
                                    idempotency_key="foreign-select")
        foreign_commit = revised(
            other_commit, commit_id=uid(1107),
            state_snapshot_id=foreign_snapshot["state_snapshot_id"],
            parent_commit_id=other_commit["commit_id"],
            operation_id=foreign_operation["operation_id"],
            source={"kind": "control_operation",
                    "operation_id": foreign_operation["operation_id"]},
        )
        advanced = revised(other_ref, head_commit_id=foreign_commit["commit_id"],
                           revision=2)
        projection = example["candidate_selection"][0]
        with pytest.raises(ContractValidationError, match="not an owned candidate"):
            store.save_bundle([
                ("workflow_operation", foreign_operation),
                ("state_snapshot", foreign_snapshot),
                ("workflow_commit", foreign_commit),
                ("workflow_ref", advanced),
                ("candidate_selection", projection),
            ], "foreign-select", operation="version.select",
                expected_ref_heads={other_ref["workflow_ref_id"]:
                                    (1, other_commit["commit_id"])},
                expected_selection_revisions={projection["candidate_group_id"]: 0})
        assert store.read_receipt("version.select", "foreign-select") is None
        assert store.get_record("workflow_ref", {
            "workflow_ref_id": other_ref["workflow_ref_id"],
        }) == other_ref
        assert store.list_records("candidate_selection") == []
        assert len(store.list_records("workflow_commit")) == 1


def test_request_digest_replays_only_exact_fingerprint(database, example):
    records = start_batch(example)
    fingerprint = "workflow-op-v1:" + "a" * 64
    with closing(SqliteStore(database)) as store:
        expected = [row for _, row in records]
        assert store.save_bundle(
            records, "start", operation="workflow.start",
            expected_session_revisions={uid(4): 0}, request_digest=fingerprint,
        ) == expected
        assert store.read_receipt_with_digest("workflow.start", "start") == (
            fingerprint, expected,
        )
        changed = deepcopy(records)
        changed[-2][1]["payload"]["text"] = "different generated output"
        assert store.save_bundle(
            changed, "start", operation="workflow.start", request_digest=fingerprint,
        ) == expected
        with pytest.raises(ContractValidationError, match="Idempotency"):
            store.save_bundle(
                records, "start", operation="workflow.start",
                request_digest="workflow-op-v1:" + "b" * 64,
            )
        with pytest.raises(ContractValidationError, match="Idempotency"):
            store.save_bundle(records, "start", operation="workflow.start")
        assert store.get_record("visible_message", {
            "visible_message_id": records[-2][1]["visible_message_id"],
        }) == records[-2][1]
    with closing(SqliteStore(database)) as reopened:
        assert reopened.read_receipt_with_digest("workflow.start", "start") == (
            fingerprint, expected,
        )
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute(
            "SELECT digest, request_digest FROM idempotency WHERE key = 'start'"
        ).fetchone() == (
            content_digest([[kind, value] for kind, value in records]), fingerprint,
        )


@pytest.mark.parametrize("invalid", [
    "", "workflow-op-v1:", "workflow-op-v1:" + "A" * 64,
    "workflow-op-v1:" + "a" * 63, "workflow-op-v1:" + "a" * 64 + "\n",
    "json-v1:sha256:" + "a" * 64, 1,
])
def test_request_digest_rejects_invalid_identity(database, example, invalid):
    with closing(SqliteStore(database)) as store:
        with pytest.raises(ContractValidationError, match="request digest"):
            store.save_bundle(start_batch(example), "start",
                              request_digest=invalid)
        assert store.read_receipt_with_digest("bundle", "start") is None
        assert store.read_bundle() == {}


@pytest.mark.parametrize("fault_point", ["after_first_write", "before_commit"])
def test_request_digest_receipt_and_records_roll_back_together(
    database, example, fault_point,
):
    fingerprint = "workflow-op-v1:" + "c" * 64
    records = start_batch(example)

    def fault(point):
        if point == fault_point:
            raise RuntimeError("interrupted")

    with closing(SqliteStore(database, fault_injector=fault)) as store:
        with pytest.raises(RuntimeError, match="interrupted"):
            store.save_bundle(
                records, "start", operation="workflow.start",
                expected_session_revisions={uid(4): 0}, request_digest=fingerprint,
            )
        assert store.read_bundle() == {}
        assert store.read_receipt_with_digest("workflow.start", "start") is None
    with closing(SqliteStore(database)) as reopened:
        assert reopened.save_bundle(
            records, "start", operation="workflow.start",
            expected_session_revisions={uid(4): 0}, request_digest=fingerprint,
        ) == [row for _, row in records]
        assert reopened.read_receipt_with_digest("workflow.start", "start")[0] == fingerprint


def test_session_cas_state_gates_and_immutable_conflicts(database, example):
    with closing(SqliteStore(database)) as store:
        start(store, example)
        with pytest.raises(ContractValidationError, match="revision conflict"):
            store.save_bundle([
                ("workflow_session", revised(example["workflow_session"][0], revision=2)),
            ], "stale", expected_session_revisions={uid(4): 0})
        with pytest.raises(ContractValidationError, match="expected revision"):
            store.save_bundle([
                ("workflow_session", revised(example["workflow_session"][0], revision=2)),
            ], "unguarded")
        with pytest.raises(ContractValidationError, match="advance by one"):
            store.save_bundle([
                ("workflow_session", revised(example["workflow_session"][0], revision=3)),
            ], "skipped", expected_session_revisions={uid(4): 1})
        with pytest.raises(ContractValidationError, match="prepared"):
            store.save_bundle([
                ("run_record", revised(example["run_record"][0], run_id=uid(901))),
            ], "instant-success")
        with pytest.raises(ContractValidationError, match="not allowed"):
            store.save_bundle([("candidate_selection", example["candidate_selection"][0])],
                              "select-bypass")
        with pytest.raises(ContractValidationError, match="not allowed"):
            store.save_bundle([("turn", example["turn"][0])], "turn-bypass")
        altered = revised(example["input_snapshot"][0])
        altered["model_parameters"]["temperature"] = 1
        with pytest.raises(ContractValidationError, match="Immutable"):
            store.save_bundle([("input_snapshot", altered)], "frozen")
        timestamped = revised(example["run_record"][0], status="running", revision=2,
                              result_turn_id=None, created_at="2026-01-01T00:00:00.000Z")
        with pytest.raises(ContractValidationError, match="frozen"):
            store.save_bundle([("run_record", timestamped)], "added-created-at")
        advance(store, example)
        with pytest.raises(ContractValidationError, match="Illegal Agent"):
            store.save_bundle([
                ("run_record", revised(example["run_record"][0], revision=4)),
            ], "archive-bypass")
        forged = revised(example["run_record"][0], status="recovery_unavailable",
                         revision=4, result_turn_id=None, input_id=uid(999))
        with pytest.raises(ContractValidationError, match="frozen"):
            store.save_bundle([("run_record", forged)], "run-identity")
        assert store.read_receipt("bundle", "archive-bypass") is None
        assert store.get_record("run_record", {"run_id": uid(7)})["status"] == "final_ready"
        archive_a(store, example)
        assert store.get_record("chain_run", {"chain_run_id": uid(8)})["status"] == "running"
        with pytest.raises(ContractValidationError, match="Illegal Agent"):
            store.save_bundle([("run_record", revised(example["run_record"][0],
                                                       status="failed", revision=5,
                                                       result_turn_id=None))], "undo-success")


def test_graph_owner_and_commit_fault_roll_back_entire_step(database, example):
    with closing(SqliteStore(database)) as store:
        start(store, example)
        other = revised(example["workflow_session"][0], workflow_session_id=uid(900))
        other_chain = revised(example["chain_run"][0], chain_run_id=uid(901),
                              workflow_session_id=uid(900), status="prepared",
                              node_run_ids=[], output_id=None)
        with pytest.raises(ContractValidationError, match="another workflow session"):
            store.save_bundle([
                ("workflow_session", other), ("chain_run", other_chain),
            ], "cross-owner", expected_session_revisions={uid(900): 0})
        assert store.read_receipt("bundle", "cross-owner") is None
        assert store.list_records("input_snapshot") == example["input_snapshot"]
    stages = []

    def fault(point):
        stages.append(point)
        if point == "before_commit":
            raise RuntimeError("interrupted")

    with closing(SqliteStore(database, fault_injector=fault)) as store:
        with pytest.raises(RuntimeError, match="interrupted"):
            store.save_bundle([
                ("workflow_session", revised(example["workflow_session"][0], revision=2)),
                ("run_record", revised(example["run_record"][0], status="running",
                                       revision=2, result_turn_id=None)),
                ("chain_run", revised(example["chain_run"][0], status="running", output_id=None)),
            ], "dispatch", expected_session_revisions={uid(4): 1})
        assert stages == ["after_first_write", "before_commit"]
        assert store.read_receipt("bundle", "dispatch") is None
        assert store.get_record("workflow_session", {"workflow_session_id": uid(4)})["revision"] == 1
        assert store.get_record("run_record", {"run_id": uid(7)})["status"] == "prepared"


def test_candidate_append_only_and_explicit_recovery(database, example):
    with closing(SqliteStore(database)) as store:
        start(store, example)
        advance(store, example)
        archive_a(store, example)
        dropped = revised(example["candidate_group"][0], candidate_refs=[])
        with pytest.raises(ContractValidationError):
            store.save_bundle([("candidate_group", dropped)], "drop-candidate")
        assert store.get_record("candidate_group", {"candidate_group_id": uid(15)}) == \
            example["candidate_group"][0]
    with closing(SqliteStore(database)) as reopened:
        assert reopened.get_record("run_record", {"run_id": uid(7)})["status"] == "succeeded"

    other_db = database.with_name("recover.sqlite")
    with closing(SqliteStore(other_db)) as store:
        start(store, example)
        advance(store, example)
    with closing(SqliteStore(other_db)) as reopened:
        assert reopened.get_record("run_record", {"run_id": uid(7)})["status"] == "final_ready"
        recovered = revised(example["run_record"][0], status="recovery_unavailable",
                            revision=4, result_turn_id=None)
        recovered_chain = revised(example["chain_run"][0],
                                  status="recovery_unavailable", output_id=None)
        reopened.save_bundle([
            ("workflow_session", revised(example["workflow_session"][0], revision=4)),
            ("run_record", recovered), ("chain_run", recovered_chain),
        ], "recover", expected_session_revisions={uid(4): 3})
        assert reopened.get_record("run_record", {"run_id": uid(7)}) == recovered
        assert reopened.read_bundle() == validate_bundle(reopened.read_bundle())


def test_failed_chain_reopens_as_recovery_without_changing_its_identity(database, example):
    with closing(SqliteStore(database)) as store:
        start(store, example)
        failed_run = revised(example["run_record"][0], status="failed", revision=2,
                             result_turn_id=None)
        failed_chain = revised(example["chain_run"][0], status="failed", output_id=None)
        failed_ref = revised(example["visible_message_ref"][0])
        failed_ref["boundary"].update(input_status="failed")
        store.save_bundle([
            ("workflow_session", revised(example["workflow_session"][0], revision=2)),
            ("run_record", failed_run), ("chain_run", failed_chain),
            ("visible_message_ref", failed_ref),
        ], "fail", expected_session_revisions={uid(4): 1})
    with closing(SqliteStore(database)) as reopened:
        recovered_chain = revised(failed_chain, status="recovery_unavailable")
        for changed in (
            revised(recovered_chain, node_run_ids=[uid(7), uid(999)]),
            revised(recovered_chain, output_id=uid(999)),
            revised(failed_chain, status="succeeded", output_id=uid(999)),
        ):
            with pytest.raises(ContractValidationError, match="Failed chain"):
                reopened.save_bundle([("chain_run", changed)], "forged-recovery")
        recovered_run = revised(failed_run, status="recovery_unavailable", revision=3)
        reopened.save_bundle([
            ("workflow_session", revised(example["workflow_session"][0], revision=3)),
            ("run_record", recovered_run), ("chain_run", recovered_chain),
        ], "reopen", expected_session_revisions={uid(4): 2})
        assert reopened.get_record("chain_run", {"chain_run_id": uid(8)}) == recovered_chain
        assert reopened.get_record("run_record", {"run_id": uid(7)}) == recovered_run
        assert reopened.read_bundle() == validate_bundle(reopened.read_bundle())


def test_unknown_storage_version_rejected_without_migration(database):
    with closing(sqlite3.connect(database)) as raw:
        raw.execute("PRAGMA user_version = 14")
        raw.commit()
    with pytest.raises(ContractValidationError, match="version"):
        SqliteStore(database)
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 14
        assert raw.execute("SELECT name FROM sqlite_master WHERE name = 'records'").fetchone() is None


def test_a_then_b_then_io_completes_with_atomic_ui_boundary(database, example):
    bundle = three_bindings(example)
    with closing(SqliteStore(database)) as store:
        start(store, bundle)
        advance(store, bundle)
        archive_a(store, bundle)
        store.select_candidate(bundle["candidate_selection"][0], 0)

        input_b, snapshot_b, prepared_b, chain_b = second_agent(bundle)
        output_a = bundle["workflow_output"][0]
        head_a = revised(bundle["node_session"][0], data_version=2)
        head_a["private_data"]["payload"] = {"head_turn_id": uid(9)}
        store.save_bundle([
            ("workflow_session", revised(bundle["workflow_session"][0], revision=4)),
            ("node_session", head_a),
            ("workflow_output", output_a),
            ("node_input", input_b), ("input_snapshot", snapshot_b),
            ("run_record", prepared_b), ("chain_run", chain_b),
        ], "start-B", operation="step", expected_session_revisions={uid(4): 3})
        assert store.get_record("chain_run", {"chain_run_id": uid(8)})["status"] == "running"
        assert store.get_record("node_session", {
            "workflow_session_id": uid(4), "node_binding_id": uid(3),
        })["private_data"]["payload"] == {"head_turn_id": uid(9)}
        store.save_bundle([
            ("workflow_session", revised(bundle["workflow_session"][0], revision=5)),
            ("run_record", revised(prepared_b, status="running", revision=2)),
        ], "dispatch-B", operation="step", expected_session_revisions={uid(4): 4})
        store.save_bundle([
            ("workflow_session", revised(bundle["workflow_session"][0], revision=6)),
            ("run_record", revised(prepared_b, status="final_ready", revision=3)),
        ], "final-B", operation="step", expected_session_revisions={uid(4): 5})
        run_b, turn_b, group_b = second_archive(bundle)
        store.archive_success([
            ("run_record", run_b), ("turn", turn_b), ("candidate_group", group_b),
        ], "archive-B")
        store.select_candidate({
            "schema_version": 1, "candidate_group_id": uid(314),
            "selected_turn_id": uid(313), "revision": 1,
        }, 0)

        output_b = revised(output_a, output_id=uid(315))
        output_b["source"].update(node_binding_id=uid(301), run_id=uid(312),
                                  turn_id=uid(313))
        input_io = revised(input_b, input_id=uid(316),
                           source={"kind": "upstream_output", "output_id": uid(315)})
        io_run = {
            "schema_version": 1, "profile": "node", "run_id": uid(317),
            "workflow_session_id": uid(4), "node_binding_id": uid(303),
            "chain_run_id": uid(8), "input_id": uid(316),
            "input_snapshot_id": None, "source_run_id": None,
            "status": "succeeded", "revision": 1,
            "result_output_id": uid(318), "superseded_by_run_id": None,
        }
        output_io = revised(output_b, output_id=uid(318))
        output_io["source"].update(node_binding_id=uid(303), run_id=uid(317),
                                   turn_id=None)
        head_b = revised(bundle["node_session"][1], data_version=2)
        head_b["private_data"]["payload"] = {"head_turn_id": uid(313)}
        io_state = revised(bundle["node_session"][2], data_version=2)
        io_state["private_data"]["payload"] = {"last_output_id": uid(318)}
        chain_io = revised(chain_b, node_run_ids=[uid(7), uid(312), uid(317)])
        store.save_bundle([
            ("workflow_session", revised(bundle["workflow_session"][0], revision=7)),
            ("node_session", head_b), ("node_session", io_state),
            ("workflow_output", output_b),
            ("node_input", input_io), ("node_run", io_run),
            ("workflow_output", output_io), ("chain_run", chain_io),
        ], "io-output", operation="step", expected_session_revisions={uid(4): 6})
        assert store.get_record("workflow_output", {"output_id": uid(318)}) == output_io
        assert store.get_record("chain_run", {"chain_run_id": uid(8)})["status"] == "running"

        final_checkpoint = revised(bundle["workflow_checkpoint"][1], output_id=uid(318))
        final_checkpoint["nodes"] = deepcopy(bundle["workflow_checkpoint"][0]["nodes"])
        final_checkpoint["nodes"][0]["selected_turn_id"] = uid(9)
        final_checkpoint["nodes"][1]["selected_turn_id"] = uid(313)
        final_checkpoint["nodes"][0]["data_version"] = 2
        final_checkpoint["nodes"][0]["private_data"] = deepcopy(head_a["private_data"])
        final_checkpoint["nodes"][1]["data_version"] = 2
        final_checkpoint["nodes"][1]["private_data"] = deepcopy(head_b["private_data"])
        final_checkpoint["nodes"][2]["data_version"] = 2
        final_checkpoint["nodes"][2]["private_data"] = deepcopy(io_state["private_data"])
        final_checkpoint["selection_refs"] = [
            {"candidate_group_id": uid(15), "selected_turn_id": uid(9)},
            {"candidate_group_id": uid(314), "selected_turn_id": uid(313)},
        ]
        user_ref = revised(bundle["visible_message_ref"][0])
        user_ref["boundary"]["input_status"] = "completed"
        assistant = revised(bundle["visible_message"][1])
        assistant["source"]["output_id"] = uid(318)
        assistant_ref = revised(bundle["visible_message_ref"][1])
        assistant_ref["boundary"]["output_id"] = uid(318)
        delivery = revised(bundle["output_delivery"][0], output_id=uid(318),
                           status="pending")
        finishing = [
            ("workflow_session", revised(bundle["workflow_session"][0], revision=8)),
            ("chain_run", revised(chain_io, status="succeeded", output_id=uid(318))),
            ("workflow_checkpoint", final_checkpoint),
            ("visible_message_ref", user_ref),
            ("visible_message", assistant),
            ("visible_message_ref", assistant_ref),
            ("output_delivery", delivery),
        ]
        assert store.save_bundle(finishing, "finish", operation="step",
                                 expected_session_revisions={uid(4): 7}) == [
                                     row for _, row in finishing
                                 ]
        with pytest.raises(ContractValidationError, match="Completed chain"):
            store.save_bundle([
                ("chain_run", revised(chain_io, status="failed", output_id=None)),
            ], "mutate-completed", operation="step")
        with pytest.raises(ContractValidationError, match="Completed chain"):
            store.save_bundle([
                ("chain_run", revised(chain_io, status="succeeded", output_id=uid(999))),
            ], "replace-final-output", operation="step")
        assert store.read_bundle() == validate_bundle(store.read_bundle())
    with closing(SqliteStore(database)) as reopened:
        assert reopened.read_receipt("step", "finish") == [row for _, row in finishing]
        assert reopened.get_record("workflow_session", {"workflow_session_id": uid(4)})["revision"] == 8
        assert reopened.get_record("visible_message_ref", {
            "workflow_session_id": uid(4), "visible_message_id": uid(20),
        })["boundary"]["after_checkpoint_id"] == uid(19)


def stop_for_control(store, example, status="failed"):
    start(store, example)
    run = revised(example["run_record"][0], status=status, revision=2, result_turn_id=None)
    chain = revised(example["chain_run"][0], status=status, output_id=None)
    store.save_bundle([
        ("workflow_session", revised(example["workflow_session"][0], revision=2)),
        ("run_record", run), ("chain_run", chain),
    ], "stop", expected_session_revisions={uid(4): 1})
    return run, chain


def run_control(kind, run, session_revision=2, **payload):
    return {
        "schema_version": 1, "operation_id": uid(1800 + run["revision"]), "kind": kind,
        "scope": {"kind": "workflow_session", "id": run["workflow_session_id"]},
        "target": {"kind": "run_record", "id": run["run_id"]},
        "idempotency_key": "control",
        "expected_revisions": [
            {"kind": "workflow_session", "id": run["workflow_session_id"],
             "revision": session_revision},
            {"kind": "run_record", "id": run["run_id"], "revision": run["revision"]},
        ],
        "payload": {"chain_run_id": run["chain_run_id"], **payload},
    }


def failure_fact(run, code, category="model"):
    return {
        "schema_version": 1, "fact_id": uid(1900),
        **{key: run[key] for key in (
            "run_id", "chain_run_id", "workflow_session_id", "node_binding_id", "snapshot_id",
        )},
        "generation": uid(1901), "sequence": 1, "kind": "execution_failed",
        "created_at": "2026-09-29T00:00:00.000Z",
        "payload": {"code": code, "category": category, "model_requests": 0, "attempts": 0},
    }


@pytest.mark.parametrize("status", ["paused", "failed"])
def test_budget_extension_preserves_initial_allowance_and_receipt(database, example, status):
    with closing(SqliteStore(database)) as store:
        run, chain = stop_for_control(store, example, status)
        requests = run["initial_budget"]["max_model_requests"]
        control = run_control(
            "extend_budget", run, additional_model_requests=2, additional_model_attempts=3,
            max_model_requests=requests + 2, max_model_attempts=requests * 4 + 3,
        )
        records = [
            ("run_record", revised(run, revision=3)),
            ("workflow_operation", control),
            ("workflow_session", revised(example["workflow_session"][0], revision=3)),
        ]
        assert store.save_bundle(
            records, "allow-more", operation="workflow.extend_budget",
            expected_session_revisions={uid(4): 2},
        ) == [row for _, row in records]
        assert store.get_record("run_record", {"run_id": run["run_id"]})["initial_budget"] == run["initial_budget"]
        assert store.get_record("chain_run", {"chain_run_id": chain["chain_run_id"]}) == chain
        assert store.read_execution_facts(run["run_id"]) == []
        assert store.save_bundle(
            records, "allow-more", operation="workflow.extend_budget",
            expected_session_revisions={uid(4): 2},
        ) == [row for _, row in records]
        current = store.get_record("run_record", {"run_id": run["run_id"]})
        second_control = run_control(
            "extend_budget", current, session_revision=3,
            additional_model_requests=0, additional_model_attempts=1,
            max_model_requests=requests + 2, max_model_attempts=requests * 4 + 4,
        )
        store.save_bundle([
            ("run_record", revised(current, revision=4)),
            ("workflow_operation", second_control),
            ("workflow_session", revised(example["workflow_session"][0], revision=4)),
        ], "allow-again", operation="workflow.extend_budget",
            expected_session_revisions={uid(4): 3})
        assert store.get_record("run_record", {"run_id": run["run_id"]})["initial_budget"] == run["initial_budget"]
    with closing(SqliteStore(database)) as reopened:
        assert reopened.read_receipt("workflow.extend_budget", "allow-more") == [
            row for _, row in records
        ]


@pytest.mark.parametrize("mutation", [
    lambda records: records[0][1]["initial_budget"].update(max_model_requests=8),
    lambda records: records[0][1].update(status="running"),
    lambda records: records[1][1]["payload"].update(max_model_attempts=1),
    lambda records: records[1][1]["payload"].update(additional_model_attempts=True),
    lambda records: records[1][1]["payload"].update(max_model_requests=65),
    lambda records: records[1][1]["expected_revisions"][1].update(revision=1),
    lambda records: records[1][1]["payload"].update(extra=True),
])
def test_budget_extension_rejects_frozen_change_bad_totals_or_stale_run(database, example, mutation):
    with closing(SqliteStore(database)) as store:
        run, _ = stop_for_control(store, example)
        requests = run["initial_budget"]["max_model_requests"]
        records = [
            ("run_record", revised(run, revision=3)),
            ("workflow_operation", run_control(
                "extend_budget", run, additional_model_requests=1, additional_model_attempts=0,
                max_model_requests=requests + 1, max_model_attempts=requests * 4,
            )),
            ("workflow_session", revised(example["workflow_session"][0], revision=3)),
        ]
        mutation(records)
        with pytest.raises(ContractValidationError):
            store.save_bundle(records, "bad-extension", operation="workflow.extend_budget",
                              expected_session_revisions={uid(4): 2})
        assert store.get_record("run_record", {"run_id": run["run_id"]}) == run
        assert store.read_receipt("workflow.extend_budget", "bad-extension") is None


def test_budget_extension_cannot_be_inserted_as_a_generic_bundle(database, example):
    with closing(SqliteStore(database)) as store:
        run, _ = stop_for_control(store, example)
        control = run_control(
            "extend_budget", run, additional_model_requests=1, additional_model_attempts=0,
            max_model_requests=5, max_model_attempts=16,
        )
        with pytest.raises(ContractValidationError, match="dedicated"):
            store.save_bundle([("workflow_operation", control)], "forged-allowance")


@pytest.mark.parametrize("code,category", [
    ("model_retry_exhausted", "model"), ("protocol_error", "protocol"),
    ("invalid_final", "protocol"), ("model_request_budget_exhausted", "model"),
    ("model_attempt_budget_exhausted", "model"),
])
def test_failed_resume_requires_matching_reason_and_preserves_chain_members(database, example, code, category):
    with closing(SqliteStore(database)) as store:
        run, chain = stop_for_control(store, example)
        store.append_execution_fact(failure_fact(run, code, category), expected_sequence=0)
        operation = run_control("resume", run, recovery_code=code)
        records = [
            ("run_record", revised(run, status="running", revision=3)),
            ("chain_run", revised(chain, status="running")),
            ("workflow_operation", operation),
            ("workflow_session", revised(example["workflow_session"][0], revision=3)),
        ]
        with pytest.raises(ContractValidationError, match="explicit matching"):
            store.save_bundle(records, "generic-resurrection",
                              expected_session_revisions={uid(4): 2})
        store.save_bundle(records, "continue-same", operation="workflow.resume",
                          expected_session_revisions={uid(4): 2})
        current = store.get_record("chain_run", {"chain_run_id": chain["chain_run_id"]})
        assert current["node_run_ids"] == chain["node_run_ids"]
        assert current["output_id"] == chain["output_id"]
        assert store.read_execution_facts(run["run_id"]) == [failure_fact(run, code, category)]


@pytest.mark.parametrize("code,category", [
    ("model_error", "model"), ("execution_interrupted", "interrupted"),
    ("configuration_error", "contract"), ("model_retry_exhausted", "contract"),
])
def test_failed_resume_rejects_unrecoverable_saved_failure(database, example, code, category):
    with closing(SqliteStore(database)) as store:
        run, chain = stop_for_control(store, example)
        store.append_execution_fact(failure_fact(run, code, category), expected_sequence=0)
        with pytest.raises(ContractValidationError, match="recoverable failure"):
            store.save_bundle([
                ("run_record", revised(run, status="running", revision=3)),
                ("chain_run", revised(chain, status="running")),
                ("workflow_operation", run_control("resume", run, recovery_code=code)),
                ("workflow_session", revised(example["workflow_session"][0], revision=3)),
            ], "deny", operation="workflow.resume", expected_session_revisions={uid(4): 2})
        assert store.get_record("run_record", {"run_id": run["run_id"]}) == run


@pytest.mark.parametrize("change", ["no_failure", "wrong_reason", "wrong_revision", "changed_members"])
def test_failed_resume_rejects_missing_or_forged_recovery_evidence(database, example, change):
    with closing(SqliteStore(database)) as store:
        run, chain = stop_for_control(store, example)
        if change != "no_failure":
            store.append_execution_fact(
                failure_fact(run, "model_retry_exhausted"), expected_sequence=0,
            )
        control = run_control("resume", run, recovery_code="model_retry_exhausted")
        resumed_chain = revised(chain, status="running")
        if change == "wrong_reason":
            control["payload"]["recovery_code"] = "protocol_error"
        elif change == "wrong_revision":
            control["expected_revisions"][1]["revision"] = 1
        elif change == "changed_members":
            resumed_chain["node_run_ids"].append(uid(999))
        with pytest.raises(ContractValidationError):
            store.save_bundle([
                ("run_record", revised(run, status="running", revision=3)),
                ("chain_run", resumed_chain),
                ("workflow_operation", control),
                ("workflow_session", revised(example["workflow_session"][0], revision=3)),
            ], "forged", operation="workflow.resume", expected_session_revisions={uid(4): 2})
        assert store.get_record("run_record", {"run_id": run["run_id"]}) == run
        assert store.get_record("chain_run", {"chain_run_id": chain["chain_run_id"]}) == chain


def test_failed_resume_cannot_bypass_unsettled_model_attempt(database, example):
    with closing(SqliteStore(database)) as store:
        run, chain = stop_for_control(store, example)
        snapshot = example["input_snapshot"][0]
        base = failure_fact(run, "model_retry_exhausted")
        request_payload = {
            "request_id": uid(2000), "request_index": 1, "messages": snapshot["s0"],
            "tools": [{
                "type": "function", "function": {
                    "name": item["name"], "parameters": item["parameters_schema"],
                    **({"description": item["description"]} if "description" in item else {}),
                },
            } for item in snapshot["tool_definitions"]],
            "model_parameters": snapshot["model_parameters"],
        }
        store.append_execution_fact(
            revised(base, kind="model_request", payload=request_payload), expected_sequence=0,
        )
        store.append_execution_fact(
            revised(base, fact_id=uid(1902), sequence=2, kind="model_attempt_started", payload={
                "request_id": uid(2000), "attempt_id": uid(2001), "request_index": 1,
                "attempt_index": 1, "retry_index": 0,
            }), expected_sequence=1,
        )
        base["payload"].update(model_requests=1, attempts=1)
        store.append_execution_fact(
            revised(base, fact_id=uid(1903), sequence=3), expected_sequence=2,
        )
        with pytest.raises(ContractValidationError, match="unsettled dispatch"):
            store.save_bundle([
                ("run_record", revised(run, status="running", revision=3)),
                ("chain_run", revised(chain, status="running")),
                ("workflow_operation", run_control("resume", run, recovery_code="model_retry_exhausted")),
                ("workflow_session", revised(example["workflow_session"][0], revision=3)),
            ], "unfinished", operation="workflow.resume", expected_session_revisions={uid(4): 2})


def test_budget_extension_respects_explicit_attempt_allowance_and_session_cas(database, example):
    example["run_record"][0]["initial_budget"]["max_model_attempts"] = 2
    with closing(SqliteStore(database)) as store:
        run, _ = stop_for_control(store, example)
        requests = run["initial_budget"]["max_model_requests"]
        records = [
            ("run_record", revised(run, revision=3)),
            ("workflow_operation", run_control(
                "extend_budget", run, additional_model_requests=0, additional_model_attempts=1,
                max_model_requests=requests, max_model_attempts=3,
            )),
            ("workflow_session", revised(example["workflow_session"][0], revision=3)),
        ]
        with pytest.raises(ContractValidationError):
            store.save_bundle(records, "stale-session", operation="workflow.extend_budget",
                              expected_session_revisions={uid(4): 1})
        store.save_bundle(records, "explicit-attempts", operation="workflow.extend_budget",
                          expected_session_revisions={uid(4): 2})
        assert store.get_record("run_record", {"run_id": run["run_id"]})["initial_budget"]["max_model_attempts"] == 2
