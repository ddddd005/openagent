"""Formal configuration copy: new identity, no session history, verifiable receipt.

The copy command is the T5 backend: it reads one formal current configuration and
its exact fixed revision inside the existing configuration command transaction,
then writes a deep-copied document under a brand-new workflow identity with
``revision=1`` / ``edit_sequence=1`` and no sessions, runs or data.  These tests
use real temporary stores and the real application command/receipt path.
"""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import validate_graph_document
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import gate_graph, run, text_graph
from test_graph_receipt_roundtrip import fixed_configuration


COPY_FIELDS = {
    "schema_version", "kind", "outcome", "source_workflow_definition_id",
    "source_definition_revision", "source_edit_sequence", "workflow_definition_id",
    "definition_revision", "edit_sequence", "session_id", "node_id_map", "document",
}
ACCEPTED_FIELDS = {
    "outcome", "source_workflow_definition_id", "source_definition_revision",
    "source_edit_sequence", "workflow_definition_id", "definition_revision",
    "edit_sequence", "session_id",
}


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "copy.sqlite")) as instance:
        yield instance


def create_source(app, service, *, name="Copy source"):
    value = fixed_configuration(text_graph(service.registry))
    value["name"] = name
    created = app.command("definition.update", {
        "workflow_definition_id": value["workflow_definition_id"],
        "document": value, "expected_edit_sequence": 0,
        "idempotency_key": str(uuid4()),
    })["result"]
    source = created["workflow_definition_id"]
    session = service.create_session(source, 1, idempotency_key=str(uuid4()))
    finished = run(service, service.get_session(session["workflow_session_id"]))
    assert finished["status"] == "succeeded"
    return source, session


def copy_request(source, target=None, **overrides):
    request = {
        "source_workflow_definition_id": source,
        "expected_source_definition_revision": 1,
        "expected_source_edit_sequence": 1,
        "target_workflow_definition_id": target or str(uuid4()),
        "target_name": "Copied workflow",
        "idempotency_key": str(uuid4()),
    }
    request.update(overrides)
    return request


def accepted_copy(app, request):
    return app.command("definition.copy", request)


def stored_receipt(service, request):
    return read_graph_application_receipt(service.database, "definition.copy", request)


def definition_records(service, identity):
    """All persisted records owned by one workflow identity, stable comparison."""
    with closing(service._store()) as store:
        return tuple(
            (row["record_type"], row["payload"]) for row in store._connection.execute(
                "SELECT record_type,payload FROM records WHERE json_valid(payload) "
                "AND json_extract(payload,'$.workflow_definition_id')=? "
                "ORDER BY record_type,record_id", (identity,)))


def count(service, query, parameters=()):
    with closing(service._store()) as store:
        return store._connection.execute(query, parameters).fetchone()[0]


def save_fixed_only(service, value, expected_revision):
    return service._change(
        "test.fixture.definition.save", str(uuid4()),
        {"document": deepcopy(value), "expected_revision": expected_revision},
        lambda repo: service._save_definition(repo, value, expected_revision))


