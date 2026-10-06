"""I06 frozen settings reach the wire across retry, resume, and reroll."""

from __future__ import annotations

import copy
import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType

import httpx
import pytest

from phase1_agent.adapter import DeepSeekAdapter
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frozen_model import (
    FrozenConfiguredAdapter, FrozenModelParameters, create_configured_adapter,
)
from phase1_agent.runtime import CanonicalModelAdapter
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="frozen-model-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def final_payload(stage, index):
    return {
        "id": f"completion-{stage}-{index}", "object": "chat.completion", "created": 1,
        "model": "provider-resolved-model", "choices": [{
            "index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": f"final-{stage}-{index}", "type": "function",
                    "function": {"name": "final_answer", "arguments": json.dumps({
                        "answer": {"text": f"result {stage} {index}"},
                    })},
                }],
            },
        }],
    }


def live_settings(service, stage, **updates):
    context, kernel, adapter, config = service._resolved[stage]
    changed = copy.deepcopy(config)
    changed["payload"]["adapter"].update(updates)
    service._resolved[stage] = context, kernel, adapter, changed


def model_settings(body):
    return {key: body[key] for key in ("model", "max_tokens", "temperature", "stream", "thinking")
            if key in body}


def ledger(database, run_id):
    with closing(SqliteStore(database)) as store:
        return store.read_execution_facts(run_id)


def factory_with_wire(captured, handler=None, configurations=None):
    def factory(stage, parameters):
        assert isinstance(parameters, MappingProxyType)
        if configurations is not None:
            configurations.append((stage, dict(parameters)))

        def transport(request):
            body = json.loads(request.content)
            captured.append((stage, body))
            if handler is not None:
                response = handler(stage, body)
                if response is not None:
                    return response
            return httpx.Response(200, json=final_payload(stage, len(captured)))

        return CanonicalModelAdapter(DeepSeekAdapter(
            "offline-dummy-key", model=parameters["model"],
            max_tokens=parameters.get("max_tokens"), temperature=parameters.get("temperature"),
            max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        ))
    return factory


def test_configured_factory_receives_detached_immutable_effective_parameters():
    source = {"model": "frozen", "max_tokens": 321, "temperature": 0.6}
    frozen = FrozenModelParameters.from_mapping(source)
    source.update(model="changed", max_tokens=999)
    received = []

    def factory(stage, parameters):
        received.append((stage, parameters))
        with pytest.raises(TypeError):
            parameters["model"] = "changed"
        return object()

    create_configured_adapter(factory, "A", frozen, legacy_defaults=frozen)
    assert dict(received[0][1]) == {
        "model": "frozen", "max_tokens": 321, "temperature": 0.6,
        "thinking": "disabled", "stream": False,
    }


def test_legacy_one_argument_factory_is_explicitly_default_only(database):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        model = service._new_adapter("A", {
            "model": "offline_fixture", "max_tokens": 2048,
            "thinking": "disabled", "stream": False,
        })
        assert isinstance(model.implementation, OfflineAdapter)
        with pytest.raises(ContractValidationError, match="only fixed default"):
            service._new_adapter("A", {"model": "changed", "max_tokens": 2048})
    assert calls == ["A"]


def test_factory_typeerror_is_not_mistaken_for_legacy_compatibility(database):
    calls = []

    def factory(stage, parameters):
        calls.append(stage)
        raise TypeError("factory implementation failed")

    with closing(WorkflowService(database, model_factory=factory)) as service:
        with pytest.raises(TypeError, match="implementation failed"):
            service._new_adapter("A", {"model": "offline_fixture", "max_tokens": 2048})
    assert calls == ["A"]


def test_declared_custom_adapter_drift_is_rejected_before_another_dispatch():
    calls = []

    class DeclaredAdapter:
        model_parameters = {"model": "frozen", "max_tokens": 321}

        def generate(self, messages, tools):
            calls.append((messages, tools))
            return object()

    implementation = DeclaredAdapter()
    adapter = FrozenConfiguredAdapter(
        implementation, FrozenModelParameters("frozen", 321),
    )
    adapter.generate([], [])
    implementation.model_parameters = {"model": "changed", "max_tokens": 999}
    with pytest.raises(ContractValidationError, match="disagrees"):
        adapter.generate([], [])
    assert calls == [([], [])]


