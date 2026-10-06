"""I17 evidence-based new execution and its shared I18 closeout boundary."""

import asyncio
import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

import pytest
import httpx

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.runtime import KernelPauseRequested, SnapshotKernel
from phase1_agent.storage import SqliteStore
from phase1_agent.tools import final_answer_tool, register_callable
from phase1_agent.workflow import A_BINDING, B_BINDING, OfflineAdapter, WorkflowService

from test_workflow_events import REF, first_run_id, reporting_components


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="execution-recovery-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def cas(service, sid, key):
    view = service.get_session(sid)
    return {
        "idempotency_key": key, "expected_session_revision": view["revision"],
        "expected_ref_revision": view["ref_revision"],
        "expected_head_commit_id": view["head_commit_id"],
    }


def bundle(path):
    with closing(SqliteStore(path)) as store:
        return validate_bundle(store.read_bundle())


def test_interrupted_batch_is_quoted_as_evidence_not_replayed(database, monkeypatch):
    entries, requests = [], []
    broken = True

    def effect(text):
        entries.append(text)
        if text == "uncertain":
            raise asyncio.CancelledError("PRIVATE-HOST-DETAIL")
        return {"text": text, "saved": True}

    tool = register_callable("inspect_text", "An interruption fixture", {
        "type": "object", "properties": {"text": {"type": "string", "description": "Fixture input"}},
        "required": ["text"], "additionalProperties": False,
    }, effect)
    monkeypatch.setattr(WorkflowService, "_tools", staticmethod(lambda: (tool, final_answer_tool())))

    class InterruptedBatch:
        def generate(self, messages, tools):
            return ModelResponse("tool_calls", tool_calls=tuple(
                ModelToolCall(str(uuid4()), "inspect_text", dumps_pretty({"text": text}))
                for text in ("saved", "uncertain", "not-dispatched")
            ))

    class Fresh(OfflineAdapter):
        def generate(self, messages, tools):
            requests.append((self.stage, copy.deepcopy(messages)))
            return super().generate(messages, tools)

    def factory(stage):
        return InterruptedBatch() if broken and stage == "A" else Fresh(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        original = service.submit(sid, "Finish the unfinished work", "input")
        service.wait_for_idle(sid)
        assert entries == ["saved", "uncertain"]
        failed = service.get_session(sid)
        assert failed["available_actions"] == ["close_execution"]
        before = bundle(database)
        old_run = before["run_record"][0]
        with closing(SqliteStore(database)) as store:
            facts_before = store.read_execution_facts(old_run["run_id"])
        close_args = cas(service, sid, "close")
        closed = service.close_execution(sid, original["chain_run_id"], **close_args)
        assert service.close_execution(sid, original["chain_run_id"], **close_args) == closed
        view = service.get_session(sid)
        assert view["head_commit_id"] == failed["head_commit_id"]
        assert view["ref_revision"] == failed["ref_revision"]
        assert view["can_submit"] and view["can_continue_workflow"]
        assert requests == []
        evidence_bundle = bundle(database)
        assert evidence_bundle.get("turn", []) == before.get("turn", [])
        assert evidence_bundle["workflow_ref"] == before["workflow_ref"]
        old = next(row for row in evidence_bundle["run_record"] if row["run_id"] == old_run["run_id"])
        assert old["status"] == "closed" and old["superseded_by_run_id"] is None
        broken = False
        arguments = cas(service, sid, "continue")
        receipt = service.continue_workflow(sid, original["chain_run_id"], **arguments)
        assert service.continue_workflow(sid, original["chain_run_id"], **arguments) == receipt
        service.wait_for_idle(sid)
        final_view = service.get_session(sid)
        assert final_view["error"] is None
        assert [row["role"] for row in final_view["messages"]] == ["user", "assistant"]
        assert final_view["messages"][0]["visible_message_id"] == original["visible_message_id"]
        evidence_message = next(row for row in requests[0][1]
                                if row["source"]["kind"] == "runtime_execution_observation")
        text = evidence_message["blocks"][0]["text"]
        data = loads_strict(text[text.index("{"):])
        observations = data["runs"][0]["tool_observations"]
        assert [row["known_outcome"] for row in observations] == [
            "success", "outcome_unknown", "never_started",
        ]
        assert observations[0]["saved_result"]["blocks"][0]["content"] == {"text": "saved", "saved": True}
        assert observations[1]["tool_execution_id"] is not None
        assert observations[2]["tool_execution_id"] is None
        assert "Check idempotency" in text and "ask the user" in text
        assert "PRIVATE-HOST-DETAIL" not in text + str(final_view)
        assert entries[:2] == ["saved", "uncertain"]
        assert "not-dispatched" not in entries
        with closing(SqliteStore(database)) as store:
            assert store.read_execution_facts(old_run["run_id"]) == facts_before
        final = bundle(database)
        assert final["execution_continuation"][0]["source_run_id"] == old_run["run_id"]
        assert final["execution_continuation"][0]["reused_run_ids"] == []
        new_run = next(row for row in final["run_record"] if row["run_id"] == receipt["run_id"])
        assert new_run["source_run_id"] is None
        assert new_run["chain_run_id"] != old_run["chain_run_id"]


def test_program_fault_is_closed_but_not_disguised_as_model_continuation(database):

    class Broken:
        def generate(self, messages, tools):
            raise ValueError("PRIVATE-ADAPTER-FAULT")

        def close(self):
            raise RuntimeError("SECONDARY-CLOSE-FAULT")

    with closing(WorkflowService(database, model_factory=lambda stage: Broken())) as service:
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "Do not conceal a program fault", "input")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"]["code"] == "adapter_contract_error"
        service.close_execution(sid, submitted["chain_run_id"], **cas(service, sid, "close"))
        closed = service.get_session(sid)
        assert closed["can_submit"] and closed["can_reroll"]
        assert not closed["can_continue_workflow"]
        assert closed["available_actions"] == ["reroll"]
        assert closed["error"]["code"] == "adapter_contract_error"
        before = bundle(database)
        with pytest.raises(ContractValidationError, match="explicit repair"):
            service.continue_workflow(
                sid, submitted["chain_run_id"], **cas(service, sid, "refuse"),
            )
        assert bundle(database) == before
        assert "PRIVATE" not in str(closed) + str(before["execution_closeout"])


