"""One accepted result has two deterministic ports after one durable archive."""

from __future__ import annotations

import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, content_digest, loads_strict
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow_result_ports import (
    ResultPortStore, make_archive_receipt, make_archive_submission, make_result_package,
    make_result_port_routes, release_result_ports, validate_archive_receipt,
    validate_archive_submission, validate_result_package, validate_result_port_release,
    validate_result_port_routes,
)

from test_execution_facts import append_all, seed, successful_facts


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


ALLOWED = (uid(3), uid(99))


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="result-ports-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder) / "workflow.sqlite"


@pytest.fixture
def example():
    path = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
    return loads_strict(path.read_text(encoding="utf-8"))


def revised(value, **changes):
    value = copy.deepcopy(value)
    value.update(changes)
    return value


def package(example):
    return make_result_package(
        example["turn"][0], example["candidate_group"][0],
        example["run_record"][0], example["workflow_output"][0],
    )


def submission(example):
    return make_archive_submission(
        package(example), make_result_port_routes(uid(3), allowed_bindings=ALLOWED),
        allowed_bindings=ALLOWED,
    )


def archived_records(value):
    return [copy.deepcopy(value["package"][key]) for key in ("turn", "group", "run")]


def receipt(value):
    return make_archive_receipt(value, archived_records(value), allowed_bindings=ALLOWED)


def fingerprint(value):
    return "workflow-op-v1:" + content_digest(value).rsplit(":", 1)[1]


def start_running(store, example):
    seed(store, example)
    run = revised(example["run_record"][0], status="running", revision=2, result_turn_id=None)
    chain = revised(example["chain_run"][0], status="running", output_id=None)
    reference = copy.deepcopy(example["visible_message_ref"][0])
    reference["boundary"].update(input_status="running")
    store.save_bundle([
        ("workflow_session", revised(example["workflow_session"][0], revision=2)),
        ("run_record", run), ("chain_run", chain), ("visible_message_ref", reference),
    ], "running", expected_session_revisions={uid(4): 1})
    append_all(store, successful_facts(example))


def save_accepted(store, example, value, *, after_put=None):
    ports = ResultPortStore(store, allowed_bindings=ALLOWED)
    records = [
        ("workflow_session", revised(example["workflow_session"][0], revision=3)),
        ("run_record", revised(example["run_record"][0], status="final_ready", revision=3, result_turn_id=None)),
    ]

    def binder(saved, _records):
        assert saved._connection.in_transaction
        assert ports.put_submission_in_transaction(value) == value
        if after_put is not None:
            after_put(saved)

    return store.save_bundle_prepared(
        lambda _saved: copy.deepcopy(records), "final-ready",
        request_digest=fingerprint({"operation": "accept", "submission": value}),
        expected_session_revisions={uid(4): 2}, after_save=binder,
    )


def archive(store, value):
    expected = value["package"]
    return store.archive_success([
        ("turn", expected["turn"]), ("candidate_group", expected["group"]),
        ("run_record", expected["run"]),
    ], value["archive_key"])


def save_output_and_release(store, example, value, saved_receipt, *, after_release=None):
    ports = ResultPortStore(store, allowed_bindings=ALLOWED)
    records = [
        ("workflow_session", revised(example["workflow_session"][0], revision=4)),
        ("workflow_output", value["package"]["output"]),
    ]

    def binder(saved, _records):
        assert saved._connection.in_transaction
        released = ports.release_in_transaction(value, saved_receipt)
        if after_release is not None:
            after_release(saved, released)

    return store.save_bundle_prepared(
        lambda _saved: copy.deepcopy(records), "output",
        request_digest=fingerprint({"operation": "release", "submission": value}),
        expected_session_revisions={uid(4): 3}, after_save=binder,
    )


