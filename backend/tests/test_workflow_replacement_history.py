"""Replacement of a later unfinished floor preserves prior stable history."""

from __future__ import annotations

import asyncio
import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(
        prefix="replacement-history-", dir=Path(__file__).resolve().parent,
    ) as folder:
        yield Path(folder) / "workflow.sqlite"


def saved_bundle(path):
    with closing(SqliteStore(path)) as store:
        return validate_bundle(store.read_bundle())


def cas(service, sid, key):
    view = service.get_session(sid)
    return {
        "idempotency_key": key,
        "expected_session_revision": view["revision"],
        "expected_ref_revision": view["ref_revision"],
        "expected_head_commit_id": view["head_commit_id"],
    }


def indexed(rows, identity):
    return {row[identity]: row for row in rows}


class FinalAdapter:
    def __init__(self, stage, calls, *, interrupted=False):
        self.stage, self.calls, self.interrupted = stage, calls, interrupted

    def generate(self, messages, tools):
        self.calls.append((self.stage, copy.deepcopy(messages)))
        if self.interrupted:
            raise asyncio.CancelledError("controlled adapter interruption")
        root = next(
            message for message in reversed(messages)
            if message["source"]["kind"] in ("human", "upstream_node")
        )
        text = loads_strict(root["blocks"][0]["text"])["text"]
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall(
                str(uuid4()), "final_answer",
                dumps_pretty({"answer": {"text": text + " / " + self.stage}}),
            ),
        ))


