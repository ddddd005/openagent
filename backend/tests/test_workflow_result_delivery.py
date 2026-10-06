"""Formal result ports share the workflow owner's accepted durable result."""

import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.runtime import KernelContractError, RunFailed
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, OfflineAdapter, WorkflowService
from phase1_agent.workflow_result_ports import ResultPortStore, make_result_port_routes


ALLOWED = (A_BINDING, B_BINDING)


@pytest.fixture
def database():
    directory = Path(__file__).resolve().parents[1] / "tmp"
    directory.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="result-delivery-", dir=directory) as folder:
        yield Path(folder) / "workflow.sqlite"


class TrackedAdapter(OfflineAdapter):
    def __init__(self, stage, requests):
        super().__init__(stage)
        self._requests = requests

    def generate(self, messages, tools):
        self._requests.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
        return super().generate(messages, tools)


class ExecutionLedger:
    def __init__(self):
        self.factories = []
        self.requests = []
        self.kernels = []

    def factory(self, stage):
        self.factories.append(stage)
        return TrackedAdapter(stage, self.requests)

    def track_kernels(self, service, monkeypatch):
        kernels = {
            id(resolved[1].implementation): resolved[1].implementation
            for resolved in service._resolved.values()
        }
        for kernel in kernels.values():
            original = kernel.run

            def tracked(snapshot, *args, _original=original, **kwargs):
                self.kernels.append(snapshot["snapshot_id"])
                return _original(snapshot, *args, **kwargs)

            monkeypatch.setattr(kernel, "run", tracked)


def bundle(database):
    with closing(SqliteStore(database)) as store:
        return store.read_bundle()


def state_for_run(database, run_id):
    with closing(SqliteStore(database)) as store:
        ports = ResultPortStore(store, allowed_bindings=ALLOWED)
        run = store.get_record("run_record", {"run_id": run_id})
        return {
            "run": run,
            "snapshot": store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]}),
            "submission": ports.get_submission(run_id),
            "release": ports.get_release(run_id),
            "facts": store.read_execution_facts(run_id),
            "archive": store.read_receipt("archive", "archive:" + run_id),
            "output": store.read_receipt("workflow.update", "output:" + run_id),
        }


def node_state(database, sid, binding):
    with closing(SqliteStore(database)) as store:
        return store.get_record("node_session", {
            "workflow_session_id": sid, "node_binding_id": binding,
        })


def submit(service, sid, key="first"):
    result = service.submit(sid, "A durable result with tools.", key)
    service.wait_for_idle(sid)
    return result


def failed_package(service, sid, stage):
    view = service.get_session(sid)
    assert view["available_actions"] == ["retry_archive"]
    assert view["error"]["code"] == "STORAGE_OR_COORDINATION_FAILED"
    assert [message["role"] for message in view["messages"]] == ["user"]
    binding = A_BINDING if stage == "A" else B_BINDING
    run_id = next(node["run_id"] for node in view["nodes"] if node["node_binding_id"] == binding)
    package = copy.deepcopy(service._active_results[run_id])
    assert package["stage"] == stage and package["session_id"] == sid
    assert package["run"]["run_id"] == run_id
    return view, run_id, package


def assert_ports_withheld(database, run_id):
    with closing(SqliteStore(database)) as store:
        ports = ResultPortStore(store, allowed_bindings=ALLOWED)
        assert ports.get_release(run_id) is None
        assert ports.read_port(run_id, "final") is None
        assert ports.read_port(run_id, "context_delta") is None