def test_strict_package_retains_one_result_with_original_message_and_output_identity(example):
    value = package(example)
    assert value == validate_result_package(value)
    assert value["turn"]["messages"] == example["turn"][0]["messages"]
    assert value["output"]["payload"] == value["turn"]["final"]["value"]
    assert value["run"]["result_turn_id"] == value["turn"]["turn_id"]
    assert "s0" not in value and "snapshot" not in value
    value["turn"]["messages"][0]["blocks"][0]["raw_arguments"] = "changed"
    assert example["turn"][0]["messages"][0]["blocks"][0]["raw_arguments"] != "changed"


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(schema_version=True),
    lambda p: p.update(kind="unvalidated_result"),
    lambda p: p.update(extra=1),
    lambda p: p["run"].update(status="failed", result_turn_id=None),
    lambda p: p["run"].update(result_turn_id=uid(100)),
    lambda p: p["turn"].update(run_id=uid(100)),
    lambda p: p["turn"].update(input_id=uid(100)),
    lambda p: p["turn"].update(snapshot_id=uid(100)),
    lambda p: p["group"].update(workflow_session_id=uid(100)),
    lambda p: p["group"].update(node_binding_id=uid(100)),
    lambda p: p["group"].update(logical_input_id=uid(100)),
    lambda p: p["group"].update(frozen_snapshot_id=uid(100)),
    lambda p: p["group"].update(parent_turn_id=uid(100)),
    lambda p: p["group"].update(candidate_refs=[]),
    lambda p: p["output"].update(workflow_session_id=uid(100)),
    lambda p: p["output"].update(chain_run_id=uid(100)),
    lambda p: p["output"].update(port_id="other"),
    lambda p: p["output"].update(payload={"text": "another answer"}),
    lambda p: p["output"]["source"].update(run_id=uid(100)),
    lambda p: p["turn"]["final"].update(message_id=uid(100)),
    lambda p: p["turn"]["messages"].pop(),
])
def test_package_rejects_unknown_versions_failed_results_and_cross_owner_links(example, mutation):
    value = package(example)
    mutation(value)
    with pytest.raises(ContractValidationError):
        validate_result_package(value)


@pytest.mark.parametrize("binding", ALLOWED)
def test_all_routes_are_explicit_fixed_and_stable(binding):
    value = make_result_port_routes(binding, allowed_bindings=ALLOWED)
    assert value == validate_result_port_routes(value, allowed_bindings=ALLOWED)
    assert value == make_result_port_routes(binding, allowed_bindings=ALLOWED)
    assert value["archive_route"]["route_id"].endswith(binding)
    assert value["final_route"]["route_id"] != value["context_route"]["route_id"]


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("archive_route"),
    lambda r: r.pop("final_route"),
    lambda r: r.pop("context_route"),
    lambda r: r.update(schema_version=True),
    lambda r: r.update(schema_version=2),
    lambda r: r.update(kind="dag_routes"),
    lambda r: r.update(extra="unsupported"),
    lambda r: r.update(node_binding_id=uid(100)),
    lambda r: r["archive_route"].update(kind="external_archive"),
    lambda r: r["final_route"].update(route_id="a live mutable route"),
    lambda r: r["context_route"].update(extra="unsupported"),
])
def test_missing_or_incompatible_routes_are_not_defaulted(mutation):
    value = make_result_port_routes(uid(3), allowed_bindings=ALLOWED)
    mutation(value)
    with pytest.raises(ContractValidationError):
        validate_result_port_routes(value, allowed_bindings=ALLOWED)


@pytest.mark.parametrize("routes", [None, {}, [], {"archive_route": "alone"}])
def test_submission_rejects_missing_route_before_any_archive(example, routes):
    with pytest.raises(ContractValidationError):
        make_archive_submission(package(example), routes, allowed_bindings=ALLOWED)


def test_submission_cannot_take_other_agents_routes(example):
    with pytest.raises(ContractValidationError, match="another Agent"):
        make_archive_submission(
            package(example), make_result_port_routes(uid(99), allowed_bindings=ALLOWED),
            allowed_bindings=ALLOWED,
        )


