"""N06 replacement of a safely paused first node keeps the user input."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import B_BINDING, OfflineAdapter, WorkflowService


def test_paused_first_node_reroll_closes_old_run_and_creates_a_new_candidate():
    started, release = Event(), Event()
    calls = []

    class BlockingAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            calls.append(self.stage)
            if self.stage == "A" and len(calls) == 1:
                started.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with TemporaryDirectory(prefix="paused-reroll-", dir=Path(__file__).parent) as folder:
        path = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(path, model_factory=BlockingAdapter)) as service:
            sid = service.create_session()["workflow_session_id"]
            submitted = service.submit(sid, "Preserve this user message", "submit")
            try:
                assert started.wait(10)
                before = service.get_session(sid)
                old_run_id = before["nodes"][0]["run_id"]
                with closing(SqliteStore(path)) as store:
                    old_run = store.get_record("run_record", {"run_id": old_run_id})
                service.interrupt(
                    sid, old_run_id, idempotency_key="interrupt",
                    expected_session_revision=before["revision"],
                    expected_run_revision=old_run["revision"],
                )
                with pytest.raises(ContractValidationError):
                    service.reroll(
                        sid, submitted["chain_run_id"], idempotency_key="too-early",
                        expected_session_revision=service.get_session(sid)["revision"],
                        expected_ref_revision=before["ref_revision"],
                        expected_head_commit_id=before["head_commit_id"],
                    )
            finally:
                release.set()
            service.wait_for_idle(sid)
            paused = service.get_session(sid)
            assert paused["can_reroll"]
            assert paused["available_actions"] == ["resume", "reroll"]
            receipt = service.reroll(
                sid, submitted["chain_run_id"], idempotency_key="replacement",
                expected_session_revision=paused["revision"],
                expected_ref_revision=paused["ref_revision"],
                expected_head_commit_id=paused["head_commit_id"],
            )
            assert service.reroll(
                sid, submitted["chain_run_id"], idempotency_key="replacement",
                expected_session_revision=paused["revision"],
                expected_ref_revision=paused["ref_revision"],
                expected_head_commit_id=paused["head_commit_id"],
            ) == receipt
            service.wait_for_idle(sid)
            with closing(SqliteStore(path)) as store:
                bundle = validate_bundle(store.read_bundle())
            old = next(row for row in bundle["run_record"] if row["run_id"] == old_run_id)
            old_chain = next(row for row in bundle["chain_run"]
                             if row["chain_run_id"] == submitted["chain_run_id"])
            new = next(row for row in bundle["run_record"] if row["run_id"] == receipt["run_id"])
            assert old["status"] == old_chain["status"] == "superseded"
            assert old["superseded_by_run_id"] == new["run_id"]
            assert new["source_run_id"] == old["run_id"]
            assert new["input_id"] != old["input_id"]
            assert len([row for row in bundle["workflow_candidate"]
                        if row["workflow_session_id"] == sid]) == 1
            view = service.get_session(sid)
            assert view["can_submit"]
            assert [row["role"] for row in view["messages"]] == ["user", "assistant"]
            assert view["messages"][0]["visible_message_id"] == submitted["visible_message_id"]
            assert view["messages"][1]["chain_run_id"] == receipt["chain_run_id"]
            assert calls.count("A") == 3  # Discarded old response, then new A's two requests.
            candidate = next(row for row in bundle["workflow_candidate"]
                             if row["chain_run_id"] == receipt["chain_run_id"])
            assert view["messages"][-1]["reply_candidates"][0]["candidate_id"] == candidate["candidate_id"]
            branch = service.create_branch(
                sid, view["messages"][-1]["visible_message_id"],
                idempotency_key="fork-completed", expected_source_revision=view["revision"],
            )
            assert [row["role"] for row in service.get_session(
                branch["workflow_session_id"])["messages"]] == ["user", "assistant"]

            later = service.reroll(
                sid, receipt["chain_run_id"], idempotency_key="another-roll",
                expected_session_revision=service.get_session(sid)["revision"],
                expected_ref_revision=service.get_session(sid)["ref_revision"],
                expected_head_commit_id=service.get_session(sid)["head_commit_id"],
            )
            service.wait_for_idle(sid)
            assert later["chain_run_id"] != receipt["chain_run_id"]
            assert len(service.get_session(sid)["messages"][-1]["reply_candidates"]) == 2


def test_paused_second_node_closes_without_claiming_new_a_as_its_replacement():
    started, release = Event(), Event()

    class BlockingB(OfflineAdapter):
        def generate(self, messages, tools):
            if self.stage == "B":
                started.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with TemporaryDirectory(prefix="paused-b-reroll-", dir=Path(__file__).parent) as folder:
        path = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(path, model_factory=BlockingB)) as service:
            sid = service.create_session()["workflow_session_id"]
            submitted = service.submit(sid, "Pause B", "submit")
            try:
                assert started.wait(10)
                before = service.get_session(sid)
                run_id = before["nodes"][1]["run_id"]
                with closing(SqliteStore(path)) as store:
                    run = store.get_record("run_record", {"run_id": run_id})
                service.interrupt(
                    sid, run_id, idempotency_key="interrupt-b",
                    expected_session_revision=before["revision"],
                    expected_run_revision=run["revision"],
                )
            finally:
                release.set()
            service.wait_for_idle(sid)
            paused = service.get_session(sid)
            with closing(SqliteStore(path)) as store:
                before_bundle = store.read_bundle()
                old_a = next(row for row in before_bundle["run_record"]
                             if row["run_id"] == before["nodes"][0]["run_id"])
            assert paused["can_reroll"]
            assert paused["available_actions"] == ["resume", "reroll"]
            receipt = service.reroll(
                sid, submitted["chain_run_id"], idempotency_key="new-chain",
                expected_session_revision=paused["revision"],
                expected_ref_revision=paused["ref_revision"],
                expected_head_commit_id=paused["head_commit_id"],
            )
            service.wait_for_idle(sid)
            with closing(SqliteStore(path)) as store:
                after = validate_bundle(store.read_bundle())
            old_b = next(row for row in after["run_record"] if row["run_id"] == run_id)
            assert old_b["status"] == "closed"
            assert old_b["superseded_by_run_id"] is None
            assert next(row for row in after["run_record"] if row["run_id"] == old_a["run_id"]) == old_a
            assert all(row in after["turn"] for row in before_bundle["turn"])
            new_a = next(row for row in after["run_record"] if row["run_id"] == receipt["run_id"])
            assert new_a["source_run_id"] is None
            assert new_a["chain_run_id"] != old_b["chain_run_id"]
            assert service.get_session(sid)["error"] is None
            assert [row["role"] for row in service.get_session(sid)["messages"]] == ["user", "assistant"]


def test_interrupted_reroll_rejects_unknown_tools_and_atomic_write_failure():
    started, release = Event(), Event()

    class BlockingA(OfflineAdapter):
        def generate(self, messages, tools):
            if self.stage == "A":
                started.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with TemporaryDirectory(prefix="paused-reroll-gates-", dir=Path(__file__).parent) as folder:
        path = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(path, model_factory=BlockingA)) as service:
            sid = service.create_session()["workflow_session_id"]
            submitted = service.submit(sid, "Atomic replacement", "submit")
            try:
                assert started.wait(10)
                view = service.get_session(sid)
                run_id = view["nodes"][0]["run_id"]
                with closing(SqliteStore(path)) as store:
                    run = store.get_record("run_record", {"run_id": run_id})
                service.interrupt(
                    sid, run_id, idempotency_key="interrupt",
                    expected_session_revision=view["revision"],
                    expected_run_revision=run["revision"],
                )
            finally:
                release.set()
            service.wait_for_idle(sid)
            paused = service.get_session(sid)
            arguments = {
                "idempotency_key": "retry-after-fault",
                "expected_session_revision": paused["revision"],
                "expected_ref_revision": paused["ref_revision"],
                "expected_head_commit_id": paused["head_commit_id"],
            }
            active = service._active_runs[run_id]
            state = active.snapshot()
            call_id = str(uuid4())
            active.queue_tool(call_id, expected_generation=state.generation,
                              expected_revision=state.revision)
            assert not service.get_session(sid)["can_reroll"]
            with pytest.raises(ContractValidationError, match="stopped and settled"):
                service.reroll(sid, submitted["chain_run_id"], **arguments)
            state = active.snapshot()
            active.start_tool(call_id, str(uuid4()),
                              expected_generation=state.generation,
                              expected_revision=state.revision)
            state = active.snapshot()
            active.settle_tool(call_id, "unknown",
                               expected_generation=state.generation,
                               expected_revision=state.revision)
            assert not service.get_session(sid)["can_reroll"]
            with pytest.raises(ContractValidationError, match="stopped and settled"):
                service.reroll(sid, submitted["chain_run_id"], **arguments)
            state = active.snapshot()
            active.settle_tool(call_id, "success",
                               expected_generation=state.generation,
                               expected_revision=state.revision)
            with closing(SqliteStore(path)) as store:
                before = store.read_bundle()
            def fault(phase):
                if phase == "after_first_write":
                    raise RuntimeError("injected")
            service._fault_injector = fault
            with pytest.raises(RuntimeError, match="injected"):
                service.reroll(sid, submitted["chain_run_id"], **arguments)
            service._fault_injector = None
            with closing(SqliteStore(path)) as store:
                assert store.read_bundle() == before
            assert service.get_session(sid)["can_reroll"]
            assert service._run_checkpoints.get(run_id) is not None
            assert service._active_runs[run_id] is active


def test_reopened_interrupted_input_cannot_reroll_without_its_live_workspace():
    started, release = Event(), Event()

    class BlockingA(OfflineAdapter):
        def generate(self, messages, tools):
            if self.stage == "A":
                started.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with TemporaryDirectory(prefix="paused-reroll-reopen-", dir=Path(__file__).parent) as folder:
        path = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(path, model_factory=BlockingA)) as service:
            sid = service.create_session()["workflow_session_id"]
            submitted = service.submit(sid, "Do not rerun after restart", "submit")
            try:
                assert started.wait(10)
                view = service.get_session(sid)
                run_id = view["nodes"][0]["run_id"]
                with closing(SqliteStore(path)) as store:
                    run = store.get_record("run_record", {"run_id": run_id})
                service.interrupt(
                    sid, run_id, idempotency_key="interrupt",
                    expected_session_revision=view["revision"],
                    expected_run_revision=run["revision"],
                )
            finally:
                release.set()
            service.wait_for_idle(sid)
        with closing(WorkflowService(path, model_factory=lambda stage: pytest.fail(stage))) as reopened:
            view = reopened.get_session(sid)
            assert not view["can_reroll"]
            with pytest.raises(ContractValidationError):
                reopened.reroll(
                    sid, submitted["chain_run_id"], idempotency_key="after-restart",
                    expected_session_revision=view["revision"],
                    expected_ref_revision=view["ref_revision"],
                    expected_head_commit_id=view["head_commit_id"],
                )


def test_interrupted_replacement_publish_retry_does_not_require_old_candidate(monkeypatch):
    started, release = Event(), Event()
    first = True
    calls = []

    class BlockingA(OfflineAdapter):
        def generate(self, messages, tools):
            nonlocal first
            calls.append(self.stage)
            if self.stage == "A" and first:
                first = False
                started.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with TemporaryDirectory(prefix="paused-reroll-publish-", dir=Path(__file__).parent) as folder:
        path = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(path, model_factory=BlockingA)) as service:
            sid = service.create_session()["workflow_session_id"]
            submitted = service.submit(sid, "Retry published candidate", "submit")
            try:
                assert started.wait(10)
                view = service.get_session(sid)
                run_id = view["nodes"][0]["run_id"]
                with closing(SqliteStore(path)) as store:
                    run = store.get_record("run_record", {"run_id": run_id})
                service.interrupt(
                    sid, run_id, idempotency_key="interrupt",
                    expected_session_revision=view["revision"],
                    expected_run_revision=run["revision"],
                )
            finally:
                release.set()
            service.wait_for_idle(sid)
            paused = service.get_session(sid)
            original_select = WorkflowService._select_workflow_candidate
            conflict = True

            def conflict_once(self, store, sid, candidate, **kwargs):
                nonlocal conflict
                if conflict:
                    conflict = False
                    raise ContractValidationError("Workflow ref head conflict")
                return original_select(self, store, sid, candidate, **kwargs)

            with monkeypatch.context() as fault:
                fault.setattr(WorkflowService, "_select_workflow_candidate", conflict_once)
                replacement = service.reroll(
                    sid, submitted["chain_run_id"], idempotency_key="replacement",
                    expected_session_revision=paused["revision"],
                    expected_ref_revision=paused["ref_revision"],
                    expected_head_commit_id=paused["head_commit_id"],
                )
                service.wait_for_idle(sid)
            with closing(SqliteStore(path)) as store:
                bundle = store.read_bundle()
            assert len(bundle["workflow_candidate"]) == 1
            assert bundle["workflow_ref"][0]["head_commit_id"] == paused["head_commit_id"]
            chain = next(row for row in bundle["chain_run"]
                         if row["chain_run_id"] == replacement["chain_run_id"])
            b_run_id = chain["node_run_ids"][1]
            attempts = len(calls)
            result = service.retry_publish(
                sid, b_run_id, idempotency_key="retry-publish",
                expected_session_revision=service.get_session(sid)["revision"],
            )
            assert result["status"] == "succeeded"
            assert len(calls) == attempts
            assert service.get_session(sid)["messages"][-1]["role"] == "assistant"


def test_b_paused_before_tool_dispatch_can_close_queued_calls_and_reroll_atomically(monkeypatch):
    entered, release = Event(), Event()
    intercepted = False
    original_event = WorkflowService._run_tool_event

    def stop_before_start(self, run_id, generation, kind, call_id, execution_id, outcome):
        nonlocal intercepted
        with closing(SqliteStore(self.database)) as store:
            run = store.get_record("run_record", {"run_id": run_id})
        if kind == "start" and run["node_binding_id"] == B_BINDING and not intercepted:
            intercepted = True
            entered.set()
            assert release.wait(20)
        return original_event(self, run_id, generation, kind, call_id, execution_id, outcome)

    monkeypatch.setattr(WorkflowService, "_run_tool_event", stop_before_start)
    with TemporaryDirectory(prefix="queued-b-reroll-", dir=Path(__file__).parent) as folder:
        path = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(path)) as service:
            sid = service.create_session()["workflow_session_id"]
            submitted = service.submit(sid, "B has accepted but not dispatched its tool", "input")
            try:
                assert entered.wait(10)
                view = service.get_session(sid)
                old_b = view["nodes"][1]
                service.interrupt(
                    sid, old_b["run_id"], idempotency_key="pause",
                    expected_session_revision=view["revision"], expected_run_revision=old_b["revision"],
                )
            finally:
                release.set()
            service.wait_for_idle(sid)
            paused = service.get_session(sid)
            assert paused["can_reroll"]
            assert service._active_runs[old_b["run_id"]].snapshot().pending_tools
            with closing(SqliteStore(path)) as store:
                before = store.read_bundle()
                facts = store.read_execution_facts(old_b["run_id"])
            assert any(row["kind"] == "message_accepted" for row in facts)
            assert not any(row["kind"] == "tool_dispatch" for row in facts)
            arguments = {
                "idempotency_key": "roll", "expected_session_revision": paused["revision"],
                "expected_ref_revision": paused["ref_revision"],
                "expected_head_commit_id": paused["head_commit_id"],
            }

            def fault(point):
                if point == "after_first_write":
                    raise RuntimeError("atomic queued closeout")

            service._fault_injector = fault
            with pytest.raises(RuntimeError, match="atomic queued closeout"):
                service.reroll(sid, submitted["chain_run_id"], **arguments)
            service._fault_injector = None
            with closing(SqliteStore(path)) as store:
                assert store.read_bundle() == before
            assert old_b["run_id"] in service._run_checkpoints
            receipt = service.reroll(sid, submitted["chain_run_id"], **arguments)
            service.wait_for_idle(sid)
            assert service.get_session(sid)["error"] is None
            with closing(SqliteStore(path)) as store:
                after = validate_bundle(store.read_bundle())
                assert store.read_execution_facts(old_b["run_id"]) == facts
            closed = next(row for row in after["run_record"] if row["run_id"] == old_b["run_id"])
            assert closed["status"] == "closed"
            assert closed["superseded_by_run_id"] is None
            assert old_b["run_id"] not in service._active_runs
            assert receipt["chain_run_id"] != submitted["chain_run_id"]
