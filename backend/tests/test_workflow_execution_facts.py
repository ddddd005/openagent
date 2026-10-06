"""I03 facts survive before archive and gate further external dispatch."""

from __future__ import annotations

import asyncio
import copy
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import UUID, uuid4

import pytest

from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.runtime import KernelContractError
from phase1_agent.storage import SqliteStore
from phase1_agent.tools import ToolExecutionError, ToolOutcomeUnknown
from phase1_agent.workflow import A_BINDING, OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(
        prefix="workflow-facts-", dir=Path(__file__).resolve().parent,
    ) as folder:
        yield Path(folder) / "workflow.sqlite"


def stage_run(database, session_id, binding=A_BINDING):
    with closing(SqliteStore(database)) as store:
        return next(
            row for row in store.list_records("run_record")
            if row["workflow_session_id"] == session_id
            and row["node_binding_id"] == binding
        )


def facts(database, run_id):
    with closing(SqliteStore(database)) as store:
        return store.read_execution_facts(run_id)


def by_kind(rows, kind):
    return [row for row in rows if row["kind"] == kind]


def accepted_messages(rows):
    return [row["payload"]["message"] for row in by_kind(rows, "message_accepted")]


def assert_identities(rows, run):
    assert rows
    assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
    assert len({row["fact_id"] for row in rows}) == len(rows)
    for row in rows:
        assert row["schema_version"] == 1
        for field in (
            "run_id", "chain_run_id", "workflow_session_id",
            "node_binding_id", "snapshot_id",
        ):
            assert row[field] == run[field]
        assert str(UUID(row["generation"])) == row["generation"]
        assert UUID(row["generation"]).version == 4


def call(name, arguments):
    return ModelToolCall(str(uuid4()), name, dumps_pretty(arguments))


def tool_batch(*texts):
    return ModelResponse("tool_calls", tool_calls=tuple(
        call("inspect_text", {"text": text}) for text in texts
    ))


def final_answer(text="[Offline draft]\n\nDone"):
    return ModelResponse("tool_calls", tool_calls=(
        call("final_answer", {"answer": {"text": text}}),
    ))