@pytest.mark.parametrize("allowed", [None, [], (), (uid(3),), (uid(3), uid(3)), (uid(3), "bad")])
def test_fixed_route_policy_is_explicit_and_not_a_generic_graph(allowed):
    with pytest.raises(ContractValidationError):
        make_result_port_routes(uid(3), allowed_bindings=allowed)


def test_submission_and_receipt_are_detached_and_link_the_same_archive_identity(example):
    value = submission(example)
    acknowledged = receipt(value)
    assert value == validate_archive_submission(value, allowed_bindings=ALLOWED)
    assert acknowledged == validate_archive_receipt(acknowledged, value, allowed_bindings=ALLOWED)
    assert value["archive_key"] == "archive:" + example["run_record"][0]["run_id"]
    assert acknowledged["identity"] == value["identity"]
    assert acknowledged["submission_digest"] == content_digest(value)
    acknowledged["identity"]["run_id"] = uid(100)
    assert value["identity"]["run_id"] == uid(7)


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(schema_version=2),
    lambda s: s.update(archive_key="different"),
    lambda s: s.update(package_digest=content_digest("different")),
    lambda s: s["identity"].update(output_id=uid(100)),
    lambda s: s.update(extra=1),
])
def test_submission_cannot_change_its_package_or_identity_correspondence(example, mutation):
    value = submission(example)
    mutation(value)
    with pytest.raises(ContractValidationError):
        validate_archive_submission(value, allowed_bindings=ALLOWED)


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop(),
    lambda r: r.append(copy.deepcopy(r[0])),
    lambda r: r.__setitem__(1, copy.deepcopy(r[0])),
    lambda r: r[0].update(turn_id=uid(100)),
    lambda r: r[1].update(candidate_group_id=uid(100)),
    lambda r: r[2].update(status="final_ready", result_turn_id=None),
])
def test_receipt_requires_the_exact_three_saved_success_records(example, mutation):
    value = submission(example)
    records = archived_records(value)
    mutation(records)
    with pytest.raises(ContractValidationError):
        make_archive_receipt(value, records, allowed_bindings=ALLOWED)


def test_archive_receipt_order_does_not_change_correspondence(example):
    value = submission(example)
    assert make_archive_receipt(value, list(reversed(archived_records(value))), allowed_bindings=ALLOWED) == receipt(value)


def test_two_ports_share_one_source_and_delta_does_not_duplicate_final_or_contain_s0(example):
    value = submission(example)
    acknowledged = receipt(value)
    released = release_result_ports(value, acknowledged, allowed_bindings=ALLOWED)
    assert released == validate_result_port_release(released, value, acknowledged, allowed_bindings=ALLOWED)
    final, delta = released["ports"]["final"], released["ports"]["context_delta"]
    assert final["source"] == delta["source"] == {"kind": "node_result", **value["identity"]}
    assert final["value"] == value["package"]["turn"]["final"]["value"]
    assert "messages" not in final and "s0" not in final and "tool_definitions" not in final
    assert delta["messages"] == value["package"]["turn"]["messages"]
    assert delta["message_ids"] == [m["message_id"] for m in delta["messages"]]
    assert sum(m["message_id"] == delta["final_message_id"] for m in delta["messages"]) == 1
    assert all(m["source"]["kind"] != "human" for m in delta["messages"])
    assert "s0" not in delta and "snapshot" not in delta
    assert final["delivery_id"] == "result-final:" + uid(7)
    assert delta["delivery_id"] == "result-context:" + uid(7)
    assert release_result_ports(value, acknowledged, allowed_bindings=ALLOWED) == released
    delta["messages"][0]["blocks"][0]["raw_arguments"] = "changed copy"
    assert value["package"]["turn"]["messages"] == example["turn"][0]["messages"]


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(extra=1),
    lambda r: r.update(schema_version=True),
    lambda r: r["ports"].update(third={}),
    lambda r: r["ports"]["final"].update(messages=[]),
    lambda r: r["ports"]["final"].update(value={"text": "fabricated"}),
    lambda r: r["ports"]["context_delta"]["messages"].append(copy.deepcopy(r["ports"]["context_delta"]["messages"][-1])),
    lambda r: r["ports"]["context_delta"].update(s0=[]),
    lambda r: r["ports"]["context_delta"]["message_ids"].reverse(),
    lambda r: r["ports"]["final"]["source"].update(run_id=uid(100)),
    lambda r: r["ports"]["final"].update(delivery_id="new random delivery"),
])
def test_release_rejects_leaks_duplicate_final_and_changed_delivery_identity(example, mutation):
    value = submission(example)
    acknowledged = receipt(value)
    released = release_result_ports(value, acknowledged, allowed_bindings=ALLOWED)
    mutation(released)
    with pytest.raises(ContractValidationError):
        validate_result_port_release(released, value, acknowledged, allowed_bindings=ALLOWED)


