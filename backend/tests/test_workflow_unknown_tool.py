"""Uncertain observations continue; host and program faults do not masquerade as them."""

import asyncio
import copy
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.active_run import ToolProgress
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.runtime import KernelContractError, KernelPauseRequested
from phase1_agent.storage import SqliteStore
from phase1_agent.tools import ToolOutcomeUnknown
from phase1_agent.workflow import A_BINDING, OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="tool-observation-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder) / "workflow.sqlite"


class ObservationAdapter:
    """Choose a new call only after inspecting the complete accepted batch."""

    def __init__(self, stage, observed, *, verify=False):
        self.stage = stage
        self.observed = observed
        self.verify = verify

    def generate(self, messages, tools):
        start = max(index for index, message in enumerate(messages) if message["role"] == "user")
        current = messages[start + 1:]
        results = [message for message in current if message["role"] == "tool"]
        if not results:
            return ModelResponse("tool_calls", tool_calls=tuple(
                ModelToolCall(str(uuid4()), "inspect_text", dumps_pretty({"text": text}))
                for text in ("known", "uncertain", "skipped-one", "skipped-two")
            ))
        self.observed.append(copy.deepcopy(current))
        if self.verify and len(results) == 4:
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "inspect_text", dumps_pretty({"text": "verify"})),
            ))
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall(str(uuid4()), "final_answer", dumps_pretty({
                "answer": {"text": "[Offline draft]\n\nUncertain effects remain explicitly recorded."},
            })),
        ))


def observation_tools(monkeypatch, calls, *, reason_code="outcome_unknown", block=None):
    original_tools = WorkflowService._tools

    def replacement_tools():
        inspect, final = original_tools()
        original_inspect = inspect._implementation

        def controlled_inspect(text):
            calls.append(text)
            if text == "uncertain":
                if block is not None:
                    started, release = block
                    started.set()
                    assert release.wait(20)
                raise ToolOutcomeUnknown("untrusted provider detail", reason_code=reason_code)
            return original_inspect(text)

        return replace(inspect, _implementation=controlled_inspect), final

    monkeypatch.setattr(WorkflowService, "_tools", staticmethod(replacement_tools))


def assert_closed_observations(messages, reason_code="outcome_unknown"):
    calls = [block for block in messages[0]["blocks"] if block["kind"] == "tool_call"]
    results = [message for message in messages if message["role"] == "tool"]
    assert [message["blocks"][0]["tool_call_id"] for message in results[:4]] == [
        call["tool_call_id"] for call in calls
    ]
    assert results[0]["source"]["kind"] == "tool"
    assert [message["source"]["reason_code"] for message in results[1:4]] == [
        reason_code, "never_started", "never_started",
    ]
    assert results[1]["source"]["tool_execution_id"] is not None
    assert all(message["source"]["tool_execution_id"] is None for message in results[2:4])
    for message in results[1:4]:
        source = message["source"]
        block = message["blocks"][0]
        assert source["kind"] == "runtime_tool_observation"
        assert block["tool_execution_id"] == source["tool_execution_id"]
        assert block["content"]["reason_code"] == source["reason_code"]
        assert "idempotent" in block["model_visible_text"]
        assert "verify external state" in block["model_visible_text"]
        assert "ask the user" in block["model_visible_text"]
        assert "untrusted provider detail" not in block["model_visible_text"]


