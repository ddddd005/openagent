"""Internal definition query envelope and the three 2.3 consumer connections.

The public HTTP and frontend shapes are not reinterpreted here.  These tests
exercise the internal read-only query and the existing configuration,
editor-input and node-catalog call paths over trusted synthetic registrations
and temporary stores only.
"""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.capability_packages import (
    CapabilityPackage, CapabilityPackageLoader, PackageManifest,
)
from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import QUERIES
from phase1_agent.graph_contracts import NodeDefinition, NodePort, NodeRegistry
from phase1_agent.graph_input_declarations import editor_input_declarations
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition
from phase1_agent.runtime_executor_contracts import ExecutorDefinition, ExecutorReference
from phase1_agent.workflow_definitions import WorkflowDefinitionCatalog

from test_graph_service import document as graph_document
from test_graph_service import node as graph_node
from test_workflow_configuration import document as configuration_document
from test_workflow_configuration import registry as configuration_registry
from test_workflow_configuration import update as save_configuration


VIEW_FIELDS = {"kind", "registration_ref", "declaration", "source", "availability"}
NODE_REFERENCE = {"component_id": "test.text", "component_version": "1"}
HOSTED_REFERENCE = {"component_id": "test.hosted", "component_version": "2"}
TYPE_REFERENCE = {"scope": "session", "type_id": "sample.definition", "schema_version": 1}


def node_definition(component_id, component_version, *, inputs=(), outputs=(), capabilities=()):
    return NodeDefinition(
        component_id, component_version, "Definition consumer node", "Test", {},
        {"type": "object", "additionalProperties": False},
        inputs=inputs, outputs=outputs, capabilities=capabilities)


def definition_registry():
    """One plain TEXT node, one hosted node and one exact data type."""
    registry = NodeRegistry()
    registry.data_types.register(DataTypeDefinition(
        "sample.definition", 1, {"type": "object"}, scope="session"))
    registry.register(node_definition(
        "test.text", "1",
        inputs=(NodePort("input", "TEXT"),),
        outputs=(NodePort("output", "TEXT"),)), None)
    reference = ExecutorReference("test.executor", "1")
    registry.executors.register_executor(
        ExecutorDefinition(reference), lambda *args, **kwargs: None)
    registry.register(node_definition(
        "test.hosted", "2", outputs=(NodePort("output", "TEXT"),)),
        None, executor_ref=reference)
    return registry


def test_definition_query_returns_the_exact_five_field_envelope():
    catalog = WorkflowDefinitionCatalog(definition_registry())

    view = catalog.get("node", NODE_REFERENCE)
    assert set(view) == VIEW_FIELDS
    assert view["kind"] == "node"
    assert view["registration_ref"] == NODE_REFERENCE
    assert view["declaration"]["component_id"] == "test.text"
    assert view["declaration"]["component_version"] == "1"
    assert view["declaration"]["executable"] is False
    assert view["source"] == []
    assert view["availability"] == "registered"

    type_view = catalog.get("data_type", TYPE_REFERENCE)
    assert set(type_view) == VIEW_FIELDS
    assert type_view["kind"] == "data_type"
    assert type_view["registration_ref"] == TYPE_REFERENCE
    assert type_view["declaration"]["type_id"] == "sample.definition"
    assert type_view["declaration"]["schema_version"] == 1
    assert type_view["declaration"]["schema"] == {"type": "object"}
    assert type_view["source"] == []
    assert type_view["availability"] == "registered"


@pytest.mark.parametrize("kind,reference", [
    ("prompt", NODE_REFERENCE),
    ("node", {"component_id": "test.text"}),
    ("node", {"component_id": "test.text", "component_version": "1", "extra": True}),
    ("node", {"component_id": "", "component_version": "1"}),
    ("data_type", {"scope": "unknown", "type_id": "sample.definition", "schema_version": 1}),
    ("data_type", {"scope": "session", "type_id": "sample.definition", "schema_version": 0}),
    ("data_type", {"scope": "session", "type_id": "sample.definition", "schema_version": True}),
])
def test_definition_query_rejects_unknown_kind_and_invalid_reference(kind, reference):
    catalog = WorkflowDefinitionCatalog(definition_registry())
    if kind not in ("node", "data_type"):
        with pytest.raises(ContractValidationError) as error:
            catalog.get(kind, reference)
        assert error.value.reason_code == "definition_unknown_kind"
        with pytest.raises(ContractValidationError) as listed:
            catalog.list(kind)
        assert listed.value.reason_code == "definition_unknown_kind"
        return
    with pytest.raises(ContractValidationError) as error:
        catalog.get(kind, reference)
    assert error.value.reason_code == "definition_invalid_reference"