def test_copy_creates_new_identity_without_sessions_runs_or_data(service):
    app = GraphApplication(service)
    source, session = create_source(app, service)
    source_definition = service.get_definition(source)
    source_fixed = service.get_definition(source, 1)
    source_sessions = service.list_sessions(source)
    source_records = definition_records(service, source)
    target = str(uuid4())
    request = copy_request(source, target)
    accepted = accepted_copy(app, request)
    result = accepted["result"]

    # Exact fixed result shape and source/target facts from the contract.
    assert set(result) == COPY_FIELDS
    assert result["schema_version"] == 1
    assert result["kind"] == "workflow.configuration-copy-result"
    assert result["outcome"] == "copied"
    assert result["source_workflow_definition_id"] == source
    assert result["source_definition_revision"] == 1
    assert result["source_edit_sequence"] == 1
    assert result["workflow_definition_id"] == target
    assert result["definition_revision"] == 1
    assert result["edit_sequence"] == 1
    assert result["session_id"] is None
    assert set(accepted["receipt"]["accepted"]) == ACCEPTED_FIELDS

    # The target is a formal current configuration with the new identity/name and
    # initial counters; the copied document is the read-back current document.
    target_document = service.get_definition(target)
    assert result["document"] == target_document
    expected_target = deepcopy(source_definition)
    expected_target.update(workflow_definition_id=target, name="Copied workflow")
    assert target_document == expected_target
    assert target_document["workflow_definition_id"] == target
    assert target_document["name"] == "Copied workflow"
    assert target_document["revision"] == 1
    assert target_document["edit_sequence"] == 1
    assert service.get_definition(target, 1)["revision"] == 1

    # Scope-local references stay consistent; shared external references are kept
    # by identity (package lock here, nothing is copied or installed).
    validate_graph_document(target_document)
    assert [item["node_binding_id"] for item in target_document["nodes"]] == [
        item["node_binding_id"] for item in source_definition["nodes"]]
    assert target_document["edges"] == source_definition["edges"]
    assert target_document["object_bindings"] == source_definition["object_bindings"]
    assert target_document.get("package_lock") == source_definition.get("package_lock")
    node_ids = {item["node_binding_id"] for item in target_document["nodes"]}
    assert all(edge["source_node_id"] in node_ids and edge["target_node_id"] in node_ids
               for edge in target_document["edges"])
    assert result["node_id_map"] == {identity: identity for identity in node_ids}

    # A copy creates no sessions, chains, runs, snapshots, commits, checkpoints,
    # variables or session objects: the target only holds its definition rows.
    assert service.list_sessions(target) == []
    target_records = definition_records(service, target)
    assert {record_type for record_type, _ in target_records} == {
        "workflow_definition_revision", "node_binding"}
    assert len(target_records) == 1 + len(target_document["nodes"])
    assert count(service, "SELECT COUNT(*) FROM workflow_checkpoints "
                         "WHERE workflow_definition_id=?", (target,)) == 0
    directory = {row["workflow_definition_id"]: row
                 for row in app.query("definition.summaries")}
    assert directory[target]["session_count"] == 0
    assert directory[target]["edit_sequence"] == 1
    assert directory[target]["has_current_configuration"] is True

    # The source is not modified or locked by the copy.
    assert service.get_definition(source) == source_definition
    assert service.get_definition(source, 1) == source_fixed
    assert service.list_sessions(source) == source_sessions
    assert definition_records(service, source) == source_records
    assert service.get_session(session["workflow_session_id"])["status"] == "succeeded"

    # The native receipt replays the original result without re-reading anything.
    assert accepted_copy(app, request)["result"] == result


def test_copy_allows_archived_source_but_never_copies_sessions(service):
    """G-A decided 2026-10-21: archived formal sources are allowed to be copied."""
    app = GraphApplication(service)
    source, session = create_source(app, service)
    service.archive_definition(source, expected_revision=1, idempotency_key=str(uuid4()))
    source_definition = service.get_definition(source)
    source_sessions = service.list_sessions(source)
    source_records = definition_records(service, source)
    target = str(uuid4())
    request = copy_request(source, target, target_name="Copy of archived")
    result = accepted_copy(app, request)["result"]

    assert result["outcome"] == "copied" and result["workflow_definition_id"] == target
    # The archived source stays archived and entirely unchanged.
    assert count(service, "SELECT archived FROM workflow_management "
                         "WHERE object_kind='definition' AND object_id=?", (source,)) == 1
    assert service.get_definition(source) == source_definition
    assert service.list_sessions(source) == source_sessions
    assert definition_records(service, source) == source_records
    # The new target is a normal, non-archived, session-free formal definition.
    directory = {row["workflow_definition_id"]: row
                 for row in app.query("definition.summaries")}
    assert source not in directory  # archived sources stay filtered by default
    assert directory[target]["archived"] is False
    assert directory[target]["session_count"] == 0
    assert service.list_sessions(target) == []
    assert count(service, "SELECT COUNT(*) FROM workflow_management "
                         "WHERE object_kind='definition' AND object_id=?", (target,)) == 0


