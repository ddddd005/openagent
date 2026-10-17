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

from test_model_package import profile_config, source_config, uid
from test_prompt_package import graph_document, graph_edge, graph_node, member
from credential_test_support import mutate_test_credential, seed_service_credential


class ModelDatabaseFixture:
    def __init__(self):
        self.factories, self.calls, self.credential_generations = [], [], []
        self.closes = 0
        self.fail = False
        self.response = None

    def factory(self, *, provider, parameters, api_key):
        self.factories.append({"provider": deepcopy(provider), "parameters": deepcopy(parameters)})
        self.credential_generations.append(
            "original" if api_key == "private-api-secret"
            else "rotated" if api_key == "rotated-private-secret" else "other")
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                if fixture.fail:
                    raise RuntimeError("private-api-secret")
                if fixture.response is not None:
                    return deepcopy(fixture.response)
                return ModelResponse("stop", provider["base_url"], usage={"completion_tokens": 1},
                                     model=parameters["model"], response_id="mock-response")

            def generate_prepared(self, prepared):
                wire = prepared["wire_request"]
                return self.generate(wire["messages"], wire.get("tools", []))

            def close(self):
                fixture.closes += 1

        return Transport()

    @staticmethod
    def write(service, sequence, *, address="https://original.test", enabled=True):
        reference = seed_service_credential(service)
        record = {
            **profile_config()["provider"], "data_schema_version": 3, "update_sequence": sequence,
            "value": {"name": "Test current provider", "protocol": "chat", "base_url": address,
                      "credential_ref": reference, "enabled": enabled},
        }
        service.save_global_resource(record, expected_sequence=sequence - 1, idempotency_key=str(uuid4()))
        if service.get_global_resource(source_config()["reference"]) is None:
            saved_profile = {
                "name": "Unit model configuration", "provider": profile_config()["provider"],
                "model": {"id": "deepseek-test", "capacity": {
                    "context_window_tokens": 400_000, "input_limit_tokens": 300_000,
                    "max_output_tokens": 32_000,
                }},
                "runtime": {"thinking": "disabled", "context_length_tokens": 300_000,
                            "compaction_threshold_bps": 8500, "output_limit_tokens": 32_000},
                "enabled": True,
            }
            service.save_global_resource({
                **source_config()["reference"], "data_schema_version": 2, "update_sequence": 1,
                "value": saved_profile,
            }, expected_sequence=0, idempotency_key=str(uuid4()))
        return record

    @staticmethod
    def set_credential(service, secret):
        record = service.get_global_resource(profile_config()["provider"])
        return mutate_test_credential(service.provider_configuration.credentials,
                                      record["value"]["credential_ref"], secret)

    @staticmethod
    def update_profile(service, *, model=None, capacity=None, runtime=None):
        record = service.get_global_resource(source_config()["reference"])
        assert record is not None
        record["update_sequence"] += 1
        if model:
            record["value"]["model"].update(model)
        if capacity:
            record["value"]["model"]["capacity"].update(capacity)
        if runtime:
            record["value"]["runtime"].update(runtime)
        service.save_global_resource(record, expected_sequence=record["update_sequence"] - 1,
                                     idempotency_key=str(uuid4()))
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
        from test_graph_service import save_definition_fixture
        save_definition_fixture(service, document)
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
    fixture = ModelDatabaseFixture()
    path = tmp_path / "models.sqlite"
    with closing(GraphWorkflowService(path, public_model_factory=fixture.factory)) as service:
        assert service.registry.get("models.chat", "5") is not None
        assert service.registry.data_types.get(CHAT_PROVIDER_TYPE, 3, scope="global") is not None
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
    monkeypatch.setenv("DEEPSEEK_API_KEY", "retired-route-must-not-rescue-missing-key")
    fixture, effects = ModelDatabaseFixture(), []
    with closing(GraphWorkflowService(tmp_path / "preflight.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        provider = service.get_global_resource(profile_config()["provider"])
        credential_ref = provider["value"]["credential_ref"]
        fixture.set_credential(service, None)
        assert service.provider_configuration.credentials.status(credential_ref)["state"] == "missing"
        initial = fixture.create(service, fixture.document(service, side_effect=effects))
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        failed = service.get_session(initial["workflow_session_id"])
        history = service.get_run(failed["workflow_session_id"], started["active_chain_run_id"])
        assert failed["status"] == "failed"
        assert history["runtime_facts"] == []
        assert not effects and not fixture.calls and not fixture.factories
        assert not service._resource_frames
        assert not service._execution_registries
        assert not service._service_runs


def test_pause_before_request_and_checkpoint_fork_use_current_profile(tmp_path, monkeypatch):
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
        assert not fixture.calls and not fixture.factories
        assert service.get_run(initial["workflow_session_id"],
                               started["active_chain_run_id"])["runtime_facts"] == []
        fixture.write(service, 2, address="https://changed.test")
        resumed = service.control(paused["workflow_session_id"], action="resume",
                                  expected_revision=paused["revision"], idempotency_key=str(uuid4()))
        service.wait(resumed["active_chain_run_id"])
        first = service.get_session(initial["workflow_session_id"])
        assert first["status"] == "succeeded", first
        assert fixture.result(first)["content"] == "https://changed.test"
        assert fixture.credential_generations == ["original"]
        second = fixture.execute(service, first)
        assert fixture.result(second)["content"] == "https://changed.test"
        assert fixture.credential_generations == ["original", "original"]
        points = service.list_checkpoints(second["workflow_session_id"])
        assert points
        fork = service.fork_checkpoint(
            second["workflow_session_id"], checkpoint_id=points[-1]["checkpoint_id"],
            expected_session_revision=second["revision"], expected_data_revision=second["data_revision"],
            expected_head_revision=second["head_revision"], idempotency_key=str(uuid4()))
        child_initial = service.get_session(fork["target_session_id"])
        child = fixture.execute(service, child_initial)
        assert fixture.result(child)["content"] == "https://changed.test"
        assert child["workflow_definition_id"] == second["workflow_definition_id"]
        assert child["definition_revision"] == second["definition_revision"]
        assert len(fixture.calls) == 3
        assert fixture.closes == 3
        assert not service._service_runs


@pytest.mark.parametrize("policy_change", ["disabled", "credential_changed", "credential_missing"])
def test_policy_change_before_first_model_request_uses_current_resource_state(tmp_path, monkeypatch, policy_change):
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
                fixture.set_credential(service, "rotated-private-secret")
            else:
                fixture.set_credential(service, None)
        finally:
            released.set()
        service.wait(started["active_chain_run_id"])
        current = service.get_session(initial["workflow_session_id"])
        history = service.get_run(current["workflow_session_id"], started["active_chain_run_id"])
        if policy_change == "credential_changed":
            assert current["status"] == "succeeded", current
            assert len(fixture.calls) == 1 and fixture.credential_generations == ["rotated"]
            assert [fact["stage"] for fact in history["runtime_facts"]] == ["request", "attempt", "outcome"]
        else:
            assert current["status"] == "failed", current
            assert history["runtime_facts"] == []
            assert not fixture.calls and not fixture.factories
        assert not service._service_runs
        assert "private-api-secret" not in json.dumps(history)
        assert "rotated-private-secret" not in json.dumps(history)


def test_unknown_transport_failure_persists_once_and_disposes_model(tmp_path, monkeypatch):
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
        assert service.get_global_resource(profile_config()["provider"]) == second