def test_close_and_continue_transactions_roll_back_without_dispatch(database):
    requests = []

    class Broken:
        def generate(self, messages, tools):
            raise asyncio.CancelledError()

    def factory(stage):
        requests.append(stage)
        return Broken()

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "Atomic closure", "input")
        service.wait_for_idle(sid)
        close_args = cas(service, sid, "close")
        before = bundle(database)

        def fault(point):
            if point == "before_commit":
                raise RuntimeError("transaction injection")

        service._fault_injector = fault
        with pytest.raises(RuntimeError, match="transaction injection"):
            service.close_execution(sid, submitted["chain_run_id"], **close_args)
        service._fault_injector = None
        assert bundle(database) == before
        service.close_execution(sid, submitted["chain_run_id"], **close_args)
        before = bundle(database)
        continue_args = cas(service, sid, "continue")
        service._fault_injector = fault
        with pytest.raises(RuntimeError, match="transaction injection"):
            service.continue_workflow(sid, submitted["chain_run_id"], **continue_args)
        service._fault_injector = None
        assert bundle(database) == before
        assert requests == ["A"]
        with pytest.raises(ContractValidationError, match="revision conflict"):
            service.continue_workflow(sid, submitted["chain_run_id"], **{
                **continue_args, "expected_session_revision": continue_args["expected_session_revision"] - 1,
            })
        with pytest.raises(ContractValidationError, match="different workflow operation"):
            service.close_execution(sid, submitted["chain_run_id"], **{
                **close_args, "expected_session_revision": close_args["expected_session_revision"] + 1,
            })


