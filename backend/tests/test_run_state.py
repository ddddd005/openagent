from dataclasses import FrozenInstanceError, fields, replace

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.run_state import AgentRunStatus as S
from phase1_agent.run_state import RunSafety, available_actions, validate_transition


SAFE = RunSafety(
    execution_stopped=True,
    checkpoint_available=True,
    unsettled_tools=False,
    unknown_tool_outcomes=False,
    budget_available=True,
    final_validated=False,
    archive_committed=False,
    start_persisted=True,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PREPARED, S.RUNNING), (S.PREPARED, S.PAUSED),
        (S.RUNNING, S.PAUSING), (S.RUNNING, S.FAILED),
        (S.PAUSING, S.PAUSED), (S.PAUSING, S.FAILED),
        (S.PAUSED, S.RUNNING), (S.PAUSED, S.SUPERSEDED),
        (S.FAILED, S.RUNNING), (S.FAILED, S.SUPERSEDED),
        (S.RECOVERY_UNAVAILABLE, S.SUPERSEDED),
    ],
)
def test_legal_edges(current, target):
    evidence = replace(SAFE, checkpoint_available=False) if current is S.RECOVERY_UNAVAILABLE else SAFE
    assert validate_transition(current, target, evidence) is None


@pytest.mark.parametrize(
    "current",
    [S.PREPARED, S.RUNNING, S.PAUSING, S.PAUSED, S.FAILED, S.FINAL_READY],
)
def test_unclosed_run_can_be_marked_recovery_unavailable(current):
    validate_transition(
        current, S.RECOVERY_UNAVAILABLE,
        replace(SAFE, checkpoint_available=False, execution_stopped=False),
    )


def test_final_validation_and_archive_are_distinct_edges():
    validated = replace(SAFE, final_validated=True)
    validate_transition(S.RUNNING, S.FINAL_READY, validated)
    with pytest.raises(ContractValidationError):
        validate_transition(S.FINAL_READY, S.SUCCEEDED, validated)
    validate_transition(S.FINAL_READY, S.SUCCEEDED, replace(validated, archive_committed=True))
    assert available_actions(S.FINAL_READY, validated) == {"retry_archive"}
    assert available_actions(S.FINAL_READY, replace(validated, archive_committed=True)) == frozenset()


def test_complete_public_transition_graph():
    edges = {
        S.PREPARED: {S.RUNNING, S.PAUSED, S.RECOVERY_UNAVAILABLE},
        S.RUNNING: {S.PAUSING, S.FAILED, S.FINAL_READY, S.RECOVERY_UNAVAILABLE},
        S.PAUSING: {S.PAUSED, S.FAILED, S.RECOVERY_UNAVAILABLE},
        S.PAUSED: {S.RUNNING, S.SUPERSEDED, S.RECOVERY_UNAVAILABLE},
        S.FAILED: {S.RUNNING, S.SUPERSEDED, S.RECOVERY_UNAVAILABLE},
        S.FINAL_READY: {S.SUCCEEDED, S.RECOVERY_UNAVAILABLE},
        S.RECOVERY_UNAVAILABLE: {S.SUPERSEDED},
        S.SUCCEEDED: set(),
        S.SUPERSEDED: set(),
    }
    for current in S:
        for target in S:
            evidence = replace(
                SAFE,
                checkpoint_available=current is not S.RECOVERY_UNAVAILABLE
                and target is not S.RECOVERY_UNAVAILABLE,
                final_validated=target in (S.FINAL_READY, S.SUCCEEDED),
                archive_committed=target is S.SUCCEEDED,
            )
            if target in edges[current]:
                validate_transition(current, target, evidence)
            else:
                with pytest.raises(ContractValidationError):
                    validate_transition(current, target, evidence)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PREPARED, S.FINAL_READY), (S.PREPARED, S.SUPERSEDED),
        (S.RUNNING, S.PAUSED), (S.RUNNING, S.SUPERSEDED),
        (S.PAUSING, S.RUNNING), (S.PAUSING, S.SUPERSEDED),
        (S.PAUSED, S.FINAL_READY), (S.FAILED, S.FINAL_READY),
        (S.FINAL_READY, S.RUNNING), (S.FINAL_READY, S.SUPERSEDED),
        (S.RECOVERY_UNAVAILABLE, S.RUNNING),
        (S.SUCCEEDED, S.SUPERSEDED), (S.SUCCEEDED, S.RUNNING),
        (S.SUPERSEDED, S.RUNNING),
    ],
)
def test_illegal_edges(current, target):
    with pytest.raises(ContractValidationError):
        validate_transition(current, target, replace(SAFE, final_validated=True, archive_committed=True))


