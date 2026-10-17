"""Uninstalled executor 2 on the real host and SQLite with offline communication."""

from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from phase1_agent.agent_executor import PublicAgentExecutor
from phase1_agent.content_contracts import text_content
from phase1_agent.context_contract import project_agent_context_message
from phase1_agent.context_prompt_v6 import assemble_native_context_prompt
from phase1_agent.context_v4 import merge_native_context_v5, validate_native_context_view_v5
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.model_execution_contracts import AGENT_EXECUTOR_REF_V2
from phase1_agent.runtime import CanonicalModelAdapter, RunFailed
from phase1_agent.runtime_executor_contracts import (
    ExecutorDefinition, ExecutorReference, ExecutorRegistry, PauseSupport,
)
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.runtime_hosting import InvocationOwner, RuntimeHost
from phase1_agent.storage import SqliteStore
from phase1_agent.tools import final_answer_tool, register_callable
from test_model_execution_requests import human
from test_model_request_fact_reader import FactHarness
from test_model_request_integration import IntegratedHost
from test_prompt_lifecycle import content, item


REFERENCE = ExecutorReference.from_dict(AGENT_EXECUTOR_REF_V2)


def _message_text(message):
    if all(block["kind"] == "text" for block in message["blocks"]):
        return "\n".join(block["text"] for block in message["blocks"])
    return None


def _once_content(item):
    return item["role"], item["text"]


def _injected_once_contents(prompt):
    frozen = {message["message_id"] for message in prompt["context"]["messages"]}
    return {(message["role"], _message_text(message)) for message, entry in
            zip(prompt["messages"], prompt["layout"])
            if entry["kind"] == "once" and message["message_id"] not in frozen}


def _persisted_once_contents(view):
    return {(message["role"], _message_text(message)) for message, entry in
            zip(view["messages"], view["layout"]) if entry["kind"] == "once"}


class KernelIntegratedHost(IntegratedHost):
    def facts(self):
        return [fact for fact in super().facts() if fact.get("kind") == "workflow.model-fact"]


