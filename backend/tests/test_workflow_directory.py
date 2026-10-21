"""Narrow workflow/session directory projections on a real temporary store.

The directory is the T3 management read: it must be a lightweight projection of
the same SQLite database, not a renamed full ``definition.list`` /
``definition.sessions``.  These tests build real fixed revisions, real sessions,
real execution history and real archived/deleted rows in a temporary store, then
assert the projection fields, filtering, counts and derived status against the
existing full-read behaviour.
"""

from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.configuration_data import (
    ConfigurationDataSession, ConfigurationRepository, WorkflowDataManagement,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.execution_data import ExecutionRecords, ExecutionState
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
import phase1_agent.workflow_directory as directory_module

from test_graph_receipt_roundtrip import fixed_configuration
from test_graph_service import document, edge, gate_graph, node, run, text_graph


DEFINITION_FIELDS = [
    "workflow_definition_id", "display_name", "fixed_definition_revision",
    "edit_sequence", "archived", "node_count", "session_count",
    "has_current_configuration",
]
SESSION_FIELDS = [
    "workflow_session_id", "workflow_definition_id", "definition_revision",
    "revision", "name", "metadata_sequence", "archived", "active_chain_run_id",
    "status",
]


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "directory.sqlite")) as instance:
        yield instance


def save(app, value, sequence):
    key = str(uuid4())
    result = app.command("definition.update", {
        "workflow_definition_id": value["workflow_definition_id"],
        "document": deepcopy(value), "expected_edit_sequence": sequence,
        "idempotency_key": key,
    })["result"]
    assert result["outcome"] in ("saved", "unchanged"), result
    return result


def save_fixed_only(service, value, expected_revision):
    """Persist a legacy fixed revision with no current coverage row."""
    key = str(uuid4())
    return service._change(
        "test.fixture.definition.save", key,
        {"document": deepcopy(value), "expected_revision": expected_revision},
        lambda repo: service._save_definition(repo, value, expected_revision))


def summarized(app, *, include_archived=False):
    parameters = {"include_archived": True} if include_archived else None
    return {row["workflow_definition_id"]: row
            for row in app.query("definition.summaries", parameters)}


def build_directory_rows(service):
    """Create formal/archived/legacy/deleted definitions and formal sessions."""
    app = GraphApplication(service)

    main = fixed_configuration(text_graph(service.registry))
    main["name"] = "Directory main"
    first = save(app, main, 0)                      # fixed revision 1, edit 1
    edited = deepcopy(first["document"])
    edited["nodes"][0]["config"]["text"] = "directory second revision"
    second = save(app, edited, 1)                   # fixed revision 2, edit 2
    renamed = deepcopy(second["document"])
    renamed["name"] = "Directory renamed"
    third = save(app, renamed, 2)                   # display only, edit 3
    assert third["definition_revision"] == 2 and third["edit_sequence"] == 3
    main_id = third["workflow_definition_id"]

    running = service.create_session(main_id, 2, idempotency_key=str(uuid4()))
    run(service, service.get_session(running["workflow_session_id"]))
    idle = service.create_session(main_id, 2, idempotency_key=str(uuid4()))
    service.update_session_metadata(
        idle["workflow_session_id"], name="idle session",
        expected_metadata_sequence=0, idempotency_key=str(uuid4()))
    archived_session = service.create_session(main_id, 2, idempotency_key=str(uuid4()))
    service.archive_session(
        archived_session["workflow_session_id"], expected_revision=1,
        idempotency_key=str(uuid4()))

    archived_definition = fixed_configuration(text_graph(service.registry))
    archived_definition["name"] = "Archived workflow"
    archived_saved = save(app, archived_definition, 0)
    archived_id = archived_saved["workflow_definition_id"]
    definition_session = service.create_session(archived_id, 1, idempotency_key=str(uuid4()))
    service.archive_definition(archived_id, expected_revision=1, idempotency_key=str(uuid4()))

    legacy = document(
        [node(service.registry, "tools.text", 21), node(service.registry, "tools.output", 22)],
        [])
    legacy["name"] = "Legacy shell"
    legacy["nodes"][1]["public_outputs"] = ["output"]
    save_fixed_only(service, legacy, 0)
    legacy_next = deepcopy(legacy)
    legacy_next["revision"] = 2
    legacy_next["name"] = "Legacy renamed"
    legacy_next["nodes"].append(node(service.registry, "tools.text", 23))
    save_fixed_only(service, legacy_next, 1)

    deleted = fixed_configuration(text_graph(service.registry))
    deleted["name"] = "Deleted workflow"
    deleted_saved = save(app, deleted, 0)
    deleted_id = deleted_saved["workflow_definition_id"]
    service.delete_definition(deleted_id, expected_revision=1, idempotency_key=str(uuid4()))

    return {
        "main": main_id,
        "archived": archived_id,
        "legacy": legacy["workflow_definition_id"],
        "deleted": deleted_id,
        "running": running["workflow_session_id"],
        "idle": idle["workflow_session_id"],
        "archived_session": archived_session["workflow_session_id"],
        "definition_session": definition_session["workflow_session_id"],
    }


