"""Workflow command dispatch and atomic public identities over existing actions."""

from __future__ import annotations

from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes
from .storage import ACTIVE_SESSION_SELECTION_ID, SqliteStore
from .workflow_control import WorkflowOperation, control_error, validate_dispatch_payload


RECEIPT_OPERATION = "workflow.operation"
_NATIVE_OPERATIONS = {
    "submit": "workflow.submit",
    "continue_pending_input": "workflow.pending",
    "retry_archive": "workflow.dispatch_retry_archive",
    "retry_publish": "workflow.dispatch_retry_publish",
    "interrupt": "workflow.interrupt",
    "resume": "workflow.resume",
    "extend_budget": "workflow.extend_budget",
    "reroll": "workflow.reroll",
    "close_execution": "workflow.close_execution",
    "continue_workflow": "workflow.continue_workflow",
    "select_candidate": "workflow.explicit_select_candidate",
    "create_branch": "workflow.fork",
    "create_and_switch_branch": "workflow.fork_switch",
    "switch_session": "workflow.switch_session",
}
_ACCEPTED_KINDS = {
    "submit", "continue_pending_input", "interrupt", "resume", "reroll",
    "continue_workflow", "retry_archive", "retry_publish",
}
_CURRENT_DISPATCH: ContextVar[DispatchIdentity | None] = ContextVar(
    "workflow_operation_dispatch", default=None,
)


def _error(message: str, status: int = 409, reason: str = "invalid_state") -> ContractValidationError:
    return control_error(message, reason, status=status)


@dataclass
class DispatchIdentity:
    operation: WorkflowOperation
    database: Path
    persisted: bool = False

    @property
    def receipt_key(self) -> str:
        scope = self.operation.scope
        return f"{scope['kind']}:{scope['id']}:{self.operation.idempotency_key}"

    @property
    def native_operation(self) -> str:
        return _NATIVE_OPERATIONS[self.operation.kind]

    @property
    def native_key(self) -> str:
        return self.operation.scope["id"] + ":" + self.operation.idempotency_key


def operation_store(path, *, fault_injector=None) -> SqliteStore:
    identity = _CURRENT_DISPATCH.get()
    if identity is not None and identity.database != Path(path).resolve():
        identity = None
    return _DispatchStore(
        path, fault_injector=fault_injector, identity=identity,
    )


def _replace_operation_reference(value: Any, old: str, new: str) -> Any:
    if type(value) is dict:
        return {
            key: new if key == "operation_id" and child == old
            else _replace_operation_reference(child, old, new)
            for key, child in value.items()
        }
    if type(value) is list:
        return [_replace_operation_reference(child, old, new) for child in value]
    return value


class _DispatchStore(SqliteStore):
    def __init__(self, path, *, fault_injector, identity):
        super().__init__(path, fault_injector=fault_injector)
        self._dispatch_identity = identity

    def _save_bundle_transaction(
        self, records, idempotency_key, *, operation="bundle",
        builder=None, additional_request_receipt=None, **kwargs,
    ):
        identity = self._dispatch_identity
        if identity is None or identity.persisted or operation != identity.native_operation:
            return super()._save_bundle_transaction(
                records, idempotency_key, operation=operation, builder=builder,
                additional_request_receipt=additional_request_receipt, **kwargs,
            )
        if additional_request_receipt is not None:
            raise _error("Dispatch boundary already has another public receipt")

        def public_records(values):
            native = next(
                (row for kind, row in values
                 if kind == "workflow_operation" and row["kind"] == identity.operation.kind),
                None,
            )
            if native is None:
                raise _error("Dispatch boundary has no matching workflow operation")
            return [
                (kind, _replace_operation_reference(
                    row, native["operation_id"], identity.operation.operation_id,
                ))
                for kind, row in values
            ]

        prepared_builder = None
        if builder is not None:
            def prepared_builder(store):
                return public_records(builder(store))
            values = records
        else:
            values = public_records(records)
        saved = super()._save_bundle_transaction(
            values, idempotency_key, operation=operation,
            builder=prepared_builder,
            additional_request_receipt=(
                RECEIPT_OPERATION, identity.receipt_key, identity.operation.request_digest,
                identity.operation.to_mapping(),
            ), **kwargs,
        )
        identity.persisted = True
        return saved


