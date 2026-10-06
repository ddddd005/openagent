"""The existing input HTTP route accepts only exact public prompt choices."""

from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from test_server import FakeService
from test_server_prompt_configs import request, running_server
from test_prompt_selection import selection


class SelectionService(FakeService):
    def submit(self, session_id, text, idempotency_key, *, prompt_selection=None):
        self.calls.append(("selected_submit", session_id, text, idempotency_key, deepcopy(prompt_selection)))
        return {"chain_run_id": "c1", "status": "prepared"}


def test_http_input_forwards_exact_selection_without_expanding_private_rules():
    service = SelectionService()
    payload = {"text": "question", "idempotency_key": "once", "prompt_selection": selection()}
    with running_server(service) as port:
        status, body = request(port, "POST", "/api/sessions/s1/inputs", payload)
    assert status == 202 and body["chain_run_id"] == "c1"
    assert service.calls == [("selected_submit", "s1", "question", "once", selection())]


def test_omitted_selection_keeps_original_three_argument_submit_compatibility():
    service = FakeService()
    with running_server(service) as port:
        status, body = request(port, "POST", "/api/sessions/s1/inputs", {
            "text": "legacy", "idempotency_key": "once",
        })
    assert status == 202 and body["chain_run_id"] == "c1"
    assert service.calls == [("submit", "s1", "legacy", "once")]


@pytest.mark.parametrize("selected", [
    None, {}, "latest",
    {"schema_version": 1, "kind": "workflow_prompt_selection", "nodes": {"Output": {}}},
    {"schema_version": 1, "kind": "workflow_prompt_selection", "nodes": {"A": {"config_id": "PRIVATE-ID", "revision": True}}},
    {"schema_version": 2, "kind": "workflow_prompt_selection", "nodes": {}},
    {**selection(), "steps": [{"PRIVATE": True}]},
])
def test_http_bad_selection_is_redacted_400_before_any_service_call(selected):
    service = SelectionService()
    with running_server(service) as port:
        status, body = request(port, "POST", "/api/sessions/s1/inputs", {
            "text": "PRIVATE-TEXT", "idempotency_key": "once", "prompt_selection": selected,
        })
    assert status == 400 and body["error"]["reason_code"] == "invalid_request"
    assert "PRIVATE" not in str(body)
    assert service.calls == []


@pytest.mark.parametrize("status,reason", [
    (404, "not_found"), (500, "storage_contract_violation"), (409, "idempotency_conflict"),
])
def test_selected_input_preserves_explicit_definition_and_storage_status(status, reason):
    class Rejected(SelectionService):
        def submit(self, *_args, **_kwargs):
            error = ContractValidationError("PRIVATE-STORAGE-BODY")
            error.status_code, error.reason_code = status, reason
            raise error
    with running_server(Rejected()) as port:
        actual_status, body = request(port, "POST", "/api/sessions/s1/inputs", {
            "text": "question", "idempotency_key": "once", "prompt_selection": selection(),
        })
    assert actual_status == status and body["error"]["reason_code"] == reason
    assert "PRIVATE" not in str(body)


def test_selected_input_rejects_extra_top_fields_and_untrusted_origin():
    service = SelectionService()
    with running_server(service) as port:
        payload = {"text": "question", "idempotency_key": "once", "prompt_selection": selection()}
        assert request(port, "POST", "/api/sessions/s1/inputs", {**payload, "steps": []})[0] == 400
        assert request(port, "POST", "/api/sessions/s1/inputs", payload, origin=False)[0] == 403
    assert service.calls == []
