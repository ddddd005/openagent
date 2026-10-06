"""Prepared Context stays frozen through candidate and recovery controls."""

import asyncio
import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import pytest

from phase1_agent.contract_json import dumps_pretty, loads_strict
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.prepared_context import PreparedPromptContext, validate_frozen_preparation
from phase1_agent.prompt_preparation import make_prompt_context_config
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService

from test_workflow_prepared_context import (
    bundle, collection, components, factory, run_turn, snapshots, uid,
)


@pytest.fixture
def tmp_path():
    with TemporaryDirectory(prefix="prepared-control-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder)


class TrackedContext(PreparedPromptContext):
    def __init__(self):
        self.prepares = []

    def prepare_context(self, node_input, history, **kwargs):
        self.prepares.append(kwargs["node_binding_id"])
        return super().prepare_context(node_input, history, **kwargs)


class VariantModel:
    def __init__(self, stage, calls, a_answers):
        self.stage, self.calls, self.a_answers = stage, calls, a_answers

    def generate(self, messages, tools):
        self.calls.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
        if self.stage == "A":
            index = sum(owner == "A" for owner, *_ in self.calls) - 1
            answer = self.a_answers[index]
        else:
            root = next(message for message in reversed(messages) if message["source"]["kind"] == "upstream_node")
            answer = loads_strict(root["blocks"][0]["text"])["text"] + " / B"
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall(str(uuid4()), "final_answer", dumps_pretty({"answer": {"text": answer}})),
        ))


def variant_factory(calls, *answers):
    return lambda stage: VariantModel(stage, calls, answers)


def reroll(service, sid, key="reroll"):
    view = service.get_session(sid)
    chain = view["messages"][-1]["chain_run_id"]
    receipt = service.reroll(
        sid, chain, idempotency_key=key, expected_session_revision=view["revision"],
        expected_ref_revision=view["ref_revision"], expected_head_commit_id=view["head_commit_id"],
    )
    service.wait_for_idle(sid)
    assert service.get_session(sid)["error"] is None
    return receipt


def lorebook():
    prompts, entries, instances = [], [], []
    for index, keyword in enumerate(("city", "dragon")):
        item = {
            "schema_version": 1, "kind": "item", "item_id": uid(3000 + index),
            "revision": 1, "name": "Book item title", "text": f"knowledge:{keyword}",
            "role": "system", "enabled": True, "placement": "before", "depth": None,
            "order": 0, "interpolation": "literal", "source": {"kind": "configuration"},
        }
        prompts.append(item)
        entry_id = uid(3010 + index)
        entries.append({
            "entry_id": entry_id, "revision": 2, "name": "Entry memo",
            "enabled": True, "keyword": keyword,
            "prompt_ref": {"item_id": item["item_id"], "revision": item["revision"]},
        })
        instances.append({"entry_id": entry_id, "item_instance_id": uid(3020 + index)})
    return {
        "book": {
            "schema_version": 1, "kind": "lorebook", "book_id": uid(3030), "revision": 3,
            "name": "Selected history only", "entries": entries,
        },
        "request": {
            "schema_version": 1, "kind": "lorebook_request", "node_id": uid(3031),
            "book_instance_id": uid(3032), "input_name": "world", "entry_instances": instances,
            "scan": {"text_source": "derived", "sources": ["human", "upstream_node", "model"]},
        },
        "prompts": prompts,
    }


def frozen_for_chain(path, chain_id):
    saved = bundle(path)
    chain = next(row for row in saved["chain_run"] if row["chain_run_id"] == chain_id)
    runs = {row["run_id"]: row for row in saved["run_record"]}
    by_snapshot = {row["snapshot_id"]: row for row in saved["input_snapshot"]}
    return [
        by_snapshot[runs[run_id]["snapshot_id"]]
        for run_id in chain["node_run_ids"] if run_id in runs
    ]


def mutate_live_context(service, stage):
    context, kernel, adapter, current = service._resolved[stage]
    changed = copy.deepcopy(current)
    changed["payload"]["context"]["collection"]["items"][0]["text"] = "current live edit"
    service._resolved[stage] = context, kernel, adapter, changed


