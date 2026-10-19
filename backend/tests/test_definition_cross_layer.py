"""Cross-layer proof: package source, query, fixed revision, evidence and execution."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import (
    CapabilityPackage, PackageDependency, PackageManifest,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition
from phase1_agent.workflow_definitions import WorkflowDefinitionCatalog

from test_graph_execution import document as execution_document
from test_graph_execution import edge, node, run_graph


PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {"schema_version": {"const": 1}, "text": {"type": "string"}},
    "required": ["schema_version", "text"],
    "additionalProperties": False,
}


def stage_packages():
    def register_types(host):
        host.register_data_type(DataTypeDefinition(
            "sample.payload", 1, PAYLOAD_SCHEMA, scope="content"))

    def register_nodes(host):
        host.register_node(NodeDefinition(
            "sample.source", "1", "Payload source", "Sample", {"text": "ready"},
            {"type": "object", "properties": {"text": {"type": "string"}},
             "required": ["text"], "additionalProperties": False},
            outputs=(NodePort("output", "sample.payload"),),
        ), lambda config, inputs, context: {
            "output": {"schema_version": 1, "text": config["text"]}})
        host.register_node(NodeDefinition(
            "sample.uppercase", "1", "Upper sample", "Sample", {},
            {"type": "object", "properties": {}, "additionalProperties": False},
            inputs=(NodePort("input", "sample.payload"),),
            outputs=(NodePort("output", "sample.payload"),),
            is_output=True,
        ), lambda config, inputs, context: {
            "output": {"schema_version": 1, "text": inputs["input"]["text"].upper()}})

    return (
        CapabilityPackage(PackageManifest(
            "sample.definition-types", "1", exports={
                "data_types": [{"scope": "content", "type_id": "sample.payload",
                                "schema_version": 1}],
            }), register_types),
        CapabilityPackage(PackageManifest(
            "sample.definition-node", "1",
            dependencies=(PackageDependency("sample.definition-types", "1"),),
            exports={"nodes": [
                {"component_id": "sample.source", "component_version": "1"},
                {"component_id": "sample.uppercase", "component_version": "1"},
            ]},
        ), register_nodes),
    )


def configuration_document(service, identity, *, component_version="1"):
    return {
        "schema_version": 3,
        "workflow_definition_id": identity,
        "revision": 1,
        "name": "Definition layer cross-layer",
        "nodes": [{
            "node_binding_id": str(uuid4()),
            "component_id": "sample.uppercase",
            "component_version": component_version,
            "title": "Upper",
            "position": {"x": 0, "y": 0},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
            "config": {},
        }],
        "edges": [],
        "object_bindings": [],
        "package_lock": deepcopy(list(service.registry.package_lock)),
    }


def make_service(path):
    return GraphWorkflowService(
        path, capability_packages=stage_packages(),
        enabled_packages={"sample.definition-node": "1"})


def test_package_definition_survives_fixed_revision_reread_and_executes(tmp_path):
    path = tmp_path / "cross.sqlite"
    service = make_service(path)
    identity = str(uuid4())
    try:
        document = configuration_document(service, identity)
        saved = service.update_definition(
            document, workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        assert (saved["outcome"], saved["definition_revision"]) == ("saved", 1)

        fixed = service.get_definition(identity, 1)
        assert fixed["nodes"][0]["component_id"] == "sample.uppercase"
        references = service.definition_references(identity, 1)
        assert references["revision"] == 1
        assert references["package_lock"] == [
            {"package_id": "sample.definition-node", "version": "1"},
            {"package_id": "sample.definition-types", "version": "1"},
        ]
        node_reference = references["nodes"][0]
        assert node_reference["registration_ref"] == {
            "component_id": "sample.uppercase", "component_version": "1"}
        assert node_reference["source"] == [
            {"package_id": "sample.definition-node", "package_version": "1"}]
        assert node_reference["availability"] == "registered"
        data_types = {item["registration_ref"]["type_id"]: item
                      for item in references["data_types"]}
        assert data_types["sample.payload"]["source"] == [
            {"package_id": "sample.definition-types", "package_version": "1"}]
        assert data_types["sample.payload"]["declaration"]["schema"] == PAYLOAD_SCHEMA

        evidence = service.definition_evidence(
            {"scope": "content", "type_id": "sample.payload", "schema_version": 1})
        assert evidence["declaration"]["type_id"] == "sample.payload"
        assert evidence["declaration"]["schema"] == PAYLOAD_SCHEMA
        assert evidence["digest"].startswith("json-v1:sha256:")
    finally:
        service.close()

    reopened = make_service(path)
    try:
        references = reopened.definition_references(identity, 1)
        assert references["nodes"][0]["source"][0]["package_id"] == "sample.definition-node"
        assert reopened.get_definition(identity, 1)["nodes"][0]["config"] == {}
        evidence = reopened.definition_evidence(
            {"scope": "content", "type_id": "sample.payload", "schema_version": 1})
        assert evidence["declaration"]["schema"] == PAYLOAD_SCHEMA
    finally:
        reopened.close()

    graph = execution_document([
        node(1, "sample.source", registry=reopened.registry),
        node(2, "sample.uppercase", registry=reopened.registry),
    ], [edge(10, 1, 2)])
    result = run_graph(graph, reopened.registry)
    assert result.status == "succeeded"
    assert result.outputs[node(2)["node_binding_id"]]["output"] == {
        "schema_version": 1, "text": "READY"}

    catalog = WorkflowDefinitionCatalog(reopened.registry, reopened._package_manifests)
    assert catalog.get("node", {
        "component_id": "sample.uppercase", "component_version": "1"})["source"] == [
        {"package_id": "sample.definition-node", "package_version": "1"}]


def test_missing_exact_version_does_not_query_latest_or_replace_fixed_revision(tmp_path):
    path = tmp_path / "missing-version.sqlite"
    service = make_service(path)
    identity = str(uuid4())
    try:
        with pytest.raises(ContractValidationError) as caught:
            service.update_definition(
                configuration_document(service, identity, component_version="2"),
                workflow_definition_id=identity, expected_edit_sequence=0,
                idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "graph_missing_node_type"
        assert service.list_definitions() == []
        assert service.definition_query.get("node", {
            "component_id": "sample.uppercase", "component_version": "2"}) is None
        assert service.definition_query.get("node", {
            "component_id": "sample.uppercase", "component_version": "1"}) is not None
    finally:
        service.close()
