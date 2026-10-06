"""Public reply-candidate projection without mutable message content."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from phase1_agent.workflow import WorkflowService


def test_view_exposes_each_durable_reply_and_selected_head():
    with TemporaryDirectory(prefix="candidate-view-", dir=Path(__file__).parent) as folder:
        with closing(WorkflowService(Path(folder) / "workflow.sqlite")) as service:
            sid = service.create_session()["workflow_session_id"]
            submitted = service.submit(sid, "Retain each reply", "submit")
            service.wait_for_idle(sid)
            original = service.get_session(sid)
            assert original["can_reroll"]
            assert not original["can_select_candidates"]
            assert original["ref_revision"] >= 2
            assert original["head_commit_id"]
            first = original["messages"][-1]["reply_candidates"]
            assert len(first) == 1 and first[0]["selected"]
            assert first[0]["payload"] == original["messages"][-1]["payload"]

            service.reroll(
                sid, submitted["chain_run_id"], idempotency_key="reroll",
                expected_session_revision=original["revision"],
                expected_ref_revision=original["ref_revision"],
                expected_head_commit_id=original["head_commit_id"],
            )
            service.wait_for_idle(sid)
            changed = service.get_session(sid)
            replies = changed["messages"][-1]["reply_candidates"]
            assert len(replies) == 2
            assert changed["can_select_candidates"]
            assert replies[0] == {**first[0], "selected": False}
            assert replies[1]["selected"]
            assert replies[1]["payload"] == changed["messages"][-1]["payload"]
            assert {item["chain_run_id"] for item in replies} == {
                submitted["chain_run_id"], changed["messages"][-1]["chain_run_id"],
            }

            service.select_candidate(
                sid, first[0]["candidate_id"], idempotency_key="select-original",
                expected_session_revision=changed["revision"],
                expected_ref_revision=changed["ref_revision"],
                expected_head_commit_id=changed["head_commit_id"],
            )
            selected = service.get_session(sid)
            assert selected["messages"][-1]["payload"] == first[0]["payload"]
            assert selected["can_select_candidates"]
            assert [item["selected"] for item in selected["messages"][-1]["reply_candidates"]] == [
                True, False,
            ]


def test_fork_projection_freezes_candidates_before_later_source_roll():
    with TemporaryDirectory(prefix="candidate-view-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database)) as service:
            sid = service.create_session()["workflow_session_id"]
            source = service.submit(sid, "Freeze completed replies", "submit")
            service.wait_for_idle(sid)
            original = service.get_session(sid)
            service.reroll(
                sid, source["chain_run_id"], idempotency_key="first-reroll",
                expected_session_revision=original["revision"],
                expected_ref_revision=original["ref_revision"],
                expected_head_commit_id=original["head_commit_id"],
            )
            service.wait_for_idle(sid)
            before_fork = service.get_session(sid)
            child_id = service.create_branch(
                sid, before_fork["messages"][-1]["visible_message_id"],
                idempotency_key="fork", expected_source_revision=before_fork["revision"],
            )["workflow_session_id"]
            frozen = service.get_session(child_id)
            assert frozen["chains"] == []
            assert frozen["can_select_candidates"]
            assert frozen["can_reroll"]
            assert frozen["messages"][-1]["reply_candidates"] == (
                before_fork["messages"][-1]["reply_candidates"]
            )

            service.reroll(
                sid, before_fork["messages"][-1]["chain_run_id"],
                idempotency_key="second-reroll",
                expected_session_revision=service.get_session(sid)["revision"],
                expected_ref_revision=service.get_session(sid)["ref_revision"],
                expected_head_commit_id=service.get_session(sid)["head_commit_id"],
            )
            service.wait_for_idle(sid)
            assert len(service.get_session(sid)["messages"][-1]["reply_candidates"]) == 3
            assert service.get_session(child_id)["messages"][-1]["reply_candidates"] == (
                frozen["messages"][-1]["reply_candidates"]
            )
            assert service.get_session(child_id)["can_select_candidates"]
            assert service.get_session(child_id)["can_reroll"]

        with closing(WorkflowService(database)) as reopened:
            assert reopened.get_session(child_id)["messages"][-1]["reply_candidates"] == (
                frozen["messages"][-1]["reply_candidates"]
            )
