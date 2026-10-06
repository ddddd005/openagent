"""I04/I05 same-process model recovery and explicit independent limits."""

import copy
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

import httpx
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.storage import SqliteStore
from phase1_agent.tools import final_answer_tool, register_callable
from phase1_agent.workflow import A_BINDING, B_BINDING, OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="model-recovery-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def bundle(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def facts(database, run_id):
    with closing(SqliteStore(database)) as store:
        return store.read_execution_facts(run_id)


def control_args(service, sid, run_id, key):
    view = service.get_session(sid)
    node = next(row for row in view["nodes"] if row["run_id"] == run_id)
    return {
        "idempotency_key": key, "expected_session_revision": view["revision"],
        "expected_run_revision": node["revision"],
    }


def set_budget(service, stage, requests, attempts):
    context, kernel, adapter, config = service._resolved[stage]
    bounded = copy.deepcopy(config)
    bounded["payload"]["kernel"].update(
        max_model_requests=requests, max_model_attempts=attempts,
    )
    service._resolved[stage] = context, kernel, adapter, bounded
    kernel.implementation._backoff = lambda retry: None


class HistoryAdapter:
    """Finish based on accepted progress rather than adapter instance memory."""

    def __init__(self, stage, calls):
        self.stage, self.calls = stage, calls

    def generate(self, messages, tools):
        self.calls.append((self.stage, copy.deepcopy(messages)))
        if not any(message["role"] == "tool" for message in messages):
            user = next(message for message in reversed(messages) if message["role"] == "user")
            text = loads_strict(user["blocks"][0]["text"])["text"]
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "inspect_text", dumps_pretty({"text": text})),
            ))
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall(str(uuid4()), "final_answer", dumps_pretty({
                "answer": {"text": "finished " + self.stage},
            })),
        ))


@pytest.mark.parametrize("failed_stage", ["A", "B"])
def test_failed_model_continues_same_run_without_replaying_finished_tools(
    database, monkeypatch, failed_stage,
):
    calls, effects = [], []
    broken = True

    def inspect(text):
        effects.append(text)
        return {"characters": len(text)}

    tool = register_callable("inspect_text", "Inspect a text.", {
        "type": "object", "properties": {"text": {"type": "string", "description": "Text"}},
        "required": ["text"], "additionalProperties": False,
    }, inspect)
    monkeypatch.setattr(WorkflowService, "_tools", staticmethod(lambda: (tool, final_answer_tool())))

    class Transient(HistoryAdapter):
        def generate(self, messages, tools):
            if (broken and self.stage == failed_stage
                    and any(message["role"] == "tool" for message in messages)):
                calls.append((self.stage, copy.deepcopy(messages)))
                raise httpx.ReadTimeout("PRIVATE TRANSIENT DETAIL")
            return super().generate(messages, tools)

    with closing(WorkflowService(database, model_factory=lambda stage: Transient(stage, calls))) as service:
        for stage in ("A", "B"):
            set_budget(service, stage, 8, 32)
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "keep completed work", "input")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        index = 0 if failed_stage == "A" else 1
        node = failed["nodes"][index]
        run_id = node["run_id"]
        assert node["status"] == "failed"
        assert failed["error"]["code"] == "model_retry_exhausted"
        assert failed["available_actions"] == ["resume", "close_execution"]
        assert node["budget"] == {
            "max_model_requests": 8, "max_model_attempts": 32,
            "model_requests": 2, "attempts": 5,
        }
        before = bundle(database)
        prefix = facts(database, run_id)
        old_generation = service._active_runs[run_id].snapshot().generation
        preserved_effects = list(effects)
        arguments = control_args(service, sid, run_id, "resume")
        broken = False
        receipt = service.resume(sid, run_id, **arguments)
        assert service.resume(sid, run_id, **arguments) == receipt
        service.wait_for_idle(sid)
        succeeded = service.get_session(sid)
        assert succeeded["error"] is None
        assert succeeded["nodes"][index]["run_id"] == run_id
        assert succeeded["chains"] == [{
            "chain_run_id": submitted["chain_run_id"], "status": "succeeded",
        }]
        assert [row["role"] for row in succeeded["messages"]] == ["user", "assistant"]
        assert succeeded["messages"][0]["visible_message_id"] == submitted["visible_message_id"]
        assert effects[:len(preserved_effects)] == preserved_effects
        assert len(effects) == 2
        after = bundle(database)
        assert after["input_snapshot"][:len(before["input_snapshot"])] == before["input_snapshot"]
        for turn in before.get("turn", []):
            assert turn in after["turn"]
        for old_run in before["run_record"]:
            new_run = next(row for row in after["run_record"] if row["run_id"] == old_run["run_id"])
            assert new_run["initial_budget"] == old_run["initial_budget"]
            if old_run["status"] == "succeeded":
                assert new_run == old_run
        ledger = facts(database, run_id)
        assert ledger[:len(prefix)] == prefix
        requests = [row for row in ledger if row["kind"] == "model_request"]
        attempts = [row for row in ledger if row["kind"] == "model_attempt_started"]
        assert [row["payload"]["request_index"] for row in requests] == [1, 2, 3]
        assert len({row["payload"]["request_id"] for row in requests}) == 3
        assert len(attempts) == 6
        assert ledger[-1]["generation"] != prefix[-1]["generation"]
        with pytest.raises(ContractValidationError, match="Stale"):
            service._on_fact(run_id, old_generation, {"kind": "execution_failed", "payload": {}})
        assert "PRIVATE" not in str(succeeded)


