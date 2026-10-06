"""Cross-restart M2 acceptance boundaries using the real offline service."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import httpx
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


def test_safe_replacement_survives_restart_without_reviving_superseded_run():
    started, release = Event(), Event()
    first_a_request = True
    model_calls = []

    class BlockingFirstA(OfflineAdapter):
        def generate(self, messages, tools):
            nonlocal first_a_request
            model_calls.append(self.stage)
            if self.stage == "A" and first_a_request:
                first_a_request = False
                started.set()
                assert release.wait(10)
            return super().generate(messages, tools)

    with TemporaryDirectory(prefix="n07-replacement-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=BlockingFirstA)) as service:
            sid = service.create_session()["workflow_session_id"]
            submitted = service.submit(sid, "One user, one reply", "submit")
            try:
                assert started.wait(10)
                running = service.get_session(sid)
                old_run_id = running["nodes"][0]["run_id"]
                service.interrupt(
                    sid, old_run_id, idempotency_key="interrupt",
                    expected_session_revision=running["revision"],
                    expected_run_revision=running["nodes"][0]["revision"],
                )
            finally:
                release.set()
            service.wait_for_idle(sid)
            paused = service.get_session(sid)
            assert paused["available_actions"] == ["resume", "reroll"]
            replacement = service.reroll(
                sid, submitted["chain_run_id"], idempotency_key="replacement",
                expected_session_revision=paused["revision"],
                expected_ref_revision=paused["ref_revision"],
                expected_head_commit_id=paused["head_commit_id"],
            )
            service.wait_for_idle(sid)
            completed = service.get_session(sid)
            assert completed["error"] is None
            assert [message["role"] for message in completed["messages"]] == [
                "user", "assistant",
            ]
            assert completed["messages"][0]["visible_message_id"] == submitted["visible_message_id"]
            assert completed["messages"][1]["chain_run_id"] == replacement["chain_run_id"]
            assert len(completed["messages"][1]["reply_candidates"]) == 1
            assert completed["messages"][1]["reply_candidates"][0]["selected"]
            with closing(SqliteStore(database)) as store:
                old = store.get_record("run_record", {"run_id": old_run_id})
                old_chain = store.get_record(
                    "chain_run", {"chain_run_id": submitted["chain_run_id"]},
                )
                assert old["status"] == old_chain["status"] == "superseded"
                assert old["superseded_by_run_id"] == replacement["run_id"]
            dispatched = list(model_calls)

        with closing(WorkflowService(
            database, model_factory=lambda stage: pytest.fail(f"restarted {stage}"),
        )) as reopened:
            recovered = reopened.get_session(sid)
            assert recovered["error"] is None
            assert recovered["messages"] == completed["messages"]
            assert recovered["chains"] == completed["chains"]
            assert recovered["can_reroll"]
            with closing(SqliteStore(database)) as store:
                bundle = validate_bundle(store.read_bundle())
                old = store.get_record("run_record", {"run_id": old_run_id})
                assert old["status"] == "superseded"
                assert len([
                    row for row in bundle["workflow_candidate"]
                    if row["workflow_session_id"] == sid
                ]) == 1
            with pytest.raises(ContractValidationError):
                reopened.resume(
                    sid, old_run_id, idempotency_key="resume-old",
                    expected_session_revision=recovered["revision"],
                    expected_run_revision=old["revision"],
                )
            assert model_calls == dispatched


def test_later_model_failure_preserves_earlier_formal_history_after_restart():
    fail_next_a = False
    model_calls = []

    class FailingLaterA(OfflineAdapter):
        def generate(self, messages, tools):
            model_calls.append(self.stage)
            if self.stage == "A" and fail_next_a:
                request = httpx.Request("POST", "https://offline.invalid/chat/completions")
                raise httpx.HTTPStatusError(
                    "PRIVATE MODEL DETAIL", request=request,
                    response=httpx.Response(400, request=request),
                )
            return super().generate(messages, tools)

    with TemporaryDirectory(prefix="n07-failed-history-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=FailingLaterA)) as service:
            sid = service.create_session()["workflow_session_id"]
            service.submit(sid, "Stable first floor", "first")
            service.wait_for_idle(sid)
            first = service.get_session(sid)
            assert [message["role"] for message in first["messages"]] == [
                "user", "assistant",
            ]
            fail_next_a = True
            service.submit(sid, "Failed second floor", "second")
            service.wait_for_idle(sid)
            failed = service.get_session(sid)
            assert failed["error"]["code"] == "model_error"
            assert "PRIVATE" not in str(failed)
            assert failed["messages"][:2] == first["messages"]
            assert [message["role"] for message in failed["messages"]] == [
                "user", "assistant", "user",
            ]
            assert not failed["can_submit"]
            with pytest.raises(ContractValidationError):
                service.create_branch(
                    sid, first["messages"][1]["visible_message_id"],
                    idempotency_key="branch-past-failed-run",
                    expected_source_revision=failed["revision"],
                )
            dispatched = list(model_calls)

        with closing(WorkflowService(
            database, model_factory=lambda stage: pytest.fail(f"restarted {stage}"),
        )) as reopened:
            recovered = reopened.get_session(sid)
            assert recovered["error"]["code"] == "RECOVERY_UNAVAILABLE"
            assert recovered["messages"][:2] == first["messages"]
            assert [message["role"] for message in recovered["messages"]] == [
                "user", "assistant", "user",
            ]
            assert not recovered["can_submit"]
            assert recovered["available_actions"] == ["close_execution"]
            assert model_calls == dispatched
