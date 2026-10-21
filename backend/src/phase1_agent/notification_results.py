"""Separate a committed business result from its local notification stage (G12).

A round's business outcome (status, outputs, commit, checkpoint and end
manifest) is settled before the local notification stages run.  When such a
post-commit notification fails, the durable business result must stay
authoritative: the waiter receives this structured exception instead of a bare
failure, while the run query can expose the recorded notification diagnostics.
"""

from copy import deepcopy
import re

from .contract_errors import DiagnosticError


ROUND_NOTIFICATION_FAILURE = "round_committed_notification_failed"
NOTIFICATION_STAGES = ("chat_snapshot", "frontend_node_state")
# Public reason expressions never include exception text: notification errors
# can carry request, provider or local-path detail that must not reach a waiter,
# a persisted diagnostic or an operational log.
NOTIFICATION_FAILURE_REASON = "notification_stage_failed"
NOTIFICATION_DIAGNOSTIC_FAILURE = "notification_diagnostic_failed"
_SAFE_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}\Z")


def _product_reason(error) -> str | None:
    """Return the product-owned reason code, never an arbitrary exception attribute."""
    reason = getattr(error, "reason_code", None)
    if (isinstance(error, DiagnosticError) and type(reason) is str
            and _SAFE_REASON.fullmatch(reason) is not None):
        return reason
    return None


def _error_type(error) -> str:
    name = type(error).__name__
    return name if _SAFE_ERROR_TYPE.fullmatch(name) is not None else "Exception"


def public_stage_failure(error) -> dict:
    """Bounded public expression for one failed local notification stage.

    The exception text is deliberately omitted because it may contain request
    payload, provider output or another private value.  A product
    ``DiagnosticError`` keeps its classified reason code; every other failure is
    reported under one fixed code plus its bounded type name.
    """
    return {"reason_code": _product_reason(error) or NOTIFICATION_FAILURE_REASON,
            "error_type": _error_type(error)}


def public_diagnostic_failure(error) -> str:
    """Bounded public expression for a failed notification-diagnostic write."""
    return (f"{_product_reason(error) or NOTIFICATION_DIAGNOSTIC_FAILURE}: "
            f"{_error_type(error)}")


class CommittedNotificationError(DiagnosticError):
    """Business result committed; local completion notification failed.

    ``business_status`` and ``business_committed`` describe the already
    committed business result.  ``notifications`` records each stage as
    succeeded, failed (with a bounded redacted reason) or not_executed.  A
    failed diagnostic record itself is reported through
    ``notification_diagnostic_recorded`` / ``notification_diagnostic_error``.
    Neither field copies exception text into the structured error.
    """

    def __init__(self, *, chain_id: str, workflow_session_id: str, business_status: str,
                 notifications: list[dict], diagnostic_recorded: bool,
                 diagnostic_error: str | None = None):
        super().__init__(
            ROUND_NOTIFICATION_FAILURE,
            "Round result is committed; local completion notification failed",
            status=500, chain_id=chain_id, workflow_session_id=workflow_session_id)
        self.chain_id = chain_id
        self.workflow_session_id = workflow_session_id
        self.business_status = business_status
        self.business_committed = True
        self.notifications = deepcopy(notifications)
        self.notification_diagnostic_recorded = diagnostic_recorded
        self.notification_diagnostic_error = diagnostic_error

    def to_dict(self) -> dict:
        return {
            "reason_code": self.reason_code,
            "chain_id": self.chain_id,
            "workflow_session_id": self.workflow_session_id,
            "business_status": self.business_status,
            "business_committed": self.business_committed,
            "notifications": deepcopy(self.notifications),
            "notification_diagnostic_recorded": self.notification_diagnostic_recorded,
            "notification_diagnostic_error": self.notification_diagnostic_error,
        }


def run_notification_stages(stages) -> tuple[list[dict], BaseException | None]:
    """Run notification stages in order, stop at the first failure.

    Returns one result per stage plus the original failure.  Only the stage name,
    outcome and the bounded public expression of a failure are recorded; the
    original exception (including its text) is returned to the caller, never
    persisted or copied into the waiter's structured error.
    """
    results: list[dict] = []
    failure: BaseException | None = None
    for index, (stage, action) in enumerate(stages):
        try:
            action()
        except Exception as error:
            results.append({
                "stage": stage, "outcome": "failed", **public_stage_failure(error),
            })
            results.extend({"stage": name, "outcome": "not_executed"}
                           for name, _ in stages[index + 1:])
            failure = error
            break
        results.append({"stage": stage, "outcome": "succeeded"})
    return results, failure
