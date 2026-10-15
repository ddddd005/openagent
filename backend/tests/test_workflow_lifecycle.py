"""Object-scoped management without model calls or live HTTP services."""

from contextlib import closing
from copy import deepcopy
from threading import Thread
from uuid import UUID, uuid4

import pytest

from phase1_agent.checkpoint_repository import CheckpointRepository, initialize_checkpoint_tables
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_records import graph_record
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.runtime_checkpoints import CHECKPOINT_SCHEMA_VERSION
from phase1_agent.storage import SqliteStore
from test_graph_service import create, document, gate_graph, run, save_definition_fixture, text_graph


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "workflow.sqlite")) as instance:
        yield instance


def _save_configuration(service, document, *, key=None):
    proposed = deepcopy(document)
    proposed["schema_version"] = 3
    proposed["package_lock"] = list(service.registry.execution_package_lock)
    for node in proposed["nodes"]:
        node["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    return service.update_definition(
        proposed, workflow_definition_id=proposed["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=key or str(uuid4()))


def _checkpoint_root(workflow_id, session_id, fixed_revision=1):
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": str(uuid4()),
        "workflow_definition_id": workflow_id,
        "workflow_session_id": session_id,
        "fixed_definition_revision": fixed_revision,
        "parent_checkpoint_id": None,
        "state_storage": {
            "mode": "full",
            "state": {
                "configuration": {
                    "workflow_definition_id": workflow_id,
                    "fixed_definition_revision": fixed_revision,
                    "nodes": {},
                },
                "agent_contexts": {},
                "variables": {},
                "chat_contexts": {},
            },
        },
    }


def _seed_checkpoint_roots(service, sessions):
    with closing(SqliteStore(service.database)) as store:
        connection = store._connection
        connection.execute("BEGIN IMMEDIATE")
        initialize_checkpoint_tables(connection)
        checkpoints = []
        try:
            for session in sessions:
                repository = CheckpointRepository(
                    connection,
                    workflow_definition_id=session["workflow_definition_id"],
                    workflow_session_id=session["workflow_session_id"],
                    fixed_definition_revision=session["definition_revision"],
                )
                checkpoint = _checkpoint_root(
                    session["workflow_definition_id"],
                    session["workflow_session_id"],
                    session["definition_revision"],
                )
                repository.insert(checkpoint)
                checkpoints.append(checkpoint)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    return checkpoints


def test_saved_workflow_has_no_session_and_backend_directory_includes_it(service):
    doc = text_graph(service.registry)
    save_definition_fixture(service, doc)
    assert service.list_sessions(doc["workflow_definition_id"]) == []
    assert service.list_definitions() == [{"document": doc, "archived": False}]
    assert dispatch_graph(service, "GET", "/api/graph/definitions") == (200, service.list_definitions())


def test_session_archive_keeps_history_but_cannot_run_edit_or_restore(service):
    view = run(service, create(service, text_graph(service.registry)))
    sid = view["workflow_session_id"]
    request = {"session_id": sid, "expected_revision": view["revision"], "idempotency_key": str(uuid4())}
    application = GraphApplication(service)
    accepted = application.command("session.archive", request)
    assert accepted["result"]["archived"] is True
    archived = service.get_session(sid)
    assert archived["outputs"] == view["outputs"]
    assert archived["archived"] and archived["readonly"]
    assert not archived["can_submit"] and archived["available_actions"] == []
    assert archived["history_refs"] == view["history_refs"]
    assert application.command("session.archive", request) == accepted
    with pytest.raises(ContractValidationError) as denied:
        service.start(sid, expected_revision=archived["revision"], idempotency_key=str(uuid4()))
    assert denied.value.reason_code == "session_archived"
    with pytest.raises(ContractValidationError) as denied:
        application.command("session.data.update", {
            "session_id": sid, "expected_revision": archived["revision"],
            "expected_data_revision": archived["data_revision"], "idempotency_key": str(uuid4()),
        })
    assert denied.value.reason_code == "not_found"
    with pytest.raises(ContractValidationError) as denied:
        dispatch_graph(service, "POST", f"/api/graph/sessions/{sid}/data", {})
    assert denied.value.reason_code == "not_found"


def test_workflow_archive_is_backend_owned_readonly_and_has_no_restore(service):
    doc = text_graph(service.registry)
    saved = _save_configuration(service, doc)
    view = service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    status, result = dispatch_graph(service, "POST",
        f"/api/graph/definitions/{doc['workflow_definition_id']}/archive",
        {"expected_revision": 1, "idempotency_key": str(uuid4())})
    assert status == 200 and result["archived"]
    assert service.list_definitions() == []
    assert service.list_definitions(include_archived=True)[0]["archived"]
    assert service.get_session(view["workflow_session_id"])["readonly"]
    with pytest.raises(ContractValidationError) as denied:
        service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    assert denied.value.reason_code == "workflow_archived"
    changed = {**deepcopy(saved["document"]), "name": "Cannot edit archive"}
    with pytest.raises(ContractValidationError) as denied:
        service.update_definition(
            changed, workflow_definition_id=doc["workflow_definition_id"],
            expected_edit_sequence=saved["edit_sequence"], idempotency_key=str(uuid4()))
    assert denied.value.reason_code == "workflow_archived"


def test_delete_session_removes_only_its_objects_and_retains_sibling(service):
    doc = text_graph(service.registry)
    deleted = run(service, create(service, doc))
    sibling = service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    result = service.delete_session(deleted["workflow_session_id"], expected_revision=deleted["revision"],
                                    idempotency_key=str(uuid4()))
    assert result["deleted"]
    assert service.get_session(sibling["workflow_session_id"]) == sibling
    assert service.get_definition(doc["workflow_definition_id"]) == doc
    with pytest.raises(ContractValidationError) as missing:
        service.get_session(deleted["workflow_session_id"])
    assert missing.value.reason_code == "not_found"
    with closing(SqliteStore(service.database)) as store:
        for table in ("session_object_bindings", "session_object_revision_membership", "program_variable_states"):
            assert store._connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (deleted["workflow_session_id"],)).fetchone()[0] == 0


