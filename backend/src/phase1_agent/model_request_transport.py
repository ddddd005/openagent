"""Trusted, pure transport declarations for prepared model requests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

import httpx

from .adapter import DeepSeekAdapter
from .contract_errors import require
from .frozen_model import FrozenConfiguredAdapter, FrozenModelParameters
from .gemini_adapter import GeminiAdapter
from .gemini_capabilities import GEMINI_PROTOCOL


CHAT_PROTOCOL = "chat"
GEMINI_MODEL_PROTOCOL = "gemini"
CHAT_PROJECTION_VERSION = "deepseek-chat@1"
GEMINI_PROJECTION_VERSION = GEMINI_PROTOCOL


@dataclass(frozen=True, repr=False)
class ModelRequestTransport:
    """Exact protocol projector and dispatch factory captured for one request."""

    capability: dict
    projection_version: str
    projector: Callable
    create_transport: Callable
    counter: Callable | None = None
    estimator_id: str | None = None
    estimator_version: str | None = None


def _default_factory(protocol, *, provider, parameters, api_key):
    require(provider["protocol"] == protocol, "model_transport_settings_invalid",
            "Provider protocol differs from the captured transport")
    from .model_service import create_chat_transport

    return create_chat_transport(provider=provider, parameters=parameters, api_key=api_key)


def _capability(protocol, model_id):
    if protocol == CHAT_PROTOCOL:
        transport_id = "deepseek-chat-completions"
        transport_version = CHAT_PROJECTION_VERSION
        thinking = ["disabled"]
        provenance = {"source": "phase1_agent.adapter.DeepSeekAdapter.prepare_request",
                      "version": CHAT_PROJECTION_VERSION}
    else:
        transport_id = "gemini-generate-content"
        transport_version = GEMINI_PROJECTION_VERSION
        thinking = ["disabled", "budget", "level"]
        provenance = {"source": "phase1_agent.gemini_adapter.GeminiAdapter.prepare_request",
                      "version": GEMINI_PROJECTION_VERSION}
    return {
        "schema_version": 1, "transport_id": transport_id, "transport_version": transport_version,
        "protocol": protocol, "model_id": model_id,
        # The supported adapters have no registered exact-model hard output ceiling.
        "output_limit_tokens": None, "tools": True, "thinking": thinking,
        "provenance": provenance,
    }


def resolve_model_request_transport(protocol: str, model_id: str,
                                    transport_factory: Callable | None = None) -> ModelRequestTransport:
    """Resolve only installed pure capabilities; transport construction is deferred until dispatch."""
    require(protocol in (CHAT_PROTOCOL, GEMINI_MODEL_PROTOCOL)
            and type(model_id) is str and 0 < len(model_id) <= 256
            and model_id.strip() == model_id
            and not any(ord(char) < 32 or ord(char) == 127 for char in model_id),
            "model_transport_capability_unavailable", "Exact protocol and model identity are required")
    if protocol == GEMINI_MODEL_PROTOCOL:
        projector = GeminiAdapter.prepare_request
        projection_version = GEMINI_PROJECTION_VERSION
    else:
        projector = DeepSeekAdapter.prepare_request
        projection_version = CHAT_PROJECTION_VERSION
    if transport_factory is not None:
        require(callable(transport_factory), "model_transport_capability_unavailable",
                "Injected transport factory must be callable")

    def create_transport(*, provider, parameters, api_key):
        require(type(parameters) is dict and parameters.get("model") == model_id,
                "model_transport_settings_invalid", "Transport model differs from its trusted declaration")
        if transport_factory is None:
            return _default_factory(protocol, provider=provider, parameters=parameters, api_key=api_key)
        transport = transport_factory(
            provider=deepcopy(provider), parameters=deepcopy(parameters), api_key=api_key)
        require(transport is not None, "model_transport_capability_unavailable",
                "Transport factory returned no implementation")
        target = transport.implementation if isinstance(transport, FrozenConfiguredAdapter) else transport
        actual_parameters = getattr(target, "model_parameters", None)
        require(FrozenModelParameters.from_mapping(actual_parameters)
                == FrozenModelParameters.from_mapping(parameters)
                and type(getattr(target, "max_retries", None)) is int
                and target.max_retries == 0
                and callable(getattr(target, "generate_prepared", None))
                and type(getattr(target, "provider_address", None)) is str,
                "model_transport_settings_invalid",
                "Injected transport must preserve exact settings, address and prepared-wire dispatch")
        expected_address = str(httpx.URL(provider["base_url"])).rstrip("/")
        actual_address = str(httpx.URL(target.provider_address)).rstrip("/")
        require(actual_address == expected_address,
                "model_transport_settings_invalid",
                "Injected transport address differs from the current provider")
        return transport

    return ModelRequestTransport(
        capability=_capability(protocol, model_id), projection_version=projection_version,
        projector=projector, create_transport=create_transport)


def project_model_request(protocol: str, model_id: str, messages: list, tools: list,
                          parameters: dict) -> dict:
    """Apply the same exact pure projector used by preparation and proof."""
    return resolve_model_request_transport(protocol, model_id).projector(
        deepcopy(messages), deepcopy(tools), deepcopy(parameters))
