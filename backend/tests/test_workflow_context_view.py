"""Workbench reads selected canonical archives and previews without side effects."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import dumps_pretty
from phase1_agent.prompt_preparation import make_prompt_context_config, validate_context_preparation
from phase1_agent.prompt_assembly import PromptAssemblyLimits
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, OUTPUT_BINDING, WorkflowService
import phase1_agent.workflow as workflow_module
from phase1_agent.workflow_context_view import SelectedArchiveReader
from test_workflow_prepared_context import bundle, collection, components, factory, run_turn, uid
from test_workflow_prepared_history import history_factory
from test_workflow_prompt_configs import config, group, item


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="context-view-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def fence(service, sid):
    view = service.get_session(sid)
    return {
        "expected_session_revision": view["revision"],
        "expected_ref_revision": view["ref_revision"],
        "expected_head_commit_id": view["head_commit_id"],
    }


def draft():
    return {"items": [item()], "groups": [group()], "config": config()}


def persistent_data(path):
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


def read(service, sid, binding=A_BINDING):
    return service.read_node_context(sid, binding, **fence(service, sid))


def preview(service, sid, binding=A_BINDING, *, text="next request", prompt_config=None):
    return service.preview_node_context(
        sid, binding, **fence(service, sid),
        prompt_config=draft() if prompt_config is None else prompt_config, text=text,
    )


def test_empty_selected_head_preview_is_ephemeral_and_read_only(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        before = persistent_data(database)
        archived = read(service, sid)
        assert archived["schema_version"] == 1 and archived["kind"] == "workflow_context_read"
        assert archived["messages"] == archived["logical_floors"] == archived["turns"] == []
        assert archived["scope"]["parent_turn_id"] is None
        assert archived["scope"]["selected_chain_run_id"] is None
        prepared = preview(service, sid)
        assert prepared["status"] == "ready" and prepared["reason_code"] is None
        assert prepared["input"]["kind"] == "ephemeral"
        evidence = validate_context_preparation(prepared["preparation"])
        assert evidence["canonical_messages"][0]["blocks"] == [{
            "kind": "text", "text": dumps_pretty({"text": "next request"}),
        }]
        assert len(evidence["collection"]["items"]) == 2
        assert evidence["assembly"]["manifest"]["usage"]["messages"] == 3
        assert persistent_data(database) == before and calls == []


def test_selected_roots_and_deltas_do_not_duplicate_cumulative_s0(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "first", "first")
        run_turn(service, sid, "second", "second")
        saved, before, call_count = bundle(database), persistent_data(database), len(calls)
        for binding in (A_BINDING, B_BINDING):
            archived = read(service, sid, binding)
            assert len(archived["turns"]) == 2 and len(archived["logical_floors"]) == 4
            assert [identity for floor in archived["logical_floors"] for identity in floor] == [
                message["message_id"] for message in archived["messages"]
            ]
            expected = []
            for summary in archived["turns"]:
                turn = next(row for row in saved["turn"] if row["turn_id"] == summary["turn_id"])
                assert summary["workflow_session_id"] == sid and summary["node_binding_id"] == binding
                expected.extend([summary["root_message_id"], *[message["message_id"] for message in turn["messages"]]])
                assert summary["message_ids"] == [message["message_id"] for message in turn["messages"]]
                assert summary["result_ports"]["context_delta"]["delivery_id"] == "result-context:" + summary["run_id"]
            assert expected == [message["message_id"] for message in archived["messages"]]
            assert all(message["source"]["kind"] != "prompt" for message in archived["messages"])
            assert archived["scope"]["parent_turn_id"] == archived["turns"][-1]["turn_id"]
            assert archived["scope"]["selected_chain_run_id"] == archived["turns"][-1]["chain_run_id"]
        prepared = preview(service, sid)
        assert len(prepared["preparation"]["logical_floors"]) == 5
        assert prepared["preparation"]["canonical_messages"][:-1] == read(service, sid)["messages"]
        assert persistent_data(database) == before and len(calls) == call_count


def test_protocol_mixed_and_formal_final_text_are_protected(database):
    calls = []
    with closing(WorkflowService(database, model_factory=history_factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "city input", "first")
        archived = read(service, sid)
        protected = {(row["message_id"], row["block_index"]) for row in archived["protected_blocks"]}
        final_ids = {row["final_message_id"] for row in archived["turns"]}
        for message in archived["messages"]:
            closed = any(block["kind"] != "text" for block in message["blocks"])
            for index, block in enumerate(message["blocks"]):
                if block["kind"] == "text" and (
                    closed or message["source"]["kind"] == "protocol_feedback"
                    or message["message_id"] in final_ids
                ):
                    assert (message["message_id"], index) in protected
        ordinary = next(message for message in archived["messages"]
                        if message["blocks"] == [{"kind": "text", "text": "city ordinary assistant text"}])
        assert (ordinary["message_id"], 0) not in protected
        assert preview(service, sid)["preparation"]["send_view"]["messages"][:-1] == archived["messages"]


def test_reroll_reads_only_selected_candidate_not_all_siblings(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        original = service.submit(sid, "candidate", "first")
        service.wait_for_idle(sid)
        prior = read(service, sid)
        service.reroll(sid, original["chain_run_id"], idempotency_key="reroll", **fence(service, sid))
        service.wait_for_idle(sid)
        selected = read(service, sid)
        assert len(selected["turns"]) == 1
        assert selected["scope"]["parent_turn_id"] != prior["scope"]["parent_turn_id"]
        assert not set(prior["turns"][0]["message_ids"]).intersection(
            message["message_id"] for message in selected["messages"]
        )


def test_fork_context_keeps_actual_ancestor_owner_and_excludes_future_parent_turns(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "first", "first")
        parent_view = service.get_session(sid)
        child = service.create_branch(
            sid, parent_view["messages"][-1]["visible_message_id"],
            expected_source_revision=parent_view["revision"], idempotency_key="fork",
        )
        child_id = child["workflow_session_id"]
        run_turn(service, sid, "parent future", "second")
        inherited = read(service, child_id)
        assert len(inherited["turns"]) == 1
        assert inherited["scope"]["workflow_session_id"] == child_id
        assert inherited["turns"][0]["workflow_session_id"] == sid
        run_turn(service, child_id, "child next", "child")
        selected = read(service, child_id)
        assert [turn["workflow_session_id"] for turn in selected["turns"]] == [sid, child_id]
        assert all("parent future" not in dumps_pretty(message) for message in selected["messages"])


@pytest.mark.parametrize("binding", [A_BINDING, B_BINDING])
def test_b_preview_is_pending_even_if_old_a_output_exists(database, binding):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        if binding == B_BINDING:
            run_turn(service, sid, "old request", "first")
        before, call_count = persistent_data(database), len(calls)
        pending = preview(service, sid, B_BINDING, text=None)
        assert pending["status"] == "pending" and pending["reason_code"] == "upstream_input_pending"
        assert pending["input"] is pending["preparation"] is None
        with pytest.raises(ContractValidationError) as caught:
            preview(service, sid, B_BINDING, text="fabricated upstream")
        assert caught.value.status_code == 400 and caught.value.reason_code == "invalid_request"
        assert persistent_data(database) == before and len(calls) == call_count


def test_stale_missing_unknown_binding_and_invalid_inline_have_typed_failures(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        before = persistent_data(database)
        cases = [
            (lambda: service.read_node_context(sid, A_BINDING, **{**fence(service, sid), "expected_session_revision": 999}),
             409, "stale_revision"),
            (lambda: service.read_node_context(uid(9000), A_BINDING, **fence(service, sid)), 404, "not_found"),
            (lambda: service.read_node_context(sid, OUTPUT_BINDING, **fence(service, sid)), 404, "not_found"),
            (lambda: service.read_node_context(sid, A_BINDING, **{**fence(service, sid), "parent_turn_id": None}),
             400, "invalid_request"),
            (lambda: preview(service, sid, prompt_config={**draft(), "PRIVATE": True}), 400, "invalid_request"),
        ]
        for call, status, reason in cases:
            with pytest.raises(ContractValidationError) as caught:
                call()
            assert caught.value.status_code == status and caught.value.reason_code == reason
        assert persistent_data(database) == before


def test_unrelated_archived_owner_and_broken_projection_are_storage_failures(database, monkeypatch):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        other = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "first", "first")
        saved = bundle(database)
        other_session = next(row for row in saved["workflow_session"] if row["workflow_session_id"] == other)
        original = SelectedArchiveReader._record

        def corrupted(reader, kind, **identity):
            value = original(reader, kind, **identity)
            if kind == "run_record":
                value["workflow_session_id"] = other_session["workflow_session_id"]
            return value

        monkeypatch.setattr(SelectedArchiveReader, "_record", corrupted)
        before = persistent_data(database)
        with pytest.raises(ContractValidationError) as caught:
            read(service, sid)
        assert caught.value.status_code == 500 and caught.value.reason_code == "storage_contract_violation"
        assert persistent_data(database) == before


def test_configured_macro_preview_preserves_inline_literal_material_read_only(database):
    configuration = make_prompt_context_config(
        collection("{{input}}"),
        steps=[{"schema_version": 1, "node_id": uid(9010), "kind": "macro",
                "enabled": True, "select": {"mode": "all"}}],
    )
    with closing(WorkflowService(database, components=components(configuration))) as service:
        sid = service.create_session()["workflow_session_id"]
        selected = draft()
        selected["items"][0].update(text="{{input}}", interpolation="literal")
        before = persistent_data(database)
        result = preview(service, sid, prompt_config=selected)
        assert result["status"] == "ready"
        evidence = validate_context_preparation(result["preparation"])
        assert any(entry["text"] == "{{input}}" for entry in evidence["collection"]["items"])
        assert evidence["processing"]["stages"][0]["trace"]["processed_instances"] == []
        assert persistent_data(database) == before


def test_configured_regex_preview_executes_inline_material_without_dispatch_or_write(database):
    configuration = make_prompt_context_config(
        collection("default"),
        steps=[{
            "schema_version": 1, "node_id": uid(9011), "kind": "regex", "enabled": True,
            "select": {"mode": "all"},
            "rule": {"pattern": "Needle", "replacement": "Changed", "flags": "", "mode": "all"},
        }],
    )
    calls = []
    with closing(WorkflowService(
        database, components=components(configuration), model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        selected = draft()
        selected["items"][0]["text"] = "Needle"
        before = persistent_data(database)
        result = preview(service, sid, prompt_config=selected)
        assert result["status"] == "ready"
        evidence = validate_context_preparation(result["preparation"])
        assert any(entry["text"] == "Changed" for entry in evidence["collection"]["items"])
        assert evidence["processing"]["stages"][0]["trace"]["step"]["kind"] == "regex"
        assert persistent_data(database) == before and calls == []


def test_public_capacity_does_not_add_a_new_execution_limit(database, monkeypatch):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        monkeypatch.setattr(workflow_module, "PromptAssemblyLimits", lambda: PromptAssemblyLimits(max_messages=1))
        run_turn(service, sid, "normal execution", "first")
        before = persistent_data(database)
        with pytest.raises(ContractValidationError) as caught:
            read(service, sid)
        assert caught.value.status_code == 400 and caught.value.reason_code == "context_limit"
        assert persistent_data(database) == before


def test_corrupted_parent_cycle_is_rejected_without_history_writes(database, monkeypatch):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "first", "first")
        original = SelectedArchiveReader._record

        def cycle(reader, kind, **identity):
            value = original(reader, kind, **identity)
            if kind == "turn":
                value["parent_turn_id"] = value["turn_id"]
            return value

        monkeypatch.setattr(SelectedArchiveReader, "_record", cycle)
        before = persistent_data(database)
        with pytest.raises(ContractValidationError) as caught:
            read(service, sid)
        assert caught.value.status_code == 500 and caught.value.reason_code == "storage_contract_violation"
        assert persistent_data(database) == before


def test_corrupted_child_head_cannot_read_a_future_ancestor_turn(database, monkeypatch):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "fork boundary", "first")
        parent_view = service.get_session(sid)
        child_id = service.create_branch(
            sid, parent_view["messages"][-1]["visible_message_id"],
            expected_source_revision=parent_view["revision"], idempotency_key="fork",
        )["workflow_session_id"]
        run_turn(service, sid, "unrelated future", "future")
        saved = bundle(database)
        head = next(row for row in saved["workflow_ref"] if row["workflow_session_id"] == sid)
        commit = next(row for row in saved["workflow_commit"] if row["commit_id"] == head["head_commit_id"])
        future_state = next(row for row in saved["state_snapshot"]
                            if row["state_snapshot_id"] == commit["state_snapshot_id"])
        future_a = next(row["selected_result"] for row in future_state["node_states"]
                        if row["node_binding_id"] == A_BINDING)
        original = SqliteStore.get_record

        def corrupted(store, kind, identity):
            value = original(store, kind, identity)
            if kind == "state_snapshot" and value["workflow_session_id"] == child_id:
                value = deepcopy(value)
                next(row for row in value["node_states"]
                     if row["node_binding_id"] == A_BINDING)["selected_result"] = future_a
                value["selection_refs"] = future_state["selection_refs"]
            return value

        monkeypatch.setattr(SqliteStore, "get_record", corrupted)
        before = persistent_data(database)
        with pytest.raises(ContractValidationError) as caught:
            read(service, child_id)
        assert caught.value.status_code == 500 and caught.value.reason_code == "storage_contract_violation"
        assert persistent_data(database) == before
