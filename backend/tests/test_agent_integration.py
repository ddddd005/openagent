"""Agent -> delta -> explicit context save using a local Chat mock only."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.content_contracts import text_content
from phase1_agent.agent_package import create_agent_package
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.tools import ToolExecutionError, ToolOutcomeUnknown, final_answer_tool, register_callable

from test_graph_service import create, document, edge, node
from test_model_package import source_config
from test_models_service_integration import ModelDatabaseFixture


class AgentTransportFixture:
    def __init__(self, *, gate=False, batch=False):
        self.calls, self.closes = [], 0
        self.entered, self.proceed = Event(), Event()
        self.gate, self.batch = gate, batch

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append(deepcopy(messages))
                fixture.entered.set()
                if fixture.gate:
                    assert fixture.proceed.wait(5), "Agent model gate timed out"
                if fixture.batch and len(fixture.calls) % 2 == 1:
                    return ModelResponse("tool_calls", tool_calls=(
                        ModelToolCall("inspect.one", "inspect_text", json.dumps({"text": "one"})),
                        ModelToolCall("inspect.two", "inspect_text", json.dumps({"text": "two"}))))
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer", json.dumps({"answer": {"text": "accepted answer"}})),))

            def close(self):
                fixture.closes += 1

        return Transport()


def graph(service):
    registry = service.registry
    read, window, current, model, assembly, execute, delta, advance, save, output = (
        node(registry, component, index) for index, component in enumerate((
            "context.read", "context.window", "tools.current-input", "models.source", "context.assembly",
            "agents.execute", "agents.delta", "context.advance", "context.save", "tools.output"), start=301))
    model["config"] = source_config()
    window["config"]["last_units"] = 1
    doc = document([read, window, current, model, assembly, execute, delta, advance, save, output], [
        edge(read, window, 1, target_port="view"),
        edge(window, assembly, 2, target_port="view"),
        edge(current, assembly, 3, target_port="current_input"),
        edge(model, execute, 4, target_port="model"),
        edge(assembly, execute, 5, target_port="prompt"),
        edge(assembly, delta, 6, target_port="prompt"),
        edge(execute, delta, 7, source_port="unit", target_port="unit"),
        edge(execute, delta, 8, source_port="facts", target_port="facts"),
        edge(window, advance, 9, target_port="view"),
        edge(delta, advance, 10, target_port="delta"),
        edge(advance, save, 11, target_port="view"),
        edge(execute, output, 12, source_port="result"),
    ])
    doc.update(schema_version=2, execution_roots=[save["node_binding_id"]],
               package_lock=list(registry.package_lock), object_bindings=[
        ObjectBinding("context", EFFECTIVE_CONTEXT_TYPE, 1, "shared",
                      readers=(read["node_binding_id"], save["node_binding_id"]),
                      writers=(save["node_binding_id"],)).to_dict()])
    return doc


def run(service, view, text):
    started = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                            idempotency_key=str(uuid4()), inputs={"text": text})
    service.wait(started["active_chain_run_id"])
    return service.get_session(view["workflow_session_id"])


def output(view, component, port="output"):
    return next(node["outputs"][port] for node in view["nodes"] if node["label"] == component)


def test_agent_two_rounds_preserve_tool_order_use_exact_view_and_explicitly_save_delta(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-agent-private-secret")
    fixture = AgentTransportFixture(batch=True)
    with closing(GraphWorkflowService(tmp_path / "agent-rounds.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        initial = create(service, graph(service))
        first = run(service, initial, "first question")
        assert first["status"] == "succeeded", first["chains"]
        assert output(first, "tools.output") == text_content("accepted answer")
        first_unit = output(first, "agents.execute", "unit")
        assert [message["role"] for message in first_unit["messages"]] == [
            "assistant", "tool", "tool", "assistant", "tool", "assistant"]
        first_view = output(first, "context.advance")
        assert len(first_view["units"]) == 1
        assert first["objects"]["context"]["revision"] == 2
        second = run(service, first, "second question")
        assert second["status"] == "succeeded", second["chains"]
        second_view = output(second, "context.advance")
        assert len(second_view["units"]) == 2
        assert second_view["units"][0]["unit"] == first_unit
        assert second_view["units"][1]["unit"]["root"]["content"] == "second question"
        assert second["objects"]["context"]["revision"] == 3
        assert len(second["objects"]["context"]["value"]["accepted_delta_ids"]) == 2
        delta = output(second, "agents.delta")
        assembly = output(second, "context.assembly")
        assert delta["basis_view_ref"] == assembly["context_ref"]
        assert delta["current_root_ref"] == assembly["current_input_ref"]
        assert delta["unit"] == output(second, "agents.execute", "unit")
        # The second round sees root + the whole prior closed batch + new root.
        wire = fixture.calls[2]
        assert [message["kind"] for message in wire] == [
            "text", "assistant_calls", "tool_result", "tool_result",
            "assistant_calls", "tool_result", "text", "text"]
        assert wire[0]["content"] == "first question" and wire[-1]["content"] == "second question"
        assert wire[2]["tool_call_id"] == wire[1]["calls"][0]["id"]
        assert wire[3]["tool_call_id"] == wire[1]["calls"][1]["id"]
        assert wire[5]["tool_call_id"] == wire[4]["calls"][0]["id"]
        assert len(fixture.calls) == 4 and fixture.closes == 2
        assert not hasattr(service, "_native_runtime")
        assert service._runtime_hosts == {} and not service._service_runs
        history = service.get_run(second["workflow_session_id"], second["selected_chain_run_id"])
        executor_facts = [fact for fact in history["runtime_facts"] if "executor_ref" in fact]
        receipts = output(second, "agents.execute", "facts")
        assert [fact["fact_id"] for fact in executor_facts] == receipts["fact_ids"]
        assert executor_facts[-1]["payload"]["kind"] == "agent_result"
        assert "offline-agent-private-secret" not in json.dumps(history)
        agent_record = next(record for record in history["node_runs"]
                            if record["node_binding_id"] == receipts["owner"]["node_binding_id"])
        assert agent_record["input_values"] == {}


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
        unit = output(final, "agents.execute", "unit")
        assert unit["root"]["content"] == "frozen question"
        assert [message["role"] for message in unit["messages"]] == ["assistant", "tool", "assistant"]


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
        first_unit = output(first, "agents.execute", "unit")
        projected = [message for message in first_unit["messages"] if message["role"] == "tool"]
        assert projected[0]["status"] == "error" and projected[0]["is_error"] is True
        assert projected[0]["outcome_reason"] == (None if outcome == "error" else outcome)
        if outcome != "error":
            assert projected[1]["outcome_reason"] == "never_started"
            assert projected[1]["status"] == "error" and projected[1]["is_error"] is True
        second = run(service, first, "second question")
        assert second["status"] == "succeeded", second["chains"]
        history = service.get_run(second["workflow_session_id"], second["selected_chain_run_id"])
        request = next(fact["payload"]["payload"] for fact in history["runtime_facts"]
                       if fact.get("executor_ref") and fact["payload"]["kind"] == "model_request")
        canonical_tools = [message for message in request["messages"] if message["role"] == "tool"]
        assert canonical_tools[0]["blocks"][0]["status"] == "error"
        assert canonical_tools[0]["blocks"][0]["is_error"] is True
        assert canonical_tools[0]["blocks"][0]["content"] == projected[0]["result"]
        assert canonical_tools[0]["blocks"][0]["model_visible_text"] == projected[0]["content"]
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


def test_context_save_failure_keeps_accepted_agent_artifacts_and_does_not_repeat_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-agent-private-secret")
    fixture = AgentTransportFixture()
    with closing(GraphWorkflowService(tmp_path / "agent-save-failed.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        entry = service.registry.get("context.save", "1")

        def fail_after_intent(config, inputs, context):
            entry.executor(config, inputs, context)
            raise RuntimeError("injected context save failure after staging")

        service.registry._nodes[("context.save", "1")] = replace(entry, executor=fail_after_intent)
        final = run(service, create(service, graph(service)), "question")
        assert final["status"] == "failed", final["chains"]
        assert final["objects"]["context"]["revision"] == 1
        assert final["objects"]["context"]["value"] == {"view_ref": None, "accepted_delta_ids": []}
        assert output(final, "agents.execute", "result") == text_content("accepted answer")
        assert output(final, "agents.delta")["unit"] == output(final, "agents.execute", "unit")
        assert len(fixture.calls) == 1 and fixture.closes == 1
        assert service._runtime_hosts == {}
        history = service.get_run(final["workflow_session_id"], final["active_chain_run_id"])
        assert any(fact.get("payload", {}).get("kind") == "agent_result" for fact in history["runtime_facts"])
        closed = service.control(final["workflow_session_id"], action="close",
                                 expected_revision=final["revision"], idempotency_key=str(uuid4()))
        assert closed["status"] == "closed" and len(fixture.calls) == 1
        advance_record = next(record for record in history["node_runs"]
                              if record["config"] == {} and record["node_binding_id"] == (
                                  next(node["node_binding_id"] for node in final["nodes"]
                                       if node["label"] == "context.advance")))
        save_node_id = next(node["node_binding_id"] for node in final["nodes"]
                            if node["label"] == "context.save")
        request = {"node_id": save_node_id, "view_output_id": advance_record["output_refs"]["output"],
                   "expected_revision": closed["revision"], "idempotency_key": str(uuid4())}
        adopted = service.adopt_context_view(closed["workflow_session_id"], **request)
        assert service.adopt_context_view(closed["workflow_session_id"], **request) == adopted
        assert adopted["session"]["objects"]["context"]["revision"] == 2
        assert len(adopted["session"]["objects"]["context"]["value"]["accepted_delta_ids"]) == 1
        assert adopted["session"]["objects"]["context"]["value"]["view_ref"]["output_id"] == (
            advance_record["output_refs"]["output"])
        assert len(fixture.calls) == 1 and fixture.closes == 1
        assert not hasattr(service, "_native_runtime") and service._runtime_hosts == {}
        assert not service._service_runs
