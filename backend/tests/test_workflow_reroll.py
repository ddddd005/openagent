"""The durable, isolated acceptance boundary for a whole-chain reroll."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="workflow-reroll-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def durable(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def owned(bundle, kind, session_id):
    return [row for row in bundle.get(kind, [])
            if row["workflow_session_id"] == session_id]


def source_result(service, database):
    sid = service.create_session()["workflow_session_id"]
    source = service.submit(sid, "Keep every result", "submit")
    service.wait_for_idle(sid)
    bundle = durable(database)
    chain = next(row for row in owned(bundle, "chain_run", sid)
                 if row["chain_run_id"] == source["chain_run_id"])
    head = owned(bundle, "workflow_ref", sid)[0]
    session = owned(bundle, "workflow_session", sid)[0]
    return sid, chain, head, session, bundle


def accept(service, sid, chain, head, session, *, key="reroll"):
    return service._accept_reroll(
        sid, chain["chain_run_id"], idempotency_key=key,
        expected_session_revision=session["revision"],
        expected_ref_revision=head["revision"],
        expected_head_commit_id=head["head_commit_id"],
    )


def test_reroll_acceptance_clones_frozen_a_without_changing_selected_history(database):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid, source, head, session, before = source_result(service, database)
        formal = service.get_session(sid)["messages"]
        receipt = accept(service, sid, source, head, session)
        assert accept(service, sid, source, head, session) == receipt
        assert calls == ["A", "B"]
        assert service.get_session(sid)["messages"] == formal
        after = durable(database)
        assert owned(after, "workflow_ref", sid) == [head]
        assert owned(after, "workflow_candidate", sid) == owned(
            before, "workflow_candidate", sid
        )
        assert owned(after, "visible_message_ref", sid) == owned(
            before, "visible_message_ref", sid
        )
        assert owned(after, "chain_run", sid)[0] == source

        chain = next(row for row in owned(after, "chain_run", sid)
                     if row["chain_run_id"] == receipt["chain_run_id"])
        origin = next(row for row in owned(after, "chain_input_origin", sid)
                      if row["chain_run_id"] == chain["chain_run_id"])
        run = next(row for row in owned(after, "run_record", sid)
                   if row["run_id"] == receipt["run_id"])
        original_run = next(row for row in owned(before, "run_record", sid)
                            if row["run_id"] == source["node_run_ids"][0])
        old_input = next(row for row in before["node_input"]
                         if row["input_id"] == source["input_id"])
        new_input = next(row for row in after["node_input"]
                         if row["input_id"] == receipt["input_id"])
        old_snapshot = next(row for row in before["input_snapshot"]
                            if row["snapshot_id"] == original_run["snapshot_id"])
        new_snapshot = next(row for row in after["input_snapshot"]
                            if row["snapshot_id"] == run["snapshot_id"])
        assert source["chain_run_id"] != chain["chain_run_id"]
        assert chain["status"] == run["status"] == "prepared"
        assert chain["node_run_ids"] == [run["run_id"]]
        assert chain["base_commit_id"] == source["base_commit_id"]
        assert chain["base_ref_revision"] == source["base_ref_revision"]
        assert run["initial_budget"] == original_run["initial_budget"]
        assert origin == {
            "schema_version": 1,
            "created_at": origin["created_at"],
            "chain_run_id": chain["chain_run_id"],
            "workflow_session_id": sid,
            "visible_message_id": old_input["source"]["visible_message_id"],
            "input_id": new_input["input_id"],
            "source_chain_run_id": source["chain_run_id"],
        }
        assert {key: value for key, value in new_input.items()
                if key not in ("input_id", "created_at")} == {
                    key: value for key, value in old_input.items()
                    if key not in ("input_id", "created_at")
                }
        assert {key: value for key, value in new_snapshot.items()
                if key not in ("snapshot_id", "input_id", "created_at")} == {
                    key: value for key, value in old_snapshot.items()
                    if key not in ("snapshot_id", "input_id", "created_at")
                }
        operation = next(row for row in after["workflow_operation"]
                         if row["operation_id"] == chain["operation_id"])
        assert operation["kind"] == "reroll"
        assert operation["target"] == {
            "kind": "chain_run", "id": source["chain_run_id"],
        }
        assert operation["expected_head_commit_id"] == head["head_commit_id"]

        with pytest.raises(ContractValidationError, match="different workflow operation request"):
            service._accept_reroll(
                sid, str(uuid4()), idempotency_key="reroll",
                expected_session_revision=session["revision"],
                expected_ref_revision=head["revision"],
                expected_head_commit_id=head["head_commit_id"],
            )
        assert durable(database) == after


def test_reroll_acceptance_rejects_stale_or_unselected_source_without_writes(database):
    with closing(WorkflowService(database)) as service:
        sid, chain, head, session, before = source_result(service, database)
        with pytest.raises(ContractValidationError, match="revision conflict"):
            service._accept_reroll(
                sid, chain["chain_run_id"], idempotency_key="stale-session",
                expected_session_revision=session["revision"] - 1,
                expected_ref_revision=head["revision"],
                expected_head_commit_id=head["head_commit_id"],
            )
        with pytest.raises(ContractValidationError, match="head conflict"):
            service._accept_reroll(
                sid, chain["chain_run_id"], idempotency_key="stale-head",
                expected_session_revision=session["revision"],
                expected_ref_revision=head["revision"] + 1,
                expected_head_commit_id=head["head_commit_id"],
            )
        with pytest.raises(ContractValidationError, match="completed, settled session"):
            accept(service, sid, {**chain, "chain_run_id": str(uuid4())},
                   head, session, key="wrong-source")
        assert durable(database) == before


def reroll(service, sid, chain, head, session, *, key="reroll"):
    return service._reroll(
        sid, chain["chain_run_id"], idempotency_key=key,
        expected_session_revision=session["revision"],
        expected_ref_revision=head["revision"],
        expected_head_commit_id=head["head_commit_id"],
    )


def test_successful_reroll_archives_both_candidates_and_auto_selects(database):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid, source, original_head, session, before = source_result(service, database)
        original_candidate = owned(before, "workflow_candidate", sid)[0]
        original_message = next(row for row in before["visible_message"]
                                if row["source"] == {
                                    "kind": "workflow_output",
                                    "output_id": original_candidate["output_id"],
                                })
        original_nodes = owned(before, "node_session", sid)
        receipt = reroll(service, sid, source, original_head, session)
        service.wait_for_idle(sid)
        assert reroll(service, sid, source, original_head, session) == receipt
        assert calls == ["A", "B", "A", "B"]
        bundle = durable(database)
        candidates = owned(bundle, "workflow_candidate", sid)
        assert len(candidates) == 2
        assert original_candidate in candidates
        selected = next(row for row in candidates
                        if row["chain_run_id"] == receipt["chain_run_id"])
        assert selected["base_commit_id"] == original_candidate["base_commit_id"]
        assert owned(bundle, "workflow_ref", sid)[0]["head_commit_id"] == selected["result_commit_id"]
        formal = service.get_session(sid)["messages"]
        assert [row["role"] for row in formal] == ["user", "assistant"]
        assert formal[-1]["chain_run_id"] == selected["chain_run_id"]
        assert original_message in bundle["visible_message"]
        assert all(row in bundle["visible_message_ref"]
                   for row in owned(before, "visible_message_ref", sid))
        assert len(owned(bundle, "visible_message_ref", sid)) == 3
        assert len(owned(bundle, "node_session", sid)) == len(original_nodes) == 3

        chain = next(row for row in owned(bundle, "chain_run", sid)
                     if row["chain_run_id"] == receipt["chain_run_id"])
        new_a, new_b = [
            next(row for row in owned(bundle, "run_record", sid)
                 if row["run_id"] == run_id)
            for run_id in chain["node_run_ids"][:2]
        ]
        source_b = next(row for row in owned(before, "run_record", sid)
                        if row["run_id"] == source["node_run_ids"][1])
        source_b_snapshot = next(row for row in before["input_snapshot"]
                                 if row["snapshot_id"] == source_b["snapshot_id"])
        new_b_snapshot = next(row for row in bundle["input_snapshot"]
                              if row["snapshot_id"] == new_b["snapshot_id"])
        assert new_b_snapshot["parent_turn_id"] == source_b_snapshot["parent_turn_id"]
        new_a_output = next(row for row in owned(bundle, "workflow_output", sid)
                            if row["source"]["run_id"] == new_a["run_id"])
        new_b_input = next(row for row in bundle["node_input"]
                           if row["input_id"] == new_b["input_id"])
        assert new_b_input["source"] == {
            "kind": "upstream_output", "output_id": new_a_output["output_id"],
        }
        assert new_b_input["payload"] == new_a_output["payload"]

        result = next(row for row in owned(bundle, "workflow_commit", sid)
                      if row["commit_id"] == selected["result_commit_id"])
        snapshot = next(row for row in owned(bundle, "state_snapshot", sid)
                        if row["state_snapshot_id"] == result["state_snapshot_id"])
        assert result["parent_commit_id"] == original_candidate["base_commit_id"]
        assert [row["role"] for row in snapshot["visible_message_refs"]] == [
            "user", "assistant",
        ]
        assert snapshot["visible_message_refs"][-1]["boundary"]["chain_run_id"] == chain["chain_run_id"]
        assert snapshot["visible_message_refs"][-1]["boundary"]["output_id"] == selected["output_id"]
        assert snapshot["visible_message_refs"][0] == next(
            row for row in before["state_snapshot"]
            if row["state_snapshot_id"] == next(
                item["state_snapshot_id"] for item in before["workflow_commit"]
                if item["commit_id"] == original_candidate["result_commit_id"]
            )
        )["visible_message_refs"][0]
        assert len([row for row in bundle["output_delivery"]
                    if row["output_id"] == selected["output_id"]
                    and row["status"] == "succeeded"]) == 1

        branch = service.create_branch(
            sid, formal[-1]["visible_message_id"], idempotency_key="fork-selected",
            expected_source_revision=service.get_session(sid)["revision"],
        )
        child = branch["workflow_session_id"]
        assert service.get_session(child)["messages"] == formal
        branched = durable(database)
        assert len(owned(branched, "workflow_candidate", sid)) == 2
        child_seed = next(row for row in owned(branched, "workflow_commit", child)
                          if row["source"]["kind"] == "session_seed")
        assert child_seed["parent_commit_id"] == selected["result_commit_id"]


def test_failed_reroll_keeps_formal_reply_and_head(database):
    calls = []

    class FailedAdapter:
        def generate(self, messages, tools):
            request = httpx.Request("POST", "https://offline.invalid/chat/completions")
            raise httpx.HTTPStatusError(
                "replacement model failed", request=request,
                response=httpx.Response(400, request=request),
            )

    def factory(stage):
        calls.append(stage)
        return FailedAdapter() if calls == ["A", "B", "A"] else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid, source, head, session, before = source_result(service, database)
        receipt = reroll(service, sid, source, head, session)
        service.wait_for_idle(sid)
        bundle = durable(database)
        rerolled = next(row for row in owned(bundle, "chain_run", sid)
                        if row["chain_run_id"] == receipt["chain_run_id"])
        assert rerolled["status"] == "failed"
        assert owned(bundle, "workflow_ref", sid) == [head]
        assert owned(bundle, "workflow_candidate", sid) == owned(
            before, "workflow_candidate", sid
        )
        assert owned(bundle, "visible_message_ref", sid) == owned(
            before, "visible_message_ref", sid
        )
        assert owned(bundle, "node_session", sid) == owned(
            before, "node_session", sid
        )
        assert service.get_session(sid)["error"]["code"] == "model_error"


def test_failed_reroll_b_keeps_accepted_a_but_no_complete_candidate(database):
    calls = []

    class FailedAdapter:
        def generate(self, messages, tools):
            request = httpx.Request("POST", "https://offline.invalid/chat/completions")
            raise httpx.HTTPStatusError(
                "replacement B failed", request=request,
                response=httpx.Response(400, request=request),
            )

    def factory(stage):
        calls.append(stage)
        return FailedAdapter() if calls == ["A", "B", "A", "B"] else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid, source, head, session, before = source_result(service, database)
        receipt = reroll(service, sid, source, head, session)
        service.wait_for_idle(sid)
        bundle = durable(database)
        rerolled = next(row for row in owned(bundle, "chain_run", sid)
                        if row["chain_run_id"] == receipt["chain_run_id"])
        assert rerolled["status"] == "failed"
        new_a = next(row for row in owned(bundle, "run_record", sid)
                     if row["run_id"] == receipt["run_id"])
        assert new_a["status"] == "succeeded"
        assert any(row["source"]["run_id"] == new_a["run_id"]
                   for row in owned(bundle, "workflow_output", sid))
        assert owned(bundle, "workflow_candidate", sid) == owned(
            before, "workflow_candidate", sid
        )
        assert owned(bundle, "workflow_ref", sid) == [head]
        assert owned(bundle, "visible_message_ref", sid) == owned(
            before, "visible_message_ref", sid
        )
        assert owned(bundle, "node_session", sid) == owned(
            before, "node_session", sid
        )


def test_reroll_head_conflict_retains_unselected_candidate_without_delivery(
    database, monkeypatch,
):
    original_save = SqliteStore.save_bundle

    def conflict(store, records, key, **kwargs):
        if kwargs.get("operation") == "workflow.select_candidate":
            raise ContractValidationError("Workflow ref head conflict")
        return original_save(store, records, key, **kwargs)

    with closing(WorkflowService(database)) as service:
        sid, source, head, session, before = source_result(service, database)
        with monkeypatch.context() as race:
            race.setattr(SqliteStore, "save_bundle", conflict)
            receipt = reroll(service, sid, source, head, session)
            service.wait_for_idle(sid)
        bundle = durable(database)
        archived = next(row for row in owned(bundle, "workflow_candidate", sid)
                        if row["chain_run_id"] == receipt["chain_run_id"])
        assert len(owned(bundle, "workflow_candidate", sid)) == 2
        assert owned(bundle, "workflow_ref", sid) == [head]
        assert owned(bundle, "node_session", sid) == owned(before, "node_session", sid)
        assert not any(row["output_id"] == archived["output_id"]
                       for row in bundle["output_delivery"])
        assert service.get_session(sid)["error"] is None
        selected = select(service, sid, archived, database, "choose-archived")
        assert selected["head_commit_id"] == archived["result_commit_id"]
        assert service.get_session(sid)["messages"][-1]["chain_run_id"] == archived["chain_run_id"]
        assert len(owned(durable(database), "workflow_candidate", sid)) == 2


def test_reroll_selection_fault_retries_without_model_redispatch(database, monkeypatch):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    original_save = SqliteStore.save_bundle
    failed = False

    def fail_once(store, records, key, **kwargs):
        nonlocal failed
        if kwargs.get("operation") == "workflow.select_candidate" and not failed:
            failed = True
            raise RuntimeError("injected selection failure")
        return original_save(store, records, key, **kwargs)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        sid, source, head, session, before = source_result(service, database)
        with monkeypatch.context() as fault:
            fault.setattr(SqliteStore, "save_bundle", fail_once)
            receipt = reroll(service, sid, source, head, session)
            service.wait_for_idle(sid)
        archived = durable(database)
        candidate = next(row for row in owned(archived, "workflow_candidate", sid)
                         if row["chain_run_id"] == receipt["chain_run_id"])
        assert owned(archived, "workflow_ref", sid) == [head]
        assert not any(row["output_id"] == candidate["output_id"]
                       for row in archived["output_delivery"])
        assert service.get_session(sid)["available_actions"] == ["retry_publish"]
        b_run_id = next(row for row in owned(archived, "chain_run", sid)
                        if row["chain_run_id"] == receipt["chain_run_id"])["node_run_ids"][1]
        result = service.retry_publish(
            sid, b_run_id, idempotency_key="retry-selected",
            expected_session_revision=service.get_session(sid)["revision"],
        )
        assert result["status"] == "succeeded"
        assert calls == ["A", "B", "A", "B"]
        selected = durable(database)
        assert owned(selected, "workflow_ref", sid)[0]["head_commit_id"] == candidate["result_commit_id"]
        assert len(owned(selected, "workflow_candidate", sid)) == 2
        assert len([row for row in selected["output_delivery"]
                    if row["output_id"] == candidate["output_id"]
                    and row["status"] == "succeeded"]) == 1


def select(service, sid, candidate, database, key):
    bundle = durable(database)
    head = owned(bundle, "workflow_ref", sid)[0]
    session = owned(bundle, "workflow_session", sid)[0]
    return service.select_candidate(
        sid, candidate["candidate_id"], idempotency_key=key,
        expected_session_revision=session["revision"],
        expected_ref_revision=head["revision"],
        expected_head_commit_id=head["head_commit_id"],
    )


def test_selecting_sibling_back_and_forth_retains_every_result(database):
    with closing(WorkflowService(database)) as service:
        sid, source, head, session, before = source_result(service, database)
        original = owned(before, "workflow_candidate", sid)[0]
        reroll(service, sid, source, head, session)
        service.wait_for_idle(sid)
        generated = durable(database)
        alternative = next(row for row in owned(generated, "workflow_candidate", sid)
                           if row["candidate_id"] != original["candidate_id"])
        all_refs = owned(generated, "visible_message_ref", sid)
        all_candidates = owned(generated, "workflow_candidate", sid)
        original_nodes = {
            row["node_binding_id"]: row for row in owned(generated, "node_session", sid)
        }
        original_delivery = next(row for row in generated["output_delivery"]
                                 if row["output_id"] == original["output_id"])
        selected = select(service, sid, original, database, "back-to-original")
        assert selected["head_commit_id"] == original["result_commit_id"]
        assert service.get_session(sid)["messages"][-1]["chain_run_id"] == source["chain_run_id"]
        back = durable(database)
        assert owned(back, "workflow_candidate", sid) == all_candidates
        assert owned(back, "visible_message_ref", sid) == all_refs
        assert original_delivery in back["output_delivery"]
        assert len([row for row in back["output_delivery"]
                    if row["output_id"] == original["output_id"]]) == 1
        assert all(row["data_version"] == original_nodes[row["node_binding_id"]]["data_version"] + 1
                   for row in owned(back, "node_session", sid))

        selected_again = select(service, sid, alternative, database, "return-to-reroll")
        assert selected_again["head_commit_id"] == alternative["result_commit_id"]
        assert service.get_session(sid)["messages"][-1]["chain_run_id"] == alternative["chain_run_id"]
        assert owned(durable(database), "workflow_candidate", sid) == all_candidates


def test_sibling_selection_rejects_stale_and_downstream_head(database):
    with closing(WorkflowService(database)) as service:
        sid, source, head, session, before = source_result(service, database)
        original = owned(before, "workflow_candidate", sid)[0]
        reroll(service, sid, source, head, session)
        service.wait_for_idle(sid)
        selected = durable(database)
        current_ref = owned(selected, "workflow_ref", sid)[0]
        current_session = owned(selected, "workflow_session", sid)[0]
        with pytest.raises(ContractValidationError, match="head conflict"):
            service.select_candidate(
                sid, original["candidate_id"], idempotency_key="stale",
                expected_session_revision=current_session["revision"],
                expected_ref_revision=current_ref["revision"] - 1,
                expected_head_commit_id=current_ref["head_commit_id"],
            )
        assert durable(database) == selected

        service.submit(sid, "Later formal turn", "later")
        service.wait_for_idle(sid)
        downstream = durable(database)
        with pytest.raises(ContractValidationError, match="current Head"):
            select(service, sid, original, database, "not-latest-floor")
        assert durable(database) == downstream


def test_repeated_reroll_preserves_every_sibling_and_original_frozen_a(database):
    with closing(WorkflowService(database)) as service:
        sid, original_chain, original_head, original_session, initial = source_result(
            service, database,
        )
        first = reroll(service, sid, original_chain, original_head, original_session,
                       key="first-reroll")
        service.wait_for_idle(sid)
        once = durable(database)
        first_chain = next(row for row in owned(once, "chain_run", sid)
                           if row["chain_run_id"] == first["chain_run_id"])
        first_head = owned(once, "workflow_ref", sid)[0]
        first_session = owned(once, "workflow_session", sid)[0]
        second = reroll(service, sid, first_chain, first_head, first_session,
                        key="second-reroll")
        service.wait_for_idle(sid)
        final = durable(database)
        candidates = owned(final, "workflow_candidate", sid)
        assert len(candidates) == 3
        assert {row["chain_run_id"] for row in candidates} == {
            original_chain["chain_run_id"], first["chain_run_id"],
            second["chain_run_id"],
        }
        newest = next(row for row in candidates
                      if row["chain_run_id"] == second["chain_run_id"])
        assert owned(final, "workflow_ref", sid)[0]["head_commit_id"] == newest["result_commit_id"]
        assert [row["role"] for row in service.get_session(sid)["messages"]] == [
            "user", "assistant",
        ]
        assert service.get_session(sid)["messages"][-1]["chain_run_id"] == second["chain_run_id"]
        assert len(owned(final, "visible_message_ref", sid)) == 4
        original_run = next(row for row in owned(initial, "run_record", sid)
                            if row["run_id"] == original_chain["node_run_ids"][0])
        repeated_run = next(row for row in owned(final, "run_record", sid)
                            if row["run_id"] == second["run_id"])
        original_snapshot = next(row for row in initial["input_snapshot"]
                                 if row["snapshot_id"] == original_run["snapshot_id"])
        repeated_snapshot = next(row for row in final["input_snapshot"]
                                 if row["snapshot_id"] == repeated_run["snapshot_id"])
        assert {key: value for key, value in repeated_snapshot.items()
                if key not in ("snapshot_id", "input_id", "created_at")} == {
                    key: value for key, value in original_snapshot.items()
                    if key not in ("snapshot_id", "input_id", "created_at")
                }
