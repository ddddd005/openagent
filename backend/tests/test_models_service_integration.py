"""Real public graph storage with local model mocks and current resources."""

from contextlib import closing
from copy import deepcopy
import json
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.contracts import ModelResponse
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.model_contract import CHAT_PROVIDER_TYPE

from test_model_package import source_config, uid
from test_prompt_package import graph_document, graph_edge, graph_node, member


class ModelDatabaseFixture:
    def __init__(self):
        self.factories, self.calls = [], []
        self.closes = 0
        self.fail = False

    def factory(self, *, provider, parameters, api_key):
        self.factories.append({"provider": deepcopy(provider), "parameters": deepcopy(parameters)})
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                if fixture.fail:
                    raise RuntimeError("private-api-secret")
                return ModelResponse("stop", provider["base_url"], usage={"completion_tokens": 1},
                                     model=parameters["model"], response_id="mock-response")

            def close(self):
                fixture.closes += 1

        return Transport()

    @staticmethod
    def write(service, sequence, *, address="https://original.test", enabled=True):
        record = {
            **source_config()["reference"], "data_schema_version": 1, "update_sequence": sequence,
            "value": {"name": "Test current provider", "protocol": "chat", "base_url": address,
                      "credential_ref": "env:DEEPSEEK_API_KEY", "enabled": enabled},
        }
        service.save_global_resource(record, expected_sequence=sequence - 1, idempotency_key=str(uuid4()))
        return record

    @staticmethod
    def document(service, *, gate=None, side_effect=None):
        registry = service.registry
        source = graph_node(registry, "models.source", 10, **source_config())
        group = graph_node(registry, "prompts.group", 11, members=[member(101, "Say hello")])
        assembly = graph_node(registry, "prompts.assembly", 12)
        chat = graph_node(registry, "models.chat", 13)
        source["public_outputs"], chat["public_outputs"] = ["output"], ["output"]
        nodes = [source, group, assembly, chat]
        edges = [graph_edge(1001, group, assembly),
                 graph_edge(1002, source, chat, target_port="model")]
        if gate:
            entered, released = gate

            def block(config, inputs, context):
                entered.set()
                assert released.wait(10), "Test did not release the model gate"
                return {"output": inputs["input"]}

            registry.register(NodeDefinition(
                "test.model-gate", "1", "Model gate", "Test", {},
                {"type": "object", "additionalProperties": False},
                inputs=(NodePort("input", "PROMPT", data_schema_version=2),),
                outputs=(NodePort("output", "PROMPT", data_schema_version=2),),
                input_storage="references",
            ), block)
            gated = graph_node(registry, "test.model-gate", 14)
            nodes.append(gated)
            edges.extend([graph_edge(1003, assembly, gated),
                          graph_edge(1004, gated, chat, target_port="prompt")])
        else:
            edges.append(graph_edge(1003, assembly, chat, target_port="prompt"))
        doc = graph_document(nodes, edges)
        if side_effect is not None:
            registry.register(NodeDefinition(
                "test.model-preflight-effect", "1", "Effect", "Test", {},
                {"type": "object", "additionalProperties": False},
                outputs=(NodePort("output", "TEXT"),), is_output=True,
            ), lambda config, inputs, context: side_effect.append("effect") or {
                "output": {"schema_version": 1, "kind": "workflow.text", "text": "effect"}})
            doc["nodes"].append(graph_node(registry, "test.model-preflight-effect", 9))
        return doc

    @staticmethod
    def create(service, document):
        service.save_definition(document, expected_revision=0, idempotency_key=str(uuid4()))
        return service.create_session(document["workflow_definition_id"], 1, idempotency_key=str(uuid4()))

    @staticmethod
    def execute(service, view):
        started = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                                idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        return service.get_session(view["workflow_session_id"])

    @staticmethod
    def result(view):
        return next(node["outputs"]["output"] for node in view["nodes"]
                    if node["node_binding_id"] == uid(13))


def test_default_loaded_model_graph_persists_secret_free_facts_and_releases_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-api-secret")
    fixture = ModelDatabaseFixture()
    path = tmp_path / "models.sqlite"
    with closing(GraphWorkflowService(path, public_model_factory=fixture.factory)) as service:
        assert service.registry.get("models.chat", "1") is not None
        assert service.registry.data_types.get(CHAT_PROVIDER_TYPE, 1, scope="global") is not None
        fixture.write(service, 1)
        final = fixture.execute(service, fixture.create(service, fixture.document(service)))
        assert final["status"] == "succeeded", final
        assert fixture.result(final)["content"] == "https://original.test"
        history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
        assert [fact["stage"] for fact in history["runtime_facts"]] == ["request", "attempt", "outcome"]
        assert len(fixture.calls) == 1
        assert fixture.closes == 1
        assert not service._service_runs
        assert not hasattr(service, "_native_runtime")
        encoded = canonical_bytes(history).decode("utf-8")
        assert "private-api-secret" not in encoded
        assert "credential_evidence" not in encoded
        assert "credential_ref" not in encoded
        assert "https://original.test" not in canonical_bytes(history["chain"]["inputs"]).decode("utf-8")
        chat_run = next(run for run in history["node_runs"] if run["node_binding_id"] == uid(13))
        assert chat_run["input_values"] == {}
        assert history["runtime_facts"][0]["details"]["input_refs"] == chat_run["input_refs"]
    with closing(GraphWorkflowService(path, public_model_factory=fixture.factory)) as service:
        assert service.get_run(final["workflow_session_id"], final["selected_chain_run_id"]) == history
        assert not service._service_runs


