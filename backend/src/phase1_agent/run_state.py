"""Pure lifecycle and control guards for the current Agent profile.

The caller owns persistence, command serialization, execution, and reconciliation.
These predicates do not create checkpoints or stop in-flight work.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum

from .contract_errors import ContractValidationError


class AgentRunStatus(str, Enum):
    PREPARED = "prepared"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    FAILED = "failed"
    FINAL_READY = "final_ready"
    SUCCEEDED = "succeeded"
    SUPERSEDED = "superseded"
    RECOVERY_UNAVAILABLE = "recovery_unavailable"


@dataclass(frozen=True)
class RunSafety:
    """Evidence supplied by the run owner, not a durable execution snapshot.

    ``checkpoint_available`` means an intact same-process checkpoint.
    ``unsettled_tools`` means started tools awaiting a known outcome; never-started
    pending calls may remain in a paused checkpoint. Unknown outcomes remain
    blocking even after execution has stopped.
    """

    execution_stopped: bool = False
    checkpoint_available: bool = False
    unsettled_tools: bool = True
    unknown_tool_outcomes: bool = True
    budget_available: bool = False
    final_validated: bool = False
    archive_committed: bool = False
    start_persisted: bool = False

    def __post_init__(self) -> None:
        for field in fields(self):
            if type(getattr(self, field.name)) is not bool:
                raise ContractValidationError(f"safety.{field.name} must be a bool")


def _status(value: AgentRunStatus, name: str) -> AgentRunStatus:
    if not isinstance(value, AgentRunStatus):
        raise ContractValidationError(f"{name} must be an AgentRunStatus")
    return value


def _safety(value: RunSafety) -> RunSafety:
    if not isinstance(value, RunSafety):
        raise ContractValidationError("safety must be a RunSafety")
    return value


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ContractValidationError(reason)


def _safe_to_close(safety: RunSafety) -> None:
    _require(safety.execution_stopped, "old execution has not stopped")
    _require(not safety.unsettled_tools, "started tools have not settled")
    _require(not safety.unknown_tool_outcomes, "tool outcomes require reconciliation")


def validate_transition(current: AgentRunStatus, target: AgentRunStatus, safety: RunSafety) -> None:
    """Reject an illegal public-state edge or an edge lacking safety evidence.

    Repeated states (including a failed archive retry) are not transitions.
    Reroll of a succeeded run creates a new candidate and leaves this state intact.
    """

    current = _status(current, "current")
    target = _status(target, "target")
    safety = _safety(safety)
    _require(current != target, "unchanged state is not a transition")
    _require(current not in (AgentRunStatus.SUCCEEDED, AgentRunStatus.SUPERSEDED),
             "terminal run cannot change state")

    if target is AgentRunStatus.RECOVERY_UNAVAILABLE:
        _require(current is not AgentRunStatus.RECOVERY_UNAVAILABLE,
                 "recovery is already unavailable")
        _require(safety.start_persisted, "recovery requires a persisted run start")
        _require(not safety.checkpoint_available, "active checkpoint is still available")
        _require(not safety.archive_committed, "committed run cannot lose recovery")
        return

    edges = {
        AgentRunStatus.PREPARED: {AgentRunStatus.RUNNING, AgentRunStatus.PAUSED},
        AgentRunStatus.RUNNING: {
            AgentRunStatus.PAUSING, AgentRunStatus.FAILED, AgentRunStatus.FINAL_READY,
        },
        AgentRunStatus.PAUSING: {AgentRunStatus.PAUSED, AgentRunStatus.FAILED},
        AgentRunStatus.PAUSED: {AgentRunStatus.RUNNING, AgentRunStatus.SUPERSEDED},
        AgentRunStatus.FAILED: {AgentRunStatus.RUNNING, AgentRunStatus.SUPERSEDED},
        AgentRunStatus.FINAL_READY: {AgentRunStatus.SUCCEEDED},
        AgentRunStatus.RECOVERY_UNAVAILABLE: {AgentRunStatus.SUPERSEDED},
    }
    _require(target in edges.get(current, set()), f"illegal transition: {current.value} -> {target.value}")
    _require(safety.start_persisted, "run start and frozen input must be persisted")
    if current is AgentRunStatus.RECOVERY_UNAVAILABLE:
        _require(not safety.checkpoint_available, "recovery-unavailable run has an active checkpoint")

    if target is AgentRunStatus.PAUSING:
        _require(not safety.archive_committed, "committed run cannot pause")
        _require(not safety.final_validated, "accepted final cannot be paused")
        return

    if target is AgentRunStatus.FAILED:
        _require(safety.execution_stopped, "failed run must stop execution")
        _require(not safety.archive_committed, "committed run cannot fail")
        _require(not safety.final_validated, "accepted final must await archive")
        return

    if target is AgentRunStatus.PAUSED:
        _safe_to_close(safety)
        _require(not safety.archive_committed, "committed run cannot pause")
        _require(not safety.final_validated, "accepted final cannot be paused")
        return

    if target is AgentRunStatus.FINAL_READY:
        _safe_to_close(safety)
        _require(safety.final_validated, "final has not been validated")
        _require(not safety.archive_committed, "final is already committed")
        return

    if target is AgentRunStatus.SUCCEEDED:
        _safe_to_close(safety)
        _require(safety.final_validated, "final has not been validated")
        _require(safety.archive_committed, "final archive has not committed")
        return

    _safe_to_close(safety)
    _require(not safety.archive_committed, "committed run cannot resume or be superseded")
    _require(not safety.final_validated, "validated final must be archived, not resumed or rerolled")
    if target is AgentRunStatus.RUNNING:
        _require(safety.budget_available, "run budget is exhausted")
        if current in (AgentRunStatus.PAUSED, AgentRunStatus.FAILED):
            _require(safety.checkpoint_available, "in-process checkpoint is unavailable")


def available_actions(status: AgentRunStatus, safety: RunSafety) -> frozenset[str]:
    """Project possible controls; acceptance still requires a serialized command.

    ``start`` is the initial dispatch gate. ``reroll`` on a succeeded run is a
    new candidate, never a transition of the successful run to superseded.
    """

    status = _status(status, "status")
    safety = _safety(safety)
    actions: set[str] = set()
    candidates = {
        AgentRunStatus.PREPARED: (("start", AgentRunStatus.RUNNING), ("pause", AgentRunStatus.PAUSED)),
        AgentRunStatus.RUNNING: (("pause", AgentRunStatus.PAUSING),),
        AgentRunStatus.PAUSED: (("resume", AgentRunStatus.RUNNING), ("reroll", AgentRunStatus.SUPERSEDED)),
        AgentRunStatus.FAILED: (("resume", AgentRunStatus.RUNNING), ("reroll", AgentRunStatus.SUPERSEDED)),
        AgentRunStatus.RECOVERY_UNAVAILABLE: (("reroll", AgentRunStatus.SUPERSEDED),),
    }
    for action, target in candidates.get(status, ()):
        try:
            validate_transition(status, target, safety)
        except ContractValidationError:
            continue
        actions.add(action)

    if status in (AgentRunStatus.PAUSED, AgentRunStatus.FAILED):
        if (safety.start_persisted and safety.checkpoint_available
                and safety.execution_stopped and not safety.unsettled_tools
                and not safety.unknown_tool_outcomes and not safety.final_validated
                and not safety.archive_committed and not safety.budget_available):
            actions.add("extend_budget")
    if status is AgentRunStatus.FINAL_READY:
        if (safety.start_persisted and safety.final_validated and not safety.archive_committed
                and safety.execution_stopped and not safety.unsettled_tools
                and not safety.unknown_tool_outcomes):
            actions.add("retry_archive")
    if status is AgentRunStatus.SUCCEEDED:
        if (safety.start_persisted and safety.archive_committed and safety.final_validated
                and safety.execution_stopped and not safety.unsettled_tools
                and not safety.unknown_tool_outcomes):
            actions.add("reroll")
    return frozenset(actions)
