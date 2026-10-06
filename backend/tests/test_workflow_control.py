import copy
import re

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import content_digest
from phase1_agent.workflow_control import (
    BudgetControlRequest, LegacyControlRequest, RunControlRequest, WorkflowOperation,
    validate_workflow_operation,
)


SESSION = "a1111111-1111-4111-8111-111111111111"
OTHER = "b2222222-2222-4222-8222-222222222222"
RUN = "c3333333-3333-4333-8333-333333333333"
REF = "d4444444-4444-4444-8444-444444444444"
SELECTION = "e5555555-5555-4555-8555-555555555555"
COMMIT = "f6666666-6666-4666-8666-666666666666"
OPERATION = "a7777777-7777-4777-8777-777777777777"


@pytest.mark.parametrize(
    ("kind", "target_kind"),
    [
        ("retry_archive", "run_record"),
        ("retry_publish", "run_record"),
        ("create_branch", "visible_message"),
        ("create_and_switch_branch", "visible_message"),
        ("continue_pending_input", "node_input"),
    ],
)
def test_legacy_control_request_target_scope_and_stable_digest(kind, target_kind):
    request = LegacyControlRequest(kind, SESSION, RUN, "repeat", 2)
    same = LegacyControlRequest(kind, SESSION, RUN, "repeat", 2)
    assert request.scope == {"kind": "workflow_session", "id": SESSION}
    assert request.target_kind == target_kind
    assert request.target == {"kind": target_kind, "id": RUN}
    assert request.request_digest == same.request_digest
    assert request.request_digest == (
        "workflow-op-v1:" + content_digest(request.to_mapping()).rsplit(":", 1)[1]
    )
    assert re.fullmatch(r"workflow-op-v1:[0-9a-f]{64}", request.request_digest)
    assert request.request_digest != LegacyControlRequest(kind, SESSION, OTHER, "repeat", 2).request_digest
    assert request.request_digest != LegacyControlRequest(kind, SESSION, RUN, "repeat", 3).request_digest


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("kind", "reroll"),
        ("kind", 5),
        ("workflow_session_id", "s1"),
        ("workflow_session_id", "A1111111-1111-4111-8111-111111111111"),
        ("target_id", "a1111111-1111-1111-8111-111111111111"),
        ("idempotency_key", ""),
        ("idempotency_key", "x" * 129),
        ("idempotency_key", "\ud800"),
        ("expected_revision", 0),
        ("expected_revision", True),
    ],
)
def test_legacy_control_request_rejects_invalid_fields(change, value):
    fields = {
        "kind": "retry_archive", "workflow_session_id": SESSION, "target_id": RUN,
        "idempotency_key": "key", "expected_revision": 1,
    }
    fields[change] = value
    with pytest.raises(ContractValidationError):
        LegacyControlRequest(**fields)


@pytest.mark.parametrize("kind", ["interrupt", "resume"])
def test_run_control_request_fingerprints_chain_target_and_both_revisions(kind):
    request = RunControlRequest(kind, SESSION, OTHER, RUN, "once", 7, 3)
    assert request.to_mapping()["target"] == {"kind": "run_record", "id": RUN}
    assert request.to_mapping()["expected_revisions"] == [
        {"kind": "workflow_session", "id": SESSION, "revision": 7},
        {"kind": "run_record", "id": RUN, "revision": 3},
    ]
    assert request.request_digest == RunControlRequest(
        kind, SESSION, OTHER, RUN, "once", 7, 3,
    ).request_digest
    assert request.request_digest != RunControlRequest(
        kind, SESSION, OTHER, RUN, "once", 7, 4,
    ).request_digest
    assert request.request_digest != RunControlRequest(
        kind, SESSION, REF, RUN, "once", 7, 3,
    ).request_digest


@pytest.mark.parametrize("change,value", [
    ("kind", "reroll"), ("workflow_session_id", "wrong"),
    ("chain_run_id", "wrong"), ("run_id", RUN.upper()),
    ("idempotency_key", ""), ("expected_session_revision", True),
    ("expected_run_revision", 0),
])
def test_run_control_request_rejects_invalid_identity_or_revision(change, value):
    values = {
        "kind": "interrupt", "workflow_session_id": SESSION,
        "chain_run_id": OTHER, "run_id": RUN, "idempotency_key": "once",
        "expected_session_revision": 2, "expected_run_revision": 3,
    }
    values[change] = value
    with pytest.raises(ContractValidationError):
        RunControlRequest(**values)


