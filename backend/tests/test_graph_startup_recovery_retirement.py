import json
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event, Thread

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.graph_service import GraphWorkflowService
from test_graph_service import gate_graph


def _business_rows(database):
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {
            name: tuple(tuple(row) for row in connection.execute(
                f'SELECT * FROM "{name}" ORDER BY rowid'))
            for name in names
        }


def _record(database, record_type, identity):
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
        row = connection.execute(
            "SELECT payload FROM records WHERE record_type=? AND record_id=?",
            (record_type, identity),
        ).fetchone()
    return json.loads(row[0])


def test_reopening_after_unfinished_run_does_not_reconcile_business_records(tmp_path, monkeypatch):
    database = tmp_path / "unfinished.sqlite"
    registry = create_package_registry().registry.detached()
    service = GraphWorkflowService(database, registry=registry)
    close_requested = Event()
    close_thread = None
    session_id = chain_id = None
    release = None
    try:
        session_id, chain_id, release = gate_graph(service)
        running = _record(database, "chain_run", chain_id)
        assert running["status"] == "running"
        assert _record(database, "workflow_session", session_id)["active_chain_run_id"] == chain_id

        original_control = service._control_hosted_invocation

        def observe_shutdown_control(identity, action, command_id):
            result = original_control(identity, action, command_id)
            if action == "pause":
                close_requested.set()
            return result

        monkeypatch.setattr(service, "_control_hosted_invocation", observe_shutdown_control)

        close_thread = Thread(target=service.close, daemon=True)
        close_thread.start()
        assert close_requested.wait(5)
        release.set()
        close_thread.join(10)
        assert not close_thread.is_alive()

        settled = _record(database, "chain_run", chain_id)
        assert settled["status"] == "paused"
        assert len(settled["completed_nodes"]) < len(settled["ordered_nodes"])
        assert _record(database, "workflow_session", session_id)["active_chain_run_id"] == chain_id

        before_reopen = _business_rows(database)
        assert not any(row[0] == "graph.run.recover" for row in before_reopen["idempotency"])
        manifests = before_reopen["graph_state_manifests"]
        assert not any(json.loads(row[2]).get("status") == "recovery_unavailable" for row in manifests)

        reopened_registry = create_package_registry().registry.detached()
        with closing(GraphWorkflowService(database, registry=reopened_registry)) as reopened:
            assert reopened._execution_errors == {}
            assert _business_rows(database) == before_reopen
    finally:
        if release is not None:
            release.set()
        if close_thread is not None:
            close_thread.join(10)
        elif not service._closed:
            service.close()
