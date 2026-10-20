"""Evidence for the capability-4.0 review follow-up.

Three independent intents:

1. Bound Agent tools reach and change real variable state even when the graph
   contains no ordinary variable write/read nodes.
2. Preparing such a graph performs no business execution and no model call.
3. A nonempty tool-binding package source outside the frozen package lock is
   rejected before any execution.
"""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent import tool_package
from phase1_agent.variable_contract import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE

from test_graph_service import edge, node
from test_models_service_integration import ModelDatabaseFixture
from test_variable_tool_bindings import (
    DEFAULT_PARAMETERS, DEFAULT_PRESENTATION, EVENT_ID, KEY, VariableToolTransport,
    _events, _registered, _run, _service, build_graph, node_output,
)
from workflow_test_support import graph


def _no_normal_graph(service):
    """A merged tool loader graph with no ordinary variable data node."""
    document = graph(service)
    execute = next(item for item in document["nodes"]
                   if item["component_id"] == "agents.execute")
    assembly = next(item for item in document["nodes"]
                    if item["component_id"] == "context.assembly")
    assembly["component_version"] = "6"
    register = node(service.registry, "tools.variable-register", 401, component_version="2",
                    object_key=KEY, name="score", value_type="integer",
                    has_initial=True, initial_value=0)
    loader = node(service.registry, "tool-bindings.variable-tools", 405, component_version="2",
                  agent_node_id=execute["node_binding_id"],
                  variables=[{"alias": "score", "object_key": KEY,
                              "operations": ["read", "set", "add", "subtract"]}],
                  list_presentation=deepcopy(DEFAULT_PRESENTATION),
                  parameters_presentation=deepcopy(DEFAULT_PARAMETERS))
    document["nodes"].extend([register, loader])
    document["edges"].append(edge(loader, assembly, 1402, source_port="tool_prompt",
                                  target_port="tool_prompt"))
    document["control_edges"] = []
    document["object_bindings"].append(ObjectBinding(
        KEY, VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION, "shared",
        readers=(register["node_binding_id"], execute["node_binding_id"]),
        writers=(register["node_binding_id"], execute["node_binding_id"]),
        registrar_node_id=register["node_binding_id"]).to_dict())
    document["event_bindings"] = [{
        "event_id": EVENT_ID, "schema_version": 1, "display_name": "Register score",
        "audience": "management", "target_node_ids": [register["node_binding_id"]],
        "payload_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }]
    parts = {"execute": execute, "assembly": assembly, "register": register, "loader": loader}
    return document, parts


def test_agent_tools_run_without_any_normal_variable_read_or_write_node(tmp_path):
    transport = VariableToolTransport([
        ("score_set", {"value": 10}), ("score_add", {"value": 2}),
        ("score_subtract", {"value": 3}), ("score_read", {}),
        ("final_answer", {"answer": {"text": "done"}}),
    ])
    database = tmp_path / "tool-bound-only.sqlite"
    with closing(_service(database, transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, parts = _no_normal_graph(service)
        normal_nodes = {"tools.variable-assign", "tools.variable-to-content"}
        assert normal_nodes.isdisjoint(
            node["component_id"] for node in document["nodes"])
        assert document["control_edges"] == []
        session = _registered(service, document)
        assert session["objects"][KEY]["value"]["value"] == 0
        # Revision 1 is the session object initialization; registration adds
        # the second revision.
        registered_revision = session["objects"][KEY]["revision"]
        assert registered_revision == 2

        chain_id, view = _run(service, session)
        assert view["status"] == "succeeded", view["chains"]
        assert transport.calls
        assert view["objects"][KEY]["value"]["value"] == 9
        # Exactly the three tool writes, with no extra write from Agent
        # settlement: initialization 1 + register 1 + three writes = 5.
        assert view["objects"][KEY]["revision"] - registered_revision == 3
        assert view["objects"][KEY]["revision"] == 5

        events = _events(service, view, chain_id)
        settled = [event["payload"] for event in events if event["kind"] == "tool_settled"]
        results = [fact["message"]["blocks"][0]["content"] for fact in settled
                   if isinstance(fact["message"]["blocks"][0]["content"], dict)
                   and "value" in fact["message"]["blocks"][0]["content"]]
        assert [result["value"] for result in results] == [10, 12, 9, 9]
        assert results[-1]["value"] == 9

        tool_prompt = node_output(view, parts["loader"], port="tool_prompt")
        assert tool_prompt["agent_node_id"] == parts["execute"]["node_binding_id"]
        assert [entry["name"] for entry in tool_prompt["entries"]] == [
            "score_read", "score_set", "score_add", "score_subtract"]

    with closing(_service(database, transport)) as reopened:
        reopened_session = reopened.get_session(session["workflow_session_id"])
        assert reopened_session["objects"][KEY]["value"]["value"] == 9
        assert reopened_session["objects"][KEY]["revision"] == 5


def test_no_normal_graph_preparation_runs_no_business_executor_or_model(
        tmp_path, monkeypatch):
    counts = {"assign": 0, "read": 0}
    original_assign = tool_package._assign
    original_read = tool_package._variable_to_content

    def counted_assign(config, inputs, context):
        counts["assign"] += 1
        return original_assign(config, inputs, context)

    def counted_read(config, inputs, context):
        counts["read"] += 1
        return original_read(config, inputs, context)

    transport = VariableToolTransport([("final_answer", {"answer": {"text": "never"}})])
    with closing(_service(tmp_path / "prepared-no-normal.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = _no_normal_graph(service)
        session = _registered(service, document)
        monkeypatch.setattr(tool_package, "_assign", counted_assign)
        monkeypatch.setattr(tool_package, "_variable_to_content", counted_read)
        submitted = []
        monkeypatch.setattr(service._executor, "submit",
                            lambda *args, **kwargs: submitted.append(args) or None)

        started = service.start(
            session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key=str(uuid4()), inputs={"text": "prepared only"})
        assert submitted
        assert service.get_session(session["workflow_session_id"])["status"] == "prepared"
        assert counts == {"assign": 0, "read": 0}
        assert transport.calls == []

        chain_id = started["active_chain_run_id"]
        assert service._tool_frames[chain_id].target_count == 4
        node_runs = service.get_run(session["workflow_session_id"], chain_id)["node_runs"]
        assert node_runs
        assert {run["status"] for run in node_runs} == {"prepared"}


@pytest.mark.parametrize("change", ["loader-source", "target-source"])
def test_nonempty_source_outside_frozen_lock_is_rejected(tmp_path, monkeypatch, change):
    transport = VariableToolTransport([("final_answer", {"answer": {"text": "never"}})])
    with closing(_service(tmp_path / ("outside-lock-" + change + ".sqlite"),
                          transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = build_graph(service)
        session = _registered(service, document)
        before = deepcopy(service.get_session(session["workflow_session_id"]))
        manifests = deepcopy(service._package_manifests)
        package_id = ("workflow.tool-prompts" if change == "loader-source"
                      else "workflow.tools")
        manifest = next(item for item in manifests if item["package_id"] == package_id)
        manifest["version"] = "9.9.9"
        service._package_manifests = tuple(manifests)

        component = (("tool-bindings.variable-tools", "2") if change == "loader-source"
                     else ("tools.variable-assign", "1"))
        view = service.definition_query.get("node", {
            "component_id": component[0], "component_version": component[1]})
        assert view["source"] == [
            {"package_id": package_id, "package_version": "9.9.9"}]
        frozen = {(row["package_id"], row["version"])
                  for row in service.registry.package_lock}
        assert (package_id, "9.9.9") not in frozen

        with pytest.raises(ContractValidationError) as caught:
            service.start(session["workflow_session_id"], expected_revision=session["revision"],
                          idempotency_key=str(uuid4()), inputs={"text": "blocked"})
        assert caught.value.reason_code == "tool_binding_source_missing"
        assert transport.calls == []
        assert service.get_session(session["workflow_session_id"]) == before