def test_repeated_b_continuation_retains_a_and_all_rolls_then_whole_chain_rerolls(database):
    fail_b = True
    entered = []

    class BrokenB:
        def generate(self, messages, tools):
            raise asyncio.CancelledError()

    def factory(stage):
        entered.append(stage)
        return BrokenB() if stage == "B" and fail_b else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "One A, multiple B attempts", "input")
        service.wait_for_idle(sid)
        original = bundle(database)
        old_a = next(row for row in original["run_record"] if row["node_binding_id"] == A_BINDING)
        assert old_a["status"] == "succeeded"
        service.close_execution(sid, submitted["chain_run_id"], **cas(service, sid, "close-1"))
        retry = service.continue_workflow(
            sid, submitted["chain_run_id"], **cas(service, sid, "continue-1"),
        )
        service.wait_for_idle(sid)
        assert entered == ["A", "B", "B"]
        service.close_execution(sid, retry["chain_run_id"], **cas(service, sid, "close-2"))
        fail_b = False
        completed = service.continue_workflow(
            sid, retry["chain_run_id"], **cas(service, sid, "continue-2"),
        )
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"] is None and view["can_reroll"]
        assert [row["role"] for row in view["messages"]] == ["user", "assistant"]
        assert view["messages"][0]["visible_message_id"] == submitted["visible_message_id"]
        assert entered == ["A", "B", "B", "B"]
        after = bundle(database)
        assert next(row for row in after["run_record"] if row["run_id"] == old_a["run_id"]) == old_a
        assert all(row in after["turn"] for row in original["turn"])
        for record in after["execution_continuation"]:
            assert record["reused_run_ids"] == [old_a["run_id"]]
        rolled = service.reroll(
            sid, completed["chain_run_id"], **cas(service, sid, "whole-chain"),
        )
        service.wait_for_idle(sid)
        rolled_view = service.get_session(sid)
        assert rolled_view["error"] is None
        assert entered[-2:] == ["A", "B"]
        assert len(rolled_view["messages"][-1]["reply_candidates"]) == 2
        assert rolled_view["messages"][-1]["chain_run_id"] == rolled["chain_run_id"]
        branch = service.create_branch(
            sid, rolled_view["messages"][-1]["visible_message_id"],
            idempotency_key="branch", expected_source_revision=rolled_view["revision"],
        )
        assert len(service.get_session(branch["workflow_session_id"])["messages"][-1]["reply_candidates"]) == 2


def test_failed_reroll_continues_on_latest_floor_and_keeps_original_candidate(database):
    failing = False

    class Interrupted:
        def generate(self, messages, tools):
            raise asyncio.CancelledError()

    def factory(stage):
        return Interrupted() if failing and stage == "B" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "Keep the existing reply", "input")
        service.wait_for_idle(sid)
        original = service.get_session(sid)
        original_id = original["messages"][-1]["reply_candidates"][0]["candidate_id"]
        saved = bundle(database)
        failing = True
        failed_roll = service.reroll(
            sid, submitted["chain_run_id"], **cas(service, sid, "failed-roll"),
        )
        service.wait_for_idle(sid)
        service.close_execution(sid, failed_roll["chain_run_id"], **cas(service, sid, "close-roll"))
        closed = service.get_session(sid)
        assert closed["messages"] == original["messages"]
        assert closed["can_continue_workflow"]
        assert closed["head_commit_id"] == original["head_commit_id"]
        failing = False
        fresh = service.continue_workflow(
            sid, failed_roll["chain_run_id"], **cas(service, sid, "continue-roll"),
        )
        service.wait_for_idle(sid)
        final = service.get_session(sid)
        assert final["error"] is None
        assert [row["role"] for row in final["messages"]] == ["user", "assistant"]
        candidates = final["messages"][-1]["reply_candidates"]
        assert len(candidates) == 2
        assert candidates[0]["candidate_id"] == original_id
        assert candidates[1]["chain_run_id"] == fresh["chain_run_id"]
        assert all(row in bundle(database)["turn"] for row in saved["turn"])
        service.submit(sid, "Freeze the previous floor", "next-input")
        service.wait_for_idle(sid)
        with pytest.raises(ContractValidationError, match="latest"):
            service.continue_workflow(
                sid, failed_roll["chain_run_id"], **cas(service, sid, "history-continue"),
            )
        with pytest.raises(ContractValidationError, match="latest"):
            service.reroll(sid, failed_roll["chain_run_id"], **cas(service, sid, "history-roll"))