def test_definition_summaries_project_current_latest_fixed_and_legacy_rows(service):
    ids = build_directory_rows(service)
    app = GraphApplication(service)
    rows = summarized(app)

    assert set(rows) == {ids["main"], ids["legacy"]}
    assert list(rows[ids["main"]]) == DEFINITION_FIELDS
    assert rows[ids["main"]] == {
        "workflow_definition_id": ids["main"],
        "display_name": "Directory renamed",
        "fixed_definition_revision": 2,
        "edit_sequence": 3,
        "archived": False,
        "node_count": 3,
        "session_count": 3,
        "has_current_configuration": True,
    }
    # A formal fixed revision without current stays visible but grants no
    # editable/copyable capability; its name/revision come from the real latest
    # fixed record instead of a fabricated current edit sequence.
    assert list(rows[ids["legacy"]]) == DEFINITION_FIELDS
    assert rows[ids["legacy"]] == {
        "workflow_definition_id": ids["legacy"],
        "display_name": "Legacy renamed",
        "fixed_definition_revision": 2,
        "edit_sequence": None,
        "archived": False,
        "node_count": 3,
        "session_count": 0,
        "has_current_configuration": False,
    }
    assert ids["deleted"] not in rows

    with_archived = summarized(app, include_archived=True)
    assert set(with_archived) == {ids["main"], ids["archived"], ids["legacy"]}
    assert with_archived[ids["archived"]] == {
        "workflow_definition_id": ids["archived"],
        "display_name": "Archived workflow",
        "fixed_definition_revision": 1,
        "edit_sequence": 1,
        "archived": True,
        "node_count": 3,
        "session_count": 1,
        "has_current_configuration": True,
    }
    # Deleted rows stay absent even when archived rows are requested.
    assert ids["deleted"] not in with_archived
    # The default response is filtered, not a re-projection of an archived view.
    assert [row["workflow_definition_id"] for row in app.query("definition.summaries")] == [
        ids["main"], ids["legacy"]]
    assert app.query("definition.summaries") == [rows[ids["main"]], rows[ids["legacy"]]]


def test_session_summaries_filter_archive_metadata_and_match_full_status(service):
    ids = build_directory_rows(service)
    app = GraphApplication(service)
    rows = app.query("session.summaries", {"workflow_definition_id": ids["main"]})

    assert [row["workflow_session_id"] for row in rows] == [ids["running"], ids["idle"]]
    assert list(rows[0]) == SESSION_FIELDS
    assert rows[0]["workflow_session_id"] == ids["running"]
    assert rows[0]["workflow_definition_id"] == ids["main"]
    assert rows[0]["definition_revision"] == 2
    assert rows[0]["name"] == "" and rows[0]["metadata_sequence"] == 0
    assert rows[0]["archived"] is False
    assert rows[0]["active_chain_run_id"] is None
    assert rows[0]["status"] == "succeeded"
    assert service.get_session(ids["running"])["status"] == "succeeded"

    assert rows[1]["workflow_session_id"] == ids["idle"]
    assert rows[1]["name"] == "idle session" and rows[1]["metadata_sequence"] == 1
    assert rows[1]["archived"] is False
    assert rows[1]["status"] == "idle"
    assert service.get_session(ids["idle"])["status"] == "idle"

    with_archived = app.query("session.summaries", {
        "workflow_definition_id": ids["main"], "include_archived": True})
    assert [row["workflow_session_id"] for row in with_archived] == [
        ids["running"], ids["idle"], ids["archived_session"]]
    assert with_archived[-1]["archived"] is True
    assert with_archived[-1]["status"] == "idle"
    assert service.get_session(ids["archived_session"])["archived"] is True
    # A session whose definition is archived is archived in the view semantics.
    assert app.query("session.summaries", {
        "workflow_definition_id": ids["archived"]}) == []
    definition_rows = app.query("session.summaries", {
        "workflow_definition_id": ids["archived"], "include_archived": True})
    assert [row["workflow_session_id"] for row in definition_rows] == [
        ids["definition_session"]]
    assert definition_rows[0]["archived"] is True
    assert service.get_session(ids["definition_session"])["archived"] is True
    assert app.query("session.summaries", {
        "workflow_definition_id": ids["legacy"]}) == []