def test_copy_does_not_pause_or_change_an_active_source(service):
    app = GraphApplication(service)
    session_id, chain_id, release = gate_graph(service)
    source = service.get_session(session_id)["workflow_definition_id"]
    source_definition = service.get_definition(source)
    source_sessions = service.list_sessions(source)
    target = str(uuid4())
    try:
        # A source with an unfinished run is still a valid read-only copy source;
        # the copy neither applies the edit run lock nor pauses the source run.
        result = accepted_copy(app, copy_request(source, target))["result"]
        assert result["outcome"] == "copied"
        assert service.get_session(session_id)["status"] == "running"
        assert service.get_session(session_id)["active_chain_run_id"] == chain_id
        assert service.get_definition(source) == source_definition
        assert service.list_sessions(source) == source_sessions
        assert service.list_sessions(target) == []
    finally:
        release.set()
    service.wait(chain_id)
    assert service.get_definition(source) == source_definition


def test_copy_rejects_stale_source_missing_current_and_deleted_source(service):
    app = GraphApplication(service)
    source, _ = create_source(app, service)

    target = str(uuid4())
    stale_revision = copy_request(
        source, target, expected_source_definition_revision=2)
    with pytest.raises(ContractValidationError) as error:
        accepted_copy(app, stale_revision)
    assert error.value.reason_code == "stale_revision"
    assert error.value.status_code == 409
    assert stored_receipt(service, stale_revision)["outcome"] == "unresolved"
    with pytest.raises(ContractValidationError):
        service.get_definition(target)

    target = str(uuid4())
    stale_sequence = copy_request(
        source, target, expected_source_edit_sequence=2)
    with pytest.raises(ContractValidationError) as error:
        accepted_copy(app, stale_sequence)
    assert error.value.reason_code == "stale_edit_sequence"
    assert error.value.status_code == 409
    with pytest.raises(ContractValidationError):
        service.get_definition(target)

    # A legacy fixed-only definition has no current configuration: it is not a
    # formal copy source and no historical fixed document fabricates one.
    legacy = fixed_configuration(text_graph(service.registry))
    legacy["name"] = "Legacy copy source"
    save_fixed_only(service, legacy, 0)
    legacy_id = legacy["workflow_definition_id"]
    target = str(uuid4())
    missing = copy_request(legacy_id, target)
    with pytest.raises(ContractValidationError) as error:
        accepted_copy(app, missing)
    assert error.value.reason_code == "graph_configuration_missing"
    assert error.value.status_code == 409
    with pytest.raises(ContractValidationError):
        service.get_definition(target)
    assert count(service, "SELECT COUNT(*) FROM workflow_current_configuration "
                         "WHERE workflow_definition_id=?", (target,)) == 0

    deleted_source, _ = create_source(app, service)
    service.delete_definition(deleted_source, expected_revision=1,
                              idempotency_key=str(uuid4()))
    target = str(uuid4())
    deleted = copy_request(deleted_source, target)
    with pytest.raises(ContractValidationError) as error:
        accepted_copy(app, deleted)
    assert error.value.reason_code == "not_found"
    assert error.value.status_code == 404
    with pytest.raises(ContractValidationError):
        service.get_definition(target)