def test_session_delete_removes_only_owned_checkpoints_and_keeps_receipts(service):
    doc = text_graph(service.registry)
    _save_configuration(service, doc)
    create_keys = [str(uuid4()) for _ in range(3)]
    sessions = [
        service.create_session(
            doc["workflow_definition_id"], 1, idempotency_key=key)
        for key in create_keys
    ]
    deleted, sibling, archived = sessions
    points = _seed_checkpoint_roots(service, sessions)
    delete_key, archive_key = str(uuid4()), str(uuid4())

    deleted_result = service.delete_session(
        deleted["workflow_session_id"],
        expected_revision=deleted["revision"],
        idempotency_key=delete_key,
    )
    assert deleted_result["deleted"]
    assert service.delete_session(
        deleted["workflow_session_id"],
        expected_revision=deleted["revision"],
        idempotency_key=delete_key,
    ) == deleted_result
    archived_result = service.archive_session(
        archived["workflow_session_id"],
        expected_revision=archived["revision"],
        idempotency_key=archive_key,
    )
    assert archived_result["archived"]
    assert service.get_session(sibling["workflow_session_id"]) == sibling

    with closing(SqliteStore(service.database)) as store:
        rows = store._connection.execute(
            "SELECT workflow_session_id,checkpoint_id FROM workflow_checkpoints "
            "ORDER BY workflow_session_id").fetchall()
        checkpoint_ids = {row["checkpoint_id"] for row in rows}
        assert points[0]["checkpoint_id"] not in checkpoint_ids
        assert points[1]["checkpoint_id"] in checkpoint_ids
        assert points[2]["checkpoint_id"] in checkpoint_ids
        assert {row["workflow_session_id"] for row in rows} == {
            sibling["workflow_session_id"], archived["workflow_session_id"],
        }
        for operation, key in (
                *[("graph.session.create", value) for value in create_keys],
                ("graph.session.delete", delete_key),
                ("graph.session.archive", archive_key)):
            assert store._connection.execute(
                "SELECT 1 FROM idempotency WHERE operation=? AND key=?", (operation, key),
            ).fetchone() is not None


def test_delete_workflow_is_targeted_and_original_receipt_does_not_restore_it(service):
    doc = text_graph(service.registry)
    view = run(service, create(service, doc))
    other = {**text_graph(service.registry), "name": "Other workflow"}
    unrelated = create(service, other)
    vault_path = service.provider_configuration.credentials.path
    vault_path.write_bytes(b"private vault test marker")
    with closing(SqliteStore(service.database)) as store:
        store._connection.execute("INSERT INTO global_resource_current VALUES(?,?,?,?,?,?)",
            ("workspace", "workflow.prompt-resource", "independent", 1, 0, '{"marker":"keep"}'))
    request = {"identity": doc["workflow_definition_id"], "expected_revision": 1,
               "idempotency_key": str(uuid4())}
    application = GraphApplication(service)
    result = application.command("definition.delete", request)
    assert result["result"]["deleted"]
    assert application.command("definition.delete", request) == result
    assert service.get_session(unrelated["workflow_session_id"]) == unrelated
    assert vault_path.read_bytes() == b"private vault test marker"
    with closing(SqliteStore(service.database)) as store:
        assert store._connection.execute("SELECT COUNT(*) FROM global_resource_current").fetchone()[0] == 1
    with pytest.raises(ContractValidationError):
        service.get_definition(doc["workflow_definition_id"])
    with pytest.raises(ContractValidationError):
        service.get_session(view["workflow_session_id"])


