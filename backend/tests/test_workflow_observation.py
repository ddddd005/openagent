"""Public observations follow selected results, not previews or mutable parent heads."""

from contextlib import closing
import json
from threading import Event

from phase1_agent.workflow import A_BINDING, OfflineAdapter, WorkflowService
from test_workflow_context_view import database, fence, persistent_data


def execute(service, sid, text="request", key="first"):
    receipt = service.submit(sid, text, key)
    service.wait_for_idle(sid)
    return receipt


def test_empty_and_completed_observation_is_read_only_and_allowlisted(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        empty = service.get_session(sid)["observation"]
        assert empty["chain_run_id"] is None
        assert all(node["result"]["availability"] == "unavailable" for node in empty["nodes"])
        receipt = execute(service, sid)
        before = persistent_data(database)
        view = service.get_session(sid)
        observed = view["observation"]
        assert observed["session_revision"] == view["revision"]
        assert observed["head_commit_id"] == view["head_commit_id"]
        assert observed["chain_run_id"] == receipt["chain_run_id"]
        assert all(node["status"] == "succeeded" for node in observed["nodes"])
        assert all(node["result"]["availability"] == "available" for node in observed["nodes"])
        assert observed["nodes"][1]["result"] == observed["nodes"][2]["result"]
        assert observed["nodes"][0]["result"]["text"] != observed["nodes"][1]["result"]["text"]
        for node in observed["nodes"]:
            assert node["workflow_session_id"] == sid
            assert node["source_workflow_session_id"] == sid
            assert set(node["result"]) == {"availability", "text", "output_id", "source_run_id"}
        assert observed["nodes"][0]["budget"]["model_requests"] == 2
        assert persistent_data(database) == before
        assert not any(word in json.dumps(observed) for word in (
            "context_delta", "credential", "tool_call", "snapshot_id", "messages", "private_data",
        ))
    with closing(WorkflowService(database)) as service:
        assert service.get_session(sid)["observation"] == observed


def test_running_observation_never_reuses_previous_result(database):
    started, release = Event(), Event()

    class Gated(OfflineAdapter):
        def generate(self, messages, tools):
            started.set()
            assert release.wait(20)
            return super().generate(messages, tools)

    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        execute(service, sid)
        service._factory = lambda stage: Gated(stage)
        receipt = service.submit(sid, "next", "second")
        try:
            assert started.wait(20)
            observed = service.get_session(sid)["observation"]
            assert observed["chain_run_id"] == receipt["chain_run_id"]
            assert observed["nodes"][0]["status"] == "running"
            assert observed["nodes"][1]["run_id"] is None
            assert all(node["result"]["availability"] == "unavailable" for node in observed["nodes"])
        finally:
            release.set()
            service.wait_for_idle(sid)


def test_candidate_selection_changes_observation_to_formal_selected_run(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        first = execute(service, sid)
        original = service.get_session(sid)["observation"]
        service.reroll(sid, first["chain_run_id"], idempotency_key="reroll", **fence(service, sid))
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        old_candidate = next(row for row in view["messages"][-1]["reply_candidates"]
                             if row["chain_run_id"] == first["chain_run_id"])
        service.select_candidate(sid, old_candidate["candidate_id"], idempotency_key="select", **fence(service, sid))
        selected = service.get_session(sid)["observation"]
        assert selected["chain_run_id"] == first["chain_run_id"]
        assert selected["nodes"] == original["nodes"]


def test_fork_observation_uses_frozen_ancestor_result_and_explicit_source_owner(database):
    with closing(WorkflowService(database)) as service:
        parent = service.create_session()["workflow_session_id"]
        execute(service, parent)
        original = service.get_session(parent)
        child = service.create_branch(
            parent, original["messages"][-1]["visible_message_id"],
            expected_source_revision=original["revision"], idempotency_key="fork",
        )["workflow_session_id"]
        execute(service, parent, "parent future", "future")
        inherited = service.get_session(child)["observation"]
        assert inherited["chain_run_id"] == original["observation"]["chain_run_id"]
        assert all(node["workflow_session_id"] == child for node in inherited["nodes"])
        assert all(node["source_workflow_session_id"] == parent for node in inherited["nodes"])
        assert inherited["nodes"][0]["node_binding_id"] == A_BINDING
        assert inherited["nodes"][0]["result"] == original["observation"]["nodes"][0]["result"]
