"""S1-01: per-workflow modification gate, busy rejection and storage-busy mapping.

The gate is process-wide over one database file: a real definition modification
in flight rejects a second modification immediately (no queue, no retry) and the
same busy state is visible to every service instance in this process.  Definition
archive/delete coordinates with the same entitlement and re-checks the durable
revision/state inside its write transaction.  SQLite lock acquisition failures
surface as one explicit storage-busy error instead of an unexplained 500.
"""

from __future__ import annotations

from contextlib import closing, nullcontext
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread
import sqlite3

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_operations import FrontendNodeOperations
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent import workflow_modification_gate as gate_module

from frontend_node_test_support import graph as frontend_graph
from frontend_node_test_support import save as save_frontend_graph
from test_graph_service import text_graph


def _read_only_scalar(database, query, parameters=()):
    """Read one scalar from the shared file without taking a write lock."""
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute(query, parameters).fetchone()
    return None if row is None else row[0]


def _registry():
    return create_package_registry().registry.detached()


def _editable(service, graph):
    """Promote one schema-2 test graph into a complete schema-3 editable document."""
    graph["schema_version"] = 3
    graph.setdefault("package_lock", list(service.registry.execution_package_lock))
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    return graph


def _fixed_change(document):
    """Return a real fixed modification of an accepted document (a saved edit)."""
    changed = deepcopy(document)
    assert changed["edges"], "the test graph needs an edge for a fixed modification"
    changed["edges"] = changed["edges"][:-1]
    return changed


def _save(service, graph, sequence, key=None):
    return service.update_definition(
        deepcopy(graph), workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=sequence, idempotency_key=key or str(uuid4()))


def _rejected(call, reason_code):
    with pytest.raises(ContractValidationError) as error:
        call()
    assert error.value.reason_code == reason_code, error.value.reason_code
    assert error.value.status_code == 409
    return error.value


class _BlockingCommit:
    """Fault injector that parks one command inside its write transaction."""

    def __init__(self):
        self.entered, self.release, self.blocking = Event(), Event(), Event()

    def __call__(self, point):
        if point == "before_commit" and self.blocking.is_set():
            self.entered.set()
            assert self.release.wait(10), "modification gate test timed out"


def _service(tmp_path, name="modification-gate.sqlite", **options):
    return GraphWorkflowService(tmp_path / name, registry=_registry(), **options)


def test_same_workflow_second_modification_is_rejected_and_not_queued(tmp_path):
    injector = _BlockingCommit()
    service = _service(tmp_path, fault_injector=injector)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        first = _save(service, graph, 0, "first")
        assert (first["outcome"], first["edit_sequence"], first["definition_revision"]) == ("saved", 1, 1)
        proposed = _fixed_change(first["document"])

        injector.blocking.set()
        accepted = {}

        def modify():
            accepted["result"] = _save(service, proposed, 1, "second")

        worker = Thread(target=modify)
        worker.start()
        try:
            assert injector.entered.wait(10), "first modification never reached its commit"
            error = _rejected(lambda: _save(service, proposed, 1, "busy"),
                              "workflow_modification_busy")
            assert error.diagnostics[0]["workflow_definition_id"] == identity
            # The busy rejection ran nothing: the first command is still parked.
            assert worker.is_alive()
        finally:
            injector.release.set()
        worker.join(10)
        assert not worker.is_alive() and "result" in accepted
        assert accepted["result"]["outcome"] == "saved"
        assert accepted["result"]["edit_sequence"] == 2

        with closing(service._store()) as store:
            assert store.read_receipt("graph.definition.update", "busy") is None
        assert service.get_definition(identity)["edit_sequence"] == 2
        # The rejected caller must present the original baseline again; the gate
        # does not hand out a newer basis.
        _rejected(lambda: _save(service, proposed, 1, "stale"), "stale_edit_sequence")
    finally:
        injector.release.set()
        service.close()


def test_two_service_instances_share_the_process_wide_busy_state(tmp_path):
    path = tmp_path / "shared-database.sqlite"
    execution = GraphWorkflowService(path, registry=_registry())
    manager = GraphManagementService(path)
    try:
        graph = _editable(execution, text_graph(execution.registry))
        identity = graph["workflow_definition_id"]
        first = _save(execution, graph, 0, "first")
        proposed = _fixed_change(first["document"])

        with gate_module.modification_gate(path, identity):
            # The management instance sees the entitlement held by the execution
            # instance; the rejection is immediate, not a separate local state.
            _rejected(lambda: _save(manager, proposed, 1, "busy"),
                      "workflow_modification_busy")
            # A different workflow owns a different entitlement and proceeds.
            other = _editable(manager, text_graph(manager.registry))
            other_saved = _save(manager, other, 0, "other")
            assert other_saved["outcome"] == "saved"
            assert other_saved["workflow_definition_id"] == other["workflow_definition_id"]

        # After the entitlement is released the queued-out edit can commit on its
        # original basis through the other instance.
        after = _save(manager, proposed, 1, "after-release")
        assert (after["outcome"], after["edit_sequence"], after["definition_revision"]) == ("saved", 2, 2)
        assert execution.get_definition(identity)["edit_sequence"] == 2
    finally:
        manager.close()
        execution.close()


