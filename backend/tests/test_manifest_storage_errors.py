"""Persisted manifest decoding is a storage error, with original bytes retained."""

from contextlib import closing
from threading import Thread

import pytest

from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.server import create_server
from test_graph_receipt_http import post
from test_graph_service import create, text_graph
from test_workflow_run_order import service


@pytest.mark.parametrize("payload", ['{"private-secret-marker":', '{"x":1,"x":2}'])
@pytest.mark.parametrize("boundary", ["start", "end"])
def test_persisted_manifest_decode_is_safe_500(service, payload, boundary, caplog):
    initial = create(service, text_graph(service.registry))
    sid = initial["workflow_session_id"]
    started = service.start(sid, expected_revision=initial["revision"],
                            idempotency_key="manifest-read")
    chain_id = started["active_chain_run_id"]
    service.wait(chain_id)
    owner_kind = "chain_start" if boundary == "start" else "chain_boundary"
    with closing(service._store()) as store:
        row = store._connection.execute(
            "SELECT owner_id FROM graph_state_manifests WHERE owner_kind=?",
            (owner_kind,)).fetchone()
        owner_id = row["owner_id"]
        store._connection.execute(
            "UPDATE graph_state_manifests SET payload=? WHERE owner_kind=? AND owner_id=?",
            (payload, owner_kind, owner_id))
        store._connection.commit()
    application = GraphApplication(service)
    calls = [
        lambda: service.get_run(sid, chain_id),
        lambda: service.get_state_manifest(sid, chain_id=chain_id, boundary=boundary),
        lambda: application.query("run.read", {"session_id": sid, "chain_id": chain_id}),
        lambda: application.query("manifest.read", {
            "session_id": sid, "chain_id": chain_id, "boundary": boundary}),
    ]
    for call in calls:
        with pytest.raises(DiagnosticError) as raised:
            call()
        error = raised.value
        assert error.reason_code == "storage_contract_violation"
        assert error.status_code == 500
        assert str(error) == "Stored state manifest JSON is invalid"
        assert isinstance(error.__cause__, ContractValidationError)
        assert "private-secret-marker" not in str(error.diagnostics)
    server = create_server(service, port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.server_address[1]
        for path, body in [
            ("/api/graph/queries", {"operation": "manifest.read", "parameters": {
                "session_id": sid, "chain_id": chain_id, "boundary": boundary}}),
            ("/api/graph/queries", {"operation": "run.read", "parameters": {
                "session_id": sid, "chain_id": chain_id}}),
            (f"/api/graph/sessions/{sid}/manifests/read", {
                "chain_id": chain_id, "boundary": boundary}),
        ]:
            status, result = post(port, path, body)
            assert status == 500
            assert result["error"]["reason_code"] == "storage_contract_violation"
            assert "private-secret-marker" not in str(result)
        status, _ = post(port, "/api/graph/queries", {
            "operation": "manifest.read", "parameters": {
                "session_id": sid, "chain_id": chain_id, "boundary": "bad"}})
        assert status == 400
        status, _ = post(port, "/api/graph/queries", '{"private-secret-marker":', raw=True)
        assert status == 400
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()
        assert not worker.is_alive()
    assert "private-secret-marker" not in caplog.text
    with closing(service._store()) as store:
        assert store._connection.execute(
            "SELECT payload FROM graph_state_manifests WHERE owner_kind=? AND owner_id=?",
            (owner_kind, owner_id)).fetchone()["payload"] == payload


def test_snapshot_manifest_decode_and_missing_scope_boundaries(service):
    from uuid import uuid4
    initial = create(service, text_graph(service.registry))
    sid = initial["workflow_session_id"]
    started = service.start(sid, expected_revision=initial["revision"],
                            idempotency_key="snapshot-manifest")
    service.wait(started["active_chain_run_id"])
    with closing(service._store()) as store:
        row = store._connection.execute(
            "SELECT owner_id,payload FROM graph_state_manifests "
            "WHERE owner_kind='state_snapshot' LIMIT 1").fetchone()
        snapshot_id = row["owner_id"]
        assert service.get_state_manifest(sid, snapshot_id=snapshot_id)
        store._connection.execute(
            "UPDATE graph_state_manifests SET payload=? "
            "WHERE owner_kind='state_snapshot' AND owner_id=?",
            ('{"x":1,"x":2}', snapshot_id))
        store._connection.commit()
    with pytest.raises(DiagnosticError) as raised:
        service.get_state_manifest(sid, snapshot_id=snapshot_id)
    assert raised.value.status_code == 500
    with pytest.raises(ContractValidationError) as raised:
        service.get_state_manifest(sid, chain_id=str(uuid4()))
    assert raised.value.status_code == 403
    with closing(service._store()) as store:
        store._connection.execute(
            "DELETE FROM graph_state_manifests WHERE owner_kind='state_snapshot' AND owner_id=?",
            (snapshot_id,))
        store._connection.commit()
    with pytest.raises(ContractValidationError) as raised:
        service.get_state_manifest(sid, snapshot_id=snapshot_id)
    assert raised.value.status_code == 404
