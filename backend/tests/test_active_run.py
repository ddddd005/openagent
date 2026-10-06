from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from phase1_agent.active_run import AcceptedProgress, ActiveRun, ToolProgress
from phase1_agent.contract_errors import ContractValidationError


SESSION = "a1111111-1111-4111-8111-111111111111"
CHAIN = "b2222222-2222-4222-8222-222222222222"
RUN = "c3333333-3333-4333-8333-333333333333"
COMMIT = "d4444444-4444-4444-8444-444444444444"
CALL_ONE = "e5555555-5555-4555-8555-555555555555"
CALL_TWO = "f6666666-6666-4666-8666-666666666666"
EXECUTION_ONE = "a7777777-7777-4777-8777-777777777777"
EXECUTION_TWO = "b8888888-8888-4888-8888-888888888888"


def workspace():
    return ActiveRun(RUN, CHAIN, SESSION, COMMIT)


def guard(view):
    return {"expected_generation": view.generation, "expected_revision": view.revision}


def test_workspace_binds_run_chain_session_and_base_with_internal_generation():
    active = workspace()
    view = active.snapshot()
    assert (view.run_id, view.chain_run_id, view.workflow_session_id, view.base_commit_id) == (
        RUN, CHAIN, SESSION, COMMIT,
    )
    assert UUID(view.generation).version == 4
    assert view.revision == 1
    assert view.accepted_progress == AcceptedProgress()
    assert view.tools == view.pending_tools == ()
    with pytest.raises(FrozenInstanceError):
        view.revision = 2
    with pytest.raises(TypeError):
        ActiveRun(RUN, CHAIN, SESSION, COMMIT, generation=CALL_ONE)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "run"),
        ("chain_run_id", 1),
        ("workflow_session_id", SESSION.upper()),
        ("base_commit_id", "d4444444-4444-1444-8444-444444444444"),
        ("base_commit_id", None),
    ],
)
def test_workspace_rejects_malformed_or_unseeded_identity(field, value):
    fields = {
        "run_id": RUN, "chain_run_id": CHAIN,
        "workflow_session_id": SESSION, "base_commit_id": COMMIT,
    }
    fields[field] = value
    with pytest.raises(ContractValidationError):
        ActiveRun(**fields)


@pytest.mark.parametrize(
    "values",
    [
        {"model_requests": True},
        {"model_requests": -1},
        {"attempts": 1.0},
        {"accepted_messages": -1},
        {"model_requests": 2, "attempts": 1},
    ],
)
def test_accepted_progress_is_strict_nonnegative_counter_value(values):
    with pytest.raises(ContractValidationError):
        AcceptedProgress(**values)


def test_accepted_progress_advances_monotonically_without_message_history():
    active = workspace()
    first = active.snapshot()
    second = active.accept_progress(
        AcceptedProgress(model_requests=1, attempts=2, accepted_messages=1), **guard(first),
    )
    assert second.revision == 2
    assert second.accepted_progress == AcceptedProgress(1, 2, 1)
    assert second.run_id == first.run_id
    assert first.accepted_progress == AcceptedProgress()
    assert second.tools == ()
    third = active.accept_progress(AcceptedProgress(1, 2, 2), **guard(second))
    assert third.revision == 3
    assert third.accepted_progress.accepted_messages == 2
    with pytest.raises(ContractValidationError, match="go backwards"):
        active.accept_progress(AcceptedProgress(1, 1, 3), **guard(third))
    with pytest.raises(ContractValidationError, match="did not advance"):
        active.accept_progress(third.accepted_progress, **guard(third))
    with pytest.raises(ContractValidationError):
        active.accept_progress({"model_requests": 2, "attempts": 2, "accepted_messages": 3},
                               **guard(third))
    assert active.snapshot() == third