class BatchAdapter:
    """Fixture state comes from accepted history and is safe across resume."""

    def __init__(self, texts, requests, *, stage="A", entry=None):
        self.texts = texts
        self.requests = requests
        self.stage = stage
        self.entry = entry

    def generate(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        if self.entry is not None:
            self.entry(messages, tools)
        start = max(index for index, message in enumerate(messages) if message["role"] == "user")
        current = messages[start + 1:]
        if not any(message["role"] == "tool" for message in current):
            return tool_batch(*self.texts)
        return final_answer()


def replace_inspect(monkeypatch, implementation):
    original = WorkflowService._tools

    def tools():
        inspect, final = original()
        return replace(inspect, _implementation=implementation), final

    monkeypatch.setattr(WorkflowService, "_tools", staticmethod(tools))


def test_batch_and_dispatch_are_durable_at_real_tool_entry_before_any_turn(
    database, monkeypatch,
):
    entered, release = Event(), Event()
    model_entries, tool_entries = [], []
    session = {}

    def model_entry(messages, tools):
        run = stage_run(database, session["sid"])
        rows = facts(database, run["run_id"])
        request = by_kind(rows, "model_request")[-1]
        attempt = by_kind(rows, "model_attempt_started")[-1]
        assert request["payload"]["messages"] == messages
        assert request["payload"]["tools"] == tools
        assert attempt["payload"]["request_id"] == request["payload"]["request_id"]
        assert request["sequence"] < attempt["sequence"]
        with closing(SqliteStore(database)) as store:
            frozen = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
            assert request["payload"]["model_parameters"] == frozen["model_parameters"]
        model_entries.append(request)

    def inspect(text):
        if text == "first":
            run = stage_run(database, session["sid"])
            rows = facts(database, run["run_id"])
            accepted = by_kind(rows, "message_accepted")[-1]
            batch = accepted["payload"]["message"]
            calls = [block for block in batch["blocks"] if block["kind"] == "tool_call"]
            assert [block["parsed_arguments"] for block in calls] == [
                {"text": "first"}, {"text": "second"}, {"text": "third"},
            ]
            assert [loads_strict(block["raw_arguments"]) for block in calls] == [
                {"text": "first"}, {"text": "second"}, {"text": "third"},
            ]
            dispatch = by_kind(rows, "tool_dispatch")
            assert len(dispatch) == 1
            assert dispatch[0]["payload"]["tool_call_id"] == calls[0]["tool_call_id"]
            assert dispatch[0]["payload"]["tool_execution_id"] is not None
            assert accepted["sequence"] < dispatch[0]["sequence"]
            assert_identities(rows, run)
            with closing(SqliteStore(database)) as store:
                assert store.list_records("turn") == []
                assert store.list_records("workflow_candidate") == []
            tool_entries.append(copy.deepcopy(rows))
            entered.set()
            assert release.wait(20)
        return {"characters": len(text), "lines": 1}

    replace_inspect(monkeypatch, inspect)

    def factory(stage):
        if stage == "A":
            return BatchAdapter(("first", "second", "third"), [], entry=model_entry)
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        session["sid"] = service.create_session()["workflow_session_id"]
        try:
            submitted = service.submit(session["sid"], "Preserve the whole accepted batch", "once")
            assert entered.wait(10)
            run = stage_run(database, session["sid"])
            while_running = facts(database, run["run_id"])
            assert while_running == tool_entries[0]
            assert by_kind(while_running, "model_attempt_finished")[0]["payload"]["usage"] is None
            assert "execution_facts" not in service.get_session(session["sid"])
        finally:
            release.set()
        service.wait_for_idle(session["sid"])
        view = service.get_session(session["sid"])
        assert view["error"] is None
        assert view["chains"] == [{
            "chain_run_id": submitted["chain_run_id"], "status": "succeeded",
        }]
        rows = facts(database, run["run_id"])
        assert rows[:len(while_running)] == while_running
        assert len(model_entries) == 2
        with closing(SqliteStore(database)) as store:
            bundle = validate_bundle(store.read_bundle())
            turn = next(row for row in bundle["turn"] if row["run_id"] == run["run_id"])
            assert accepted_messages(rows) == turn["messages"]
            assert len(bundle["turn"]) == 2
            assert len(bundle["workflow_candidate"]) == 1
            assert len(bundle["visible_message"]) == 2


def test_unknown_and_never_started_are_saved_before_same_run_model_continues(
    database, monkeypatch,
):
    executed, requests, inspected = [], [], []
    session = {}

    def inspect(text):
        executed.append(text)
        if text == "uncertain":
            raise ToolOutcomeUnknown("PRIVATE-UNKNOWN-DIAGNOSTIC")
        return {"text": text}

    def model_entry(messages, tools):
        if not any(message["source"]["kind"] == "runtime_tool_observation" for message in messages):
            return
        run = stage_run(database, session["sid"])
        rows = facts(database, run["run_id"])
        settled = by_kind(rows, "tool_settled")
        assert [row["payload"]["outcome"] for row in settled] == [
            "success", "outcome_unknown", "never_started", "never_started",
        ]
        assert [row["payload"]["tool_execution_id"] is None for row in settled] == [
            False, False, True, True,
        ]
        assert len(by_kind(rows, "tool_dispatch")) == 2
        for row in settled:
            message = row["payload"]["message"]
            assert message in accepted_messages(rows)
            accepted = next(
                fact for fact in by_kind(rows, "message_accepted")
                if fact["payload"]["message"] == message
            )
            assert row["sequence"] < accepted["sequence"]
        assert "PRIVATE-UNKNOWN-DIAGNOSTIC" not in str(rows)
        inspected.append(rows)

    replace_inspect(monkeypatch, inspect)

    def factory(stage):
        if stage == "A":
            return BatchAdapter(
                ("known", "uncertain", "skipped-one", "skipped-two"),
                requests, entry=model_entry,
            )
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = session["sid"] = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "Decide from explicit observations", "once")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"] is None
        assert inspected
        assert executed[:2] == ["known", "uncertain"]
        assert not set(executed) & {"skipped-one", "skipped-two"}
        run = stage_run(database, sid)
        assert run["chain_run_id"] == submitted["chain_run_id"]
        rows = facts(database, run["run_id"])
        assert len({row["generation"] for row in rows}) == 1
        assert not by_kind(rows, "execution_failed")
        assert len(by_kind(rows, "model_request")) == len(requests) == 2
        with closing(SqliteStore(database)) as store:
            turn = store.get_record("turn", {"turn_id": run["result_turn_id"]})
            assert accepted_messages(rows) == turn["messages"]


