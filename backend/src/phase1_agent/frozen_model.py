"""Validated, immutable model settings for a frozen execution."""

from __future__ import annotations

import math
from collections.abc import Mapping
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
    thinking: tuple | str = "disabled"

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
        thinking = parameters.get("thinking", "disabled")
        if thinking != "disabled":
            if not isinstance(thinking, Mapping):
                raise ContractValidationError("Thinking settings must be explicit")
            mode = thinking.get("mode")
            keys = {"mode", "include_summary", "budget" if mode == "budget" else "level"}
            if mode not in ("budget", "level") or set(thinking) != keys \
                    or type(thinking["include_summary"]) is not bool:
                raise ContractValidationError("Thinking settings have unsupported fields")
            if mode == "budget" and (type(thinking["budget"]) is not int or thinking["budget"] < -1):
                raise ContractValidationError("Thinking budget must be -1 or a nonnegative integer")
            if mode == "level" and thinking["level"] not in ("minimal", "low", "medium", "high"):
                raise ContractValidationError("Unsupported thinking level")
            thinking = (mode, thinking["budget" if mode == "budget" else "level"],
                        thinking["include_summary"])
        if parameters.get("stream", False) is not False:
            raise ContractValidationError("Only non-streaming model requests are supported")
        return cls(model, max_tokens, temperature, thinking)

    def as_mapping(self) -> Mapping[str, Any]:
        parameters: dict[str, Any] = {
            "model": self.model, "thinking": self.thinking if self.thinking == "disabled" else {
                "mode": self.thinking[0],
                "budget" if self.thinking[0] == "budget" else "level": self.thinking[1],
                "include_summary": self.thinking[2],
            }, "stream": False,
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
        target = self.implementation
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

    def generate_prepared(self, messages, tools, prepared):
        self.verify_settings()
        if self._dispatch_guard is not None:
            self._dispatch_guard()
        generate = getattr(self.implementation, "generate_prepared", None)
        return (generate(prepared) if callable(generate)
                else self.implementation.generate(messages, tools))

    def close(self) -> None:
        close = getattr(self.implementation, "close", None)
        if callable(close):
            close()
