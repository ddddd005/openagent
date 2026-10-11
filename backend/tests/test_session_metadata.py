"""Formal session names do not edit execution state or depend on editor drafts."""

from contextlib import closing
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore
from test_graph_service import create, gate_graph, run, text_graph


@pytest.fixture
def service(tmp_path):
    database = tmp_path / "session-metadata.sqlite"
    with closing(GraphWorkflowService(database)) as instance:
        yield instance


def save_name(service, sid, name, sequence=0, key=None):
    return service.update_session_metadata(
        sid, name=name, expected_metadata_sequence=sequence,
        idempotency_key=key or str(uuid4()))


def test_name_defaults_and_saved_value_survive_a_fresh_service(tmp_path):
    database = tmp_path / "session-metadata.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        doc = text_graph(service.registry)
        sid = create(service, doc)["workflow_session_id"]
        assert service.get_session_metadata(sid) == {
            "schema_version": 1, "kind": "workflow.session-metadata",
            "workflow_definition_id": doc["workflow_definition_id"],
            "workflow_session_id": sid, "name": "", "metadata_sequence": 0}
        accepted = save_name(service, sid, "  Session one  ")
        assert accepted["name"] == "Session one" and accepted["metadata_sequence"] == 1
    with closing(GraphWorkflowService(database)) as restarted:
        assert restarted.get_session_metadata(sid) == accepted
        assert restarted.get_session(sid)["name"] == "Session one"
        assert restarted.list_sessions(doc["workflow_definition_id"])[0]["metadata_sequence"] == 1
    with closing(SqliteStore(database)) as store:
        assert store._connection.execute("SELECT COUNT(*) FROM local_editor_drafts").fetchone()[0] == 0


def test_name_changes_only_metadata_and_is_object_scoped(service):
    doc = text_graph(service.registry)
    original = run(service, create(service, doc))
    sid = original["workflow_session_id"]
    sibling = service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    before_run = service.get_run(sid, original["chains"][0]["chain_run_id"])
    save_name(service, sid, "Target")
    renamed = service.get_session(sid)
    assert {key: value for key, value in renamed.items() if key not in ("name", "metadata_sequence")} == {
        key: value for key, value in original.items() if key not in ("name", "metadata_sequence")}
    assert service.get_session(sibling["workflow_session_id"]) == sibling
    assert service.get_run(sid, original["chains"][0]["chain_run_id"]) == before_run


def test_name_can_change_while_a_run_is_active_without_stopping_it(service):
    sid, chain_id, release = gate_graph(service)
    before = service.get_session(sid)
    try:
        accepted = save_name(service, sid, "Running")
        after = service.get_session(sid)
        assert accepted["name"] == "Running"
        assert after["active_chain_run_id"] == chain_id
        assert after["revision"] == before["revision"]
        assert after["head_revision"] == before["head_revision"]
    finally:
        release.set()
        service.wait(chain_id)


def test_cas_and_replay_use_the_original_request(service):
    view = create(service, text_graph(service.registry))
    sid, key = view["workflow_session_id"], str(uuid4())
    first = save_name(service, sid, "First", key=key)
    save_name(service, sid, "Second", sequence=1)
    assert save_name(service, sid, "First", key=key) == first
    assert service.get_session_metadata(sid)["name"] == "Second"
    with pytest.raises(ContractValidationError) as conflict:
        save_name(service, sid, "Different", key=key)
    assert conflict.value.reason_code == "idempotency_conflict"
    with pytest.raises(ContractValidationError) as stale:
        save_name(service, sid, "Stale")
    assert stale.value.reason_code == "stale_metadata_sequence"
    assert service.get_session_metadata(sid)["metadata_sequence"] == 2


