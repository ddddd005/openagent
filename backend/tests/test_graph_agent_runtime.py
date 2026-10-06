"""Offline graph Agent adaptation retains native facts and safe control boundaries."""

import copy
import json
from dataclasses import asdict
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_agent_runtime import (
    GraphAgentCallbacks, GraphAgentIdentity, GraphAgentRuntime, GraphAgentSuspended,
    make_graph_agent_snapshot, resolve_graph_model, validate_graph_agent_accepted,
    validate_graph_model_resource,
)
from phase1_agent.model_configuration import default_provider
from phase1_agent.model_selection import verify_model_binding
from phase1_agent.runtime import KernelContractError, SnapshotKernel
from phase1_agent.prompt_assembly import PromptAssemblyLimits, assemble_prompt_collection


def uid():
    return str(uuid4())


def response(name, arguments, provider_id):
    return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
        id=provider_id, name=name, raw_arguments=json.dumps(arguments),
    ),))


def final(text="done"):
    return response("final_answer", {"answer": {"text": text}}, "final-call")


class Adapter:
    def __init__(self, steps, events):
        self.steps, self.events = list(steps), events
        self.requests = []
        self.closed = False

    def generate(self, messages, tools):
        assert self.events[-1]["kind"] == "model_attempt_started"
        self.requests.append(copy.deepcopy(messages))
        value = self.steps.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, *scripts, requests=8, attempts=32):
        self.identity = GraphAgentIdentity(*(uid() for _ in range(5)))
        self.binding = {
            "node_id": uid(), "provider": default_provider(), "credential_evidence": "1" * 64,
            "parameters": {"model": "deepseek-flash", "max_tokens": 2048,
                           "thinking": "disabled", "stream": False},
        }
        self.limits = {"max_model_requests": requests, "max_model_attempts": attempts}
        root = {"schema_version": 1, "message_id": uid(), "role": "user",
                "source": {"kind": "human", "visible_message_id": uid()},
                "blocks": [{"kind": "text", "text": "input"}]}
        assembly = assemble_prompt_collection(
            {"schema_version": 1, "kind": "prompt_collection", "items": []}, [root],
            logical_floors=[[root["message_id"]]], prompt_message_ids=[],
        )
        self.snapshot = make_graph_agent_snapshot(
            identity=self.identity, model_binding=self.binding, parent_turn_id=None,
            s0=[root], config=self.limits, input_id=uid(), evidence={
                "schema_version": 1, "kind": "workflow.prompt-assembly", "prompt": assembly,
                "current_root": root, "context_basis": [],
            },
        )
        self.scripts, self.adapters = list(scripts), []
        self.prepared, self.facts, self.archived, self.progress = [], [], [], []
        self.paused = False
        self.callbacks = GraphAgentCallbacks(
            self.prepared.append, self.facts.append, self.progress.append,
            self.archived.append, lambda: self.paused,
        )

        def factory(binding):
            assert binding == self.binding
            assert self.prepared == [self.snapshot]
            adapter = Adapter(self.scripts.pop(0), self.facts)
            self.adapters.append(adapter)
            return adapter

        self.runtime = GraphAgentRuntime(factory, kernel=SnapshotKernel(backoff=lambda _: None))

    def execute(self):
        return self.runtime.execute(identity=self.identity, snapshot=self.snapshot,
                                    model_binding=self.binding, limits=self.limits, callbacks=self.callbacks)


def test_native_multiturn_tool_facts_and_final_are_archived_before_release():
    harness = Harness([response("inspect_text", {"text": "a\nb"}, "inspect-call"), final()])
    accepted = harness.execute().to_dict()
    assert accepted == harness.archived[0]
    assert accepted["identity"] == asdict(harness.identity)
    assert accepted["turn"]["run_id"] == harness.identity.node_run_id
    assert [message["role"] for message in accepted["turn"]["messages"]] == [
        "assistant", "tool", "assistant", "tool",
    ]
    assert accepted["turn"]["messages"][1]["blocks"][0]["content"] == {"characters": 3, "lines": 2}
    assert accepted["progress"] == {"model_requests": 2, "attempts": 2, "accepted_messages": 4}
    assert harness.adapters[0].requests[1] == harness.snapshot["s0"] + accepted["turn"]["messages"][:2]
    assert harness.adapters[0].closed
    assert not harness.runtime.has_checkpoint(harness.identity.node_run_id)
    assert validate_graph_agent_accepted(accepted) == accepted
    accepted["turn"]["final"]["value"]["text"] = "tampered"
    assert harness.runtime.read_evidence(harness.identity.node_run_id)["accepted"]["turn"]["final"]["value"] == {"text": "done"}
    with pytest.raises(ContractValidationError):
        validate_graph_agent_accepted(accepted)