@pytest.mark.parametrize("axis,requests,attempts,code", [
    ("requests", 1, 4, "model_request_budget_exhausted"),
    ("attempts", 8, 1, "model_attempt_budget_exhausted"),
])
def test_budget_extension_preserves_initial_limits_and_requires_explicit_resume(
    database, axis, requests, attempts, code,
):
    calls = []
    with closing(WorkflowService(database, model_factory=lambda stage: HistoryAdapter(stage, calls))) as service:
        set_budget(service, "A", requests, attempts)
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "independent limits", "input")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        run_id = failed["nodes"][0]["run_id"]
        assert failed["error"]["code"] == code
        assert failed["available_actions"] == ["extend_budget", "close_execution"]
        assert failed["nodes"][0]["budget"]["model_requests"] == 1
        assert failed["nodes"][0]["budget"]["attempts"] == 1
        with pytest.raises(ContractValidationError, match="exhausted"):
            service.resume(sid, run_id, **control_args(service, sid, run_id, "premature"))
        before = bundle(database)
        prefix = facts(database, run_id)
        arguments = {
            **control_args(service, sid, run_id, "extend"),
            "additional_model_requests": 1 if axis == "requests" else 0,
            "additional_model_attempts": 1 if axis == "attempts" else 0,
        }
        receipt = service.extend_budget(sid, run_id, **arguments)
        assert service.extend_budget(sid, run_id, **arguments) == receipt
        assert len(calls) == 1
        assert facts(database, run_id) == prefix
        ready = service.get_session(sid)
        assert ready["available_actions"] == ["resume", "close_execution"]
        assert ready["ref_revision"] == failed["ref_revision"]
        assert ready["head_commit_id"] == failed["head_commit_id"]
        current = bundle(database)
        old_run = before["run_record"][0]
        new_run = current["run_record"][0]
        assert new_run["initial_budget"] == old_run["initial_budget"]
        assert new_run["status"] == "failed"
        assert new_run["revision"] == old_run["revision"] + 1
        assert current["input_snapshot"] == before["input_snapshot"]
        with pytest.raises(ContractValidationError, match="[Ii]dempotency"):
            service.extend_budget(sid, run_id, **{
                **arguments, "additional_model_requests": 2,
            })
        with pytest.raises(ContractValidationError, match="revision"):
            service.extend_budget(sid, run_id, **{**arguments, "idempotency_key": "stale"})
        service.resume(sid, run_id, **control_args(service, sid, run_id, "resume"))
        service.wait_for_idle(sid)
        done = service.get_session(sid)
        assert done["error"] is None
        assert done["chains"][0]["chain_run_id"] == submitted["chain_run_id"]
        assert done["nodes"][0]["budget"]["model_requests"] == 2
        assert done["nodes"][0]["budget"]["attempts"] == 2
        assert len(bundle(database)["visible_message"]) == 2