@pytest.mark.parametrize("reason_code", ["outcome_unknown", "interrupted"])
@pytest.mark.parametrize("verify", [False, True])
def test_explicit_unknown_closes_batch_and_model_continues_same_run(
    database, monkeypatch, reason_code, verify,
):
    calls, observed = [], []
    observation_tools(monkeypatch, calls, reason_code=reason_code)

    def factory(stage):
        return ObservationAdapter(stage, observed, verify=verify) if stage == "A" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "Observe uncertain effects", "once")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"] is None
        assert view["unresolved_tool_executions"] == []
        assert view["chains"] == [{
            "chain_run_id": submitted["chain_run_id"], "status": "succeeded",
        }]
        assert [row["role"] for row in view["messages"]] == ["user", "assistant"]
        assert_closed_observations(observed[0], reason_code)
        assert calls[:2] == ["known", "uncertain"]
        assert calls[2:-1] == (["verify"] if verify else [])
        assert not set(calls) & {"skipped-one", "skipped-two"}
        with closing(SqliteStore(database)) as store:
            bundle = validate_bundle(store.read_bundle())
            assert len(bundle["chain_run"]) == 1
            assert len(bundle["turn"]) == 2
            assert len(bundle["workflow_candidate"]) == 1
            a_run = next(row for row in bundle["run_record"] if row["node_binding_id"] == A_BINDING)
            assert a_run["chain_run_id"] == submitted["chain_run_id"]
            a_turn = next(row for row in bundle["turn"] if row["run_id"] == a_run["run_id"])
            assert_closed_observations(a_turn["messages"], reason_code)
            assert a_run["source_run_id"] is None
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"redispatched {stage}"),
    )) as reopened:
        assert reopened.get_session(sid)["messages"] == view["messages"]
        assert reopened.get_session(sid)["error"] is None


def test_explicit_unknown_finishes_batch_before_pause_and_resume_never_dispatches_skipped(
    database, monkeypatch,
):
    started, release = Event(), Event()
    calls, observed = [], []
    observation_tools(monkeypatch, calls, block=(started, release))

    def factory(stage):
        return ObservationAdapter(stage, observed, verify=True) if stage == "A" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submitted = service.submit(sid, "Pause after uncertain observation", "once")
            assert started.wait(10)
            running = service.get_session(sid)
            run = running["nodes"][0]
            receipt = service.interrupt(
                sid, run["run_id"], idempotency_key="pause",
                expected_session_revision=running["revision"], expected_run_revision=run["revision"],
            )
            assert receipt["status"] == "pausing"
        finally:
            release.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["error"] is None
        assert paused["nodes"][0]["status"] == "paused"
        assert paused["nodes"][0]["run_id"] == run["run_id"]
        assert paused["chains"] == [{
            "chain_run_id": submitted["chain_run_id"], "status": "paused",
        }]
        active = service._active_runs[run["run_id"]].snapshot()
        assert [tool.status for tool in active.tools] == [
            "success", "outcome_unknown", "never_started", "never_started",
        ]
        assert active.pending_tools == ()
        checkpoint = service._run_checkpoints[run["run_id"]]
        assert checkpoint.pending_tools == ()
        assert_closed_observations(list(checkpoint.messages))
        assert calls == ["known", "uncertain"] and observed == []
        service.resume(
            sid, run["run_id"], idempotency_key="resume",
            expected_session_revision=paused["revision"],
            expected_run_revision=paused["nodes"][0]["revision"],
        )
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert completed["nodes"][0]["run_id"] == run["run_id"]
        assert completed["chains"] == [{
            "chain_run_id": submitted["chain_run_id"], "status": "succeeded",
        }]
        assert calls[:3] == ["known", "uncertain", "verify"]
        assert not set(calls) & {"skipped-one", "skipped-two"}
        with closing(SqliteStore(database)) as store:
            assert len(validate_bundle(store.read_bundle())["turn"]) == 2


def test_next_input_projects_archived_observations_without_rewriting_previous_turn(
    database, monkeypatch,
):
    calls, observed = [], []
    observation_tools(monkeypatch, calls)

    def factory(stage):
        return ObservationAdapter(stage, observed) if stage == "A" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        first = service.submit(sid, "First input", "first")
        service.wait_for_idle(sid)
        with closing(SqliteStore(database)) as store:
            first_run = next(
                row for row in store.list_records("run_record")
                if row["node_binding_id"] == A_BINDING
                and row["chain_run_id"] == first["chain_run_id"]
            )
            original_turn = store.get_record("turn", {"turn_id": first_run["result_turn_id"]})
        second = service.submit(sid, "Second input", "second")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        assert [message["role"] for message in service.get_session(sid)["messages"]] == [
            "user", "assistant", "user", "assistant",
        ]
        with closing(SqliteStore(database)) as store:
            bundle = validate_bundle(store.read_bundle())
            assert store.get_record("turn", {"turn_id": original_turn["turn_id"]}) == original_turn
            second_run = next(
                row for row in bundle["run_record"]
                if row["node_binding_id"] == A_BINDING
                and row["chain_run_id"] == second["chain_run_id"]
            )
            snapshot = store.get_record("input_snapshot", {"snapshot_id": second_run["snapshot_id"]})
            archived_observations = [
                message for message in original_turn["messages"]
                if message["source"]["kind"] == "runtime_tool_observation"
            ]
            projected_observations = [
                message for message in snapshot["s0"]
                if message["source"]["kind"] == "runtime_tool_observation"
            ]
            assert archived_observations == projected_observations
            assert snapshot["parent_turn_id"] == original_turn["turn_id"]
            assert len(bundle["turn"]) == 4
        assert calls.count("known") == calls.count("uncertain") == 2
        assert not set(calls) & {"skipped-one", "skipped-two"}