def test_pause_resume_appends_new_generation_without_reaccepting_or_reexecuting(
    database, monkeypatch,
):
    entered, release = Event(), Event()
    executed, requests = [], []

    def inspect(text):
        executed.append(text)
        if text == "first":
            entered.set()
            assert release.wait(20)
        return {"text": text}

    replace_inspect(monkeypatch, inspect)

    def factory(stage):
        return BatchAdapter(("first", "second"), requests) if stage == "A" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            submitted = service.submit(sid, "Pause after saving first result", "once")
            assert entered.wait(10)
            before = service.get_session(sid)
            run_id = before["nodes"][0]["run_id"]
            receipt = service.interrupt(
                sid, run_id, idempotency_key="pause",
                expected_session_revision=before["revision"],
                expected_run_revision=before["nodes"][0]["revision"],
            )
            assert receipt["status"] == "pausing"
        finally:
            release.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["nodes"][0]["status"] == "paused"
        prefix = facts(database, run_id)
        assert len(by_kind(prefix, "tool_dispatch")) == 1
        assert len(by_kind(prefix, "tool_settled")) == 1
        assert len(accepted_messages(prefix)) == 2
        assert executed == ["first"]
        service.resume(
            sid, run_id, idempotency_key="resume",
            expected_session_revision=paused["revision"],
            expected_run_revision=paused["nodes"][0]["revision"],
        )
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert completed["nodes"][0]["run_id"] == run_id
        assert completed["chains"] == [{
            "chain_run_id": submitted["chain_run_id"], "status": "succeeded",
        }]
        rows = facts(database, run_id)
        assert rows[:len(prefix)] == prefix
        assert len({row["generation"] for row in rows}) == 2
        assert_identities(rows, stage_run(database, sid))
        assert executed.count("first") == executed.count("second") == 1
        messages = accepted_messages(rows)
        assert len({message["message_id"] for message in messages}) == len(messages)
        executions = [
            row["payload"]["tool_execution_id"] for row in by_kind(rows, "tool_dispatch")
        ]
        assert len(executions) == len(set(executions)) == 3
        with closing(SqliteStore(database)) as store:
            turn = store.get_record(
                "turn", {"turn_id": stage_run(database, sid)["result_turn_id"]},
            )
            assert messages == turn["messages"]


def test_retry_uses_one_request_distinct_attempts_and_preserves_unknown_usage(database):
    wire_calls = []
    usage = {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}

    class RetryAdapter:
        def generate(self, messages, tools):
            wire_calls.append((copy.deepcopy(messages), copy.deepcopy(tools)))
            if len(wire_calls) == 1:
                raise TimeoutError("PRIVATE-TRANSPORT-DIAGNOSTIC")
            return replace(
                final_answer(), usage=copy.deepcopy(usage),
                response_id="provider-response", model="offline-usage",
            )

    def factory(stage):
        return RetryAdapter() if stage == "A" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Preserve request and physical attempt identity", "once")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        rows = facts(database, stage_run(database, sid)["run_id"])
        requests = by_kind(rows, "model_request")
        started = by_kind(rows, "model_attempt_started")
        finished = by_kind(rows, "model_attempt_finished")
        assert len(requests) == 1
        assert len(started) == len(finished) == 2
        assert wire_calls[0] == wire_calls[1]
        assert {row["payload"]["request_id"] for row in started + finished} == {
            requests[0]["payload"]["request_id"],
        }
        assert [row["payload"]["retry_index"] for row in started] == [0, 1]
        assert [row["payload"]["attempt_index"] for row in started] == [1, 2]
        assert [row["payload"]["request_index"] for row in started] == [1, 1]
        assert len({row["payload"]["attempt_id"] for row in started}) == 2
        assert [row["payload"]["attempt_id"] for row in finished] == [
            row["payload"]["attempt_id"] for row in started
        ]
        assert [row["payload"]["outcome"] for row in finished] == ["model_error", "responded"]
        assert finished[0]["payload"]["usage"] is None
        assert finished[1]["payload"]["usage"] == usage
        assert finished[1]["payload"]["response_id"] == "provider-response"
        assert finished[1]["payload"]["model"] == "offline-usage"
        assert "PRIVATE-TRANSPORT-DIAGNOSTIC" not in str(rows)


