"""Display caches follow explicit stored-data reads after accepted settlement."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.execution_data import RunData
from phase1_agent.graph_store import GraphRecordStore

from frontend_node_test_support import create as _create, graph as _graph, owner as _owner, save as _save
from test_graph_service import run
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.session_objects import SessionObjectStore


def test_automatic_snapshot_is_a_real_postcommit_read_and_independent_copy(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "snapshot-postcommit.sqlite")) as service:
        graph, messages, frontends = _graph(service)
        graph = _save(service, graph)
        initial = _create(service, graph)
        owner = _owner(graph, frontends[0], initial)
        assert service.get_cached_frontend_node_snapshot(*owner) is None
        reads = []
        original = service._read_frontend_snapshot

        def observe(data, *args):
            assert isinstance(data, RunData)
            assert not hasattr(data, "store") and not hasattr(data, "connection")
            result = original(data, *args)
            reads.append(deepcopy(result))
            return result

        monkeypatch.setattr(service, "_read_frontend_snapshot", observe)
        final = run(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        assert reads
        snapshot = service.get_cached_frontend_node_snapshot(*owner)
        record = final["objects"]["chat-0"]
        assert snapshot == reads[-1]
        assert snapshot["basis"] == {
            "revision": record["revision"], "revision_id": record["revision_id"],
        }
        assert snapshot["basis"]["revision"] >= initial["objects"]["chat-0"]["revision"] + 1
        assert snapshot["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] == (
            messages[0]["config"]["text"])
        snapshot["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] = "browser-local edit"
        assert service.get_cached_frontend_node_snapshot(*owner) == reads[-1]
        assert service.get_session(final["workflow_session_id"])["objects"]["chat-0"] == record


def test_two_frontend_nodes_and_two_sessions_have_separate_display_caches(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "snapshot-isolation.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2)
        graph = _save(service, graph)
        first, second = _create(service, graph), _create(service, graph)
        other_owner = _owner(graph, frontends[0], second)
        empty = service.read_frontend_node_snapshot(*other_owner)
        assert empty["chat"] == {"rounds": []}
        completed = run(service, first)
        assert completed["status"] == "succeeded", completed["chains"]
        snapshots = [service.get_cached_frontend_node_snapshot(
            *_owner(graph, frontend, first)) for frontend in frontends]
        assert all(len(snapshot["chat"]["rounds"]) == 1 for snapshot in snapshots)
        assert snapshots[0]["owner"]["object_key"] != snapshots[1]["owner"]["object_key"]
        assert service.get_cached_frontend_node_snapshot(*other_owner) == empty
        assert service.get_cached_frontend_node_snapshot(
            *_owner(graph, frontends[1], second)) is None
        with pytest.raises(ContractValidationError):
            service.read_frontend_node_snapshot(
                graph["workflow_definition_id"], graph["revision"] + 1,
                frontends[0]["node_binding_id"], first["workflow_session_id"])


@pytest.mark.parametrize("failure", ["reader", "registrar", "type"])
def test_snapshot_direct_read_rejects_wrong_access_registration_and_type(tmp_path, monkeypatch, failure):
    with closing(GraphWorkflowService(tmp_path / "snapshot-boundaries.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2, readers=failure != "reader")
        if failure == "registrar":
            frontends[0]["config"]["object_key"] = "chat-1"
            graph["object_bindings"][1]["readers"].append(frontends[0]["node_binding_id"])
        graph = _save(service, graph)
        initial = _create(service, graph)
        if failure == "type":
            original = SessionObjectStore.read

            def wrong_type(objects, *args, **kwargs):
                return {**original(objects, *args, **kwargs), "type_id": "other.type"}

            monkeypatch.setattr(SessionObjectStore, "read", wrong_type)
        with pytest.raises(ContractValidationError) as error:
            service.read_frontend_node_snapshot(*_owner(graph, frontends[0], initial))
        assert error.value.reason_code == (
            "session_object_access_denied" if failure == "reader" else "frontend_node_registration_mismatch")
        assert service._frontend_node_snapshots == {}


def test_failed_frontend_node_does_not_create_snapshot_cache(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "snapshot-rollback.sqlite")) as service:
        graph, _, frontends = _graph(service)
        graph = _save(service, graph)
        initial = _create(service, graph)
        original = service._save_effects

        def fail_before_apply(repo, sid, document, event, **kwargs):
            if event["node_binding_id"] == frontends[0]["node_binding_id"]:
                raise OSError("frontend append settlement interrupted")
            return original(repo, sid, document, event, **kwargs)

        monkeypatch.setattr(service, "_save_effects", fail_before_apply)
        final = run(service, initial)
        assert final["status"] == "archive_failed", final["chains"]
        assert service.get_cached_frontend_node_snapshot(
            *_owner(graph, frontends[0], initial)) is None
        assert final["objects"]["chat-0"]["value"]["rounds"]


def test_postcommit_read_failure_keeps_write_and_read_retry_never_appends_again(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "snapshot-read-failure.sqlite")) as service:
        graph, _, frontends = _graph(service)
        graph = _save(service, graph)
        initial = _create(service, graph)
        owner = _owner(graph, frontends[0], initial)
        original = service._read_frontend_snapshot

        def fail_read(*args):
            raise OSError("committed display read failed")

        monkeypatch.setattr(service, "_read_frontend_snapshot", fail_read)
        started = service.start(
            initial["workflow_session_id"], expected_revision=initial["revision"],
            idempotency_key="snapshot-read-failure")
        with pytest.raises(ContractValidationError) as failure:
            service.wait(started["active_chain_run_id"])
        assert failure.value.reason_code == "result_committed_notification_failed"
        final = service.get_session(initial["workflow_session_id"])
        assert final["status"] != "succeeded"
        assert "result_committed_notification_failed" in str(final["chains"])
        record = final["objects"]["chat-0"]
        assert record["revision"] >= initial["objects"]["chat-0"]["revision"] + 1
        assert len(record["value"]["rounds"]) == 1
        assert service.get_cached_frontend_node_snapshot(*owner) is None
        monkeypatch.setattr(service, "_read_frontend_snapshot", original)
        snapshot = service.read_frontend_node_snapshot(*owner)
        assert snapshot["basis"]["revision_id"] == record["revision_id"]
        with closing(service._store()) as store:
            repo = GraphRecordStore(store)
            chain = repo.rows("chain_run", workflow_session_id=initial["workflow_session_id"])[0]
            accepted = next(item for item in repo.rows("node_run", chain_run_id=chain["chain_run_id"])
                            if item["node_binding_id"] == frontends[0]["node_binding_id"])
            event = {
                "event": "succeeded", "settlement_committed": True,
                "node_binding_id": accepted["node_binding_id"], "node_run_id": accepted["run_id"],
                "output_refs": accepted["output_refs"],
            }
        for _ in range(2):
            service._accept_frontend_node_output(
                initial["workflow_session_id"], chain["chain_run_id"], event)
        assert service.get_session(initial["workflow_session_id"])["objects"]["chat-0"] == record
        assert service.get_cached_frontend_node_snapshot(*owner) == snapshot