def test_repeated_failed_resume_appends_facts_without_reusing_failure_receipt(database):
    calls = []
    broken = True

    class Model:
        def generate(self, messages, tools):
            calls.append(copy.deepcopy(messages))
            if broken:
                raise httpx.ConnectError("offline")
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "final_answer", dumps_pretty({"answer": {"text": "done"}})),
            ))

    with closing(WorkflowService(database, model_factory=lambda stage: Model())) as service:
        set_budget(service, "A", 8, 32)
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "two failed requests", "input")
        service.wait_for_idle(sid)
        run_id = service.get_session(sid)["nodes"][0]["run_id"]
        first = facts(database, run_id)
        service.resume(sid, run_id, **control_args(service, sid, run_id, "resume1"))
        service.wait_for_idle(sid)
        twice = service.get_session(sid)
        assert twice["nodes"][0]["status"] == "failed"
        assert twice["nodes"][0]["budget"]["model_requests"] == 2
        assert twice["nodes"][0]["budget"]["attempts"] == 8
        second = facts(database, run_id)
        assert second[:len(first)] == first
        broken = False
        service.resume(sid, run_id, **control_args(service, sid, run_id, "resume2"))
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        assert len(bundle(database)["chain_run"]) == 1
        assert len(bundle(database)["visible_message"]) == 2
        assert facts(database, run_id)[:len(second)] == second


@pytest.mark.parametrize("fault", ["auth", "configuration", "host"])
def test_non_model_recoverable_failures_never_gain_resume_or_extension(database, fault):
    calls = []

    class Model:
        def generate(self, messages, tools):
            calls.append(1)
            if fault == "auth":
                request = httpx.Request("POST", "https://offline.invalid/")
                raise httpx.HTTPStatusError("private", request=request,
                                            response=httpx.Response(401, request=request))
            if fault == "configuration":
                raise ValueError("adapter defect")
            import asyncio
            raise asyncio.CancelledError()

    with closing(WorkflowService(database, model_factory=lambda stage: Model())) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "not a recoverable model fault", "input")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        run_id = view["nodes"][0]["run_id"]
        assert view["available_actions"] == ["close_execution"]
        assert run_id not in service._run_checkpoints
        before = bundle(database)
        with pytest.raises(ContractValidationError, match="checkpoint"):
            service.resume(sid, run_id, **control_args(service, sid, run_id, "resume"))
        with pytest.raises(ContractValidationError, match="checkpoint"):
            service.extend_budget(sid, run_id, additional_model_requests=1,
                                  **control_args(service, sid, run_id, "extend"))
        assert len(calls) == 1
        assert bundle(database) == before


@pytest.mark.parametrize("action", ["extend", "resume"])
def test_budget_and_resume_commit_failure_dispatches_nothing(database, action):
    calls = []
    with closing(WorkflowService(database, model_factory=lambda stage: HistoryAdapter(stage, calls))) as service:
        set_budget(service, "A", 1, 4)
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "atomic budget control", "input")
        service.wait_for_idle(sid)
        run_id = service.get_session(sid)["nodes"][0]["run_id"]
        if action == "resume":
            service.extend_budget(sid, run_id, additional_model_requests=1,
                                  **control_args(service, sid, run_id, "extend-ok"))
        before = bundle(database)
        arguments = control_args(service, sid, run_id, action)

        def fail_commit(point):
            if point == "before_commit":
                raise RuntimeError("injected commit failure")

        service._fault_injector = fail_commit
        with pytest.raises(RuntimeError, match="injected"):
            if action == "extend":
                service.extend_budget(sid, run_id, additional_model_requests=1, **arguments)
            else:
                service.resume(sid, run_id, **arguments)
        service._fault_injector = None
        assert bundle(database) == before
        assert len(calls) == 1
        if action == "extend":
            service.extend_budget(sid, run_id, additional_model_requests=1, **arguments)
        else:
            service.resume(sid, run_id, **arguments)
            service.wait_for_idle(sid)
            assert service.get_session(sid)["error"] is None