def test_host_cancellation_keeps_in_process_identity_but_never_retries(monkeypatch):
    calls = []
    original_tools = WorkflowService._tools

    def uncertain_tools():
        inspect, final = original_tools()

        def interrupted_inspect(text):
            calls.append(text)
            raise asyncio.CancelledError()

        return replace(inspect, _implementation=interrupted_inspect), final

    monkeypatch.setattr(WorkflowService, "_tools", staticmethod(uncertain_tools))
    with TemporaryDirectory(prefix="unknown-tool-", dir=Path(__file__).resolve().parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database, model_factory=OfflineAdapter)) as service:
            sid = service.create_session()["workflow_session_id"]
            submission = service.submit(sid, "An external effect may have happened", "once")
            service.wait_for_idle(sid)
            view = service.get_session(sid)
            assert view["error"]["code"] == "execution_interrupted"
            assert "host interrupted execution" in view["error"]["message"]
            assert view["available_actions"] == ["close_execution"]
            assert [item["role"] for item in view["messages"]] == ["user"]
            assert view["chains"] == [{
                "chain_run_id": submission["chain_run_id"], "status": "failed",
            }]
            assert len(view["unresolved_tool_executions"]) == 1
            execution = view["unresolved_tool_executions"][0]
            assert execution["tool_call_id"] != execution["tool_execution_id"]
            assert calls == ["An external effect may have happened"]
            assert isinstance(service._execution_failures[sid].__cause__, asyncio.CancelledError)
            run = view["nodes"][0]
            with pytest.raises(ContractValidationError):
                service.resume(
                    sid, run["run_id"], idempotency_key="unsafe-resume",
                    expected_session_revision=view["revision"],
                    expected_run_revision=run["revision"],
                )
            with closing(SqliteStore(database)) as store:
                assert store.list_records("turn") == []
                assert store.list_records("workflow_candidate") == []
        with closing(WorkflowService(
            database, model_factory=lambda stage: pytest.fail(f"redispatched {stage}"),
        )) as reopened:
            view = reopened.get_session(sid)
            assert view["available_actions"] == ["close_execution"]
            assert view["unresolved_tool_executions"] == []
            assert calls == ["An external effect may have happened"]


@pytest.mark.parametrize("origin", ["tool", "adapter", "callback"])
def test_program_fault_retains_typed_cause_without_fabricated_observations(
    database, monkeypatch, origin,
):
    cause = ValueError("private implementation diagnostic")
    original_tools = WorkflowService._tools

    def broken_tools():
        inspect, final = original_tools()

        def broken_inspect(text):
            raise cause

        return replace(inspect, _implementation=broken_inspect), final

    class BrokenAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            raise cause

    if origin == "tool":
        monkeypatch.setattr(WorkflowService, "_tools", staticmethod(broken_tools))
    elif origin == "callback":
        original_event = WorkflowService._run_tool_event

        def broken_event(service, run_id, generation, kind, call_id, execution_id, outcome):
            if kind == "queue":
                raise cause
            return original_event(service, run_id, generation, kind, call_id, execution_id, outcome)

        monkeypatch.setattr(WorkflowService, "_run_tool_event", broken_event)
    factory = BrokenAdapter if origin == "adapter" else OfflineAdapter
    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Do not hide program faults", "once")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"]["code"] == {
            "tool": "tool_contract_error", "adapter": "adapter_contract_error",
            "callback": "control_error",
        }[origin]
        assert "program contract was violated" in view["error"]["message"]
        assert "private implementation diagnostic" not in str(view)
        failure = service._execution_failures[sid]
        assert isinstance(failure, KernelContractError)
        assert failure.__cause__ is cause
        assert not any(message["role"] == "tool" for message in failure.messages)
        assert [row["role"] for row in view["messages"]] == ["user"]
        with closing(SqliteStore(database)) as store:
            assert store.list_records("turn") == []
            assert store.list_records("workflow_candidate") == []
    assert service._execution_failures == {}