@pytest.mark.parametrize("status", list(S))
def test_no_state_is_its_own_transition(status):
    with pytest.raises(ContractValidationError):
        validate_transition(status, status, SAFE)


@pytest.mark.parametrize(
    ("current", "target", "change"),
    [
        (S.PREPARED, S.RUNNING, {"start_persisted": False}),
        (S.PREPARED, S.RUNNING, {"budget_available": False}),
        (S.PREPARED, S.PAUSED, {"execution_stopped": False}),
        (S.RUNNING, S.FAILED, {"execution_stopped": False}),
        (S.PAUSING, S.PAUSED, {"execution_stopped": False}),
        (S.PAUSING, S.PAUSED, {"unsettled_tools": True}),
        (S.PAUSING, S.PAUSED, {"unknown_tool_outcomes": True}),
        (S.RUNNING, S.PAUSING, {"final_validated": True}),
        (S.RUNNING, S.FAILED, {"final_validated": True}),
        (S.PAUSED, S.RUNNING, {"checkpoint_available": False}),
        (S.PAUSED, S.RUNNING, {"start_persisted": False}),
        (S.FAILED, S.RUNNING, {"checkpoint_available": False}),
        (S.FAILED, S.RUNNING, {"budget_available": False}),
        (S.PAUSED, S.RUNNING, {"unknown_tool_outcomes": True}),
        (S.PAUSED, S.RUNNING, {"unsettled_tools": True}),
        (S.PAUSED, S.SUPERSEDED, {"execution_stopped": False}),
        (S.FAILED, S.SUPERSEDED, {"unknown_tool_outcomes": True}),
        (S.RECOVERY_UNAVAILABLE, S.SUPERSEDED, {"unsettled_tools": True}),
        (S.RECOVERY_UNAVAILABLE, S.SUPERSEDED, {"execution_stopped": False}),
        (S.RUNNING, S.RECOVERY_UNAVAILABLE, {"start_persisted": False}),
    ],
)
def test_safety_facts_block_transitions(current, target, change):
    evidence = replace(SAFE, checkpoint_available=False) if (
        current is S.RECOVERY_UNAVAILABLE or target is S.RECOVERY_UNAVAILABLE
    ) else SAFE
    with pytest.raises(ContractValidationError):
        validate_transition(current, target, replace(evidence, **change))


@pytest.mark.parametrize("current", [S.PREPARED, S.PAUSING])
def test_unknown_outcomes_keep_run_out_of_paused_pending_reconciliation(current):
    unknown = replace(SAFE, unknown_tool_outcomes=True)
    with pytest.raises(ContractValidationError, match="reconciliation"):
        validate_transition(current, S.PAUSED, unknown)
    if current is S.PAUSING:
        assert available_actions(S.PAUSING, unknown) == frozenset()
    assert available_actions(S.PAUSED, unknown) == frozenset()


@pytest.mark.parametrize("current", [S.PREPARED, S.RUNNING, S.PAUSING, S.PAUSED, S.FAILED, S.FINAL_READY])
def test_recovery_unavailable_requires_lost_checkpoint(current):
    with pytest.raises(ContractValidationError, match="checkpoint"):
        validate_transition(current, S.RECOVERY_UNAVAILABLE, SAFE)
    assert available_actions(S.RECOVERY_UNAVAILABLE, SAFE) == frozenset()
    with pytest.raises(ContractValidationError, match="checkpoint"):
        validate_transition(S.RECOVERY_UNAVAILABLE, S.SUPERSEDED, SAFE)


