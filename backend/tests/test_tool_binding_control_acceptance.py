"""Bound variable tools obey existing run control and frozen-basis guards.

All calls use a temporary SQLite database, synthetic credentials and a local
transport. The pause gate is after the first write has committed and been
independently reread, not inside its transaction.
"""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent import tool_package
from phase1_agent.contract_errors import ContractValidationError

from test_models_service_integration import ModelDatabaseFixture
from test_tool_binding_reread_failure import _write_evidence
from test_tool_binding_review_evidence import _no_normal_graph
from test_variable_tool_bindings import (
    KEY, VariableToolTransport, _events, _registered, _service,
)


@pytest.mark.parametrize("action", ["resume", "close"])
def test_control_after_accepted_tool_write_preserves_it_without_replay(
        tmp_path, monkeypatch, action):
    entered, release = Event(), Event()
    assignments, invocations = [], []
    original_assign = tool_package._assign

    def counted_assign(config, inputs, context):
        assignments.append(config["operation"])
        return original_assign(config, inputs, context)

    monkeypatch.setattr(tool_package, "_assign", counted_assign)
    transport = VariableToolTransport([
        ("score_set", {"value": 10}), ("score_add", {"value": 2}),
        ("score_read", {}), ("final_answer", {"answer": {"text": "done"}}),
    ])
    database = tmp_path / ("tool-control-" + action + ".sqlite")
    with closing(_service(database, transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = _no_normal_graph(service)
        session = _registered(service, document)
        sid = session["workflow_session_id"]
        original_invoke = service._invoke_bound_tool

        def gated_invoke(context, payload):
            result = original_invoke(context, payload)
            invocations.append((context, deepcopy(payload)))
            if len(invocations) == 1:
                entered.set()
                assert release.wait(10), "accepted tool pause gate timed out"
            return result

        monkeypatch.setattr(service, "_invoke_bound_tool", gated_invoke)
        started = service.start(
            sid, expected_revision=session["revision"],
            idempotency_key=str(uuid4()), inputs={"text": "controlled tools"})
        chain_id = started["active_chain_run_id"]
        try:
            assert entered.wait(10), "first tool write did not return"
            current = service.get_session(sid)
            assert current["objects"][KEY]["value"]["value"] == 10
            assert current["objects"][KEY]["revision"] == 3
            service.control(
                sid, action="pause", expected_revision=current["revision"],
                idempotency_key=str(uuid4()))
        finally:
            release.set()
        service.wait(chain_id)
        paused = service.get_session(sid)
        assert paused["status"] == "paused", paused["chains"]
        assert assignments == ["set"]
        assert len(transport.calls) == 1
        assert _write_evidence(service, sid)[0] == 1
        assert chain_id in service._tool_frames
        assert chain_id in service._execution_registries
        first_settled = [event["payload"] for event in _events(service, paused, chain_id)
                         if event["kind"] == "tool_settled"]
        assert len(first_settled) == 1
        assert first_settled[0]["outcome"] == "success"

        context, payload = invocations[0]
        late_payload = {
            **payload, "tool_call_id": "late-paused-call",
            "tool_execution_id": str(uuid4()),
        }
        with pytest.raises(ContractValidationError) as caught:
            original_invoke(context, late_payload)
        assert caught.value.reason_code == "tool_invocation_scope_invalid"
        assert assignments == ["set"]
        assert _write_evidence(service, sid)[0] == 1

        request = {
            "action": action, "expected_revision": paused["revision"],
            "idempotency_key": str(uuid4()),
        }
        accepted = service.control(sid, **request)
        assert service.control(sid, **request) == accepted
        if action == "resume":
            service.wait(chain_id)
        final = service.get_session(sid)
        expected_value = 12 if action == "resume" else 10
        expected_revision = 4 if action == "resume" else 3
        expected_writes = 2 if action == "resume" else 1
        assert final["status"] == ("succeeded" if action == "resume" else "closed")
        assert final["objects"][KEY]["value"]["value"] == expected_value
        assert final["objects"][KEY]["revision"] == expected_revision
        assert assignments == (["set", "add"] if action == "resume" else ["set"])
        assert len(transport.calls) == (4 if action == "resume" else 1)
        writes, receipts = _write_evidence(service, sid)
        assert writes == len(receipts) == expected_writes
        assert chain_id not in service._tool_frames
        assert chain_id not in service._execution_registries
        assert chain_id not in service._runtime_hosts
        final_settled = [event["payload"] for event in _events(service, final, chain_id)
                         if event["kind"] == "tool_settled"]
        execution_id = payload["tool_execution_id"]
        assert sum(item["tool_execution_id"] == execution_id
                   for item in final_settled) == 1

    with closing(_service(database, transport)) as reopened:
        persisted = reopened.get_session(sid)
        assert persisted["status"] == final["status"]
        assert persisted["objects"][KEY]["value"]["value"] == expected_value
        assert persisted["objects"][KEY]["revision"] == expected_revision
        assert _write_evidence(reopened, sid) == (writes, receipts)
        assert len(transport.calls) == (4 if action == "resume" else 1)


def test_missing_run_basis_rejects_tool_before_the_real_executor(
        tmp_path, monkeypatch):
    assignments, failures = [], []
    original_assign = tool_package._assign

    def counted_assign(config, inputs, context):
        assignments.append(config["operation"])
        return original_assign(config, inputs, context)

    monkeypatch.setattr(tool_package, "_assign", counted_assign)
    transport = VariableToolTransport([("score_set", {"value": 10})])
    with closing(_service(tmp_path / "tool-missing-basis.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = _no_normal_graph(service)
        session = _registered(service, document)
        original_bridge = service._tool_bridge

        def bridge(context, operation, payload):
            if operation == "invoke":
                service._execution_registries.pop(context.chain_run_id)
                try:
                    return original_bridge(context, operation, payload)
                except ContractValidationError as exc:
                    failures.append(exc.reason_code)
                    raise
            return original_bridge(context, operation, payload)

        monkeypatch.setattr(service, "_tool_bridge", bridge)
        sid = session["workflow_session_id"]
        started = service.start(
            sid, expected_revision=session["revision"],
            idempotency_key=str(uuid4()), inputs={"text": "missing run basis"})
        chain_id = started["active_chain_run_id"]
        with pytest.raises(ContractValidationError) as caught:
            service.wait(chain_id)
        assert caught.value.reason_code == "run_basis_unavailable"
        failed = service.get_session(sid)
        assert failures == ["run_basis_unavailable"]
        assert failed["status"] == "recovery_unavailable", failed["chains"]
        assert failed["chains"][-1]["diagnostic"]["reason_code"] == "run_basis_unavailable"
        assert failed["available_actions"] == ["close"]
        assert assignments == []
        assert len(transport.calls) == 1
        assert failed["objects"][KEY] == session["objects"][KEY]
        assert _write_evidence(service, session["workflow_session_id"]) == (0, [])
        assert chain_id not in service._tool_frames
        settled = [event["payload"] for event in _events(service, failed, chain_id)
                   if event["kind"] == "tool_settled"]
        assert len(settled) == 1
        assert settled[0]["outcome"] != "success"
