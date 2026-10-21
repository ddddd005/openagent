"""Production configuration saves are consumed by prepared runs and public outputs."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_operations import FrontendNodeOperations
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import document, edge, node, run, uid


def _save_graph(app, graph, expected_edit_sequence):
    request = {
        "workflow_definition_id": graph["workflow_definition_id"],
        "document": deepcopy(graph), "expected_edit_sequence": expected_edit_sequence,
        "idempotency_key": str(uuid4()),
    }
    result = app.command("definition.update", request)["result"]
    assert result["outcome"] == "saved", result
    return result, request


def _text(definition, node_id):
    return next(entry["config"]["text"] for entry in definition["nodes"]
                if entry["node_binding_id"] == node_id)


def _node_id(entry):
    return entry["node_binding_id"] if isinstance(entry, dict) else entry


def _output(session, entry, port="output"):
    node_id = _node_id(entry)
    return next(item["outputs"][port] for item in session["nodes"]
                if item["node_binding_id"] == node_id)


def _observation(view, entry, port="output"):
    node_id = _node_id(entry)
    return next(row for row in view["outputs"]
                if row["node_binding_id"] == node_id and row["port_id"] == port)


def test_prepared_run_and_public_output_keep_the_saved_configuration(tmp_path):
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "editable input gate timed out"
        return {"output": text_content("gate")}

    with closing(GraphWorkflowService(tmp_path / "configuration-execution.sqlite")) as service:
        service.registry.register(NodeDefinition(
            "test.editable-gate", "1", "Gate", "Test", {}, {"type": "object"},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), gate)
        wait = node(service.registry, "test.editable-gate", 18201)
        source = node(service.registry, "tools.editable-input", 18202, text="A")
        output = node(service.registry, "tools.output", 18203)
        output["public_outputs"] = ["output"]
        graph = document([wait, source, output], [edge(source, output, 18201)])
        graph.update(
            schema_version=3, package_lock=list(service.registry.execution_package_lock),
            control_edges=[{
                "edge_id": str(uuid4()), "source_node_id": wait["node_binding_id"],
                "target_node_id": source["node_binding_id"],
            }],
        )
        for entry in graph["nodes"]:
            entry["configuration_policy"] = deepcopy(service.registry.get(
                entry["component_id"], entry["component_version"],
            ).definition.to_dict()["default_configuration_policy"])

        app = GraphApplication(service)
        created, _ = _save_graph(app, graph, 0)
        assert created["edit_sequence"] == created["definition_revision"] == 1
        view = service.create_session(
            graph["workflow_definition_id"], created["definition_revision"],
            idempotency_key=str(uuid4()))
        started = service.start(
            view["workflow_session_id"], expected_revision=view["revision"],
            idempotency_key=str(uuid4()),
        )
        try:
            assert entered.wait(5), "editable input execution did not reach gate"
            changed = deepcopy(created["document"])
            source_node = next(entry for entry in changed["nodes"]
                               if entry["node_binding_id"] == source["node_binding_id"])
            source_node["config"]["text"] = "B"
            # G21: the prepared/active run locks user configuration edits.  The
            # former intent "save adjustable text while the run is in flight"
            # is replaced by the approved rule; the save happens after the run.
            with pytest.raises(ContractValidationError) as rejected:
                _save_graph(app, changed, created["edit_sequence"])
            assert rejected.value.reason_code == "configuration_run_locked"
            assert service.get_definition(graph["workflow_definition_id"])["edit_sequence"] == 1
        finally:
            release.set()
            service.wait(started["active_chain_run_id"])

        first = service.get_session(view["workflow_session_id"])
        assert first["status"] == "succeeded", first["chains"]
        assert _output(first, source) == text_content("A")
        assert _output(first, output) == text_content("A")
        with closing(service._store()) as store:
            frozen = store._connection.execute(
                "SELECT json_extract(payload,'$.config.text') FROM records "
                "WHERE record_type='node_run' AND json_extract(payload,'$.chain_run_id')=? "
                "AND json_extract(payload,'$.node_binding_id')=?",
                (started["active_chain_run_id"], source["node_binding_id"]),
            ).fetchone()[0]
        assert frozen == "A"
        saved, _ = _save_graph(app, changed, created["edit_sequence"])
        assert saved["edit_sequence"] == 2
        assert saved["definition_revision"] == 1
        assert saved["changed_categories"] == ["adjustable"]
        assert _text(service.get_definition(graph["workflow_definition_id"]),
                     source["node_binding_id"]) == "B"
        first_outputs = app.query("output.list", {"session_id": view["workflow_session_id"]})
        observed = _observation(first_outputs, output["node_binding_id"])
        assert observed["availability"] == "produced"
        assert observed["payload"] == text_content("A")
        assert observed["source"]["definition_revision"] == 1

        second = run(service, first)
        assert second["status"] == "succeeded", second["chains"]
        assert _output(second, source) == text_content("B")
        assert _output(second, output) == text_content("B")
        second_outputs = app.query("output.list", {"session_id": view["workflow_session_id"]})
        assert _observation(second_outputs, output["node_binding_id"])["payload"] == text_content("B")
        history = app.query("output.history", {
            "session_id": view["workflow_session_id"],
            "workflow_definition_id": graph["workflow_definition_id"],
            "definition_revision": 1, "node_id": output["node_binding_id"], "port_id": "output",
        })
        assert [row["payload"] for row in history["outputs"]] == [
            text_content("A"), text_content("B")]

        fixed = service.get_definition(graph["workflow_definition_id"], 1)
        assert _text(fixed, source["node_binding_id"]) == "A"
        assert _text(service.get_definition(graph["workflow_definition_id"]),
                     source["node_binding_id"]) == "B"


def test_bound_frontend_patch_is_the_value_consumed_by_the_prepared_run(tmp_path):
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "editable input gate timed out"
        return {"output": text_content("gate")}

    with closing(GraphWorkflowService(tmp_path / "frontend-configuration-execution.sqlite")) as service:
        service.registry.register(NodeDefinition(
            "test.editable-gate", "1", "Gate", "Test", {}, {"type": "object"},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), gate)
        wait = node(service.registry, "test.editable-gate", 18301)
        source = node(service.registry, "tools.editable-input", 18302, text="A")
        output = node(service.registry, "tools.output", 18303)
        output["public_outputs"] = ["output"]
        frontend_entry = service.registry.get("frontend.node", "2")
        frontend = {
            "node_binding_id": uid(18304), "component_id": "frontend.node",
            "component_version": "2", "title": "frontend.node", "position": {"x": 0, "y": 200},
            "config": {
                **deepcopy(frontend_entry.definition.default_config),
                "object_key": "chat-0", "input_node_id": source["node_binding_id"],
            },
        }
        graph = document([wait, source, output, frontend], [
            edge(source, output, 18301),
            edge(source, frontend, 18302, target_port="content"),
        ])
        graph.update(
            schema_version=3, package_lock=list(service.registry.execution_package_lock),
            control_edges=[{
                "edge_id": str(uuid4()), "source_node_id": wait["node_binding_id"],
                "target_node_id": source["node_binding_id"],
            }],
            object_bindings=[{
                "object_key": "chat-0", "type_id": CHAT_CONTEXT_TYPE, "schema_version": 1,
                "scope": "shared", "readers": [frontend["node_binding_id"]],
                "writers": [frontend["node_binding_id"]], "owner_node_id": None,
                "registrar_node_id": frontend["node_binding_id"],
            }],
        )
        for entry in graph["nodes"]:
            entry["configuration_policy"] = deepcopy(service.registry.get(
                entry["component_id"], entry["component_version"],
            ).definition.to_dict()["default_configuration_policy"])

        app = GraphApplication(service)
        created, _ = _save_graph(app, graph, 0)
        view = service.create_session(
            graph["workflow_definition_id"], created["definition_revision"],
            idempotency_key=str(uuid4()))
        started = service.start(
            view["workflow_session_id"], expected_revision=view["revision"],
            idempotency_key=str(uuid4()),
        )
        try:
            assert entered.wait(5), "editable input execution did not reach gate"
            owner = {
                "workflow_definition_id": graph["workflow_definition_id"],
                "definition_revision": 1, "node_binding_id": frontend["node_binding_id"],
                "session_id": view["workflow_session_id"],
            }
            bound = FrontendNodeOperations(service).bind(**owner)
            # G21: the node-page narrow patch is a new user configuration
            # submission, so it is rejected while the prepared/active run is
            # unfinished.  It is accepted after the run settles and is then
            # consumed by the next prepared round.
            with pytest.raises(ContractValidationError) as rejected:
                bound.patch_input(
                    "B", expected_edit_sequence=1, idempotency_key="runtime-node-page")
            assert rejected.value.reason_code == "configuration_run_locked"
            assert _text(service.get_definition(graph["workflow_definition_id"]),
                         source["node_binding_id"]) == "A"
        finally:
            release.set()
            service.wait(started["active_chain_run_id"])

        first = service.get_session(view["workflow_session_id"])
        assert first["status"] == "succeeded", first["chains"]
        assert _output(first, source) == text_content("A")
        assert _output(first, output) == text_content("A")
        with closing(service._store()) as store:
            frozen = store._connection.execute(
                "SELECT json_extract(payload,'$.config.text') FROM records "
                "WHERE record_type='node_run' AND json_extract(payload,'$.chain_run_id')=? "
                "AND json_extract(payload,'$.node_binding_id')=?",
                (started["active_chain_run_id"], source["node_binding_id"]),
            ).fetchone()[0]
        assert frozen == "A"

        bound = FrontendNodeOperations(service).bind(**owner)
        patched = bound.patch_input(
            "B", expected_edit_sequence=1, idempotency_key="runtime-node-page-after-run")
        assert patched["outcome"] == "saved", patched
        assert patched["edit_sequence"] == 2
        assert patched["definition_revision"] == 1
        assert _text(patched["document"], source["node_binding_id"]) == "B"
        first_outputs = app.query("output.list", {"session_id": view["workflow_session_id"]})
        assert _observation(first_outputs, output["node_binding_id"])["payload"] == text_content("A")

        second = run(service, first)
        assert second["status"] == "succeeded", second["chains"]
        assert _output(second, source) == text_content("B")
        assert _output(second, output) == text_content("B")
        second_outputs = app.query("output.list", {"session_id": view["workflow_session_id"]})
        assert _observation(second_outputs, output["node_binding_id"])["payload"] == text_content("B")
        history = app.query("output.history", {
            "session_id": view["workflow_session_id"],
            "workflow_definition_id": graph["workflow_definition_id"],
            "definition_revision": 1, "node_id": output["node_binding_id"], "port_id": "output",
        })
        assert [row["payload"] for row in history["outputs"]] == [
            text_content("A"), text_content("B")]
