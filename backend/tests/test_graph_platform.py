from contextlib import closing
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackage, PackageManifest
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort, text_value
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import WriteIntent

from test_graph_service import create, document, edge, node, run
from test_graph_session_objects import task_package, object_graph


def test_public_v2_catalog_object_http_and_strict_old_catalog(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "http.sqlite", capability_packages=[task_package()],
                                      enabled_packages={"example.tasks": "1.0.0"})) as service:
        v1 = dispatch_graph(service, "GET", "/api/graph/node-types")[1]
        v2 = dispatch_graph(service, "GET", "/api/graph/node-types/v2")[1]
        assert v1["schema_version"] == 1 and v2["schema_version"] == 2
        assert not any(row["component_id"] == "workflow.global-content" and row["component_version"] == "2"
                       for row in v1["node_types"])
        assert any(row["component_id"] == "workflow.object-read" for row in v2["node_types"])
        assert dispatch_graph(service, "GET", "/api/graph/platform")[1]["host_protocol_version"] == 1
        view = create(service, object_graph(service))
        sid, writer = view["workflow_session_id"], view["nodes"][0]["node_binding_id"]
        endpoint = "/api/graph/sessions/" + sid
        assert dispatch_graph(service, "GET", endpoint + "/objects")[1]["objects"]["main"]["value"] == {"count": 0}
        response = dispatch_graph(service, "POST", endpoint + "/objects/write", {
            "node_id": writer, "writes": [WriteIntent("main", 1, "edit", {"count": 8}).to_dict()],
            "expected_revision": view["revision"], "idempotency_key": "http-edit",
        })[1]
        assert response["session"]["objects"]["main"]["value"] == {"count": 8}
        with pytest.raises(ContractValidationError):
            dispatch_graph(service, "POST", endpoint + "/objects/write", {"allow_all": True})


def test_package_changes_and_object_edit_refuse_active_run_and_paused_manifests_are_separate(tmp_path):
    entered, release = Event(), Event()

    def register(host):
        def wait(config, inputs, context):
            entered.set()
            assert release.wait(10)
            return {"output": text_value("gate")}
        host.register_node(NodeDefinition("example.gate", "1", "Gate", "Test", {},
            {"type": "object", "additionalProperties": False}, outputs=(NodePort("output", "TEXT"),)), wait)

    gate_package = CapabilityPackage(PackageManifest("example.gate", "1"), register)
    with closing(GraphWorkflowService(tmp_path / "active.sqlite", capability_packages=[task_package(), gate_package],
        enabled_packages={"example.tasks": "1.0.0", "example.gate": "1"})) as service:
        doc = object_graph(service)
        gate = node(service.registry, "example.gate", 0)
        output = node(service.registry, "output", 10)
        doc["nodes"].extend([gate, output])
        doc["edges"].append(edge(gate, output, 20))
        initial = create(service, doc)
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"], idempotency_key="start")
        chain = started["active_chain_run_id"]
        assert entered.wait(5)
        try:
            with pytest.raises(ContractValidationError) as failure:
                service.configure_capability_packages({"workflow.compat": "1.0.0"})
            assert failure.value.reason_code == "package_change_during_execution"
            current = service.get_session(initial["workflow_session_id"])
            with pytest.raises(ContractValidationError):
                service.update_session_objects(current["workflow_session_id"], node_id=doc["nodes"][0]["node_binding_id"],
                    writes=[WriteIntent("main", current["objects"]["main"]["revision"], "concurrent", {"count": 4}).to_dict()],
                    expected_revision=current["revision"], idempotency_key="concurrent")
            service.control(current["workflow_session_id"], action="pause", expected_revision=current["revision"],
                            idempotency_key="pause")
        finally:
            release.set()
        service.wait(chain)
        paused = service.get_session(initial["workflow_session_id"])
        assert paused["status"] == "paused" and paused["head_commit_id"] == initial["head_commit_id"]
        before = service.get_run(paused["workflow_session_id"], chain)["state_manifests"]["end"]
        assert before["status"] == "paused"
        service.control(paused["workflow_session_id"], action="resume", expected_revision=paused["revision"],
                        idempotency_key="resume")
        service.wait(chain)
        complete = service.get_session(paused["workflow_session_id"])
        assert complete["status"] == "succeeded"
        assert service.get_run(paused["workflow_session_id"], chain)["state_manifests"]["end"]["status"] == "succeeded"
        assert before["status"] == "paused"