@pytest.mark.parametrize("origin", ["tool", "adapter", "host"])
def test_failed_tail_is_durable_without_fabricated_observation_or_restart_dispatch(
    database, monkeypatch, origin,
):
    entered = []
    private = "PRIVATE-FAILURE-DIAGNOSTIC"

    def inspect(text):
        entered.append(text)
        if origin == "host":
            raise asyncio.CancelledError(private)
        raise ValueError(private)

    class BrokenAdapter:
        def generate(self, messages, tools):
            raise ValueError(private)

    if origin != "adapter":
        replace_inspect(monkeypatch, inspect)

    def factory(stage):
        if stage == "A":
            return BrokenAdapter() if origin == "adapter" else BatchAdapter(("effect", "skip"), [])
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Do not manufacture a definite tool result", "once")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"]["code"] == {
            "tool": "tool_contract_error", "adapter": "adapter_contract_error",
            "host": "execution_interrupted",
        }[origin]
        assert "secondary_code" not in view["error"]
        run = stage_run(database, sid)
        rows = facts(database, run["run_id"])
        failures = by_kind(rows, "execution_failed")
        assert len(failures) == 1
        assert failures[0]["payload"]["code"] == view["error"]["code"]
        assert failures[0]["payload"]["category"] == (
            "interrupted" if origin == "host" else "contract"
        )
        assert not any(message["role"] == "tool" for message in accepted_messages(rows))
        assert private not in str(rows) + str(view)
        if origin != "adapter":
            assert entered == ["effect"]
            settled = by_kind(rows, "tool_settled")
            assert len(settled) == 1
            assert settled[0]["payload"]["outcome"] == "unknown"
            assert settled[0]["payload"]["message"] is None
            assert len(by_kind(rows, "tool_dispatch")) == 1
        else:
            assert not by_kind(rows, "tool_dispatch")
            finished = by_kind(rows, "model_attempt_finished")
            assert finished[-1]["payload"]["outcome"] == "adapter_contract_error"
        with closing(SqliteStore(database)) as store:
            assert store.list_records("turn") == []
            assert store.list_records("workflow_candidate") == []
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"Restart dispatched {stage}"),
    )) as reopened:
        assert reopened.get_session(sid)["available_actions"] == ["close_execution"]
        assert facts(database, run["run_id"]) == rows
        assert_identities(rows, run)
        assert [message["role"] for message in reopened.get_session(sid)["messages"]] == ["user"]


@pytest.mark.parametrize("blocked_kind", [
    "model_request", "model_attempt_started", "message_accepted", "tool_dispatch",
])
def test_failed_fact_write_prevents_the_next_external_entry(
    database, monkeypatch, blocked_kind,
):
    model_calls, tool_calls = [], []
    original_append = SqliteStore.append_execution_fact

    def failing_append(store, fact, expected_sequence):
        if fact["kind"] == blocked_kind:
            raise RuntimeError("PRIVATE-FACT-WRITE-DIAGNOSTIC")
        return original_append(store, fact, expected_sequence=expected_sequence)

    class TrackedAdapter:
        def generate(self, messages, tools):
            model_calls.append("A")
            return tool_batch("first", "second")

    monkeypatch.setattr(SqliteStore, "append_execution_fact", failing_append)
    replace_inspect(monkeypatch, lambda text: tool_calls.append(text) or {"text": text})

    def factory(stage):
        return TrackedAdapter() if stage == "A" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Do not dispatch without durable facts", "once")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"]["code"] == "fact_persistence_error"
        assert model_calls == ([] if blocked_kind.startswith("model_") else ["A"])
        assert tool_calls == []
        assert "PRIVATE-FACT-WRITE-DIAGNOSTIC" not in str(view)
        assert isinstance(service._execution_failures[sid], KernelContractError)
        rows = facts(database, stage_run(database, sid)["run_id"])
        assert not by_kind(rows, "tool_dispatch")
        assert not by_kind(rows, blocked_kind)
        assert not any(message["role"] == "tool" for message in accepted_messages(rows))
        assert "PRIVATE-FACT-WRITE-DIAGNOSTIC" not in str(rows)
        with closing(SqliteStore(database)) as store:
            assert store.list_records("turn") == []
            assert store.list_records("workflow_candidate") == []