def test_missing_credential_preflight_rolls_back_before_any_graph_effect(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    fixture, effects = ModelDatabaseFixture(), []
    with closing(GraphWorkflowService(tmp_path / "preflight.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        initial = fixture.create(service, fixture.document(service, side_effect=effects))
        with pytest.raises(ContractValidationError) as caught:
            service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                          idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "model_credential_unavailable"
        assert service.get_session(initial["workflow_session_id"]) == initial
        assert not effects and not fixture.calls and not fixture.factories
        assert not service._resource_frames
        assert not service._execution_registries
        assert not service._service_runs


def test_pause_preserves_frozen_provider_next_run_and_fork_use_current(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-api-secret")
    fixture = ModelDatabaseFixture()
    entered, released = Event(), Event()
    with closing(GraphWorkflowService(tmp_path / "paused.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        initial = fixture.create(service, fixture.document(service, gate=(entered, released)))
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        try:
            assert entered.wait(10)
            current = service.get_session(initial["workflow_session_id"])
            service.control(current["workflow_session_id"], action="pause",
                            expected_revision=current["revision"], idempotency_key=str(uuid4()))
        finally:
            released.set()
        service.wait(started["active_chain_run_id"])
        paused = service.get_session(initial["workflow_session_id"])
        assert paused["status"] == "paused", paused
        assert not fixture.calls
        assert sum(instance.active_frame_count for instance in service.host_service_instances(
            started["active_chain_run_id"]) if hasattr(instance, "active_frame_count")) == 1
        fixture.write(service, 2, address="https://changed.test")
        resumed = service.control(paused["workflow_session_id"], action="resume",
                                  expected_revision=paused["revision"], idempotency_key=str(uuid4()))
        service.wait(resumed["active_chain_run_id"])
        first = service.get_session(initial["workflow_session_id"])
        assert first["status"] == "succeeded", first
        assert fixture.result(first)["content"] == "https://original.test"
        candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]["candidate_id"]
        second = fixture.execute(service, first)
        assert fixture.result(second)["content"] == "https://changed.test"
        fork = service.fork_graph_candidate(
            second["workflow_session_id"], candidate_id=candidate,
            expected_revision=second["revision"], expected_data_revision=second["data_revision"],
            expected_head_revision=second["head_revision"], idempotency_key=str(uuid4()))
        child = fixture.execute(service, fork)
        assert fixture.result(child)["content"] == "https://changed.test"
        assert fixture.result(first)["binding_id"] != fixture.result(child)["binding_id"]
        assert len(fixture.calls) == 3
        assert fixture.closes == 3
        assert not service._service_runs


@pytest.mark.parametrize("policy_change", ["disabled", "credential_changed", "credential_missing"])
def test_policy_changed_before_dispatch_has_not_dispatched_fact_and_releases_frame(tmp_path, monkeypatch, policy_change):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-api-secret")
    fixture = ModelDatabaseFixture()
    entered, released = Event(), Event()
    with closing(GraphWorkflowService(tmp_path / "revoked.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        initial = fixture.create(service, fixture.document(service, gate=(entered, released)))
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        try:
            assert entered.wait(10)
            if policy_change == "disabled":
                fixture.write(service, 2, enabled=False)
            elif policy_change == "credential_changed":
                monkeypatch.setenv("DEEPSEEK_API_KEY", "rotated-private-secret")
            else:
                monkeypatch.delenv("DEEPSEEK_API_KEY")
        finally:
            released.set()
        service.wait(started["active_chain_run_id"])
        failed = service.get_session(initial["workflow_session_id"])
        assert failed["status"] == "failed", failed
        history = service.get_run(failed["workflow_session_id"], started["active_chain_run_id"])
        assert history["runtime_facts"][-1]["details"]["classification"] == "not_dispatched"
        assert not fixture.calls
        assert not service._service_runs
        assert "private-api-secret" not in json.dumps(history)
        assert "rotated-private-secret" not in json.dumps(history)


def test_unknown_transport_failure_persists_once_and_disposes_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-api-secret")
    fixture = ModelDatabaseFixture()
    fixture.fail = True
    with closing(GraphWorkflowService(tmp_path / "unknown.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        failed = fixture.execute(service, fixture.create(service, fixture.document(service)))
        assert failed["status"] == "failed", failed
        history = service.get_run(failed["workflow_session_id"], failed["active_chain_run_id"])
        assert history["runtime_facts"][-1]["details"]["classification"] == "dispatch_unknown"
        assert len(fixture.calls) == 1 and fixture.closes == 1
        assert not service._service_runs
        assert "private-api-secret" not in json.dumps(history)


def test_current_provider_write_cas_and_idempotency_keep_only_current_value(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-api-secret")
    fixture = ModelDatabaseFixture()
    with closing(GraphWorkflowService(tmp_path / "cas.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        first = fixture.write(service, 1)
        command = {"expected_sequence": 1, "idempotency_key": str(uuid4())}
        second = {**first, "update_sequence": 2,
                  "value": {**first["value"], "base_url": "https://changed.test"}}
        receipt = service.save_global_resource(second, **command)
        assert service.save_global_resource(second, **command) == receipt
        with pytest.raises(ContractValidationError) as caught:
            service.save_global_resource(second, expected_sequence=1, idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "stale_revision"
        assert service.get_global_resource(source_config()["reference"]) == second