def test_definition_query_requires_an_exact_registered_version():
    catalog = WorkflowDefinitionCatalog(definition_registry())
    for kind, reference in (
            ("node", {"component_id": "test.text", "component_version": "9"}),
            ("data_type", {"scope": "session", "type_id": "sample.definition", "schema_version": 2})):
        assert catalog.get(kind, reference) is None
        with pytest.raises(ContractValidationError) as error:
            catalog.require(kind, reference)
        assert error.value.reason_code == "definition_missing"


def test_definition_views_do_not_expose_mutable_registration_state():
    registry = definition_registry()
    catalog = WorkflowDefinitionCatalog(registry)
    nodes_before = registry.catalog()
    types_before = registry.data_types.catalog()
    node_view = catalog.get("node", NODE_REFERENCE)
    type_view = catalog.get("data_type", TYPE_REFERENCE)

    node_view["declaration"]["display_name"] = "mutated"
    node_view["declaration"]["inputs"][0]["data_type"] = "JSON"
    node_view["registration_ref"]["component_id"] = "mutated.node"
    node_view["source"].append({"package_id": "mutated.package", "package_version": "0"})
    node_view["availability"] = "unavailable"
    type_view["declaration"]["schema"]["type"] = "string"

    assert catalog.get("node", NODE_REFERENCE)["declaration"]["display_name"] == "Definition consumer node"
    assert catalog.get("node", NODE_REFERENCE)["registration_ref"] == NODE_REFERENCE
    assert catalog.get("node", NODE_REFERENCE)["source"] == []
    assert catalog.get("node", NODE_REFERENCE)["availability"] == "registered"
    assert catalog.get("data_type", TYPE_REFERENCE)["declaration"]["schema"] == {"type": "object"}
    assert catalog.node_catalog() == nodes_before
    assert registry.catalog() == nodes_before
    assert registry.data_types.catalog() == types_before


def shared_type_package(package_id):
    reference = {"scope": "session", "type_id": "sample.shared", "schema_version": 1}

    def register(host):
        host.register_shared_data_type(DataTypeDefinition(
            "sample.shared", 1, {"type": "object"}, scope="session"))

    return CapabilityPackage(
        PackageManifest(package_id, "1.0.0", exports={"data_types": [reference]}), register)


def test_shared_data_type_query_reports_both_providers_in_stable_order():
    loaded = CapabilityPackageLoader((
        shared_type_package("sample.first"), shared_type_package("sample.second"),
    )).load({"sample.first": "1.0.0", "sample.second": "1.0.0"})
    reference = {"scope": "session", "type_id": "sample.shared", "schema_version": 1}
    catalog = WorkflowDefinitionCatalog(loaded.registry, loaded.package_manifests)

    view = catalog.get("data_type", reference)
    assert [(item["package_id"], item["package_version"]) for item in view["source"]] == [
        ("sample.first", "1.0.0"), ("sample.second", "1.0.0")]
    assert view["declaration"] == loaded.registry.data_types.get(
        "sample.shared", 1, scope="session").to_dict()
    assert view["declaration"]["type_id"] == "sample.shared"
    assert view["declaration"]["schema"] == {"type": "object"}
    assert catalog.get("data_type", reference) == view
    # The same exact declaration is contributed by both manifests.
    assert [manifest["exports"]["data_types"] for manifest in loaded.package_manifests] == [
        [reference], [reference]]
    # Source order does not depend on how manifests are enumerated.
    reversed_catalog = WorkflowDefinitionCatalog(
        loaded.registry, tuple(reversed(loaded.package_manifests)))
    assert reversed_catalog.get("data_type", reference) == view


def test_list_and_node_catalog_project_only_their_own_category():
    registry = definition_registry()
    catalog = WorkflowDefinitionCatalog(registry)

    node_views = catalog.list("node")
    assert {view["kind"] for view in node_views} == {"node"}
    assert [view["registration_ref"] for view in node_views] == [HOSTED_REFERENCE, NODE_REFERENCE]
    assert all(view["source"] == [] and view["availability"] == "registered" for view in node_views)

    type_views = catalog.list("data_type")
    assert {view["kind"] for view in type_views} == {"data_type"}
    assert {(view["registration_ref"]["scope"], view["registration_ref"]["type_id"],
             view["registration_ref"]["schema_version"]) for view in type_views} == {
        (item["scope"], item["type_id"], item["schema_version"])
        for item in registry.data_types.catalog()}
    assert catalog.list() == [*node_views, *type_views]

    assert catalog.node_catalog() == registry.catalog()
    plain = next(item for item in catalog.node_catalog() if item["component_id"] == "test.text")
    hosted = next(item for item in catalog.node_catalog() if item["component_id"] == "test.hosted")
    assert plain["executable"] is False and "executor_ref" not in plain
    assert hosted["executable"] is True
    assert hosted["executor_ref"] == {"executor_id": "test.executor", "exact_version": "1"}