_RULES = [
    ("initialize_session", "workflow_session", "workflow_session", ()),
    ("submit", "workflow_session", "workflow_session", ("workflow_session",)),
    ("continue_pending_input", "workflow_session", "node_input", ("workflow_session",)),
    ("retry_archive", "workflow_session", "run_record", ("workflow_session",)),
    ("retry_publish", "workflow_session", "run_record", ("workflow_session",)),
    ("interrupt", "workflow_session", "run_record", ("workflow_session", "run_record")),
    ("resume", "workflow_session", "run_record", ("workflow_session", "run_record")),
    ("extend_budget", "workflow_session", "run_record", ("workflow_session", "run_record")),
    ("reroll", "workflow_session", "chain_run", ("workflow_session", "workflow_ref")),
    ("select_candidate", "workflow_session", "candidate", ("workflow_ref",)),
    ("create_branch", "workflow_session", "visible_message", ("workflow_session", "workflow_ref")),
    (
        "create_and_switch_branch", "workflow_session", "visible_message",
        ("workflow_session", "workflow_ref", "session_selection"),
    ),
    ("switch_session", "session_selection", "workflow_session", ("session_selection",)),
]


def _record(kind="reroll"):
    _, scope_kind, target_kind, revisions = next(rule for rule in _RULES if rule[0] == kind)
    scope_id = SELECTION if scope_kind == "session_selection" else SESSION
    target_id = SESSION if kind in ("initialize_session", "submit", "switch_session") else RUN
    ids = {
        "workflow_session": SESSION,
        "run_record": RUN,
        "workflow_ref": REF,
        "session_selection": SELECTION,
    }
    return {
        "schema_version": 1,
        "operation_id": OPERATION,
        "kind": kind,
        "scope": {"kind": scope_kind, "id": scope_id},
        "target": {"kind": target_kind, "id": target_id},
        "idempotency_key": "once",
        "expected_revisions": [
            {"kind": name, "id": ids[name], "revision": 2} for name in revisions
        ],
        "payload": {},
    }


@pytest.mark.parametrize("kind", [rule[0] for rule in _RULES])
def test_planned_operation_accepts_only_matching_target_and_revision_scopes(kind):
    raw = _record(kind)
    parsed = validate_workflow_operation(raw)
    operation = WorkflowOperation.from_mapping(raw)
    assert parsed == operation.to_mapping()
    assert operation.operation_id == OPERATION
    assert operation.kind == kind
    assert operation.scope == raw["scope"]
    assert operation.target == raw["target"]
    assert operation.idempotency_key == "once"
    assert re.fullmatch(r"workflow-op-v1:[0-9a-f]{64}", operation.request_digest)
    assert operation.request_digest == WorkflowOperation(raw).request_digest

    wrong_target = copy.deepcopy(raw)
    wrong_target["target"]["kind"] = (
        "run_record" if raw["target"]["kind"] == "workflow_session" else "workflow_session"
    )
    with pytest.raises(ContractValidationError):
        validate_workflow_operation(wrong_target)

    wrong_revision = copy.deepcopy(raw)
    if wrong_revision["expected_revisions"]:
        wrong_revision["expected_revisions"][0]["kind"] = "candidate"
    else:
        wrong_revision["expected_revisions"].append(
            {"kind": "workflow_session", "id": SESSION, "revision": 1}
        )
    with pytest.raises(ContractValidationError):
        validate_workflow_operation(wrong_revision)


