"""A fork may reroll its terminal inherited reply into child-owned candidates."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import WorkflowService


def records(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def owned(bundle, kind, sid):
    return [row for row in bundle.get(kind, [])
            if row["workflow_session_id"] == sid]


def reroll(service, sid, key, source=None):
    view = service.get_session(sid)
    receipt = service.reroll(
        sid, source or view["messages"][-1]["chain_run_id"],
        idempotency_key=key, expected_session_revision=view["revision"],
        expected_ref_revision=view["ref_revision"],
        expected_head_commit_id=view["head_commit_id"],
    )
    service.wait_for_idle(sid)
    assert service.get_session(sid)["error"] is None
    return receipt


def test_child_reroll_inherited_reply_preserves_frozen_parent_and_all_candidates():
    with TemporaryDirectory(prefix="inherited-reroll-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database)) as service:
            parent = service.create_session()["workflow_session_id"]
            service.submit(parent, "First floor", "submit")
            service.wait_for_idle(parent)
            reroll(service, parent, "parent-roll")
            parent_view = service.get_session(parent)
            old_parent = records(database)
            source_reply = parent_view["messages"][-1]
            child = service.create_branch(
                parent, source_reply["visible_message_id"],
                idempotency_key="fork", expected_source_revision=parent_view["revision"],
                candidate_id=source_reply["reply_candidates"][0]["candidate_id"],
            )["workflow_session_id"]
            child_view = service.get_session(child)
            assert child_view["can_reroll"]
            selected_source = child_view["messages"][-1]["chain_run_id"]
            assert selected_source != source_reply["chain_run_id"]
            assert owned(records(database), "chain_run", child) == []
            accepted = reroll(service, child, "child-roll")
            after = records(database)
            assert accepted["source_chain_run_id"] == selected_source
            assert len(owned(after, "workflow_candidate", child)) == 1
            assert all(row in after["workflow_candidate"]
                       for row in owned(old_parent, "workflow_candidate", parent))
            assert owned(after, "workflow_ref", parent) == owned(old_parent, "workflow_ref", parent)
            assert service.get_session(parent)["messages"] == parent_view["messages"]
            child_view = service.get_session(child)
            assert len(child_view["messages"][-1]["reply_candidates"]) == 3
            assert child_view["messages"][-1]["chain_run_id"] == accepted["chain_run_id"]
            assert child_view["can_reroll"]
            inherited_id = child_view["messages"][-1]["reply_candidates"][0]["candidate_id"]
            generated_id = next(
                row["candidate_id"] for row in child_view["messages"][-1]["reply_candidates"]
                if row["chain_run_id"] == accepted["chain_run_id"]
            )
            service.select_candidate(
                child, inherited_id, idempotency_key="back-to-inherited",
                expected_session_revision=child_view["revision"],
                expected_ref_revision=child_view["ref_revision"],
                expected_head_commit_id=child_view["head_commit_id"],
            )
            inherited_view = service.get_session(child)
            assert inherited_view["messages"][-1]["chain_run_id"] == selected_source
            nested_inherited = service.create_branch(
                child, inherited_view["messages"][-1]["visible_message_id"],
                idempotency_key="fork-inherited-again",
                expected_source_revision=inherited_view["revision"],
            )["workflow_session_id"]
            assert service.get_session(nested_inherited)["can_reroll"]
            reroll(service, nested_inherited, "nested-inherited-roll")
            assert len(service.list_reply_candidate_floors(nested_inherited)[0]["candidate_ids"]) == 4
            assert service.get_session(parent)["messages"] == parent_view["messages"]
            inherited_view = service.get_session(child)
            service.select_candidate(
                child, generated_id, idempotency_key="back-to-child",
                expected_session_revision=inherited_view["revision"],
                expected_ref_revision=inherited_view["ref_revision"],
                expected_head_commit_id=inherited_view["head_commit_id"],
            )
            assert reroll(service, child, "child-second")["source_chain_run_id"] == accepted["chain_run_id"]
            assert len(service.list_reply_candidate_floors(child)[0]["candidate_ids"]) == 4
            assert len(owned(records(database), "workflow_candidate", child)) == 2
            frozen_child_ids = service.list_reply_candidate_floors(child)[0]["candidate_ids"]
            reroll(service, parent, "parent-later-roll")
            assert len(service.list_reply_candidate_floors(parent)[0]["candidate_ids"]) == 3
            assert service.list_reply_candidate_floors(child)[0]["candidate_ids"] == frozen_child_ids

            selected = service.get_session(child)
            grandchild = service.create_branch(
                child, selected["messages"][-1]["visible_message_id"],
                idempotency_key="fork-grandchild", expected_source_revision=selected["revision"],
            )["workflow_session_id"]
            assert len(service.list_reply_candidate_floors(grandchild)[0]["candidate_ids"]) == 4
            service.submit(child, "Next floor", "next")
            service.wait_for_idle(child)
            later = service.get_session(child)
            with pytest.raises(ContractValidationError, match="terminal selected inherited reply"):
                service.reroll(
                    child, selected_source, idempotency_key="historical",
                    expected_session_revision=later["revision"],
                    expected_ref_revision=later["ref_revision"],
                    expected_head_commit_id=later["head_commit_id"],
                )
        with closing(WorkflowService(database)) as reopened:
            assert len(reopened.list_reply_candidate_floors(child)[0]["candidate_ids"]) == 4
            validate_bundle(records(database))
