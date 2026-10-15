"""Versioned logical-chat nodes use workflow-owned metadata and real stored snapshots."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE
from phase1_agent.frontend_contracts import FRONTEND_STATE_TYPE
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.session_objects import SessionObjectStore
from test_frontend_node_runtime import _create, _graph as _old_graph, _output, _save
from test_frontend_node_snapshots import _owner
from test_graph_service import run
from test_success_checkpoints import checkpoint_history


def _graph(service, **options):
    graph, messages, frontends = _old_graph(service, **options)
    for frontend in frontends:
        frontend["component_version"] = "2"
    for binding in graph["object_bindings"]:
        binding["type_id"] = CHAT_CONTEXT_TYPE
    return graph, messages, frontends


def test_new_version_registers_logical_chat_without_changing_old_node_contract(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "v2-declarations.sqlite")) as service:
        old = service.registry.get("frontend.node", "1").definition.to_dict()
        current = service.registry.get("frontend.node", "2").definition.to_dict()
        assert old["provided_session_item_types"] == [{"type_id": FRONTEND_STATE_TYPE, "schema_version": 1}]
        assert current["provided_session_item_types"] == [{"type_id": CHAT_CONTEXT_TYPE, "schema_version": 1}]
        assert current["outputs"][0]["data_schema_version"] == 2
        assert old["outputs"][0]["data_schema_version"] == 1
        assert current["default_config"] == old["default_config"]


@pytest.mark.parametrize("bound", [False, True])
def test_graph_runs_without_frontend_and_saves_inline_chat_with_real_success_point(tmp_path, bound):
    with closing(GraphWorkflowService(tmp_path / "v2-run.sqlite")) as service:
        graph, messages, frontends = _graph(service, bound=bound)
        graph = _save(service, graph)
        initial = _create(service, graph)
        final = run(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        key = frontends[0]["config"]["object_key"]
        value = final["objects"][key]["value"]
        assert "source_ref" not in str(value) and "view_ref" not in str(value)
        assert len(value["rounds"]) == 1
        floor = value["rounds"][0]
        assert floor["submitted_input"] == (messages[0]["config"]["text"] if bound else None)
        assert floor["base_checkpoint_id"] is None
        candidate = floor["candidates"][0]
        assert candidate["messages"][0]["text"] == messages[0]["config"]["text"]
        assert candidate["result_checkpoint_id"] is not None
        assert floor["selected_candidate_id"] == candidate["candidate_id"]
        proof = _output(final, frontends[0], "proof")
        assert proof["schema_version"] == 2 and proof["round"]["round_id"] == floor["round_id"]
        assert proof["round"]["candidate_id"] == candidate["candidate_id"]
        assert _output(final, frontends[0], "commit")["receipt"]["object_key"] == key
        history = checkpoint_history(service, final)
        assert history["state"]["chat_contexts"][key]["value"] == value
        assert candidate["result_checkpoint_id"] == history["latest"]["checkpoint_id"]
        saved = deepcopy(value)
        second = run(service, final)
        assert second["status"] == "succeeded", second["chains"]
        assert second["objects"][key]["value"]["rounds"][0] == saved["rounds"][0]
        assert len(second["objects"][key]["value"]["rounds"]) == 2


def test_logical_snapshot_reads_stored_data_without_artifact_resolution_and_is_detached(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "v2-snapshot.sqlite")) as service:
        graph, _, frontends = _graph(service)
        graph = _save(service, graph)
        initial = _create(service, graph)
        final = run(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        owner = _owner(graph, frontends[0], final)
        monkeypatch.setattr(SessionObjectStore, "_resolve_write_artifact",
                            lambda *args, **kwargs: pytest.fail("Logical chat snapshots cannot require artifacts"))
        snapshot = service.read_frontend_node_snapshot(*owner)
        assert set(snapshot) == {"schema_version", "kind", "owner", "basis", "chat"}
        assert snapshot["schema_version"] == 2
        assert snapshot["chat"] == final["objects"]["chat-0"]["value"]
        assert snapshot["chat"]["rounds"][0]["candidates"][0]["result_checkpoint_id"] is not None
        snapshot["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] = "local"
        assert service.get_cached_frontend_node_snapshot(*owner)["chat"] == final["objects"]["chat-0"]["value"]


def test_two_logical_nodes_and_sessions_do_not_share_values_or_snapshots(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "v2-isolation.sqlite")) as service:
        graph, _, frontends = _graph(service, count=2)
        graph = _save(service, graph)
        first, other = _create(service, graph), _create(service, graph)
        final = run(service, first)
        assert final["status"] == "succeeded", final["chains"]
        assert service.get_session(other["workflow_session_id"])["objects"] == other["objects"]
        for index, frontend in enumerate(frontends):
            snapshot = service.read_frontend_node_snapshot(*_owner(graph, frontend, final))
            assert snapshot["owner"]["object_key"] == "chat-" + str(index)
            assert snapshot["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] == "message " + str(index)
            empty = service.read_frontend_node_snapshot(*_owner(graph, frontend, other))
            assert empty["chat"] == {"rounds": []}