def record_definition_gets(monkeypatch):
    calls = []
    original = WorkflowDefinitionCatalog.get

    def recording_get(self, kind, reference):
        calls.append((kind, deepcopy(reference)))
        return original(self, kind, reference)

    monkeypatch.setattr(WorkflowDefinitionCatalog, "get", recording_get)
    return calls


def test_configuration_descriptor_reads_the_definition_query(monkeypatch, tmp_path):
    calls = record_definition_gets(monkeypatch)
    with closing(GraphWorkflowService(
            tmp_path / "definition-configuration.sqlite",
            registry=configuration_registry())) as service:
        saved = save_configuration(service, configuration_document())
        assert saved["outcome"] == "saved"
        assert calls
        assert all(kind == "node" for kind, _ in calls)
        assert {"component_id": "prompts.item", "component_version": "3"} in [
            reference for _, reference in calls]

        calls.clear()
        descriptor = service._configuration_descriptor(saved["document"]["nodes"][0])
        assert calls == [("node", {"component_id": "prompts.item", "component_version": "3"})]
        assert descriptor["adjustable_configuration"]["policy"] == "prompt"
        assert service.get_definition(
            saved["workflow_definition_id"])["nodes"][0]["config"]["text"] == "A"


def test_configuration_missing_node_still_reports_graph_missing_node_type(monkeypatch, tmp_path):
    calls = record_definition_gets(monkeypatch)
    with closing(GraphWorkflowService(
            tmp_path / "definition-configuration-missing.sqlite",
            registry=configuration_registry())) as service:
        with pytest.raises(ContractValidationError) as error:
            save_configuration(service, configuration_document(component="missing.component"))
        assert error.value.reason_code == "graph_missing_node_type"
        assert ("node", {"component_id": "missing.component", "component_version": "3"}) in calls
        assert service.list_definitions() == []


def test_editor_input_declarations_use_the_catalog_and_application_query(tmp_path):
    registry = create_package_registry().registry.detached()
    source = graph_node(registry, "current-input", 1)
    document = graph_document([source], [])
    catalog = WorkflowDefinitionCatalog(registry)

    direct = editor_input_declarations(registry, deepcopy(document), definitions=catalog)
    expected = [{"name": "text", "data_type": "TEXT", "required": True,
                 "node_ids": [source["node_binding_id"]], "conflict": False}]
    assert direct["inputs"] == expected
    assert direct["kind"] == "workflow.input-declarations"
    assert direct["required_for_run"] is None
    assert direct["diagnostics"]

    spec = next(item for item in QUERIES if item.name == "definition.inputs")
    assert spec.method == "_editor_inputs"
    assert spec.required == frozenset({"document"})
    with closing(GraphWorkflowService(
            tmp_path / "definition-inputs.sqlite", registry=registry)) as service:
        queried = GraphApplication(service).query(
            "definition.inputs", {"document": deepcopy(document)})
    assert queried == direct


def legacy_node_types(catalog):
    """The former protocol 1 filter, kept as an independent expectation."""
    builtin = {"TEXT", "PROMPT", "JSON"}
    result = []
    for node in catalog:
        ports = [*node["inputs"], *node["outputs"]]
        for mode in node.get("port_modes", {}).values():
            ports.extend([*mode["inputs"], *mode["outputs"]])
        if all(port["data_type"] in builtin and port.get("data_schema_version", 1) == 1
               for port in ports) and not set(node["capabilities"]) & {"objects:read", "objects:write"}:
            result.append(node)
    return result


def test_node_types_protocol_two_is_the_exact_registry_catalog(tmp_path):
    registry = create_package_registry().registry.detached()
    with closing(GraphWorkflowService(
            tmp_path / "definition-node-types.sqlite", registry=registry)) as service:
        result = service.node_types(protocol_version=2)
    assert result["schema_version"] == 2
    assert result["node_types"] == registry.catalog()


def test_node_types_protocol_one_keeps_the_legacy_filter(tmp_path):
    registry = create_package_registry().registry.detached()
    registry.register(node_definition(
        "sample.legacy-text", "1",
        inputs=(NodePort("input", "TEXT"),),
        outputs=(NodePort("output", "TEXT"),)), None)
    registry.register(node_definition(
        "sample.objects", "1",
        outputs=(NodePort("output", "TEXT"),),
        capabilities=("objects:write",)), None)

    with closing(GraphWorkflowService(
            tmp_path / "definition-legacy-catalog.sqlite", registry=registry)) as service:
        legacy = service.node_types(protocol_version=1)
        current = service.node_types(protocol_version=2)

    assert legacy["schema_version"] == 1
    assert legacy["node_types"] == legacy_node_types(registry.catalog())
    components = {item["component_id"] for item in legacy["node_types"]}
    assert "sample.legacy-text" in components
    assert "sample.objects" not in components
    assert "tools.current-input" not in components
    current_components = {item["component_id"] for item in current["node_types"]}
    assert {"sample.legacy-text", "sample.objects", "tools.current-input"} <= current_components