def test_settle_write_failure_preserves_dispatch_but_cannot_replay_or_continue(
    database, monkeypatch,
):
    model_calls, tool_calls = [], []
    original_append = SqliteStore.append_execution_fact

    def failing_append(store, fact, expected_sequence):
        if fact["kind"] == "tool_settled":
            raise RuntimeError("PRIVATE-SETTLE-DIAGNOSTIC")
        return original_append(store, fact, expected_sequence=expected_sequence)

    monkeypatch.setattr(SqliteStore, "append_execution_fact", failing_append)
    replace_inspect(monkeypatch, lambda text: tool_calls.append(text) or {"text": text})

    def factory(stage):
        return BatchAdapter(("first", "second"), model_calls) if stage == "A" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Effects cannot be erased by a ledger write failure", "once")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"]["code"] == "fact_persistence_error"
        assert len(model_calls) == 1 and tool_calls == ["first"]
        run = stage_run(database, sid)
        rows = facts(database, run["run_id"])
        assert len(by_kind(rows, "tool_dispatch")) == 1
        assert by_kind(rows, "tool_settled") == []
        assert not any(message["role"] == "tool" for message in accepted_messages(rows))
        assert "PRIVATE-SETTLE-DIAGNOSTIC" not in str(rows) + str(view)
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"Restart dispatched {stage}"),
    )) as reopened:
        assert reopened.get_session(sid)["available_actions"] == ["close_execution"]
        assert facts(database, run["run_id"]) == rows
        assert tool_calls == ["first"]


def test_cooperative_pause_still_preserves_usage_of_unaccepted_model_response(database):
    entered, release = Event(), Event()
    usage = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    requests = []
    first = True

    class BlockingFinalAdapter:
        def generate(self, messages, tools):
            nonlocal first
            requests.append(copy.deepcopy(messages))
            if first:
                first = False
                entered.set()
                assert release.wait(20)
            return replace(final_answer(), usage=copy.deepcopy(usage))

    def factory(stage):
        return BlockingFinalAdapter() if stage == "A" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        try:
            service.submit(sid, "Account for an unaccepted late response", "once")
            assert entered.wait(10)
            before = service.get_session(sid)
            run_id = before["nodes"][0]["run_id"]
            service.interrupt(
                sid, run_id, idempotency_key="pause",
                expected_session_revision=before["revision"],
                expected_run_revision=before["nodes"][0]["revision"],
            )
        finally:
            release.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["nodes"][0]["status"] == "paused"
        prefix = facts(database, run_id)
        assert by_kind(prefix, "model_attempt_finished")[-1]["payload"]["usage"] == usage
        assert accepted_messages(prefix) == []
        assert by_kind(prefix, "tool_dispatch") == []
        service.resume(
            sid, run_id, idempotency_key="resume",
            expected_session_revision=paused["revision"],
            expected_run_revision=paused["nodes"][0]["revision"],
        )
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        rows = facts(database, run_id)
        assert rows[:len(prefix)] == prefix
        assert [row["payload"]["request_index"] for row in by_kind(rows, "model_request")] == [1, 2]
        assert len({row["payload"]["request_id"] for row in by_kind(rows, "model_request")}) == 2
        assert [row["payload"]["usage"] for row in by_kind(rows, "model_attempt_finished")] == [
            usage, usage,
        ]
        assert len(requests) == 2


def test_known_tool_error_is_a_durable_result_not_execution_failure(database, monkeypatch):
    calls = []

    def inspect(text):
        calls.append(text)
        if text == "known-error":
            raise ToolExecutionError("PRIVATE-KNOWN-TOOL-ERROR")
        return {"text": text}

    replace_inspect(monkeypatch, inspect)

    def factory(stage):
        return BatchAdapter(("known-error", "next"), []) if stage == "A" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Known failure is not unknown", "once")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        run = stage_run(database, sid)
        rows = facts(database, run["run_id"])
        settled = by_kind(rows, "tool_settled")
        assert [row["payload"]["outcome"] for row in settled] == ["error", "success", "success"]
        assert settled[0]["payload"]["message"]["source"]["kind"] == "tool"
        assert calls[:2] == ["known-error", "next"]
        assert not by_kind(rows, "execution_failed")
        assert "PRIVATE-KNOWN-TOOL-ERROR" not in str(rows)
        with closing(SqliteStore(database)) as store:
            turn = store.get_record("turn", {"turn_id": run["result_turn_id"]})
            assert accepted_messages(rows) == turn["messages"]