def test_recovery_reroll_waits_for_old_execution_and_outcome_reconciliation():
    lost = replace(SAFE, checkpoint_available=False, execution_stopped=False,
                   unknown_tool_outcomes=True)
    validate_transition(S.RUNNING, S.RECOVERY_UNAVAILABLE, lost)
    assert available_actions(S.RECOVERY_UNAVAILABLE, lost) == frozenset()
    stopped = replace(lost, execution_stopped=True)
    assert available_actions(S.RECOVERY_UNAVAILABLE, stopped) == frozenset()
    reconciled = replace(stopped, unknown_tool_outcomes=False)
    assert available_actions(S.RECOVERY_UNAVAILABLE, reconciled) == {"reroll"}
    validate_transition(S.RECOVERY_UNAVAILABLE, S.SUPERSEDED, reconciled)


def test_pending_never_dispatched_tools_do_not_block_safe_resume():
    validate_transition(S.PAUSED, S.RUNNING, SAFE)


def test_default_evidence_cannot_dispatch_resume_reroll_or_commit():
    default = RunSafety()
    for current, target in (
        (S.PREPARED, S.RUNNING), (S.PAUSED, S.RUNNING),
        (S.PAUSED, S.SUPERSEDED), (S.FINAL_READY, S.SUCCEEDED),
    ):
        with pytest.raises(ContractValidationError):
            validate_transition(current, target, default)
    assert all(not available_actions(status, default) for status in S)


def test_actions_match_guarded_edges_and_budget_extension():
    assert available_actions(S.PREPARED, SAFE) == {"start", "pause"}
    assert available_actions(S.RUNNING, SAFE) == {"pause"}
    assert available_actions(S.PAUSING, SAFE) == frozenset()
    assert available_actions(S.PAUSED, SAFE) == {"resume", "reroll"}
    assert available_actions(S.FAILED, SAFE) == {"resume", "reroll"}
    assert available_actions(S.RECOVERY_UNAVAILABLE, replace(SAFE, checkpoint_available=False)) == {"reroll"}
    assert available_actions(S.SUPERSEDED, SAFE) == frozenset()
    blocked = replace(SAFE, budget_available=False)
    assert available_actions(S.PAUSED, blocked) == {"reroll", "extend_budget"}
    assert available_actions(S.FAILED, blocked) == {"reroll", "extend_budget"}


def test_succeeded_reroll_preserves_success_and_needs_settled_committed_evidence():
    committed = replace(SAFE, final_validated=True, archive_committed=True)
    assert available_actions(S.SUCCEEDED, committed) == {"reroll"}
    for change in ({"execution_stopped": False}, {"unknown_tool_outcomes": True},
                   {"unsettled_tools": True}, {"archive_committed": False}):
        assert available_actions(S.SUCCEEDED, replace(committed, **change)) == frozenset()
    with pytest.raises(ContractValidationError):
        validate_transition(S.SUCCEEDED, S.SUPERSEDED, committed)


def test_final_ready_never_offers_model_resume_or_reroll():
    validated = replace(SAFE, final_validated=True)
    for change in ({"unsettled_tools": True}, {"unknown_tool_outcomes": True},
                   {"execution_stopped": False}):
        unsafe = replace(validated, **change)
        assert available_actions(S.FINAL_READY, unsafe) == frozenset()
        with pytest.raises(ContractValidationError):
            validate_transition(S.FINAL_READY, S.SUCCEEDED, unsafe)


def test_incorrect_public_values_and_safety_types_are_rejected():
    for current, target, safety in (
        ("requesting", S.RUNNING, SAFE), ("prepared", S.RUNNING, SAFE),
        (S.PREPARED, "tool_settling", SAFE), (S.PREPARED, S.RUNNING, None),
    ):
        with pytest.raises(ContractValidationError):
            validate_transition(current, target, safety)
    with pytest.raises(ContractValidationError):
        available_actions("requesting", SAFE)


@pytest.mark.parametrize("field", [field.name for field in fields(RunSafety)])
@pytest.mark.parametrize("invalid", [0, 1, None, "true"])
def test_all_safety_fields_require_exact_bool(field, invalid):
    with pytest.raises(ContractValidationError):
        replace(SAFE, **{field: invalid})


def test_safety_is_frozen():
    with pytest.raises(FrozenInstanceError):
        SAFE.execution_stopped = False
