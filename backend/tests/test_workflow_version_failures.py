"""Real-service failure boundaries for archived workflow candidates and Head selection."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
import phase1_agent.workflow as workflow_module
from phase1_agent.workflow import B_BINDING, OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="workflow-version-failures-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def durable(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def session_rows(bundle, kind, session_id):
    return [row for row in bundle.get(kind, [])
            if row["workflow_session_id"] == session_id]


def current_head(bundle, session_id):
    refs = session_rows(bundle, "workflow_ref", session_id)
    assert len(refs) == 1
    return refs[0]


def b_run_id(bundle, session_id):
    runs = [row for row in session_rows(bundle, "run_record", session_id)
            if row["node_binding_id"] == B_BINDING]
    assert len(runs) == 1
    return runs[0]["run_id"]


def count_dispatch(monkeypatch):
    models, tools = [], []
    original_register = workflow_module.register_callable

    def counted_register(name, description, schema, callback):
        if name == "inspect_text":
            implementation = callback

            def counted_tool(text):
                tools.append(text)
                return implementation(text)

            callback = counted_tool
        return original_register(name, description, schema, callback)

    monkeypatch.setattr(workflow_module, "register_callable", counted_register)

    def factory(stage):
        models.append(stage)
        return OfflineAdapter(stage)

    return factory, models, tools


def fail_once_at(monkeypatch, operation, mode):
    original_save = SqliteStore._save_bundle_transaction
    attempts = []

    def interrupted_save(store, records, key, **kwargs):
        if kwargs.get("operation") != operation or attempts:
            return original_save(store, records, key, **kwargs)
        attempts.append(key)
        if mode == "head_conflict":
            kwargs = dict(kwargs)
            kwargs["expected_ref_heads"] = {
                ref_id: (revision, str(uuid4()))
                for ref_id, (revision, _) in kwargs["expected_ref_heads"].items()
            }
            return original_save(store, records, key, **kwargs)

        previous_injector = store._fault_injector

        def before_commit(point):
            if previous_injector is not None:
                previous_injector(point)
            if point == "before_commit":
                raise RuntimeError("injected candidate boundary failure")

        store._fault_injector = before_commit
        try:
            return original_save(store, records, key, **kwargs)
        finally:
            store._fault_injector = previous_injector

    monkeypatch.setattr(SqliteStore, "_save_bundle_transaction", interrupted_save)
    return attempts


def test_candidate_archive_commit_fault_rolls_back_then_in_process_retry(database, monkeypatch):
    factory, models, tools = count_dispatch(monkeypatch)
    with closing(WorkflowService(database, model_factory=factory)) as service:
        session_id = service.create_session()["workflow_session_id"]
        original_head = current_head(durable(database), session_id)
        with monkeypatch.context() as failure:
            attempts = fail_once_at(failure, "workflow.archive_candidate", "before_commit")
            service.submit(session_id, "Archive transaction fault", "submit")
            service.wait_for_idle(session_id)
        assert len(attempts) == 1
        failed = durable(database)
        assert current_head(failed, session_id) == original_head
        assert session_rows(failed, "workflow_candidate", session_id) == []
        assert len(session_rows(failed, "workflow_commit", session_id)) == 1
        assert len(session_rows(failed, "state_snapshot", session_id)) == 1
        assert len(session_rows(failed, "workflow_output", session_id)) == 2
        assert [row["role"] for row in service.get_session(session_id)["messages"]] == ["user"]
        assert models == ["A", "B"]
        assert len(tools) == 2

        run_id = b_run_id(failed, session_id)
        revision = service.get_session(session_id)["revision"]
        result = service.retry_publish(
            session_id, run_id, idempotency_key="retry-archive-candidate",
            expected_session_revision=revision,
        )
        assert result["status"] == "succeeded"
        assert models == ["A", "B"]
        assert len(tools) == 2
        completed = durable(database)
        assert len(session_rows(completed, "workflow_candidate", session_id)) == 1
        assert current_head(completed, session_id)["revision"] == original_head["revision"] + 1
        assert [row["role"] for row in service.get_session(session_id)["messages"]] == [
            "user", "assistant",
        ]
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"redispatched {stage} after reopen"),
    )) as reopened:
        assert [row["role"] for row in reopened.get_session(session_id)["messages"]] == [
            "user", "assistant",
        ]


def test_archive_fault_after_restart_does_not_invent_a_candidate(database, monkeypatch):
    factory, models, tools = count_dispatch(monkeypatch)
    with closing(WorkflowService(database, model_factory=factory)) as service:
        session_id = service.create_session()["workflow_session_id"]
        original_head = current_head(durable(database), session_id)
        with monkeypatch.context() as failure:
            fail_once_at(failure, "workflow.archive_candidate", "before_commit")
            service.submit(session_id, "Unarchived result cannot restart", "submit")
            service.wait_for_idle(session_id)
        failed = durable(database)
        run_id = b_run_id(failed, session_id)
        assert session_rows(failed, "workflow_candidate", session_id) == []
        assert models == ["A", "B"]
        assert len(tools) == 2
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"redispatched {stage} after restart"),
    )) as reopened:
        view = reopened.get_session(session_id)
        assert view["error"]["code"] == "RECOVERY_UNAVAILABLE"
        with pytest.raises(ContractValidationError, match="unpublished completed output"):
            reopened.retry_publish(
                session_id, run_id, idempotency_key="restart-no-result",
                expected_session_revision=view["revision"],
            )
        after = durable(database)
        assert current_head(after, session_id) == original_head
        assert session_rows(after, "workflow_candidate", session_id) == []
        assert [row["role"] for row in view["messages"]] == ["user"]
        assert len(tools) == 2


@pytest.mark.parametrize("mode", ["before_commit", "head_conflict"])
def test_archived_candidate_survives_selection_failure_and_reopens_for_retry(
    database, monkeypatch, mode,
):
    factory, models, tools = count_dispatch(monkeypatch)
    with closing(WorkflowService(database, model_factory=factory)) as service:
        session_id = service.create_session()["workflow_session_id"]
        original_head = current_head(durable(database), session_id)
        with monkeypatch.context() as failure:
            attempts = fail_once_at(failure, "workflow.select_candidate", mode)
            service.submit(session_id, f"Selection {mode}", "submit")
            service.wait_for_idle(session_id)
        assert len(attempts) == 1
        archived = durable(database)
        candidates = session_rows(archived, "workflow_candidate", session_id)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert current_head(archived, session_id) == original_head
        assert any(row["commit_id"] == candidate["result_commit_id"]
                   for row in session_rows(archived, "workflow_commit", session_id))
        assert len(session_rows(archived, "workflow_commit", session_id)) == 2
        assert [row["role"] for row in service.get_session(session_id)["messages"]] == ["user"]
        assert models == ["A", "B"]
        assert len(tools) == 2
        run_id = b_run_id(archived, session_id)
    with closing(WorkflowService(
        database, model_factory=lambda stage: pytest.fail(f"redispatched {stage} during retry"),
    )) as reopened:
        view = reopened.get_session(session_id)
        assert current_head(durable(database), session_id) == original_head
        result = reopened.retry_publish(
            session_id, run_id, idempotency_key="retry-selection",
            expected_session_revision=view["revision"],
        )
        assert result["status"] == "succeeded"
        assert reopened.retry_publish(
            session_id, run_id, idempotency_key="retry-selection",
            expected_session_revision=view["revision"],
        ) == result
        assert len(tools) == 2
        selected = durable(database)
        assert session_rows(selected, "workflow_candidate", session_id) == [candidate]
        assert current_head(selected, session_id)["head_commit_id"] == candidate["result_commit_id"]
        assert current_head(selected, session_id)["revision"] == original_head["revision"] + 1
        assert [row["role"] for row in reopened.get_session(session_id)["messages"]] == [
            "user", "assistant",
        ]