def test_definition_archive_coordinates_with_edits(tmp_path, monkeypatch):
    service = _service(tmp_path, name="archive-gate.sqlite")
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        saved = _save(service, graph, 0, "first")

        entered, release = Event(), Event()

        def blocking_stop(sessions, key):
            entered.set()
            assert release.wait(10), "archive stop gate timed out"

        monkeypatch.setattr(service, "_stop_owned_runs", blocking_stop)
        archived = {}

        def archive():
            archived["result"] = service.archive_definition(
                identity, expected_revision=1, idempotency_key="archive")

        worker = Thread(target=archive)
        worker.start()
        try:
            assert entered.wait(10), "archive never entered its coordination window"
            # While the archive owns the entitlement a real edit is rejected.
            _rejected(lambda: _save(service, _fixed_change(saved["document"]), 1, "edit-during-archive"),
                      "workflow_modification_busy")
        finally:
            release.set()
        worker.join(10)
        assert not worker.is_alive() and archived["result"]["archived"] is True

        # After the archive committed the durable state is the final authority.
        _rejected(lambda: _save(service, saved["document"], 1, "edit-after-archive"),
                  "workflow_archived")
        # Re-archiving with a new key is checked against the durable archived
        # state inside the write transaction instead of silently repeating.
        _rejected(lambda: service.archive_definition(
            identity, expected_revision=1, idempotency_key="archive-again"), "workflow_archived")
    finally:
        service.close()


def test_definition_delete_is_rejected_while_an_edit_is_in_flight(tmp_path):
    injector = _BlockingCommit()
    service = _service(tmp_path, name="delete-gate.sqlite", fault_injector=injector)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        first = _save(service, graph, 0, "first")
        proposed = _fixed_change(first["document"])
        injector.blocking.set()
        accepted = {}

        def modify():
            accepted["result"] = _save(service, proposed, 1, "second")

        worker = Thread(target=modify)
        worker.start()
        try:
            assert injector.entered.wait(10)
            _rejected(lambda: service.delete_definition(
                identity, expected_revision=1, idempotency_key="delete-busy"),
                "workflow_modification_busy")
        finally:
            injector.release.set()
        worker.join(10)
        assert accepted["result"]["outcome"] == "saved"
        deleted = service.delete_definition(identity, expected_revision=2, idempotency_key="delete")
        assert deleted["deleted"] is True
        assert service.list_definitions(include_archived=True) == []
    finally:
        injector.release.set()
        service.close()


def test_archive_rechecks_expected_revision_inside_the_write_transaction(tmp_path, monkeypatch):
    service = _service(tmp_path, name="transaction-recheck.sqlite")
    path = service.database
    manager = GraphManagementService(path)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        saved = _save(service, graph, 0, "first")
        assert saved["definition_revision"] == 1

        # A separate process cannot see this process's in-memory gate.  Bypass it
        # here to prove the write transaction itself re-checks the durable
        # revision before archiving.
        monkeypatch.setattr(
            gate_module, "modification_gate",
            lambda database, workflow_definition_id: nullcontext())
        edited = {}

        def foreign_process_edit():
            edited["result"] = manager.update_definition(
                _fixed_change(saved["document"]), workflow_definition_id=identity,
                expected_edit_sequence=1, idempotency_key="foreign-edit")
            return edited["result"]

        monkeypatch.setattr(service, "_stop_owned_runs",
                            lambda sessions, key: foreign_process_edit())

        _rejected(lambda: service.archive_definition(
            identity, expected_revision=1, idempotency_key="stale-archive"), "stale_revision")
        assert edited["result"]["outcome"] == "saved"
        assert edited["result"]["definition_revision"] == 2
        rows = manager.list_definitions(include_archived=True)
        assert len(rows) == 1 and rows[0]["archived"] is False
        assert rows[0]["document"]["revision"] == 2
    finally:
        manager.close()
        service.close()


