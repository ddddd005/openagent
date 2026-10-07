"""Agent package slice: existing kernel, synthetic public model boundary."""

from copy import deepcopy
import json
from uuid import uuid4

import pytest

from phase1_agent.agent_executor import AGENT_EXECUTOR_REF, validate_execution_projection
from phase1_agent.agent_package import create_agent_package
from phase1_agent.content_contracts import text_content
from phase1_agent.context_contract import read_context_view
from phase1_agent.context_prompt import assemble_context_prompt
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_execution import NodeExecutionContext
from phase1_agent.capability_registry import create_package_registry
from phase1_agent.runtime import KernelContractError
from phase1_agent.runtime_hosting import InvocationOwner, RuntimeHost
from phase1_agent.tools import final_answer_tool, register_callable

from test_context_package import ref, uid, record
from test_model_package import source_config


def response(*calls):
    return {"finish_reason": "tool_calls", "content": None, "tool_calls": [
        {"id": identity, "name": name, "raw_arguments": json.dumps(arguments), "type": "function"}
        for identity, name, arguments in calls],
        "usage": None, "response_id": "fixture", "model": "deepseek-test"}


def final(text):
    return response(("final.provider", "final_answer", {"answer": {"text": text}}))


class Fixture:
    def __init__(self, *, steps=None, pause_after_tool=False, pause_during_response=False, config=None):
        self.tool_calls, self.model_calls, self.facts = [], [], []
        self.pause_after_tool = pause_after_tool
        self.pause_during_response = pause_during_response
        self.steps = list(steps or [final("answer")])

        def inspect(text):
            self.tool_calls.append(text)
            return {"characters": len(text)}

        tools = (register_callable(
            "inspect_text", "Inspect text", {"type": "object", "properties": {
                "text": {"type": "string", "description": "Text to inspect"}},
                                            "required": ["text"], "additionalProperties": False}, inspect),
                 final_answer_tool())
        self.registry = create_package_registry(packages=[create_agent_package(tools=tools)],
                                                enabled={"workflow.agents": "1.0.0"}).registry
        view = read_context_view(record(), uid(1), "context")
        self.prompt = assemble_context_prompt([], view, text_content("question"),
                                              context_ref=ref(60), current_input_ref=ref(61))
        model_source = source_config()
        self.binding = {"schema_version": 1, "kind": "workflow.model-binding", "binding_id": uid(70),
                        **model_source, "capabilities": {"protocol": "chat", "tools": True, "stream": False,
                                                       "thinking": "disabled"}}
        self.references = {"prompt": [{"edge_id": uid(80), "output_id": uid(81), "order": 0}],
                           "model": [{"edge_id": uid(82), "output_id": uid(83), "order": 0}]}
        definition = self.registry.get("agents.execute", "1").definition
        self.context = NodeExecutionContext(
            definition=definition, node_binding_id=uid(3), workflow_session_id=uid(1),
            chain_run_id=uid(2), node_run_id=uid(4), state={"revision": 0, "values": {}},
            private_states={}, external_inputs={}, type_registry=self.registry.data_types,
            input_refs=self.references, host=self.host_call)
        self.owner = InvocationOwner(uid(1), uid(2), uid(3), uid(4))
        self.host = RuntimeHost(self.registry.executors, fact_sink=self.accept_fact)
        self.config = config or definition.default_config
        self.host.start(self.owner, AGENT_EXECUTOR_REF, self.config,
                        {"prompt": self.prompt, "model": self.binding}, self.context)
        self.handle = self.host._invocations[self.owner].handle

    def host_call(self, context, capability, operation, payload):
        if capability == "artifacts:read":
            port = payload["port"]
            value = {"prompt": self.prompt, "model": self.binding}[port]
            return {"value": deepcopy(value), "producer": {
                "workflow_session_id": uid(1), "chain_run_id": uid(2),
                "node_binding_id": uid(90 if port == "prompt" else 91),
                "node_run_id": uid(92 if port == "prompt" else 93)},
                "output_id": self.references[port][0]["output_id"]}
        assert capability == "models:call" and operation == "kernel-model"
        self.model_calls.append(deepcopy(payload))
        value = self.steps.pop(0)
        if isinstance(value, Exception):
            raise value
        if self.pause_during_response:
            self.host.request_pause(self.owner, "pause.inflight.response")
            self.pause_during_response = False
        return {"schema_version": 1, "kind": "workflow.model-result", "binding_id": self.binding["binding_id"],
                "request_id": str(uuid4()), **deepcopy(value),
                "fact_refs": [{"fact_id": str(uuid4())} for _ in range(3)]}

    def accept_fact(self, envelope):
        self.facts.append(deepcopy(envelope))
        event = envelope["payload"]
        if (self.pause_after_tool and event["kind"] == "message_accepted"
                and event["payload"]["message"]["role"] == "tool"
                and len(self.tool_calls) == 1):
            self.host.request_pause(self.owner, "pause.after.first.tool")
        return {"fact_id": envelope["fact_id"]}