def test_submission_is_atomic_with_final_ready_and_replay_skips_both_callbacks(database, example):
    value = submission(example)
    with closing(SqliteStore(database)) as store:
        start_running(store, example)
        saved = save_accepted(store, example, value)
        ports = ResultPortStore(store, allowed_bindings=ALLOWED)
        assert ports.get_submission(uid(7)) == value
        assert ports.get_release(uid(7)) is None
        assert ports.read_port(uid(7), "final") is None

        def unexpected(*_args):
            raise AssertionError("Accepted replay must not rerun its binder")

        assert save_accepted(store, example, value, after_put=unexpected) == saved
        assert store.get_record("run_record", {"run_id": uid(7)})["status"] == "final_ready"
    with closing(SqliteStore(database)) as reopened:
        ports = ResultPortStore(reopened, allowed_bindings=ALLOWED)
        assert ports.get_submission(uid(7)) == value
        assert ports.get_release(uid(7)) is None


def test_failed_acceptance_rolls_back_package_and_run_transition(database, example):
    value = submission(example)
    with closing(SqliteStore(database)) as store:
        start_running(store, example)
        before = store.read_bundle()

        def failure(_saved):
            raise RuntimeError("injected after accepted mapping")

        with pytest.raises(RuntimeError, match="accepted mapping"):
            save_accepted(store, example, value, after_put=failure)
        ports = ResultPortStore(store, allowed_bindings=ALLOWED)
        assert ports.get_submission(uid(7)) is None
        assert store.read_bundle() == before
        save_accepted(store, example, value)
        assert ports.get_submission(uid(7)) == value


def test_ports_require_actual_archive_receipt_and_actual_saved_business_output(database, example):
    value = submission(example)
    with closing(SqliteStore(database)) as store:
        start_running(store, example)
        save_accepted(store, example, value)
        ports = ResultPortStore(store, allowed_bindings=ALLOWED)
        store._connection.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(ContractValidationError, match="saved archive receipt"):
                ports.release_in_transaction(value, receipt(value))
            assert store._connection.in_transaction
            assert ports.get_release(uid(7)) is None
        finally:
            store._connection.execute("ROLLBACK")
        acknowledged = make_archive_receipt(value, archive(store, value), allowed_bindings=ALLOWED)
        assert store.get_record("run_record", {"run_id": uid(7)})["status"] == "succeeded"
        store._connection.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(ContractValidationError):
                ports.release_in_transaction(value, acknowledged)
            assert store._connection.in_transaction
            assert ports.get_release(uid(7)) is None
        finally:
            store._connection.execute("ROLLBACK")
        assert not store.list_records("workflow_output")