def test_tool_identity_lifecycle_preserves_order_and_blocks_blind_reexecution():
    active = workspace()
    view = active.snapshot()
    view = active.queue_tool(CALL_ONE, **guard(view))
    view = active.queue_tool(CALL_TWO, **guard(view))
    assert [tool.tool_call_id for tool in view.pending_tools] == [CALL_ONE, CALL_TWO]
    with pytest.raises(ContractValidationError, match="already been accepted"):
        active.queue_tool(CALL_ONE, **guard(view))
    with pytest.raises(ContractValidationError, match="Only a started"):
        active.settle_tool(CALL_ONE, "success", **guard(view))

    view = active.start_tool(CALL_ONE, EXECUTION_ONE, **guard(view))
    assert view.pending_tools[0] == ToolProgress(CALL_ONE, EXECUTION_ONE, "started")
    with pytest.raises(ContractValidationError, match="Only a never-started"):
        active.start_tool(CALL_ONE, EXECUTION_TWO, **guard(view))
    with pytest.raises(ContractValidationError, match="already been used"):
        active.start_tool(CALL_TWO, EXECUTION_ONE, **guard(view))

    view = active.settle_tool(CALL_ONE, "success", **guard(view))
    assert view.tools[0] == ToolProgress(CALL_ONE, EXECUTION_ONE, "success")
    assert view.pending_tools == (ToolProgress(CALL_TWO, None, "pending"),)
    with pytest.raises(ContractValidationError):
        active.start_tool(CALL_ONE, EXECUTION_TWO, **guard(view))
    with pytest.raises(ContractValidationError):
        active.queue_tool(CALL_ONE, **guard(view))
    with pytest.raises(ContractValidationError):
        active.settle_tool(CALL_ONE, "error", **guard(view))
    assert active.snapshot() == view


def test_unknown_started_outcome_stays_pending_until_explicit_reconciliation():
    active = workspace()
    view = active.queue_tool(CALL_ONE, **guard(active.snapshot()))
    view = active.start_tool(CALL_ONE, EXECUTION_ONE, **guard(view))
    view = active.settle_tool(CALL_ONE, "unknown", **guard(view))
    assert view.pending_tools == (ToolProgress(CALL_ONE, EXECUTION_ONE, "unknown"),)
    with pytest.raises(ContractValidationError):
        active.start_tool(CALL_ONE, EXECUTION_TWO, **guard(view))
    with pytest.raises(ContractValidationError):
        active.settle_tool(CALL_ONE, "unknown", **guard(view))
    with pytest.raises(ContractValidationError):
        active.settle_tool(CALL_ONE, "not_executed", **guard(view))
    view = active.settle_tool(CALL_ONE, "error", **guard(view))
    assert view.pending_tools == ()
    assert view.tools[0] == ToolProgress(CALL_ONE, EXECUTION_ONE, "error")


@pytest.mark.parametrize("outcome", ["outcome_unknown", "interrupted"])
def test_explicit_observation_is_terminal_and_remaining_call_never_enters_execution(outcome):
    active = workspace()
    view = active.queue_tool(CALL_ONE, **guard(active.snapshot()))
    view = active.queue_tool(CALL_TWO, **guard(view))
    view = active.start_tool(CALL_ONE, EXECUTION_ONE, **guard(view))
    view = active.settle_tool(CALL_ONE, outcome, **guard(view))
    view = active.skip_tool(CALL_TWO, **guard(view))
    assert view.pending_tools == ()
    assert view.tools == (
        ToolProgress(CALL_ONE, EXECUTION_ONE, outcome),
        ToolProgress(CALL_TWO, None, "never_started"),
    )
    for operation in (
        lambda: active.start_tool(CALL_ONE, EXECUTION_TWO, **guard(view)),
        lambda: active.start_tool(CALL_TWO, EXECUTION_TWO, **guard(view)),
        lambda: active.settle_tool(CALL_ONE, "success", **guard(view)),
        lambda: active.settle_tool(CALL_TWO, "success", **guard(view)),
        lambda: active.skip_tool(CALL_TWO, **guard(view)),
    ):
        with pytest.raises(ContractValidationError):
            operation()
    assert active.snapshot() == view


def test_callbacks_cannot_overtake_unresolved_accepted_calls():
    active = workspace()
    view = active.queue_tool(CALL_ONE, **guard(active.snapshot()))
    view = active.queue_tool(CALL_TWO, **guard(view))
    with pytest.raises(ContractValidationError, match="accepted call order"):
        active.start_tool(CALL_TWO, EXECUTION_TWO, **guard(view))
    with pytest.raises(ContractValidationError, match="accepted call order"):
        active.skip_tool(CALL_TWO, **guard(view))
    view = active.start_tool(CALL_ONE, EXECUTION_ONE, **guard(view))
    with pytest.raises(ContractValidationError):
        active.skip_tool(CALL_ONE, **guard(view))
    with pytest.raises(ContractValidationError, match="unresolved execution"):
        active.queue_tool(COMMIT, **guard(view))
    view = active.settle_tool(CALL_ONE, "unknown", **guard(view))
    with pytest.raises(ContractValidationError):
        active.skip_tool(CALL_ONE, **guard(view))
    with pytest.raises(ContractValidationError, match="accepted call order"):
        active.start_tool(CALL_TWO, EXECUTION_TWO, **guard(view))
    assert active.snapshot() == view
    view = active.settle_tool(CALL_ONE, "outcome_unknown", **guard(view))
    view = active.skip_tool(CALL_TWO, **guard(view))
    assert view.pending_tools == ()


