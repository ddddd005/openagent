from __future__ import annotations

import pytest

from phase1_agent.context import PromptContext
from phase1_agent.contracts import RunResult


def make_result(
    *,
    dispatch_started: bool,
    history_delta: tuple[dict, ...] = (),
    status: str = "success",
) -> RunResult:
    return RunResult(
        status=status,  # type: ignore[arg-type]
        history_delta=history_delta,
        dispatch_started=dispatch_started,
    )


def test_build_uses_fixed_system_and_current_user_without_mutating_history() -> None:
    context = PromptContext("fixed instructions")

    first = context.build("run-1", "hello")
    assert first == (
        {
            "kind": "text",
            "role": "system",
            "content": "fixed instructions",
            "id": "system",
            "source": "fixed_prompt",
        },
        {
            "kind": "text",
            "role": "user",
            "content": "hello",
            "id": "run-1:user",
            "source": "real_user",
        },
    )
    assert context.history == ()

    first[0]["content"] = "mutated"
    repeated = context.build("run-1", "hello")
    assert repeated[0]["content"] == "fixed instructions"
    assert repeated[1]["content"] == "hello"


def test_commit_adds_only_real_user_and_complete_history_delta() -> None:
    context = PromptContext("system")
    context.build("run-1", "question")
    delta = (
        {
            "kind": "assistant_calls",
            "id": "assistant-1",
            "role": "assistant",
            "source": "assistant_turn",
            "calls": [{"id": "call-1", "name": "lookup", "raw_arguments": "{}"}],
        },
        {
            "kind": "tool_result",
            "id": "tool-1",
            "role": "tool",
            "source": "tool_execution",
            "tool_call_id": "call-1",
            "name": "lookup",
            "status": "success",
            "content": "answer",
        },
        {
            "kind": "text",
            "id": "assistant-2",
            "role": "assistant",
            "source": "assistant_turn",
            "content": "final",
        },
    )

    context.commit("run-1", make_result(dispatch_started=True, history_delta=delta))

    history = context.history
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "assistant"]
    assert history[0]["content"] == "question"
    assert all(message["role"] != "system" for message in history)
    assert history[0] == {
        "kind": "text",
        "role": "user",
        "content": "question",
        "id": "run-1:user",
        "source": "real_user",
    }
    assert history[1:] == delta


def test_commit_preserves_existing_delta_metadata_and_isolates_result_mutation() -> None:
    context = PromptContext("system")
    context.build("run-1", "question")
    message = {
        "kind": "text",
        "role": "assistant",
        "content": {"text": "final"},
        "id": "model-7",
        "source": "adapter",
    }
    result = make_result(dispatch_started=True, history_delta=(message,))

    context.commit("run-1", result)
    message["content"]["text"] = "changed"
    exposed = context.history
    exposed[-1]["content"]["text"] = "changed again"

    assert context.history[-1] == {
        "kind": "text",
        "role": "assistant",
        "content": {"text": "final"},
        "id": "model-7",
        "source": "adapter",
    }


def test_no_dispatch_discards_user_and_delta_and_releases_pending_run() -> None:
    context = PromptContext("system")
    context.build("run-1", "not dispatched")
    result = make_result(
        dispatch_started=False,
        history_delta=(
            {
                "kind": "text",
                "id": "assistant-1",
                "role": "assistant",
                "source": "assistant_turn",
                "content": "must be discarded",
            },
        ),
    )

    context.commit("run-1", result)
    context.commit("run-1", result)
    assert context.history == ()

    next_prompt = context.build("run-2", "next")
    assert next_prompt[-1]["content"] == "next"


def test_one_pending_run_and_same_run_input_conflicts() -> None:
    context = PromptContext("system")
    context.build("run-1", "hello")

    with pytest.raises(ValueError):
        context.build("run-1", "different")
    with pytest.raises(RuntimeError):
        context.build("run-2", "overlap")

    context.commit("run-1", make_result(dispatch_started=False))
    assert context.build("run-2", "overlap")[-1]["content"] == "overlap"


def test_duplicate_commit_is_idempotent_but_conflicting_commit_is_rejected() -> None:
    context = PromptContext("system")
    context.build("run-1", "hello")
    result = make_result(
        dispatch_started=True,
        history_delta=(
            {
                "kind": "text",
                "id": "assistant-1",
                "role": "assistant",
                "source": "assistant_turn",
                "content": "done",
            },
        ),
    )

    context.commit("run-1", result)
    committed_history = context.history
    context.commit("run-1", result)
    assert context.history == committed_history

    conflicting = make_result(
        dispatch_started=True,
        history_delta=(
            {
                "kind": "text",
                "id": "assistant-1",
                "role": "assistant",
                "source": "assistant_turn",
                "content": "different",
            },
        ),
    )
    with pytest.raises(ValueError):
        context.commit("run-1", conflicting)
    assert context.history == committed_history


def test_instances_have_independent_histories() -> None:
    first = PromptContext("system")
    second = PromptContext("system")
    first.build("run-1", "private")
    first.commit("run-1", make_result(dispatch_started=True))

    assert first.history == (
        {
            "kind": "text",
            "role": "user",
            "content": "private",
            "id": "run-1:user",
            "source": "real_user",
        },
    )
    assert second.history == ()