def test_release_mapping_and_output_roll_back_together_then_retry_same_package(database, example):
    value = submission(example)
    with closing(SqliteStore(database)) as store:
        start_running(store, example)
        save_accepted(store, example, value)
        acknowledged = make_archive_receipt(value, archive(store, value), allowed_bindings=ALLOWED)
        before = store.read_bundle()
        facts = store.read_execution_facts(uid(7))
        ports = ResultPortStore(store, allowed_bindings=ALLOWED)

        def failure(_saved, _released):
            raise RuntimeError("injected after release mapping")

        with pytest.raises(RuntimeError, match="release mapping"):
            save_output_and_release(store, example, value, acknowledged, after_release=failure)
        assert store.read_bundle() == before
        assert not store.list_records("workflow_output")
        assert ports.get_release(uid(7)) is None
        assert store.get_record("run_record", {"run_id": uid(7)})["status"] == "succeeded"
        assert store.read_receipt("archive", value["archive_key"]) is not None
        saved = save_output_and_release(store, example, value, acknowledged)
        assert ports.get_release(uid(7)) == release_result_ports(value, acknowledged, allowed_bindings=ALLOWED)
        assert store.read_execution_facts(uid(7)) == facts
        assert len(store.list_records("turn")) == 1
        assert len(store.list_records("candidate_group")) == 1

        def unexpected(*_args):
            raise AssertionError("Released replay must not rerun its binder")

        assert save_output_and_release(store, example, value, acknowledged, after_release=unexpected) == saved
        assert ports.read_port(uid(7), "final")["value"] == example["turn"][0]["final"]["value"]
        assert ports.read_port(uid(7), "context_delta")["messages"] == example["turn"][0]["messages"]
    with closing(SqliteStore(database)) as reopened:
        assert ResultPortStore(reopened, allowed_bindings=ALLOWED).get_release(uid(7)) is not None


def test_duplicate_submission_same_run_different_output_identity_is_rejected(database, example):
    value = submission(example)
    changed_package = package(example)
    changed_package["output"]["output_id"] = uid(100)
    changed = make_archive_submission(changed_package, value["routes"], allowed_bindings=ALLOWED)
    with closing(SqliteStore(database)) as store:
        start_running(store, example)
        save_accepted(store, example, value)
        ports = ResultPortStore(store, allowed_bindings=ALLOWED)
        store._connection.execute("BEGIN IMMEDIATE")
        try:
            assert ports.put_submission_in_transaction(value) == value
            with pytest.raises(ContractValidationError, match="different content"):
                ports.put_submission_in_transaction(changed)
            assert store._connection.in_transaction
            assert ports.get_submission(uid(7)) == value
        finally:
            store._connection.execute("ROLLBACK")


def test_port_writes_do_not_start_or_commit_caller_transactions(database, example):
    value = submission(example)
    with closing(SqliteStore(database)) as store:
        ports = ResultPortStore(store, allowed_bindings=ALLOWED)
        with pytest.raises(ContractValidationError, match="caller"):
            ports.put_submission_in_transaction(value)
        with pytest.raises(ContractValidationError, match="caller"):
            ports.release_in_transaction(value, receipt(value))
        assert not store._connection.in_transaction
        assert ports.get_submission(uid(7)) is None
        with pytest.raises(ContractValidationError, match="Unknown formal"):
            ports.read_port(uid(7), "preview")


def test_stored_port_payload_corruption_is_loud_not_a_missing_port(database, example):
    value = submission(example)
    with closing(SqliteStore(database)) as store:
        start_running(store, example)
        save_accepted(store, example, value)
        ports = ResultPortStore(store, allowed_bindings=ALLOWED)
        changed = copy.deepcopy(value)
        changed["identity"]["run_id"] = uid(100)
        store._connection.execute(
            "UPDATE result_port_submissions SET payload = ? WHERE run_id = ?",
            (canonical_bytes(changed).decode("utf-8"), uid(7)),
        )
        with pytest.raises(ContractValidationError, match="digest"):
            ports.get_submission(uid(7))
