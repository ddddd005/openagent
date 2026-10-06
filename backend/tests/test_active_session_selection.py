"""Durable active-session selection is independent of a session's Head."""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import ACTIVE_SESSION_SELECTION_ID, SqliteStore
from phase1_agent.workflow import OfflineAdapter, WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="active-session-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def selection(database):
    with closing(SqliteStore(database)) as store:
        bundle = validate_bundle(store.read_bundle())
        rows = bundle["session_selection"]
        assert len(rows) == 1
        assert rows[0]["session_selection_id"] == ACTIVE_SESSION_SELECTION_ID
        return rows[0], bundle


def test_first_session_selects_once_and_explicit_switch_survives_restart(database):
    with closing(WorkflowService(database, model_factory=OfflineAdapter)) as service:
        source = service.create_session()["workflow_session_id"]
        assert service.get_active_session() == {
            "active_workflow_session_id": source, "revision": 1,
        }
        child = service.create_session()["workflow_session_id"]
        assert service.get_active_session()["active_workflow_session_id"] == source
        first, _ = selection(database)
        result = service.switch_session(
            child, idempotency_key="choose-child", expected_selection_revision=1,
        )
        assert result["active_workflow_session_id"] == child
        assert result["revision"] == 2
        assert service.switch_session(
            child, idempotency_key="choose-child", expected_selection_revision=1,
        ) == result
        with pytest.raises(ContractValidationError):
            service.switch_session(
                source, idempotency_key="choose-child", expected_selection_revision=1,
            )
        with pytest.raises(ContractValidationError):
            service.switch_session(
                source, idempotency_key="stale", expected_selection_revision=1,
            )
        assert service.get_active_session() == result
        latest, bundle = selection(database)
        assert latest["revision"] == first["revision"] + 1
        assert latest["active_workflow_session_id"] == child
        switch = [row for row in bundle["workflow_operation"] if row["kind"] == "switch_session"]
        assert len(switch) == 1
        assert switch[0]["scope"] == {
            "kind": "session_selection", "id": ACTIVE_SESSION_SELECTION_ID,
        }
        assert switch[0]["target"] == {"kind": "workflow_session", "id": child}
    with closing(WorkflowService(database, model_factory=OfflineAdapter)) as reopened:
        assert reopened.get_active_session() == result


def test_branch_create_and_switch_commits_pointer_with_child(database):
    with closing(WorkflowService(database, model_factory=OfflineAdapter)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Fork this", "input")
        service.wait_for_idle(source)
        view = service.get_session(source)
        assistant = view["messages"][-1]["visible_message_id"]
        original = service.get_active_session()
        branch = service.create_branch(
            source, assistant, idempotency_key="branch",
            expected_source_revision=view["revision"],
        )
        assert service.get_active_session() == original
        source_revision = service.get_session(source)["revision"]
        with pytest.raises(ContractValidationError):
            service.create_and_switch_branch(
                source, assistant, idempotency_key="stale-pointer",
                expected_source_revision=source_revision,
                expected_selection_revision=original["revision"] + 1,
            )
        assert len(service.list_sessions()) == 2
        switched = service.create_and_switch_branch(
            source, assistant, idempotency_key="branch-switch",
            expected_source_revision=source_revision,
            expected_selection_revision=original["revision"],
        )
        assert switched["workflow_session_id"] != branch["workflow_session_id"]
        assert switched["active_workflow_session_id"] == switched["workflow_session_id"]
        assert switched["selection_revision"] == original["revision"] + 1
        assert service.get_active_session() == {
            "active_workflow_session_id": switched["workflow_session_id"],
            "revision": switched["selection_revision"],
        }
        assert service.create_and_switch_branch(
            source, assistant, idempotency_key="branch-switch",
            expected_source_revision=source_revision,
            expected_selection_revision=original["revision"],
        ) == switched
        assert len(service.list_sessions()) == 3
        latest, bundle = selection(database)
        assert latest["active_workflow_session_id"] == switched["workflow_session_id"]
        assert any(row["kind"] == "create_and_switch_branch"
                   for row in bundle["workflow_operation"])


def test_plain_branch_receipt_freezes_actual_active_session(database):
    with closing(WorkflowService(database, model_factory=OfflineAdapter)) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Independent pointer", "input")
        service.wait_for_idle(source)
        other = service.create_session()["workflow_session_id"]
        service.switch_session(
            other, idempotency_key="choose-other", expected_selection_revision=1,
        )
        source_view = service.get_session(source)
        assistant = source_view["messages"][-1]["visible_message_id"]
        branch = service.create_branch(
            source, assistant, idempotency_key="plain",
            expected_source_revision=source_view["revision"],
        )
        assert branch["active_workflow_session_id"] == other
        assert service.get_active_session()["active_workflow_session_id"] == other
        service.switch_session(
            source, idempotency_key="choose-source", expected_selection_revision=2,
        )
        assert service.create_branch(
            source, assistant, idempotency_key="plain",
            expected_source_revision=source_view["revision"],
        ) == branch


def test_failed_branch_switch_rolls_back_child_and_selection(database):
    fail = False

    def inject(point):
        if fail and point == "before_commit":
            raise RuntimeError("simulated transaction failure")

    with closing(WorkflowService(
        database, model_factory=OfflineAdapter, fault_injector=inject,
    )) as service:
        source = service.create_session()["workflow_session_id"]
        service.submit(source, "Atomic fork", "input")
        service.wait_for_idle(source)
        view = service.get_session(source)
        active = service.get_active_session()
        fail = True
        with pytest.raises(RuntimeError, match="simulated"):
            service.create_and_switch_branch(
                source, view["messages"][-1]["visible_message_id"],
                idempotency_key="atomic", expected_source_revision=view["revision"],
                expected_selection_revision=active["revision"],
            )
        fail = False
        assert service.get_active_session() == active
        assert len(service.list_sessions()) == 1
        assert service.get_session(source)["revision"] == view["revision"]


def test_existing_database_without_selection_is_seeded_once_on_reopen(database):
    with closing(WorkflowService(database, model_factory=OfflineAdapter)) as service:
        service.create_session()
        newest = service.create_session()["workflow_session_id"]
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "DELETE FROM records WHERE record_type = 'session_selection'"
        )
        connection.commit()
    with closing(WorkflowService(database, model_factory=OfflineAdapter)) as reopened:
        assert reopened.get_active_session() == {
            "active_workflow_session_id": newest, "revision": 1,
        }
        reopened.create_session()
        assert reopened.get_active_session()["active_workflow_session_id"] == newest
