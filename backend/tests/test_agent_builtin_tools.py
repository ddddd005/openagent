"""Agent-local built-in declarations and real offline execution boundaries."""

from contextlib import closing
from copy import deepcopy
import json
import socket
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, ValidationError

from phase1_agent.agent_executor import AGENT_OUTPUT_SCHEMA, _agent_tools
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.tools import final_answer_tool, register_callable
from phase1_agent.workflow_tool_catalog import builtin_workflow_tools

from credential_test_support import TestProtector
from test_stage4_model_request_installation import (
    _agent_graph, _checkpoint, _execute, _output, _resources,
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Agent built-in tool tests must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def final_response(answer, identity):
    return ModelResponse("tool_calls", tool_calls=(
        ModelToolCall(identity, "final_answer", json.dumps({"answer": answer})),
    ))


def test_final_answer_declares_and_validates_the_supplied_output_schema():
    output = {
        "type": "object", "properties": {"result": {"type": "integer"}},
        "required": ["result"], "additionalProperties": False,
    }
    original = deepcopy(output)
    tool = final_answer_tool(output_schema=output)
    output["properties"]["result"]["type"] = "string"
    assert tool.definition["function"]["parameters"] is tool.schema
    assert tool.schema["properties"]["answer"] == original
    assert "plain-text" in tool.definition["function"]["description"]
    tool.validate({"answer": {"result": 7}})
    assert tool.execute({"answer": {"result": 7}}) == {"result": 7}
    for arguments in ({}, {"answer": {"text": "wrong field"}},
                      {"answer": {"result": "7"}}, {"answer": {"result": 7}, "extra": True}):
        with pytest.raises(ValidationError):
            tool.validate(arguments)
    with pytest.raises(TypeError):
        tool.schema["properties"]["answer"]["properties"]["result"]["type"] = "number"
    assert final_answer_tool().schema["properties"]["answer"] == {
        "type": "object", "description": "The final JSON object, validated separately against output_spec.",
    }


def test_default_agent_tools_bind_the_final_schema_without_changing_shared_tools():
    shared = builtin_workflow_tools()
    tools = _agent_tools(None)
    assert [tool.name for tool in tools] == [tool.name for tool in shared]
    original_inspect, original_final = shared
    inspect, final = tools
    assert inspect.definition == original_inspect.definition
    assert inspect.execute({"text": "one\ntwo"}) == {"characters": 7, "lines": 2}
    assert final.schema["properties"]["answer"] == AGENT_OUTPUT_SCHEMA
    assert "ends the Agent turn" in final.definition["function"]["description"]
    assert original_final.schema != final.schema
    final.validate({"answer": {"text": "Complete result"}})


@pytest.mark.parametrize("arguments", [
    {}, {"answer": "plain text"}, {"answer": {"answer": "wrong field"}},
    {"answer": {"text": ""}}, {"answer": {"text": 7}},
    {"answer": {"text": "ok", "extra": True}}, {"answer": {"text": "ok"}, "extra": True},
])
def test_agent_final_parameters_reject_the_reported_shape_and_other_invalid_values(arguments):
    final = _agent_tools(())[0]
    with pytest.raises(ValidationError):
        final.validate(arguments)


def test_agent_always_installs_final_and_preserves_extension_implementation():
    effects = []
    schema = {
        "type": "object", "properties": {"value": {"type": "string", "description": "Value"}},
        "required": ["value"], "additionalProperties": False,
    }
    extension = register_callable("record", "Record a value", schema, lambda value: effects.append(value))
    tools = _agent_tools((extension,))
    assert tools[0] is extension
    assert [tool.name for tool in tools] == ["record", "final_answer"]
    tools[0].execute({"value": "offline"})
    assert effects == ["offline"]
    supplied = final_answer_tool()
    assert _agent_tools((supplied,))[0].schema["properties"]["answer"] == AGENT_OUTPUT_SCHEMA
    # Duplicate declarations remain visible to the kernel's registry validation.
    assert len(_agent_tools((supplied, supplied))) == 2


class OfflineProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests, self.closes = [], 0

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate_prepared(self, prepared):
                wire = deepcopy(prepared["wire_request"])
                fixture.requests.append(wire)
                declared = {tool["function"]["name"]: tool["function"] for tool in wire["tools"]}
                final = declared["final_answer"]
                assert final["parameters"]["properties"]["answer"] == AGENT_OUTPUT_SCHEMA
                assert "plain-text" in final["description"] and "ends the Agent turn" in final["description"]
                assert "inspect_text" in declared
                assert fixture.responses, "Unexpected additional model request"
                return fixture.responses.pop(0)

            def close(self):
                fixture.closes += 1

        return Transport()


def executor_events(service, session):
    history = service.get_run(session["workflow_session_id"], session["selected_chain_run_id"])
    return [fact["payload"] for fact in history["runtime_facts"] if "executor_ref" in fact]


def test_model_receives_both_declarations_and_two_rounds_keep_context_checkpoints_and_fork(tmp_path):
    provider = OfflineProvider([
        final_response({"text": "First answer"}, "first-final"),
        final_response({"text": "Second answer"}, "second-final"),
    ])
    with closing(GraphWorkflowService(
            tmp_path / "builtins.sqlite", credential_protector=TestProtector(),
            public_model_factory=provider.factory)) as service:
        _, profile = _resources(service)
        initial, branches = _agent_graph(service, profile, count=1)
        branch = branches[0]
        definition = service.get_definition(initial["workflow_definition_id"])
        first = _execute(service, initial, "First input")
        assert first["status"] == "succeeded", first["chains"]
        assert _output(first, branch["agent"], "result")["text"] == "First answer"
        events = executor_events(service, first)
        terminal = next(event["payload"] for event in events if event["kind"] == "agent_result")
        request = next(event["payload"] for event in events if event["kind"] == "model_request")
        final = next(tool for tool in terminal["snapshot"]["tool_definitions"] if tool["name"] == "final_answer")
        assert final["parameters_schema"]["properties"]["answer"] == terminal["snapshot"]["output_schema"]
        assert final["description"] == request["tools"][-1]["function"]["description"]
        prompt = next(node["outputs"]["output"] for node in first["nodes"]
                      if node["label"] == "context.assembly")
        assert terminal["snapshot"]["s0"] == prompt["messages"]
        assert request["messages"] == prompt["messages"]
        assert terminal["model_requests"] == terminal["attempts"] == 1
        assert len(first["objects"][branch["key"]]["value"]["messages"]) == 2
        first_point, first_state = _checkpoint(service, first)
        assert first_state["agent_contexts"][branch["key"]]["value"] == (
            first["objects"][branch["key"]]["value"])
        frozen_first = deepcopy(first["objects"])

        second = _execute(service, first, "Second input")
        assert second["status"] == "succeeded", second["chains"]
        assert _output(second, branch["agent"], "result")["text"] == "Second answer"
        assert "First input" in json.dumps(provider.requests[1]["messages"])
        assert "First answer" in json.dumps(provider.requests[1]["messages"])
        assert len(second["objects"][branch["key"]]["value"]["messages"]) == 4
        point, state = _checkpoint(service, second)
        assert point["checkpoint_id"] != first_point["checkpoint_id"]
        assert state["agent_contexts"][branch["key"]]["value"] == second["objects"][branch["key"]]["value"]
        assert first["objects"] == frozen_first
        target = service.fork_checkpoint(
            second["workflow_session_id"], checkpoint_id=point["checkpoint_id"],
            expected_session_revision=second["revision"], expected_data_revision=second["data_revision"],
            expected_head_revision=second["head_revision"], idempotency_key=str(uuid4()))
        child = service.get_session(target["target_session_id"])
        assert child["objects"][branch["key"]]["value"] == second["objects"][branch["key"]]["value"]
        assert len(provider.requests) == provider.closes == 2
        assert service.get_definition(initial["workflow_definition_id"]) == definition
        for wire in provider.requests:
            parameters = next(tool["function"]["parameters"] for tool in wire["tools"]
                              if tool["function"]["name"] == "final_answer")
            Draft202012Validator(parameters).validate({"answer": {"text": "Complete result"}})


@pytest.mark.parametrize("plain_response", [False, True], ids=["wrong-field", "plain-text"])
def test_existing_feedback_can_correct_one_invalid_response_without_losing_proof(tmp_path, plain_response):
    first = (ModelResponse("stop", content="Already rewritten text") if plain_response
             else final_response({"answer": "wrong nested field"}, "invalid-final"))
    provider = OfflineProvider([first, final_response({"text": "Corrected result"}, "corrected-final")])
    with closing(GraphWorkflowService(
            tmp_path / "correction.sqlite", credential_protector=TestProtector(),
            public_model_factory=provider.factory)) as service:
        _, profile = _resources(service)
        initial, branches = _agent_graph(service, profile, count=1)
        final = _execute(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        assert _output(final, branches[0]["agent"], "result")["text"] == "Corrected result"
        assert len(provider.requests) == 2
        terminal = next(event["payload"] for event in executor_events(service, final)
                        if event["kind"] == "agent_result")
        assert terminal["model_requests"] == terminal["attempts"] == 2
        prompt = next(node["outputs"]["output"] for node in final["nodes"]
                      if node["label"] == "context.assembly")
        assert terminal["snapshot"]["s0"] == prompt["messages"]
        value = final["objects"][branches[0]["key"]]["value"]
        assert not any(message.get("source", {}).get("kind") == "protocol_feedback"
                       for message in value["messages"])
        point, state = _checkpoint(service, final)
        assert point is not None and state["agent_contexts"][branches[0]["key"]]["value"] == value


def test_repeated_invalid_final_still_fails_without_extra_retries_or_checkpoint(tmp_path):
    provider = OfflineProvider([
        final_response({"answer": "wrong nested field"}, "invalid-first"),
        final_response({"answer": "still wrong"}, "invalid-second"),
    ])
    with closing(GraphWorkflowService(
            tmp_path / "invalid.sqlite", credential_protector=TestProtector(),
            public_model_factory=provider.factory)) as service:
        _, profile = _resources(service)
        initial, branches = _agent_graph(service, profile, count=1)
        before = deepcopy(initial["objects"])
        failed = _execute(service, initial)
        assert failed["status"] == "failed"
        assert failed["chains"][-1]["diagnostic"]["code"] == "invalid_final"
        assert len(provider.requests) == 2
        assert failed["objects"] == before
        assert _checkpoint(service, failed) == (None, None)
        events = executor_events(service, failed)
        failure = next(event["payload"] for event in events if event["kind"] == "execution_failed")
        assert failure["model_requests"] == failure["attempts"] == 2
        assert not any(event["kind"] == "agent_result" for event in events)
