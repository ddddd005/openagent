"""Execution facts are immutable causal evidence, not reconstructed Turn history."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.execution_facts import (
    validate_execution_fact, validate_execution_fact_history,
)
from phase1_agent.storage import SqliteStore


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


@pytest.fixture
def example():
    return loads_strict(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="execution-facts-", dir=Path(__file__).parent) as directory:
        yield Path(directory) / "execution-facts.sqlite"


def seed(store, example):
    kinds = (
        "node_definition", "node_binding", "workflow_definition_revision", "workflow_session",
        "node_session", "node_input", "input_snapshot", "workflow_checkpoint",
    )
    records = [(kind, deepcopy(example[kind][0])) for kind in kinds]
    run = deepcopy(example["run_record"][0])
    run.update(status="prepared", revision=1, result_turn_id=None)
    chain = deepcopy(example["chain_run"][0])
    chain.update(status="prepared", output_id=None)
    reference = deepcopy(example["visible_message_ref"][0])
    reference["boundary"].update(input_status="pending", chain_run_id=None)
    records.extend((
        ("run_record", run), ("chain_run", chain),
        ("visible_message", deepcopy(example["visible_message"][0])),
        ("visible_message_ref", reference),
    ))
    store.save_bundle(records, "start", expected_session_revisions={uid(4): 0})
    return records


def fact(kind, payload, sequence=1, **changes):
    value = {
        "schema_version": 1, "fact_id": uid(1000 + sequence),
        "run_id": uid(7), "chain_run_id": uid(8), "workflow_session_id": uid(4),
        "node_binding_id": uid(3), "snapshot_id": uid(6), "generation": uid(900),
        "sequence": sequence, "kind": kind,
        "created_at": "2026-09-29T00:00:00.000Z", "payload": deepcopy(payload),
    }
    value.update(changes)
    return value


def request(snapshot, request_id=uid(10), request_index=1, messages=None):
    return {
        "request_id": request_id, "request_index": request_index,
        "messages": deepcopy(snapshot["s0"] if messages is None else messages),
        "tools": [{
            "type": "function", "function": {
                "name": item["name"], "parameters": deepcopy(item["parameters_schema"]),
                **({"description": item["description"]} if "description" in item else {}),
            },
        } for item in snapshot["tool_definitions"]],
        "model_parameters": deepcopy(snapshot["model_parameters"]),
    }


def attempt_start(request_id=uid(10), request_index=1, attempt_index=1, retry_index=0):
    return {
        "request_id": request_id, "attempt_id": uid(2000 + attempt_index),
        "request_index": request_index, "attempt_index": attempt_index, "retry_index": retry_index,
    }


def attempt_finish(request_id=uid(10), attempt_index=1, outcome="responded"):
    return {
        "request_id": request_id, "attempt_id": uid(2000 + attempt_index), "outcome": outcome,
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        "response_id": "provider-response", "model": "offline_fixture",
    }


def successful_facts(example):
    snapshot = example["input_snapshot"][0]
    messages = example["turn"][0]["messages"]
    facts = []
    accepted = []
    for index in range(2):
        assistant, result = messages[index * 2:index * 2 + 2]
        request_id = assistant["source"]["request_id"]
        block = result["blocks"][0]
        entries = [
            ("model_request", request(snapshot, request_id, index + 1, snapshot["s0"] + accepted)),
            ("model_attempt_started", attempt_start(request_id, index + 1, index + 1)),
            ("model_attempt_finished", attempt_finish(request_id, index + 1)),
            ("message_accepted", {"message": assistant}),
            ("tool_dispatch", {
                "tool_call_id": block["tool_call_id"],
                "tool_execution_id": block["tool_execution_id"],
            }),
            ("tool_settled", {
                "tool_call_id": block["tool_call_id"],
                "tool_execution_id": block["tool_execution_id"],
                "outcome": "success", "message": result,
            }),
            ("message_accepted", {"message": result}),
        ]
        for kind, payload in entries:
            facts.append(fact(kind, payload, len(facts) + 1))
        accepted.extend((assistant, result))
    return facts


def append_all(store, facts):
    for value in facts:
        assert store.append_execution_fact(value, expected_sequence=value["sequence"] - 1) == value


def observation(call_id, execution_id, reason, message_id):
    content = {
        "reason_code": reason, "error": "Execution result is not available.",
        "retry_guidance": "Assess idempotence and verify external state before retrying.",
    }
    return {
        "schema_version": 2, "message_id": message_id, "role": "tool",
        "source": {
            "kind": "runtime_tool_observation", "tool_call_id": call_id,
            "tool_execution_id": execution_id, "reason_code": reason,
        },
        "blocks": [{
            "kind": "tool_result", "tool_call_id": call_id, "tool_execution_id": execution_id,
            "status": "error", "is_error": True, "content": content,
            "model_visible_text": canonical_bytes(content).decode("utf-8"),
        }],
    }


def unknown_batch_facts(example):
    prefix = successful_facts(example)[:4]
    assistant = prefix[3]["payload"]["message"]
    second_call = deepcopy(assistant["blocks"][0])
    second_call["tool_call_id"] = uid(80)
    assistant["blocks"].append(second_call)
    first_message = observation(uid(16), uid(17), "outcome_unknown", uid(81))
    second_message = observation(uid(80), None, "never_started", uid(82))
    for kind, payload in [
        ("tool_dispatch", {"tool_call_id": uid(16), "tool_execution_id": uid(17)}),
        ("tool_settled", {
            "tool_call_id": uid(16), "tool_execution_id": uid(17),
            "outcome": "outcome_unknown", "message": first_message,
        }),
        ("message_accepted", {"message": first_message}),
        ("tool_settled", {
            "tool_call_id": uid(80), "tool_execution_id": None,
            "outcome": "never_started", "message": second_message,
        }),
        ("message_accepted", {"message": second_message}),
        ("model_request", request(
            example["input_snapshot"][0], uid(90), 2,
            example["input_snapshot"][0]["s0"] + [assistant, first_message, second_message],
        )),
    ]:
        prefix.append(fact(kind, payload, len(prefix) + 1))
    return prefix


def test_fact_history_durable_complete_detached_and_separate_from_history(database, example):
    values = successful_facts(example)
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        before = store.read_bundle()
        append_all(store, values)
        assert store.read_bundle() == before
        assert store.list_records("turn") == []
        assert store.read_execution_facts(uid(7)) == values
        detached = store.read_execution_facts(uid(7))
        detached[0]["payload"]["messages"][0]["blocks"][0]["text"] = "local edit"
        assert store.read_execution_facts(uid(7)) == values
        returned = store.append_execution_fact(values[0], expected_sequence=0)
        returned["payload"]["messages"][0]["blocks"][0]["text"] = "local edit"
        assert store.read_execution_facts(uid(7)) == values
    with closing(SqliteStore(database)) as reopened:
        assert reopened.read_execution_facts(uid(7)) == values
        assert reopened.read_bundle() == before
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 13
        assert raw.execute("SELECT COUNT(*) FROM execution_facts").fetchone()[0] == 14
        assert raw.execute(
            "SELECT COUNT(*) FROM records WHERE record_type = 'execution_fact'"
        ).fetchone()[0] == 0


def test_fact_identity_replay_is_exact_and_sequence_cas_fences_two_stores(database, example):
    values = successful_facts(example)
    with closing(SqliteStore(database)) as first, closing(SqliteStore(database)) as second:
        seed(first, example)
        first.append_execution_fact(values[0], expected_sequence=0)
        assert second.append_execution_fact(values[0], expected_sequence=0) == values[0]
        changed = deepcopy(values[0])
        changed["created_at"] = "2026-09-29T00:00:00.001Z"
        with pytest.raises(ContractValidationError, match="Immutable"):
            second.append_execution_fact(changed, expected_sequence=0)
        competing = deepcopy(values[0])
        competing["fact_id"] = uid(9999)
        with pytest.raises(ContractValidationError, match="sequence conflict"):
            second.append_execution_fact(competing, expected_sequence=0)
        with pytest.raises(ContractValidationError, match="advance by one"):
            first.append_execution_fact(values[1], expected_sequence=0)
        assert first.read_execution_facts(uid(7)) == values[:1]
        second.append_execution_fact(values[1], expected_sequence=1)
        assert first.read_execution_facts(uid(7)) == values[:2]


@pytest.mark.parametrize("point", ["fact.before_write", "fact.after_write", "fact.before_commit"])
def test_fact_fault_rolls_back_without_changing_saved_history(database, example, point):
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        before = store.read_bundle()

    def injected(current):
        if current == point:
            raise RuntimeError("injected fact failure")

    value = successful_facts(example)[0]
    with closing(SqliteStore(database, fault_injector=injected)) as store:
        with pytest.raises(RuntimeError, match="injected fact failure"):
            store.append_execution_fact(value, expected_sequence=0)
        assert store.read_execution_facts(uid(7)) == []
        assert store.read_bundle() == before
    with closing(SqliteStore(database)) as retry:
        assert retry.append_execution_fact(value, expected_sequence=0) == value


@pytest.mark.parametrize("version", [0, 1, 2, 3, 4])
def test_explicit_v5_migration_preserves_old_records_without_inventing_facts(
    database, example, version,
):
    payload = canonical_bytes(example["workflow_session"][0]).decode("utf-8")
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
        if version >= 2:
            raw.execute("ALTER TABLE idempotency ADD COLUMN request_digest TEXT")
        raw.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)", (
            "workflow_session", uid(4), payload, 0, "2026-09-29T00:00:00.000Z",
        ))
        raw.execute(f"PRAGMA user_version = {version}")
        raw.commit()
    with closing(SqliteStore(database)) as store:
        assert store.get_record("workflow_session", {"workflow_session_id": uid(4)}) == \
            example["workflow_session"][0]
        assert store.read_execution_facts(uid(7)) == []
        assert set(store.read_bundle()) == {"workflow_session"}
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 13
        assert raw.execute("SELECT COUNT(*) FROM execution_facts").fetchone()[0] == 0
        for table in (
            "workbench_session_owners", "workbench_resource_revisions",
            "workbench_resource_heads", "workbench_resource_receipts",
            "session_object_bindings", "session_object_values",
            "session_object_revision_membership", "session_object_receipts",
            "graph_state_manifests", "graph_project_packages",
            "global_resource_current", "global_resource_receipts",
            "registered_type_contracts",
        ):
            assert raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("schema_version", True),
    ("fact_id", "not-a-uuid"), ("run_id", uid(15).upper()), ("generation", 1),
    ("sequence", True), ("sequence", 0), ("sequence", 1.0),
    ("created_at", "2026-02-30T00:00:00.000Z"),
    ("created_at", "2026-09-29T00:00:00Z"), ("kind", "private_checkpoint"),
    ("payload", {"code": "failed", "category": "contract", "model_requests": 0, "attempts": 0,
                 "private_checkpoint": {}}),
])
def test_fact_structural_contract_rejects_invalid_version_identity_and_payload(field, value):
    record = fact("execution_failed", {
        "code": "failed", "category": "contract", "model_requests": 0, "attempts": 0,
    })
    record[field] = value
    with pytest.raises(ContractValidationError):
        validate_execution_fact(record)


@pytest.mark.parametrize("value", [float("inf"), (1,), {1: "invalid"}])
def test_fact_requires_strict_json_without_python_or_nonfinite_values(value):
    record = fact("model_attempt_finished", attempt_finish())
    record["payload"]["usage"] = {"invalid": value}
    with pytest.raises(ContractValidationError):
        validate_execution_fact(record)


@pytest.mark.parametrize("field,value", [
    ("run_id", uid(99)), ("chain_run_id", uid(99)), ("workflow_session_id", uid(99)),
    ("node_binding_id", uid(99)), ("snapshot_id", uid(99)),
])
def test_fact_storage_checks_run_snapshot_chain_ownership(database, example, field, value):
    record = successful_facts(example)[0]
    record[field] = value
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        with pytest.raises(ContractValidationError):
            store.append_execution_fact(record, expected_sequence=0)
        assert store.read_execution_facts(uid(7)) == []


@pytest.mark.parametrize("alter", ["missing_message", "parameters", "tool_schema", "tool_name"])
def test_request_must_include_all_frozen_messages_parameters_and_tools(database, example, alter):
    record = successful_facts(example)[0]
    payload = record["payload"]
    if alter == "missing_message":
        payload["messages"] = []
    elif alter == "parameters":
        payload["model_parameters"]["temperature"] = 1
    elif alter == "tool_schema":
        payload["tools"][0]["function"]["parameters"] = {}
    else:
        payload["tools"][0]["function"]["name"] = "another_tool"
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        with pytest.raises(ContractValidationError, match="complete|frozen"):
            store.append_execution_fact(record, expected_sequence=0)
        assert store.read_execution_facts(uid(7)) == []


@pytest.mark.parametrize("index,field,value", [
    (0, "request_index", 2), (1, "request_id", uid(999)),
    (1, "request_index", 2), (1, "attempt_index", 2), (1, "retry_index", 1),
    (2, "attempt_id", uid(999)), (4, "tool_call_id", uid(999)),
    (5, "tool_execution_id", uid(999)),
])
def test_fact_reference_and_attempt_sequence_errors_are_atomic(
    database, example, index, field, value,
):
    values = successful_facts(example)
    forged = deepcopy(values[index])
    forged["payload"][field] = value
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        append_all(store, values[:index])
        with pytest.raises(ContractValidationError):
            store.append_execution_fact(forged, expected_sequence=index)
        assert store.read_execution_facts(uid(7)) == values[:index]


def test_tools_require_prior_accepted_call_dispatch_and_exact_settled_message(database, example):
    values = successful_facts(example)
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        premature_dispatch = deepcopy(values[4])
        premature_dispatch.update(sequence=1, fact_id=uid(777))
        with pytest.raises(ContractValidationError, match="accepted call"):
            store.append_execution_fact(premature_dispatch, expected_sequence=0)
        append_all(store, values[:4])
        result_before_dispatch = deepcopy(values[5])
        result_before_dispatch.update(sequence=5)
        with pytest.raises(ContractValidationError, match="dispatch"):
            store.append_execution_fact(result_before_dispatch, expected_sequence=4)
        append_all(store, values[4:6])
        forged = deepcopy(values[6])
        forged["payload"]["message"]["blocks"][0]["content"] = {"note": "forged"}
        with pytest.raises(ContractValidationError, match="prior settlement"):
            store.append_execution_fact(forged, expected_sequence=6)
        assert store.read_execution_facts(uid(7)) == values[:6]
        store.append_execution_fact(values[6], expected_sequence=6)
        duplicate = fact("message_accepted", values[6]["payload"], 8)
        with pytest.raises(ContractValidationError, match="reused"):
            store.append_execution_fact(duplicate, expected_sequence=7)


def test_accepted_assistant_requires_responded_attempt_and_preserves_raw_arguments(example):
    values = successful_facts(example)
    with pytest.raises(ContractValidationError, match="responded"):
        validate_execution_fact_history([fact("message_accepted", values[3]["payload"])])
    forged = deepcopy(values)
    forged[3]["payload"]["message"]["blocks"][0]["raw_arguments"] = '{"missing": 1}'
    with pytest.raises(ContractValidationError, match="raw_arguments"):
        validate_execution_fact_history(forged, snapshot=example["input_snapshot"][0])


def test_attempt_retry_is_separate_from_request_and_keeps_reported_or_unknown_usage(example):
    snapshot = example["input_snapshot"][0]
    entries = [
        ("model_request", request(snapshot)),
        ("model_attempt_started", attempt_start()),
        ("model_attempt_finished", {**attempt_finish(outcome="model_error"), "usage": None}),
        ("model_attempt_started", attempt_start(attempt_index=2, retry_index=1)),
        ("model_attempt_finished", attempt_finish(attempt_index=2)),
    ]
    values = [fact(kind, payload, index + 1) for index, (kind, payload) in enumerate(entries)]
    assert validate_execution_fact_history(values, snapshot=snapshot) == values
    assert values[2]["payload"]["usage"] is None
    assert values[4]["payload"]["usage"]["prompt_tokens"] == 12
    responded_retry = fact("model_attempt_started", attempt_start(attempt_index=3, retry_index=2), 6)
    with pytest.raises(ContractValidationError, match="Responded"):
        validate_execution_fact_history([*values, responded_retry], snapshot=snapshot)


def test_request_without_attempt_and_new_resume_generation_are_valid_but_never_regress(example):
    snapshot = example["input_snapshot"][0]
    first = fact("model_request", request(snapshot))
    resumed = fact("model_request", request(snapshot, uid(40), 2), 2, generation=uid(901))
    assert validate_execution_fact_history([first, resumed], snapshot=snapshot) == [first, resumed]
    reverted = fact("execution_failed", {
        "code": "model_error", "category": "model", "model_requests": 2, "attempts": 0,
    }, 3, generation=uid(900))
    with pytest.raises(ContractValidationError, match="retired"):
        validate_execution_fact_history([first, resumed, reverted], snapshot=snapshot)


def test_unclosed_dispatch_and_unknown_fault_remain_saved_without_invented_result(database, example):
    values = successful_facts(example)[:5]
    block = example["turn"][0]["messages"][1]["blocks"][0]
    unknown = fact("tool_settled", {
        "tool_call_id": block["tool_call_id"], "tool_execution_id": block["tool_execution_id"],
        "outcome": "unknown", "message": None,
    }, 6)
    failure = fact("execution_failed", {
        "code": "tool_contract_error", "category": "contract", "model_requests": 1, "attempts": 1,
    }, 7)
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        append_all(store, [*values, unknown, failure])
        assert store.list_records("turn") == []
    with closing(SqliteStore(database)) as reopened:
        assert reopened.read_execution_facts(uid(7)) == [*values, unknown, failure]
        assert reopened.read_execution_facts(uid(7))[-2]["payload"]["message"] is None


@pytest.mark.parametrize("stage", [1, 2, 5, 6])
def test_every_incomplete_boundary_reopens_without_synthetic_completion(database, example, stage):
    values = successful_facts(example)[:stage]
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        append_all(store, values)
    with closing(SqliteStore(database)) as reopened:
        assert reopened.read_execution_facts(uid(7)) == values


@pytest.mark.parametrize("corruption", [
    "unknown_kind", "unknown_version", "duplicate_json_key", "column_identity", "sequence_gap",
    "forged_owner", "missing_dispatch",
])
def test_fact_reopen_detects_corrupt_or_unknown_persisted_data(database, example, corruption):
    values = successful_facts(example)[:7]
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        append_all(store, values)
    with closing(sqlite3.connect(database)) as raw:
        original = deepcopy(values[0])
        if corruption == "unknown_kind":
            original["kind"] = "private_checkpoint"
        elif corruption == "unknown_version":
            original["schema_version"] = 99
        elif corruption == "column_identity":
            original["fact_id"] = uid(888)
        elif corruption == "forged_owner":
            original["workflow_session_id"] = uid(888)
        elif corruption == "sequence_gap":
            raw.execute("DELETE FROM execution_facts WHERE sequence = 2")
        elif corruption == "missing_dispatch":
            value = deepcopy(values[4])
            value.update(kind="execution_failed", payload={
                "code": "failed", "category": "contract", "model_requests": 1, "attempts": 1,
            })
            raw.execute(
                "UPDATE execution_facts SET payload = ? WHERE sequence = 5",
                (canonical_bytes(value).decode("utf-8"),),
            )
        if corruption == "duplicate_json_key":
            text = '{"schema_version":1,' + canonical_bytes(original).decode("utf-8")[1:]
            raw.execute("UPDATE execution_facts SET payload = ? WHERE sequence = 1", (text,))
        elif corruption not in {"sequence_gap", "missing_dispatch"}:
            raw.execute(
                "UPDATE execution_facts SET payload = ? WHERE sequence = 1",
                (canonical_bytes(original).decode("utf-8"),),
            )
        raw.commit()
    with closing(SqliteStore(database)) as reopened:
        with pytest.raises(ContractValidationError):
            reopened.read_execution_facts(uid(7))
        with pytest.raises(ContractValidationError):
            reopened.append_execution_fact(successful_facts(example)[7], expected_sequence=7)


def test_fact_failure_counters_are_facts_not_fabricated_usage(database, example):
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        forged = fact("execution_failed", {
            "code": "adapter_contract_error", "category": "contract",
            "model_requests": 1, "attempts": 1,
        })
        with pytest.raises(ContractValidationError, match="counters"):
            store.append_execution_fact(forged, expected_sequence=0)
        valid = fact("execution_failed", {
            "code": "configuration_error", "category": "contract",
            "model_requests": 0, "attempts": 0,
        })
        assert store.append_execution_fact(valid, expected_sequence=0) == valid


@pytest.mark.parametrize("outcome", ["success", "error", "outcome_unknown", "interrupted", "unknown"])
def test_only_never_started_may_omit_tool_execution_identity(outcome):
    record = fact("tool_settled", {
        "tool_call_id": uid(16), "tool_execution_id": None, "outcome": outcome, "message": None,
    })
    with pytest.raises(ContractValidationError, match="Only never_started"):
        validate_execution_fact(record)


def test_unknown_then_unstarted_batch_is_complete_without_fabricated_dispatch(database, example):
    values = unknown_batch_facts(example)
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        append_all(store, values)
        facts = store.read_execution_facts(uid(7))
        dispatches = [row for row in facts if row["kind"] == "tool_dispatch"]
        assert len(dispatches) == 1
        skipped = next(row for row in facts if row["kind"] == "tool_settled"
                       and row["payload"]["outcome"] == "never_started")
        assert skipped["payload"]["tool_execution_id"] is None
        assert facts[-1]["payload"]["messages"][-1] == skipped["payload"]["message"]


@pytest.mark.parametrize("prefix_length,kind,payload", [
    (4, "tool_dispatch", {"tool_call_id": uid(80), "tool_execution_id": uid(83)}),
    (4, "tool_settled", {
        "tool_call_id": uid(80), "tool_execution_id": None, "outcome": "never_started",
        "message": observation(uid(80), None, "never_started", uid(84)),
    }),
    (4, "tool_settled", {
        "tool_call_id": uid(16), "tool_execution_id": None, "outcome": "never_started",
        "message": observation(uid(16), None, "never_started", uid(84)),
    }),
    (7, "tool_dispatch", {"tool_call_id": uid(80), "tool_execution_id": uid(83)}),
])
def test_dispatch_and_settlement_cannot_bypass_order_or_unknown_batch(
    database, example, prefix_length, kind, payload,
):
    values = unknown_batch_facts(example)
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        append_all(store, values[:prefix_length])
        bypass = fact(kind, payload, prefix_length + 1, fact_id=uid(988))
        with pytest.raises(ContractValidationError, match="order|uncertain"):
            store.append_execution_fact(bypass, expected_sequence=prefix_length)
        assert store.read_execution_facts(uid(7)) == values[:prefix_length]


def test_request_cannot_skip_a_known_result_before_its_message_acceptance(database, example):
    values = successful_facts(example)
    messages = example["turn"][0]["messages"][:2]
    bypass = fact("model_request", request(
        example["input_snapshot"][0], uid(90), 2, example["input_snapshot"][0]["s0"] + messages,
    ), 7)
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        append_all(store, values[:6])
        with pytest.raises(ContractValidationError, match="unaccepted"):
            store.append_execution_fact(bypass, expected_sequence=6)


@pytest.mark.parametrize("role,source", [
    ("system", {"kind": "prompt", "prompt_id": uid(700), "revision": 1}),
    ("user", {"kind": "human", "visible_message_id": uid(14)}),
    ("user", {"kind": "upstream_node", "output_id": uid(701)}),
    ("user", {"kind": "prompt", "prompt_id": uid(700), "revision": 1}),
])
def test_message_facts_cannot_claim_frozen_or_invented_external_messages(role, source):
    message = {
        "schema_version": 1, "message_id": uid(702), "role": role, "source": source,
        "blocks": [{"kind": "text", "text": "This is not a kernel increment."}],
    }
    with pytest.raises(ContractValidationError, match="kernel-produced"):
        validate_execution_fact(fact("message_accepted", {"message": message}))


def test_tool_message_acceptance_cannot_reorder_settled_batch(example):
    values = unknown_batch_facts(example)
    wrong = fact("message_accepted", values[8]["payload"], 7)
    with pytest.raises(ContractValidationError, match="call order"):
        validate_execution_fact_history([*values[:6], wrong], snapshot=example["input_snapshot"][0])


def test_repeated_owner_checks_do_not_scale_with_fact_count(database, example, monkeypatch):
    values = successful_facts(example)
    with closing(SqliteStore(database)) as store:
        seed(store, example)
        append_all(store, values)
        original = store._execution_fact_owner
        seen = []

        def checked(value):
            seen.append(value["fact_id"])
            return original(value)

        monkeypatch.setattr(store, "_execution_fact_owner", checked)
        assert store.read_execution_facts(uid(7)) == values
        assert len(seen) == 1
        seen.clear()
        assert store.append_execution_fact(values[0], expected_sequence=0) == values[0]
        assert len(seen) == 1