def accept_retry(service, store, *, session_id: str, run_id: str, chain_run_id: str) -> None:
    """Reserve a validated multi-transaction retry before its first side effect.

    Replays report accepted/unconfirmed and never repeat an unfinished retry.
    The session projection remains the source of truth for its actual outcome.
    """
    identity = _CURRENT_DISPATCH.get()
    if identity is None or identity.persisted:
        return
    if identity.operation.kind not in ("retry_archive", "retry_publish"):
        raise _error("Retry acceptance differs from its operation")
    if (identity.operation.scope["id"] != session_id
            or identity.operation.target["id"] != run_id
            or identity.database != service.database):
        raise _error("Retry acceptance identifies another workflow target", reason="ownership_mismatch")
    operation = identity.operation.to_mapping()
    operation["payload"] = {
        "chain_run_id": chain_run_id,
        "dispatch_accepted": True,
    }
    store.save_bundle(
        [("workflow_operation", operation)], identity.native_key,
        operation=identity.native_operation,
    )


def _project_result(service, operation: WorkflowOperation, records: list[dict]) -> dict[str, Any]:
    kind = operation.kind
    if kind in ("submit", "continue_pending_input"):
        return service._submission_receipt(records)
    if kind in ("create_branch", "create_and_switch_branch"):
        return service._fork_receipt(records, kind == "create_and_switch_branch")
    if kind in ("interrupt", "resume"):
        run = next(row for row in records if row.get("profile") == "agent")
        return service._run_control_receipt(records, "running" if kind == "resume" else run["status"])
    if kind == "extend_budget":
        return service._budget_receipt(records)
    if kind in ("reroll", "continue_workflow"):
        return service._reroll_receipt(records)
    if kind == "close_execution":
        return service._closeout_receipt(records)
    if kind == "switch_session":
        selection = next(row for row in records if "session_selection_id" in row)
        return {
            "active_workflow_session_id": selection["active_workflow_session_id"],
            "revision": selection["revision"],
        }
    if kind == "select_candidate":
        head = next(row for row in records if "workflow_ref_id" in row)
        session = next(
            row for row in records if row.get("workflow_session_id") == operation.scope["id"]
            and "definition_revision" in row and "source" in row and "revision" in row
        )
        return {
            "workflow_session_id": operation.scope["id"], "candidate_id": operation.target["id"],
            "head_commit_id": head["head_commit_id"], "ref_revision": head["revision"],
            "session_revision": session["revision"], "status": "succeeded",
        }
    if kind in ("retry_archive", "retry_publish"):
        primary = next(row for row in records if row.get("operation_id") == operation.operation_id)
        return {
            "workflow_session_id": operation.scope["id"], "run_id": operation.target["id"],
            "chain_run_id": primary["payload"]["chain_run_id"],
            "status": "accepted", "completion": "unconfirmed",
        }
    raise _error("Unsupported workflow operation", 400, "unsupported")


def _public_receipt(service, operation, records):
    return {
        "operation_id": operation.operation_id,
        "kind": operation.kind,
        "status": "accepted" if operation.kind in _ACCEPTED_KINDS else "completed",
        "result": _project_result(service, operation, records),
    }


def operation_http_status(kind: str) -> int:
    if kind in _ACCEPTED_KINDS:
        return 202
    return 201 if kind in ("create_branch", "create_and_switch_branch") else 200


def _check_identity(store, identity: DispatchIdentity):
    operation = identity.operation
    prior = store.read_receipt_with_digest(RECEIPT_OPERATION, identity.receipt_key)
    if prior is not None:
        if prior[0] != operation.request_digest:
            raise _error("Idempotency key has different workflow operation request",
                         reason="idempotency_conflict")
        return prior[1]
    previous = next(
        (row for row in store.list_records("workflow_operation")
         if row["operation_id"] == operation.operation_id), None,
    )
    if previous is not None:
        raise _error("Operation ID already belongs to another request", reason="idempotency_conflict")
    if store.read_receipt(identity.native_operation, identity.native_key) is not None:
        raise _error("Idempotency key already belongs to a legacy operation",
                     reason="idempotency_conflict")
    return None