def test_configured_adapter_drift_during_retry_is_a_program_fault(database):
    captured = []

    class MutableAdapter:
        def __init__(self, stage, parameters):
            self.model_parameters = dict(parameters)
            self.wire = factory_with_wire(captured, self.fail_after_mutation)(stage, parameters)

        def fail_after_mutation(self, stage, body):
            self.model_parameters["model"] = "changed"
            return httpx.Response(503, json={"error": {"message": "temporary"}})

        def generate(self, messages, tools):
            return self.wire.generate(messages, tools)

        def close(self):
            self.wire.close()

    with closing(WorkflowService(database, model_factory=MutableAdapter)) as service:
        service._resolved["A"][1].implementation._backoff = lambda retry: None
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "prevent drift", "input")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        assert failed["error"]["code"] == "adapter_contract_error"
        assert "resume" not in failed["available_actions"]
        failures = [
            row["payload"] for row in ledger(database, failed["nodes"][0]["run_id"])
            if row["kind"] == "execution_failed"
        ]
        assert failures[-1]["category"] == "contract"
    assert len(captured) == 1


@pytest.mark.parametrize("change", [
    {"stream": True}, {"thinking": "enabled"}, {"unknown_setting": 1},
    {"max_tokens": True}, {"temperature": float("nan")},
])
def test_invalid_frozen_settings_never_construct_or_dispatch_model(database, change):
    calls = []
    with closing(WorkflowService(
        database, model_factory=lambda stage, parameters: calls.append(stage),
    )) as service:
        with pytest.raises(ContractValidationError):
            service._new_adapter("A", {
                "model": "offline_fixture", "max_tokens": 2048, **change,
            })
    assert calls == []


def test_discrepant_factory_adapter_is_rejected_before_http_dispatch(database):
    captured = []

    def factory(stage, parameters):
        return CanonicalModelAdapter(DeepSeekAdapter(
            "offline-dummy-key", model="wrong-model", max_tokens=2048, max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(
                lambda request: captured.append(request) or httpx.Response(500)
            )),
        ))

    with closing(WorkflowService(database, model_factory=factory)) as service:
        with pytest.raises(ContractValidationError, match="disagrees"):
            service._new_adapter("A", {"model": "offline_fixture", "max_tokens": 2048})
    assert captured == []


def test_default_deepseek_path_uses_frozen_parameters_and_zero_sdk_retries(
    database, monkeypatch,
):
    captured, constructed = [], []
    original_adapter = DeepSeekAdapter

    class MockAdapter(original_adapter):
        def __init__(self, **kwargs):
            constructed.append(copy.deepcopy(kwargs))
            client = httpx.Client(transport=httpx.MockTransport(
                lambda request: captured.append(json.loads(request.content))
                or httpx.Response(200, json=final_payload("wire", len(captured)))
            ))
            super().__init__(**kwargs, http_client=client)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-dummy-key")
    monkeypatch.setattr("phase1_agent.adapter.DeepSeekAdapter", MockAdapter)
    with closing(WorkflowService(database, mode="deepseek")) as service:
        model = service._new_adapter("A", {
            "model": "frozen-real-path", "max_tokens": 171, "temperature": 0.45,
            "thinking": "disabled", "stream": False,
        })
        try:
            model.implementation.legacy_adapter.generate(
                [{"kind": "text", "role": "user", "content": "no network"}], [],
            )
        finally:
            model.close()
    assert constructed[0]["max_retries"] == 0
    assert model_settings(captured[0]) == {
        "model": "frozen-real-path", "max_tokens": 171, "temperature": 0.45,
        "stream": False, "thinking": {"type": "disabled"},
    }


def test_logical_retry_keeps_frozen_wire_after_live_configuration_changes(database):
    captured = []
    service = None

    def handler(stage, body):
        if stage == "A" and len([row for row in captured if row[0] == "A"]) == 1:
            live_settings(service, "A", model="changed-live", max_tokens=777, temperature=1.8)
            return httpx.Response(503, json={"error": {"message": "temporary"}})
        return None

    with closing(WorkflowService(
        database, model_factory=factory_with_wire(captured, handler),
    )) as service:
        service._resolved["A"][1].implementation._backoff = lambda retry: None
        sid = service.create_session()["workflow_session_id"]
        receipt = service.submit(sid, "freeze retry", "input")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        a_bodies = [body for stage, body in captured if stage == "A"]
        assert len(a_bodies) == 2
        assert a_bodies[0] == a_bodies[1]
        assert model_settings(a_bodies[0]) == {
            "model": "offline_fixture", "max_tokens": 2048,
            "stream": False, "thinking": {"type": "disabled"},
        }
        with closing(SqliteStore(database)) as store:
            chain = store.get_record("chain_run", {"chain_run_id": receipt["chain_run_id"]})
        facts = ledger(database, chain["node_run_ids"][0])
        assert len([row for row in facts if row["kind"] == "model_request"]) == 1
        assert len([row for row in facts if row["kind"] == "model_attempt_started"]) == 2