def assert_closed_same_result(database, sid, package, before, ledger, calls_before):
    run_id = package["run"]["run_id"]
    after = state_for_run(database, run_id)
    assert after["run"] == package["run"]
    assert after["snapshot"] == before["snapshot"]
    assert after["facts"] == before["facts"]
    assert after["submission"]["package"] == {
        "schema_version": 1, "kind": "node_result_package",
        **{name: package[name] for name in ("turn", "group", "run", "output")},
    }
    released = after["release"]
    assert released is not None
    assert released["identity"] == after["submission"]["identity"]
    assert released["ports"]["final"]["value"] == package["output"]["payload"]
    assert released["ports"]["context_delta"]["messages"] == package["turn"]["messages"]
    assert after["archive"] is not None and after["output"] is not None
    assert ledger.kernels.count(before["snapshot"]["snapshot_id"]) == 1
    assert [request for request in ledger.requests if request[0] == package["stage"]] == calls_before
    state = node_state(database, sid, package["run"]["node_binding_id"])
    assert state["data_version"] == 2
    assert state["private_data"]["payload"]["head_turn_id"] == package["turn"]["turn_id"]
    saved = bundle(database)
    assert sum(turn["turn_id"] == package["turn"]["turn_id"] for turn in saved["turn"]) == 1
    assert sum(group["candidate_group_id"] == package["group"]["candidate_group_id"]
               for group in saved["candidate_group"]) == 1
    assert sum(output["output_id"] == package["output"]["output_id"]
               for output in saved["workflow_output"]) == 1


def retry_and_assert(service, sid, run_id, view, stage, ledger):
    factories_before = list(ledger.factories)
    retry = service.retry_archive(
        sid, run_id, idempotency_key="retry-same-package",
        expected_session_revision=view["revision"],
    )
    assert retry["status"] == "succeeded" and retry["run_id"] == run_id
    assert service.get_session(sid)["error"] is None
    assert ledger.factories == factories_before + (["B"] if stage == "A" else [])
    assert service.retry_archive(
        sid, run_id, idempotency_key="retry-same-package",
        expected_session_revision=view["revision"],
    ) == retry


def fail_first_a_archive(monkeypatch):
    original = SqliteStore.archive_success

    def fail(store, records, key):
        run = next(value for kind, value in records if kind == "run_record")
        if run["node_binding_id"] == A_BINDING:
            raise RuntimeError("injected A archive failure")
        return original(store, records, key)

    monkeypatch.setattr(SqliteStore, "archive_success", fail)
    return original


def accepted_retry(sid, run_id, chain_id):
    return {
        "workflow_session_id": sid, "run_id": run_id, "chain_run_id": chain_id,
        "status": "accepted", "completion": "unconfirmed",
    }


def retry_request(service, sid, run_id, key):
    from test_workflow_operations import envelope

    return envelope(service, sid, "retry_archive", run_id, key=key)


def resume_run(service, sid, run_id, key):
    view = service.get_session(sid)
    node = next(row for row in view["nodes"] if row["run_id"] == run_id)
    receipt = service.resume(
        sid, run_id, idempotency_key=key,
        expected_session_revision=view["revision"], expected_run_revision=node["revision"],
    )
    service.wait_for_idle(sid)
    assert service.get_session(sid)["error"] is None
    assert receipt["run_id"] == run_id


