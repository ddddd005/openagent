"""A third-party service runs through GraphRuntimeHost with no Agent or model."""

from contextlib import closing
import json
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.content_contracts import text_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import create, document, node, uid
from test_host_services import DEFINITION, GRANT, REFERENCE, UppercaseService


def test_independent_plugin_service_uses_generic_dispatch_and_run_lifecycle(tmp_path):
    instances = []
    peer_business = "other-node-private-business"

    def register(host):
        def factory(environment):
            assert not hasattr(environment, "_store")
            with pytest.raises(Exception) as caught:
                environment.read_resource({"envelope_version": 1, "scope": "workspace",
                                           "type_id": "sample.resource", "resource_id": uid(99)})
            assert caught.value.reason_code == "graph_resource_dependency_undeclared"
            instance = UppercaseService(environment)
            instances.append(instance)
            return instance
        host.register_service(DEFINITION, factory)
        host.register_node(NodeDefinition(
            "sample.upper", "1", "Upper", "Sample", {}, {"type": "object", "additionalProperties": False},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),), is_output=True,
            capabilities=("sample:format",), service_requirements=(GRANT,)),
            lambda config, inputs, context: {"output": text_content(context.host_call(
                "sample:format", "upper", {"text": "independent plugin"})["text"])})
        host.register_node(NodeDefinition(
            "sample.peer", "1", "Peer", "Sample", {}, {"type": "object", "additionalProperties": False},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),), is_output=True,
            private_state_schema={"type": "object", "properties": {"business": {"type": "string"}},
                                  "required": ["business"], "additionalProperties": False},
            private_state_default={"business": peer_business}),
            lambda config, inputs, context: {"output": text_content(peer_business)})

    package = CapabilityPackage(PackageManifest("sample.services", "1",
        dependencies=(PackageDependency("workflow.content", "1.0.0"),), schema_version=3,
        exports={"services": [REFERENCE.to_dict()],
                 "nodes": [{"component_id": "sample.upper", "component_version": "1"},
                           {"component_id": "sample.peer", "component_version": "1"}]}), register)
    with closing(GraphWorkflowService(tmp_path / "services.sqlite",
            capability_packages=(package,), enabled_packages={"sample.services": "1"})) as service:
        capabilities = service.platform_capabilities()
        assert capabilities["services"] == [DEFINITION.to_dict()]
        for index in range(2):
            work = node(service.registry, "sample.upper", 801 + index)
            peer = node(service.registry, "sample.peer", 901 + index)
            initial = create(service, document([work, peer], []))
            started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                    idempotency_key=str(uuid4()))
            service.wait(started["active_chain_run_id"])
            final = service.get_session(initial["workflow_session_id"])
            assert final["status"] == "succeeded"
            assert final["nodes"][0]["outputs"]["output"] == text_content("INDEPENDENT PLUGIN")
            assert final["nodes"][1]["outputs"]["output"] == text_content(peer_business)
            notification = instances[-1].accepted[0]
            assert set(notification) == {
                "event", "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id",
                "outputs", "output_refs", "settlement_committed"}
            assert notification["event"] == "succeeded" and notification["settlement_committed"] is True
            assert notification["node_binding_id"] == work["node_binding_id"]
            assert notification["outputs"] == {"output": text_content("INDEPENDENT PLUGIN")}
            assert peer_business not in json.dumps(notification)
        assert len(instances) == 2 and instances[0] is not instances[1]
        assert all(instance.calls == instance.releases == 1 for instance in instances)
        assert all(len(instance.accepted) == 1 for instance in instances)
        assert service._service_runs == {} and service._runtime_hosts == {}
        with pytest.raises(Exception) as caught:
            instances[0].environment.read_resource({"envelope_version": 1, "scope": "workspace",
                "type_id": "sample.resource", "resource_id": uid(99)})
        assert caught.value.reason_code == "host_service_unavailable"