class KernelHarness:
    def __init__(self, directory, *, pause_after_lookup=False, pause_after_summary=False,
                 drop_terminal_ack=False):
        self.integrated = KernelIntegratedHost(directory)
        self.mock = self.integrated.mock
        self.context = self.mock.context
        self.owner = InvocationOwner.from_dict({
            key: getattr(self.context, key) for key in (
                "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")
        })
        self.pause_after_lookup = pause_after_lookup
        self.pause_after_summary = pause_after_summary
        self.drop_terminal_ack = drop_terminal_ack
        self.lookup_calls, self.handles, self.preparations, self.results = [], [], [], []
        self.accepted_envelopes, self.accept_calls = {}, []
        self.policy = {
            "schema_version": 2, "kind": "workflow.context-compaction-policy",
            "enabled": True, "keep_depth": 0, "summary_prompt": "Summarize permitted text.",
        }
        initial = validate_native_context_view_v5({
            "schema_version": 5, "kind": "workflow.context-view",
            "owner": {"workflow_session_id": self.context.workflow_session_id, "object_key": "context"},
            "basis": {"revision_id": str(uuid4()), "head_revision": 1},
            "messages": [project_agent_context_message(human("previous context " * 500))],
            "layout": [{"kind": "history", "round_id": str(uuid4()), "compaction": "allowed"}],
            "accepted_delta_ids": [], "applied_delta_ids": [],
            "generation": 0,
            "derivation": {"operation": "read", "input_view_ref": None, "receipts_ref": None},
        })
        self.once = item(701, "ONCE_MATERIAL")
        self.fixed = item(702, "FIXED_MATERIAL", role="system",
                          lifecycle="per_request", compaction="never")
        self.prompt = assemble_native_context_prompt(
            [content(self.once, self.fixed)], initial, text_content("Current task"),
            context_ref={"scope": "artifact", "output_id": str(uuid4())},
            current_input_ref={"scope": "artifact", "output_id": str(uuid4())})
        input_refs = self.integrated.resources.evidence["consumer_run"]["input_refs"]
        self.input_values = {
            "prompt": self.prompt, "model": self.mock.binding, "compaction_policy": self.policy,
        }
        for port in ("prompt", "compaction_policy"):
            input_refs[port] = [{"edge_id": str(uuid4()), "output_id": str(uuid4()), "order": 0}]
        self.integrated.resources.persist_evidence("consumer_run")
        self.mock.input_refs = deepcopy(input_refs)
        self.context.input_artifact_refs = lambda port: deepcopy(input_refs.get(port, []))
        self.context.host_call = self.host_call
        self.policy_ref = {"scope": "artifact", "output_id": input_refs["compaction_policy"][0]["output_id"]}
        self.mock.on_communication = self.communication
        tools = (
            register_callable(
                "lookup", "Inspect the synthetic input.",
                {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                self.lookup),
            final_answer_tool(),
        )
        registry = ExecutorRegistry()

        def factory(config, inputs, context):
            handle = PublicAgentExecutor(
                config, inputs, context, tools=tools, schema_version=2)
            self.handles.append(handle)
            return handle

        registry.register_executor(
            ExecutorDefinition(REFERENCE, continuation_mode="same_process"), factory)
        registry.register_pause_support(PauseSupport(REFERENCE), lambda handle, token: handle.retains(token))
        self.runtime = RuntimeHost(registry, fact_sink=self.accept_fact)
        self.runtime.start(self.owner, REFERENCE, {}, self.input_values, self.context)

    def host_call(self, capability, operation, payload):
        if (capability, operation) == ("artifacts:read", "resolve-input"):
            port = payload["port"]
            return {
                "value": deepcopy(self.input_values[port]),
                "producer": self.owner.to_dict(),
                "output_id": self.context.input_artifact_refs(port)[0]["output_id"],
            }
        result = self.mock.service(self.context, capability, operation, payload)
        if operation == "prepare-request":
            self.preparations.append(deepcopy(result))
        elif operation == "dispatch-request":
            self.results.append(deepcopy(result))
        return result

    def save_runtime(self, output, context):
        profile = self.integrated.resources.profile
        profile["update_sequence"] += 1
        profile["value"]["runtime"].update(
            output_limit_tokens=output, context_length_tokens=context)
        self.integrated.resources.save(profile)

    def lookup(self):
        self.lookup_calls.append("executed")
        self.save_runtime(16000, 10000)
        return {"value": "found"}

    def communication(self):
        index = len(self.mock.communications)
        if index == 1:
            self.mock.response = ModelResponse("tool_calls", tool_calls=(
                ModelToolCall("provider-lookup", "lookup", "{}"),), model="mock-model")
        elif index == 2:
            self.mock.response = ModelResponse("stop", "Short retained summary.", model="mock-model")
            self.save_runtime(8000, 8000)
        else:
            self.mock.response = ModelResponse("tool_calls", tool_calls=(
                ModelToolCall("provider-final", "final_answer",
                              json.dumps({"answer": {"text": "Accepted answer"}})),), model="mock-model")

    def accept_fact(self, envelope):
        self.accept_calls.append(deepcopy(envelope))
        identity = envelope["fact_id"]
        previous = self.accepted_envelopes.get(identity)
        assert previous is None or previous == envelope
        with closing(SqliteStore(self.integrated.resources.path)) as store:
            facts = RuntimeFactStore(store)
            store._connection.execute("BEGIN IMMEDIATE")
            receipt = facts.accept(
                owner=envelope["owner"], stream_id="executor:agents.snapshot-kernel@2.0.0", fact=envelope)
            store._connection.execute("COMMIT")
        self.accepted_envelopes[identity] = deepcopy(envelope)
        event = envelope["payload"]
        if event["kind"] == "context_compaction_decided":
            self.save_runtime(12000, 11000)
        if (self.pause_after_lookup and event["kind"] == "message_accepted"
                and event["payload"]["message"]["role"] == "tool"
                and len(self.lookup_calls) == 1):
            self.pause_after_lookup = False
            self.runtime.request_pause(self.owner, "pause.after.lookup")
        if self.pause_after_summary and event["kind"] == "context_compaction_applied":
            self.pause_after_summary = False
            self.runtime.request_pause(self.owner, "pause.after.summary")
        if self.drop_terminal_ack and event["kind"] == "agent_result":
            self.drop_terminal_ack = False
            raise OSError("Synthetic terminal acknowledgment loss")
        return receipt

    def executor_facts(self, receipts):
        with closing(SqliteStore(self.integrated.resources.path)) as store:
            return RuntimeFactStore(store).executor_facts(
                receipts["owner"], receipts["executor_ref"], receipts["fact_ids"])

    def model_facts(self, receipts):
        dispatched = {result["request_id"] for result in self.results}
        reader_host = FactHarness.from_integrated(
            self.integrated,
            preparations=[prepared for prepared in self.preparations if prepared["request_id"] in dispatched],
            results=self.results)
        reader_host.receipts = deepcopy(receipts)
        reader_host.evidence["input_output"]["payload"] = deepcopy(receipts)
        reader_host.persist("input_output")
        reader_host.payload["requests"] = deepcopy(receipts["model_requests"])
        return reader_host.read()

    def prove(self, receipts, executor_facts, model_facts):
        from phase1_agent.context_update_v2 import prove_context_merge_execution_v2
        return prove_context_merge_execution_v2(
            self.prompt, self.mock.binding, receipts, executor_facts,
            model_facts=model_facts, policy=self.policy, policy_ref=self.policy_ref,
            project_messages=CanonicalModelAdapter._project,
            resolve_transport=self.mock.resolve_transport)

    def close(self):
        self.runtime.release(self.owner)
        self.mock.close()


@pytest.fixture
def kernel_host(tmp_path):
    host = KernelHarness(tmp_path)
    yield host
    host.close()


def test_real_kernel_tools_compaction_receipts_and_authorized_update_proof(kernel_host):
    host = kernel_host
    outcome = host.runtime.drive(host.owner)
    assert outcome.status == "succeeded"
    assert outcome.outputs["result"] == text_content("Accepted answer")
    receipts = outcome.outputs["receipts"]
    assert receipts["schema_version"] == 2
    assert host.lookup_calls == ["executed"]
    assert [event["payload"]["kind"] for event in host.accepted_envelopes.values()].count(
        "context_compaction_applied") == 1
    assert len(receipts["model_requests"]) == 3
    executor_facts = host.executor_facts(receipts)
    assert executor_facts[-1]["payload"]["payload"]["model_requests"] == 3
    assert executor_facts[-1]["payload"]["payload"]["attempts"] == 3
    model_facts = host.model_facts(receipts)
    before = (host.integrated.read_count, len(host.mock.communications),
              len(host.mock.broker.freeze_calls), deepcopy(host.integrated.facts()))
    host.save_runtime(5000, 9000)
    execution = host.prove(receipts, executor_facts, model_facts)
    assert execution["final"]["value"] == {"text": "Accepted answer"}
    receipts_ref = {"scope": "artifact", "output_id": str(uuid4())}
    merged = merge_native_context_v5(
        host.prompt["context"], host.prompt["context_ref"], receipts, receipts_ref, host.prompt, execution)
    assert merged["derivation"] == {
        "operation": "merge", "input_view_ref": host.prompt["context_ref"], "receipts_ref": receipts_ref}
    assert len(merged["applied_delta_ids"]) == 1
    with pytest.raises(ContractValidationError):
        merge_native_context_v5(merged, receipts_ref, receipts, receipts_ref, host.prompt, execution)
    assert before == (host.integrated.read_count, len(host.mock.communications),
                      len(host.mock.broker.freeze_calls), host.integrated.facts())
    assert _once_content(host.once) not in _persisted_once_contents(merged)
    assert _injected_once_contents(host.prompt) == {_once_content(host.once)}
    assert merged["messages"][-1]["blocks"] == [{"kind": "text", "text": "Accepted answer"}]
    assert not any("FIXED_MATERIAL" in block.get("text", "")
                   for message in merged["messages"] for block in message["blocks"])
    assert any(block.get("tool_name") == "lookup"
               for message in merged["messages"] for block in message["blocks"])
    assert not any(block.get("tool_name") == "final_answer"
                   for message in merged["messages"] for block in message["blocks"])
    assert b"synthetic-integration-key" not in canonical_bytes({
        "receipts": receipts, "executor_facts": executor_facts, "model_facts": model_facts})


def test_host_pause_keeps_completed_tool_and_reads_new_settings_only_on_next_request(tmp_path):
    host = KernelHarness(tmp_path, pause_after_lookup=True)
    try:
        paused = host.runtime.drive(host.owner)
        assert paused.status == "paused"
        original = deepcopy(host.integrated.facts())
        assert host.lookup_calls == ["executed"] and len(host.mock.communications) == 1
        host.save_runtime(14000, 10000)
        host.runtime.resume(host.owner, "resume.original.kernel")
        completed = host.runtime.drive(host.owner)
        assert completed.status == "succeeded"
        assert host.lookup_calls == ["executed"] and len(host.mock.communications) == 3
        assert host.integrated.facts()[:3] == original
        receipts = completed.outputs["receipts"]
        execution = host.prove(receipts, host.executor_facts(receipts), host.model_facts(receipts))
        operation = next(value for value in execution["operations"] if value["kind"] == "compact")
        assert operation["trigger_decision"]["preparation"]["settings"]["parameters"]["max_tokens"] == 14000
        assert execution["final"]["value"] == {
            "text": "Accepted answer"}
    finally:
        host.close()


def test_host_terminal_acceptance_reuses_original_fact_and_never_redrives_kernel(tmp_path):
    host = KernelHarness(tmp_path, drop_terminal_ack=True)
    try:
        with pytest.raises(OSError, match="terminal acknowledgment"):
            host.runtime.drive(host.owner)
        assert host.runtime.has_pending_acceptance(host.owner)
        before = deepcopy(host.integrated.facts())
        terminal = next(envelope for envelope in host.accepted_envelopes.values()
                        if envelope["payload"]["kind"] == "agent_result")
        assert host.lookup_calls == ["executed"] and len(host.mock.communications) == 3
        host.save_runtime(5000, 9000)
        completed = host.runtime.retry_acceptance(host.owner)
        assert completed.status == "succeeded"
        repeated = [envelope for envelope in host.accept_calls if envelope["fact_id"] == terminal["fact_id"]]
        assert repeated == [terminal, terminal]
        assert host.integrated.facts() == before
        assert host.lookup_calls == ["executed"] and len(host.mock.communications) == 3
        receipts = completed.outputs["receipts"]
        execution = host.prove(receipts, host.executor_facts(receipts), host.model_facts(receipts))
        assert execution["final"]["value"] == {
            "text": "Accepted answer"}
    finally:
        host.close()


def test_host_pause_after_summary_retains_replacement_and_sends_only_new_normal_on_resume(tmp_path):
    host = KernelHarness(tmp_path, pause_after_summary=True)
    try:
        assert host.runtime.drive(host.owner).status == "paused"
        original = deepcopy(host.integrated.facts())
        assert len(host.mock.communications) == 2
        assert host.lookup_calls == ["executed"]
        host.save_runtime(6000, 8000)
        host.runtime.resume(host.owner, "resume.after.summary")
        completed = host.runtime.drive(host.owner)
        assert completed.status == "succeeded"
        assert host.integrated.facts()[:6] == original
        assert host.lookup_calls == ["executed"]
        receipts = completed.outputs["receipts"]
        execution = host.prove(receipts, host.executor_facts(receipts), host.model_facts(receipts))
        assert sum(value["kind"] == "compact" for value in execution["operations"]) == 1
        dispatched = {result["request_id"] for result in host.results}
        dispatched_preparations = [
            preparation for preparation in host.preparations if preparation["request_id"] in dispatched]
        assert len(dispatched_preparations) == len(host.mock.communications) == 3
        assert [call["parameters"]["max_tokens"] for call in host.mock.communications] == [
            preparation["settings"]["parameters"]["max_tokens"] for preparation in dispatched_preparations]
        assert dispatched_preparations[-1]["settings"]["parameters"]["max_tokens"] == 6000
        assert execution["final"]["value"] == {
            "text": "Accepted answer"}
    finally:
        host.close()


def test_unknown_host_call_never_outputs_context_or_replays_after_resource_save(kernel_host):
    host = kernel_host
    host.mock.transport_error = TimeoutError("Synthetic uncertain communication")
    with pytest.raises(RunFailed) as failed:
        host.runtime.drive(host.owner)
    assert failed.value.code == "model_dispatch_unknown"
    facts = deepcopy(host.integrated.facts())
    assert facts[-1]["details"]["classification"] == "dispatch_unknown"
    host.save_runtime(16000, 10000)
    assert host.runtime.snapshot(host.owner)["status"] == "failed"
    assert host.lookup_calls == [] and len(host.mock.communications) == 1
    assert host.integrated.facts() == facts
    assert not any(event["payload"]["kind"] == "agent_result"
                   for event in host.accepted_envelopes.values())


@pytest.mark.parametrize("tamper", ["owner", "order", "settlement", "candidate"])
def test_full_update_rejects_modified_ownership_timeline_and_result(kernel_host, tamper):
    host = kernel_host
    outcome = host.runtime.drive(host.owner)
    receipts = deepcopy(outcome.outputs["receipts"])
    facts, models = host.executor_facts(receipts), host.model_facts(receipts)
    if tamper == "owner":
        receipts["owner"]["node_run_id"] = str(uuid4())
    elif tamper == "order":
        receipts["model_requests"].reverse()
    elif tamper == "settlement":
        settled = next(fact for fact in facts if fact["payload"]["kind"] == "tool_settled")
        settled["payload"]["payload"]["message"]["blocks"][0]["content"] = {"value": "forged"}
    else:
        terminal = next(fact for fact in facts if fact["payload"]["kind"] == "agent_result")
        terminal["payload"]["payload"]["final"]["value"]["text"] = "forged"
    with pytest.raises(ContractValidationError):
        host.prove(receipts, facts, models)
