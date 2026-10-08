"""Controlled native Agent graph execution with offline provider fixtures."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.model_contract import (
    validate_model_source_config, validate_native_model_source_config,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.storage import SqliteStore

from workflow_test_support import run
from test_context_native_integration import NativeTransport, native_graph, node_output
from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture


class RecordingTransport:
    def __init__(self, *, tool_batch=False, pause_summary=False):
        self.calls, self.factories, self.closes = [], [], 0
        self.tool_batch, self.pause_summary = tool_batch, pause_summary
        self.summary_entered, self.summary_release = Event(), Event()

    def factory(self, *, provider, parameters, api_key):
        fixture = self
        fixture.factories.append(deepcopy(parameters))

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools),
                                      "parameters": deepcopy(parameters)})
                if "SUMMARY_ONLY" in messages[-1].get("content", ""):
                    fixture.summary_entered.set()
                    if fixture.pause_summary:
                        assert fixture.summary_release.wait(5), "Offline summary gate timed out"
                    return ModelResponse("stop", content="Retained background checkpoint.", usage={
                        "prompt_tokens": 999, "completion_tokens": 5,
                        "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 935,
                    }, response_id="mock-summary", model=parameters["model"])
                if fixture.tool_batch and messages[-1]["role"] == "user":
                    return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                        "inspect.provider", "inspect_text", '{"text":"actual tool"}'),))
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer", json.dumps({"answer": {"text": "Native answer"}})),))

            def close(self):
                fixture.closes += 1

        return Transport()


def test_native_rounds_keep_actual_tool_ids_and_project_final_answer_as_text(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-agent")
    fixture = RecordingTransport(tool_batch=True)
    with closing(GraphWorkflowService(tmp_path / "native-rounds.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service, policy=False, once=False)
        first = run(service, create(service, doc), "First user turn")
        assert first["status"] == "succeeded", first["chains"]
        first_view = node_output(first, entries["a"]["merge"])
        calls = [message for message in first_view["messages"] if message["role"] == "assistant"
                 and any(block["kind"] == "tool_call" for block in message["blocks"])]
        assert len(calls) == 1
        call_id = calls[0]["blocks"][0]["tool_call_id"]
        second = run(service, first, "Second user turn")
        assert second["status"] == "succeeded", second["chains"]
        historical = [message for message in fixture.calls[2]["messages"] if message["kind"] == "assistant_calls"]
        assert historical[0]["calls"][0]["id"] == call_id
        paired = [message for message in fixture.calls[2]["messages"] if message["kind"] == "tool_result"]
        assert paired[0]["tool_call_id"] == call_id
        final_view = node_output(second, entries["a"]["merge"])
        assert all(block.get("tool_name") != "final_answer"
                   for message in final_view["messages"] for block in message["blocks"])
        assert final_view["messages"][-1]["role"] == "assistant"
        assert final_view["messages"][-1]["blocks"] == [{"kind": "text", "text": "Native answer"}]
        assert all("FIXED_RULE" not in block.get("text", "")
                   for message in final_view["messages"] for block in message["blocks"])
        assert len(fixture.calls) == 4 and fixture.closes == 2


def test_summary_output_limit_is_controlled_and_usage_is_preserved_without_claiming_a_real_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-agent")
    fixture = RecordingTransport()
    with closing(GraphWorkflowService(tmp_path / "native-summary.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service)
        model = next(node for node in doc["nodes"] if node["component_id"] == "models.source")
        model["config"]["parameters"]["max_tokens"] = 256
        model["config"]["capacity"].update(output_reserve_tokens=256, summary_max_tokens=16)
        final = run(service, create(service, doc), "Current task")
        assert final["status"] == "succeeded", final["chains"]
        assert [call["parameters"]["max_tokens"] for call in fixture.calls] == [16, 256]
        assert fixture.calls[0]["tools"] == fixture.calls[1]["tools"]
        packet = node_output(final, entries["a"]["agent"], "context")
        compact = packet["operations"][0]
        assert compact["request"]["model_parameters"]["max_tokens"] == 16
        assert compact["result"]["usage"] == {
            "prompt_tokens": 999, "completion_tokens": 5,
            "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 935,
        }
        history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
        model_requests = [fact for fact in history["runtime_facts"] if fact.get("stage") == "request"]
        auxiliary = next(fact for fact in model_requests if fact["details"].get("purpose") == "context_compaction")
        assert auxiliary["details"]["transport_arguments"]["max_tokens"] == 16
        assert auxiliary["details"]["cold_input_estimate"]["token_count_kind"] == "utf8_bytes_estimate"
        terminal = next(fact["payload"]["payload"] for fact in history["runtime_facts"]
                        if fact.get("payload", {}).get("kind") == "agent_result")
        assert terminal["model_requests"] == terminal["attempts"] == 1
        assert fixture.closes == 2


def test_cold_input_budget_rejects_summary_before_any_provider_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-agent")
    fixture = RecordingTransport()
    with closing(GraphWorkflowService(tmp_path / "cold-budget.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, _ = native_graph(service)
        model = next(node for node in doc["nodes"] if node["component_id"] == "models.source")
        model["config"]["capacity"]["max_cold_input_tokens"] = 100
        initial = create(service, doc)
        before = deepcopy(initial["objects"]["context-a"])
        failed = run(service, initial, "Current task")
        assert failed["status"] == "failed", failed["chains"]
        assert fixture.calls == [] and fixture.factories == []
        assert failed["objects"]["context-a"] == before
        history = service.get_run(failed["workflow_session_id"], failed["active_chain_run_id"])
        failures = [fact["payload"]["payload"] for fact in history["runtime_facts"]
                    if fact.get("payload", {}).get("kind") == "execution_failed"]
        assert failures[-1]["code"] == "context_compaction_cold_input_budget_exceeded"


def test_native_pause_after_inflight_summary_resumes_same_view_without_another_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-agent")
    fixture = RecordingTransport(pause_summary=True)
    with closing(GraphWorkflowService(tmp_path / "native-pause.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service)
        initial = create(service, doc)
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()), inputs={"text": "Continue after pause"})
        assert fixture.summary_entered.wait(5)
        current = service.get_session(initial["workflow_session_id"])
        service.control(current["workflow_session_id"], action="pause",
                        expected_revision=current["revision"], idempotency_key=str(uuid4()))
        fixture.summary_release.set()
        service.wait(started["active_chain_run_id"])
        paused = service.get_session(initial["workflow_session_id"])
        assert paused["status"] == "paused", paused["chains"]
        assert len(fixture.calls) == 1
        service.control(paused["workflow_session_id"], action="resume",
                        expected_revision=paused["revision"], idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        final = service.get_session(initial["workflow_session_id"])
        assert final["status"] == "succeeded", final["chains"]
        assert len(fixture.calls) == 2
        packet = node_output(final, entries["a"]["agent"], "context")
        assert sum(operation["kind"] == "compact" for operation in packet["operations"]) == 1
        assert final["objects"]["context-a"]["revision"] == 2


def test_model_source_versions_do_not_guess_or_silently_accept_capacity_fields():
    from test_model_package import source_config
    old = source_config()
    native = {**deepcopy(old), "capacity": {
        "context_window_tokens": 10_000, "output_reserve_tokens": 128,
        "summary_max_tokens": 16, "max_cold_input_tokens": 5000,
    }}
    native["parameters"]["max_tokens"] = 128
    assert validate_native_model_source_config(native) == native
    with pytest.raises(ContractValidationError):
        validate_model_source_config(native)
    for field in ("context_window_tokens", "max_cold_input_tokens"):
        invalid = deepcopy(native)
        invalid["capacity"][field] = 0
        with pytest.raises(ContractValidationError):
            validate_native_model_source_config(invalid)
    missing_output = deepcopy(native)
    missing_output["parameters"].pop("max_tokens")
    with pytest.raises(ContractValidationError):
        validate_native_model_source_config(missing_output)


def test_summary_model_outcome_commit_failure_never_replays_dispatch_or_adopts_context(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-agent")
    fixture = RecordingTransport()
    original, fail_once = RuntimeFactStore.accept, [True]

    def fail_outcome(store, *, owner, stream_id, fact):
        if fact.get("kind") == "workflow.model-fact" and fact.get("stage") == "outcome" and fail_once[0]:
            fail_once[0] = False
            raise OSError("injected private model outcome commit fault")
        return original(store, owner=owner, stream_id=stream_id, fact=fact)

    monkeypatch.setattr(RuntimeFactStore, "accept", fail_outcome)
    with closing(GraphWorkflowService(tmp_path / "model-outcome-failure.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, _ = native_graph(service)
        initial = create(service, doc)
        before = deepcopy(initial["objects"]["context-a"])
        failed = run(service, initial, "Do not adopt an unaccepted summary")
        assert failed["status"] == "failed", failed["chains"]
        assert len(fixture.calls) == 1
        assert failed["objects"]["context-a"] == before
        history = service.get_run(failed["workflow_session_id"], failed["active_chain_run_id"])
        failures = [fact["payload"]["payload"] for fact in history["runtime_facts"]
                    if fact.get("payload", {}).get("kind") == "execution_failed"]
        assert failures[-1]["code"] == "model_fact_acceptance_failed"
        assert not any(fact.get("payload", {}).get("kind") in ("context_compaction_applied", "agent_result")
                       for fact in history["runtime_facts"])
        with pytest.raises(ContractValidationError):
            service.control(failed["workflow_session_id"], action="retry_acceptance",
                            expected_revision=failed["revision"], idempotency_key=str(uuid4()))
        assert len(fixture.calls) == 1


def test_native_update_proves_actual_policy_even_when_no_compaction_was_triggered(tmp_path, monkeypatch):
    from phase1_agent.agent_executor import validate_execution_update

    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-agent")
    fixture = RecordingTransport()
    with closing(GraphWorkflowService(tmp_path / "policy-proof.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service, once=False)
        final = run(service, create(service, doc), "No need to summarize this short request")
        assert final["status"] == "succeeded", final["chains"]
        packet = node_output(final, entries["a"]["agent"], "context")
        assert not any(operation["kind"] == "compact" for operation in packet["operations"])
        model = next(node for node in doc["nodes"] if node["component_id"] == "models.source")
        prompt = node_output(final, entries["a"]["assembly"])
        binding = node_output(final, model)
        policy = node_output(final, entries["policy"])
        policy_ref = packet["compaction_policy_ref"]
        with closing(SqliteStore(service.database)) as store:
            receipts = packet["receipts"]
            facts = RuntimeFactStore(store).executor_facts(
                receipts["owner"], receipts["executor_ref"], receipts["fact_ids"])
        assert validate_execution_update(
            packet, facts, prompt, binding, policy=policy, policy_ref=policy_ref) == {
                "message_id": facts[-1]["payload"]["payload"]["final"]["message_id"],
                "value": {"text": "Native answer"},
            }
        with pytest.raises(ContractValidationError, match="Absent accepted compaction policy"):
            validate_execution_update(packet, facts, prompt, binding)
        for forgery in ("payload", "snapshot-ref", "packet-ref"):
            altered_facts, altered_packet = deepcopy(facts), deepcopy(packet)
            snapshot = altered_facts[-1]["payload"]["payload"]["snapshot"]["config"]["payload"]
            if forgery == "payload":
                snapshot["context_compaction"]["policy"]["enabled"] = False
            elif forgery == "snapshot-ref":
                snapshot["frozen_compaction_policy_ref"]["output_id"] = str(uuid4())
            else:
                altered_packet["compaction_policy_ref"]["output_id"] = str(uuid4())
            with pytest.raises(ContractValidationError, match="exact accepted input artifact"):
                validate_execution_update(
                    altered_packet, altered_facts, prompt, binding, policy=policy, policy_ref=policy_ref)


def test_native_type_contracts_reopen_in_independent_processes_with_different_hash_seeds(tmp_path):
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    script = f"import sys; sys.path.insert(0, {source_root!r})\n" + """
from contextlib import closing
from phase1_agent.contract_json import content_digest
from phase1_agent.graph_service import GraphWorkflowService

with closing(GraphWorkflowService(sys.argv[1])) as service:
    capacity = service.registry.data_types.get("MODEL_BINDING", 2, scope="content").schema[
        "properties"]["capacity"]
    assert capacity["required"] == [
        "context_window_tokens", "output_reserve_tokens", "summary_max_tokens", "max_cold_input_tokens"]
    print(content_digest({
        "data_types": service.registry.data_types.catalog(),
        "nodes": service.registry.catalog(),
        "services": service.registry.services.catalog(),
        "information_sources": service.registry.information_sources.catalog(),
        "package_lock": list(service.registry.package_lock),
    }))
"""
    digests = []
    database = tmp_path / "cross-process-native-contracts.sqlite"
    for seed in ("1", "2"):
        completed = subprocess.run(
            [sys.executable, "-c", script, str(database)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True, capture_output=True, check=True, timeout=30,
        )
        digests.append(completed.stdout.strip())
    assert digests[0].startswith("json-v1:sha256:") and digests[0] == digests[1]
