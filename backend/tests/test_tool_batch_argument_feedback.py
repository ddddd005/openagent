"""Schema errors reject the complete batch and let the same Agent correct it."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_message_history
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.runtime import KernelPaused, RunFailed
from phase1_agent.tools import register_callable
from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from test_runtime import SCHEMA, ScriptedAdapter, batch, call, execute, final, setup
from workflow_test_support import AgentTransportFixture, graph


def _invalid_batch():
    return batch(
        call("inspect_text", '{"text":"valid-but-not-executed"}', "valid.old"),
        call("inspect_text", '{"text":12}', "invalid.old"),
        call("inspect_text", '{"text":"also-not-executed"}', "later.old"))


@pytest.mark.parametrize("pause", [False, True])
def test_entire_schema_invalid_batch_is_unexecuted_and_same_kernel_can_continue(pause):
    effects = []
    snapshot, tools = setup()
    tools = (register_callable("inspect_text", "Inspect text", SCHEMA,
                               lambda text: effects.append(text) or {"text": text}), tools[1])
    adapter = ScriptedAdapter(
        _invalid_batch(), batch(call("inspect_text", '{"text":"corrected"}', "new")),
        final({"text": "accepted"}))
    checkpoint = None
    if pause:
        with pytest.raises(KernelPaused) as raised:
            execute(adapter, snapshot=snapshot, tools=tools,
                    on_boundary=lambda boundary: boundary != "after_tool_batch_rejected")
        checkpoint = raised.value.checkpoint
        assert effects == []
        assert len(adapter.inputs) == 1
        assert checkpoint.pending_tools == ()
        assert checkpoint.messages[0]["source"]["kind"] == "protocol_feedback"
    result = execute(adapter, snapshot=snapshot, tools=tools, checkpoint=checkpoint)
    assert effects == ["corrected"]
    assert result.model_requests == result.attempts == 3
    feedback = result.messages[0]
    assert feedback["source"]["kind"] == "protocol_feedback"
    assert "Invalid tool arguments for: inspect_text." in feedback["blocks"][0]["text"]
    assert "No tools in this batch were executed" in feedback["blocks"][0]["text"]
    assert adapter.inputs[1][0][-1] == feedback
    assert len([block for message in result.messages for block in message["blocks"]
                if block["kind"] == "tool_call"]) == 2
    validate_message_history(snapshot["s0"] + result.messages)


@pytest.mark.parametrize("tail", [
    call("missing", "{}", "missing"),
    call("inspect_text", '{"text":"x"}', "invalid.old"),
    call("inspect_text", '{"text":NaN}', "bad.json"),
    call("final_answer", '{"answer":{"text":"x"}}', "bad.final"),
])
def test_schema_error_does_not_bypass_remaining_batch_protocol_validation(tail):
    snapshot, tools = setup()
    adapter = ScriptedAdapter(
        batch(call("inspect_text", '{"text":12}', "invalid.old"), tail))
    with pytest.raises(RunFailed) as raised:
        execute(adapter, snapshot=snapshot, tools=tools)
    assert raised.value.code == "protocol_error"
    assert raised.value.messages == []
    assert len(adapter.inputs) == 1


def test_real_agent_rejected_batch_is_proven_from_original_facts_and_feedback_cannot_be_forged(
        tmp_path, monkeypatch):
    import phase1_agent.agent_executor as agent_module
    import phase1_agent.context_update_v2 as update_module
    fixture = AgentTransportFixture()
    base_factory = fixture.factory
    effects = []
    original_tools = agent_module._agent_tools

    def tools(*args, **kwargs):
        result = original_tools(*args, **kwargs)
        return tuple(replace(tool, _implementation=lambda text: effects.append(text) or {"text": text})
                     if tool.name == "inspect_text" else tool for tool in result)

    monkeypatch.setattr(agent_module, "_agent_tools", tools)

    def factory(**options):
        transport = base_factory(**options)
        def generate(messages, registered):
            fixture.calls.append(deepcopy(messages))
            if len(fixture.calls) == 1:
                return _invalid_batch()
            if len(fixture.calls) == 2:
                return batch(call("inspect_text", '{"text":"corrected"}', "corrected.new"))
            return final({"text": "accepted"}, "final.new")
        transport.generate = generate
        return transport

    captured = []
    original_prove = update_module.prove_context_merge_execution_v2
    def prove(*args, **kwargs):
        captured.append((deepcopy(args), {
            key: value if callable(value) else deepcopy(value) for key, value in kwargs.items()}))
        return original_prove(*args, **kwargs)
    monkeypatch.setattr(update_module, "prove_context_merge_execution_v2", prove)
    with closing(GraphWorkflowService(
            tmp_path / "batch-feedback.sqlite", public_model_factory=factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        initial = create(service, graph(service))
        started = service.start(
            initial["workflow_session_id"], expected_revision=initial["revision"],
            idempotency_key="batch-feedback", inputs={"text": "question"})
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        completed = service.get_session(initial["workflow_session_id"])
        assert completed["status"] == "succeeded", completed["chains"]
        assert effects == ["corrected"]
        assert len(fixture.calls) == 3
        facts = service.get_run(initial["workflow_session_id"], chain_id)["runtime_facts"]
        assert sum(fact.get("payload", {}).get("kind") == "tool_dispatch" for fact in facts) == 2
        assert captured
        args, kwargs = captured[-1]
        valid_args = deepcopy(args)
        assistant = next(fact["payload"]["payload"]["message"] for fact in valid_args[3]
                         if fact["payload"]["kind"] == "message_accepted"
                         and fact["payload"]["payload"]["message"]["role"] == "assistant")
        assistant["role"] = "user"
        assistant["source"]["kind"] = "protocol_feedback"
        assistant["blocks"] = [{"kind": "text", "text": (
            "Invalid tool arguments for: inspect_text. No tools in this batch were executed. "
            "Generate a new tool call batch with valid arguments.")}]
        with pytest.raises(ContractValidationError):
            original_prove(*valid_args, **kwargs)
        events = args[3]
        feedback = next(fact["payload"]["payload"]["message"] for fact in events
                        if fact["payload"]["kind"] == "message_accepted"
                        and fact["payload"]["payload"]["message"]["source"]["kind"] == "protocol_feedback")
        feedback["blocks"][0]["text"] = "Forged parameter feedback"
        with pytest.raises(ContractValidationError):
            original_prove(*args, **kwargs)