def test_a_b_result_ports_keep_only_current_delta_and_survive_service_reopen(database, monkeypatch):
    ledger, releases = ExecutionLedger(), {}
    with closing(WorkflowService(database, model_factory=ledger.factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        sid = service.create_session()["workflow_session_id"]
        for index in range(2):
            submit(service, sid, str(index))
            assert service.get_session(sid)["error"] is None
        saved = bundle(database)
        assert len(saved["turn"]) == 4
        assert len(saved["candidate_group"]) == 4
        assert len(saved["visible_message"]) == 4
        assert ledger.factories == ["A", "B", "A", "B"]
        assert len(ledger.kernels) == 4
        with closing(SqliteStore(database)) as store:
            ports = ResultPortStore(store, allowed_bindings=ALLOWED)
            for run in saved["run_record"]:
                accepted = ports.get_submission(run["run_id"])
                released = ports.get_release(run["run_id"])
                assert accepted is not None and released is not None
                turn = accepted["package"]["turn"]
                snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
                final, delta = released["ports"]["final"], released["ports"]["context_delta"]
                assert final["source"] == delta["source"] == {
                    "kind": "node_result", **released["identity"],
                }
                assert final["value"] == turn["final"]["value"] == accepted["package"]["output"]["payload"]
                assert final["delivery_id"] == "result-final:" + run["run_id"]
                assert delta["delivery_id"] == "result-context:" + run["run_id"]
                assert "messages" not in final and "s0" not in final
                assert "tool_definitions" not in final
                assert delta["messages"] == turn["messages"]
                assert delta["parent_turn_id"] == turn["parent_turn_id"]
                assert delta["message_ids"] == [message["message_id"] for message in turn["messages"]]
                assert set(delta["message_ids"]).isdisjoint(
                    message["message_id"] for message in snapshot["s0"]
                )
                assert "s0" not in delta and "snapshot" not in delta
                assert all(message["source"]["kind"] not in ("human", "upstream_node", "prompt")
                           for message in delta["messages"])
                assert sum(message["message_id"] == delta["final_message_id"]
                           for message in delta["messages"]) == 1
                assert len(delta["messages"]) == 4
                releases[run["run_id"]] = released
                if turn["parent_turn_id"] is not None:
                    assert len(snapshot["s0"]) > len(delta["messages"])
    calls_before = copy.deepcopy(ledger.requests)
    with closing(WorkflowService(database, model_factory=ledger.factory)) as reopened:
        assert reopened.get_session(sid)["error"] is None
        assert len(reopened.get_session(sid)["messages"]) == 4
        assert ledger.factories == ["A", "B", "A", "B"]
        assert ledger.requests == calls_before
        with closing(SqliteStore(database)) as store:
            ports = ResultPortStore(store, allowed_bindings=ALLOWED)
            assert {run_id: ports.get_release(run_id) for run_id in releases} == releases


@pytest.mark.parametrize("stage", ["A", "B"])
def test_archive_failure_withholds_both_ports_then_retries_only_same_accepted_result(
    database, monkeypatch, stage,
):
    ledger = ExecutionLedger()
    binding = A_BINDING if stage == "A" else B_BINDING
    original = SqliteStore.archive_success

    def fail_archive(store, records, key):
        run = next(value for kind, value in records if kind == "run_record")
        if run["node_binding_id"] == binding:
            raise RuntimeError("injected archive failure")
        return original(store, records, key)

    monkeypatch.setattr(SqliteStore, "archive_success", fail_archive)
    with closing(WorkflowService(database, model_factory=ledger.factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        sid = service.create_session()["workflow_session_id"]
        submit(service, sid)
        view, run_id, package = failed_package(service, sid, stage)
        before = state_for_run(database, run_id)
        assert before["run"]["status"] == "final_ready"
        assert before["submission"] is not None
        assert before["archive"] is None and before["output"] is None
        assert node_state(database, sid, binding)["data_version"] == 1
        assert_ports_withheld(database, run_id)
        calls_before = [request for request in ledger.requests if request[0] == stage]
        monkeypatch.setattr(SqliteStore, "archive_success", original)
        retry_and_assert(service, sid, run_id, view, stage, ledger)
        assert_closed_same_result(database, sid, package, before, ledger, calls_before)


@pytest.mark.parametrize("stage", ["A", "B"])
def test_archived_result_survives_output_release_rollback_then_reuses_original_delivery(
    database, monkeypatch, stage,
):
    ledger = ExecutionLedger()
    binding = A_BINDING if stage == "A" else B_BINDING
    original = ResultPortStore.release_in_transaction

    def fail_release(ports, submission, receipt):
        released = original(ports, submission, receipt)
        if submission["identity"]["node_binding_id"] == binding:
            raise RuntimeError("injected after result release")
        return released

    monkeypatch.setattr(ResultPortStore, "release_in_transaction", fail_release)
    with closing(WorkflowService(database, model_factory=ledger.factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        sid = service.create_session()["workflow_session_id"]
        submit(service, sid)
        view, run_id, package = failed_package(service, sid, stage)
        before = state_for_run(database, run_id)
        assert before["run"]["status"] == "succeeded"
        assert before["submission"] is not None and before["archive"] is not None
        assert before["output"] is None
        assert node_state(database, sid, binding)["data_version"] == 1
        assert_ports_withheld(database, run_id)
        assert not any(output["output_id"] == package["output"]["output_id"]
                       for output in bundle(database).get("workflow_output", []))
        calls_before = [request for request in ledger.requests if request[0] == stage]
        monkeypatch.setattr(ResultPortStore, "release_in_transaction", original)
        retry_and_assert(service, sid, run_id, view, stage, ledger)
        assert_closed_same_result(database, sid, package, before, ledger, calls_before)
        after = state_for_run(database, run_id)
        assert after["archive"] == before["archive"]
        assert after["submission"] == before["submission"]


@pytest.mark.parametrize("stage", ["A", "B"])
def test_final_ready_submission_rollback_preserves_in_process_package_for_storage_only_retry(
    database, monkeypatch, stage,
):
    ledger = ExecutionLedger()
    binding = A_BINDING if stage == "A" else B_BINDING
    original = ResultPortStore.put_submission_in_transaction

    def fail_submission(ports, submission):
        accepted = original(ports, submission)
        if submission["identity"]["node_binding_id"] == binding:
            raise RuntimeError("injected after accepted submission")
        return accepted

    monkeypatch.setattr(ResultPortStore, "put_submission_in_transaction", fail_submission)
    with closing(WorkflowService(database, model_factory=ledger.factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        sid = service.create_session()["workflow_session_id"]
        submit(service, sid)
        view, run_id, package = failed_package(service, sid, stage)
        before = state_for_run(database, run_id)
        assert before["run"]["status"] == "running"
        assert before["submission"] is None
        assert before["archive"] is None and before["output"] is None
        assert node_state(database, sid, binding)["data_version"] == 1
        assert_ports_withheld(database, run_id)
        assert not any(turn["turn_id"] == package["turn"]["turn_id"]
                       for turn in bundle(database).get("turn", []))
        calls_before = [request for request in ledger.requests if request[0] == stage]
        monkeypatch.setattr(ResultPortStore, "put_submission_in_transaction", original)
        retry_and_assert(service, sid, run_id, view, stage, ledger)
        assert_closed_same_result(database, sid, package, before, ledger, calls_before)


def test_repeated_accepted_delivery_does_not_advance_node_projection_or_history(
    database, monkeypatch,
):
    ledger, packages = ExecutionLedger(), {}
    original = WorkflowService._archive_accepted_node

    def remember(service, package):
        packages[package["stage"]] = copy.deepcopy(package)
        return original(service, package)

    monkeypatch.setattr(WorkflowService, "_archive_accepted_node", remember)
    with closing(WorkflowService(database, model_factory=ledger.factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        sid = service.create_session()["workflow_session_id"]
        submit(service, sid)
        assert service.get_session(sid)["error"] is None
        before = bundle(database)
        requests_before = copy.deepcopy(ledger.requests)
        kernels_before = list(ledger.kernels)
        facts_before = {
            package["run"]["run_id"]: state_for_run(database, package["run"]["run_id"])
            for package in packages.values()
        }
        assert set(packages) == {"A", "B"}
        for stage in ("A", "B"):
            for _index in range(2):
                assert original(service, copy.deepcopy(packages[stage])) == packages[stage]["output"]
        assert canonical_bytes(bundle(database)) == canonical_bytes(before)
        assert ledger.requests == requests_before and ledger.kernels == kernels_before
        assert ledger.factories == ["A", "B"]
        assert {
            run_id: state_for_run(database, run_id) for run_id in facts_before
        } == facts_before
        assert not service._active_results
        assert all(node_state(database, sid, binding)["data_version"] == 2 for binding in ALLOWED)


@pytest.mark.parametrize("bad_route", ["absent", "missing_archive", "wrong_route", "wrong_owner"])
def test_missing_or_bad_explicit_archive_routes_reject_preparation_without_dispatch(
    database, monkeypatch, bad_route,
):
    ledger, dispatches = ExecutionLedger(), []
    with closing(WorkflowService(database, model_factory=ledger.factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        sid = service.create_session()["workflow_session_id"]
        context, kernel, adapter, config = service._resolved["A"]
        config = copy.deepcopy(config)
        route = make_result_port_routes(A_BINDING, allowed_bindings=ALLOWED)
        if bad_route == "absent":
            route = None
        elif bad_route == "missing_archive":
            route.pop("archive_route")
        elif bad_route == "wrong_route":
            route["final_route"]["route_id"] = "unsupported-live-route"
        else:
            route = make_result_port_routes(B_BINDING, allowed_bindings=ALLOWED)
        config["payload"]["result_port_routes"] = route
        service._resolved["A"] = context, kernel, adapter, config
        original_dispatch = service._executor.submit

        def tracked_dispatch(*args, **kwargs):
            dispatches.append((args, kwargs))
            return original_dispatch(*args, **kwargs)

        monkeypatch.setattr(service._executor, "submit", tracked_dispatch)
        before = bundle(database)
        with pytest.raises(ContractValidationError):
            service.submit(sid, "Do not dispatch an invalid route.", "invalid-route")
        assert dispatches == []
        assert ledger.factories == [] and ledger.requests == [] and ledger.kernels == []
        assert bundle(database) == before
        assert service.get_session(sid)["messages"] == []


def test_a_archive_retry_classifies_new_b_model_failure_then_resumes_same_b_run(
    database, monkeypatch,
):
    ledger, broken = ExecutionLedger(), True
    original_archive = fail_first_a_archive(monkeypatch)

    class TransientB(TrackedAdapter):
        def generate(self, messages, tools):
            if self.stage == "B" and broken:
                self._requests.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
                raise httpx.ReadTimeout("PRIVATE B MODEL ERROR")
            return super().generate(messages, tools)

    def factory(stage):
        ledger.factories.append(stage)
        return TransientB(stage, ledger.requests)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        service._resolved["B"][1].implementation._backoff = lambda _retry: None
        sid = service.create_session()["workflow_session_id"]
        submitted = submit(service, sid)
        initial, a_id, a_package = failed_package(service, sid, "A")
        before_a = state_for_run(database, a_id)
        a_calls = [request for request in ledger.requests if request[0] == "A"]
        monkeypatch.setattr(SqliteStore, "archive_success", original_archive)
        request = retry_request(service, sid, a_id, "retry-a-then-b")
        accepted = service.dispatch_operation(request)
        assert accepted["status"] == "accepted"
        assert accepted["result"] == accepted_retry(sid, a_id, submitted["chain_run_id"])
        failed = service.get_session(sid)
        b_node = next(row for row in failed["nodes"] if row["node_binding_id"] == B_BINDING)
        b_id = b_node["run_id"]
        assert b_node["status"] == "failed"
        assert failed["error"]["code"] == "model_retry_exhausted"
        assert "resume" in failed["available_actions"]
        assert isinstance(service._execution_failures[sid], RunFailed)
        assert not isinstance(service._execution_failures[sid], KernelContractError)
        assert b_id in service._run_checkpoints
        before_b = state_for_run(database, b_id)
        assert before_b["facts"][-1]["payload"]["category"] == "model"
        assert_ports_withheld(database, b_id)
        calls_before = copy.deepcopy(ledger.requests)
        assert service.dispatch_operation(request) == accepted
        assert service.retry_archive(
            sid, a_id, idempotency_key=request["idempotency_key"],
            expected_session_revision=initial["revision"],
        ) == accepted["result"]
        assert ledger.requests == calls_before and ledger.factories == ["A", "B"]
        assert_closed_same_result(database, sid, a_package, before_a, ledger, a_calls)
        archived_a = state_for_run(database, a_id)
        assert "PRIVATE" not in str(failed) and "PRIVATE" not in str(accepted)
        broken = False
        resume_run(service, sid, b_id, "resume-new-b")
        completed = service.get_session(sid)
        assert next(row["run_id"] for row in completed["nodes"]
                    if row["node_binding_id"] == B_BINDING) == b_id
        assert ledger.kernels.count(before_a["snapshot"]["snapshot_id"]) == 1
        assert ledger.kernels.count(before_b["snapshot"]["snapshot_id"]) == 2
        assert state_for_run(database, a_id) == archived_a
        assert state_for_run(database, b_id)["snapshot"] == before_b["snapshot"]
        assert state_for_run(database, b_id)["facts"][:len(before_b["facts"])] == before_b["facts"]
        assert service.dispatch_operation(request) == accepted


@pytest.mark.parametrize("fault", ["adapter", "execution"])
def test_a_archive_retry_classifies_new_b_program_failure_without_model_recovery(
    database, monkeypatch, fault,
):
    ledger = ExecutionLedger()
    original_archive = fail_first_a_archive(monkeypatch)

    class BrokenB(TrackedAdapter):
        def generate(self, messages, tools):
            if self.stage == "B" and fault == "adapter":
                self._requests.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
                raise RuntimeError("PRIVATE B ADAPTER PROGRAM ERROR")
            return super().generate(messages, tools)

    def factory(stage):
        ledger.factories.append(stage)
        return BrokenB(stage, ledger.requests)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        sid = service.create_session()["workflow_session_id"]
        submitted = submit(service, sid)
        initial, a_id, a_package = failed_package(service, sid, "A")
        before_a = state_for_run(database, a_id)
        a_calls = [request for request in ledger.requests if request[0] == "A"]
        monkeypatch.setattr(SqliteStore, "archive_success", original_archive)
        if fault == "execution":
            original_execute = service._execute_node

            def broken_execute(owner, chain_id, stage, run_id, **kwargs):
                if stage == "B":
                    raise RuntimeError("PRIVATE B COORDINATOR PROGRAM ERROR")
                return original_execute(owner, chain_id, stage, run_id, **kwargs)

            monkeypatch.setattr(service, "_execute_node", broken_execute)
        accepted = service.retry_archive(
            sid, a_id, idempotency_key="retry-a-program",
            expected_session_revision=initial["revision"],
        )
        assert accepted == accepted_retry(sid, a_id, submitted["chain_run_id"])
        failed = service.get_session(sid)
        b_node = next(row for row in failed["nodes"] if row["node_binding_id"] == B_BINDING)
        b_id = b_node["run_id"]
        code = "adapter_contract_error" if fault == "adapter" else "PROGRAM_OR_STORAGE_FAILED"
        assert failed["error"]["code"] == (
            "adapter_contract_error" if fault == "adapter" else "STORAGE_OR_COORDINATION_FAILED"
        )
        assert failed["available_actions"] == ["close_execution"]
        assert b_id not in service._run_checkpoints
        assert state_for_run(database, b_id)["facts"][-1]["payload"]["category"] == "contract"
        assert state_for_run(database, b_id)["facts"][-1]["payload"]["code"] == code
        calls_before, kernels_before = copy.deepcopy(ledger.requests), list(ledger.kernels)
        assert service.retry_archive(
            sid, a_id, idempotency_key="retry-a-program",
            expected_session_revision=initial["revision"],
        ) == accepted
        assert ledger.requests == calls_before and ledger.kernels == kernels_before
        assert_closed_same_result(database, sid, a_package, before_a, ledger, a_calls)
        assert "PRIVATE" not in str(failed) and "PRIVATE" not in str(accepted)
        service.close_execution(
            sid, submitted["chain_run_id"], idempotency_key="close-b-program",
            expected_session_revision=failed["revision"],
            expected_ref_revision=failed["ref_revision"],
            expected_head_commit_id=failed["head_commit_id"],
        )
        closeout = bundle(database)["execution_closeout"][0]
        assert closeout["diagnostic"] == {"code": code, "category": "contract"}
        assert not service.get_session(sid)["can_continue_workflow"]
        assert state_for_run(database, a_id)["run"] == a_package["run"]


def test_a_archive_retry_honors_prepared_b_pause_and_clears_old_archive_error(
    database, monkeypatch,
):
    ledger = ExecutionLedger()
    original_archive = fail_first_a_archive(monkeypatch)
    with closing(WorkflowService(database, model_factory=ledger.factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        sid = service.create_session()["workflow_session_id"]
        submitted = submit(service, sid)
        initial, a_id, a_package = failed_package(service, sid, "A")
        before_a = state_for_run(database, a_id)
        a_calls = [request for request in ledger.requests if request[0] == "A"]
        monkeypatch.setattr(SqliteStore, "archive_success", original_archive)
        original_prepare = service._prepare_next_node

        def pause_prepared_b(owner, chain_id, output):
            original_prepare(owner, chain_id, output)
            prepared = service.get_session(owner)
            b_node = next(row for row in prepared["nodes"] if row["node_binding_id"] == B_BINDING)
            assert b_node["status"] == "prepared"
            service.interrupt(
                owner, b_node["run_id"], idempotency_key="pause-new-b",
                expected_session_revision=prepared["revision"],
                expected_run_revision=b_node["revision"],
            )

        monkeypatch.setattr(service, "_prepare_next_node", pause_prepared_b)
        accepted = service.retry_archive(
            sid, a_id, idempotency_key="retry-a-paused",
            expected_session_revision=initial["revision"],
        )
        assert accepted == accepted_retry(sid, a_id, submitted["chain_run_id"])
        paused = service.get_session(sid)
        b_node = next(row for row in paused["nodes"] if row["node_binding_id"] == B_BINDING)
        b_id = b_node["run_id"]
        assert b_node["status"] == "paused" and paused["error"] is None
        assert sid not in service._execution_failures
        assert service._run_checkpoints[b_id] is None
        assert "resume" in paused["available_actions"]
        assert ledger.factories == ["A"]
        assert state_for_run(database, b_id)["facts"] == []
        assert_ports_withheld(database, b_id)
        assert service.retry_archive(
            sid, a_id, idempotency_key="retry-a-paused",
            expected_session_revision=initial["revision"],
        ) == accepted
        assert_closed_same_result(database, sid, a_package, before_a, ledger, a_calls)
        monkeypatch.setattr(service, "_prepare_next_node", original_prepare)
        resume_run(service, sid, b_id, "resume-paused-b")
        assert ledger.factories == ["A", "B"]
        assert ledger.kernels.count(before_a["snapshot"]["snapshot_id"]) == 1
        assert state_for_run(database, b_id)["run"]["status"] == "succeeded"


@pytest.mark.parametrize("api", ["legacy", "dispatch"])
def test_a_archive_retry_records_new_b_host_signal_once_and_replays_acceptance(
    database, monkeypatch, api,
):
    ledger = ExecutionLedger()
    original_archive = fail_first_a_archive(monkeypatch)

    class HostStoppedB(TrackedAdapter):
        def generate(self, messages, tools):
            if self.stage == "B":
                self._requests.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
                raise KeyboardInterrupt("PRIVATE B HOST SIGNAL")
            return super().generate(messages, tools)

    def factory(stage):
        ledger.factories.append(stage)
        return HostStoppedB(stage, ledger.requests)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        ledger.track_kernels(service, monkeypatch)
        sid = service.create_session()["workflow_session_id"]
        submitted = submit(service, sid)
        initial, a_id, a_package = failed_package(service, sid, "A")
        before_a = state_for_run(database, a_id)
        a_calls = [request for request in ledger.requests if request[0] == "A"]
        monkeypatch.setattr(SqliteStore, "archive_success", original_archive)
        request = retry_request(service, sid, a_id, "retry-a-host")

        def retry():
            if api == "dispatch":
                return service.dispatch_operation(request)
            return service.retry_archive(
                sid, a_id, idempotency_key=request["idempotency_key"],
                expected_session_revision=initial["revision"],
            )

        with pytest.raises(KeyboardInterrupt, match="B HOST SIGNAL"):
            retry()
        failed = service.get_session(sid)
        b_id = next(row["run_id"] for row in failed["nodes"] if row["node_binding_id"] == B_BINDING)
        assert failed["error"]["code"] == "HOST_INTERRUPTED"
        assert failed["available_actions"] == ["close_execution"]
        assert isinstance(service._execution_failures[sid], KeyboardInterrupt)
        before_b = state_for_run(database, b_id)
        assert before_b["facts"][-1]["payload"]["category"] == "interrupted"
        assert before_b["facts"][-1]["payload"]["code"] == "HOST_INTERRUPTED"
        assert_ports_withheld(database, b_id)
        calls_before, kernels_before = copy.deepcopy(ledger.requests), list(ledger.kernels)
        accepted = retry()
        result = accepted["result"] if api == "dispatch" else accepted
        assert result == accepted_retry(sid, a_id, submitted["chain_run_id"])
        assert retry() == accepted
        assert ledger.requests == calls_before and ledger.kernels == kernels_before
        assert ledger.factories == ["A", "B"]
        assert_closed_same_result(database, sid, a_package, before_a, ledger, a_calls)
        assert "PRIVATE" not in str(failed) and "PRIVATE" not in str(accepted)
        service.close_execution(
            sid, submitted["chain_run_id"], idempotency_key="close-b-host",
            expected_session_revision=failed["revision"],
            expected_ref_revision=failed["ref_revision"],
            expected_head_commit_id=failed["head_commit_id"],
        )
        assert bundle(database)["execution_closeout"][0]["diagnostic"] == {
            "code": "HOST_INTERRUPTED", "category": "interrupted",
        }
