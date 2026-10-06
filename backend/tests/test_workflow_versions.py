"""Real WorkflowService state-version history over the offline fixed workflow."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import httpx
import pytest

from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, OUTPUT_BINDING, OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="workflow-versions-", dir=Path(__file__).resolve().parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def read_bundle(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def in_session(bundle, kind, session_id):
    return [
        row for row in bundle.get(kind, [])
        if row["workflow_session_id"] == session_id
    ]


def head(bundle, session_id):
    refs = in_session(bundle, "workflow_ref", session_id)
    assert len(refs) == 1
    return refs[0]


def commit(bundle, commit_id):
    return next(row for row in bundle["workflow_commit"] if row["commit_id"] == commit_id)


def snapshot(bundle, commit_row):
    return next(
        row for row in bundle["state_snapshot"]
        if row["state_snapshot_id"] == commit_row["state_snapshot_id"]
    )


def operation(bundle, commit_row):
    return next(
        row for row in bundle["workflow_operation"]
        if row["operation_id"] == commit_row["operation_id"]
    )


def seed(bundle, session_id):
    seeds = [
        row for row in in_session(bundle, "workflow_commit", session_id)
        if row["source"] == {"kind": "session_seed", "workflow_session_id": session_id}
    ]
    assert len(seeds) == 1
    return seeds[0]


def assert_node_projection(state, checkpoint, *, output_id=None):
    checkpoint_nodes = {node["node_binding_id"]: node for node in checkpoint["nodes"]}
    state_nodes = {node["node_binding_id"]: node for node in state["node_states"]}
    assert set(state_nodes) == set(checkpoint_nodes) == {A_BINDING, B_BINDING, OUTPUT_BINDING}
    for binding, frozen in checkpoint_nodes.items():
        node = state_nodes[binding]
        assert node["data_version"] == frozen["data_version"]
        assert node["private_data"] == frozen["private_data"]
        if frozen["selected_turn_id"] is not None:
            assert node["selected_result"] == {
                "kind": "agent_turn", "turn_id": frozen["selected_turn_id"],
            }
        elif binding == OUTPUT_BINDING and output_id is not None:
            assert node["selected_result"] == {"kind": "node_output", "output_id": output_id}
        else:
            assert node["selected_result"] is None


def assert_visible_projection(state, bundle, session_id):
    refs = sorted(
        in_session(bundle, "visible_message_ref", session_id),
        key=lambda row: row["sequence"],
    )
    assert state["visible_message_refs"] == [
        {
            "visible_message_id": row["visible_message_id"],
            "sequence": row["sequence"],
            "role": row["role"],
            "boundary": row["boundary"],
        }
        for row in refs
    ]


def test_create_session_persists_a_root_seed_head_and_operation(database):
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"unexpected {stage} model"),
    )) as service:
        session_id = service.create_session()["workflow_session_id"]
        bundle = read_bundle(database)
        root = seed(bundle, session_id)
        ref = head(bundle, session_id)
        assert ref["head_commit_id"] == root["commit_id"]
        assert ref["revision"] == 1
        assert root["parent_commit_id"] is None
        assert root["workflow_session_id"] == session_id
        assert operation(bundle, root)["kind"] == "initialize_session"
        assert operation(bundle, root)["scope"] == {
            "kind": "workflow_session", "id": session_id,
        }
        assert operation(bundle, root)["target"] == {
            "kind": "workflow_session", "id": session_id,
        }
        assert operation(bundle, root)["expected_revisions"] == []
        initial = next(
            row for row in in_session(bundle, "workflow_checkpoint", session_id)
            if row["kind"] == "initial"
        )
        state = snapshot(bundle, root)
        assert_node_projection(state, initial)
        assert state["selection_refs"] == initial["selection_refs"] == []
        assert state["visible_message_refs"] == []
        assert state["pending_input_id"] is None
        assert len(in_session(bundle, "workflow_commit", session_id)) == 1
        assert len(in_session(bundle, "state_snapshot", session_id)) == 1


def test_successful_submit_persists_candidate_result_and_head_projection(database):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        session_id = service.create_session()["workflow_session_id"]
        original = read_bundle(database)
        root = seed(original, session_id)
        root_ref = head(original, session_id)
        receipt = service.submit(session_id, "Version this request", "once")
        service.wait_for_idle(session_id)
        view = service.get_session(session_id)
        assert view["error"] is None
        assert [row["role"] for row in view["messages"]] == ["user", "assistant"]
        assert calls == ["A", "B"]

        bundle = read_bundle(database)
        candidates = in_session(bundle, "workflow_candidate", session_id)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate["chain_run_id"] == receipt["chain_run_id"]
        chain = in_session(bundle, "chain_run", session_id)[0]
        assert chain["status"] == "succeeded"
        assert chain["schema_version"] == 2
        assert chain["base_commit_id"] == root["commit_id"]
        assert candidate["output_id"] == chain["output_id"]
        assert candidate["base_commit_id"] == root["commit_id"]
        result = commit(bundle, candidate["result_commit_id"])
        assert chain["operation_id"] == result["operation_id"]
        assert result["parent_commit_id"] == root["commit_id"]
        assert result["source"] == {
            "kind": "candidate", "candidate_id": candidate["candidate_id"],
        }
        assert operation(bundle, result)["kind"] == "submit"
        assert head(bundle, session_id)["head_commit_id"] == result["commit_id"]
        assert head(bundle, session_id)["revision"] == root_ref["revision"] + 1
        completed = next(
            row for row in in_session(bundle, "workflow_checkpoint", session_id)
            if row["checkpoint_id"] == candidate["checkpoint_id"]
        )
        assert completed["kind"] == "completed_output"
        assert completed["output_id"] == candidate["output_id"]
        state = snapshot(bundle, result)
        assert_node_projection(state, completed, output_id=candidate["output_id"])
        assert state["selection_refs"] == completed["selection_refs"]
        assert_visible_projection(state, bundle, session_id)
        assert state["pending_input_id"] is None
        assert len(in_session(bundle, "workflow_commit", session_id)) == 2
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"reopen redispatched {stage}"),
    )) as reopened:
        assert [row["role"] for row in reopened.get_session(session_id)["messages"]] == [
            "user", "assistant",
        ]
        persisted = read_bundle(database)
        assert head(persisted, session_id)["head_commit_id"] == result["commit_id"]
        assert snapshot(persisted, commit(persisted, result["commit_id"])) == state
        assert in_session(persisted, "workflow_candidate", session_id) == [candidate]


def test_failed_b_does_not_advance_head_or_create_complete_candidate(database):
    calls = []

    class FailedAdapter:
        def generate(self, messages, tools):
            request = httpx.Request("POST", "https://offline.invalid/chat/completions")
            raise httpx.HTTPStatusError(
                "offline B failure", request=request,
                response=httpx.Response(400, request=request),
            )

    def factory(stage):
        calls.append(stage)
        return FailedAdapter() if stage == "B" else OfflineAdapter(stage)

    with closing(WorkflowService(database, model_factory=factory)) as service:
        session_id = service.create_session()["workflow_session_id"]
        root = seed(read_bundle(database), session_id)
        root_ref = head(read_bundle(database), session_id)
        service.submit(session_id, "B should fail", "once")
        service.wait_for_idle(session_id)
        assert calls == ["A", "B"]
        assert service.get_session(session_id)["error"]["code"] == "model_error"
        bundle = read_bundle(database)
        chain = in_session(bundle, "chain_run", session_id)[0]
        assert chain["schema_version"] == 2
        assert chain["base_commit_id"] == root["commit_id"]
        assert head(bundle, session_id) == root_ref
        assert in_session(bundle, "workflow_candidate", session_id) == []
        assert in_session(bundle, "workflow_commit", session_id) == [root]
        assert len(in_session(bundle, "state_snapshot", session_id)) == 1
        assert [row["role"] for row in service.get_session(session_id)["messages"]] == ["user"]
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"reopen redispatched {stage}"),
    )) as reopened:
        assert reopened.get_session(session_id)["error"]["code"] == "RECOVERY_UNAVAILABLE"
        assert head(read_bundle(database), session_id) == root_ref


def test_fork_seed_uses_stable_source_commit_and_pending_start_waits_for_result(database):
    started, release = Event(), Event()
    block_child = False

    class PausableAdapter(OfflineAdapter):
        def generate(self, messages, tools):
            if block_child and self.stage == "A":
                started.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with closing(WorkflowService(database, model_factory=PausableAdapter)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Branch both anchors", "source")
        service.wait_for_idle(source)
        source_view = service.get_session(source)
        assert source_view["error"] is None
        first, latest = [row["visible_message_id"] for row in source_view["messages"]]
        original = read_bundle(database)
        source_root = seed(original, source)
        source_head = head(original, source)
        source_result = commit(original, source_head["head_commit_id"])

        assistant_branch = service.create_branch(
            source, latest, idempotency_key="assistant",
            expected_source_revision=source_view["revision"],
        )
        assistant_child = assistant_branch["workflow_session_id"]
        bundle = read_bundle(database)
        assistant_seed = seed(bundle, assistant_child)
        assert assistant_seed["parent_commit_id"] == source_result["commit_id"]
        assert operation(bundle, assistant_seed)["kind"] == "create_branch"
        assert operation(bundle, assistant_seed)["payload"]["requested_action"] == "create_branch"
        assert head(bundle, assistant_child)["head_commit_id"] == assistant_seed["commit_id"]
        assistant_state = snapshot(bundle, assistant_seed)
        assert [ref["role"] for ref in assistant_state["visible_message_refs"]] == [
            "user", "assistant",
        ]
        assert in_session(bundle, "chain_run", assistant_child) == []
        assert head(bundle, source) == source_head

        user_branch = service.create_and_switch_branch(
            source, first, idempotency_key="user",
            expected_source_revision=service.get_session(source)["revision"],
        )
        child = user_branch["workflow_session_id"]
        pending_input_id = user_branch["pending_input_id"]
        bundle = read_bundle(database)
        child_seed = seed(bundle, child)
        assert child_seed["parent_commit_id"] == source_root["commit_id"]
        assert operation(bundle, child_seed)["kind"] == "create_branch"
        assert operation(bundle, child_seed)["payload"]["requested_action"] == "create_and_switch_branch"
        child_ref = head(bundle, child)
        assert child_ref["head_commit_id"] == child_seed["commit_id"]
        state = snapshot(bundle, child_seed)
        assert state["pending_input_id"] == pending_input_id
        assert [ref["role"] for ref in state["visible_message_refs"]] == ["user"]
        assert state["visible_message_refs"][0]["boundary"]["input_status"] == "pending"
        assert head(bundle, source) == source_head

        try:
            block_child = True
            continued = service.continue_pending_input(
                child, pending_input_id, idempotency_key="first-start",
                expected_session_revision=service.get_session(child)["revision"],
            )
            assert started.wait(10)
            running = read_bundle(database)
            assert head(running, child) == child_ref
            assert in_session(running, "workflow_candidate", child) == []
            running_chain = in_session(running, "chain_run", child)[0]
            assert running_chain["chain_run_id"] == continued["chain_run_id"]
            assert running_chain["schema_version"] == 2
            assert running_chain["base_commit_id"] == child_seed["commit_id"]
        finally:
            release.set()
        service.wait_for_idle(child)
        assert service.get_session(child)["error"] is None
        completed = read_bundle(database)
        candidate = in_session(completed, "workflow_candidate", child)
        assert len(candidate) == 1
        assert candidate[0]["base_commit_id"] == child_seed["commit_id"]
        result = commit(completed, candidate[0]["result_commit_id"])
        assert running_chain["operation_id"] == result["operation_id"]
        assert result["parent_commit_id"] == child_seed["commit_id"]
        assert operation(completed, result)["kind"] == "continue_pending_input"
        assert head(completed, child)["head_commit_id"] == result["commit_id"]
        assert head(completed, child)["revision"] == child_ref["revision"] + 1
        assert snapshot(completed, result)["pending_input_id"] is None
        assert [ref["role"] for ref in snapshot(completed, result)["visible_message_refs"]] == [
            "user", "assistant",
        ]
        assert head(completed, source) == source_head