@pytest.mark.parametrize("interrupted_stage", ["A", "B"])
@pytest.mark.parametrize("replacement_action", ["continue_workflow", "reroll"])
def test_later_closed_floor_replacement_preserves_stable_history(
    database, interrupted_stage, replacement_action,
):
    interrupted, calls, created = False, [], []

    def factory(stage):
        created.append(stage)
        return FinalAdapter(
            stage, calls, interrupted=interrupted and stage == interrupted_stage,
        )

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        stable_input = service.submit(sid, "Stable first floor", "stable")
        service.wait_for_idle(sid)
        stable_view = service.get_session(sid)
        assert stable_view["error"] is None
        stable = saved_bundle(database)
        stable_refs = [
            row["visible_message_id"] for row in stable["visible_message_ref"]
            if row["workflow_session_id"] == sid
        ]
        assert len(stable_refs) == 2
        assert created == ["A", "B"]

        interrupted = True
        unfinished = service.submit(sid, "Interrupted second floor", "interrupted")
        service.wait_for_idle(sid)
        failed_view = service.get_session(sid)
        assert failed_view["error"] is not None
        assert failed_view["can_close_execution"]
        assert [row["role"] for row in failed_view["messages"]] == ["user", "assistant", "user"]
        failed = saved_bundle(database)
        old_chain = next(
            row for row in failed["chain_run"] if row["chain_run_id"] == unfinished["chain_run_id"]
        )
        old_runs = indexed(failed["run_record"], "run_id")
        interrupted_run = old_runs[old_chain["node_run_ids"][-1]]
        expected_binding = A_BINDING if interrupted_stage == "A" else B_BINDING
        assert interrupted_run["node_binding_id"] == expected_binding
        with closing(SqliteStore(database)) as store:
            interrupted_facts = store.read_execution_facts(interrupted_run["run_id"])
        calls_before_close = len(calls)
        close_args = cas(service, sid, "close")
        closed_receipt = service.close_execution(sid, unfinished["chain_run_id"], **close_args)
        assert service.close_execution(sid, unfinished["chain_run_id"], **close_args) == closed_receipt
        closed_view = service.get_session(sid)
        assert closed_view["can_submit"] and closed_view["can_reroll"]
        assert closed_view["can_continue_workflow"]
        assert closed_view["head_commit_id"] == stable_view["head_commit_id"]
        assert len(calls) == calls_before_close
        closed = saved_bundle(database)
        assert closed["workflow_candidate"] == stable["workflow_candidate"]
        assert closed["turn"] == failed["turn"]
        assert closed["input_snapshot"] == failed["input_snapshot"]
        assert all(
            indexed(closed["visible_message"], "visible_message_id")[row["visible_message_id"]] == row
            for row in stable["visible_message"]
        )

        interrupted = False
        created_before_replace = len(created)
        replace_args = cas(service, sid, "replacement")
        replace = getattr(service, replacement_action)
        receipt = replace(sid, unfinished["chain_run_id"], **replace_args)
        service.wait_for_idle(sid)
        replaced_view = service.get_session(sid)
        assert replaced_view["error"] is None, replaced_view
        assert replaced_view["can_submit"]
        assert [row["role"] for row in replaced_view["messages"]] == [
            "user", "assistant", "user", "assistant",
        ]
        assert [row["visible_message_id"] for row in replaced_view["messages"][:2]] == stable_refs
        assert replaced_view["messages"][0]["visible_message_id"] == stable_input["visible_message_id"]
        assert replaced_view["messages"][2]["visible_message_id"] == unfinished["visible_message_id"]
        assert sum(
            row["visible_message_id"] == unfinished["visible_message_id"]
            for row in replaced_view["messages"]
        ) == 1
        assert replaced_view["messages"][-1]["chain_run_id"] == receipt["chain_run_id"]
        assert receipt["chain_run_id"] != unfinished["chain_run_id"]
        expected_created = (
            ["B"] if replacement_action == "continue_workflow" and interrupted_stage == "B"
            else ["A", "B"]
        )
        assert created[created_before_replace:] == expected_created

        replaced = saved_bundle(database)
        assert all(
            indexed(replaced["turn"], "turn_id")[row["turn_id"]] == row for row in failed["turn"]
        )
        assert all(
            indexed(replaced["workflow_candidate"], "candidate_id")[row["candidate_id"]] == row
            for row in stable["workflow_candidate"]
        )
        assert all(
            indexed(replaced["input_snapshot"], "snapshot_id")[row["snapshot_id"]] == row
            for row in failed["input_snapshot"]
        )
        assert len(replaced["workflow_candidate"]) == 2
        old_run_after = indexed(replaced["run_record"], "run_id")[interrupted_run["run_id"]]
        assert old_run_after["status"] == "closed"
        assert old_run_after["superseded_by_run_id"] is None
        assert sum(
            row["role"] == "user" and row["visible_message_id"] == unfinished["visible_message_id"]
            for row in replaced["visible_message_ref"]
        ) == 1
        with closing(SqliteStore(database)) as store:
            assert store.read_execution_facts(interrupted_run["run_id"]) == interrupted_facts
        if replacement_action == "continue_workflow":
            continuation = next(
                row for row in replaced["execution_continuation"]
                if row["chain_run_id"] == receipt["chain_run_id"]
            )
            expected_reused = (
                [old_chain["node_run_ids"][0]] if interrupted_stage == "B" else []
            )
            assert continuation["reused_run_ids"] == expected_reused
            replacement_messages = calls[calls_before_close][1]
            assert any(
                message["source"]["kind"] == "runtime_execution_observation"
                for message in replacement_messages
            )

        next_input = service.submit(sid, "Normal third floor", "next-normal")
        service.wait_for_idle(sid)
        next_view = service.get_session(sid)
        assert next_view["error"] is None, next_view
        assert next_view["can_submit"]
        assert [row["role"] for row in next_view["messages"]] == [
            "user", "assistant", "user", "assistant", "user", "assistant",
        ]
        assert [row["visible_message_id"] for row in next_view["messages"][:4]] == [
            row["visible_message_id"] for row in replaced_view["messages"]
        ]
        assert next_view["messages"][-2]["visible_message_id"] == next_input["visible_message_id"]
        final = saved_bundle(database)
        assert len(final["workflow_candidate"]) == 3
        assert all(
            indexed(final["turn"], "turn_id")[row["turn_id"]] == row for row in replaced["turn"]
        )
        assert all(
            indexed(final["workflow_candidate"], "candidate_id")[row["candidate_id"]] == row
            for row in replaced["workflow_candidate"]
        )
        assert len([row for row in final["visible_message"] if row["role"] == "user"]) == 3