@pytest.mark.parametrize("origin", ["adapter", "tool"])
@pytest.mark.parametrize("recorder_fails", [False, True])
def test_host_termination_is_not_swallowed_when_its_last_fact_cannot_be_saved(
    database, monkeypatch, origin, recorder_fails,
):
    terminated = SystemExit("PRIVATE-HOST-TERMINATION")
    original_append = SqliteStore.append_execution_fact
    blocked_kind = "model_attempt_finished" if origin == "adapter" else "tool_settled"
    calls = []

    def append(store, fact, expected_sequence):
        if recorder_fails and fact["kind"] == blocked_kind:
            raise RuntimeError("PRIVATE-LAST-FACT-DIAGNOSTIC")
        return original_append(store, fact, expected_sequence=expected_sequence)

    class TerminatingAdapter:
        def generate(self, messages, tools):
            calls.append("adapter")
            raise terminated

    def inspect(text):
        calls.append("tool")
        raise terminated

    monkeypatch.setattr(SqliteStore, "append_execution_fact", append)
    if origin == "tool":
        replace_inspect(monkeypatch, inspect)

    def factory(stage):
        if stage == "A":
            return TerminatingAdapter() if origin == "adapter" else BatchAdapter(("effect",), [])
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Host termination must remain host termination", "once")
        with pytest.raises(SystemExit) as caught:
            service.wait_for_idle(sid)
        assert caught.value is terminated
        assert calls == [origin]
        rows = facts(database, stage_run(database, sid)["run_id"])
        tail = by_kind(rows, blocked_kind)
        assert len(tail) == (0 if recorder_fails else 1)
        if tail:
            assert tail[0]["payload"]["outcome"] == (
                "host_interrupted" if origin == "adapter" else "unknown"
            )
        assert "PRIVATE-HOST-TERMINATION" not in str(rows)
        assert "PRIVATE-LAST-FACT-DIAGNOSTIC" not in str(rows)
        assert not any(message["role"] == "tool" for message in accepted_messages(rows))


@pytest.mark.parametrize("origin", ["tool", "adapter", "host"])
def test_secondary_failure_fact_error_does_not_replace_the_primary_cause(
    database, monkeypatch, origin,
):
    cause = (
        asyncio.CancelledError("PRIVATE-PRIMARY-FAILURE")
        if origin == "host" else ValueError("PRIVATE-PRIMARY-FAILURE")
    )
    original_append = SqliteStore.append_execution_fact

    def append(store, fact, expected_sequence):
        if fact["kind"] == "execution_failed":
            raise RuntimeError("PRIVATE-SECONDARY-FAILURE")
        return original_append(store, fact, expected_sequence=expected_sequence)

    class BrokenAdapter:
        def generate(self, messages, tools):
            raise cause

    def inspect(text):
        raise cause

    monkeypatch.setattr(SqliteStore, "append_execution_fact", append)
    if origin != "adapter":
        replace_inspect(monkeypatch, inspect)

    def factory(stage):
        if stage == "A":
            return BrokenAdapter() if origin == "adapter" else BatchAdapter(("effect",), [])
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Keep the primary exception and known facts", "once")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"]["code"] == {
            "tool": "tool_contract_error", "adapter": "adapter_contract_error",
            "host": "execution_interrupted",
        }[origin]
        assert service._execution_failures[sid].__cause__ is cause
        if origin == "host":
            assert view["error"]["secondary_code"] == "fact_persistence_error"
        assert any("fact_persistence_error" in note for note in cause.__notes__)
        assert "PRIVATE-SECONDARY-FAILURE" not in str(cause.__notes__)
        rows = facts(database, stage_run(database, sid)["run_id"])
        assert by_kind(rows, "execution_failed") == []
        assert "PRIVATE-PRIMARY-FAILURE" not in str(rows) + str(view)
        assert "PRIVATE-SECONDARY-FAILURE" not in str(rows) + str(view)
        assert not any(message["role"] == "tool" for message in accepted_messages(rows))