def test_pause_resume_retains_snapshot_scope_completed_tool_and_cumulative_facts():
    harness = Harness([response("inspect_text", {"text": "once"}, "inspect-call")], [final()])

    def progress(event):
        harness.progress.append(event)
        if event["accepted_messages"] == 2:
            harness.paused = True

    harness.callbacks = GraphAgentCallbacks(
        harness.prepared.append, harness.facts.append, progress,
        harness.archived.append, lambda: harness.paused,
    )
    with pytest.raises(GraphAgentSuspended) as caught:
        harness.execute()
    assert caught.value.status == "paused"
    assert harness.runtime.has_checkpoint(harness.identity.node_run_id)
    assert harness.archived == []
    before = harness.runtime.read_evidence(harness.identity.node_run_id)
    harness.paused = False
    package = harness.execute().to_dict()
    assert package["snapshot"] == before["snapshot"] == harness.snapshot
    assert package["turn"]["run_id"] == harness.identity.node_run_id
    assert package["facts"][:len(before["facts"])] == before["facts"]
    assert [fact["sequence"] for fact in package["facts"]] == list(range(1, len(package["facts"]) + 1))
    assert len({fact["generation"] for fact in package["facts"]}) == 2
    assert sum(fact["kind"] == "tool_dispatch" for fact in package["facts"]) == 2
    assert harness.prepared == [harness.snapshot]
    assert len(harness.adapters) == 2
    assert package["progress"]["model_requests"] == 2


def test_budget_extension_preserves_frozen_snapshot_and_accepted_progress():
    harness = Harness([response("inspect_text", {"text": "once"}, "inspect-call")], [final()], requests=1, attempts=1)
    with pytest.raises(GraphAgentSuspended) as caught:
        harness.execute()
    assert caught.value.status == "budget_exhausted"
    evidence = harness.runtime.read_evidence(harness.identity.node_run_id)
    assert evidence["progress"] == {"model_requests": 1, "attempts": 1, "accepted_messages": 2}
    preview = harness.runtime.preview_budget(harness.identity.node_run_id, requests=1, attempts=1)
    assert preview == {"max_model_requests": 2, "max_model_attempts": 2}
    assert harness.runtime.read_evidence(harness.identity.node_run_id)["limits"] == harness.limits
    harness.limits = harness.runtime.extend_budget(harness.identity.node_run_id, requests=1, attempts=1)
    result = harness.execute().to_dict()
    assert result["limits"] == {"max_model_requests": 2, "max_model_attempts": 2}
    assert result["snapshot"]["config"]["payload"]["graph_agent"]["max_model_requests"] == 1
    assert result["progress"]["model_requests"] == 2
    with pytest.raises(ContractValidationError):
        harness.runtime.extend_budget(harness.identity.node_run_id, requests=1, attempts=0)


def test_archive_retry_reuses_exact_accepted_package_without_factory_or_dispatch():
    harness = Harness([final()])
    packages = []

    def fail_archive(package):
        packages.append(package)
        raise OSError("offline archive failure")

    harness.callbacks = GraphAgentCallbacks(harness.prepared.append, harness.facts.append,
                                            harness.progress.append, fail_archive)
    with pytest.raises(GraphAgentSuspended) as caught:
        harness.execute()
    assert caught.value.status == "archive_failed"
    assert not harness.runtime.has_checkpoint(harness.identity.node_run_id)
    harness.callbacks = GraphAgentCallbacks(harness.prepared.append, harness.facts.append,
                                            harness.progress.append, harness.archived.append)
    retried = harness.runtime.retry_archive(harness.identity.node_run_id, callbacks=harness.callbacks).to_dict()
    assert retried == packages[0] == harness.archived[0]
    assert len(harness.adapters) == len(harness.adapters[0].requests) == 1
    with pytest.raises(ContractValidationError):
        harness.execute()


