"""Current request-service HTTP checks retained from the retired model facade."""

from contextlib import contextmanager
import json

import httpx
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.model_request_transport import resolve_model_request_transport
from phase1_agent.model_service import create_chat_transport
from test_model_request_service import ServiceHarness


@contextmanager
def current_http_model(handler):
    harness = ServiceHarness()
    observed, transports = [], []

    def record(request):
        observed.append(json.loads(request.content))
        return handler(request)

    def factory(**arguments):
        transport = create_chat_transport(
            **arguments, http_client=httpx.Client(transport=httpx.MockTransport(record)))
        transports.append(transport)
        return transport

    harness.service = harness.make_service(
        resolve_transport=lambda protocol, model: resolve_model_request_transport(
            protocol, model, factory))
    try:
        yield harness, observed, transports
    finally:
        harness.close()


def successful_response(request):
    return httpx.Response(200, request=request, json={
        "id": "offline-http-response", "object": "chat.completion", "created": 0,
        "model": "synthetic-chat",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "HTTP mock"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    })


def test_current_http_projection_matches_accepted_wire_and_disables_sdk_retries():
    with current_http_model(successful_response) as (harness, observed, transports):
        prepared = harness.prepare()
        result = harness.dispatch(prepared)
        request_fact = next(fact for fact in harness.accepted.values() if fact["stage"] == "request")
        assert result["content"] == "HTTP mock"
        assert result["usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        assert observed == [request_fact["details"]["wire_request"]]
        assert observed[0]["max_tokens"] == prepared["settings"]["parameters"]["max_tokens"]
        assert observed[0]["thinking"] == {"type": "disabled"}
        assert transports[0].implementation.max_retries == 0
        assert transports[0].implementation._client.max_retries == 0
        assert harness.dispatch(prepared) == result
        assert len(observed) == 1


def test_current_http_timeout_has_one_transport_attempt_and_never_replays():
    def timeout(request):
        raise httpx.ReadTimeout("synthetic-private-key", request=request)

    with current_http_model(timeout) as (harness, observed, transports):
        prepared = harness.prepare()
        for iteration in range(2):
            with pytest.raises(ContractValidationError) as failed:
                harness.dispatch(prepared)
            assert failed.value.reason_code == "model_dispatch_unknown"
            assert len(observed) == 1
        outcome = next(fact for fact in harness.accepted.values() if fact["stage"] == "outcome")
        assert outcome["details"]["classification"] == "dispatch_unknown"
        assert transports[0].implementation._client.max_retries == 0
        assert b"synthetic-private-key" not in canonical_bytes(list(harness.accepted.values()))


@pytest.mark.parametrize("status", [400, 429, 503])
def test_current_http_rejection_preserves_status_without_retry_or_provider_secret(status):
    def reject(request):
        return httpx.Response(status, request=request, json={
            "error": {"message": "synthetic-private-key"}})

    with current_http_model(reject) as (harness, observed, transports):
        prepared = harness.prepare()
        diagnostics = []
        for iteration in range(2):
            with pytest.raises(ContractValidationError) as failed:
                harness.dispatch(prepared)
            assert failed.value.reason_code == "model_provider_error"
            diagnostics.append(failed.value.diagnostics)
        outcome = next(fact for fact in harness.accepted.values() if fact["stage"] == "outcome")
        assert outcome["details"]["classification"] == "provider_error"
        assert outcome["details"]["status_code"] == status
        assert len(observed) == len(transports) == 1
        assert b"synthetic-private-key" not in canonical_bytes(
            [diagnostics, list(harness.accepted.values())])


def test_current_malformed_http_response_is_confirmed_invalid_and_never_replayed():
    def malformed(request):
        return httpx.Response(200, request=request, json={"choices": []})

    with current_http_model(malformed) as (harness, observed, transports):
        prepared = harness.prepare()
        for iteration in range(2):
            with pytest.raises(ContractValidationError) as failed:
                harness.dispatch(prepared)
            assert failed.value.reason_code == "model_response_invalid"
        outcome = next(fact for fact in harness.accepted.values() if fact["stage"] == "outcome")
        assert outcome["details"]["classification"] == "response_invalid"
        assert len(observed) == len(transports) == 1