def test_opt_in_registration_exposes_public_contracts_without_constructing_agent_or_model():
    class UnusedKernel:
        def run(self, *args, **kwargs):
            raise AssertionError("registration must not execute")

    package = create_agent_package(kernel=UnusedKernel())
    loaded = create_package_registry(packages=[package], enabled={"workflow.agents": "1.0.0"})
    assert loaded.registry.get("agents.execute", "1").executor is None
    assert loaded.registry.get("agents.execute", "1").executor_ref == AGENT_EXECUTOR_REF
    assert loaded.registry.executors.supported_controls(AGENT_EXECUTOR_REF) == ("pause", "resume")
    assert loaded.registry.data_types.get("AGENT_RECEIPTS", 1, scope="content")
    definition = loaded.registry.get("agents.execute", "1").definition
    assert definition.default_config == {}
    assert definition.config_schema["properties"] == {}
    assert definition.config_schema["additionalProperties"] is False
    catalog_entry = next(item for item in loaded.registry.catalog()
                         if item["component_id"] == "agents.execute")
    assert catalog_entry["default_config"] == {}
    assert catalog_entry["config_schema"]["properties"] == {}
    assert create_package_registry(enabled={}).registry.get("agents.execute", "1") is None


def test_snapshot_frozen_public_binding_has_no_provider_body_and_result_has_owned_receipts():
    fixture = Fixture()
    snapshot = deepcopy(fixture.handle.snapshot)
    assert snapshot["config"]["payload"]["public_model_binding"] == fixture.binding
    assert "credential_ref" not in json.dumps(snapshot)
    assert "base_url" not in json.dumps(snapshot)
    assert snapshot["config"]["payload"]["transport_projection"][0]["identity_policy"] == (
        "frozen_transport_projection")
    fixture.prompt["current_input"]["content"] = "edited after factory"
    # Factory has already isolated and validated its immutable business input.
    outcome = fixture.host.drive(fixture.owner)
    assert outcome.status == "succeeded"
    unit, receipts = outcome.outputs["unit"], outcome.outputs["facts"]
    assert unit["root"]["content"] == "question"
    assert outcome.outputs["result"] == text_content("answer")
    assert [message["role"] for message in unit["messages"]] == ["assistant", "tool", "assistant"]
    assert unit["messages"][0]["tool_calls"][0]["name"] == "final_answer"
    assert unit["messages"][1]["tool_call_id"] == unit["messages"][0]["tool_calls"][0]["id"]
    assert receipts["owner"] == fixture.owner.to_dict()
    assert receipts["fact_ids"] == [fact["fact_id"] for fact in fixture.facts]
    assert "messages" not in receipts and "payload" not in receipts
    assert fixture.host.active_handle_count == 0 and fixture.handle.disposed
    assert fixture.handle.snapshot is None and fixture.handle.checkpoint is None
    assert len(fixture.model_calls) == 1