def test_closed_later_execution_unblocks_stable_fork_and_fresh_submit(database):
    failing = False

    class Interrupted:
        def generate(self, messages, tools):
            raise asyncio.CancelledError()

    def factory(stage):
        return Interrupted() if failing else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Stable first floor", "first")
        service.wait_for_idle(sid)
        stable = service.get_session(sid)
        failing = True
        failed = service.submit(sid, "Abandoned second floor", "second")
        service.wait_for_idle(sid)
        service.close_execution(sid, failed["chain_run_id"], **cas(service, sid, "close"))
        closed = service.get_session(sid)
        assert closed["can_submit"]
        branch = service.create_branch(
            sid, stable["messages"][-1]["visible_message_id"],
            idempotency_key="stable-branch", expected_source_revision=closed["revision"],
        )
        assert service.get_session(branch["workflow_session_id"])["messages"] == stable["messages"]
        failing = False
        service.submit(sid, "Fresh third floor", "third")
        service.wait_for_idle(sid)
        final = service.get_session(sid)
        assert final["error"] is None and final["can_submit"]
        assert final["messages"][:2] == stable["messages"]
        assert len(final["messages"]) == 4
        records = bundle(database)
        assert len([row for row in records["visible_message"] if row["role"] == "user"]) == 3


def test_missing_legacy_evidence_stays_unknown_without_fake_tool_pairs(database):
    # The first durable acceptance precedes model construction. A process can
    # disappear here without leaving a ledger or executing a tool.
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        original_executor = service._executor

        class NotDispatched:
            def submit(self, *args, **kwargs):
                from concurrent.futures import Future
                return Future()

        service._executor = NotDispatched()
        accepted = service.submit(sid, "No durable runtime facts", "input")
        service._executor = original_executor
    requests = []

    class Recording(OfflineAdapter):
        def generate(self, messages, tools):
            requests.append(copy.deepcopy(messages))
            return super().generate(messages, tools)

    with closing(WorkflowService(database, model_factory=Recording)) as reopened:
        assert requests == []
        reopened.close_execution(sid, accepted["chain_run_id"], **cas(reopened, sid, "close"))
        reopened.continue_workflow(sid, accepted["chain_run_id"], **cas(reopened, sid, "continue"))
        reopened.wait_for_idle(sid)
        message = next(row for row in requests[0]
                       if row["source"]["kind"] == "runtime_execution_observation")
        text = message["blocks"][0]["text"]
        data = loads_strict(text[text.index("{"):])
        assert data["runs"][0]["evidence_available"] is False
        assert data["runs"][0]["accepted_messages"] == []
        assert data["runs"][0]["tool_observations"] == []
        assert "unknown, not that nothing ran" in text
        assert not any(row["role"] == "tool" for row in requests[0])


