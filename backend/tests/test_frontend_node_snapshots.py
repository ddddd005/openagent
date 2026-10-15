"""Display caches follow explicit stored-data reads after accepted settlement."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_store import GraphRecordStore

from test_frontend_node_runtime import _create, _graph, _save
from test_graph_service import edge, node, run
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.session_objects import SessionObjectStore


def _owner(graph, frontend, session):
    return (graph["workflow_definition_id"], graph["revision"],
            frontend["node_binding_id"], session["workflow_session_id"])


def test_automatic_snapshot_is_a_real_postcommit_read_and_independent_copy(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "snapshot-postcommit.sqlite")) as service:
        graph, messages, frontends = _graph(service)
        graph = _save(service, graph)
        initial = _create(service, graph)
        owner = _owner(graph, frontends[0], initial)
        assert service.get_cached_frontend_node_snapshot(*owner) is None
        reads = []
        original = service._read_frontend_snapshot

        def observe(repo, *args):
            assert not repo.store._connection.in_transaction
            result = original(repo, *args)
            reads.append(deepcopy(result))
            return result

        monkeypatch.setattr(service, "_read_frontend_snapshot", observe)
        final = run(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        assert len(reads) == 1
        snapshot = service.get_cached_frontend_node_snapshot(*owner)
        record = final["objects"]["chat-0"]
        assert snapshot == reads[0]
        assert snapshot["basis"] == {
            "revision": record["revision"], "revision_id": record["revision_id"],
        }
        assert snapshot["basis"]["revision"] == initial["objects"]["chat-0"]["revision"] + 1
        assert snapshot["entries"][0]["content"]["text"] == messages[0]["config"]["text"]
        assert snapshot["source"]["node_binding_id"] == frontends[0]["node_binding_id"]
        snapshot["entries"][0]["content"]["text"] = "browser-local edit"
        assert service.get_cached_frontend_node_snapshot(*owner) == reads[0]
        assert service.get_session(final["workflow_session_id"])["objects"]["chat-0"] == record


def test_two_frontend_nodes_and_two_sessions_have_separate_display_caches(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "snapshot-isolation.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2)
        graph = _save(service, graph)
        first, second = _create(service, graph), _create(service, graph)
        other_owner = _owner(graph, frontends[0], second)
        empty = service.read_frontend_node_snapshot(*other_owner)
        assert empty["entries"] == []
        completed = run(service, first)
        assert completed["status"] == "succeeded", completed["chains"]
        snapshots = [service.get_cached_frontend_node_snapshot(
            *_owner(graph, frontend, first)) for frontend in frontends]
        assert all(len(snapshot["entries"]) == 1 for snapshot in snapshots)
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


def test_other_accepted_writer_does_not_change_cache_until_another_read(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "snapshot-explicit-read.sqlite")) as service:
        graph, messages, frontends = _graph(service)
        reader = node(service.registry, "frontend.state.output", 19101, object_key="chat-0")
        append = node(service.registry, "frontend.state.append", 19102, object_key="chat-0")
        output = node(service.registry, "tools.output", 19103)
        for entry in (reader, append, output):
            entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        graph["nodes"].extend([reader, append, output])
        graph["edges"] = [
            edge(reader, append, 19101, source_port="view", target_port="view"),
            edge(messages[0], append, 19102, target_port="content"),
            edge(messages[0], output, 19103),
        ]
        graph["control_edges"] = [{
            "edge_id": edge(reader, append, 19104)["edge_id"],
            "source_node_id": append["node_binding_id"],
            "target_node_id": output["node_binding_id"],
        }]
        graph["object_bindings"][0]["readers"].extend(
            [reader["node_binding_id"], append["node_binding_id"]])
        graph["object_bindings"][0]["writers"].append(append["node_binding_id"])
        graph = _save(service, graph)
        initial = _create(service, graph)
        owner = _owner(graph, frontends[0], initial)
        empty = service.read_frontend_node_snapshot(*owner)
        final = run(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        assert len(final["objects"]["chat-0"]["value"]["entries"]) == 1
        assert service.get_cached_frontend_node_snapshot(*owner) == empty
        refreshed = service.read_frontend_node_snapshot(*owner)
        assert len(refreshed["entries"]) == 1
        assert refreshed["basis"]["revision_id"] == final["objects"]["chat-0"]["revision_id"]
        assert empty["entries"] == []


def test_rolled_back_append_never_creates_a_display_cache(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "snapshot-rollback.sqlite")) as service:
        graph, _, frontends = _graph(service)
        graph = _save(service, graph)
        initial = _create(service, graph)
        original = service._save_effects

        def fail_after_apply(repo, sid, document, event):
            receipts = original(repo, sid, document, event)
            if event["node_binding_id"] == frontends[0]["node_binding_id"]:
                raise OSError("rollback after CAS")
            return receipts

        monkeypatch.setattr(service, "_save_effects", fail_after_apply)
        final = run(service, initial)
        assert final["status"] == "archive_failed", final["chains"]
        assert final["objects"] == initial["objects"]
        assert service.get_cached_frontend_node_snapshot(
            *_owner(graph, frontends[0], initial)) is None


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
        assert record["revision"] == 2 and len(record["value"]["entries"]) == 1
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
