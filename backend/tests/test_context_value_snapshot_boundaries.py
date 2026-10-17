"""Explicit context loading and settlement preserve per-node value snapshots."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

from phase1_agent.context_contract import project_agent_context_value
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import create, node
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import AgentTransportFixture, base_graph, run


def _node(view, binding_id):
    return next(item for item in view["nodes"] if item["node_binding_id"] == binding_id)


def _output(view, binding_id, port="output"):
    return _node(view, binding_id)["outputs"][port]


def test_agent_success_without_context_merge_does_not_write_object(tmp_path):
    transport = AgentTransportFixture()
    with closing(GraphWorkflowService(
            tmp_path / "agent-without-merge.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document = base_graph(service)
        merge = next(item for item in document["nodes"] if item["component_id"] == "context.merge")
        read = next(item for item in document["nodes"] if item["component_id"] == "context.output")
        document["nodes"] = [item for item in document["nodes"]
                             if item["node_binding_id"] != merge["node_binding_id"]]
        document["edges"] = [edge for edge in document["edges"]
                             if merge["node_binding_id"] not in (
                                 edge["source_node_id"], edge["target_node_id"])]
        binding = document["object_bindings"][0]
        binding["readers"] = [read["node_binding_id"]]
        binding["writers"] = []

        initial = create(service, document)
        original = deepcopy(initial["objects"]["context"])
        execute = next(item for item in document["nodes"] if item["component_id"] == "agents.execute")

        final = run(service, initial, "Only run the Agent; leave persistence untouched.")

        assert final["status"] == "succeeded", final["chains"]
        assert _node(final, execute["node_binding_id"])["status"] == "succeeded"
        assert set(_node(final, execute["node_binding_id"])["outputs"]) == {"result", "receipts"}
        assert final["objects"]["context"] == original
        assert final["objects"]["context"]["revision"] == 1


def test_merge_commits_new_head_without_refreshing_old_read_artifact(tmp_path):
    transport = AgentTransportFixture()
    with closing(GraphWorkflowService(
            tmp_path / "explicit-context-snapshots.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document = base_graph(service)
        read = next(item for item in document["nodes"] if item["component_id"] == "context.output")
        merge = next(item for item in document["nodes"] if item["component_id"] == "context.merge")
        reread = node(service.registry, "context.output", 1308)
        reread["config"]["object_key"] = "context"
        document["nodes"].append(reread)
        document.setdefault("control_edges", []).append({
            "edge_id": str(uuid4()),
            "source_node_id": merge["node_binding_id"],
            "target_node_id": reread["node_binding_id"],
        })
        document["object_bindings"][0]["readers"].append(reread["node_binding_id"])

        initial = create(service, document)
        version_one = deepcopy(initial["objects"]["context"])
        final = run(service, initial, "Commit once, then load the new head.")

        assert final["status"] == "succeeded", final["chains"]
        first_read = deepcopy(_output(final, read["node_binding_id"]))
        merged = _output(final, merge["node_binding_id"])
        commit = _output(final, merge["node_binding_id"], "commit")
        second_read = _output(final, reread["node_binding_id"])
        version_two = final["objects"]["context"]

        assert first_read["basis"] == {
            "revision_id": version_one["revision_id"], "head_revision": version_one["revision"]}
        assert project_agent_context_value(first_read) == version_one["value"]
        assert first_read["messages"] == version_one["value"]["messages"] == []
        assert commit["receipt"] == {
            "object_key": "context",
            "revision": 2,
            "revision_id": version_two["revision_id"],
            "deleted": False,
        }
        assert version_two["revision"] == 2
        assert version_two["value"] == project_agent_context_value(merged)
        assert second_read["basis"] == {
            "revision_id": version_two["revision_id"], "head_revision": version_two["revision"]}
        assert second_read["messages"] == version_two["value"]["messages"]
        assert first_read == _output(final, read["node_binding_id"])

        chain = next(item for item in final["chains"]
                     if item["chain_run_id"] == final["selected_chain_run_id"])
        assert chain["ordered_nodes"].index(merge["node_binding_id"]) \
            < chain["ordered_nodes"].index(reread["node_binding_id"])
        assert transport.calls and len(transport.calls) == 1