def test_model_failure_then_status_write_failure_stays_program_fault_after_restart(database):
    entered, release = Event(), Event()

    class ModelFailure:
        def generate(self, messages, tools):
            entered.set()
            assert release.wait(20)
            raise httpx.ReadTimeout("PRIVATE-TRANSPORT-DETAIL")

    with closing(WorkflowService(database, model_factory=lambda stage: ModelFailure())) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submitted = service.submit(sid, "Do not lose the secondary storage fault", "input")
            assert entered.wait(10)

            def fault(point):
                if point == "before_commit":
                    raise RuntimeError("PRIVATE-STATUS-WRITE-DETAIL")

            service._fault_injector = fault
        finally:
            release.set()
        service.wait_for_idle(sid)
        service._fault_injector = None
        view = service.get_session(sid)
        assert view["error"]["code"] == "ERROR_RECORD_NOT_SAVED"
        with closing(SqliteStore(database)) as store:
            run = next(row for row in store.list_records("run_record")
                       if row["workflow_session_id"] == sid)
            facts = store.read_execution_facts(run["run_id"])
        failures = [row["payload"] for row in facts if row["kind"] == "execution_failed"]
        assert failures[-2]["category"] == "model"
        assert failures[-1]["category"] == "contract"
        assert failures[-1]["code"] == "PROGRAM_OR_STORAGE_FAILED"
        assert "PRIVATE-" not in str(facts) + str(view)
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"reopened dispatched {stage}"),
    )) as reopened:
        reopened.close_execution(
            sid, submitted["chain_run_id"], **cas(reopened, sid, "close"),
        )
        closed = reopened.get_session(sid)
        assert closed["error"]["code"] == "PROGRAM_OR_STORAGE_FAILED"
        assert closed["can_reroll"] and not closed["can_continue_workflow"]
        with pytest.raises(ContractValidationError, match="explicit repair"):
            reopened.continue_workflow(
                sid, submitted["chain_run_id"], **cas(reopened, sid, "refuse"),
            )


def test_successful_a_coordination_fault_cannot_be_hidden_by_empty_b_ledger(database, monkeypatch):
    original_prepare = WorkflowService._prepare_next_node

    def prepare_then_fail(self, sid, chain_id, output):
        original_prepare(self, sid, chain_id, output)
        raise RuntimeError("PRIVATE-COORDINATION-DETAIL")

    monkeypatch.setattr(WorkflowService, "_prepare_next_node", prepare_then_fail)
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "Save the program fault on the successful A", "input")
        service.wait_for_idle(sid)
        before = bundle(database)
        old_a = next(row for row in before["run_record"] if row["node_binding_id"] == A_BINDING)
        old_b = next(row for row in before["run_record"] if row["node_binding_id"] == B_BINDING)
        assert old_a["status"] == "succeeded" and old_b["status"] == "prepared"
        with closing(SqliteStore(database)) as store:
            assert store.read_execution_facts(old_b["run_id"]) == []
            chain = store.get_record("chain_run", {"chain_run_id": submitted["chain_run_id"]})
            records, closeout = service._closeout_records(store, sid, chain, "execution_failed")
            assert closeout["diagnostic"] == {
                "code": "PROGRAM_OR_STORAGE_FAILED", "category": "contract",
            }
            closeout["diagnostic"] = {"code": "UNRECORDED_PROGRAM_FAILURE", "category": "contract"}
            arguments = cas(service, sid, "tampered")
            head = service._workflow_ref(store, sid)
            operation = service._operation(
                "close_execution", sid, "chain_run", submitted["chain_run_id"], "tampered",
                [
                    {"kind": "workflow_session", "id": sid, "revision": arguments["expected_session_revision"]},
                    {"kind": "workflow_ref", "id": head["workflow_ref_id"], "revision": head["revision"]},
                ], {"closeout_id": closeout["closeout_id"]},
                expected_head_commit_id=head["head_commit_id"],
            )
            with pytest.raises(ContractValidationError, match="diagnostic differs"):
                service._save(
                    store, sid, [*records, ("workflow_operation", operation)], "tampered",
                    operation="workflow.close_execution",
                    expected_ref_heads={head["workflow_ref_id"]: (head["revision"], head["head_commit_id"])},
                )
            assert store.read_bundle() == before
        service.close_execution(sid, submitted["chain_run_id"], **cas(service, sid, "close"))
        closed = service.get_session(sid)
        assert closed["can_reroll"] and not closed["can_continue_workflow"]
        assert next(row for row in bundle(database)["run_record"] if row["run_id"] == old_a["run_id"]) == old_a
        with closing(SqliteStore(database)) as store:
            old_facts = store.read_execution_facts(old_a["run_id"])
            late = copy.deepcopy(old_facts[-1])
            late.update(fact_id=str(uuid4()), sequence=len(old_facts) + 1)
            with pytest.raises(ContractValidationError, match="Closed execution"):
                store.append_execution_fact(late, expected_sequence=len(old_facts))


