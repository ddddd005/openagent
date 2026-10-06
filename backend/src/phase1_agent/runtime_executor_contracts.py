"""Exact-version contracts for trusted, independently hosted executors."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import Draft202012Validator

from .host_sdk import bounded_name, ensure


EXECUTOR_PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class ExecutorReference:
    executor_id: str
    exact_version: str

    def to_dict(self) -> dict:
        ensure(bounded_name(self.executor_id) and bounded_name(self.exact_version),
               "runtime_invalid_executor", "Executor references require an identity and exact version")
        return {"executor_id": self.executor_id, "exact_version": self.exact_version}

    @classmethod
    def from_dict(cls, value: Any) -> ExecutorReference:
        ensure(type(value) is dict and set(value) == {"executor_id", "exact_version"},
               "runtime_invalid_executor", "Executor reference fields are invalid")
        result = cls(**value)
        result.to_dict()
        return result


@dataclass(frozen=True)
class ExecutorDefinition:
    reference: ExecutorReference
    protocol_version: int = EXECUTOR_PROTOCOL_VERSION
    continuation_mode: str = "none"
    fact_schema: dict = field(default_factory=lambda: {"type": "object"})

    def to_dict(self) -> dict:
        ensure(isinstance(self.reference, ExecutorReference)
               and type(self.protocol_version) is int
               and self.protocol_version == EXECUTOR_PROTOCOL_VERSION,
               "runtime_protocol_mismatch", "Executor protocol is not supported")
        ensure(self.continuation_mode in ("none", "same_process"),
               "runtime_unsupported_continuation", "Only none and same_process continuation are supported")
        ensure(type(self.fact_schema) is dict, "runtime_invalid_executor", "Fact schema must be an object")
        Draft202012Validator.check_schema(self.fact_schema)
        return {"executor_ref": self.reference.to_dict(), "protocol_version": self.protocol_version,
                "continuation_mode": self.continuation_mode, "fact_schema": copy.deepcopy(self.fact_schema)}


@dataclass(frozen=True)
class PauseSupport:
    reference: ExecutorReference
    protocol_version: int = EXECUTOR_PROTOCOL_VERSION
    continuation_mode: str = "same_process"

    def to_dict(self) -> dict:
        ensure(isinstance(self.reference, ExecutorReference)
               and type(self.protocol_version) is int
               and self.protocol_version == EXECUTOR_PROTOCOL_VERSION,
               "runtime_protocol_mismatch", "Pause protocol is not supported")
        ensure(self.continuation_mode == "same_process", "runtime_unsupported_continuation",
               "Pause support currently requires same_process continuation")
        return {"executor_ref": self.reference.to_dict(), "protocol_version": self.protocol_version,
                "continuation_mode": self.continuation_mode}


@dataclass(frozen=True)
class RegisteredExecutor:
    definition: ExecutorDefinition
    factory: Callable


@dataclass(frozen=True)
class RegisteredPauseSupport:
    definition: PauseSupport
    adapter: Callable


class ExecutorRegistry:
    """Factories create handles; optional adapters validate retained safe points."""

    def __init__(self) -> None:
        self._executors: dict[ExecutorReference, RegisteredExecutor] = {}
        self._pause: dict[ExecutorReference, RegisteredPauseSupport] = {}
        self._frozen = False

    def register_executor(self, definition: ExecutorDefinition, factory: Callable) -> None:
        ensure(not self._frozen, "host_registry_frozen", "The executor registry is frozen")
        ensure(isinstance(definition, ExecutorDefinition), "runtime_invalid_executor",
               "Executor registration requires a public definition")
        definition.to_dict()
        ensure(callable(factory), "runtime_invalid_executor", "Executor factory must be callable")
        ensure(definition.reference not in self._executors, "runtime_duplicate_executor",
               "Exact executor version is already registered")
        self._executors[definition.reference] = RegisteredExecutor(copy.deepcopy(definition), factory)

    def register_pause_support(self, definition: PauseSupport, adapter: Callable) -> None:
        ensure(not self._frozen, "host_registry_frozen", "The executor registry is frozen")
        ensure(isinstance(definition, PauseSupport), "runtime_invalid_pause_support",
               "Pause registration requires a public definition")
        definition.to_dict()
        executor = self.get(definition.reference)
        ensure(executor is not None, "runtime_missing_executor",
               "Pause support requires its exact executor version to be registered first")
        ensure(executor.definition.continuation_mode == definition.continuation_mode,
               "runtime_pause_capability_mismatch", "Pause support cannot upgrade executor continuation")
        ensure(callable(adapter), "runtime_invalid_pause_support", "Pause adapter must be callable")
        ensure(definition.reference not in self._pause, "runtime_duplicate_pause_support",
               "Exact pause support is already registered")
        self._pause[definition.reference] = RegisteredPauseSupport(copy.deepcopy(definition), adapter)

    def get(self, reference: ExecutorReference) -> RegisteredExecutor | None:
        return self._executors.get(reference)

    def pause_support(self, reference: ExecutorReference) -> RegisteredPauseSupport | None:
        return self._pause.get(reference)

    def supported_controls(self, reference: ExecutorReference) -> tuple[str, ...]:
        ensure(self.get(reference) is not None, "runtime_missing_executor", "Exact executor is unavailable")
        return ("pause", "resume") if self.pause_support(reference) is not None else ()

    def catalog(self) -> list[dict]:
        return [entry.definition.to_dict() for _, entry in sorted(
            self._executors.items(), key=lambda item: (item[0].executor_id, item[0].exact_version))]

    def pause_catalog(self) -> list[dict]:
        return [entry.definition.to_dict() for _, entry in sorted(
            self._pause.items(), key=lambda item: (item[0].executor_id, item[0].exact_version))]

    def registrations(self) -> tuple[RegisteredExecutor, ...]:
        return tuple(self._executors.values())

    def pause_registrations(self) -> tuple[RegisteredPauseSupport, ...]:
        return tuple(self._pause.values())

    def detached(self, *, frozen: bool = False) -> ExecutorRegistry:
        result = ExecutorRegistry()
        result._executors = {ref: RegisteredExecutor(copy.deepcopy(entry.definition), entry.factory)
                             for ref, entry in self._executors.items()}
        result._pause = {ref: RegisteredPauseSupport(copy.deepcopy(entry.definition), entry.adapter)
                         for ref, entry in self._pause.items()}
        result._frozen = frozen
        return result
