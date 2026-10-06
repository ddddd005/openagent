"""Archived reply variants remain browseable and forkable at their floor."""

import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="inherited-selection-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def durable(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def owned(bundle, kind, sid):
    return [row for row in bundle.get(kind, [])
            if row["workflow_session_id"] == sid]


def head(bundle, sid):
    return owned(bundle, "workflow_ref", sid)[0]


def result_state(bundle, commit_id):
    commit = next(row for row in bundle["workflow_commit"]
                  if row["commit_id"] == commit_id)
    return next(row for row in bundle["state_snapshot"]
                if row["state_snapshot_id"] == commit["state_snapshot_id"])


def roll(service, sid, key):
    view = service.get_session(sid)
    service.reroll(
        sid, view["messages"][-1]["chain_run_id"], idempotency_key=key,
        expected_session_revision=view["revision"],
        expected_ref_revision=view["ref_revision"],
        expected_head_commit_id=view["head_commit_id"],
    )
    service.wait_for_idle(sid)
    assert service.get_session(sid)["error"] is None


def select(service, sid, candidate_id, key, view=None):
    view = view or service.get_session(sid)
    return service.select_candidate(
        sid, candidate_id, idempotency_key=key,
        expected_session_revision=view["revision"],
        expected_ref_revision=view["ref_revision"],
        expected_head_commit_id=view["head_commit_id"],
    )


def three_floors(service):
    sid = service.create_session()["workflow_session_id"]
    for floor, count in ((1, 4), (2, 3), (3, 1)):
        service.submit(sid, f"Floor {floor}", f"submit-{floor}")
        service.wait_for_idle(sid)
        for variant in range(1, count):
            roll(service, sid, f"floor-{floor}-roll-{variant}")
    view = service.get_session(sid)
    assert [len(row["reply_candidates"]) for row in view["messages"]
            if row["role"] == "assistant"] == [4, 3, 1]
    return sid, view