@pytest.mark.parametrize("signal_type", [SystemExit, KeyboardInterrupt, GeneratorExit])
def test_host_signal_remains_loud_and_can_be_closed_without_restart(database, signal_type):
    stopped = signal_type("PRIVATE-HOST-SIGNAL")
    failing = True
    entered = []

    class Stop:
        def generate(self, messages, tools):
            raise stopped

    def factory(stage):
        entered.append(stage)
        return Stop() if failing else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        accepted = service.submit(sid, "A stopped worker has no recoverable runtime", "input")
        with pytest.raises(signal_type) as caught:
            service.wait_for_idle(sid)
        assert caught.value is stopped
        view = service.get_session(sid)
        assert view["error"]["code"] == "HOST_INTERRUPTED"
        assert view["available_actions"] == ["close_execution"]
        assert entered == ["A"]
        service.close_execution(sid, accepted["chain_run_id"], **cas(service, sid, "close"))
        closed = service.get_session(sid)
        assert closed["can_continue_workflow"] and closed["can_reroll"]
        assert entered == ["A"]
        records = bundle(database)
        assert records["execution_closeout"][0]["diagnostic"] == {
            "code": "HOST_INTERRUPTED", "category": "interrupted",
        }
        assert "PRIVATE-HOST-SIGNAL" not in str(records) + str(view)
        failing = False
        service.continue_workflow(sid, accepted["chain_run_id"], **cas(service, sid, "continue"))
        service.wait_for_idle(sid)
        assert entered == ["A", "A", "B"]
        assert service.get_session(sid)["error"] is None


def test_host_signal_during_user_pause_does_not_leave_an_uncloseable_workspace(database):
    entered, release = Event(), Event()
    stopped = SystemExit()

    class StopAfterPause:
        def generate(self, messages, tools):
            entered.set()
            assert release.wait(20)
            raise stopped

    with closing(WorkflowService(database, model_factory=lambda stage: StopAfterPause())) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            accepted = service.submit(sid, "Pause and host exit are distinct", "input")
            assert entered.wait(10)
            before = service.get_session(sid)
            run = before["nodes"][0]
            service.interrupt(
                sid, run["run_id"], idempotency_key="pause",
                expected_session_revision=before["revision"], expected_run_revision=run["revision"],
            )
        finally:
            release.set()
        with pytest.raises(SystemExit) as caught:
            service.wait_for_idle(sid)
        assert caught.value is stopped
        view = service.get_session(sid)
        assert view["chains"][-1]["status"] == "running"
        assert view["nodes"][0]["status"] == "pausing"
        assert view["available_actions"] == ["close_execution"]
        service.close_execution(sid, accepted["chain_run_id"], **cas(service, sid, "close"))
        assert service.get_session(sid)["can_continue_workflow"]
        assert service.get_session(sid)["chains"][-1]["status"] == "closed"


class DirectKernelBaseFault(BaseException):
    pass


class RetainedExitKernel(SnapshotKernel):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure
        self.callbacks = None
        self.generation = None

    def run(self, *args, on_event, **kwargs):
        self.callbacks = {key: value for key, value in kwargs.items() if key.startswith("on_")}
        self.callbacks["on_event"] = on_event
        self.generation = on_event("chapter.check.progress", REF, {"chapter": 1})["generation"]
        on_event.preview("unaccepted preview")
        if self.failure is not None:
            raise self.failure
        return super().run(*args, **kwargs)