def test_nondefault_snapshot_model_parameters_are_the_actual_wire_settings(database):
    captured = []
    with closing(WorkflowService(database, model_factory=factory_with_wire(captured))) as service:
        live_settings(service, "A", model="nondefault-frozen-a", max_tokens=171, temperature=0.6)
        sid = service.create_session()["workflow_session_id"]
        receipt = service.submit(sid, "nondefault freeze", "input")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        with closing(SqliteStore(database)) as store:
            chain = store.get_record("chain_run", {"chain_run_id": receipt["chain_run_id"]})
            run = store.get_record("run_record", {"run_id": chain["node_run_ids"][0]})
            snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
        a_body = next(body for stage, body in captured if stage == "A")
        assert a_body["model"] == snapshot["model_parameters"]["model"] == "nondefault-frozen-a"
        assert a_body["max_tokens"] == snapshot["model_parameters"]["max_tokens"] == 171
        assert a_body["temperature"] == snapshot["model_parameters"]["temperature"] == 0.6


def test_snapshot_model_parameters_disagreeing_with_config_are_rejected(database):
    captured, configurations = [], []
    with closing(WorkflowService(database, model_factory=factory_with_wire(
        captured, configurations=configurations,
    ))) as service:
        sid = service.create_session()["workflow_session_id"]
        receipt = service.submit(sid, "consistent evidence", "input")
        service.wait_for_idle(sid)
        with closing(SqliteStore(database)) as store:
            chain = store.get_record("chain_run", {"chain_run_id": receipt["chain_run_id"]})
            run = store.get_record("run_record", {"run_id": chain["node_run_ids"][0]})
            snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
        snapshot["model_parameters"]["model"] = "contradictory-model"
        with pytest.raises(ContractValidationError, match="disagree"):
            service._snapshot_components(snapshot)
    assert len(captured) == len(configurations) == 2


def test_same_run_model_resume_reuses_original_frozen_wire(database):
    captured, configurations = [], []
    broken = True

    def handler(stage, body):
        if stage == "A" and broken:
            return httpx.Response(503, json={"error": {"message": "temporary"}})
        return None

    with closing(WorkflowService(database, model_factory=factory_with_wire(
        captured, handler, configurations,
    ))) as service:
        service._resolved["A"][1].implementation._backoff = lambda retry: None
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "freeze resume", "input")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        assert failed["error"]["code"] == "model_retry_exhausted"
        run_id = failed["nodes"][0]["run_id"]
        assert len([row for row in captured if row[0] == "A"]) == 4
        live_settings(service, "A", model="changed-live", max_tokens=999)
        broken = False
        service.resume(sid, run_id, idempotency_key="resume",
                       expected_session_revision=failed["revision"],
                       expected_run_revision=failed["nodes"][0]["revision"])
        service.wait_for_idle(sid)
        finished = service.get_session(sid)
        assert finished["error"] is None
        assert finished["nodes"][0]["run_id"] == run_id
        assert finished["chains"] == [{
            "chain_run_id": submitted["chain_run_id"], "status": "succeeded",
        }]
        a_parameters = [parameters for stage, parameters in configurations if stage == "A"]
        assert len(a_parameters) == 2 and a_parameters[0] == a_parameters[1]
        a_settings = [model_settings(body) for stage, body in captured if stage == "A"]
        assert len(a_settings) == 5 and all(settings == a_settings[0] for settings in a_settings)


def test_reroll_freezes_both_original_node_settings_not_live_configuration(database):
    captured, configurations = [], []
    with closing(WorkflowService(database, model_factory=factory_with_wire(
        captured, configurations=configurations,
    ))) as service:
        sid = service.create_session()["workflow_session_id"]
        source = service.submit(sid, "freeze whole chain", "input")
        service.wait_for_idle(sid)
        previous = service.get_session(sid)
        assert previous["error"] is None
        live_settings(service, "A", model="changed-a", max_tokens=111)
        live_settings(service, "B", model="changed-b", max_tokens=222)
        service.reroll(
            sid, source["chain_run_id"], idempotency_key="reroll",
            expected_session_revision=previous["revision"],
            expected_ref_revision=previous["ref_revision"],
            expected_head_commit_id=previous["head_commit_id"],
        )
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        assert [stage for stage, _ in configurations] == ["A", "B", "A", "B"]
        assert configurations[:2] == configurations[2:]
        for stage in ("A", "B"):
            settings = [model_settings(body) for owner, body in captured if owner == stage]
            assert len(settings) == 2 and settings[0] == settings[1]