def test_historical_candidate_fork_truncates_and_child_can_switch(database):
    with closing(WorkflowService(database)) as service:
        sid, source = three_floors(service)
        source_head = head(durable(database), sid)
        first_assistant = source["messages"][1]
        first_candidate = first_assistant["reply_candidates"][0]
        with pytest.raises(ContractValidationError, match="terminal|current Head"):
            select(service, sid, first_candidate["candidate_id"], "historical-select")
        with pytest.raises(ContractValidationError, match="selected Head"):
            service.reroll(
                sid, first_assistant["chain_run_id"], idempotency_key="historical-roll",
                expected_session_revision=source["revision"],
                expected_ref_revision=source["ref_revision"],
                expected_head_commit_id=source["head_commit_id"],
            )

        branch = service.create_branch(
            sid, first_assistant["visible_message_id"],
            idempotency_key="historical-fork",
            expected_source_revision=source["revision"],
            candidate_id=first_candidate["candidate_id"],
        )
        child = branch["workflow_session_id"]
        child_view = service.get_session(child)
        assert [row["role"] for row in child_view["messages"]] == ["user", "assistant"]
        assert child_view["messages"][-1]["visible_message_id"] != first_assistant["visible_message_id"]
        assert child_view["messages"][-1]["payload"] == first_candidate["payload"]
        assert len(child_view["messages"][-1]["reply_candidates"]) == 4
        assert [row["selected"] for row in child_view["messages"][-1]["reply_candidates"]] == [
            True, False, False, False,
        ]
        assert [len(row["candidate_ids"])
                for row in service.list_reply_candidate_floors(child)] == [4]
        bundle = durable(database)
        assert head(bundle, sid) == source_head
        assert owned(bundle, "workflow_candidate", child) == []
        seed = owned(bundle, "workflow_commit", child)[0]
        selected_candidate = next(row for row in bundle["workflow_candidate"]
                                  if row["candidate_id"] == first_candidate["candidate_id"])
        assert seed["parent_commit_id"] == selected_candidate["result_commit_id"]
        assert result_state(bundle, head(bundle, child)["head_commit_id"])[
            "visible_message_refs"][-1]["visible_message_id"] == child_view[
                "messages"][-1]["visible_message_id"]

        other_id = child_view["messages"][-1]["reply_candidates"][2]["candidate_id"]
        receipt = select(service, child, other_id, "child-select", child_view)
        assert select(service, child, other_id, "child-select", child_view) == receipt
        after = service.get_session(child)
        assert after["messages"][-1]["payload"] == (
            child_view["messages"][-1]["reply_candidates"][2]["payload"]
        )
        assert [row["selected"] for row in after["messages"][-1]["reply_candidates"]] == [
            False, False, True, False,
        ]
        with pytest.raises(ContractValidationError, match="different workflow operation request"):
            select(service, child, first_candidate["candidate_id"], "child-select", child_view)
        changed = durable(database)
        assert head(changed, sid) == source_head
        assert owned(changed, "workflow_candidate", child) == []
        assert result_state(changed, receipt["head_commit_id"])[
            "visible_message_refs"][-1]["visible_message_id"] == after[
                "messages"][-1]["visible_message_id"]

        select(service, child, first_candidate["candidate_id"], "child-select-back")
        assert service.get_session(child)["messages"][-1]["payload"] == first_candidate["payload"]
        service.submit(child, "Continue from selected history", "child-submit")
        service.wait_for_idle(child)
        assert [row["role"] for row in service.get_session(child)["messages"]] == [
            "user", "assistant", "user", "assistant",
        ]
        with pytest.raises(ContractValidationError, match="terminal formal reply"):
            select(service, child, other_id, "after-downstream")
        durable(database)

        second_assistant = source["messages"][3]
        second_candidates = second_assistant["reply_candidates"]
        second_branch = service.create_branch(
            sid, second_assistant["visible_message_id"],
            idempotency_key="second-floor-fork",
            expected_source_revision=service.get_session(sid)["revision"],
            candidate_id=second_candidates[0]["candidate_id"],
        )
        second_child = second_branch["workflow_session_id"]
        second_view = service.get_session(second_child)
        assert [row["role"] for row in second_view["messages"]] == [
            "user", "assistant", "user", "assistant",
        ]
        assert [len(row["candidate_ids"]) for row in
                service.list_reply_candidate_floors(second_child)] == [4, 3]
        assert [row["selected"] for row in
                second_view["messages"][-1]["reply_candidates"]] == [True, False, False]
        assert second_view["messages"][-1]["payload"] == second_candidates[0]["payload"]
        with pytest.raises(ContractValidationError, match="another workflow session"):
            select(service, second_child, first_candidate["candidate_id"], "old-floor-read-only")
        select(service, second_child, second_candidates[1]["candidate_id"], "second-floor-select")
        assert service.get_session(second_child)["messages"][-1]["payload"] == (
            second_candidates[1]["payload"]
        )
        assert head(durable(database), sid) == source_head

    with closing(WorkflowService(database)) as reopened:
        assert len(reopened.get_session(child)["messages"]) == 4
        assert len(reopened.get_session(sid)["messages"]) == 6
        assert [len(row["candidate_ids"]) for row in
                reopened.list_reply_candidate_floors(second_child)] == [4, 3]
        assert head(durable(database), sid) == source_head


