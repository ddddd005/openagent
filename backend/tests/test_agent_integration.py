"""Agent -> delta -> explicit context save using a local Chat mock only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.agent_package import create_agent_package
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.tools import ToolExecutionError, ToolOutcomeUnknown, final_answer_tool, register_callable

from test_graph_service import create, document, edge, node
from test_model_package import source_config
from test_models_service_integration import ModelDatabaseFixture


from workflow_test_support import AgentTransportFixture, graph, output, run


def test_service_close_accepts_inflight_response_once_and_restart_cannot_redispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-close-private-secret")
    fixture = AgentTransportFixture(gate=True)
    database = tmp_path / "close-inflight.sqlite"
    service = GraphWorkflowService(database, public_model_factory=fixture.factory)
    pause_requested = Event()
    original_control = service._control_hosted_invocation

    def observe_control(chain_id, action, command_id):
        original_control(chain_id, action, command_id)
        if action == "pause":
            pause_requested.set()

    monkeypatch.setattr(service, "_control_hosted_invocation", observe_control)
    try:
        ModelDatabaseFixture.write(service, 1)
        initial = create(service, graph(service))
        sid = initial["workflow_session_id"]
        started = service.start(sid, expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()), inputs={"text": "original close request"})
        chain_id = started["active_chain_run_id"]
        assert fixture.entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as shutdown:
            closed = shutdown.submit(service.close)
            try:
                assert pause_requested.wait(5)
                assert not closed.done()
            finally:
                fixture.proceed.set()
            closed.result(timeout=10)
        assert len(fixture.calls) == fixture.closes == 1
        assert not service._runtime_hosts and not service._service_runs
    finally:
        fixture.proceed.set()
        if not service._closed:
            service.close()
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as reopened:
        current = reopened.get_session(sid)
        assert current["status"] == "recovery_unavailable"
        assert current["head_commit_id"] == initial["head_commit_id"]
        history = reopened.get_run(sid, chain_id)
        model_facts = [fact for fact in history["runtime_facts"]
                       if fact.get("kind") == "workflow.model-fact"]
        assert [fact["stage"] for fact in model_facts] == ["request", "attempt", "outcome"]
        assert model_facts[-1]["details"]["classification"] == "response_received"
        assert "offline-close-private-secret" not in json.dumps(history)
        with pytest.raises(ContractValidationError) as caught:
            reopened.control(sid, action="resume", expected_revision=current["revision"],
                             idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "recovery_unavailable"
        assert len(fixture.calls) == 1
        reopened.control(sid, action="close", expected_revision=current["revision"],
                         idempotency_key=str(uuid4()))
        assert reopened.get_session(sid)["status"] == "closed"
        assert len(fixture.calls) == fixture.closes == 1


def test_agent_pause_while_mock_model_inflight_retains_response_and_resumes_without_redispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-agent-private-secret")
    fixture = AgentTransportFixture(gate=True)
    with closing(GraphWorkflowService(tmp_path / "agent-paused.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        initial = create(service, graph(service))
        sid = initial["workflow_session_id"]
        started = service.start(sid, expected_revision=initial["revision"], idempotency_key=str(uuid4()),
                                inputs={"text": "frozen question"})
        chain_id = started["active_chain_run_id"]
        try:
            assert fixture.entered.wait(5)
            current = service.get_session(sid)
            requested = service.control(sid, action="pause", expected_revision=current["revision"],
                                        idempotency_key=str(uuid4()))
            assert requested["status"] == "running"
        finally:
            fixture.proceed.set()
        service.wait(chain_id)
        paused = service.get_session(sid)
        assert paused["status"] == "paused", paused["chains"]
        assert paused["objects"]["context"]["revision"] == 1
        assert paused["objects"]["context"]["value"]["view_ref"] is None
        host = service._runtime_hosts[chain_id]
        assert host.active_handle_count == 1 and len(fixture.calls) == 1
        request = {"action": "resume", "expected_revision": paused["revision"], "idempotency_key": str(uuid4())}
        first = service.control(sid, **request)
        assert service.control(sid, **request) == first
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert final["objects"]["context"]["revision"] == 2
        assert len(fixture.calls) == 1 and fixture.closes == 1
        assert host.active_handle_count == 0 and chain_id not in service._runtime_hosts
        messages = output(final, "agents.execute", "context")["next_view"]["messages"]
        assert messages[0]["blocks"][0]["text"] == "frozen question"
        assert [message["role"] for message in messages] == ["user", "assistant"]
        operations = output(final, "agents.execute", "context")["operations"]
        assert [operation["message"]["role"] for operation in operations if operation["kind"] == "append"] == [
            "assistant", "tool"]


@pytest.mark.parametrize("outcome", ["error", "outcome_unknown", "interrupted"])
def test_two_round_history_preserves_typed_error_and_unknown_outcomes_without_success_fiction(
        tmp_path, monkeypatch, outcome):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-agent-private-secret")
    fixture, invocations = AgentTransportFixture(batch=True), []

    def inspect(text):
        invocations.append(text)
        if text == "one":
            if outcome == "error":
                raise ToolExecutionError("explicit fixture failure")
            raise ToolOutcomeUnknown("uncertain fixture effect", reason_code=outcome)
        return {"characters": len(text)}

    tools = (register_callable(
        "inspect_text", "Inspect text", {"type": "object", "properties": {
            "text": {"type": "string", "description": "Text to inspect"}},
            "required": ["text"], "additionalProperties": False}, inspect), final_answer_tool())
    with closing(GraphWorkflowService(
            tmp_path / f"agent-{outcome}.sqlite", public_model_factory=fixture.factory,
            capability_packages=[create_agent_package(tools=tools)])) as service:
        ModelDatabaseFixture.write(service, 1)
        first = run(service, create(service, graph(service)), "first question")
        assert first["status"] == "succeeded", first["chains"]
        messages = output(first, "agents.execute", "context")["next_view"]["messages"]
        observed = [message for message in messages if message["role"] == "tool"]
        projected = [message["blocks"][0] for message in observed]
        assert projected[0]["status"] == "error" and projected[0]["is_error"] is True
        assert observed[0]["source"].get("reason_code") == (None if outcome == "error" else outcome)
        if outcome != "error":
            assert observed[1]["source"]["reason_code"] == "never_started"
            assert projected[1]["status"] == "error" and projected[1]["is_error"] is True
        second = run(service, first, "second question")
        assert second["status"] == "succeeded", second["chains"]
        history = service.get_run(second["workflow_session_id"], second["selected_chain_run_id"])
        request = next(fact["payload"]["payload"] for fact in history["runtime_facts"]
                       if fact.get("executor_ref") and fact["payload"]["kind"] == "model_request")
        canonical_tools = [message for message in request["messages"] if message["role"] == "tool"]
        assert canonical_tools[0]["blocks"][0]["status"] == "error"
        assert canonical_tools[0]["blocks"][0]["is_error"] is True
        assert canonical_tools[0]["blocks"][0]["content"] == projected[0]["content"]
        assert canonical_tools[0]["blocks"][0]["model_visible_text"] == projected[0]["model_visible_text"]
        if outcome != "error":
            assert canonical_tools[0]["source"]["kind"] == "runtime_tool_observation"
            assert canonical_tools[0]["source"]["reason_code"] == outcome
            assert canonical_tools[1]["source"]["reason_code"] == "never_started"
            assert canonical_tools[1]["blocks"][0]["tool_execution_id"] is None
        else:
            assert canonical_tools[0]["source"]["kind"] == "tool"
        assert invocations == (["one", "two", "one", "two"] if outcome == "error" else ["one", "one"])
        assert len(fixture.calls) == 4
        assert service._runtime_hosts == {} and not service._service_runs