def test_lorebook_actual_request_uses_selected_parent_not_hidden_reroll_sibling(tmp_path):
    path, calls = tmp_path / "lorebook-controls.sqlite", []
    config = make_prompt_context_config(collection(), lorebooks=[lorebook()])
    with closing(WorkflowService(
        path, components=components(config),
        model_factory=variant_factory(calls, "selected original", "hidden dragon sibling", "continued selected path"),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "city", "original")
        first_candidate = bundle(path)["workflow_candidate"][0]
        first_snapshot = next(row for row in snapshots(path) if row["node_binding_id"] == A_BINDING)
        assert any(message["blocks"] == [{"kind": "text", "text": "knowledge:city"}] for message in calls[0][1])
        assert validate_frozen_preparation(first_snapshot)["lorebook"][0]["trace"]["decisions"][0]["status"] == "activated"
        reroll(service, sid)
        view = service.get_session(sid)
        service.select_candidate(
            sid, first_candidate["candidate_id"], idempotency_key="restore-original",
            expected_session_revision=view["revision"], expected_ref_revision=view["ref_revision"],
            expected_head_commit_id=view["head_commit_id"],
        )
        run_turn(service, sid, "next", "continue")
        latest = next(
            row for row in snapshots(path)
            if row["node_binding_id"] == A_BINDING and row["parent_turn_id"] is not None
        )
        evidence = validate_frozen_preparation(latest)
        assert [row["status"] for row in evidence["lorebook"][0]["trace"]["decisions"]] == ["activated", "not_matched"]
        canonical_text = dumps_pretty(evidence["canonical_messages"])
        assert "selected original" in canonical_text
        assert "hidden dragon sibling" not in canonical_text
        assert not any(item["text"] == "knowledge:dragon" for item in evidence["collection"]["items"])
        assert first_candidate in bundle(path)["workflow_candidate"]
        assert len(bundle(path)["workflow_candidate"]) == 3


def test_reroll_reuses_frozen_a_and_reprepares_b_from_old_rules_and_new_a(tmp_path):
    path, calls, context = tmp_path / "reroll-prepared.sqlite", [], TrackedContext()
    step = {
        "schema_version": 1, "node_id": uid(3100), "kind": "regex",
        "enabled": True, "select": {"mode": "all"},
        "rule": {"pattern": "a", "replacement": "aa", "flags": "", "mode": "all"},
    }
    config = make_prompt_context_config(collection("a"), steps=[step])
    with closing(WorkflowService(
        path, components=components(config, context=context),
        model_factory=variant_factory(calls, "first A result", "different A result"),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "same user", "first")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        old_a, old_b = frozen_for_chain(path, submitted["chain_run_id"])
        mutate_live_context(service, "A")
        mutate_live_context(service, "B")
        rolled = reroll(service, sid)
        new_a, new_b = frozen_for_chain(path, rolled["chain_run_id"])
        assert new_a["s0"] == old_a["s0"]
        assert validate_frozen_preparation(new_a) == validate_frozen_preparation(old_a)
        b_evidence = validate_frozen_preparation(new_b)
        old_b_evidence = validate_frozen_preparation(old_b)
        assert b_evidence["config"] == old_b_evidence["config"]
        assert b_evidence["collection"]["items"][0]["text"] == "aa"
        assert b_evidence["processing"]["stages"][0]["trace"]["input_digest"] == (
            old_b_evidence["processing"]["stages"][0]["trace"]["input_digest"]
        )
        root = b_evidence["canonical_messages"][-1]
        assert loads_strict(root["blocks"][0]["text"]) == {"text": "different A result"}
        assert root["message_id"] != old_b_evidence["root_locator"]["message_id"]
        assert "current live edit" not in dumps_pretty(new_a["s0"] + new_b["s0"])
        assert context.prepares == [A_BINDING, B_BINDING, B_BINDING]
        assert calls[0][1] == calls[2][1] == old_a["s0"]
        assert calls[3][1] == new_b["s0"]
        assert len(bundle(path)["workflow_candidate"]) == 2
        assert old_a in snapshots(path) and old_b in snapshots(path)


def test_retry_archive_only_closes_existing_result_without_repreparing(tmp_path, monkeypatch):
    path, calls, context = tmp_path / "archive-prepared.sqlite", [], TrackedContext()
    original_archive = SqliteStore.archive_success

    def fail_b_archive(store, records, key):
        run = next(row for kind, row in records if kind == "run_record")
        if run["node_binding_id"] == B_BINDING:
            raise RuntimeError("injected archive failure")
        return original_archive(store, records, key)

    monkeypatch.setattr(SqliteStore, "archive_success", fail_b_archive)
    config = make_prompt_context_config(collection("saved instruction"))
    with closing(WorkflowService(
        path, components=components(config, context=context), model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "city", "first")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        assert failed["available_actions"] == ["retry_archive"]
        run_id = failed["nodes"][1]["run_id"]
        frozen_before = snapshots(path)
        calls_before, prepares_before = copy.deepcopy(calls), list(context.prepares)
        mutate_live_context(service, "B")
        monkeypatch.setattr(SqliteStore, "archive_success", original_archive)
        receipt = service.retry_archive(
            sid, run_id, idempotency_key="close-existing", expected_session_revision=failed["revision"],
        )
        assert receipt["status"] == "succeeded"
        assert service.get_session(sid)["error"] is None
        assert calls == calls_before and context.prepares == prepares_before
        assert snapshots(path) == frozen_before
        assert all(validate_frozen_preparation(row) for row in frozen_before)


def test_exhausted_model_retry_then_same_run_resume_never_reprocesses_a(tmp_path):
    path, calls, context = tmp_path / "resume-prepared.sqlite", [], TrackedContext()
    broken = True

    class RecoveringModel:
        def __init__(self, stage):
            self.stage = stage

        def generate(self, messages, tools):
            if self.stage == "A" and broken:
                calls.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
                raise httpx.ReadTimeout("injected transient fixture")
            return factory(calls)(self.stage).generate(messages, tools)

    config = make_prompt_context_config(collection("once only"))
    with closing(WorkflowService(
        path, components=components(config, context=context), model_factory=RecoveringModel,
    )) as service:
        service._resolved["A"][1].implementation._backoff = lambda retry: None
        sid = service.create_session()["workflow_session_id"]
        submitted = service.submit(sid, "city", "first")
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        assert failed["error"]["code"] == "model_retry_exhausted"
        run_id = failed["nodes"][0]["run_id"]
        original = snapshots(path)[0]
        assert context.prepares == [A_BINDING]
        assert len(calls) == 4
        mutate_live_context(service, "A")
        broken = False
        service.resume(
            sid, run_id, idempotency_key="resume-same-run",
            expected_session_revision=failed["revision"],
            expected_run_revision=failed["nodes"][0]["revision"],
        )
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert completed["nodes"][0]["run_id"] == run_id
        assert completed["chains"] == [{"chain_run_id": submitted["chain_run_id"], "status": "succeeded"}]
        assert snapshots(path)[0] == original
        assert context.prepares == [A_BINDING, B_BINDING]
        a_calls = [messages for stage, messages, _tools in calls if stage == "A"]
        assert len(a_calls) == 5 and all(messages == original["s0"] for messages in a_calls)
        assert validate_frozen_preparation(original)["collection"]["items"][0]["text"] == "once only"


def test_fork_keeps_all_candidates_and_frozen_ancestors_while_child_prepares_new_input(tmp_path):
    path, calls = tmp_path / "fork-prepared.sqlite", []
    config = make_prompt_context_config(collection())
    with closing(WorkflowService(
        path, components=components(config),
        model_factory=variant_factory(calls, "first candidate", "selected second candidate", "child answer"),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "city", "first")
        reroll(service, sid)
        before = bundle(path)
        original_floors = service.list_reply_candidate_floors(sid)
        view = service.get_session(sid)
        branch = service.create_branch(
            sid, view["messages"][-1]["visible_message_id"],
            idempotency_key="fork-all", expected_source_revision=view["revision"],
        )
        child = branch["workflow_session_id"]
        assert service.list_reply_candidate_floors(child) == original_floors
        assert len(original_floors[0]["candidate_ids"]) == 2
        run_turn(service, child, "child work", "child")
        after = bundle(path)
        assert all(row in after["input_snapshot"] for row in before["input_snapshot"])
        assert all(row in after["turn"] for row in before["turn"])
        assert all(row in after["workflow_candidate"] for row in before["workflow_candidate"])
        assert service.list_reply_candidate_floors(sid) == original_floors
        assert service.list_reply_candidate_floors(child)[:1] == original_floors
        a_snapshot = next(
            row for row in after["input_snapshot"]
            if row["workflow_session_id"] == child and row["node_binding_id"] == A_BINDING
        )
        evidence = validate_frozen_preparation(a_snapshot)
        assert evidence["send_view"]["workflow_session_id"] == child
        assert "selected second candidate" in dumps_pretty(evidence["canonical_messages"])
        assert "first candidate" not in dumps_pretty(evidence["canonical_messages"])
        assert len(evidence["logical_floors"]) == 3
        assert len(after["workflow_candidate"]) == 3


@pytest.mark.parametrize("failed_stage", ["A", "B"])
def test_lost_scene_closes_then_manual_continuation_appends_only_saved_observation(tmp_path, failed_stage):
    path, calls, first_context = tmp_path / "lost-scene-prepared.sqlite", [], TrackedContext()
    broken = False

    class InterruptedModel:
        def __init__(self, stage):
            self.stage = stage

        def generate(self, messages, tools):
            if broken and self.stage == failed_stage:
                calls.append((self.stage, copy.deepcopy(messages), copy.deepcopy(tools)))
                raise asyncio.CancelledError("fixture scene interrupted")
            return factory(calls)(self.stage).generate(messages, tools)

    config = make_prompt_context_config(collection("frozen interruption instruction"))
    with closing(WorkflowService(
        path, components=components(config, context=first_context), model_factory=InterruptedModel,
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "stable history", "stable")
        stable = bundle(path)
        broken = True
        interrupted = service.submit(sid, "unfinished city", "interrupted")
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["available_actions"] == ["close_execution"]
        failed_binding = A_BINDING if failed_stage == "A" else B_BINDING
        failed_run_id = next(row["run_id"] for row in view["nodes"] if row["node_binding_id"] == failed_binding)
        before = bundle(path)
        old_run = next(row for row in before["run_record"] if row["run_id"] == failed_run_id)
        original = next(row for row in before["input_snapshot"] if row["snapshot_id"] == old_run["snapshot_id"])
        original_evidence = validate_frozen_preparation(original)
        with closing(SqliteStore(path)) as store:
            original_facts = store.read_execution_facts(failed_run_id)
    # Reopening loses the active workspace and must dispatch nothing by itself.
    broken = False
    reopened_context = TrackedContext()
    calls_before = copy.deepcopy(calls)
    with closing(WorkflowService(
        path, components=components(config, context=reopened_context), model_factory=InterruptedModel,
    )) as service:
        assert calls == calls_before and reopened_context.prepares == []
        assert service.get_session(sid)["available_actions"] == ["close_execution"]
        close_view = service.get_session(sid)
        service.close_execution(
            sid, interrupted["chain_run_id"], idempotency_key="close-lost-scene",
            expected_session_revision=close_view["revision"],
            expected_ref_revision=close_view["ref_revision"],
            expected_head_commit_id=close_view["head_commit_id"],
        )
        closed = service.get_session(sid)
        assert closed["can_continue_workflow"] and closed["can_submit"]
        assert calls == calls_before and reopened_context.prepares == []
        closed_bundle = bundle(path)
        assert closed_bundle["workflow_ref"] == before["workflow_ref"]
        assert all(row in closed_bundle["turn"] for row in before["turn"])
        mutate_live_context(service, failed_stage)
        resumed = service.continue_workflow(
            sid, interrupted["chain_run_id"], idempotency_key="manual-new-execution",
            expected_session_revision=closed["revision"], expected_ref_revision=closed["ref_revision"],
            expected_head_commit_id=closed["head_commit_id"],
        )
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None, repr(service._execution_failures.get(sid))
        after = bundle(path)
        new_run = next(row for row in after["run_record"] if row["run_id"] == resumed["run_id"])
        new_snapshot = next(row for row in after["input_snapshot"] if row["snapshot_id"] == new_run["snapshot_id"])
        assert new_run["run_id"] != failed_run_id
        assert new_run["chain_run_id"] != old_run["chain_run_id"]
        assert new_snapshot["config"] == original["config"]
        assert new_snapshot["s0"][:-1] == original["s0"]
        assert validate_frozen_preparation(new_snapshot) == original_evidence
        observation = new_snapshot["s0"][-1]
        assert observation["schema_version"] == 2
        assert observation["source"]["kind"] == "runtime_execution_observation"
        assert observation["source"]["source_run_id"] == failed_run_id
        assert observation not in original_evidence["canonical_messages"]
        continued_calls = calls[len(calls_before):]
        assert continued_calls[0][0] == failed_stage and continued_calls[0][1] == new_snapshot["s0"]
        assert reopened_context.prepares == ([B_BINDING] if failed_stage == "A" else [])
        assert all(row in after["turn"] for row in before["turn"])
        assert all(row in after["input_snapshot"] for row in before["input_snapshot"])
        with closing(SqliteStore(path)) as store:
            assert store.read_execution_facts(failed_run_id) == original_facts
        # Stable selected history remains usable for the next ordinary input.
        run_turn(service, sid, "next ordinary work", "after-continuation")
        final_bundle = bundle(path)
        assert all(row in final_bundle["turn"] for row in stable["turn"])
        assert len(service.get_session(sid)["messages"]) == 6
        latest_run_id = service.get_session(sid)["nodes"][0]["run_id"]
        latest_run = next(row for row in final_bundle["run_record"] if row["run_id"] == latest_run_id)
        latest_a = next(
            row for row in final_bundle["input_snapshot"]
            if row["snapshot_id"] == latest_run["snapshot_id"]
        )
        latest_evidence = validate_frozen_preparation(latest_a)
        assert len(latest_evidence["logical_floors"]) == 5
        assert not any(
            message["source"]["kind"] == "runtime_execution_observation"
            for message in latest_evidence["canonical_messages"]
        )
