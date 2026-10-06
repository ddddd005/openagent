"""Pure workflow-operation contracts; dispatch and persistence live elsewhere.

LegacyControlRequest describes the five existing service actions without
changing their HTTP bodies. WorkflowOperation validates the v1 envelope;
WorkflowService additionally validates action-specific payloads and live state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value


_LEGACY_TARGETS = {
    "retry_archive": "run_record",
    "retry_publish": "run_record",
    "create_branch": "visible_message",
    "create_and_switch_branch": "visible_message",
    "continue_pending_input": "node_input",
}

# Each operation is routed by a workflow-level owner even when its target is a
# run, message, or candidate. Relationship and state checks remain the service's
# responsibility; this table only fixes the public type/revision vocabulary.
_OPERATION_RULES: dict[str, tuple[str, str, frozenset[str]]] = {
    "initialize_session": ("workflow_session", "workflow_session", frozenset()),
    "submit": ("workflow_session", "workflow_session", frozenset({"workflow_session"})),
    "continue_pending_input": ("workflow_session", "node_input", frozenset({"workflow_session"})),
    "retry_archive": ("workflow_session", "run_record", frozenset({"workflow_session"})),
    "retry_publish": ("workflow_session", "run_record", frozenset({"workflow_session"})),
    "interrupt": ("workflow_session", "run_record", frozenset({"workflow_session", "run_record"})),
    "resume": ("workflow_session", "run_record", frozenset({"workflow_session", "run_record"})),
    "extend_budget": (
        "workflow_session", "run_record", frozenset({"workflow_session", "run_record"}),
    ),
    "reroll": ("workflow_session", "chain_run", frozenset({"workflow_session", "workflow_ref"})),
    "close_execution": (
        "workflow_session", "chain_run", frozenset({"workflow_session", "workflow_ref"}),
    ),
    "continue_workflow": (
        "workflow_session", "chain_run", frozenset({"workflow_session", "workflow_ref"}),
    ),
    "select_candidate": ("workflow_session", "candidate", frozenset({"workflow_ref"})),
    "create_branch": (
        "workflow_session", "visible_message", frozenset({"workflow_session", "workflow_ref"}),
    ),
    "create_and_switch_branch": (
        "workflow_session", "visible_message",
        frozenset({"workflow_session", "workflow_ref", "session_selection"}),
    ),
    "switch_session": ("session_selection", "workflow_session", frozenset({"session_selection"})),
}

_ENVELOPE_FIELDS = {
    "schema_version", "operation_id", "kind", "scope", "target",
    "idempotency_key", "expected_revisions", "payload",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def control_error(message: str, reason_code: str, *, status: int = 409) -> ContractValidationError:
    """Attach a bounded public refusal category without exposing private text."""
    error = ContractValidationError(message)
    error.status_code = status
    error.reason_code = reason_code
    return error


def _uuid4(value: Any, field: str) -> None:
    _require(type(value) is str, f"{field} must be a canonical UUID v4")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ContractValidationError(f"{field} must be a canonical UUID v4") from exc
    _require(parsed.version == 4 and str(parsed) == value, f"{field} must be a canonical UUID v4")


def _key(value: Any) -> None:
    _require(type(value) is str and 1 <= len(value) <= 128, "A bounded idempotency key is required")
    validate_json_value(value)


def _revision(value: Any) -> None:
    _require(type(value) is int and value >= 1, "Expected revisions must be positive integers")


def _digest(value: dict[str, Any]) -> str:
    return "workflow-op-v1:" + content_digest(value).rsplit(":", 1)[1]


def _reference(value: Any, field: str) -> dict[str, str]:
    _require(type(value) is dict and set(value) == {"kind", "id"}, f"{field} must be a typed reference")
    _require(type(value["kind"]) is str and bool(value["kind"]), f"{field}.kind is invalid")
    _uuid4(value["id"], f"{field}.id")
    return {"kind": value["kind"], "id": value["id"]}


def _no_generation(value: Any) -> None:
    if type(value) is dict:
        _require("generation" not in value, "Executor generation is internal")
        for child in value.values():
            _no_generation(child)
    elif type(value) is list:
        for child in value:
            _no_generation(child)


@dataclass(frozen=True)
class LegacyControlRequest:
    """Normalized, immutable identity for one of the existing five actions."""

    kind: str
    workflow_session_id: str
    target_id: str
    idempotency_key: str
    expected_revision: int

    def __post_init__(self) -> None:
        _require(type(self.kind) is str and self.kind in _LEGACY_TARGETS, "Unknown legacy action")
        _uuid4(self.workflow_session_id, "workflow_session_id")
        _uuid4(self.target_id, "target_id")
        _key(self.idempotency_key)
        _revision(self.expected_revision)

    @property
    def scope(self) -> dict[str, str]:
        return {"kind": "workflow_session", "id": self.workflow_session_id}

    @property
    def target_kind(self) -> str:
        return _LEGACY_TARGETS[self.kind]

    @property
    def target(self) -> dict[str, str]:
        return {"kind": self.target_kind, "id": self.target_id}

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "target": self.target,
            "idempotency_key": self.idempotency_key,
            "expected_revisions": [
                {"kind": "workflow_session", "id": self.workflow_session_id,
                 "revision": self.expected_revision},
            ],
        }

    @property
    def request_digest(self) -> str:
        return _digest(self.to_mapping())


@dataclass(frozen=True)
class RunControlRequest:
    """Stable request identity for one in-process run control command."""

    kind: str
    workflow_session_id: str
    chain_run_id: str
    run_id: str
    idempotency_key: str
    expected_session_revision: int
    expected_run_revision: int

    def __post_init__(self) -> None:
        _require(self.kind in ("interrupt", "resume"), "Unknown run control action")
        for name in ("workflow_session_id", "chain_run_id", "run_id"):
            _uuid4(getattr(self, name), name)
        _key(self.idempotency_key)
        _revision(self.expected_session_revision)
        _revision(self.expected_run_revision)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": {"kind": "workflow_session", "id": self.workflow_session_id},
            "target": {"kind": "run_record", "id": self.run_id},
            "idempotency_key": self.idempotency_key,
            "expected_revisions": [
                {"kind": "workflow_session", "id": self.workflow_session_id,
                 "revision": self.expected_session_revision},
                {"kind": "run_record", "id": self.run_id,
                 "revision": self.expected_run_revision},
            ],
            "payload": {"chain_run_id": self.chain_run_id},
        }

    @property
    def request_digest(self) -> str:
        return _digest(self.to_mapping())


@dataclass(frozen=True)
class BudgetControlRequest:
    """Explicit request and transport-attempt allowance for one stopped run."""

    kind: str
    workflow_session_id: str
    chain_run_id: str
    run_id: str
    idempotency_key: str
    expected_session_revision: int
    expected_run_revision: int
    additional_model_requests: int = 0
    additional_model_attempts: int = 0

    def __post_init__(self) -> None:
        _require(self.kind == "extend_budget", "Unknown budget control action")
        for name in ("workflow_session_id", "chain_run_id", "run_id"):
            _uuid4(getattr(self, name), name)
        _key(self.idempotency_key)
        _revision(self.expected_session_revision)
        _revision(self.expected_run_revision)
        for name, maximum in (
            ("additional_model_requests", 64), ("additional_model_attempts", 256),
        ):
            value = getattr(self, name)
            _require(type(value) is int and 0 <= value <= maximum,
                     f"{name} must be an integer between 0 and {maximum}")
        _require(self.additional_model_requests + self.additional_model_attempts > 0,
                 "Budget extension must add requests or attempts")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": {"kind": "workflow_session", "id": self.workflow_session_id},
            "target": {"kind": "run_record", "id": self.run_id},
            "idempotency_key": self.idempotency_key,
            "expected_revisions": [
                {"kind": "workflow_session", "id": self.workflow_session_id,
                 "revision": self.expected_session_revision},
                {"kind": "run_record", "id": self.run_id,
                 "revision": self.expected_run_revision},
            ],
            "payload": {
                "chain_run_id": self.chain_run_id,
                "additional_model_requests": self.additional_model_requests,
                "additional_model_attempts": self.additional_model_attempts,
            },
        }

    @property
    def request_digest(self) -> str:
        return _digest(self.to_mapping())


def validate_workflow_operation(value: Any) -> dict[str, Any]:
    """Validate an operation envelope without consulting live state.

    Revisions for the scope or target must name that exact ID. For a ref or
    selection pointer, database ownership is checked by the coordinator.
    """
    _require(type(value) is dict, "Workflow operation must be an object")
    _require(
        _ENVELOPE_FIELDS <= set(value)
        and set(value) <= _ENVELOPE_FIELDS | {"expected_head_commit_id"},
        "Workflow operation has missing or unknown fields",
    )
    validate_json_value(value)
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1,
             "Unsupported workflow operation schema version")
    _uuid4(value["operation_id"], "operation_id")
    kind = value["kind"]
    if kind == "steer":
        raise control_error("Steering is not supported", "unsupported", status=400)
    _require(type(kind) is str and kind in _OPERATION_RULES, "Unknown workflow operation kind")
    scope_kind, target_kind, revision_kinds = _OPERATION_RULES[kind]
    scope = _reference(value["scope"], "scope")
    target = _reference(value["target"], "target")
    _require(scope["kind"] == scope_kind, "Wrong operation scope kind")
    _require(target["kind"] == target_kind, "Wrong operation target kind")
    if kind in ("initialize_session", "submit"):
        _require(scope["id"] == target["id"], "Submission target must be its owning session")
    _key(value["idempotency_key"])
    expected = value["expected_revisions"]
    _require(type(expected) is list and len(expected) == len(revision_kinds),
             "Wrong expected revision scopes")
    normalized_revisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in expected:
        _require(type(item) is dict and set(item) == {"kind", "id", "revision"},
                 "Expected revision must identify one object")
        revision_kind = item["kind"]
        _require(type(revision_kind) is str and revision_kind in revision_kinds
                 and revision_kind not in seen, "Wrong or duplicate expected revision scope")
        _uuid4(item["id"], "expected_revisions.id")
        _revision(item["revision"])
        if revision_kind == scope["kind"]:
            _require(item["id"] == scope["id"], "Scope revision identifies another object")
        if revision_kind == target["kind"]:
            _require(item["id"] == target["id"], "Target revision identifies another object")
        seen.add(revision_kind)
        normalized_revisions.append(dict(item))
    _require(seen == revision_kinds, "Missing expected revision scope")
    _require(type(value["payload"]) is dict, "Operation payload must be a JSON object")
    _no_generation(value["payload"])
    if "expected_head_commit_id" in value:
        _require("workflow_ref" in revision_kinds, "Head expectation requires a workflow ref revision")
        if value["expected_head_commit_id"] is not None:
            _uuid4(value["expected_head_commit_id"], "expected_head_commit_id")
    result = {
        "schema_version": 1,
        "operation_id": value["operation_id"],
        "kind": kind,
        "scope": scope,
        "target": target,
        "idempotency_key": value["idempotency_key"],
        "expected_revisions": sorted(normalized_revisions, key=lambda item: (item["kind"], item["id"])),
        "payload": value["payload"],
    }
    if "expected_head_commit_id" in value:
        result["expected_head_commit_id"] = value["expected_head_commit_id"]
    return loads_strict(canonical_bytes(result).decode("utf-8"))


@dataclass(frozen=True, init=False)
class WorkflowOperation:
    """Immutable validated envelope for the WorkflowService dispatcher."""

    _encoded: bytes

    def __init__(self, value: dict[str, Any]) -> None:
        object.__setattr__(self, "_encoded", canonical_bytes(validate_workflow_operation(value)))

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> WorkflowOperation:
        return cls(value)

    def to_mapping(self) -> dict[str, Any]:
        return loads_strict(self._encoded.decode("utf-8"))

    @property
    def operation_id(self) -> str:
        return self.to_mapping()["operation_id"]

    @property
    def kind(self) -> str:
        return self.to_mapping()["kind"]

    @property
    def scope(self) -> dict[str, str]:
        return self.to_mapping()["scope"]

    @property
    def target(self) -> dict[str, str]:
        return self.to_mapping()["target"]

    @property
    def idempotency_key(self) -> str:
        return self.to_mapping()["idempotency_key"]

    @property
    def expected_revisions(self) -> list[dict[str, Any]]:
        return self.to_mapping()["expected_revisions"]

    @property
    def expected_head_commit_id(self) -> str | None:
        return self.to_mapping().get("expected_head_commit_id")

    @property
    def payload(self) -> dict[str, Any]:
        return self.to_mapping()["payload"]

    @property
    def request_digest(self) -> str:
        return _digest(self.to_mapping())


def validate_dispatch_payload(operation: WorkflowOperation) -> None:
    """Validate caller-owned fields, not the service's enriched stored payload."""
    kind, payload = operation.kind, operation.payload
    if kind == "submit":
        _require(set(payload) == {"text"}, "Submission payload requires only text")
        _require(
            type(payload["text"]) is str and bool(payload["text"].strip())
            and len(payload["text"]) <= 12000,
            "Input must contain 1 to 12000 characters",
        )
    elif kind == "extend_budget":
        fields = {"additional_model_requests", "additional_model_attempts"}
        _require(set(payload) == fields, "Budget payload must identify both additions")
        for field, maximum in (
            ("additional_model_requests", 64), ("additional_model_attempts", 256),
        ):
            _require(type(payload[field]) is int and 0 <= payload[field] <= maximum,
                     "Budget addition is outside its bounded limit")
        _require(sum(payload.values()) > 0, "Budget extension must add requests or attempts")
    elif kind in ("create_branch", "create_and_switch_branch"):
        _require(set(payload) <= {"candidate_id"}, "Unknown branch payload field")
        if "candidate_id" in payload:
            _uuid4(payload["candidate_id"], "payload.candidate_id")
    else:
        _require(not payload, "This operation requires an empty payload")
    if any(item["kind"] == "workflow_ref" for item in operation.expected_revisions):
        _require("expected_head_commit_id" in operation.to_mapping(),
                 "Workflow ref operations require an explicit Head expectation")
