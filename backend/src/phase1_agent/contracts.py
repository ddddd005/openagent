"""Internal records shared by the context, kernel, and model boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from typing import Any, Literal, Protocol, Sequence


Message = dict[str, Any]
RunStatus = Literal["success", "protocol_error", "model_error", "cancelled", "steps_exhausted"]


@dataclass(frozen=True)
class ModelToolCall:
    id: str | None
    name: str | None
    raw_arguments: str | None
    type: str | None = "function"


@dataclass(frozen=True)
class ModelResponse:
    finish_reason: str | None
    content: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()
    usage: dict[str, Any] | None = None
    response_id: str | None = None
    model: str | None = None


class ModelAdapter(Protocol):
    def generate(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]) -> ModelResponse: ...


class CallableTool(Protocol):
    name: str
    schema: dict[str, Any]
    definition: dict[str, Any]

    def validate(self, arguments: dict[str, Any]) -> None: ...

    def execute(self, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class RunLimits:
    max_model_requests: int = 4
    correction_budget: int = 1


@dataclass(frozen=True)
class RunInput:
    run_id: str
    s0: Sequence[Message]
    tools: Sequence[CallableTool]
    adapter: ModelAdapter
    output_spec: dict[str, Any]
    correction_message: str
    limits: RunLimits = field(default_factory=RunLimits)
    cancellation: Event | None = None


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    history_delta: tuple[Message, ...] = ()
    output: str | None = None
    error: dict[str, str] | None = None
    diagnostics: dict[str, Any] | None = None
    dispatch_started: bool = False
