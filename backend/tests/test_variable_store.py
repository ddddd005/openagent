"""Variable preparation primitives, exact snapshots and shared transaction rollback."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, content_digest, loads_strict
from phase1_agent.prompt_variables import assign_variable
from phase1_agent.prompt_variables import create_variable_registry
from phase1_agent.storage import SqliteStore
from phase1_agent.variable_store import VariableStore


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
VARIABLE_TABLES = (
    "variable_registries", "variable_states", "variable_heads", "variable_preparations",
)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="variables-", dir=Path(__file__).parent) as directory:
        yield Path(directory) / "workflow.sqlite"


def seed(store, *, session_ids=(4, 40)):
    example = loads_strict(EXAMPLE.read_text(encoding="utf-8"))
    records = [
        (kind, deepcopy(row))
        for kind in ("node_definition", "node_binding", "workflow_definition_revision")
        for row in example[kind]
    ]
    second_binding = deepcopy(example["node_binding"][0])
    second_binding["node_binding_id"] = uid(30)
    records.append(("node_binding", second_binding))
    records[2][1]["bindings"].append(uid(30))
    sessions = []
    for number in session_ids:
        session = deepcopy(example["workflow_session"][0])
        session["workflow_session_id"] = uid(number)
        sessions.append(("workflow_session", session))
    store.save_bundle(
        [*records, *sessions], "seed", operation="variable-test-seed",
        expected_session_revisions={uid(number): 0 for number in session_ids},
    )


def registry(definitions=None):
    return create_variable_registry(
        workflow_id=uid(2), revision=1,
        definitions=definitions if definitions is not None else [
            {"name": "name", "type": "string", "default": ""},
            {"name": "counter", "type": "integer"},
            {"name": "ratio", "type": "number", "default": 1.0},
            {"name": "enabled", "type": "boolean", "default": False},
        ],
    )


def assignment(name="counter", value=5, node_id="assign-1"):
    return {"node_id": node_id, "name": name, "value": value}


def prepare(variables, *, session=4, expected=0, assignments=None, key="prepare"):
    return variables.prepare(
        workflow_session_id=uid(session), workflow_id=uid(2), registry_revision=1,
        expected_revision=expected, assignments=[] if assignments is None else assignments,
        idempotency_key=key,
    )


def current(variables, session=4):
    return variables.get_current(
        workflow_session_id=uid(session), workflow_id=uid(2), registry_revision=1,
    )


def reference(state):
    return {field: state[field] for field in (
        "workflow_session_id", "workflow_id", "registry_revision", "revision",
    )}


def assert_reason(error, reason, status):
    assert error.value.reason_code == reason
    assert error.value.status_code == status


def setup(store):
    seed(store)
    variables = VariableStore(store)
    variables.register_registry(registry())
    return variables


def test_exact_registry_and_initialization_have_detached_json_values(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        original = registry()
        assert variables.register_registry(original) == original
        original["definitions"][0]["default"] = "caller changed"
        loaded = variables.get_registry(uid(2), 1)
        loaded["definitions"][0]["default"] = "reader changed"
        assert variables.get_registry(uid(2), 1) == registry()
        assert variables.get_registry(uid(2), 2) is None
        assert current(variables) is None

        receipt = prepare(variables)
        assert receipt["kind"] == "variable_preparation_receipt"
        assert receipt["before"] is None
        state = receipt["after"]
        assert state["revision"] == 1
        assert state["previous_ref"] is None
        assert state["values"]["name"]["value"] == ""
        assert "value" not in state["values"]["counter"]
        assert type(state["values"]["ratio"]["value"]) is float
        assert state["values"]["enabled"]["value"] is False
        assert set(state["values"]) == {"name", "counter", "ratio", "enabled"}
        state["values"]["name"]["value"] = "returned edit"
        assert current(variables)["values"]["name"]["value"] == ""
        assert variables.read_preparation("prepare")["after"]["values"]["name"]["value"] == ""
        assert canonical_bytes(current(variables)) == canonical_bytes(loads_strict(
            canonical_bytes(current(variables)).decode("utf-8"),
        ))
        assert len(store.list_records("run_record")) == 0


def test_session_global_values_are_independent_and_presets_rebound_per_consumer(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        first = prepare(variables, assignments=[assignment(value=10)])
        second = prepare(variables, session=40, assignments=[assignment(value=20)], key="second")
        first_ref = reference(first["after"])
        snapshot_a = variables.read_snapshot(first_ref, node_binding_id=uid(3))
        snapshot_b = variables.read_snapshot(first_ref, node_binding_id=uid(30))
        other = variables.read_snapshot(reference(second["after"]), node_binding_id=uid(3))
        assert snapshot_a["values"]["counter"] == snapshot_b["values"]["counter"]
        assert snapshot_a["node_binding_id"] == uid(3)
        assert snapshot_b["values"]["node_binding_id"]["value"] == uid(30)
        assert other["values"]["workflow_session_id"]["value"] == uid(40)
        assert other["values"]["counter"]["value"] == 20
        snapshot_b["values"]["counter"]["value"] = 100
        assert variables.read_snapshot(first_ref, node_binding_id=uid(30))[
            "values"
        ]["counter"]["value"] == 10
        assert current(variables, 40)["values"]["counter"]["value"] == 20


def test_ordered_assignments_overwrite_current_and_keep_every_exact_assignment(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        before = prepare(variables)["after"]
        assignments = [assignment(value=1), assignment(value=2, node_id="assign-2")]
        receipt = prepare(variables, expected=1, assignments=assignments, key="replace")
        assert receipt["before"] == before
        assert receipt["request"]["assignments"] == assignments
        assert receipt["after"]["previous_ref"] == reference(before)
        assert receipt["after"]["values"]["counter"] == {
            "type": "integer", "value": 2,
            "source": {"kind": "assignment", "workflow_id": uid(2),
                       "registry_revision": 1, "node_id": "assign-2"},
        }
        assignments[1]["value"] = 999
        assert variables.read_preparation("replace") == receipt
        assert variables.get_state(reference(before)) == before
        assert current(variables)["revision"] == 2


def test_receipt_replay_after_newer_revision_and_process_reopen(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        first = prepare(variables, assignments=[assignment(value=1)])
        prepare(variables, expected=1, assignments=[assignment(value=2)], key="second")
        assert prepare(variables, assignments=[assignment(value=1)]) == first
        assert current(variables)["revision"] == 2
    with closing(SqliteStore(database)) as store:
        variables = VariableStore(store)
        assert prepare(variables, assignments=[assignment(value=1)]) == first
        assert current(variables)["revision"] == 2
        assert store._connection.execute("SELECT COUNT(*) FROM variable_states").fetchone()[0] == 2
        assert store._connection.execute(
            "SELECT COUNT(*) FROM variable_preparations",
        ).fetchone()[0] == 2


@pytest.mark.parametrize("changes", [
    {"assignments": [assignment(value=2)]},
    {"session": 40},
    {"expected": 1},
    {"assignments": [assignment(value=1, node_id="different-node")]},
])
def test_same_key_different_request_rejects_without_mutation(database, changes):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        first = prepare(variables, assignments=[assignment(value=1)])
        kwargs = {"assignments": [assignment(value=1)], **changes}
        with pytest.raises(ContractValidationError) as error:
            prepare(variables, **kwargs)
        assert_reason(error, "idempotency_conflict", 409)
        assert current(variables) == first["after"]
        assert current(variables, 40) is None


@pytest.mark.parametrize("first,second", [(1, 1.0), (1.0, 1)])
def test_same_json_identity_distinguishes_integer_from_float(database, first, second):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        prepare(variables, assignments=[assignment(name="ratio", value=first)])
        with pytest.raises(ContractValidationError) as error:
            prepare(variables, assignments=[assignment(name="ratio", value=second)])
        assert_reason(error, "idempotency_conflict", 409)
        assert type(current(variables)["values"]["ratio"]["value"]) is type(first)


def test_cas_conflict_and_immutable_registry_refuse_same_python_equal_number(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        prepare(variables)
        with pytest.raises(ContractValidationError) as error:
            prepare(variables, assignments=[assignment()], key="stale")
        assert_reason(error, "stale_revision", 409)
        changed = registry()
        changed["definitions"][2]["default"] = 1
        with pytest.raises(ContractValidationError) as error:
            variables.register_registry(changed)
        assert_reason(error, "immutable_conflict", 409)
        assert variables.get_registry(uid(2), 1) == registry()
        assert variables.read_preparation("stale") is None


@pytest.mark.parametrize("point", [
    "variable_before_state_write", "variable_after_state_write",
    "variable_after_head_write", "variable_after_receipt_write", "variable_before_commit",
])
def test_fault_rolls_back_state_head_and_receipt_and_retry_is_safe(database, point):
    with closing(SqliteStore(database)) as store:
        setup(store)

    def fail(observed):
        if observed == point:
            raise RuntimeError("injected variable failure")

    with closing(SqliteStore(database, fault_injector=fail)) as store:
        variables = VariableStore(store)
        with pytest.raises(RuntimeError, match="injected variable failure"):
            prepare(variables, assignments=[assignment()])
        assert current(variables) is None
        assert variables.read_preparation("prepare") is None
        for table in VARIABLE_TABLES[1:]:
            assert store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with closing(SqliteStore(database)) as store:
        assert prepare(VariableStore(store), assignments=[assignment()])["after"]["revision"] == 1


@pytest.mark.parametrize("point", [
    "variable_before_registry_write", "variable_after_registry_write", "variable_before_commit",
])
def test_registry_failure_has_no_partial_definition(database, point):
    with closing(SqliteStore(database)) as store:
        seed(store)

    def fail(observed):
        if observed == point:
            raise RuntimeError("registry failure")

    with closing(SqliteStore(database, fault_injector=fail)) as store:
        variables = VariableStore(store)
        with pytest.raises(RuntimeError, match="registry failure"):
            variables.register_registry(registry())
        assert variables.get_registry(uid(2), 1) is None


def test_preparation_shares_outer_durable_boundary_and_rollback_keeps_saved_history(database):
    with closing(SqliteStore(database)) as store:
        seed(store)
        variables = VariableStore(store)
        store._connection.execute("BEGIN IMMEDIATE")
        variables.register_registry_in_transaction(registry())
        receipt = variables.prepare_in_transaction(
            workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
            expected_revision=0, assignments=[assignment()], idempotency_key="atomic",
        )
        assert store._connection.in_transaction
        store._connection.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            ("test-boundary", "prepared-input", '{"saved":"new frozen input"}', 1, "now"),
        )
        store._connection.execute("ROLLBACK")
        assert variables.get_registry(uid(2), 1) is None
        assert variables.read_preparation("atomic") is None
        assert store._get("test-boundary", "prepared-input") is None
        assert store.get_record("workflow_session", {"workflow_session_id": uid(4)})[
            "revision"
        ] == 1

        store._connection.execute("BEGIN IMMEDIATE")
        variables.register_registry_in_transaction(registry())
        committed = variables.prepare_in_transaction(
            workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
            expected_revision=0, assignments=[assignment()], idempotency_key="atomic",
        )
        assert committed == receipt
        store._connection.execute("COMMIT")
    with closing(SqliteStore(database)) as store:
        assert VariableStore(store).read_preparation("atomic") == receipt


def test_savepoint_failure_preserves_callers_transaction_without_partial_variable_writes(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        store._connection.execute("BEGIN IMMEDIATE")
        store._connection.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            ("test-boundary", "caller-write", '{"saved":true}', 1, "now"),
        )
        store._fault_injector = lambda point: (
            (_ for _ in ()).throw(RuntimeError("savepoint failure"))
            if point == "variable_after_head_write" else None
        )
        with pytest.raises(RuntimeError, match="savepoint failure"):
            variables.prepare_in_transaction(
                workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
                expected_revision=0, assignments=[assignment()], idempotency_key="shared",
            )
        assert store._connection.in_transaction
        assert current(variables) is None
        assert variables.read_preparation("shared") is None
        assert store._get("test-boundary", "caller-write") == {"saved": True}
        store._connection.execute("ROLLBACK")


@pytest.mark.parametrize("operation", ["register", "prepare", "fork"])
def test_transaction_composable_methods_refuse_autocommit(database, operation):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        state = prepare(variables)["after"]
        with pytest.raises(ContractValidationError) as error:
            if operation == "register":
                variables.register_registry_in_transaction(registry())
            elif operation == "prepare":
                variables.prepare_in_transaction(
                    workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
                    expected_revision=1, assignments=[], idempotency_key="nested",
                )
            else:
                variables.fork_in_transaction(
                    source_ref=reference(state), workflow_session_id=uid(40),
                    expected_revision=0, idempotency_key="nested",
                )
        assert_reason(error, "storage_contract_violation", 500)
        assert current(variables)["revision"] == 1


def test_atomic_convenience_refuses_outer_transaction_without_rolling_it_back(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        store._connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ContractValidationError) as error:
            prepare(variables)
        assert_reason(error, "storage_contract_violation", 500)
        assert store._connection.in_transaction
        assert current(variables) is None
        store._connection.execute("ROLLBACK")


def test_fork_uses_explicit_stable_revision_and_is_independent_after_reopen(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        stable = prepare(variables, assignments=[assignment(value=11)])["after"]
        prepare(variables, expected=1, assignments=[assignment(value=22)], key="newer")
        child = variables.fork(
            source_ref=reference(stable), workflow_session_id=uid(40),
            expected_revision=0, idempotency_key="fork",
        )
        assert child["after"]["values"] == stable["values"]
        assert child["after"]["source"]["source_ref"] == reference(stable)
        assert child["after"]["workflow_session_id"] == uid(40)
        child_snapshot = variables.read_snapshot(reference(child["after"]), node_binding_id=uid(30))
        assert child_snapshot["values"]["workflow_session_id"]["value"] == uid(40)
        assert child_snapshot["values"]["node_binding_id"]["value"] == uid(30)
        prepare(variables, session=40, expected=1, assignments=[assignment(value=33)], key="child")
        assert current(variables)["values"]["counter"]["value"] == 22
        assert current(variables, 40)["values"]["counter"]["value"] == 33
        assert variables.get_state(reference(stable))["values"]["counter"]["value"] == 11
        assert store.list_records("run_record") == []
    with closing(SqliteStore(database)) as store:
        variables = VariableStore(store)
        assert variables.fork(
            source_ref=reference(stable), workflow_session_id=uid(40),
            expected_revision=0, idempotency_key="fork",
        ) == child
        assert current(variables, 40)["revision"] == 2
        assert current(variables)["revision"] == 2


def test_fork_is_composable_and_conflicts_cannot_overwrite_existing_child(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        stable = prepare(variables)["after"]
        store._connection.execute("BEGIN IMMEDIATE")
        variables.fork_in_transaction(
            source_ref=reference(stable), workflow_session_id=uid(40),
            expected_revision=0, idempotency_key="fork",
        )
        store._connection.execute("ROLLBACK")
        assert current(variables, 40) is None
        prepare(variables, session=40, key="existing")
        with pytest.raises(ContractValidationError) as error:
            variables.fork(
                source_ref=reference(stable), workflow_session_id=uid(40),
                expected_revision=0, idempotency_key="fork",
            )
        assert_reason(error, "stale_revision", 409)
        assert variables.read_preparation("fork") is None


@pytest.mark.parametrize("bad_assignment", [
    assignment(name="unknown"), assignment(name="workflow_session_id", value=uid(40)),
    assignment(value=True), assignment(value=1.0), assignment(value=float("inf")),
    assignment(node_id=""), assignment(node_id=" "), assignment(node_id=7),
    {"node_id": "assign", "name": "counter"}, {"node_id": "assign", "name": "counter",
                                               "value": 1, "extra": True},
])
def test_bad_assignment_keeps_prior_state_and_no_success_receipt(database, bad_assignment):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        original = prepare(variables)["after"]
        with pytest.raises(ContractValidationError):
            prepare(variables, expected=1, assignments=[assignment(), bad_assignment], key="bad")
        assert current(variables) == original
        assert variables.read_preparation("bad") is None


@pytest.mark.parametrize("field,value", [
    ("workflow_session_id", "session"), ("workflow_session_id", uid(400)),
    ("workflow_id", "workflow"), ("workflow_id", uid(200)),
    ("registry_revision", True), ("registry_revision", 2),
    ("expected_revision", True), ("expected_revision", -1),
    ("assignments", ()), ("idempotency_key", ""), ("idempotency_key", "a" * 129),
    ("idempotency_key", "\ud800"),
])
def test_bad_identity_missing_owner_or_wrong_revision_refuses_preparation(database, field, value):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        kwargs = {
            "workflow_session_id": uid(4), "workflow_id": uid(2), "registry_revision": 1,
            "expected_revision": 0, "assignments": [], "idempotency_key": "invalid",
            field: value,
        }
        with pytest.raises(ContractValidationError):
            variables.prepare(**kwargs)
        assert current(variables) is None
        assert variables.read_preparation("invalid") is None


@pytest.mark.parametrize("binding", [uid(300), "not-uuid", uid(0xABC).upper(), None])
def test_consumer_binding_must_belong_to_exact_workflow_definition(database, binding):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        state = prepare(variables)["after"]
        with pytest.raises(ContractValidationError):
            variables.read_snapshot(reference(state), node_binding_id=binding)
        assert current(variables) == state


def test_missing_registry_or_different_session_workflow_refuses_without_invented_defaults(database):
    with closing(SqliteStore(database)) as store:
        seed(store)
        variables = VariableStore(store)
        with pytest.raises(ContractValidationError) as error:
            prepare(variables)
        assert_reason(error, "invalid_request", 400)
        assert variables.read_preparation("prepare") is None
        variables.register_registry(registry())
        session = store._get("workflow_session", uid(4))
        session["definition_revision"] = 2
        store._connection.execute(
            "UPDATE records SET payload = ? WHERE record_type = 'workflow_session' AND record_id = ?",
            (canonical_bytes(session).decode("utf-8"), uid(4)),
        )
        with pytest.raises(ContractValidationError) as error:
            prepare(variables)
        assert_reason(error, "invalid_request", 400)
        assert store._connection.execute("SELECT COUNT(*) FROM variable_states").fetchone()[0] == 0


@pytest.mark.parametrize("corruption", [
    "bad_json", "extra", "before", "after", "request", "digest", "missing_after",
])
def test_corrupt_receipt_never_replays_fabricated_success(database, corruption):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        receipt = prepare(variables, assignments=[assignment()])
        if corruption == "bad_json":
            payload = "{"
        else:
            if corruption == "extra":
                receipt["extra"] = True
            elif corruption == "before":
                receipt["before"] = receipt["after"]
            elif corruption == "after":
                receipt["after"]["values"]["counter"]["value"] = 500
            elif corruption == "request":
                receipt["request"]["assignments"][0]["value"] = 500
            elif corruption == "missing_after":
                store._connection.execute("DELETE FROM variable_states")
            payload = canonical_bytes(receipt).decode("utf-8")
        store._connection.execute(
            "UPDATE variable_preparations SET payload = ? WHERE idempotency_key = 'prepare'",
            (payload,),
        )
        if corruption == "digest":
            store._connection.execute("UPDATE variable_preparations SET digest = 'bad'")
        with pytest.raises(ContractValidationError) as error:
            prepare(variables, assignments=[assignment()])
        assert_reason(error, "storage_contract_violation", 500)


@pytest.mark.parametrize("mutation", [
    lambda state: state.update(schema_version=True),
    lambda state: state.update(revision=2),
    lambda state: state.update(workflow_session_id=uid(40)),
    lambda state: state["values"].update(node_binding_id={"type": "string", "value": uid(3)}),
    lambda state: state["values"]["counter"].update(value=True),
    lambda state: state["values"]["counter"]["source"].update(registry_revision=True),
    lambda state: state.update(previous_ref={"bad": "reference"}),
    lambda state: state["source"].update(kind="active_run"),
])
def test_corrupt_state_cannot_be_read_as_snapshot_or_current(database, mutation):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        state = prepare(variables, assignments=[assignment()])["after"]
        stable_ref = reference(state)
        mutation(state)
        store._connection.execute(
            "UPDATE variable_states SET payload = ?",
            (canonical_bytes(state).decode("utf-8"),),
        )
        for read in (
            lambda: variables.get_state(stable_ref),
            lambda: current(variables),
            lambda: variables.read_snapshot(stable_ref, node_binding_id=uid(3)),
        ):
            with pytest.raises(ContractValidationError) as error:
                read()
            assert_reason(error, "storage_contract_violation", 500)


@pytest.mark.parametrize("link", ["previous", "fork"])
def test_saved_state_missing_history_reference_is_not_accepted(database, link):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        first = prepare(variables)["after"]
        if link == "previous":
            state = prepare(variables, expected=1, key="second")["after"]
        else:
            state = variables.fork(
                source_ref=reference(first), workflow_session_id=uid(40),
                expected_revision=0, idempotency_key="fork",
            )["after"]
        store._connection.execute(
            "DELETE FROM variable_states WHERE workflow_session_id = ? AND revision = 1",
            (uid(4),),
        )
        with pytest.raises(ContractValidationError) as error:
            variables.get_state(reference(state))
        assert_reason(error, "storage_contract_violation", 500)


def test_concurrent_cas_has_one_winner_and_identical_replay_has_one_state(database):
    with closing(SqliteStore(database)) as store:
        setup(store)

    def write(number):
        with closing(SqliteStore(database)) as store:
            try:
                return prepare(VariableStore(store), assignments=[assignment(value=number)],
                               key=f"race-{number}")
            except ContractValidationError as exc:
                return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (1, 2)))
    winners = [result for result in results if isinstance(result, dict)]
    losers = [result for result in results if isinstance(result, ContractValidationError)]
    assert len(winners) == len(losers) == 1
    assert losers[0].reason_code == "stale_revision"
    with closing(SqliteStore(database)) as store:
        assert current(VariableStore(store)) == winners[0]["after"]
        assert store._connection.execute("SELECT COUNT(*) FROM variable_states").fetchone()[0] == 1

    def replay(_):
        request = winners[0]["request"]
        with closing(SqliteStore(database)) as store:
            return VariableStore(store).prepare(
                **{key: value for key, value in request.items() if key != "operation"},
                idempotency_key=winners[0]["idempotency_key"],
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        replays = list(pool.map(replay, (1, 2)))
    assert replays == [winners[0], winners[0]]
    with closing(SqliteStore(database)) as store:
        assert store._connection.execute("SELECT COUNT(*) FROM variable_preparations").fetchone()[0] == 1


def test_v7_migration_retains_v6_data_and_does_not_invent_variable_state(database):
    with closing(SqliteStore(database)) as store:
        seed(store)
        before = store.read_bundle()
        store._connection.execute(
            "INSERT INTO prompt_revisions VALUES (?, ?, ?, ?)",
            ("item", uid(99), 1, '{"saved":"old-prompt-row"}'),
        )
        for table in reversed(VARIABLE_TABLES):
            store._connection.execute(f"DROP TABLE {table}")
        store._connection.execute("PRAGMA user_version = 6")
    with closing(SqliteStore(database)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert store.read_bundle() == before
        assert store._connection.execute("SELECT payload FROM prompt_revisions").fetchone()[0] == (
            '{"saved":"old-prompt-row"}'
        )
        for table in VARIABLE_TABLES:
            assert store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert VariableStore(store).get_registry(uid(2), 1) is None
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 13


def request_digest(value):
    return "workflow-op-v1:" + content_digest(value).rsplit(":", 1)[1]


def prepared_records():
    example = loads_strict(EXAMPLE.read_text(encoding="utf-8"))
    session = deepcopy(example["workflow_session"][0])
    session["revision"] = 2
    return [
        ("workflow_session", session),
        *[(kind, deepcopy(example[kind][0])) for kind in (
            "visible_message", "node_input", "input_snapshot",
        )],
    ]


def test_bundle_builder_and_binding_share_one_transaction_and_replay_skips_both(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        invocations = []
        state_ref = {}

        def builder(current_store):
            assert current_store is store and store._connection.in_transaction
            invocations.append("prepare")
            receipt = variables.prepare_in_transaction(
                workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
                expected_revision=0, assignments=[assignment()], idempotency_key="variables",
            )
            state_ref.update(reference(receipt["after"]))
            bound = variables.snapshot_from_state(receipt["after"], node_binding_id=uid(3))
            assert bound["values"]["counter"]["value"] == 5
            return prepared_records()

        def bind(current_store, records):
            assert current_store is store and records[-1]["snapshot_id"] == uid(6)
            invocations.append("bind")
            variables.bind_state_in_transaction("input_snapshot", uid(6), state_ref)

        arguments = {
            "request_digest": request_digest({"kind": "prepared-start", "input": "one"}),
            "operation": "variables.start", "expected_session_revisions": {uid(4): 1},
            "after_save": bind,
        }
        saved = store.save_bundle_prepared(builder, "atomic-start", **arguments)
        assert saved == [row for _, row in prepared_records()]
        assert variables.get_bound_state_ref("input_snapshot", uid(6)) == state_ref
        assert invocations == ["prepare", "bind"]
        assert store.save_bundle_prepared(builder, "atomic-start", **arguments) == saved
        assert invocations == ["prepare", "bind"]
        with pytest.raises(ContractValidationError, match="different content"):
            store.save_bundle_prepared(
                builder, "atomic-start",
                **{**arguments, "request_digest": request_digest({"different": "request"})},
            )
        assert invocations == ["prepare", "bind"]
    with closing(SqliteStore(database)) as store:
        def never(_):
            pytest.fail("Restart replay must not prepare variables again")

        assert store.save_bundle_prepared(
            never, "atomic-start", **{**arguments, "after_save": lambda *_: pytest.fail("Replay binder")},
        ) == saved
        assert VariableStore(store).get_bound_state_ref("input_snapshot", uid(6)) == state_ref


@pytest.mark.parametrize("point", [
    "variable_after_head_write", "after_first_write",
    "variable_after_binding_write", "before_commit",
])
def test_prepared_bundle_failure_rolls_back_variables_public_records_binding_and_receipts(database, point):
    with closing(SqliteStore(database)) as store:
        setup(store)

    def fail(observed):
        if observed == point:
            raise RuntimeError("atomic preparation failure")

    with closing(SqliteStore(database, fault_injector=fail)) as store:
        variables = VariableStore(store)
        original = store.read_bundle()
        frozen_ref = {}

        def builder(_):
            state = variables.prepare_in_transaction(
                workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
                expected_revision=0, assignments=[assignment()], idempotency_key="variables",
            )["after"]
            frozen_ref.update(reference(state))
            return prepared_records()

        def binder(_, records):
            variables.bind_state_in_transaction("input_snapshot", uid(6), frozen_ref)

        with pytest.raises(RuntimeError, match="atomic preparation failure"):
            store.save_bundle_prepared(
                builder, "atomic-start", request_digest=request_digest({"request": "one"}),
                operation="variables.start", expected_session_revisions={uid(4): 1},
                after_save=binder,
            )
        assert store.read_bundle() == original
        assert current(variables) is None
        assert variables.read_preparation("variables") is None
        assert store.read_receipt("variables.start", "atomic-start") is None
        assert store._connection.execute("SELECT COUNT(*) FROM variable_bindings").fetchone()[0] == 0


def test_stable_binding_is_immutable_and_does_not_follow_latest_values(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        original = prepare(variables, assignments=[assignment(value=11)])["after"]
        store.save_bundle(prepared_records(), "owner", expected_session_revisions={uid(4): 1})
        store._connection.execute("BEGIN IMMEDIATE")
        first = variables.bind_state_in_transaction("input_snapshot", uid(6), reference(original))
        assert variables.bind_state_in_transaction("input_snapshot", uid(6), reference(original)) == first
        store._connection.execute("COMMIT")
        newer = prepare(variables, expected=1, assignments=[assignment(value=22)], key="new")["after"]
        assert variables.get_bound_state_ref("input_snapshot", uid(6)) == reference(original)
        store._connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ContractValidationError) as error:
            variables.bind_state_in_transaction("input_snapshot", uid(6), reference(newer))
        assert_reason(error, "immutable_conflict", 409)
        assert store._connection.in_transaction
        store._connection.execute("ROLLBACK")
        assert variables.read_snapshot(
            variables.get_bound_state_ref("input_snapshot", uid(6)), node_binding_id=uid(3),
        )["values"]["counter"]["value"] == 11


@pytest.mark.parametrize("kind,owner,source_session", [
    ("unknown", uid(6), 4), ("input_snapshot", uid(600), 4),
    ("input_snapshot", uid(6), 40),
])
def test_binding_invalid_or_cross_session_reference_is_refused(database, kind, owner, source_session):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        state = prepare(variables, session=source_session, key="state")["after"]
        store.save_bundle(prepared_records(), "owner", expected_session_revisions={uid(4): 1})
        store._connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ContractValidationError):
            variables.bind_state_in_transaction(kind, owner, reference(state))
        assert store._connection.in_transaction
        assert store._connection.execute("SELECT COUNT(*) FROM variable_bindings").fetchone()[0] == 0
        store._connection.execute("ROLLBACK")


def test_frozen_snapshot_import_uses_exact_values_not_current_head_and_replays(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        frozen = prepare(variables, assignments=[assignment(value=11)])["after"]
        pure = variables.snapshot_from_state(frozen, node_binding_id=uid(3))
        pure = assign_variable(pure, "name", "new A root", node_id="root-dependency")
        prepare(variables, expected=1, assignments=[assignment(value=99)], key="live-newer")
        imported = variables.prepare_snapshot(
            pure, expected_revision=2, idempotency_key="frozen-B", source_ref=reference(frozen),
        )
        assert imported["before"]["values"]["counter"]["value"] == 99
        assert imported["after"]["values"]["counter"]["value"] == 11
        assert imported["after"]["values"]["name"]["value"] == "new A root"
        assert imported["after"]["source"]["kind"] == "frozen_snapshot"
        assert imported["after"]["source"]["source_ref"] == reference(frozen)
        assert imported["request"]["snapshot"] == pure
        assert variables.prepare_snapshot(
            pure, expected_revision=2, idempotency_key="frozen-B", source_ref=reference(frozen),
        ) == imported
    with closing(SqliteStore(database)) as store:
        variables = VariableStore(store)
        assert variables.prepare_snapshot(
            pure, expected_revision=2, idempotency_key="frozen-B", source_ref=reference(frozen),
        ) == imported
        assert current(variables)["revision"] == 3


@pytest.mark.parametrize("operation", ["prepare", "prepare_snapshot"])
def test_preparation_proof_digest_is_saved_replays_and_conflicting_proof_is_refused(database, operation):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        proof = content_digest({"frozen": {"masked_rule": "original"}, "rederivation": None})
        if operation == "prepare_snapshot":
            source = prepare(variables, assignments=[assignment(value=11)])["after"]
            snapshot = variables.snapshot_from_state(source, node_binding_id=uid(3))

            def call(digest):
                return variables.prepare_snapshot(
                    snapshot, expected_revision=1, idempotency_key="proof",
                    source_ref=reference(source), preparation_digest=digest,
                )
        else:
            def call(digest):
                return variables.prepare(
                    workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
                    expected_revision=0, assignments=[assignment(value=11)],
                    idempotency_key="proof", preparation_digest=digest,
                )

        receipt = call(proof)
        assert receipt["request"]["preparation_digest"] == proof
        assert variables.read_preparation("proof") == receipt
        before = deepcopy(current(variables))
        assert call(proof) == receipt
        alternate = content_digest({"frozen": {"masked_rule": "fabricated"}, "rederivation": None})
        for digest in (alternate, None):
            with pytest.raises(ContractValidationError) as error:
                call(digest)
            assert_reason(error, "idempotency_conflict", 409)
            assert current(variables) == before
            assert variables.read_preparation("proof") == receipt
    with closing(SqliteStore(database)) as store:
        assert VariableStore(store).read_preparation("proof") == receipt
        assert VariableStore(store).get_state(reference(receipt["after"])) == receipt["after"]


@pytest.mark.parametrize("operation", ["prepare", "prepare_snapshot"])
@pytest.mark.parametrize("digest", [
    True, 4, "", "workflow-op-v1:" + "a" * 64,
    "json-v1:sha256:" + "a" * 63, "json-v1:sha256:" + "a" * 65,
    "json-v1:sha256:" + "A" * 64, "json-v1:sha256:" + "g" * 64,
])
def test_invalid_preparation_proof_digest_never_writes_state_or_receipt(database, operation, digest):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        source = prepare(variables)["after"] if operation == "prepare_snapshot" else None
        before = deepcopy(current(variables))
        counts = {
            table: store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in VARIABLE_TABLES
        }
        with pytest.raises(ContractValidationError, match="preparation digest") as error:
            if source is None:
                variables.prepare(
                    workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
                    expected_revision=0, assignments=[], idempotency_key="bad-proof",
                    preparation_digest=digest,
                )
            else:
                variables.prepare_snapshot(
                    variables.snapshot_from_state(source, node_binding_id=uid(3)),
                    expected_revision=1, idempotency_key="bad-proof",
                    source_ref=reference(source), preparation_digest=digest,
                )
        assert_reason(error, "invalid_request", 400)
        assert current(variables) == before
        assert variables.read_preparation("bad-proof") is None
        assert {
            table: store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in VARIABLE_TABLES
        } == counts


def test_none_preparation_digest_keeps_original_request_shape(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        first = variables.prepare(
            workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
            expected_revision=0, assignments=[], idempotency_key="legacy",
            preparation_digest=None,
        )
        assert "preparation_digest" not in first["request"]
        assert prepare(variables, key="legacy") == first
        snapshot = variables.snapshot_from_state(first["after"], node_binding_id=uid(3))
        imported = variables.prepare_snapshot(
            snapshot, expected_revision=1, idempotency_key="legacy-snapshot",
            source_ref=reference(first["after"]), preparation_digest=None,
        )
        assert "preparation_digest" not in imported["request"]
        assert variables.prepare_snapshot(
            snapshot, expected_revision=1, idempotency_key="legacy-snapshot",
            source_ref=reference(first["after"]),
        ) == imported


def test_corrupt_proof_digest_in_stored_receipt_is_not_replayed_even_if_request_digest_is_updated(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        receipt = variables.prepare(
            workflow_session_id=uid(4), workflow_id=uid(2), registry_revision=1,
            expected_revision=0, assignments=[], idempotency_key="proof",
            preparation_digest=content_digest({"frozen": {}, "rederivation": None}),
        )
        receipt["request"]["preparation_digest"] = None
        store._connection.execute(
            "UPDATE variable_preparations SET digest = ?, payload = ? WHERE idempotency_key = 'proof'",
            (content_digest(receipt["request"]), canonical_bytes(receipt).decode("utf-8")),
        )
        with pytest.raises(ContractValidationError) as error:
            variables.read_preparation("proof")
        assert_reason(error, "storage_contract_violation", 500)


def test_frozen_snapshot_import_can_use_true_fork_ancestor_but_not_unrelated_session(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        parent = prepare(variables, assignments=[assignment(value=11)])["after"]
        snapshot = variables.snapshot_from_state(parent, node_binding_id=uid(3))
        snapshot["workflow_session_id"] = uid(40)
        snapshot["values"]["workflow_session_id"]["value"] = uid(40)
        with pytest.raises(ContractValidationError, match="fork ancestor"):
            variables.prepare_snapshot(
                snapshot, expected_revision=0, idempotency_key="not-ancestor",
                source_ref=reference(parent),
            )
        child_session = store._get("workflow_session", uid(40))
        child_session["source"] = {
            "kind": "fork", "source_workflow_session_id": uid(4),
            "visible_message_id": uid(14), "fork_anchor_id": uid(500),
        }
        store._connection.execute(
            "UPDATE records SET payload = ? WHERE record_type = 'workflow_session' AND record_id = ?",
            (canonical_bytes(child_session).decode("utf-8"), uid(40)),
        )
        imported = variables.prepare_snapshot(
            snapshot, expected_revision=0, idempotency_key="child",
            source_ref=reference(parent),
        )
        assert imported["after"]["workflow_session_id"] == uid(40)
        assert imported["after"]["values"] == parent["values"]
        assert current(variables)["values"]["counter"]["value"] == 11
        assert current(variables, 40)["values"]["counter"]["value"] == 11


def test_v8_migration_creates_no_bindings_or_result_ports(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        saved = prepare(variables, assignments=[assignment()])["after"]
        for table in ("variable_bindings", "result_port_submissions", "result_port_releases"):
            store._connection.execute(f"DROP TABLE {table}")
        store._connection.execute("PRAGMA user_version = 7")
    with closing(SqliteStore(database)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert current(VariableStore(store)) == saved
        for table in ("variable_bindings", "result_port_submissions", "result_port_releases"):
            assert store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_public_fact_read_does_not_end_or_rollback_callers_preparation_transaction(database):
    with closing(SqliteStore(database)) as store:
        setup(store)
        store._connection.execute("BEGIN IMMEDIATE")
        store._connection.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            ("test-boundary", "caller-facts", '{"saved":true}', 1, "now"),
        )
        assert store.read_execution_facts(uid(800)) == []
        assert store._connection.in_transaction
        assert store._get("test-boundary", "caller-facts") == {"saved": True}
        store._connection.execute("ROLLBACK")
        assert store._get("test-boundary", "caller-facts") is None


def test_builder_refuses_nested_transaction_without_rolling_back_callers_work(database):
    with closing(SqliteStore(database)) as store:
        setup(store)
        store._connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ContractValidationError, match="ownership"):
            store.save_bundle_prepared(
                lambda _: pytest.fail("Nested builder must not run"), "nested",
                request_digest=request_digest({"nested": True}),
            )
        assert store._connection.in_transaction
        store._connection.execute("ROLLBACK")


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(schema_version=True),
    lambda value: value.update(owner_id=uid(999)),
    lambda value: value["state_ref"].update(revision=99),
    lambda value: value.update(workflow_session_id=uid(40)),
])
def test_corrupt_variable_binding_cannot_redirect_a_stable_owner(database, mutation):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        state = prepare(variables)["after"]
        store.save_bundle(prepared_records(), "owner", expected_session_revisions={uid(4): 1})
        store._connection.execute("BEGIN IMMEDIATE")
        value = variables.bind_state_in_transaction("input_snapshot", uid(6), reference(state))
        store._connection.execute("COMMIT")
        mutation(value)
        store._connection.execute(
            "UPDATE variable_bindings SET payload = ?", (canonical_bytes(value).decode("utf-8"),),
        )
        with pytest.raises(ContractValidationError) as error:
            variables.get_bound_state_ref("input_snapshot", uid(6))
        assert_reason(error, "storage_contract_violation", 500)


def test_frozen_snapshot_import_failure_rolls_back_without_replacing_saved_basis(database):
    with closing(SqliteStore(database)) as store:
        variables = setup(store)
        state = prepare(variables)["after"]
        pure = assign_variable(
            variables.snapshot_from_state(state, node_binding_id=uid(3)),
            "counter", 12, node_id="declared",
        )

        def fail(point):
            if point == "variable_after_state_write":
                raise RuntimeError("snapshot import failure")

        store._fault_injector = fail
        with pytest.raises(RuntimeError, match="snapshot import failure"):
            variables.prepare_snapshot(
                pure, expected_revision=1, idempotency_key="import", source_ref=reference(state),
            )
        assert current(variables) == state
        assert variables.read_preparation("import") is None