def test_fact_storage_failure_prevents_model_dispatch_and_does_not_resume():
    harness = Harness([final()])

    def fail_before_dispatch(fact):
        if fact["kind"] == "model_attempt_started":
            raise OSError("private storage detail")
        harness.facts.append(fact)

    harness.callbacks = GraphAgentCallbacks(harness.prepared.append, fail_before_dispatch,
                                            harness.progress.append, harness.archived.append)
    with pytest.raises(KernelContractError) as caught:
        harness.execute()
    assert caught.value.code == "fact_persistence_error"
    assert harness.adapters[0].requests == []
    assert harness.adapters[0].closed
    assert harness.archived == []
    assert not harness.runtime.has_checkpoint(harness.identity.node_run_id)
    with pytest.raises(ContractValidationError):
        harness.execute()


def test_internal_history_growth_is_bounded_before_next_dispatch():
    harness = Harness([response("inspect_text", {"text": "once"}, "inspect-call"), final()])
    root = harness.snapshot["s0"][0]
    harness.snapshot["config"]["payload"]["graph_preparation"]["prompt"] = assemble_prompt_collection(
        {"schema_version": 1, "kind": "prompt_collection", "items": []}, [root],
        logical_floors=[[root["message_id"]]], prompt_message_ids=[],
        limits=PromptAssemblyLimits(max_messages=1),
    )
    with pytest.raises(KernelContractError) as caught:
        harness.execute()
    assert caught.value.code == "prompt_capacity_exceeded"
    assert len(harness.adapters[0].requests) == 1
    assert sum(fact["kind"] == "model_request" for fact in harness.facts) == 1
    assert harness.archived == []
    assert not harness.runtime.has_checkpoint(harness.identity.node_run_id)


def test_resume_rejects_changed_snapshot_before_factory_and_new_process_has_no_checkpoint():
    harness = Harness([final()])
    harness.paused = True
    with pytest.raises(GraphAgentSuspended):
        harness.execute()
    harness.snapshot["s0"][0]["blocks"][0]["text"] = "changed"
    with pytest.raises(ContractValidationError, match="frozen"):
        harness.execute()
    assert len(harness.adapters) == 1
    replacement = GraphAgentRuntime(lambda _: pytest.fail("unexpected adapter"))
    assert not replacement.has_checkpoint(harness.identity.node_run_id)
    with pytest.raises(ContractValidationError):
        replacement.retry_archive(harness.identity.node_run_id, callbacks=harness.callbacks)


class Catalog:
    def __init__(self):
        self.provider = default_provider()
        self.revisions = {1: copy.deepcopy(self.provider)}

    def get_current(self, kind, identity):
        assert kind == "provider"
        return copy.deepcopy(self.provider) if identity == self.provider["provider_id"] else None

    def get_revision(self, kind, identity, revision):
        assert kind == "provider"
        return copy.deepcopy(self.revisions.get(revision)) if identity == self.provider["provider_id"] else None


def test_global_current_provider_resolves_once_and_dispatch_rejects_credential_change(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-credential")
    catalog = Catalog()
    value = resolve_graph_model({"provider_id": catalog.provider["provider_id"],
                                 "parameters": {"model": "deepseek-flash", "max_tokens": 2048}},
                                catalog, node_id=uid())
    assert validate_graph_model_resource(value) == value
    assert "offline-test-credential" not in repr(value)
    assert verify_model_binding(value["binding"], catalog) == "offline-test-credential"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-replaced-credential")
    with pytest.raises(ContractValidationError) as caught:
        verify_model_binding(value["binding"], catalog)
    assert caught.value.reason_code == "credential_changed"
    catalog.provider.update(revision=2, enabled=False)
    catalog.revisions[2] = copy.deepcopy(catalog.provider)
    with pytest.raises(ContractValidationError) as caught:
        verify_model_binding(value["binding"], catalog)
    assert caught.value.reason_code == "provider_unavailable"


@pytest.mark.parametrize("config,reason", [
    ({"provider_id": None, "parameters": {"model": "deepseek-flash"}}, "provider_missing"),
    ({"provider_id": default_provider()["provider_id"],
      "parameters": {"model": "deepseek-flash", "max_tokens": 8193}}, "model_parameters_unsupported"),
])
def test_provider_failures_are_typed_without_model_initialization(config, reason):
    with pytest.raises(ContractValidationError) as caught:
        resolve_graph_model(config, Catalog(), node_id=uid())
    assert caught.value.reason_code == reason
