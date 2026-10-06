"""Explicit selection of an archived, unpublished workflow candidate."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="workflow-explicit-selection-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def durable(database):
    with closing(SqliteStore(database)) as store:
        return validate_bundle(store.read_bundle())


def session_rows(bundle, kind, session_id):
    return [row for row in bundle.get(kind, [])
            if row["workflow_session_id"] == session_id]


def head(bundle, session_id):
    refs = session_rows(bundle, "workflow_ref", session_id)
    assert len(refs) == 1
    return refs[0]


def unpublished_candidate(database, monkeypatch):
    calls = []

    def factory(stage):
        calls.append(stage)
        return OfflineAdapter(stage)

    original_save = SqliteStore.save_bundle

    def fail_auto_selection(store, records, key, **kwargs):
        if kwargs.get("operation") == "workflow.select_candidate":
            raise RuntimeError("selection fault after candidate archive")
        return original_save(store, records, key, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(SqliteStore, "save_bundle", fail_auto_selection)
        with closing(WorkflowService(database, model_factory=factory)) as service:
            session_id = service.create_session()["workflow_session_id"]
            service.submit(session_id, "Archive before selecting", "submit")
            service.wait_for_idle(session_id)
            assert [row["role"] for row in service.get_session(session_id)["messages"]] == ["user"]
    bundle = durable(database)
    candidates = session_rows(bundle, "workflow_candidate", session_id)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["base_commit_id"] == head(bundle, session_id)["head_commit_id"]
    assert calls == ["A", "B"]
    return session_id, candidate, bundle


def select(service, session_id, candidate, bundle, *, key="explicit"):
    return service.select_candidate(
        session_id, candidate["candidate_id"], idempotency_key=key,
        expected_session_revision=session_rows(bundle, "workflow_session", session_id)[0]["revision"],
        expected_ref_revision=head(bundle, session_id)["revision"],
        expected_head_commit_id=head(bundle, session_id)["head_commit_id"],
    )


def test_explicit_selection_persists_projection_and_replays_after_reopen(database, monkeypatch):
    session_id, candidate, archived = unpublished_candidate(database, monkeypatch)
    before_runs = session_rows(archived, "run_record", session_id)
    before_deliveries = [row for row in archived["output_delivery"]
                         if row["output_id"] == candidate["output_id"]]
    assert len(before_deliveries) == 1 and before_deliveries[0]["status"] == "pending"

    def no_model(stage):
        pytest.fail(f"explicit selection redispatched {stage}")

    with closing(WorkflowService(database, model_factory=no_model)) as service:
        receipt = select(service, session_id, candidate, archived)
        assert receipt == {
            "workflow_session_id": session_id,
            "candidate_id": candidate["candidate_id"],
            "head_commit_id": candidate["result_commit_id"],
            "ref_revision": head(archived, session_id)["revision"] + 1,
            "session_revision": session_rows(archived, "workflow_session", session_id)[0]["revision"] + 1,
            "status": "succeeded",
        }
        assert select(service, session_id, candidate, archived) == receipt
        with pytest.raises(ContractValidationError, match="different workflow operation request"):
            service.select_candidate(
                session_id, str(uuid4()), idempotency_key="explicit",
                expected_session_revision=session_rows(archived, "workflow_session", session_id)[0]["revision"],
                expected_ref_revision=head(archived, session_id)["revision"],
                expected_head_commit_id=head(archived, session_id)["head_commit_id"],
            )
        assert [row["role"] for row in service.get_session(session_id)["messages"]] == [
            "user", "assistant",
        ]

    selected = durable(database)
    assert head(selected, session_id)["head_commit_id"] == candidate["result_commit_id"]
    assert session_rows(selected, "workflow_candidate", session_id) == [candidate]
    assert session_rows(selected, "run_record", session_id) == before_runs
    assert [row for row in selected["output_delivery"]
            if row["output_id"] == candidate["output_id"]][0]["status"] == "succeeded"
    assert len(session_rows(selected, "visible_message_ref", session_id)) == 2
    group_ids = {row["candidate_group_id"]
                 for row in session_rows(selected, "candidate_group", session_id)}
    assert len([row for row in selected["candidate_selection"]
                if row["candidate_group_id"] in group_ids]) == 2
    operations = [row for row in selected["workflow_operation"]
                  if row["kind"] == "select_candidate"
                  and row["scope"] == {"kind": "workflow_session", "id": session_id}]
    assert len(operations) == 1
    assert operations[0]["target"] == {"kind": "candidate", "id": candidate["candidate_id"]}
    assert operations[0]["payload"] == {
        "expected_session_revision": session_rows(archived, "workflow_session", session_id)[0]["revision"],
    }

    with closing(WorkflowService(database, model_factory=no_model)) as reopened:
        assert select(reopened, session_id, candidate, archived) == receipt
        assert [row["role"] for row in reopened.get_session(session_id)["messages"]] == [
            "user", "assistant",
        ]


def test_explicit_selection_rejects_stale_and_foreign_candidate_without_writes(database, monkeypatch):
    session_id, candidate, archived = unpublished_candidate(database, monkeypatch)
    with closing(WorkflowService(database, model_factory=lambda stage: pytest.fail(stage))) as service:
        other_id = service.create_session()["workflow_session_id"]
        baseline = durable(database)
        revision = session_rows(archived, "workflow_session", session_id)[0]["revision"]
        ref = head(archived, session_id)
        with pytest.raises(ContractValidationError, match="revision conflict"):
            service.select_candidate(
                session_id, candidate["candidate_id"], idempotency_key="stale-session",
                expected_session_revision=revision - 1,
                expected_ref_revision=ref["revision"],
                expected_head_commit_id=ref["head_commit_id"],
            )
        with pytest.raises(ContractValidationError, match="head conflict"):
            service.select_candidate(
                session_id, candidate["candidate_id"], idempotency_key="stale-head",
                expected_session_revision=revision,
                expected_ref_revision=ref["revision"],
                expected_head_commit_id=str(uuid4()),
            )
        with pytest.raises(ContractValidationError, match="head conflict"):
            service.select_candidate(
                session_id, candidate["candidate_id"], idempotency_key="stale-ref",
                expected_session_revision=revision,
                expected_ref_revision=ref["revision"] + 1,
                expected_head_commit_id=ref["head_commit_id"],
            )
        other_bundle = durable(database)
        with pytest.raises(ContractValidationError, match="another workflow session"):
            select(service, other_id, candidate, other_bundle, key="foreign")
        assert durable(database) == baseline

        select(service, session_id, candidate, archived, key="selected")
        selected = durable(database)
        with pytest.raises(ContractValidationError, match="current Head"):
            select(service, session_id, candidate, selected, key="select-again")
        assert durable(database) == selected


def test_explicit_selection_fault_rolls_back_operation_and_projection(database, monkeypatch):
    session_id, candidate, archived = unpublished_candidate(database, monkeypatch)
    with closing(WorkflowService(database, model_factory=lambda stage: pytest.fail(stage))) as service:
        def fail_commit(point):
            if point == "before_commit":
                raise RuntimeError("commit fault")

        service._fault_injector = fail_commit
        with pytest.raises(RuntimeError, match="commit fault"):
            select(service, session_id, candidate, archived, key="fault")
        service._fault_injector = None
        assert durable(database) == archived
        receipt = select(service, session_id, candidate, archived, key="fault")
        assert receipt["head_commit_id"] == candidate["result_commit_id"]
        assert select(service, session_id, candidate, archived, key="fault") == receipt


def test_explicit_selection_uses_transactional_head_cas(database, monkeypatch):
    session_id, candidate, archived = unpublished_candidate(database, monkeypatch)
    original_save = SqliteStore._save_bundle_transaction
    ref = head(archived, session_id)
    injected_heads = []

    def conflict_at_commit(store, records, key, **kwargs):
        if kwargs.get("operation") == "workflow.explicit_select_candidate":
            assert key == session_id + ":cas"
            assert kwargs["expected_ref_heads"] == {
                ref["workflow_ref_id"]: (ref["revision"], ref["head_commit_id"]),
            }
            assert kwargs["builder"] is not None
            assert not store._connection.in_transaction
            injected_heads.append(kwargs["expected_ref_heads"])
            kwargs = dict(kwargs)
            kwargs["expected_ref_heads"] = {
                ref_id: (revision, str(uuid4()))
                for ref_id, (revision, _) in kwargs["expected_ref_heads"].items()
            }
        return original_save(store, records, key, **kwargs)

    with closing(WorkflowService(database, model_factory=lambda stage: pytest.fail(stage))) as service:
        with monkeypatch.context() as conflict:
            conflict.setattr(SqliteStore, "_save_bundle_transaction", conflict_at_commit)
            with pytest.raises(ContractValidationError, match="head conflict"):
                select(service, session_id, candidate, archived, key="cas")
        assert len(injected_heads) == 1
        assert durable(database) == archived
        with closing(SqliteStore(database)) as store:
            assert store.read_receipt(
                "workflow.explicit_select_candidate", session_id + ":cas",
            ) is None
        receipt = select(service, session_id, candidate, archived, key="cas")
        assert receipt["status"] == "succeeded"
        assert receipt["head_commit_id"] == candidate["result_commit_id"]
        selected = durable(database)
        assert session_rows(selected, "run_record", session_id) == session_rows(
            archived, "run_record", session_id,
        )

    with closing(WorkflowService(database, model_factory=lambda stage: pytest.fail(stage))) as reopened:
        assert select(reopened, session_id, candidate, archived, key="cas") == receipt
        assert durable(database) == selected


def test_explicit_selection_clears_in_process_retry_projection(database, monkeypatch):
    original_save = SqliteStore.save_bundle

    def fail_auto_selection(store, records, key, **kwargs):
        if kwargs.get("operation") == "workflow.select_candidate":
            raise RuntimeError("selection fault after candidate archive")
        return original_save(store, records, key, **kwargs)

    with closing(WorkflowService(database)) as service:
        session_id = service.create_session()["workflow_session_id"]
        with monkeypatch.context() as failure:
            failure.setattr(SqliteStore, "save_bundle", fail_auto_selection)
            service.submit(session_id, "Select without reopening", "submit")
            service.wait_for_idle(session_id)
        archived = durable(database)
        candidate = session_rows(archived, "workflow_candidate", session_id)[0]
        assert service.get_session(session_id)["available_actions"] == ["retry_publish"]

        select(service, session_id, candidate, archived)
        view = service.get_session(session_id)
        assert view["available_actions"] == []
        assert view["error"] is None
        assert view["can_submit"]