def test_new_execution_clears_previous_in_process_diagnostic(database):
    started, release = Event(), Event()

    class BlockingAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            if self.stage == "A" and self.requests == 0:
                started.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with closing(WorkflowService(database, model_factory=BlockingAdapter)) as service:
        sid = service.create_session()["workflow_session_id"]
        service._execution_failures[sid] = RuntimeError("previous worker")
        try:
            service.submit(sid, "A new execution owns its diagnostic", "once")
            assert started.wait(10)
            assert sid not in service._execution_failures
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        assert service._execution_failures == {}


def test_service_callback_shape_execution_identity_and_order_are_strict(database, monkeypatch):
    call_one, call_two, execution_one, execution_two = [str(uuid4()) for _ in range(4)]
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"fixture dispatched {stage}"),
    )) as service:
        scheduled = Future()
        monkeypatch.setattr(service._executor, "submit", lambda *args, **kwargs: scheduled)
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "Validate real callback ownership without dispatch", "once")
        scheduled.set_result(False)
        with closing(SqliteStore(database)) as store:
            chain = store.get_record("chain_run", {"chain_run_id": submitted["chain_run_id"]})
            run_id = chain["node_run_ids"][0]
            run = store.get_record("run_record", {"run_id": run_id})
            run.update(status="running", revision=run["revision"] + 1)
            chain["status"] = "running"
            service._save(store, sid, [("run_record", run), ("chain_run", chain)], "start:" + run_id)
        active = service._active_runs[run_id]
        generation = active.snapshot().generation

        def event(kind, call_id=call_one, execution_id=None, outcome=None):
            service._run_tool_event(run_id, generation, kind, call_id, execution_id, outcome)

        for args in (
            ("queue", call_one, execution_one, None),
            ("queue", call_one, None, "success"),
            ("unrecognized", call_one, None, None),
        ):
            with pytest.raises(ContractValidationError):
                event(*args)
        event("queue")
        event("queue", call_two)
        for args in (
            ("start", call_one, execution_one, "success"),
            ("start", call_two, execution_two, None),
            ("skip", call_two, None, "never_started"),
            ("settle", call_one, execution_one, "success"),
        ):
            with pytest.raises(ContractValidationError):
                event(*args)
        event("start", call_one, execution_one)
        for args in (
            ("settle", call_one, execution_two, "success"),
            ("settle", call_one, None, "success"),
            ("skip", call_one, None, "never_started"),
        ):
            with pytest.raises(ContractValidationError):
                event(*args)
        service._interrupting.add(run_id)
        with pytest.raises(KernelPauseRequested):
            event("start", call_two, execution_two)
        event("settle", call_one, execution_one, "outcome_unknown")
        for args in (
            ("skip", call_two, execution_two, "never_started"),
            ("skip", call_two, None, "success"),
        ):
            with pytest.raises(ContractValidationError):
                event(*args)
        event("skip", call_two, None, "never_started")
        assert active.snapshot().tools == (
            ToolProgress(call_one, execution_one, "outcome_unknown"),
            ToolProgress(call_two, None, "never_started"),
        )
        with pytest.raises(ContractValidationError):
            event("skip", call_two, None, "never_started")
        with pytest.raises(ContractValidationError):
            event("settle", call_one, execution_one, "success")
        with pytest.raises(KernelPauseRequested):
            service._run_tool_event(run_id, str(uuid4()), "queue", str(uuid4()), None, None)