def test_skip_preserves_cas_and_never_assigns_execution_identity():
    active = workspace()
    old = active.queue_tool(CALL_ONE, **guard(active.snapshot()))
    current = active.fence(**guard(old))
    with pytest.raises(ContractValidationError, match="Stale ActiveRun generation"):
        active.skip_tool(CALL_ONE, expected_generation=old.generation,
                         expected_revision=current.revision)
    with pytest.raises(ContractValidationError, match="Stale ActiveRun revision"):
        active.skip_tool(CALL_ONE, expected_generation=current.generation,
                         expected_revision=old.revision)
    current = active.skip_tool(CALL_ONE, **guard(current))
    assert current.tools == (ToolProgress(CALL_ONE, None, "never_started"),)
    with pytest.raises(ContractValidationError):
        active.skip_tool(CALL_TWO, **guard(current))


def test_tool_progress_rejects_type_confusion_and_impossible_status():
    for args in (
        (CALL_ONE, EXECUTION_ONE, "pending"),
        (CALL_ONE, None, "started"),
        (CALL_ONE, None, "unknown"),
        (CALL_ONE, None, "outcome_unknown"),
        (CALL_ONE, None, "interrupted"),
        (CALL_ONE, EXECUTION_ONE, "never_started"),
        (CALL_ONE, EXECUTION_ONE, "not_executed"),
        (CALL_ONE, EXECUTION_ONE, 1),
        ("bad", None, "pending"),
    ):
        with pytest.raises(ContractValidationError):
            ToolProgress(*args)


def test_stale_revision_and_generation_reject_without_state_change():
    active = workspace()
    original = active.snapshot()
    view = active.queue_tool(CALL_ONE, **guard(original))
    with pytest.raises(ContractValidationError, match="Stale ActiveRun revision"):
        active.queue_tool(CALL_TWO, **guard(original))
    with pytest.raises(ContractValidationError):
        active.queue_tool(CALL_TWO, expected_generation="bad", expected_revision=view.revision)
    with pytest.raises(ContractValidationError):
        active.queue_tool(CALL_TWO, expected_generation=view.generation, expected_revision=True)
    fenced = active.fence(**guard(view))
    assert fenced.generation != view.generation
    assert fenced.revision == view.revision + 1
    with pytest.raises(ContractValidationError, match="Stale ActiveRun generation"):
        active.start_tool(CALL_ONE, EXECUTION_ONE, expected_generation=view.generation,
                          expected_revision=fenced.revision)
    with pytest.raises(ContractValidationError, match="Stale ActiveRun revision"):
        active.start_tool(CALL_ONE, EXECUTION_ONE, expected_generation=fenced.generation,
                          expected_revision=view.revision)
    assert active.snapshot() == fenced
    current = active.start_tool(CALL_ONE, EXECUTION_ONE, **guard(fenced))
    assert current.run_id == original.run_id
    assert current.chain_run_id == original.chain_run_id
    assert current.base_commit_id == original.base_commit_id


def test_concurrent_callbacks_with_same_guard_accept_only_one_revision():
    active = workspace()
    prior = active.snapshot()

    def update(tool_call_id):
        return active.queue_tool(tool_call_id, **guard(prior))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() if not future.exception() else future.exception()
                    for future in [pool.submit(update, CALL_ONE), pool.submit(update, CALL_TWO)]]
    accepted = [value for value in outcomes if not isinstance(value, Exception)]
    rejected = [value for value in outcomes if isinstance(value, ContractValidationError)]
    assert len(accepted) == len(rejected) == 1
    assert "Stale ActiveRun revision" in str(rejected[0])
    assert active.snapshot().revision == prior.revision + 1
    assert len(active.snapshot().tools) == 1