def test_session_status_follows_active_chain_then_head_selection(service):
    app = GraphApplication(service)
    session_id, chain_id, release = gate_graph(service)
    definition_id = service.get_session(session_id)["workflow_definition_id"]
    try:
        # The active round chain decides while it is unfinished, exactly as the
        # full view reports it; this is not "the latest chain" of the session.
        full = service.get_session(session_id)
        assert full["status"] == "running" and full["active_chain_run_id"] == chain_id
        rows = app.query("session.summaries", {"workflow_definition_id": definition_id})
        assert len(rows) == 1
        assert rows[0]["status"] == full["status"] == "running"
        assert rows[0]["active_chain_run_id"] == chain_id
    finally:
        release.set()
    service.wait(chain_id)
    # With no active chain the Head-selected round chain decides.
    full = service.get_session(session_id)
    assert full["status"] == "succeeded" and full["active_chain_run_id"] is None
    rows = app.query("session.summaries", {"workflow_definition_id": definition_id})
    assert rows[0]["status"] == full["status"] == "succeeded"
    assert rows[0]["active_chain_run_id"] is None


def event_definition(service, entered, release):
    def execute(config, inputs, context):
        entered.set()
        assert release.wait(5), "event gate timed out"
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.directory-event-gate", "1", "Directory Event Gate", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), execute)
    source = node(service.registry, "tools.current-input", 41, input_name="text")
    gate = node(service.registry, "test.directory-event-gate", 42)
    output = node(service.registry, "tools.output", 43)
    output["public_outputs"] = ["output"]
    value = document([source, gate, output],
                     [edge(source, gate, 1), edge(gate, output, 2)])
    value["schema_version"] = 3
    for item in value["nodes"]:
        item["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    value["package_lock"] = list(service.registry.execution_package_lock)
    value["event_bindings"] = [{
        "event_id": "directory.append", "schema_version": 1, "display_name": "Append",
        "audience": "management", "target_node_ids": [output["node_binding_id"]],
        "payload_schema": {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        },
    }]
    saved = service.update_definition(
        value, workflow_definition_id=value["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))
    return service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))


def test_session_status_does_not_promote_a_completed_event_chain(service):
    from threading import Event

    entered, release = Event(), Event()
    session = event_definition(service, entered, release)
    app = GraphApplication(service)
    definition_id = session["workflow_definition_id"]
    try:
        started = service.submit_event(
            session["workflow_session_id"],
            workflow_definition_id=definition_id, definition_revision=1,
            event_id="directory.append", event_schema_version=1,
            payload={"text": "directory event"}, expected_revision=session["revision"],
            idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        assert entered.wait(5), "event gate was not reached"
        # The active event chain is what the full view reports while it exists.
        full = service.get_session(session["workflow_session_id"])
        assert full["status"] == "running"
        rows = app.query("session.summaries", {"workflow_definition_id": definition_id})
        assert rows[0]["status"] == full["status"] == "running"
        assert rows[0]["active_chain_run_id"] == chain_id
    finally:
        release.set()
    service.wait(chain_id)
    # A completed event chain is not the Head-selected round chain, so the
    # session returns to its inherited (seed -> idle) head semantics.
    full = service.get_session(session["workflow_session_id"])
    assert full["active_chain_run_id"] is None
    assert full["status"] == "idle"
    rows = app.query("session.summaries", {"workflow_definition_id": definition_id})
    assert rows[0]["status"] == full["status"] == "idle"
    assert rows[0]["active_chain_run_id"] is None


def test_summaries_use_narrow_reads_and_close_one_fresh_store_per_response(service, monkeypatch):
    app = GraphApplication(service)
    big = fixed_configuration(text_graph(service.registry))
    big["name"] = "Narrow reads"
    # A large fixed document must stay in SQLite: the projection may look at its
    # scalar JSON paths, but no full document/output/variable body enters Python.
    big["nodes"][0]["config"]["text"] = "x" * 200_000
    created = save(app, big, 0)
    definition_id = created["workflow_definition_id"]
    session = service.create_session(definition_id, 1, idempotency_key=str(uuid4()))
    run(service, service.get_session(session["workflow_session_id"]))

    calls = []

    def poison(name):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(name + " must not be used by directory summaries")
        return fail

    monkeypatch.setattr(service, "_view", poison("service._view"))
    monkeypatch.setattr(service, "_compose_configuration",
                        poison("service._compose_configuration"))
    monkeypatch.setattr(service.configuration_service, "list_configuration_definitions",
                        poison("configuration_service.list_configuration_definitions"))
    monkeypatch.setattr(WorkflowDataManagement, "execution_read",
                        poison("data_management.execution_read"))
    monkeypatch.setattr(WorkflowDataManagement, "read_execution",
                        poison("data_management.read_execution"))
    for name in ("fixed_document", "definition_revisions", "all_definition_revisions"):
        monkeypatch.setattr(ConfigurationRepository, name, poison("repository." + name))
    for name in ("fixed_document", "definition_revisions", "all_definition_revisions",
                 "configuration_view"):
        monkeypatch.setattr(ConfigurationDataSession, name, poison("session." + name))
    for name in ("outputs", "outputs_for_chain", "private_states", "node_runs"):
        monkeypatch.setattr(ExecutionRecords, name, poison("records." + name))
    monkeypatch.setattr(ExecutionState, "variables", poison("state.variables"))

    loaded_payloads = []
    original_loads = directory_module.loads_strict

    def record_payload(text):
        loaded_payloads.append(len(text))
        return original_loads(text)

    monkeypatch.setattr(directory_module, "loads_strict", record_payload)

    stores = []
    original_open = service.environment.open_store

    def tracked_open(*, fault_injector=None):
        store = original_open(fault_injector=fault_injector)
        stores.append(store)
        return store

    monkeypatch.setattr(service.environment, "open_store", tracked_open)

    read_transactions = []
    original_directory_init = directory_module.WorkflowDirectoryRead.__init__

    def tracked_directory_init(self, connection):
        # The projection is constructed only after the independent read
        # transaction has begun on the one fresh connection.
        read_transactions.append(connection.in_transaction)
        original_directory_init(self, connection)

    monkeypatch.setattr(directory_module.WorkflowDirectoryRead, "__init__",
                        tracked_directory_init)

    definitions = app.query("definition.summaries")
    sessions = app.query("session.summaries", {"workflow_definition_id": definition_id})

    assert definitions == [{
        "workflow_definition_id": definition_id,
        "display_name": "Narrow reads",
        "fixed_definition_revision": 1,
        "edit_sequence": 1,
        "archived": False,
        "node_count": 3,
        "session_count": 1,
        "has_current_configuration": True,
    }]
    assert len(sessions) == 1 and sessions[0]["status"] == "succeeded"
    assert calls == []
    # Only narrow commit-source/head payloads may be decoded; never a document.
    assert loaded_payloads and max(loaded_payloads) < 4096
    # Each response owns one fresh store, closed after its read transaction.
    assert len(stores) == 2 and all(store is not None for store in stores)
    assert read_transactions == [True, True]
    for store in stores:
        with pytest.raises(sqlite3.ProgrammingError):
            store._connection.execute("SELECT 1")


def test_summary_arguments_are_strict(service):
    ids = build_directory_rows(service)
    app = GraphApplication(service)
    for invalid in ("true", 1, None):
        with pytest.raises(ContractValidationError) as error:
            app.query("definition.summaries", {"include_archived": invalid})
        assert error.value.reason_code == "invalid_request"
        with pytest.raises(ContractValidationError) as error:
            app.query("session.summaries", {
                "workflow_definition_id": ids["main"], "include_archived": invalid})
        assert error.value.reason_code == "invalid_request"
    for identity in (str(uuid4()) + "x", "not-a-uuid", 42):
        with pytest.raises(ContractValidationError) as error:
            app.query("session.summaries", {"workflow_definition_id": identity})
        assert error.value.reason_code == "invalid_request"
    with pytest.raises(ContractValidationError) as error:
        app.query("session.summaries", {})
    assert error.value.reason_code == "invalid_request"
    # The service entries themselves also reject non-canonical identities and
    # non-boolean filters when called directly, not only through the contract.
    with pytest.raises(ContractValidationError) as error:
        service.session_summaries("not-a-uuid")
    assert error.value.reason_code == "invalid_request"
    with pytest.raises(ContractValidationError) as error:
        service.definition_summaries(include_archived=1)
    assert error.value.reason_code == "invalid_request"