def test_copy_rejects_existing_target_and_bad_parameters(service):
    app = GraphApplication(service)
    source, _ = create_source(app, service)

    # An existing formal definition is a conflict; it is never updated in place.
    existing_value = fixed_configuration(text_graph(service.registry))
    existing_value["name"] = "Existing target"
    existing = app.command("definition.update", {
        "workflow_definition_id": existing_value["workflow_definition_id"],
        "document": existing_value, "expected_edit_sequence": 0,
        "idempotency_key": str(uuid4()),
    })["result"]["document"]
    existing_id = existing["workflow_definition_id"]
    with pytest.raises(ContractValidationError) as error:
        accepted_copy(app, copy_request(source, existing_id))
    assert error.value.reason_code == "graph_definition_conflict"
    assert error.value.status_code == 409
    assert service.get_definition(existing_id) == existing

    # A legacy fixed-only target is still an existing formal identity.
    legacy = fixed_configuration(text_graph(service.registry))
    save_fixed_only(service, legacy, 0)
    legacy_id = legacy["workflow_definition_id"]
    with pytest.raises(ContractValidationError) as error:
        accepted_copy(app, copy_request(source, legacy_id))
    assert error.value.reason_code == "graph_definition_conflict"

    # An archived target is not reusable either.
    archived_value = fixed_configuration(text_graph(service.registry))
    archived = app.command("definition.update", {
        "workflow_definition_id": archived_value["workflow_definition_id"],
        "document": archived_value, "expected_edit_sequence": 0,
        "idempotency_key": str(uuid4()),
    })["result"]["document"]
    archived_id = archived["workflow_definition_id"]
    service.archive_definition(archived_id, expected_revision=1,
                               idempotency_key=str(uuid4()))
    with pytest.raises(ContractValidationError) as error:
        accepted_copy(app, copy_request(source, archived_id))
    assert error.value.reason_code == "graph_definition_conflict"
    assert service.get_definition(archived_id) == archived

    # Public contract shape validation (contract layer) and the service's own
    # defense for direct callers: both reject bad parameters before any effect.
    target = str(uuid4())
    current_count = count(
        service, "SELECT COUNT(*) FROM workflow_current_configuration")
    bad_public = [
        copy_request(source, source),                                 # same identity
        copy_request(source, target, expected_source_definition_revision=0),
        copy_request(source, target, expected_source_edit_sequence=0),
        copy_request(source, target, target_name=""),
        copy_request(source, target, target_name=" padded "),
        copy_request(source, target, target_name="x" * 129),
        copy_request(source, target, source_workflow_definition_id="not-a-uuid"),
    ]
    for request in bad_public:
        with pytest.raises(ContractValidationError) as error:
            accepted_copy(app, request)
        assert error.value.reason_code == "invalid_request"
        with closing(service._store()) as store:
            assert store.read_receipt(
                "graph.definition.copy", request["idempotency_key"]) is None
    for kwargs in (
        {"source_workflow_definition_id": source,
         "target_workflow_definition_id": source},
        {"target_workflow_definition_id": "not-a-uuid"},
        {"expected_source_definition_revision": True},
        {"expected_source_edit_sequence": -1},
        {"target_name": "x" * 129},
    ):
        parameters = {
            "source_workflow_definition_id": source,
            "expected_source_definition_revision": 1,
            "expected_source_edit_sequence": 1,
            "target_workflow_definition_id": str(uuid4()),
            "target_name": "Direct copy",
            "idempotency_key": str(uuid4()),
            **kwargs,
        }
        with pytest.raises(ContractValidationError) as error:
            service.copy_definition(**parameters)
        assert error.value.reason_code == "invalid_request"
    # The rejected requests wrote no target configuration at all.
    assert current_count == 3
    assert count(service, "SELECT COUNT(*) FROM workflow_current_configuration") == current_count


def test_copy_rolls_back_business_and_receipt_on_commit_failure(tmp_path):
    failure = {"active": False}

    def inject(point):
        if point == "before_commit" and failure["active"]:
            raise RuntimeError("synthetic copy commit failure")

    service = GraphWorkflowService(tmp_path / "copy-fault.sqlite", fault_injector=inject)
    try:
        app = GraphApplication(service)
        source, _ = create_source(app, service)
        source_definition = service.get_definition(source)
        source_records = definition_records(service, source)
        target = str(uuid4())
        request = copy_request(source, target)
        failure["active"] = True
        with pytest.raises(RuntimeError):
            accepted_copy(app, request)
        failure["active"] = False
        # Neither business rows nor the native/application receipt survived.
        with pytest.raises(ContractValidationError):
            service.get_definition(target)
        assert definition_records(service, target) == ()
        assert service._store().read_receipt(
            "graph.definition.copy", request["idempotency_key"]) is None
        assert stored_receipt(service, request)["outcome"] == "unresolved"
        assert service.get_definition(source) == source_definition
        assert definition_records(service, source) == source_records
        # Retrying the same key succeeds now because the failed attempt fully
        # rolled back instead of leaving a conflicting receipt.
        retried = accepted_copy(app, request)["result"]
        assert retried["outcome"] == "copied"
        assert service.get_definition(target) == retried["document"]
        assert stored_receipt(service, request)["outcome"] == "matched"
    finally:
        service.close()


def test_copy_same_key_replays_and_different_request_conflicts(service):
    app = GraphApplication(service)
    source, _ = create_source(app, service)
    target = str(uuid4())
    request = copy_request(source, target, idempotency_key="copy-replay-key")
    first = accepted_copy(app, request)["result"]
    replayed = accepted_copy(app, request)["result"]
    assert replayed == first
    assert len(definition_records(service, target)) == 1 + len(first["document"]["nodes"])

    changed = copy_request(source, target, idempotency_key="copy-replay-key",
                           target_name="Different name")
    with pytest.raises(ContractValidationError) as error:
        accepted_copy(app, changed)
    assert error.value.reason_code == "idempotency_conflict"
    assert error.value.status_code == 409
    assert service.get_definition(target) == first["document"]