def test_archive_waits_for_target_stop_and_does_not_block_other_workflow(service):
    sid, chain_id, release = gate_graph(service)
    before = service.get_session(sid)
    accepted, failures = [], []

    def archive():
        try:
            accepted.append(service.archive_session(sid, expected_revision=before["revision"],
                                                     idempotency_key=str(uuid4())))
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=archive)
    thread.start()
    other = {**text_graph(service.registry), "name": "Can save while A stops"}
    try:
        _save_configuration(service, other)
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and not failures
    assert accepted and accepted[0]["archived"]
    assert service.get_session(sid)["active_chain_run_id"] is None
    assert service.get_run(sid, chain_id)["chain"]["status"] == "closed"


def test_failed_archive_write_is_not_reported_complete(service):
    view = create(service, text_graph(service.registry))
    sid = view["workflow_session_id"]

    def fail(point):
        if point == "before_commit":
            raise RuntimeError("archive write failed")

    service._fault_injector = fail
    with pytest.raises(RuntimeError, match="archive write failed"):
        service.archive_session(sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
    service._fault_injector = None
    assert not service.get_session(sid)["archived"]


def test_unrelated_damaged_workflow_does_not_block_save_but_target_ownership_is_checked(service):
    with closing(SqliteStore(service.database)) as store:
        store._connection.execute("INSERT INTO records VALUES(?,?,?,?,?)",
                                  ("workflow_session", str(uuid4()), '{"invalid":"unrelated"}', 0, "2026-10-11"))
    incomplete = document([], [])
    saved = _save_configuration(service, incomplete)
    assert service.get_definition(incomplete["workflow_definition_id"]) == saved["document"]
    with closing(SqliteStore(service.database)) as store:
        repo = GraphRecordStore(store)
        orphan = graph_record("workflow_session", workflow_session_id=str(uuid4()),
            workflow_definition_id=str(uuid4()), definition_revision=1, revision=1,
            source={"kind": "new"}, active_chain_run_id=None)
        with pytest.raises(ContractValidationError):
            repo.atomic("test.orphan", str(uuid4()), {}, lambda target: target.put("workflow_session", orphan))


def test_owner_filtered_records_preserve_insertion_order(service):
    identity = str(uuid4())
    sessions = [str(UUID(int=number, version=4)) for number in (9, 2, 6)]
    with closing(SqliteStore(service.database)) as store:
        for sid in sessions:
            store._write("workflow_session", graph_record(
                "workflow_session", workflow_session_id=sid, workflow_definition_id=identity,
                definition_revision=1, revision=1, source={"kind": "new"}, active_chain_run_id=None))
        repo = GraphRecordStore(store)
        assert repo.rows("workflow_session", workflow_definition_id=identity) == repo.rows("workflow_session")
        assert [row["workflow_session_id"] for row in repo.rows(
            "workflow_session", workflow_definition_id=identity)] == sessions


def test_delete_rejects_unknown_mock_model_evidence_even_after_explicit_close(tmp_path):
    from credential_test_support import TestProtector
    from test_stage4_model_request_installation import Answers, _agent_graph, _execute, _resources

    answers = Answers(unknown=True)
    with closing(GraphWorkflowService(tmp_path / "unknown-model.sqlite",
                                      credential_protector=TestProtector(),
                                      public_model_factory=answers.factory)) as instance:
        _, profile = _resources(instance)
        initial, _ = _agent_graph(instance, profile, count=1)
        failed = _execute(instance, initial)
        sid, chain_id = failed["workflow_session_id"], failed["active_chain_run_id"]
        before = instance.get_run(sid, chain_id)["runtime_facts"]
        assert before[-1]["details"]["classification"] == "dispatch_unknown"
        closed = instance.control(sid, action="close", expected_revision=failed["revision"],
                                  idempotency_key=str(uuid4()))
        with pytest.raises(ContractValidationError) as denied:
            instance.delete_session(sid, expected_revision=closed["revision"], idempotency_key=str(uuid4()))
        assert denied.value.reason_code == "execution_result_unresolved"
        assert instance.get_session(sid) == closed
        assert instance.get_run(sid, chain_id)["runtime_facts"] == before
        assert len(answers.messages) == answers.closes == 1


def test_delete_checks_unconfirmed_status_before_closing_target(service):
    from test_graph_checkpoint_retry import fail_checkpoint_twice

    initial = create(service, text_graph(service.registry))
    fail_checkpoint_twice(service)
    pending = run(service, initial)
    sid, chain_id = pending["workflow_session_id"], pending["active_chain_run_id"]
    assert pending["status"] == "archive_failed"
    with closing(SqliteStore(service.database)) as store:
        store._connection.execute(
            "UPDATE records SET payload=json_set(payload,'$.diagnostic',NULL) "
            "WHERE record_type='chain_run' AND json_extract(payload,'$.chain_run_id')=?", (chain_id,))
    before = service.get_session(sid)
    with pytest.raises(ContractValidationError) as denied:
        service.delete_session(sid, expected_revision=before["revision"], idempotency_key=str(uuid4()))
    assert denied.value.reason_code == "execution_result_unresolved"
    assert service.get_session(sid) == before