def test_candidate_fork_request_identity_and_rejections(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Two candidates", "submit")
        service.wait_for_idle(sid)
        roll(service, sid, "roll")
        view = service.get_session(sid)
        reply = view["messages"][-1]
        first, second = [row["candidate_id"] for row in reply["reply_candidates"]]
        branch = service.create_and_switch_branch(
            sid, reply["visible_message_id"], idempotency_key="branch",
            expected_source_revision=view["revision"], candidate_id=first,
        )
        assert service.create_and_switch_branch(
            sid, reply["visible_message_id"], idempotency_key="branch",
            expected_source_revision=view["revision"], candidate_id=first,
        ) == branch
        with pytest.raises(ContractValidationError, match="different workflow operation request"):
            service.create_and_switch_branch(
                sid, reply["visible_message_id"], idempotency_key="branch",
                expected_source_revision=view["revision"], candidate_id=second,
            )
        with pytest.raises(ContractValidationError, match="assistant anchor"):
            service.create_branch(
                sid, view["messages"][0]["visible_message_id"],
                idempotency_key="user-candidate",
                expected_source_revision=service.get_session(sid)["revision"],
                candidate_id=first,
            )
        with pytest.raises(ContractValidationError, match="frozen fork floor"):
            service.create_branch(
                sid, reply["visible_message_id"], idempotency_key="foreign-candidate",
                expected_source_revision=service.get_session(sid)["revision"],
                candidate_id=str(uuid4()),
            )
        child = branch["workflow_session_id"]
        child_view = service.get_session(child)
        with pytest.raises(ContractValidationError, match="already the current Head"):
            select(service, child, first, "same")
        with pytest.raises(ContractValidationError, match="revision conflict"):
            select(service, child, second, "stale", {
                **child_view, "revision": child_view["revision"] + 1,
            })
        with pytest.raises(ContractValidationError, match="Record not found"):
            select(service, child, str(uuid4()), "unknown")
        assert service.get_session(child) == child_view


def test_inherited_selection_fault_rolls_back_and_graph_rejects_tampering(
    database, monkeypatch,
):
    armed = False

    def fail(point):
        if armed and point == "before_commit":
            raise RuntimeError("inherited selection fault")

    with closing(WorkflowService(database, fault_injector=fail)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Select inherited", "submit")
        service.wait_for_idle(sid)
        roll(service, sid, "roll")
        source = service.get_session(sid)
        reply = source["messages"][-1]
        child = service.create_branch(
            sid, reply["visible_message_id"], idempotency_key="fork",
            expected_source_revision=source["revision"],
        )["workflow_session_id"]
        target = reply["reply_candidates"][0]["candidate_id"]
        before = durable(database)
        child_view = service.get_session(child)
        original_save = SqliteStore._save_bundle_transaction

        def conflict_at_commit(store, records, key, **kwargs):
            if kwargs.get("operation") == "workflow.explicit_select_candidate":
                kwargs = dict(kwargs)
                kwargs["expected_ref_heads"] = {
                    ref_id: (revision, str(uuid4()))
                    for ref_id, (revision, _) in kwargs["expected_ref_heads"].items()
                }
            return original_save(store, records, key, **kwargs)

        with monkeypatch.context() as conflict:
            conflict.setattr(SqliteStore, "_save_bundle_transaction", conflict_at_commit)
            with pytest.raises(ContractValidationError, match="head conflict"):
                select(service, child, target, "cas", child_view)
        assert durable(database) == before
        armed = True
        with pytest.raises(RuntimeError, match="inherited selection fault"):
            select(service, child, target, "fault", child_view)
        armed = False
        assert durable(database) == before
        receipt = select(service, child, target, "fault", child_view)
        assert select(service, child, target, "fault", child_view) == receipt
    bundle = durable(database)
    tampered = copy.deepcopy(bundle)
    child_commit = next(row for row in tampered["workflow_commit"]
                        if row["commit_id"] == receipt["head_commit_id"])
    child_state = next(row for row in tampered["state_snapshot"]
                       if row["state_snapshot_id"] == child_commit["state_snapshot_id"])
    child_state["visible_message_refs"][-1]["boundary"]["output_id"] = str(uuid4())
    with pytest.raises(ContractValidationError):
        validate_bundle(tampered)


def test_nested_fork_with_shared_turn_ancestors_keeps_frozen_variants(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Common ancestor", "first")
        service.wait_for_idle(sid)
        service.submit(sid, "Second floor", "second")
        service.wait_for_idle(sid)
        roll(service, sid, "second-variant")
        source = service.get_session(sid)
        reply = source["messages"][-1]
        first, second = [row["candidate_id"] for row in reply["reply_candidates"]]
        child = service.create_branch(
            sid, reply["visible_message_id"], idempotency_key="first-fork",
            expected_source_revision=source["revision"], candidate_id=first,
        )["workflow_session_id"]
        source_head = head(durable(database), sid)
        child_view = service.get_session(child)
        select(service, child, second, "switch-to-sibling")
        selected = service.get_session(child)
        assert [len(row["reply_candidates"]) for row in selected["messages"]
                if row["role"] == "assistant"] == [1, 2]
        grandchild = service.create_branch(
            child, selected["messages"][-1]["visible_message_id"],
            idempotency_key="nested-fork",
            expected_source_revision=selected["revision"], candidate_id=first,
        )["workflow_session_id"]
        assert head(durable(database), sid) == source_head
        nested = service.get_session(grandchild)
        assert nested["messages"][-1]["payload"] == (
            child_view["messages"][-1]["reply_candidates"][0]["payload"]
        )
        assert [len(row["candidate_ids"]) for row in
                service.list_reply_candidate_floors(grandchild)] == [1, 2]
        select(service, grandchild, second, "nested-sibling")
        assert service.get_session(grandchild)["messages"][-1]["payload"] == (
            selected["messages"][-1]["payload"]
        )
        service.submit(grandchild, "Continue nested history", "nested-submit")
        service.wait_for_idle(grandchild)
        assert [row["role"] for row in service.get_session(grandchild)["messages"]] == [
            "user", "assistant", "user", "assistant", "user", "assistant",
        ]
        assert head(durable(database), sid) == source_head

    with closing(WorkflowService(database)) as reopened:
        assert [len(row["candidate_ids"]) for row in
                reopened.list_reply_candidate_floors(grandchild)] == [1, 2, 1]
        durable(database)