def assert_stopped_kernel_callbacks_are_fenced(service, kernel, run_id):
    generation = service._active_runs[run_id].snapshot().generation
    assert generation != kernel.generation
    source = service._event_sources[run_id]
    assert source.generation == generation == service._event_generations[run_id]
    sequence = source.sequence
    saved = bundle(service.database)
    with closing(SqliteStore(service.database)) as store:
        facts = store.read_execution_facts(run_id)
    assert kernel.callbacks["on_event"]("chapter.check.progress", REF, {"chapter": 2}) is None
    assert kernel.callbacks["on_event"].preview("late") is None
    assert kernel.callbacks["on_boundary"]("before_request") is False
    with pytest.raises(KernelPauseRequested):
        kernel.callbacks["on_tool_event"]("queue", str(uuid4()), None, None)
    with pytest.raises(KernelPauseRequested):
        kernel.callbacks["on_progress"]({"model_requests": 1, "attempts": 1, "accepted_messages": 0})
    with pytest.raises(ContractValidationError, match="Stale"):
        kernel.callbacks["on_fact"]({"kind": "model_request", "payload": {}})
    assert source.sequence == sequence
    assert bundle(service.database) == saved
    with closing(SqliteStore(service.database)) as store:
        assert store.read_execution_facts(run_id) == facts
    assert saved.get("turn", []) == []
    assert next(row for row in saved["run_record"] if row["run_id"] == run_id)["status"] == "running"