def test_storage_busy_is_reported_as_409_not_an_internal_failure(tmp_path, monkeypatch):
    service = _service(tmp_path, name="storage-busy.sqlite")
    try:
        graph = _editable(service, text_graph(service.registry))
        _save(service, graph, 0, "initial")
        proposed = _fixed_change(graph)

        def busy_open(**kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(service.environment, "open_store", busy_open)
        _rejected(lambda: _save(service, proposed, 1, "busy-open"), "graph_storage_busy")

        # Only the busy/locked shape is rewritten; other OperationalError values
        # keep their original meaning.
        def broken_open(**kwargs):
            raise sqlite3.OperationalError("no such table: records")

        monkeypatch.setattr(service.environment, "open_store", broken_open)
        with pytest.raises(sqlite3.OperationalError) as broken:
            _save(service, proposed, 1, "broken-open")
        assert "no such table" in str(broken.value)

        # A real SQLite lock refusal during storage acquisition follows the same
        # mapping instead of surfacing through the generic 500 path.
        monkeypatch.undo()
        real_connect = sqlite3.connect
        holder = real_connect(service.database, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            monkeypatch.setattr(
                sqlite3, "connect",
                lambda *args, **kwargs: real_connect(*args, **{**kwargs, "timeout": 0}))
            _rejected(lambda: _save(service, proposed, 1, "locked-store"), "graph_storage_busy")
        finally:
            holder.execute("ROLLBACK")
            holder.close()
    finally:
        service.close()


def test_exact_replay_is_allowed_while_another_modification_is_in_flight(tmp_path):
    injector = _BlockingCommit()
    service = _service(tmp_path, name="replay-gate.sqlite", fault_injector=injector)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        first = _save(service, graph, 0, "replay-key")
        assert (first["outcome"], first["edit_sequence"]) == ("saved", 1)
        proposed = _fixed_change(first["document"])

        injector.blocking.set()
        accepted = {}

        def modify():
            accepted["result"] = _save(service, proposed, 1, "second")

        worker = Thread(target=modify)
        worker.start()
        try:
            assert injector.entered.wait(10), "modification never reached its commit"
            # Reading the accepted result is not a second real modification: the
            # exact replay is served from the persisted receipt, without the
            # entitlement and without another business write.
            assert _save(service, graph, 0, "replay-key") == first
            assert _read_only_scalar(
                service.database,
                "SELECT COUNT(*) FROM idempotency WHERE operation='graph.definition.update'") == 1
            # A different body with the same key keeps the existing conflict even
            # while the workflow is busy.
            _rejected(lambda: _save(service, proposed, 1, "replay-key"), "idempotency_conflict")
            # A real new modification still gets the explicit busy rejection.
            _rejected(lambda: _save(service, proposed, 1, "busy-new"), "workflow_modification_busy")
        finally:
            injector.release.set()
        worker.join(10)
        assert accepted["result"]["edit_sequence"] == 2
        assert _read_only_scalar(
            service.database,
            "SELECT COUNT(*) FROM idempotency WHERE operation='graph.definition.update'") == 2
    finally:
        injector.release.set()
        service.close()


def test_exact_narrow_patch_replay_is_allowed_while_a_modification_is_in_flight(tmp_path):
    injector = _BlockingCommit()
    service = _service(tmp_path, name="patch-replay-gate.sqlite", fault_injector=injector)
    try:
        value, _messages, frontends = frontend_graph(service, bound=True)
        saved = save_frontend_graph(service, value)
        identity = saved["workflow_definition_id"]
        bound = FrontendNodeOperations(service).bind(
            workflow_definition_id=identity, definition_revision=saved["revision"],
            node_binding_id=frontends[0]["node_binding_id"], session_id=None)
        first = bound.patch_input("A", expected_edit_sequence=1, idempotency_key="patch-key")
        assert (first["outcome"], first["edit_sequence"]) == ("saved", 2)
        proposed = _fixed_change(service.get_definition(identity))

        injector.blocking.set()
        accepted = {}

        def modify():
            accepted["result"] = _save(service, proposed, 2, "second")

        worker = Thread(target=modify)
        worker.start()
        try:
            assert injector.entered.wait(10), "modification never reached its commit"
            replay = bound.patch_input("A", expected_edit_sequence=1, idempotency_key="patch-key")
            assert replay == first
            assert _read_only_scalar(
                service.database,
                "SELECT COUNT(*) FROM idempotency "
                "WHERE operation='graph.frontend-node.patch-input'") == 1
            _rejected(lambda: bound.patch_input(
                "B", expected_edit_sequence=1, idempotency_key="patch-key"), "idempotency_conflict")
            _rejected(lambda: bound.patch_input(
                "C", expected_edit_sequence=2, idempotency_key="patch-new"), "workflow_modification_busy")
        finally:
            injector.release.set()
        worker.join(10)
        assert accepted["result"]["edit_sequence"] == 3
        assert _read_only_scalar(
            service.database,
            "SELECT COUNT(*) FROM idempotency "
            "WHERE operation='graph.frontend-node.patch-input'") == 1
    finally:
        injector.release.set()
        service.close()
