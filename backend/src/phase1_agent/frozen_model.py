"""Validated, immutable model settings for a frozen execution."""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
import httpx

from .contract_errors import ContractValidationError


@dataclass(frozen=True)
class FrozenModelParameters:
    model: str
    max_tokens: int | None = None
    temperature: float | int | None = None

    @classmethod
    def from_mapping(cls, parameters: Mapping[str, Any]) -> FrozenModelParameters:
        if not isinstance(parameters, Mapping) or "model" not in parameters:
            raise ContractValidationError("Frozen model settings require an explicit model")
        if not set(parameters) <= {"model", "max_tokens", "temperature", "thinking", "stream"}:
            raise ContractValidationError("Unsupported frozen model setting")
        model = parameters["model"]
        if type(model) is not str or not model.strip():
            raise ContractValidationError("Frozen model name must be nonempty")
        max_tokens = parameters.get("max_tokens")
        if "max_tokens" in parameters and (type(max_tokens) is not int or max_tokens < 1):
            raise ContractValidationError("Frozen max_tokens must be a positive integer")
        temperature = parameters.get("temperature")
        if "temperature" in parameters and (
            type(temperature) not in (int, float)
            or not math.isfinite(temperature)
            or not 0 <= temperature <= 2
        ):
            raise ContractValidationError("Frozen temperature must be between 0 and 2")
        if parameters.get("thinking", "disabled") != "disabled":
            raise ContractValidationError("Only disabled thinking is supported")
        if parameters.get("stream", False) is not False:
            raise ContractValidationError("Only non-streaming model requests are supported")
        return cls(model, max_tokens, temperature)

    def as_mapping(self) -> Mapping[str, Any]:
        parameters: dict[str, Any] = {
            "model": self.model, "thinking": "disabled", "stream": False,
        }
        if self.max_tokens is not None:
            parameters["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            parameters["temperature"] = self.temperature
        return MappingProxyType(parameters)


class FrozenConfiguredAdapter:
    """Fence declared adapter settings before each transport attempt."""

    def __init__(
        self, implementation: Any, parameters: FrozenModelParameters, *,
        dispatch_guard=None, provider_address=None,
    ):
        self.implementation = implementation
        self._parameters = parameters
        self._dispatch_guard = dispatch_guard
        self._provider_address = provider_address
        self.verify_settings()

    def verify_settings(self) -> None:
        target = getattr(self.implementation, "legacy_adapter", self.implementation)
        undeclared = object()
        declared = getattr(target, "model_parameters", undeclared)
        if declared is not undeclared and FrozenModelParameters.from_mapping(declared) != self._parameters:
            raise ContractValidationError("Model adapter disagrees with frozen execution settings")
        if getattr(target, "max_retries", 0) != 0:
            raise ContractValidationError("Workflow model adapters require zero SDK retries")
        if (self._provider_address is not None and hasattr(target, "provider_address")
                and target.provider_address != str(httpx.URL(self._provider_address)).rstrip("/")):
            raise ContractValidationError("Model adapter disagrees with frozen provider address")

    def generate(self, messages, tools):
        self.verify_settings()
        if self._dispatch_guard is not None:
            self._dispatch_guard()
        return self.implementation.generate(messages, tools)

    def close(self) -> None:
        close = getattr(self.implementation, "close", None)
        if callable(close):
            close()


def create_configured_adapter(
    factory: Callable[..., Any], stage: str, parameters: FrozenModelParameters,
    *, legacy_defaults: FrozenModelParameters, provider=None,
) -> Any:
    """Adapt the old fixed-default test hook without ignoring nondefault settings."""
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("Model factory must expose its configuration signature") from exc
    arguments = ((stage, parameters.as_mapping(), MappingProxyType(dict(provider)))
                 if provider is not None else (stage, parameters.as_mapping()))
    try:
        signature.bind(*arguments)
    except TypeError:
        if provider is not None:
            raise ContractValidationError("Selected provider requires a provider-aware model factory") from None
        try:
            signature.bind(stage)
        except TypeError as exc:
            raise ContractValidationError("Model factory must accept stage and frozen settings") from exc
        if parameters != legacy_defaults:
            raise ContractValidationError(
                "Legacy model_factory(stage) supports only fixed default model settings"
            )
        return factory(stage)
    return factory(*arguments)