@pytest.mark.parametrize("signal_type", [asyncio.CancelledError, GeneratorExit, DirectKernelBaseFault])
def test_direct_kernel_base_exit_is_fenced_and_classified_before_manual_close(database, signal_type):
    stopped = signal_type("PRIVATE-DIRECT-EXIT")
    kernel = RetainedExitKernel(stopped)
    created = []

    def factory(stage):
        created.append(stage)
        return OfflineAdapter(stage)

    with closing(WorkflowService(
        database, components=reporting_components(kernel), model_factory=factory,
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        accepted = service.submit(sid, "Do not leave an exited worker eligible", "input")
        with pytest.raises(signal_type) as caught:
            service.wait_for_idle(sid)
        assert caught.value is stopped
        assert service._execution_failures[sid] is stopped
        run_id = first_run_id(service, accepted)
        assert_stopped_kernel_callbacks_are_fenced(service, kernel, run_id)
        view = service.get_session(sid)
        contract_fault = signal_type is DirectKernelBaseFault
        assert view["error"]["code"] == (
            "STORAGE_OR_COORDINATION_FAILED" if contract_fault else "HOST_INTERRUPTED"
        )
        assert view["available_actions"] == ["close_execution"]
        assert created == ["A"]
        with closing(SqliteStore(database)) as store:
            facts = store.read_execution_facts(run_id)
        assert not any(row["kind"] == "model_request" for row in facts)
        diagnostic = {
            "code": "PROGRAM_OR_STORAGE_FAILED" if contract_fault else "HOST_INTERRUPTED",
            "category": "contract" if contract_fault else "interrupted",
        }
        assert facts[-1]["kind"] == "execution_failed"
        assert {field: facts[-1]["payload"][field] for field in ("code", "category")} == diagnostic
        service.close_execution(sid, accepted["chain_run_id"], **cas(service, sid, "close"))
        closed = service.get_session(sid)
        assert closed["can_reroll"]
        assert closed["can_continue_workflow"] == (not contract_fault)
        assert bundle(database)["execution_closeout"][0]["diagnostic"] == diagnostic
        assert "PRIVATE-DIRECT-EXIT" not in str(closed) + str(facts)
        if contract_fault:
            with pytest.raises(ContractValidationError, match="explicit repair"):
                service.continue_workflow(sid, accepted["chain_run_id"], **cas(service, sid, "refuse"))
            assert created == ["A"]
        else:
            kernel.failure = None
            service.continue_workflow(sid, accepted["chain_run_id"], **cas(service, sid, "continue"))
            service.wait_for_idle(sid)
            assert created == ["A", "A", "B"]
            assert service.get_session(sid)["error"] is None
            with closing(SqliteStore(database)) as store:
                assert store.read_execution_facts(run_id) == facts


@pytest.mark.parametrize("primary_type", [asyncio.CancelledError, DirectKernelBaseFault])
@pytest.mark.parametrize("cleanup_type", [GeneratorExit, RuntimeError])
def test_adapter_close_cannot_replace_a_direct_kernel_base_exit(database, primary_type, cleanup_type):
    primary = primary_type("PRIVATE-PRIMARY-EXIT")
    secondary = cleanup_type("PRIVATE-SECONDARY-CLOSE")
    kernel = RetainedExitKernel(primary)

    class CloseFault(OfflineAdapter):
        def close(self):
            raise secondary

    with closing(WorkflowService(
        database, components=reporting_components(kernel), model_factory=CloseFault,
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        accepted = service.submit(sid, "Preserve the original exit object", "input")
        with pytest.raises(primary_type) as caught:
            service.wait_for_idle(sid)
        assert caught.value is primary
        assert service._execution_failures[sid] is primary
        assert getattr(primary, "__notes__", []) == ["Adapter cleanup also failed."]
        assert_stopped_kernel_callbacks_are_fenced(service, kernel, first_run_id(service, accepted))


@pytest.mark.parametrize("secondary_boundary", ["diagnostic_write", "preview_projection"])
def test_direct_kernel_base_exit_survives_secondary_diagnosis_and_projection_faults(
    database, monkeypatch, secondary_boundary,
):
    primary = asyncio.CancelledError("PRIVATE-PRIMARY-EXIT")
    secondary = SystemExit("PRIVATE-SECONDARY-DIAGNOSIS")
    kernel = RetainedExitKernel(primary)
    with closing(WorkflowService(database, components=reporting_components(kernel))) as service:
        if secondary_boundary == "diagnostic_write":
            def fail_diagnosis(*args):
                raise secondary

            monkeypatch.setattr(service, "_persist_execution_failure", fail_diagnosis)
        else:
            original_publish = service._publish_event

            def fail_projection(run_id, event_type, payload, **kwargs):
                if event_type == "preview_cleared":
                    generation = service._active_runs[run_id].snapshot().generation
                    assert generation != kernel.generation
                    assert service._event_sources[run_id].generation == generation
                    assert service._event_generations[run_id] == generation
                    raise secondary
                return original_publish(run_id, event_type, payload, **kwargs)

            monkeypatch.setattr(service, "_publish_event", fail_projection)
        sid = service.create_session()["workflow_session_id"]
        accepted = service.submit(sid, "Fence before fallible reporting", "input")
        with pytest.raises(asyncio.CancelledError) as caught:
            service.wait_for_idle(sid)
        assert caught.value is primary
        assert service._execution_failures[sid] is primary
        assert service.get_session(sid)["error"]["code"] == "HOST_INTERRUPTED"
        assert_stopped_kernel_callbacks_are_fenced(service, kernel, first_run_id(service, accepted))


def test_independent_adapter_close_base_signal_is_not_swallowed(database):
    stopped = GeneratorExit("PRIVATE-INDEPENDENT-CLOSE")
    kernel = RetainedExitKernel(None)
    created = []

    class CloseSignal(OfflineAdapter):
        def close(self):
            raise stopped

    def factory(stage):
        created.append(stage)
        return CloseSignal(stage)

    with closing(WorkflowService(
        database, components=reporting_components(kernel), model_factory=factory,
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        accepted = service.submit(sid, "An independent close signal remains loud", "input")
        with pytest.raises(GeneratorExit) as caught:
            service.wait_for_idle(sid)
        assert caught.value is stopped
        assert service._execution_failures[sid] is stopped
        assert service.get_session(sid)["error"]["code"] == "HOST_INTERRUPTED"
        assert created == ["A"]
        run_id = first_run_id(service, accepted)
        assert_stopped_kernel_callbacks_are_fenced(service, kernel, run_id)
        with closing(SqliteStore(database)) as store:
            facts = store.read_execution_facts(run_id)
        assert any(row["kind"] == "message_accepted" for row in facts)
        assert facts[-1]["payload"]["code"] == "HOST_INTERRUPTED"
