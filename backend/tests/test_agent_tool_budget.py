"""The Agent's real merged tool set is explicitly bounded before transport.

Static package tools, prepared bound business tools and the single merged
``final_answer`` control tool share one 120-tool model budget.  Exceeding it is
a contract failure raised while the executor is constructed, before any model
request fact or transport call.
"""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent import agent_executor
from phase1_agent.agent_executor import _agent_tools
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.tools import register_bound_tool, register_callable

from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import AgentTransportFixture, graph


TOOL_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "Text"}},
    "required": ["text"],
    "additionalProperties": False,
}


def _static_tools(count):
    return tuple(
        register_callable(f"t{index}", f"Static test tool {index}.", deepcopy(TOOL_SCHEMA),
                          lambda text: {"value": text})
        for index in range(count)
    )


def _bound_tools(count):
    return tuple(
        register_bound_tool(f"b{index}", f"Bound test tool {index}.", deepcopy(TOOL_SCHEMA),
                            lambda arguments, invocation: {})
        for index in range(count)
    )


def test_static_tools_and_final_answer_fit_the_exact_bounded_tool_budget():
    tools = _agent_tools(_static_tools(119))
    assert len(tools) == 120
    assert tools[-1].name == "final_answer"
    assert [tool.name for tool in tools[:-1]] == [f"t{index}" for index in range(119)]


def test_static_tools_over_the_merged_tool_budget_fail_with_the_explicit_code():
    with pytest.raises(ContractValidationError) as caught:
        _agent_tools(_static_tools(120))
    assert caught.value.reason_code == "agent_tool_budget_exceeded"


def test_bound_business_tools_share_the_merged_budget_with_static_and_final_tools():
    tools = _agent_tools(_static_tools(1), _bound_tools(118))
    assert len(tools) == 120
    assert [tool.name for tool in tools[:2]] == ["t0", "final_answer"]
    assert tools[-1].name == "b117"


def test_one_bound_tool_over_the_merged_tool_budget_fails_with_the_explicit_code():
    with pytest.raises(ContractValidationError) as caught:
        _agent_tools(_static_tools(1), _bound_tools(119))
    assert caught.value.reason_code == "agent_tool_budget_exceeded"


def test_duplicate_bound_names_keep_the_distinct_name_conflict():
    duplicate = _bound_tools(1)
    with pytest.raises(ContractValidationError) as caught:
        _agent_tools((), (*duplicate, *duplicate))
    assert caught.value.reason_code == "agent_tool_conflict"


def test_full_binding_budget_cannot_exceed_the_merged_static_and_final_set():
    tools = _agent_tools(None, _bound_tools(118))
    assert len(tools) == 120
    assert [tool.name for tool in tools[:2]] == ["inspect_text", "final_answer"]
    with pytest.raises(ContractValidationError) as caught:
        _agent_tools(None, _bound_tools(120))
    assert caught.value.reason_code == "agent_tool_budget_exceeded"


def test_merged_tool_budget_failure_precedes_the_first_model_request(tmp_path, monkeypatch):
    transport = AgentTransportFixture()
    monkeypatch.setattr(agent_executor, "builtin_workflow_tools",
                        lambda: _static_tools(120))
    with closing(GraphWorkflowService(
            tmp_path / "agent-tool-budget.sqlite",
            public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        session = create(service, graph(service))
        started = service.start(
            session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key=str(uuid4()), inputs={"text": "bounded tools"})
        service.wait(started["active_chain_run_id"])
        failed = service.get_session(session["workflow_session_id"])
        assert failed["status"] == "failed"
        assert failed["chains"][-1]["diagnostic"]["code"] == "agent_tool_budget_exceeded"
        assert transport.calls == []
        facts = service.get_run(
            session["workflow_session_id"], started["active_chain_run_id"])["runtime_facts"]
        kinds = [fact["payload"].get("kind") for fact in facts
                 if isinstance(fact.get("payload"), dict)]
        assert "model_request" not in kinds
        assert "model_result" not in kinds