def _check_revisions(service, store, operation):
    scope = operation.scope
    if scope["kind"] == "workflow_session":
        session = service._session(store, scope["id"])
    else:
        if scope["id"] != ACTIVE_SESSION_SELECTION_ID:
            raise _error("Session selection scope is not owned by this workflow service",
                         reason="ownership_mismatch")
        session = None
    head = None
    revisions = {item["kind"]: item for item in operation.expected_revisions}
    for kind, expected in revisions.items():
        if kind == "workflow_session":
            current = session
        elif kind == "workflow_ref":
            current = service._workflow_ref(store, scope["id"])
            head = current
            if current["workflow_ref_id"] != expected["id"]:
                raise _error("Workflow ref belongs to another session", reason="ownership_mismatch")
            if current["head_commit_id"] != operation.expected_head_commit_id:
                raise _error("Workflow ref Head conflict", reason="stale_revision")
        elif kind == "run_record":
            current = store.get_record("run_record", {"run_id": expected["id"]})
            if current["workflow_session_id"] != scope["id"]:
                raise _error("Run belongs to another workflow session", reason="ownership_mismatch")
        else:
            if expected["id"] != ACTIVE_SESSION_SELECTION_ID:
                raise _error("Session selection revision identifies another selection",
                             reason="ownership_mismatch")
            current = service._session_selection(store)
        if current is None or current["revision"] != expected["revision"]:
            raise _error("Workflow operation revision conflict", reason="stale_revision")
    return session, head, revisions


def _delegate(service, operation, session, revisions):
    kind = operation.kind
    session_id, target = operation.scope["id"], operation.target["id"]
    options = {"idempotency_key": operation.idempotency_key}
    if kind == "switch_session":
        return service.switch_session(
            target, **options,
            expected_selection_revision=revisions["session_selection"]["revision"],
        )
    revision = session["revision"]
    if kind == "submit":
        return service.submit(session_id, operation.payload["text"], operation.idempotency_key)
    if kind in ("create_branch", "create_and_switch_branch"):
        branch = service.create_branch if kind == "create_branch" else service.create_and_switch_branch
        if kind == "create_and_switch_branch":
            options["expected_selection_revision"] = revisions["session_selection"]["revision"]
        return branch(
            session_id, target, **options, **operation.payload,
            expected_source_revision=revision,
        )
    options["expected_session_revision"] = revision
    if kind in ("interrupt", "resume", "extend_budget"):
        options["expected_run_revision"] = revisions["run_record"]["revision"]
        return getattr(service, kind)(session_id, target, **options, **operation.payload)
    if kind in ("reroll", "close_execution", "continue_workflow", "select_candidate"):
        options.update(
            expected_ref_revision=revisions["workflow_ref"]["revision"],
            expected_head_commit_id=operation.expected_head_commit_id,
        )
    return getattr(service, kind)(session_id, target, **options)


def dispatch_workflow_operation(service, value: dict[str, Any] | WorkflowOperation) -> dict[str, Any]:
    try:
        operation = value if isinstance(value, WorkflowOperation) else WorkflowOperation(value)
        validate_dispatch_payload(operation)
    except ContractValidationError as exc:
        exc.status_code = getattr(exc, "status_code", 400)
        exc.reason_code = getattr(exc, "reason_code", "invalid_request")
        raise
    if operation.kind == "initialize_session":
        raise _error("Session initialization is only exposed through session creation", 400, "unsupported")
    identity = DispatchIdentity(operation, service.database)
    with service._lock, closing(service._store()) as store:
        service._check_open()
        prior = _check_identity(store, identity)
        if prior is not None:
            return _public_receipt(service, operation, prior)
        session, _, revisions = _check_revisions(service, store, operation)
        if operation.kind in ("retry_archive", "retry_publish"):
            existing = (
                service._archive_retry_receipts if operation.kind == "retry_archive"
                else service._publish_retry_receipts
            )
            if (operation.scope["id"], operation.idempotency_key) in existing:
                raise _error("Idempotency key already belongs to a legacy operation",
                             reason="idempotency_conflict")
        token = _CURRENT_DISPATCH.set(identity)
        try:
            _delegate(service, operation, session, revisions)
        except ContractValidationError as exc:
            if not hasattr(exc, "reason_code"):
                exc.reason_code = "invalid_state"
            raise
        finally:
            _CURRENT_DISPATCH.reset(token)
        receipt = store.read_receipt(RECEIPT_OPERATION, identity.receipt_key)
        if receipt is None:
            raise _error("Workflow operation did not cross its durable dispatch boundary")
        return _public_receipt(service, operation, receipt)