def test_tool_batch_pause_retains_accepted_response_and_pending_tool_order_without_resend():
    fixture = Fixture(steps=[
        response(("provider.a", "inspect_text", {"text": "one"}),
                 ("provider.b", "inspect_text", {"text": "two"})),
        final("complete")], pause_after_tool=True)
    assert fixture.host.drive(fixture.owner).status == "paused"
    assert fixture.tool_calls == ["one"] and len(fixture.model_calls) == 1
    checkpoint = fixture.handle.checkpoint
    assert len(checkpoint.pending_tools) == 1 and checkpoint.pending_tools[0].arguments == {"text": "two"}
    assert fixture.handle.retains(checkpoint)
    first = fixture.host.resume(fixture.owner, "continue.once")
    assert fixture.host.resume(fixture.owner, "continue.once") == first
    outcome = fixture.host.drive(fixture.owner)
    assert outcome.status == "succeeded"
    assert fixture.tool_calls == ["one", "two"] and len(fixture.model_calls) == 2
    unit = outcome.outputs["unit"]
    assert [message["role"] for message in unit["messages"]] == [
        "assistant", "tool", "tool", "assistant", "tool", "assistant"]
    first_calls = unit["messages"][0]["tool_calls"]
    assert [message["tool_call_id"] for message in unit["messages"][1:3]] == [
        call["id"] for call in first_calls]
    wire = fixture.model_calls[1]["messages"]
    assert [message["kind"] for message in wire] == [
        "text", "assistant_calls", "tool_result", "tool_result"]
    assert wire[-2]["tool_call_id"] == wire[1]["calls"][0]["id"]
    assert wire[-1]["tool_call_id"] == wire[1]["calls"][1]["id"]
    validate_execution_projection(unit, outcome.outputs["facts"], fixture.facts, fixture.handle.prompt)


def test_pause_while_response_inflight_accepts_it_before_safe_point_and_never_resends():
    fixture = Fixture(pause_during_response=True)
    assert fixture.host.drive(fixture.owner).status == "paused"
    assert len(fixture.model_calls) == 1
    assert len(fixture.handle.checkpoint.messages) == 1
    assert len(fixture.handle.checkpoint.pending_tools) == 1
    assert fixture.handle.checkpoint.pending_tools[0].name == "final_answer"
    fixture.host.resume(fixture.owner, "resume.retained")
    result = fixture.host.drive(fixture.owner)
    assert result.status == "succeeded" and result.outputs["result"] == text_content("answer")
    assert len(fixture.model_calls) == 1


def test_protocol_correction_feedback_stays_in_actual_facts_and_not_effective_human_history():
    stopped = {"finish_reason": "stop", "content": "preliminary", "tool_calls": [],
               "usage": None, "response_id": None, "model": "deepseek-test"}
    fixture = Fixture(steps=[stopped, final("corrected")])
    result = fixture.host.drive(fixture.owner)
    assert result.status == "succeeded"
    unit = result.outputs["unit"]
    assert all(message["role"] != "user" for message in unit["messages"])
    assert any(fact["payload"]["kind"] == "message_accepted"
               and fact["payload"]["payload"]["message"]["role"] == "user" for fact in fixture.facts)
    assert len(fixture.model_calls) == 2
    validate_execution_projection(unit, result.outputs["facts"], fixture.facts, fixture.prompt)


def test_delta_projection_validates_persisted_facts_and_uses_own_producer_owner():
    fixture = Fixture()
    outcome = fixture.host.drive(fixture.owner)
    definition = fixture.registry.get("agents.delta", "1").definition
    values = {"prompt": fixture.prompt, "unit": outcome.outputs["unit"], "facts": outcome.outputs["facts"]}
    refs = {"prompt": fixture.references["prompt"],
            "unit": [{"edge_id": uid(101), "output_id": uid(102), "order": 0}],
            "facts": [{"edge_id": uid(103), "output_id": uid(104), "order": 0}]}

    def host_call(ctx, capability, operation, payload):
        if capability == "facts:read":
            assert payload["port"] == "facts" and payload["producer"] == fixture.owner.to_dict()
            return deepcopy(fixture.facts)
        port = payload["port"]
        return {"value": deepcopy(values[port]), "producer": fixture.owner.to_dict(),
                "output_id": refs[port][0]["output_id"]}

    ctx = NodeExecutionContext(
        definition=definition, node_binding_id=uid(110), workflow_session_id=uid(1), chain_run_id=uid(2),
        node_run_id=uid(111), state={"revision": 0, "values": {}}, private_states={}, external_inputs={},
        type_registry=fixture.registry.data_types, input_refs=refs, host=host_call)
    execute = fixture.registry.get("agents.delta", "1").executor
    delta = execute({}, values, ctx)["output"]
    assert delta["owner"]["node_binding_id"] == uid(110) and delta["owner"]["node_run_id"] == uid(111)
    assert delta["unit_ref"] == ref(102) and delta["fact_receipt_refs"] == [ref(104)]
    assert delta["frozen_prompt_ref"] == ref(81)
    assert delta["basis_view_ref"] == fixture.prompt["context_ref"]
    assert delta["current_root_ref"] == fixture.prompt["current_input_ref"]
    assert execute({}, values, ctx)["output"]["delta_id"] == delta["delta_id"]
    forged = deepcopy(values)
    forged["unit"]["messages"][-1]["content"] = "forged"
    values["unit"] = deepcopy(forged["unit"])
    with pytest.raises(ContractValidationError) as caught:
        execute({}, forged, ctx)
    assert caught.value.reason_code == "agent_projection_mismatch"