def test_checkpoint_tampering_is_rejected_before_state_change_or_dispatch(database):
    calls = []
    with closing(WorkflowService(database, model_factory=lambda stage: HistoryAdapter(stage, calls))) as service:
        set_budget(service, "A", 1, 4)
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "authenticated checkpoint", "input")
        service.wait_for_idle(sid)
        run_id = service.get_session(sid)["nodes"][0]["run_id"]
        checkpoint = service._run_checkpoints[run_id]
        forged = replace(checkpoint, attempts=0)
        service._run_checkpoints[run_id] = forged
        service._execution_failures[sid].checkpoint = forged
        before = bundle(database)
        with pytest.raises(ContractValidationError, match="checkpoint"):
            service.extend_budget(sid, run_id, additional_model_requests=1,
                                  **control_args(service, sid, run_id, "extend"))
        assert bundle(database) == before
        assert len(calls) == 1


def test_restart_keeps_budget_receipt_but_never_recreates_failed_checkpoint(database):
    calls = []
    with closing(WorkflowService(database, model_factory=lambda stage: HistoryAdapter(stage, calls))) as service:
        set_budget(service, "A", 1, 4)
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "durable allowance is not live context", "input")
        service.wait_for_idle(sid)
        run_id = service.get_session(sid)["nodes"][0]["run_id"]
        arguments = control_args(service, sid, run_id, "extend")
        receipt = service.extend_budget(sid, run_id, additional_model_requests=1, **arguments)
        prefix = facts(database, run_id)
    with closing(WorkflowService(database, model_factory=lambda stage: pytest.fail("restarted"))) as reopened:
        view = reopened.get_session(sid)
        assert view["nodes"][0]["status"] == "recovery_unavailable"
        assert view["available_actions"] == ["close_execution"]
        assert view["nodes"][0]["budget"]["max_model_requests"] == 2
        assert reopened.extend_budget(sid, run_id, additional_model_requests=1, **arguments) == receipt
        assert facts(database, run_id) == prefix
        with pytest.raises(ContractValidationError, match="checkpoint"):
            reopened.resume(sid, run_id, **control_args(reopened, sid, run_id, "resume"))
        assert len(calls) == 1


@pytest.mark.parametrize("pause", [False, True])
def test_cleanup_program_fault_never_exposes_recoverable_model_or_pause_checkpoint(database, pause):
    entered, release = Event(), Event()

    class Model:
        def generate(self, messages, tools):
            if pause:
                entered.set()
                assert release.wait(10)
                return ModelResponse("stop", content="discard this late response")
            raise httpx.ConnectError("transient")

        def close(self):
            raise RuntimeError("PRIVATE cleanup fault")

    with closing(WorkflowService(database, model_factory=lambda stage: Model())) as service:
        set_budget(service, "A", 8, 32)
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "cleanup is a program boundary", "input")
        if pause:
            try:
                assert entered.wait(10)
                run_id = service.get_session(sid)["nodes"][0]["run_id"]
                service.interrupt(sid, run_id, **control_args(service, sid, run_id, "interrupt"))
            finally:
                release.set()
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        run_id = failed["nodes"][0]["run_id"]
        assert failed["nodes"][0]["status"] == "failed"
        assert failed["error"]["code"] == "adapter_close_error"
        assert failed["available_actions"] == ["close_execution"]
        assert run_id not in service._run_checkpoints
        assert "PRIVATE" not in str(failed)
        failures = [row["payload"] for row in facts(database, run_id)
                    if row["kind"] == "execution_failed"]
        assert failures[-1]["category"] == "contract"
        service.close_execution(sid, submitted["chain_run_id"], idempotency_key="close",
                                expected_session_revision=failed["revision"],
                                expected_ref_revision=failed["ref_revision"],
                                expected_head_commit_id=failed["head_commit_id"])
        assert not service.get_session(sid)["can_continue_workflow"]


