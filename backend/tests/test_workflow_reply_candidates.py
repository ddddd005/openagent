"""Read-only enumeration of complete replies belonging to one formal user."""

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
from phase1_agent.workflow_reply_candidates import list_workflow_reply_candidates


@pytest.fixture
def completed():
    with TemporaryDirectory(prefix="reply-candidates-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database)) as service:
            sid = service.create_session()["workflow_session_id"]
            receipt = service.submit(sid, "Save every complete reply", "submit")
            service.wait_for_idle(sid)
            with closing(SqliteStore(database)) as store:
                yield validate_bundle(store.read_bundle()), sid, receipt["visible_message_id"]


def test_original_candidate_is_found_without_changing_validated_bundle(completed):
    bundle, sid, user_id = completed
    original = copy.deepcopy(bundle)
    candidate = bundle["workflow_candidate"][0]

    assert list_workflow_reply_candidates(bundle, sid, user_id) == [{
        "candidate_id": candidate["candidate_id"],
        "chain_run_id": candidate["chain_run_id"],
        "output_id": candidate["output_id"],
    }]
    assert bundle == original


def test_later_formal_user_in_same_session_is_not_a_reply_candidate():
    with TemporaryDirectory(prefix="reply-candidates-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database)) as service:
            sid = service.create_session()["workflow_session_id"]
            first = service.submit(sid, "First floor", "first")
            service.wait_for_idle(sid)
            second = service.submit(sid, "Second floor", "second")
            service.wait_for_idle(sid)
            with closing(SqliteStore(database)) as store:
                bundle = validate_bundle(store.read_bundle())

    first_candidates = list_workflow_reply_candidates(
        bundle, sid, first["visible_message_id"],
    )
    second_candidates = list_workflow_reply_candidates(
        bundle, sid, second["visible_message_id"],
    )
    assert [row["chain_run_id"] for row in first_candidates] == [first["chain_run_id"]]
    assert [row["chain_run_id"] for row in second_candidates] == [second["chain_run_id"]]


def _descendant(bundle, sid, user_id, source_id, *, status):
    """Add only fields read by this query to exercise its validated-bundle traversal."""
    chain_id = str(uuid4())
    output_id = str(uuid4()) if status == "succeeded" else None
    bundle["chain_run"].append({
        "chain_run_id": chain_id, "workflow_session_id": sid,
        "input_id": str(uuid4()), "status": status, "output_id": output_id,
    })
    bundle.setdefault("chain_input_origin", []).append({
        "chain_run_id": chain_id, "workflow_session_id": sid,
        "visible_message_id": user_id, "source_chain_run_id": source_id,
    })
    if output_id is not None:
        candidate_id = str(uuid4())
        bundle["workflow_candidate"].append({
            "candidate_id": candidate_id, "workflow_session_id": sid,
            "chain_run_id": chain_id, "output_id": output_id,
        })
        return chain_id, candidate_id, output_id
    return chain_id, None, None


def test_recursive_rerolls_keep_all_successful_results_in_archive_order(completed):
    bundle, sid, user_id = completed
    records = copy.deepcopy(bundle)
    root = records["workflow_candidate"][0]
    first = _descendant(records, sid, user_id, root["chain_run_id"], status="succeeded")
    failed = _descendant(records, sid, user_id, first[0], status="failed")
    last = _descendant(records, sid, user_id, failed[0], status="succeeded")
    sibling = _descendant(records, sid, user_id, root["chain_run_id"], status="succeeded")
    before = copy.deepcopy(records)

    assert list_workflow_reply_candidates(records, sid, user_id) == [
        {"candidate_id": root["candidate_id"], "chain_run_id": root["chain_run_id"],
         "output_id": root["output_id"]},
        {"candidate_id": first[1], "chain_run_id": first[0], "output_id": first[2]},
        {"candidate_id": last[1], "chain_run_id": last[0], "output_id": last[2]},
        {"candidate_id": sibling[1], "chain_run_id": sibling[0], "output_id": sibling[2]},
    ]
    assert records == before


def test_missing_foreign_and_unfinished_user_anchors_are_rejected(completed):
    bundle, sid, user_id = completed
    assistant_id = next(row["visible_message_id"] for row in bundle["visible_message_ref"]
                        if row["role"] == "assistant")
    for session_id, message_id in ((sid, str(uuid4())), (sid, assistant_id),
                                   (str(uuid4()), user_id)):
        with pytest.raises(ContractValidationError, match="formal user anchor"):
            list_workflow_reply_candidates(bundle, session_id, message_id)

    unfinished = copy.deepcopy(bundle)
    ref = next(row for row in unfinished["visible_message_ref"] if row["role"] == "user")
    ref["boundary"]["input_status"] = "failed"
    with pytest.raises(ContractValidationError, match="completed user anchor"):
        list_workflow_reply_candidates(unfinished, sid, user_id)


def test_foreign_inherited_chain_and_inconsistent_candidate_are_rejected(completed):
    bundle, sid, user_id = completed
    foreign = copy.deepcopy(bundle)
    foreign["visible_message_ref"][0]["workflow_session_id"] = str(uuid4())
    with pytest.raises(ContractValidationError, match="another session"):
        list_workflow_reply_candidates(
            foreign, foreign["visible_message_ref"][0]["workflow_session_id"], user_id,
        )

    invalid = copy.deepcopy(bundle)
    invalid["workflow_candidate"][0]["output_id"] = str(uuid4())
    with pytest.raises(ContractValidationError, match="completed result"):
        list_workflow_reply_candidates(invalid, sid, user_id)

    absent = copy.deepcopy(bundle)
    absent["workflow_candidate"].clear()
    with pytest.raises(ContractValidationError, match="complete original candidate"):
        list_workflow_reply_candidates(absent, sid, user_id)


def test_related_origin_cannot_cross_session_or_user_boundary(completed):
    bundle, sid, user_id = completed
    for field in ("workflow_session_id", "visible_message_id"):
        invalid = copy.deepcopy(bundle)
        _descendant(
            invalid, sid, user_id, invalid["chain_run"][0]["chain_run_id"],
            status="succeeded",
        )
        invalid["chain_input_origin"][0][field] = str(uuid4())
        with pytest.raises(ContractValidationError, match="another session or input"):
            list_workflow_reply_candidates(invalid, sid, user_id)
