from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.model_request_transport import (
    CHAT_PROJECTION_VERSION, GEMINI_PROJECTION_VERSION,
    project_model_request, resolve_model_request_transport,
)


def _parameters(model):
    return {"model": model, "thinking": "disabled", "stream": False, "max_tokens": 32_000}


def _provider(protocol):
    return {"protocol": protocol, "base_url": "https://provider.invalid/v1/"}


def test_trusted_protocol_resolvers_are_pure_and_do_not_invent_output_caps():
    chat = resolve_model_request_transport("chat", "deepseek-chat")
    gemini = resolve_model_request_transport("gemini", "gemini-2.5-flash")
    assert chat.projection_version == CHAT_PROJECTION_VERSION == "deepseek-chat@1"
    assert gemini.projection_version == GEMINI_PROJECTION_VERSION == "gemini-generate-content@1"
    for definition in (chat, gemini):
        assert definition.capability["output_limit_tokens"] is None
        assert definition.counter is None
        assert definition.estimator_id is None and definition.estimator_version is None
        assert callable(definition.projector) and callable(definition.create_transport)


def test_both_existing_projectors_build_actual_protocol_wire_with_current_output_limit():
    messages = [{"kind": "text", "role": "user", "content": "hello"}]
    chat = project_model_request("chat", "deepseek-chat", messages, [], _parameters("deepseek-chat"))
    gemini = project_model_request(
        "gemini", "gemini-2.5-flash", messages, [], _parameters("gemini-2.5-flash"))
    assert chat["projection"] == CHAT_PROJECTION_VERSION
    assert chat["wire_request"]["max_tokens"] == 32_000
    assert "temperature" not in chat["wire_request"]
    assert gemini["projection"] == GEMINI_PROJECTION_VERSION
    assert gemini["wire_request"]["generationConfig"]["maxOutputTokens"] == 32_000
    assert "tools" not in gemini["wire_request"]


def test_resolver_defers_injected_factory_until_dispatch_and_checks_its_declaration():
    calls = []

    class PreparedTransport:
        max_retries = 0
        provider_address = "https://provider.invalid/v1"

        def __init__(self, parameters):
            self.model_parameters = deepcopy(parameters)

        def generate_prepared(self, prepared):
            return prepared

    def factory(*, provider, parameters, api_key):
        calls.append((deepcopy(provider), deepcopy(parameters), api_key))
        return PreparedTransport(parameters)

    definition = resolve_model_request_transport("chat", "deepseek-chat", factory)
    assert calls == []
    result = definition.create_transport(
        provider=_provider("chat"), parameters=_parameters("deepseek-chat"), api_key="fixture-only")
    assert calls == [(_provider("chat"), _parameters("deepseek-chat"), "fixture-only")]
    assert result.generate_prepared({"wire_request": {"model": "deepseek-chat"}}) == {
        "wire_request": {"model": "deepseek-chat"}}


def test_default_factory_builds_current_adapters_with_zero_retries_without_sending():
    for protocol, model in (("chat", "deepseek-chat"), ("gemini", "gemini-2.5-flash")):
        definition = resolve_model_request_transport(protocol, model)
        transport = definition.create_transport(
            provider=_provider(protocol), parameters=_parameters(model), api_key="offline-fixture")
        implementation = transport.implementation
        assert implementation.max_retries == 0
        assert implementation.model_parameters == _parameters(model)
        assert implementation.provider_address == "https://provider.invalid/v1"
        transport.close()


@pytest.mark.parametrize("factory", [
    lambda **kwargs: object(),
    lambda **kwargs: type("Transport", (), {
        "max_retries": 0, "model_parameters": {**_parameters("deepseek-chat"), "max_tokens": 8192},
        "generate_prepared": lambda self, prepared: None,
    })(),
    lambda **kwargs: type("Transport", (), {
        "max_retries": 1, "model_parameters": _parameters("deepseek-chat"),
        "provider_address": "https://provider.invalid/v1/",
        "generate_prepared": lambda self, prepared: None,
    })(),
    lambda **kwargs: type("Transport", (), {
        "max_retries": 0, "model_parameters": _parameters("deepseek-chat"),
        "provider_address": "https://other.invalid/v1",
        "generate_prepared": lambda self, prepared: None,
    })(),
])
def test_injected_transport_must_support_exact_prepared_wire_settings(factory):
    definition = resolve_model_request_transport("chat", "deepseek-chat", factory)
    with pytest.raises(ContractValidationError):
        definition.create_transport(
            provider=_provider("chat"), parameters=_parameters("deepseek-chat"), api_key="fixture-only")


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "proxy-custom-flash"])
def test_gemini_model_names_do_not_require_registration_or_infer_capacity(model):
    definition = resolve_model_request_transport("gemini", model)
    assert definition.capability["model_id"] == model
    assert definition.capability["output_limit_tokens"] is None
    assert definition.capability["thinking"] == ["disabled", "budget", "level"]
    prepared = definition.projector(
        [{"kind": "text", "role": "user", "content": "hello"}], [], _parameters(model))
    assert prepared["model"] == model
    assert prepared["wire_request"]["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 0, "includeThoughts": False}


@pytest.mark.parametrize("model", ["", " gemini-3.8-flash", "gemini-3.8-flash ", "bad\nmodel", "x" * 257])
def test_gemini_still_requires_an_exact_valid_model_identity(model):
    with pytest.raises(ContractValidationError):
        resolve_model_request_transport("gemini", model)


def test_unknown_protocol_is_still_rejected():
    with pytest.raises(ContractValidationError):
        resolve_model_request_transport("unknown", "some-model")
