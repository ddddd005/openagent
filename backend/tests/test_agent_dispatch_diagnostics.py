"""Sanitized pre-dispatch causes survive Agent execution without retries."""

from contextlib import closing
import json

import pytest

from phase1_agent.contract_errors import ModelRequestError, PublicDiagnostic
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.runtime import RunFailed
from test_models_service_integration import ModelDatabaseFixture
from test_graph_service import create
from test_runtime import ScriptedAdapter, execute
from workflow_test_support import AgentTransportFixture, graph, run


def test_kernel_keeps_explicit_public_diagnostic_and_does_not_retry():
    safe = PublicDiagnostic({"code": "model_not_dispatched", "cause_code": "model_credential_changed",
                             "step": "dispatch_authorization", "recovery": "save_provider_key"})
    adapter = ScriptedAdapter(ModelRequestError("model_not_dispatched", public_diagnostic=safe))
    with pytest.raises(RunFailed) as caught:
        execute(adapter)
    assert caught.value.code == "model_not_dispatched"
    assert caught.value.public_diagnostic.to_dict() == safe.to_dict()
    assert len(adapter.inputs) == 1


def test_agent_pre_dispatch_failure_persists_safe_cause_and_recovery(tmp_path, monkeypatch):
    fixture = AgentTransportFixture()

    def revoke_before_dispatch(**arguments):
        transport = fixture.factory(**arguments)
        ModelDatabaseFixture.set_credential(service, "rotated-private-secret")
        return transport

    with closing(GraphWorkflowService(tmp_path / "diagnostics.sqlite",
                                      public_model_factory=revoke_before_dispatch)) as service:
        ModelDatabaseFixture().write(service, 1)
        failed = run(service, create(service, graph(service)), "Test diagnostic propagation")
        assert failed["status"] == "failed", failed
        diagnostic = next(node["diagnostic"] for node in failed["nodes"] if node["status"] == "failed")
        assert diagnostic["code"] == "model_not_dispatched"
        assert diagnostic["cause_code"] == "model_credential_changed"
        assert diagnostic["step"] == "dispatch_authorization"
        assert diagnostic["recovery"] == "save_provider_key"
        history = service.get_run(failed["workflow_session_id"], failed["active_chain_run_id"])
        outcome = next(fact["details"] for fact in reversed(history["runtime_facts"])
                       if fact.get("kind") == "workflow.model-fact" and fact.get("stage") == "outcome")
        assert outcome["classification"] == "not_dispatched"
        assert outcome["diagnostic"]["cause_code"] == diagnostic["cause_code"]
        assert not fixture.calls
        assert "private-api-secret" not in json.dumps([failed, history])
        assert "rotated-private-secret" not in json.dumps([failed, history])