def test_failed_metadata_transaction_does_not_report_or_preserve_a_success(service):
    view = create(service, text_graph(service.registry))
    sid = view["workflow_session_id"]

    def fail(point):
        if point == "before_commit":
            raise RuntimeError("metadata write failed")

    service._fault_injector = fail
    try:
        with pytest.raises(RuntimeError, match="metadata write failed"):
            save_name(service, sid, "Unsaved")
    finally:
        service._fault_injector = None
    assert service.get_session_metadata(sid)["metadata_sequence"] == 0
    assert service.get_session(sid)["name"] == ""


@pytest.mark.parametrize("name,sequence", [
    (None, 0), ("x" * 129, 0), ("x\n", 0), ("x\x7f", 0), ("\ud800", 0), ("x", True), ("x", -1)])
def test_invalid_names_or_sequences_never_write(service, name, sequence):
    view = create(service, text_graph(service.registry))
    with pytest.raises(ContractValidationError) as invalid:
        save_name(service, view["workflow_session_id"], name, sequence)
    assert invalid.value.reason_code == "invalid_request"
    assert service.get_session_metadata(view["workflow_session_id"])["metadata_sequence"] == 0


def test_archived_session_is_readable_and_its_name_is_readonly(service):
    view = create(service, text_graph(service.registry))
    sid = view["workflow_session_id"]
    save_name(service, sid, "Archived")
    service.archive_session(sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
    assert service.get_session_metadata(sid)["name"] == "Archived"
    with pytest.raises(ContractValidationError) as denied:
        save_name(service, sid, "Cannot change", sequence=1)
    assert denied.value.reason_code == "session_archived"


def test_archived_workflow_makes_session_names_readonly(service):
    doc = text_graph(service.registry)
    sid = create(service, doc)["workflow_session_id"]
    service.archive_definition(doc["workflow_definition_id"], expected_revision=1, idempotency_key=str(uuid4()))
    with pytest.raises(ContractValidationError) as denied:
        save_name(service, sid, "Cannot change")
    assert denied.value.reason_code == "workflow_archived"


def test_blank_and_full_length_unicode_names_are_formal_metadata(service):
    sid = create(service, text_graph(service.registry))["workflow_session_id"]
    assert save_name(service, sid, "\U0001f642" * 128)["name"] == "\U0001f642" * 128
    assert save_name(service, sid, "   ", sequence=1)["name"] == ""
    assert service.get_session_metadata(sid)["metadata_sequence"] == 2


def test_deletion_removes_only_the_target_metadata_and_receipt_does_not_restore_it(service):
    doc = text_graph(service.registry)
    first = create(service, doc)
    second = service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    sid, key = first["workflow_session_id"], str(uuid4())
    original = save_name(service, sid, "Delete", key=key)
    save_name(service, second["workflow_session_id"], "Keep")
    service.delete_session(sid, expected_revision=first["revision"], idempotency_key=str(uuid4()))
    assert save_name(service, sid, "Delete", key=key) == original
    assert service.get_session_metadata(second["workflow_session_id"])["name"] == "Keep"
    with pytest.raises(ContractValidationError) as missing:
        service.get_session_metadata(sid)
    assert missing.value.reason_code == "not_found"


def test_application_metadata_command_returns_a_matching_durable_receipt(service):
    view = create(service, text_graph(service.registry))
    application = GraphApplication(service)
    request = {"session_id": view["workflow_session_id"], "name": "Formal",
               "expected_metadata_sequence": 0, "idempotency_key": str(uuid4())}
    accepted = application.command("session.metadata.update", request)
    assert accepted["receipt"]["accepted"]["name"] == "Formal"
    assert accepted["receipt"]["accepted"]["metadata_sequence"] == 1
    assert application.command("session.metadata.update", request) == accepted
    assert application.query("session.metadata.read", {"session_id": request["session_id"]}) == accepted["result"]
    receipt = application.query("receipt.read", {"operation": "session.metadata.update", "parameters": request})
    assert receipt["outcome"] == "matched"
    assert receipt["receipt"] == accepted["receipt"]
    assert receipt["result"] == accepted["result"]