def test_receipt_read_stays_matched_after_source_advance_delete_and_target_delete(service):
    app = GraphApplication(service)
    source, _ = create_source(app, service)
    target = str(uuid4())
    request = copy_request(source, target)
    accepted = accepted_copy(app, request)
    result = accepted["result"]
    assert stored_receipt(service, request)["outcome"] == "matched"
    assert app.query("receipt.read", {
        "operation": "definition.copy", "parameters": request,
    }) == stored_receipt(service, request)

    # Source advance after the accepted copy must not invalidate the receipt.
    source_definition = service.get_definition(source)
    advanced = deepcopy(source_definition)
    advanced["name"] = "Source advanced"
    advanced = app.command("definition.update", {
        "workflow_definition_id": source, "document": advanced,
        "expected_edit_sequence": source_definition["edit_sequence"],
        "idempotency_key": str(uuid4()),
    })["result"]["document"]
    assert advanced["name"] == "Source advanced"
    matched = stored_receipt(service, request)
    assert matched["outcome"] == "matched"
    assert matched["result"] == result
    assert matched["receipt"] == accepted["receipt"]

    # Later target deletion does not remove the accepted evidence or rebuild it.
    service.delete_definition(target, expected_revision=1, idempotency_key=str(uuid4()))
    with pytest.raises(ContractValidationError):
        service.get_definition(target)
    assert stored_receipt(service, request)["result"] == result
    # A replayed command returns the persisted result and never rebuilds the
    # deleted target from the original request.
    assert accepted_copy(app, request)["result"] == result
    with pytest.raises(ContractValidationError):
        service.get_definition(target)
    assert definition_records(service, target) == ()

    # Later source deletion is equally irrelevant to the frozen receipt.
    service.delete_definition(source, expected_revision=1, idempotency_key=str(uuid4()))
    with pytest.raises(ContractValidationError):
        service.get_definition(source)
    matched = stored_receipt(service, request)
    assert matched["outcome"] == "matched"
    assert matched["result"] == result
    assert accepted_copy(app, request)["result"] == result
    assert app.query("definition.summaries") == []


def test_receipt_read_rejects_tampered_copy_result(service):
    import sqlite3
    from phase1_agent.contract_json import canonical_bytes, loads_strict

    app = GraphApplication(service)
    source, _ = create_source(app, service)
    target = str(uuid4())
    request = copy_request(source, target)
    accepted = accepted_copy(app, request)
    result = accepted["result"]
    assert stored_receipt(service, request)["outcome"] == "matched"

    with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
        raw = connection.execute(
            "SELECT result_payload FROM idempotency WHERE operation=? AND key=?",
            ("graph.definition.copy", request["idempotency_key"]),
        ).fetchone()[0]

        def tamper(change):
            payload = loads_strict(raw)
            stored = payload[0]["graph_result"]
            change(stored)
            connection.execute(
                "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
                (canonical_bytes(payload).decode("utf-8"),
                 "graph.definition.copy", request["idempotency_key"]))
            try:
                read = stored_receipt(service, request)
                assert read["outcome"] == "unresolved", read
                assert read["reason_code"] == "receipt_invalid", read
            finally:
                connection.execute(
                    "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
                    (raw, "graph.definition.copy", request["idempotency_key"]))

        tamper(lambda stored: stored.update(definition_revision=2))
        tamper(lambda stored: stored.update(edit_sequence=2))
        tamper(lambda stored: stored.update(session_id=str(uuid4())))
        tamper(lambda stored: stored.update(outcome="saved"))
        tamper(lambda stored: stored["document"].update(workflow_definition_id=str(uuid4())))
        tamper(lambda stored: stored["document"].update(name="Forged name"))
        tamper(lambda stored: stored["document"].update(revision=2))
        tamper(lambda stored: stored["node_id_map"].update(
            {next(iter(stored["node_id_map"])): str(uuid4())}))
        tamper(lambda stored: stored.pop("source_edit_sequence"))

    assert stored_receipt(service, request)["result"] == result
