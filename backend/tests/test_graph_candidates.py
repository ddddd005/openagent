"""Shared helpers for current graph completion candidates."""

from contextlib import closing
from uuid import uuid4

from phase1_agent.storage import SqliteStore
from test_graph_service import service


def execute(service, view, text="one"):
    started = service.start(
        view["workflow_session_id"], expected_revision=view["revision"],
        idempotency_key=str(uuid4()), inputs={"text": text},
    )
    service.wait(started["active_chain_run_id"])
    final = service.get_session(view["workflow_session_id"])
    assert final["status"] == "succeeded"
    return final


def choose(service, view, candidate_id, *, fork=False, key=None):
    method = service.fork_graph_candidate if fork else service.select_graph_candidate
    return method(
        view["workflow_session_id"], candidate_id=candidate_id,
        expected_revision=view["revision"], expected_data_revision=view["data_revision"],
        expected_head_revision=view["head_revision"], idempotency_key=key or str(uuid4()),
    )


def records(service):
    with closing(SqliteStore(service.database)) as store:
        return store.read_bundle(include_graph=True)