def test_projection_rejects_a_forged_final_and_a_tool_dispatch_without_its_accepted_call():
    fixture = Fixture()
    result = fixture.host.drive(fixture.owner)
    forged_facts = deepcopy(fixture.facts)
    forged_unit = deepcopy(result.outputs["unit"])
    forged_facts[-1]["payload"]["payload"]["final"]["value"]["text"] = "forged answer"
    forged_unit["messages"][-1]["content"] = "forged answer"
    with pytest.raises(ContractValidationError) as caught:
        validate_execution_projection(forged_unit, result.outputs["facts"], forged_facts, fixture.prompt)
    assert caught.value.reason_code == "agent_result_unaccepted"
    forged_facts = deepcopy(fixture.facts)
    dispatch = next(fact for fact in forged_facts if fact["payload"]["kind"] == "tool_dispatch")
    dispatch["payload"]["payload"]["tool_call_id"] = str(uuid4())
    with pytest.raises(ContractValidationError, match="no unexecuted accepted call"):
        validate_execution_projection(result.outputs["unit"], result.outputs["facts"], forged_facts, fixture.prompt)


@pytest.mark.parametrize("pause_after_tool", [False, True])
def test_agent_ends_on_final_answer_after_old_request_and_attempt_limits_without_replay(pause_after_tool):
    texts = [f"round-{index}" for index in range(32)]
    fixture = Fixture(steps=[
        *[response((f"provider.inspect.{index}", "inspect_text", {"text": text}))
          for index, text in enumerate(texts)],
        final("complete after 32 tools")], pause_after_tool=pause_after_tool)
    outcome = fixture.host.drive(fixture.owner)
    if pause_after_tool:
        assert outcome.status == "paused"
        assert fixture.tool_calls == texts[:1] and len(fixture.model_calls) == 1
        assert fixture.handle.checkpoint.model_requests == fixture.handle.checkpoint.attempts == 1
        fixture.host.resume(fixture.owner, "resume.unlimited")
        outcome = fixture.host.drive(fixture.owner)
    assert outcome.status == "succeeded"
    assert outcome.outputs["result"] == text_content("complete after 32 tools")
    assert fixture.config == {}
    assert fixture.tool_calls == texts and len(fixture.model_calls) == 33
    assert len({call["request_key"] for call in fixture.model_calls}) == 33
    result_fact = fixture.facts[-1]["payload"]
    assert result_fact["kind"] == "agent_result"
    assert result_fact["payload"]["model_requests"] == result_fact["payload"]["attempts"] == 33
    assert len(outcome.outputs["unit"]["messages"]) == 67
    validate_execution_projection(outcome.outputs["unit"], outcome.outputs["facts"], fixture.facts, fixture.prompt)
    assert fixture.host.active_handle_count == 0 and fixture.handle.checkpoint is None


def test_unknown_public_dispatch_error_does_not_trigger_kernel_retry_or_replay():
    error = ContractValidationError("Dispatch outcome is unknown")
    error.reason_code = "model_dispatch_unknown"
    fixture = Fixture(steps=[error])
    with pytest.raises(KernelContractError) as caught:
        fixture.host.drive(fixture.owner)
    assert caught.value.code == "adapter_contract_error"
    assert len(fixture.model_calls) == 1 and fixture.host.active_handle_count == 0
    assert any(fact["payload"]["kind"] == "execution_failed" for fact in fixture.facts)
