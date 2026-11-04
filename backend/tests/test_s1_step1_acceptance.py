"""Independent verifier acceptance checks for step 1 backend behavior.

Owned by the independent verifier; independent of the implementation team's own
tests.  Covers the backend parts of V1-01 (same-instance and process-shared busy
rejection, old-baseline CAS, gate release, archive transaction re-check),
V1-02 (display/adjustable changes invalidate the old edit baseline) and V1-04
(arbitration of unknown configuration requests and durable evidence facts).

Every test uses an isolated temporary SQLite database.  No product service,
business database or model call is used.
"""

from __future__ import annotations

from contextlib import closing, nullcontext
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread
import sqlite3
import time
from uuid import uuid4

import pytest

from phase1_agent import workflow_modification_gate as gate_module
from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore

from test_graph_service import text_graph
from test_workflow_configuration import (
    document as adjustable_document,
    registry as adjustable_registry,
    update as adjustable_update,
)

UPDATE = "definition.update"
UPDATE_OWNER = "workflow-configuration"
NATIVE_UPDATE = "graph.definition.update"
ABANDON = "graph.request.abandon"
ABANDONED_PREFIX = "graph.request.abandoned.v1:"


# ---------------------------------------------------------------------------
# local fixtures (independent of the implementation team's helper modules)
# ---------------------------------------------------------------------------

def _registry():
    return create_package_registry().registry.detached()