def test_legacy_budget_database_reopens_without_rewriting_frozen_definitions(database):
    class LegacyWorkflow(WorkflowService):
        def _register_components(self):
            super()._register_components()
            for _, _, _, config in self._resolved.values():
                config["payload"]["kernel"].pop("max_model_attempts")

    with closing(LegacyWorkflow(database)) as legacy:
        sid = legacy.create_session()["workflow_session_id"]
        legacy.submit(sid, "legacy definition", "first")
        legacy.wait_for_idle(sid)
        before = bundle(database)
        assert all("max_model_attempts" not in row["initial_budget"] for row in before["run_record"])
    with closing(WorkflowService(database)) as current:
        reopened = bundle(database)
        assert reopened == before
        assert current.get_session(sid)["nodes"][0]["budget"]["max_model_attempts"] == 32
        current.submit(sid, "new definition defaults", "second")
        current.wait_for_idle(sid)
        after = bundle(database)
        assert after["node_binding"] == before["node_binding"]
        for run in before["run_record"]:
            assert run in after["run_record"]
        assert after["input_snapshot"][:len(before["input_snapshot"])] == before["input_snapshot"]
        assert current.get_session(sid)["nodes"][0]["budget"]["max_model_attempts"] == 32


def test_default_offline_preview_resume_uses_only_this_input_accepted_progress(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "previous completed floor", "first")
        service.wait_for_idle(sid)
        first = service.get_session(sid)
        set_budget(service, "A", 1, 4)
        submitted = service.submit(sid, "current floor", "second")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        run_id = failed["nodes"][0]["run_id"]
        assert failed["nodes"][0]["budget"]["model_requests"] == 1
        old_facts = facts(database, run_id)
        service.extend_budget(sid, run_id, additional_model_requests=1,
                              **control_args(service, sid, run_id, "extend"))
        service.resume(sid, run_id, **control_args(service, sid, run_id, "resume"))
        service.wait_for_idle(sid)
        final = service.get_session(sid)
        assert final["error"] is None
        assert final["messages"][:2] == first["messages"]
        assert final["messages"][2]["visible_message_id"] == submitted["visible_message_id"]
        assert final["messages"][3]["payload"]["text"] == "[Offline revision]\n\ncurrent floor"
        ledger = facts(database, run_id)
        assert ledger[:len(old_facts)] == old_facts
        dispatches = [row for row in ledger if row["kind"] == "tool_dispatch"]
        assert len(dispatches) == 2


def test_failure_fact_storage_fault_is_explicit_and_cannot_enter_model_continuation(database):
    class Model:
        def generate(self, messages, tools):
            raise httpx.ConnectError("transient provider fault")

    with closing(WorkflowService(database, model_factory=lambda stage: Model())) as service:
        set_budget(service, "A", 8, 32)
        original = service._on_fact

        def fail_diagnostic(run_id, generation, event):
            if event["kind"] == "execution_failed":
                raise RuntimeError("PRIVATE fact persistence failure")
            original(run_id, generation, event)

        service._on_fact = fail_diagnostic
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "storage fault is not a model observation", "input")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        run_id = failed["nodes"][0]["run_id"]
        assert failed["error"]["code"] == "fact_persistence_error"
        assert failed["available_actions"] == ["close_execution"]
        assert run_id not in service._run_checkpoints
        assert "PRIVATE" not in str(failed)
        diagnostic = next(row["payload"] for row in reversed(facts(database, run_id))
                          if row["kind"] == "execution_failed")
        assert diagnostic["code"] == "fact_persistence_error"
        assert diagnostic["category"] == "contract"
        service.close_execution(sid, submitted["chain_run_id"], idempotency_key="close",
                                expected_session_revision=failed["revision"],
                                expected_ref_revision=failed["ref_revision"],
                                expected_head_commit_id=failed["head_commit_id"])
        assert not service.get_session(sid)["can_continue_workflow"]