def test_envelope_normalizes_revision_order_and_keeps_payload_immutable():
    raw = _record("reroll")
    raw["payload"] = {"nested": {"a": [1, True, None]}, "text": "你好"}
    raw["expected_head_commit_id"] = COMMIT
    operation = WorkflowOperation(raw)
    digest = operation.request_digest
    reversed_order = copy.deepcopy(raw)
    reversed_order["expected_revisions"].reverse()
    assert WorkflowOperation(reversed_order).request_digest == digest
    assert operation.expected_head_commit_id == COMMIT
    assert operation.payload == raw["payload"]
    raw["payload"]["nested"]["a"].append("changed")
    assert operation.request_digest == digest
    result = operation.to_mapping()
    result["payload"]["nested"]["a"].append("changed again")
    assert operation.payload == {"nested": {"a": [1, True, None]}, "text": "你好"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(schema_version=2),
        lambda r: r.update(schema_version=True),
        lambda r: r.update(operation_id="not-a-uuid"),
        lambda r: r.update(operation_id=SESSION.upper()),
        lambda r: r.update(operation_id=2),
        lambda r: r.update(kind="pause"),
        lambda r: r.update(idempotency_key=42),
        lambda r: r.update(idempotency_key=""),
        lambda r: r.update(generation=1),
        lambda r: r.update(expected_revision=2),
        lambda r: r["scope"].update(extra=1),
        lambda r: r["scope"].update(id=OTHER),
        lambda r: r["target"].update(id="bad"),
        lambda r: r["expected_revisions"].append(r["expected_revisions"][0]),
        lambda r: r["expected_revisions"][0].update(id=OTHER),
        lambda r: r["expected_revisions"][0].update(revision=True),
        lambda r: r["expected_revisions"][0].update(revision=0),
        lambda r: r.update(payload=[]),
        lambda r: r.update(payload={"nested": [{"generation": 1}]}),
        lambda r: r.update(payload={"x": float("nan")}),
        lambda r: r.update(expected_head_commit_id="bad"),
    ],
)
def test_planned_operation_rejects_bad_or_confused_envelopes(mutation):
    raw = _record()
    mutation(raw)
    with pytest.raises(ContractValidationError):
        WorkflowOperation.from_mapping(raw)


def test_planned_operation_rejects_missing_unknown_and_unrelated_head():
    raw = _record("retry_archive")
    with pytest.raises(ContractValidationError):
        WorkflowOperation({**raw, "expected_head_commit_id": COMMIT})
    with pytest.raises(ContractValidationError):
        WorkflowOperation({key: value for key, value in raw.items() if key != "payload"})
    with pytest.raises(ContractValidationError):
        WorkflowOperation({**raw, "surprise": 1})
    with pytest.raises(ContractValidationError):
        WorkflowOperation({**raw, "expected_revisions": [
            {"kind": "workflow_session", "id": OTHER, "revision": 2},
        ]})


def test_head_root_must_be_explicit_and_only_with_ref_revision():
    raw = _record("select_candidate")
    assert "expected_head_commit_id" not in WorkflowOperation(raw).to_mapping()
    raw["expected_head_commit_id"] = None
    assert "expected_head_commit_id" in WorkflowOperation(raw).to_mapping()


def test_budget_control_identity_includes_both_allowances_and_versions():
    request = BudgetControlRequest("extend_budget", SESSION, OTHER, RUN, "once", 7, 3, 2, 8)
    assert request.to_mapping()["payload"] == {
        "chain_run_id": OTHER, "additional_model_requests": 2, "additional_model_attempts": 8,
    }
    assert request.request_digest == BudgetControlRequest(
        "extend_budget", SESSION, OTHER, RUN, "once", 7, 3, 2, 8,
    ).request_digest
    for changes in ({}, {"additional_model_attempts": 7}, {"expected_run_revision": 4}):
        values = {
            "kind": "extend_budget", "workflow_session_id": SESSION, "chain_run_id": OTHER,
            "run_id": RUN, "idempotency_key": "once",
            "expected_session_revision": 7, "expected_run_revision": 3,
            "additional_model_requests": 3, "additional_model_attempts": 8,
        }
        values.update(changes)
        assert request.request_digest != BudgetControlRequest(**values).request_digest


@pytest.mark.parametrize("requests,attempts", [
    (0, 0), (-1, 1), (1, -1), (65, 0), (0, 257), (True, 0), (0, 1.0),
])
def test_budget_control_rejects_empty_invalid_or_unbounded_extension(requests, attempts):
    with pytest.raises(ContractValidationError):
        BudgetControlRequest(
            "extend_budget", SESSION, OTHER, RUN, "once", 7, 3, requests, attempts,
        )