def _editable(service, graph):
    """Promote a schema-2 fixture graph into one complete schema-3 document."""
    value = deepcopy(graph)
    value["schema_version"] = 3
    value.setdefault("package_lock", list(service.registry.execution_package_lock))
    for entry in value["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    return value


def _fixed_change(document):
    changed = deepcopy(document)
    assert changed["edges"], "the fixture graph needs an edge"
    changed["edges"] = changed["edges"][:-1]
    return changed


def _fixed_config_change(document, suffix):
    """A real fixed-category change that does not consume graph edges."""
    changed = deepcopy(document)
    changed["nodes"][0]["config"]["text"] = f"apple apple {suffix}"
    return changed


def _save(service, graph, sequence, key):
    return service.update_definition(
        deepcopy(graph), workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=sequence, idempotency_key=key)


def _rejected(call, code, *, status=409):
    with pytest.raises(ContractValidationError) as error:
        call()
    assert error.value.reason_code == code, error.value.reason_code
    assert error.value.status_code == status, error.value.status_code
    return error.value


def _service(tmp_path, name):
    return GraphWorkflowService(tmp_path / name, registry=_registry())


def _park_first_change(monkeypatch):
    """Block the first command inside its write transaction after its change.

    The patch is test-side only: it parks the first call to the repository's
    inside-transaction validation so a real BEGIN IMMEDIATE write transaction
    stays open while the test issues the competing call.
    """
    entered, release = Event(), Event()
    original = GraphRecordStore._validate_changes
    state = {"block": True}

    def validate(store_self):
        if state["block"]:
            state["block"] = False
            entered.set()
            assert release.wait(10), "verifier modification-gate test timed out"
        return original(store_self)

    monkeypatch.setattr(GraphRecordStore, "_validate_changes", validate)
    return entered, release


def _readonly(database, sql, parameters=()):
    """Read the durable database snapshot without opening a writer."""
    with closing(sqlite3.connect(
            Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]


def _receipt_count(database, operation):
    return _readonly(
        database, "SELECT COUNT(*) AS n FROM idempotency WHERE operation=?",
        (operation,))[0]["n"]


def _evidence(app, operation, identity, owner_kind, request):
    return app.command("request-evidence.write", {
        "operation": operation, "idempotency_key": request["idempotency_key"],
        "owner_kind": owner_kind, "owner_id": identity,
        "request": deepcopy(request)})["result"]


def _abandon(app, identity, operation, target_key, target_digest, key):
    return app.command("request.abandon", {
        "workflow_definition_id": identity, "target_operation": operation,
        "target_idempotency_key": target_key, "target_request_digest": target_digest,
        "idempotency_key": key})


def _evidence_resolution(database, operation, key):
    return _readonly(
        database, "SELECT resolution FROM pending_commands WHERE operation=? AND idempotency_key=?",
        (operation, key))[0]["resolution"]


def _update_request(graph, key, sequence=0):
    return {"workflow_definition_id": graph["workflow_definition_id"],
            "document": deepcopy(graph), "expected_edit_sequence": sequence,
            "idempotency_key": key}


# ---------------------------------------------------------------------------
# V1-01: in-flight mutual exclusion, explicit busy rejection, old-baseline CAS
# ---------------------------------------------------------------------------

def test_s1_v1_01_same_instance_busy_is_rejected_immediately_and_old_baseline_fails(
        tmp_path, monkeypatch):
    service = _service(tmp_path, "v1-01-same-instance.sqlite")
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        first = _save(service, graph, 0, "first")
        assert (first["outcome"], first["edit_sequence"], first["definition_revision"]) == (
            "saved", 1, 1)
        proposed = _fixed_change(first["document"])

        entered, release = _park_first_change(monkeypatch)
        outcome = {}

        def modify():
            outcome["result"] = _save(service, proposed, 1, "second")

        worker = Thread(target=modify)
        worker.start()
        try:
            assert entered.wait(10), "first modification never entered its transaction"
            started = time.monotonic()
            error = _rejected(
                lambda: _save(service, proposed, 1, "busy"), "workflow_modification_busy")
            elapsed = time.monotonic() - started
            # No queue, no retry: the rejection happens while the first
            # transaction is still parked.
            assert elapsed < 2.0, f"busy rejection waited {elapsed:.2f}s"
            assert error.diagnostics[0]["workflow_definition_id"] == identity
            assert worker.is_alive(), "the parked first modification ended unexpectedly"
        finally:
            release.set()
        worker.join(10)
        assert not worker.is_alive() and "result" in outcome
        assert (outcome["result"]["outcome"], outcome["result"]["edit_sequence"],
                outcome["result"]["definition_revision"]) == ("saved", 2, 2)

        # The rejected command ran nothing.
        assert _receipt_count(service.database, NATIVE_UPDATE) == 2
        assert _readonly(
            service.database,
            "SELECT COUNT(*) AS n FROM idempotency WHERE operation=? AND key=?",
            (NATIVE_UPDATE, "busy"))[0]["n"] == 0
        assert service.get_definition(identity)["edit_sequence"] == 2

        # A rejected caller must present the original baseline again; the gate
        # does not hand out the newer basis.
        _rejected(lambda: _save(service, proposed, 1, "stale"), "stale_edit_sequence")
        assert _readonly(
            service.database,
            "SELECT COUNT(*) AS n FROM idempotency WHERE operation=? AND key=?",
            (NATIVE_UPDATE, "stale"))[0]["n"] == 0
        assert service.get_definition(identity)["edit_sequence"] == 2
    finally:
        service.close()


def test_s1_v1_01_two_instances_share_busy_state_and_other_workflow_gate_is_free(
        tmp_path, monkeypatch):
    path = tmp_path / "v1-01-shared-database.sqlite"
    execution = GraphWorkflowService(path, registry=_registry())
    manager = GraphManagementService(path)
    try:
        graph = _editable(execution, text_graph(execution.registry))
        identity = graph["workflow_definition_id"]
        first = _save(execution, graph, 0, "first")
        proposed = _fixed_change(first["document"])
        other_identity = str(uuid4())

        entered, release = _park_first_change(monkeypatch)
        outcome = {}

        def modify():
            outcome["result"] = _save(execution, proposed, 1, "second")

        worker = Thread(target=modify)
        worker.start()
        try:
            assert entered.wait(10), "first modification never entered its transaction"
            # The second instance observes the first instance's entitlement.
            _rejected(lambda: _save(manager, proposed, 1, "manager-busy"),
                      "workflow_modification_busy")
            # The entitlement is per workflow: another workflow is not blocked.
            with gate_module.modification_gate(path, other_identity):
                pass
        finally:
            release.set()
        worker.join(10)
        assert not worker.is_alive()

        second = outcome["result"]
        assert (second["edit_sequence"], second["definition_revision"]) == (2, 2)
        assert manager.get_definition(identity)["edit_sequence"] == 2
        assert execution.get_definition(identity)["edit_sequence"] == 2

        # Another instance can still read/validate the same durable baseline and
        # must present it exactly; the new baseline was not handed out.
        _rejected(lambda: _save(manager, proposed, 1, "manager-stale"),
                  "stale_edit_sequence")
        next_document = _fixed_change(second["document"])
        accepted = _save(manager, next_document, 2, "manager-after")
        assert (accepted["outcome"], accepted["edit_sequence"],
                accepted["definition_revision"]) == ("saved", 3, 3)
        assert execution.get_definition(identity)["edit_sequence"] == 3
    finally:
        manager.close()
        execution.close()


def test_s1_v1_01_gate_is_released_after_rejection_and_storage_busy_is_mapped(
        tmp_path, monkeypatch):
    service = _service(tmp_path, "v1-01-release.sqlite")
    try:
        graph = _editable(service, text_graph(service.registry))
        first = _save(service, graph, 0, "first")
        assert first["edit_sequence"] == 1

        # A rejected validation still releases the entitlement.
        bad_basis = deepcopy(first["document"])
        bad_basis["edit_sequence"] = 42
        _rejected(lambda: _save(service, bad_basis, 1, "rejected-basis"), "invalid_request",
                  status=400)
        after_reject = _save(service, _fixed_change(first["document"]), 1, "after-reject")
        assert after_reject["outcome"] == "saved" and after_reject["edit_sequence"] == 2

        original_open = service.environment.open_store

        def busy_open(**options):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(service.environment, "open_store", busy_open)
        _rejected(lambda: _save(service, _fixed_change(after_reject["document"]), 2,
                                "busy-storage"), "graph_storage_busy")

        # Only the true busy/locked shape is remapped; other SQLite errors keep
        # their original meaning instead of becoming a fake busy 409.
        def broken_open(**options):
            raise sqlite3.OperationalError("no such table: records")

        monkeypatch.setattr(service.environment, "open_store", broken_open)
        with pytest.raises(sqlite3.OperationalError) as broken:
            _save(service, _fixed_change(after_reject["document"]), 2, "broken-storage")
        assert "no such table" in str(broken.value)

        monkeypatch.setattr(service.environment, "open_store", original_open)
        after_busy = _save(service, _fixed_change(after_reject["document"]), 2, "after-busy")
        assert after_busy["outcome"] == "saved" and after_busy["edit_sequence"] == 3
    finally:
        service.close()


def test_s1_v1_01_definition_archive_transaction_rechecks_and_delete_rejects_busy(
        tmp_path, monkeypatch):
    path = tmp_path / "v1-01-lifecycle.sqlite"
    service = GraphWorkflowService(path, registry=_registry())
    manager = GraphManagementService(path)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        first = _save(service, graph, 0, "first")
        second = _save(service, _fixed_change(first["document"]), 1, "second")
        assert (second["edit_sequence"], second["definition_revision"]) == (2, 2)

        # A definition delete is refused immediately while a real edit owns the
        # entitlement (no queue).
        entered, release = _park_first_change(monkeypatch)
        outcome = {}

        def modify():
            outcome["result"] = _save(service, _fixed_config_change(second["document"], "third"),
                                      2, "third")

        worker = Thread(target=modify)
        worker.start()
        try:
            assert entered.wait(10)
            _rejected(lambda: service.delete_definition(
                identity, expected_revision=2, idempotency_key="delete-busy"),
                "workflow_modification_busy")
        finally:
            release.set()
        worker.join(10)
        assert not worker.is_alive()
        third = outcome["result"]
        assert (third["edit_sequence"], third["definition_revision"]) == (3, 3)

        # Simulate a foreign process edit: it cannot share the in-memory gate,
        # so the write transaction itself must re-check the durable revision.
        monkeypatch.setattr(
            gate_module, "modification_gate",
            lambda database, workflow_definition_id: nullcontext())
        foreign = {}

        def foreign_edit(sessions, key):
            foreign["result"] = manager.update_definition(
                _fixed_config_change(third["document"], "foreign"),
                workflow_definition_id=identity,
                expected_edit_sequence=3, idempotency_key="foreign-edit")

        original_stop = service._stop_owned_runs
        monkeypatch.setattr(service, "_stop_owned_runs", foreign_edit)
        _rejected(lambda: service.archive_definition(
            identity, expected_revision=3, idempotency_key="stale-archive"), "stale_revision")
        assert foreign["result"]["outcome"] == "saved"
        assert foreign["result"]["definition_revision"] == 4
        rows = manager.list_definitions(include_archived=True)
        assert len(rows) == 1 and rows[0]["archived"] is False
        assert rows[0]["document"]["revision"] == 4

        monkeypatch.setattr(service, "_stop_owned_runs", original_stop)
        archived = service.archive_definition(
            identity, expected_revision=4, idempotency_key="archive-current")
        assert archived["archived"] is True
    finally:
        manager.close()
        service.close()


def test_s1_v1_01_exact_replay_stays_available_while_another_edit_is_in_flight(
        tmp_path, monkeypatch):
    """R1: reading an accepted result is not a second real modification.

    An exact replay of an already committed request must return the original
    result even while another modification owns the entitlement; a real new
    modification is still rejected as busy, and the replay adds no receipt.
    """
    service = _service(tmp_path, "v1-01-replay-busy.sqlite")
    try:
        graph = _editable(service, text_graph(service.registry))
        accepted = _save(service, graph, 0, "accepted-replay")
        assert accepted["outcome"] == "saved"
        proposed = _fixed_change(accepted["document"])
        assert _receipt_count(service.database, NATIVE_UPDATE) == 1

        entered, release = _park_first_change(monkeypatch)
        result = {}

        def modify():
            result["result"] = _save(service, proposed, 1, "in-flight")

        worker = Thread(target=modify)
        worker.start()
        try:
            assert entered.wait(10), "in-flight modification never entered its transaction"
            # The exact replay is a read of the committed result, not a write:
            # it succeeds while the other modification is parked.
            replay = _save(service, graph, 0, "accepted-replay")
            assert replay == accepted
            # A same-key different-body request keeps the existing conflict.
            _rejected(lambda: _save(service, proposed, 0, "accepted-replay"),
                      "idempotency_conflict")
            # A real new modification is still rejected as busy, never queued.
            _rejected(lambda: _save(service, proposed, 1, "new-busy"),
                      "workflow_modification_busy")
            # The replay wrote no receipt and did not change the durable state.
            assert _receipt_count(service.database, NATIVE_UPDATE) == 1
        finally:
            release.set()
        worker.join(10)
        assert not worker.is_alive() and "result" in result
        assert (result["result"]["outcome"], result["result"]["edit_sequence"]) == ("saved", 2)
        assert _receipt_count(service.database, NATIVE_UPDATE) == 2
        # After the entitlement is released the same replay still returns the
        # original accepted result, not the newer state.
        assert _save(service, graph, 0, "accepted-replay") == accepted
        assert _receipt_count(service.database, NATIVE_UPDATE) == 2
    finally:
        service.close()


# ---------------------------------------------------------------------------
# V1-02: display-only / adjustable changes invalidate the old edit baseline
# ---------------------------------------------------------------------------

def test_s1_v1_02_display_only_change_advances_sequence_and_rejects_old_baseline(tmp_path):
    service = _service(tmp_path, "v1-02-display.sqlite")
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        first = _save(service, graph, 0, "first")
        assert (first["outcome"], first["edit_sequence"], first["definition_revision"]) == (
            "saved", 1, 1)

        display = deepcopy(first["document"])
        display["name"] = "display rename only"
        display["nodes"][0]["position"] = {"x": 999, "y": 111}
        displayed = _save(service, display, 1, "display-change")
        assert displayed["outcome"] == "saved"
        assert displayed["changed_categories"] == ["display"], displayed["changed_categories"]
        # A display change must not advance the fixed revision ...
        assert displayed["definition_revision"] == 1
        # ... but it must advance the edit sequence.
        assert displayed["edit_sequence"] == 2

        # The old edit baseline (old body, edit_sequence 1) must now fail even
        # though the fixed revision did not move.
        _rejected(lambda: _save(service, deepcopy(first["document"]), 1, "old-basis"),
                  "stale_edit_sequence")

        current = service.get_definition(identity)
        assert current["revision"] == 1
        assert current["name"] == "display rename only"
        assert current["nodes"][0]["position"] == {"x": 999, "y": 111}
        assert _readonly(
            service.database,
            "SELECT COUNT(*) AS n FROM idempotency WHERE operation=? AND key=?",
            (NATIVE_UPDATE, "old-basis"))[0]["n"] == 0
    finally:
        service.close()


def test_s1_v1_02_adjustable_only_change_keeps_fixed_revision_and_rejects_old_baseline(tmp_path):
    service = GraphWorkflowService(
        tmp_path / "v1-02-adjustable.sqlite", registry=adjustable_registry())
    try:
        graph = adjustable_document()
        identity = graph["workflow_definition_id"]
        first = adjustable_update(service, graph, 0, "first")
        assert (first["outcome"], first["definition_revision"], first["edit_sequence"]) == (
            "saved", 1, 1)

        changed = deepcopy(first["document"])
        changed["nodes"][0]["config"]["text"] = "B"
        adjustable = adjustable_update(service, changed, 1, "adjustable-change")
        assert adjustable["outcome"] == "saved"
        assert adjustable["changed_categories"] == ["adjustable"], adjustable["changed_categories"]
        assert adjustable["definition_revision"] == 1
        assert adjustable["edit_sequence"] == 2

        _rejected(lambda: adjustable_update(service, deepcopy(first["document"]), 1,
                                            "old-basis"), "stale_edit_sequence")
        current = service.get_definition(identity)
        assert current["nodes"][0]["config"]["text"] == "B"
        assert _readonly(
            service.database,
            "SELECT COUNT(*) AS n FROM idempotency WHERE operation=? AND key=?",
            (NATIVE_UPDATE, "old-basis"))[0]["n"] == 0
    finally:
        service.close()


# ---------------------------------------------------------------------------
# V1-04 backend: arbitration of unknown configuration requests
# ---------------------------------------------------------------------------

def test_s1_v1_04_uncommitted_request_is_abandoned_and_late_request_is_refused(tmp_path):
    service = _service(tmp_path, "v1-04-abandoned.sqlite")
    app = GraphApplication(service)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        request = _update_request(graph, "business-key")
        evidence = _evidence(app, UPDATE, identity, UPDATE_OWNER, request)
        digest = evidence["request_digest"]
        assert _evidence_resolution(service.database, UPDATE, "business-key") == "pending"

        arbitration = _abandon(app, identity, UPDATE, "business-key", digest,
                               "abandon-key")["result"]
        assert (arbitration["outcome"], arbitration["evidence_resolution"],
                arbitration["result"]) == ("abandoned", "archived", None)
        assert arbitration["workflow_definition_id"] == identity
        assert arbitration["request_digest"] == digest

        # Durable facts, read-only snapshot.
        assert _evidence_resolution(service.database, UPDATE, "business-key") == "archived"
        abandoned = _readonly(
            service.database,
            "SELECT key,digest,request_digest FROM idempotency WHERE operation=?",
            (ABANDONED_PREFIX + NATIVE_UPDATE,))
        assert len(abandoned) == 1
        assert abandoned[0]["key"] == "business-key"
        assert abandoned[0]["request_digest"].startswith("workflow-op-v1:")
        assert abandoned[0]["request_digest"] == abandoned[0]["digest"]
        assert _receipt_count(service.database, NATIVE_UPDATE) == 0
        assert _receipt_count(service.database, ABANDON) == 1

        # The late original request cannot execute through the application
        # command contract ...
        late = {}
        with pytest.raises(ContractValidationError) as late_error:
            app.command(UPDATE, deepcopy(request))
        late["code"] = late_error.value.reason_code
        assert late["code"] == "request_abandoned"
        # ... nor through the direct native service entry.
        _rejected(lambda: service.update_definition(
            deepcopy(request["document"]), workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key="business-key"), "request_abandoned")
        # The same key with a different body is an idempotency conflict, not an
        # execution and not a silent overwrite.
        altered = deepcopy(request)
        altered["document"] = _fixed_change(altered["document"])
        with pytest.raises(ContractValidationError) as conflict:
            app.command(UPDATE, altered)
        assert conflict.value.reason_code == "idempotency_conflict"
        assert _receipt_count(service.database, NATIVE_UPDATE) == 0
        # No business definition was ever written by the late request.
        assert _readonly(
            service.database,
            "SELECT COUNT(*) AS n FROM records WHERE record_type='workflow_definition_revision'",
        )[0]["n"] == 0

        # Replaying the arbitration command returns the stored arbitration.
        replay = _abandon(app, identity, UPDATE, "business-key", digest, "abandon-key")
        assert replay["result"] == arbitration
    finally:
        service.close()


def test_s1_v1_04_committed_request_returns_formal_result_without_reexecution(tmp_path):
    service = _service(tmp_path, "v1-04-committed.sqlite")
    app = GraphApplication(service)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        request = _update_request(graph, "business-key")
        digest = _evidence(app, UPDATE, identity, UPDATE_OWNER, request)["request_digest"]
        accepted = app.command(UPDATE, deepcopy(request))
        assert accepted["result"]["outcome"] == "saved"
        assert _receipt_count(service.database, NATIVE_UPDATE) == 1
        assert _evidence_resolution(service.database, UPDATE, "business-key") == "pending"

        arbitration = _abandon(app, identity, UPDATE, "business-key", digest,
                               "abandon-key")["result"]
        assert arbitration["outcome"] == "committed"
        assert arbitration["evidence_resolution"] == "verified"
        assert arbitration["result"] == accepted["result"]

        # No re-execution, no second business receipt.
        assert _receipt_count(service.database, NATIVE_UPDATE) == 1
        assert _evidence_resolution(service.database, UPDATE, "business-key") == "verified"
        # A later exact business replay still returns the accepted receipt.
        replayed = app.command(UPDATE, deepcopy(request))
        assert replayed["result"] == accepted["result"]
        assert _receipt_count(service.database, NATIVE_UPDATE) == 1
    finally:
        service.close()


def test_s1_v1_04_arbitration_waits_for_an_in_flight_request_and_returns_committed(
        tmp_path, monkeypatch):
    """User termination while the business request may still be writing.

    The arbitration transaction must serialize against the in-flight business
    transaction: if that request commits first the user gets its formal result
    (no re-execution); the termination must never race ahead and abandon a
    request that actually committed.
    """
    service = _service(tmp_path, "v1-04-inflight.sqlite")
    app = GraphApplication(service)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        request = _update_request(graph, "business-key")
        digest = _evidence(app, UPDATE, identity, UPDATE_OWNER, request)["request_digest"]

        entered, release = _park_first_change(monkeypatch)
        business = {}

        def run_business():
            business["result"] = app.command(UPDATE, deepcopy(request))

        business_thread = Thread(target=run_business)
        business_thread.start()
        try:
            assert entered.wait(10), "business request never entered its write transaction"
            arbitration = {}
            started, done = Event(), Event()

            def run_abandon():
                started.set()
                try:
                    arbitration["result"] = _abandon(
                        app, identity, UPDATE, "business-key", digest, "abandon-key")
                finally:
                    done.set()

            arbitration_thread = Thread(target=run_abandon)
            arbitration_thread.start()
            try:
                assert started.wait(5)
                # While the business transaction is parked the arbitration can
                # only wait for one terminal business fact, not guess.
                assert not done.wait(0.5), (
                    "arbitration did not wait for the in-flight write transaction")
            finally:
                release.set()
            business_thread.join(10)
            arbitration_thread.join(10)
            assert not business_thread.is_alive() and not arbitration_thread.is_alive()
        finally:
            release.set()

        assert business["result"]["result"]["outcome"] == "saved"
        committed = arbitration["result"]["result"]
        assert committed["outcome"] == "committed"
        assert committed["result"] == business["result"]["result"]
        assert _receipt_count(service.database, NATIVE_UPDATE) == 1
        assert _evidence_resolution(service.database, UPDATE, "business-key") == "verified"
    finally:
        service.close()


def test_s1_task7_r3_abandon_and_recovery_reads_bypass_package_publication_failure(tmp_path):
    """R3: termination/recovery reads stay available when package publication fails.

    A normal definition save still reports the 503 package-publication failure,
    but the user can still arbitrate an unknown request and read evidence.
    """
    service = _service(tmp_path, "task7-r3-publication.sqlite")
    app = GraphApplication(service)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        request = _update_request(graph, "business-key")
        digest = _evidence(app, UPDATE, identity, UPDATE_OWNER, request)["request_digest"]

        service._publication_failure = {"reason": "verifier-injected publication failure"}
        # A normal save is still gated by the failed package publication.
        _rejected(lambda: app.command(UPDATE, deepcopy(request)),
                  "package_publication_failed", status=503)
        # The recovery path stays available and completes the arbitration.
        assert app.query("request-evidence.read", {
            "owner_kind": UPDATE_OWNER, "owner_id": identity})[0]["resolution"] == "pending"
        arbitration = _abandon(app, identity, UPDATE, "business-key", digest,
                               "abandon-key")["result"]
        assert arbitration["outcome"] == "abandoned"
        assert _evidence_resolution(service.database, UPDATE, "business-key") == "archived"
        # Whatever entry the late request tries, the publication failure state or
        # the durable termination must prevent any business write.
        _rejected(lambda: app.command(UPDATE, deepcopy(request)),
                  "package_publication_failed", status=503)
        with pytest.raises(ContractValidationError):
            service.update_definition(
                deepcopy(request["document"]), workflow_definition_id=identity,
                expected_edit_sequence=0, idempotency_key="business-key")
        assert _receipt_count(service.database, NATIVE_UPDATE) == 0
        assert _readonly(
            service.database,
            "SELECT COUNT(*) AS n FROM records WHERE record_type='workflow_definition_revision'",
        )[0]["n"] == 0
    finally:
        service.close()


def test_s1_v1_04_wrong_digest_is_refused_without_terminating_the_request(tmp_path):
    service = _service(tmp_path, "v1-04-wrong-digest.sqlite")
    app = GraphApplication(service)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        request = _update_request(graph, "business-key")
        _evidence(app, UPDATE, identity, UPDATE_OWNER, request)

        _rejected(lambda: _abandon(app, identity, UPDATE, "business-key",
                                   "0" * 64, "abandon-key"), "request_evidence_conflict")
        assert _evidence_resolution(service.database, UPDATE, "business-key") == "pending"
        assert _receipt_count(service.database, ABANDONED_PREFIX + NATIVE_UPDATE) == 0
        # The original request was not terminated by the refused arbitration.
        accepted = app.command(UPDATE, deepcopy(request))
        assert accepted["result"]["outcome"] == "saved"
    finally:
        service.close()


def test_s1_v1_04_arbitration_unavailable_paths_leave_the_request_executable(tmp_path):
    service = _service(tmp_path, "v1-04-unavailable.sqlite")
    app = GraphApplication(service)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        request = _update_request(graph, "business-key")
        digest = _evidence(app, UPDATE, identity, UPDATE_OWNER, request)["request_digest"]

        # Missing evidence: the backend cannot bind the original contract, so it
        # must stay unknown instead of pretending a cancellation succeeded.
        _rejected(lambda: _abandon(app, identity, UPDATE, "unknown-key", "a" * 64,
                                   "abandon-missing"), "request_arbitration_unavailable")
        # Evidence belonging to another workflow cannot be arbitrated here.
        _rejected(lambda: _abandon(app, str(uuid4()), UPDATE, "business-key", digest,
                                   "abandon-foreign"), "request_arbitration_unavailable")
        # A non-arbitrable target operation is an invalid request.
        with pytest.raises(ContractValidationError) as invalid:
            _abandon(app, identity, "session.create", "business-key", digest, "abandon-op")
        assert invalid.value.reason_code == "invalid_request"
        assert _evidence_resolution(service.database, UPDATE, "business-key") == "pending"
        assert _receipt_count(service.database, ABANDONED_PREFIX + NATIVE_UPDATE) == 0

        # Nothing was terminated: the original request still executes.
        accepted = app.command(UPDATE, deepcopy(request))
        assert accepted["result"]["outcome"] == "saved"
    finally:
        service.close()


def test_s1_v1_04_arbitration_lock_refusal_is_an_explicit_storage_busy(tmp_path, monkeypatch):
    service = _service(tmp_path, "v1-04-storage-busy.sqlite")
    app = GraphApplication(service)
    try:
        graph = _editable(service, text_graph(service.registry))
        identity = graph["workflow_definition_id"]
        request = _update_request(graph, "business-key")
        digest = _evidence(app, UPDATE, identity, UPDATE_OWNER, request)["request_digest"]

        real_connect = sqlite3.connect
        holder = real_connect(service.database, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            monkeypatch.setattr(
                sqlite3, "connect",
                lambda *args, **options: real_connect(
                    *args, **{**options, "timeout": 0}))
            _rejected(lambda: _abandon(app, identity, UPDATE, "business-key", digest,
                                       "abandon-busy"), "graph_storage_busy")
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        monkeypatch.undo()

        # The refusal wrote nothing and the request is still arbitrable.
        assert _evidence_resolution(service.database, UPDATE, "business-key") == "pending"
        assert _receipt_count(service.database, ABANDON) == 0
        assert _receipt_count(service.database, ABANDONED_PREFIX + NATIVE_UPDATE) == 0
        arbitration = _abandon(app, identity, UPDATE, "business-key", digest,
                               "abandon-busy")["result"]
        assert arbitration["outcome"] == "abandoned"
    finally:
        service.close()
