"""Fenced in-process progress for one run, not a resumable kernel checkpoint.

This workspace records counters and tool identities only. It neither executes
tools nor authorizes N05 resume, and it must not be persisted as a Snapshot or
used to reconstruct model history. The coordinator owns execution and safety.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from uuid import UUID, uuid4

from .contract_errors import ContractValidationError


_TOOL_OUTCOMES = frozenset({"success", "error", "unknown", "outcome_unknown", "interrupted"})
_TOOL_STATUSES = _TOOL_OUTCOMES | {"pending", "started", "never_started"}
_UNRESOLVED = frozenset({"pending", "started", "unknown"})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _uuid4(value: object, field: str) -> None:
    _require(type(value) is str, f"{field} must be a canonical UUID v4")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ContractValidationError(f"{field} must be a canonical UUID v4") from exc
    _require(parsed.version == 4 and str(parsed) == value, f"{field} must be a canonical UUID v4")


def _revision(value: object) -> None:
    _require(type(value) is int and value >= 1, "Expected revision must be a positive integer")


@dataclass(frozen=True)
class AcceptedProgress:
    """Observed counters, not a copy of accepted message content."""

    model_requests: int = 0
    attempts: int = 0
    accepted_messages: int = 0

    def __post_init__(self) -> None:
        for name in ("model_requests", "attempts", "accepted_messages"):
            value = getattr(self, name)
            _require(type(value) is int and value >= 0, f"{name} must be nonnegative")
        _require(self.model_requests <= self.attempts, "Attempts cannot trail model requests")


@dataclass(frozen=True)
class ToolProgress:
    tool_call_id: str
    tool_execution_id: str | None
    status: str

    def __post_init__(self) -> None:
        _uuid4(self.tool_call_id, "tool_call_id")
        _require(type(self.status) is str and self.status in _TOOL_STATUSES,
                 "Unknown tool status")
        if self.status in {"pending", "never_started"}:
            _require(self.tool_execution_id is None, "Never-started tool has no execution identity")
        else:
            _uuid4(self.tool_execution_id, "tool_execution_id")


@dataclass(frozen=True)
class ActiveRunView:
    run_id: str
    chain_run_id: str
    workflow_session_id: str
    base_commit_id: str
    generation: str
    revision: int
    accepted_progress: AcceptedProgress
    tools: tuple[ToolProgress, ...]

    @property
    def pending_tools(self) -> tuple[ToolProgress, ...]:
        return tuple(tool for tool in self.tools if tool.status in _UNRESOLVED)


class ActiveRun:
    """Local workspace with atomic revision and opaque executor-generation CAS."""

    def __init__(
        self, run_id: str, chain_run_id: str, workflow_session_id: str, base_commit_id: str,
    ) -> None:
        for name, value in (
            ("run_id", run_id), ("chain_run_id", chain_run_id),
            ("workflow_session_id", workflow_session_id), ("base_commit_id", base_commit_id),
        ):
            _uuid4(value, name)
        self._lock = RLock()
        self._view = ActiveRunView(
            run_id=run_id, chain_run_id=chain_run_id,
            workflow_session_id=workflow_session_id, base_commit_id=base_commit_id,
            generation=str(uuid4()), revision=1, accepted_progress=AcceptedProgress(),
            tools=(),
        )

    def snapshot(self) -> ActiveRunView:
        with self._lock:
            return self._view

    def _guard(self, generation: str, revision: int) -> None:
        _uuid4(generation, "expected_generation")
        _revision(revision)
        _require(generation == self._view.generation, "Stale ActiveRun generation")
        _require(revision == self._view.revision, "Stale ActiveRun revision")

    def _advance(self, **changes: object) -> ActiveRunView:
        self._view = replace(self._view, revision=self._view.revision + 1, **changes)
        return self._view

    def _first_unresolved(self, index: int) -> None:
        _require(
            all(tool.status not in _UNRESOLVED for tool in self._view.tools[:index]),
            "Tool callbacks must follow accepted call order",
        )

    def accept_progress(
        self, progress: AcceptedProgress, *, expected_generation: str, expected_revision: int,
    ) -> ActiveRunView:
        _require(type(progress) is AcceptedProgress, "Progress must be AcceptedProgress")
        with self._lock:
            self._guard(expected_generation, expected_revision)
            old = self._view.accepted_progress
            _require(
                all(getattr(progress, name) >= getattr(old, name)
                    for name in ("model_requests", "attempts", "accepted_messages")),
                "Accepted progress cannot go backwards",
            )
            _require(progress != old, "Accepted progress did not advance")
            return self._advance(accepted_progress=progress)

    def queue_tool(
        self, tool_call_id: str, *, expected_generation: str, expected_revision: int,
    ) -> ActiveRunView:
        _uuid4(tool_call_id, "tool_call_id")
        with self._lock:
            self._guard(expected_generation, expected_revision)
            _require(all(tool.tool_call_id != tool_call_id for tool in self._view.tools),
                     "Tool call identity has already been accepted")
            _require(not any(tool.status in {"started", "unknown"} for tool in self._view.tools),
                     "New tool calls cannot be queued during an unresolved execution")
            return self._advance(tools=self._view.tools + (ToolProgress(tool_call_id, None, "pending"),))

    def start_tool(
        self, tool_call_id: str, tool_execution_id: str, *,
        expected_generation: str, expected_revision: int,
    ) -> ActiveRunView:
        _uuid4(tool_call_id, "tool_call_id")
        _uuid4(tool_execution_id, "tool_execution_id")
        with self._lock:
            self._guard(expected_generation, expected_revision)
            _require(all(tool.tool_execution_id != tool_execution_id for tool in self._view.tools),
                     "Tool execution identity has already been used")
            tools = list(self._view.tools)
            index = next((index for index, tool in enumerate(tools)
                          if tool.tool_call_id == tool_call_id), None)
            _require(index is not None and tools[index].status == "pending",
                     "Only a never-started pending tool can enter execution")
            self._first_unresolved(index)
            tools[index] = ToolProgress(tool_call_id, tool_execution_id, "started")
            return self._advance(tools=tuple(tools))

    def settle_tool(
        self, tool_call_id: str, outcome: str, *,
        expected_generation: str, expected_revision: int,
    ) -> ActiveRunView:
        _uuid4(tool_call_id, "tool_call_id")
        _require(type(outcome) is str and outcome in _TOOL_OUTCOMES,
                 "Unknown tool outcome")
        with self._lock:
            self._guard(expected_generation, expected_revision)
            tools = list(self._view.tools)
            index = next((index for index, tool in enumerate(tools)
                          if tool.tool_call_id == tool_call_id), None)
            _require(index is not None and tools[index].status in {"started", "unknown"},
                     "Only a started or uncertain tool can settle")
            self._first_unresolved(index)
            _require(tools[index].status != outcome, "Tool outcome has already been recorded")
            tools[index] = ToolProgress(tool_call_id, tools[index].tool_execution_id, outcome)
            return self._advance(tools=tuple(tools))

    def skip_tool(
        self, tool_call_id: str, *, expected_generation: str, expected_revision: int,
    ) -> ActiveRunView:
        """Close an accepted call without assigning a tool execution identity."""
        _uuid4(tool_call_id, "tool_call_id")
        with self._lock:
            self._guard(expected_generation, expected_revision)
            tools = list(self._view.tools)
            index = next((index for index, tool in enumerate(tools)
                          if tool.tool_call_id == tool_call_id), None)
            _require(index is not None and tools[index].status == "pending",
                     "Only a never-started pending tool can be skipped")
            self._first_unresolved(index)
            tools[index] = ToolProgress(tool_call_id, None, "never_started")
            return self._advance(tools=tuple(tools))

    def fence(self, *, expected_generation: str, expected_revision: int) -> ActiveRunView:
        """Revoke old callbacks; this does not stop an executor or settle tools."""
        with self._lock:
            self._guard(expected_generation, expected_revision)
            next_generation = str(uuid4())
            while next_generation == self._view.generation:
                next_generation = str(uuid4())
            return self._advance(generation=next_generation)
