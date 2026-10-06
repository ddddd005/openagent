"""A fork freezes all archived reply variants without copying their executions."""

import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="fork-candidates-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def durable(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def owned(bundle, kind, sid):
    return [row for row in bundle.get(kind, []) if row["workflow_session_id"] == sid]


def roll(service, database, sid, key):
    bundle = durable(database)
    head = owned(bundle, "workflow_ref", sid)[0]
    session = owned(bundle, "workflow_session", sid)[0]
    source = next(row for row in owned(bundle, "workflow_candidate", sid)
                  if row["result_commit_id"] == head["head_commit_id"])
    receipt = service._reroll(
        sid, source["chain_run_id"], idempotency_key=key,
        expected_session_revision=session["revision"],
        expected_ref_revision=head["revision"],
        expected_head_commit_id=head["head_commit_id"],
    )
    service.wait_for_idle(sid)
    return receipt


def fork_latest(service, sid, key):
    view = service.get_session(sid)
    return service.create_branch(
        sid, view["messages"][-1]["visible_message_id"],
        idempotency_key=key, expected_source_revision=view["revision"],
    )


def test_fork_freezes_all_rolls_and_source_later_roll_does_not_leak(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Preserve variants", "submit")
        service.wait_for_idle(sid)
        roll(service, database, sid, "first-roll")
        before = durable(database)
        candidates = owned(before, "workflow_candidate", sid)
        source_head = owned(before, "workflow_ref", sid)[0]
        source_visible = service.get_session(sid)["messages"]

        branch = fork_latest(service, sid, "fork-two")
        child = branch["workflow_session_id"]
        frozen = service.list_reply_candidate_floors(child)
        assert frozen == [{
            "user_visible_message_id": source_visible[0]["visible_message_id"],
            "candidate_ids": [row["candidate_id"] for row in candidates],
            "selected_candidate_id": candidates[-1]["candidate_id"],
        }]
        assert service.get_session(child)["messages"] == source_visible
        assert owned(durable(database), "chain_run", child) == []
        assert owned(durable(database), "workflow_ref", sid)[0] == source_head

        roll(service, database, sid, "later-roll")
        assert len(service.list_reply_candidate_floors(sid)[0]["candidate_ids"]) == 3
        assert service.list_reply_candidate_floors(child) == frozen
        stored = durable(database)
        anchor = next(row for row in stored["fork_anchor"]
                      if row["fork_anchor_id"] == branch["fork_anchor_id"])
        assert anchor["candidate_floors"] == frozen
        assert len(owned(stored, "workflow_candidate", sid)) == 3
        assert owned(stored, "workflow_candidate", child) == []

    with closing(WorkflowService(database)) as reopened:
        assert reopened.list_reply_candidate_floors(child) == frozen
        assert reopened.get_session(child)["messages"] == source_visible


def test_fork_carries_earlier_floors_then_child_adds_its_own(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "First floor", "first")
        service.wait_for_idle(sid)
        roll(service, database, sid, "first-floor-roll")
        service.submit(sid, "Second floor", "second")
        service.wait_for_idle(sid)
        branch = fork_latest(service, sid, "first-child")
        child = branch["workflow_session_id"]
        original = service.list_reply_candidate_floors(child)
        assert [len(row["candidate_ids"]) for row in original] == [2, 1]

        service.submit(child, "Third floor", "third")
        service.wait_for_idle(child)
        child_floors = service.list_reply_candidate_floors(child)
        assert child_floors[:2] == original
        assert [len(row["candidate_ids"]) for row in child_floors] == [2, 1, 1]
        grandchild = fork_latest(service, child, "grandchild")
        assert service.list_reply_candidate_floors(grandchild["workflow_session_id"]) == child_floors
        stored = durable(database)
        assert len(owned(stored, "chain_run", grandchild["workflow_session_id"])) == 0


def test_user_fork_keeps_prior_rolls_but_not_the_source_reply_to_pending_user(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "First floor", "first")
        service.wait_for_idle(sid)
        roll(service, database, sid, "first-roll")
        second = service.submit(sid, "Pending on branch", "second")
        service.wait_for_idle(sid)
        source_floors = service.list_reply_candidate_floors(sid)
        source_view = service.get_session(sid)

        branch = service.create_branch(
            sid, second["visible_message_id"], idempotency_key="fork-user",
            expected_source_revision=source_view["revision"],
        )
        child = branch["workflow_session_id"]
        assert service.list_reply_candidate_floors(child) == source_floors[:1]
        assert [row["role"] for row in service.get_session(child)["messages"]] == [
            "user", "assistant", "user",
        ]
        service.continue_pending_input(
            child, branch["pending_input_id"], idempotency_key="start-pending",
            expected_session_revision=service.get_session(child)["revision"],
        )
        service.wait_for_idle(child)
        child_floors = service.list_reply_candidate_floors(child)
        assert child_floors[0] == source_floors[0]
        assert child_floors[1]["user_visible_message_id"] == second["visible_message_id"]
        assert child_floors[1]["candidate_ids"] != source_floors[1]["candidate_ids"]
        assert len(child_floors[1]["candidate_ids"]) == 1
        validate_bundle(durable(database))


def test_graph_rejects_reordered_foreign_or_mismatched_selected_candidates(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "First floor", "first")
        service.wait_for_idle(sid)
        roll(service, database, sid, "first-roll")
        service.submit(sid, "Second floor", "second")
        service.wait_for_idle(sid)
        branch = fork_latest(service, sid, "fork-validation")
    bundle = durable(database)
    anchor = next(row for row in bundle["fork_anchor"]
                  if row["fork_anchor_id"] == branch["fork_anchor_id"])
    assert [len(row["candidate_ids"]) for row in anchor["candidate_floors"]] == [2, 1]

    reordered = copy.deepcopy(bundle)
    floor = next(row for row in reordered["fork_anchor"]
                 if row["fork_anchor_id"] == branch["fork_anchor_id"])["candidate_floors"][0]
    floor["candidate_ids"].reverse()
    with pytest.raises(ContractValidationError, match="archived floor prefix"):
        validate_bundle(reordered)

    foreign = copy.deepcopy(bundle)
    floor = next(row for row in foreign["fork_anchor"]
                 if row["fork_anchor_id"] == branch["fork_anchor_id"])["candidate_floors"][0]
    floor["candidate_ids"][1] = anchor["candidate_floors"][1]["candidate_ids"][0]
    with pytest.raises(ContractValidationError, match="archived floor prefix"):
        validate_bundle(foreign)

    wrong_selection = copy.deepcopy(bundle)
    floor = next(row for row in wrong_selection["fork_anchor"]
                 if row["fork_anchor_id"] == branch["fork_anchor_id"])["candidate_floors"][0]
    floor["selected_candidate_id"] = floor["candidate_ids"][0]
    with pytest.raises(ContractValidationError, match="child formal reply"):
        validate_bundle(wrong_selection)

    missing_floor = copy.deepcopy(bundle)
    next(row for row in missing_floor["fork_anchor"]
         if row["fork_anchor_id"] == branch["fork_anchor_id"])["candidate_floors"].pop(0)
    with pytest.raises(ContractValidationError, match="child visible prefix"):
        validate_bundle(missing_floor)


def test_fork_failure_rolls_back_manifest_and_source_revision(database):
    armed = False

    def fail(point):
        if armed and point == "before_commit":
            raise RuntimeError("fork transaction failed")

    with closing(WorkflowService(database, fault_injector=fail)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Atomic variants", "submit")
        service.wait_for_idle(sid)
        roll(service, database, sid, "roll")
        before = durable(database)
        view = service.get_session(sid)
        armed = True
        with pytest.raises(RuntimeError, match="fork transaction failed"):
            fork_latest(service, sid, "fault")
        armed = False
        assert durable(database) == before
        branch = fork_latest(service, sid, "fault")
        assert len(service.list_reply_candidate_floors(branch["workflow_session_id"])[0]["candidate_ids"]) == 2
