"""Preparation-frame failures must not degrade into a static-tool Agent.

These tests cover the review finding that the Agent's own ``specs`` bridge
treated a missing chain frame the same as a legitimate empty binding set.
"""

from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError

from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from test_variable_tool_bindings import (
    VariableToolTransport, _registered, _run, _service, build_graph,
)
from workflow_test_support import graph


def test_missing_frame_fails_at_agent_specs_before_the_model(monkeypatch, tmp_path):
    transport = VariableToolTransport([("final_answer", {"answer": {"text": "never"}})])
    with closing(_service(tmp_path / "missing-frame.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, parts = build_graph(service)
        session = _registered(service, document)
        original = service._tool_bridge

        def bridge(context, operation, payload):
            if operation == "specs" and context.node_binding_id == parts["execute"]["node_binding_id"]:
                service._tool_frames.pop(context.chain_run_id, None)
            return original(context, operation, payload)

        monkeypatch.setattr(service, "_tool_bridge", bridge)
        chain_id, view = _run(service, session, text="missing frame")
        assert view["status"] == "failed", view["chains"]
        assert view["chains"][-1]["diagnostic"]["code"] == "tool_binding_not_prepared"
        assert transport.calls == []


def test_frame_without_business_bindings_still_allows_empty_specs(tmp_path):
    transport = VariableToolTransport([("final_answer", {"answer": {"text": "done"}})])
    with closing(_service(tmp_path / "empty-frame.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        session = create(service, graph(service))
        started = service.start(
            session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key=str(uuid4()), inputs={"text": "no business tools"})
        service.wait(started["active_chain_run_id"])
        view = service.get_session(session["workflow_session_id"])
        assert view["status"] == "succeeded", view["chains"]
        assert len(transport.calls) == 1
        assert "score_read" not in {tool["function"]["name"]
                                   for tool in transport.calls[0]["tools"]}


def test_non_described_node_cannot_read_an_agent_tool_set(monkeypatch, tmp_path):
    transport = VariableToolTransport([("final_answer", {"answer": {"text": "never"}})])
    with closing(_service(tmp_path / "foreign-descriptor.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, parts = build_graph(service)
        session = _registered(service, document)
        monkeypatch.setattr(service._executor, "submit", lambda *args, **kwargs: None)
        started = service.start(
            session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key=str(uuid4()), inputs={"text": "prepared only"})
        assert service.get_session(session["workflow_session_id"])["status"] == "prepared"
        context = SimpleNamespace(
            chain_run_id=started["active_chain_run_id"],
            node_binding_id=parts["assembly"]["node_binding_id"],
            workflow_session_id=session["workflow_session_id"], node_run_id=str(uuid4()))
        with pytest.raises(ContractValidationError) as caught:
            service._bound_tool_specs(
                context, {"agent_node_id": parts["execute"]["node_binding_id"]})
        assert caught.value.reason_code == "tool_binding_access_denied"
