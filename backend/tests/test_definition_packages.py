"""Definition-stage proof over the real trusted package loading path.

Both sample definitions are registered by ``CapabilityPackage`` callbacks that
run through ``CapabilityPackageLoader`` and ``HostRegistration``.  Tests do not
inject the sample node or data type into a final registry themselves, and the
loader has no ambient base: ``workflow.content`` is an installed package that
the node requirement resolves for its TEXT@2 ports.  The execution test adds
``workflow.tools`` only to obtain the stock ``tools.text`` source node.
"""

import pytest

from phase1_agent.capability_packages import (
    CapabilityPackage, CapabilityPackageLoader, PackageDependency, PackageManifest,
)
from phase1_agent.content_contracts import create_content_package
from phase1_agent.definition_packages import (
    TrustedLocalPackageSource, WorkflowDefinitionPackages,
)
from phase1_agent.host_sdk import HostContractError
from phase1_agent.tool_package import create_tool_package
from phase1_agent.workflow_definitions import WorkflowDefinitionCatalog

from definition_stage_fixture import (
    NODE_REFERENCE, TYPE_REFERENCE, definition_stage_packages, partial_registration_package,
)
from test_graph_execution import document, edge, node, run_graph


NODE_REQUEST = {"sample.definition-node": "1"}
TYPES_SOURCE = [{"package_id": "sample.definition-types", "package_version": "1"}]
NODE_SOURCE = [{"package_id": "sample.definition-node", "package_version": "1"}]
STAGE_LOCK = (
    {"package_id": "sample.definition-node", "version": "1"},
    {"package_id": "sample.definition-types", "version": "1"},
    {"package_id": "workflow.content", "version": "1.0.0"},
)


def stage_source(*extra_packages):
    """Content is an installed package; the node requirement resolves it."""
    return TrustedLocalPackageSource(
        (create_content_package(), *definition_stage_packages(), *extra_packages))


def loaded_stage():
    return WorkflowDefinitionPackages(stage_source()).load(NODE_REQUEST)


def test_node_only_request_resolves_dependency_first_and_locks_exact_versions():
    packages = WorkflowDefinitionPackages(stage_source())
    resolved = packages.resolve(NODE_REQUEST)
    assert [item.package_id for item in resolved.registration_order] == [
        "sample.definition-types", "workflow.content", "sample.definition-node",
    ]

    loaded = packages.load(NODE_REQUEST)
    assert [manifest["package_id"] for manifest in loaded.package_manifests] == [
        "sample.definition-types", "workflow.content", "sample.definition-node",
    ]
    assert loaded.package_lock == STAGE_LOCK
    manifests = {manifest["package_id"]: manifest for manifest in loaded.package_manifests}
    assert manifests["sample.definition-types"]["version"] == "1"
    assert manifests["sample.definition-types"]["exports"]["data_types"] == [TYPE_REFERENCE]
    assert manifests["sample.definition-types"]["exports"]["nodes"] == []
    assert manifests["workflow.content"]["version"] == "1.0.0"
    assert manifests["sample.definition-node"]["version"] == "1"
    assert manifests["sample.definition-node"]["exports"]["nodes"] == [NODE_REFERENCE]
    assert manifests["sample.definition-node"]["exports"]["data_types"] == []

    assert loaded.registry.data_types.get("TEXT", 2, scope="content") is not None
    assert loaded.registry.data_types.get("sample.payload", 1, scope="session") is not None
    assert loaded.registry.get("sample.uppercase", "1") is not None


def test_catalog_reports_exact_source_for_loaded_node_and_data_type():
    loaded = loaded_stage()
    catalog = WorkflowDefinitionCatalog(loaded.registry, loaded.package_manifests)

    node_view = catalog.get("node", NODE_REFERENCE)
    assert node_view["kind"] == "node"
    assert node_view["registration_ref"] == NODE_REFERENCE
    assert node_view["declaration"]["component_id"] == "sample.uppercase"
    assert node_view["declaration"]["component_version"] == "1"
    assert node_view["source"] == NODE_SOURCE
    assert node_view["availability"] == "registered"

    type_view = catalog.get("data_type", TYPE_REFERENCE)
    assert type_view["kind"] == "data_type"
    assert type_view["registration_ref"] == TYPE_REFERENCE
    assert type_view["declaration"]["type_id"] == "sample.payload"
    assert type_view["declaration"]["schema"] == {"type": "integer"}
    assert type_view["source"] == TYPES_SOURCE
    assert type_view["availability"] == "registered"


def test_loaded_node_executes_uppercase_in_zero_agent_graph():
    packages = WorkflowDefinitionPackages(stage_source(create_tool_package()))
    loaded = packages.load({**NODE_REQUEST, "workflow.tools": "1.0.0"})
    value = document([
        node(1, config={"text": "ready"}, registry=loaded.registry),
        node(2, "sample.uppercase", registry=loaded.registry),
    ], [edge(10, 1, 2)])

    result = run_graph(value, loaded.registry)

    assert result.status == "succeeded"
    assert result.outputs[node(2)["node_binding_id"]]["output"]["text"] == "READY"
    assert WorkflowDefinitionCatalog(
        loaded.registry, loaded.package_manifests).get("node", NODE_REFERENCE)["source"] == NODE_SOURCE


def test_failed_dependency_and_partial_registration_keep_original_diagnostics_and_prior_load():
    orphan = CapabilityPackage(
        PackageManifest(
            "sample.definition-orphan", "1", schema_version=1,
            dependencies=(PackageDependency("sample.definition-types", "2"),),
        ),
        lambda host: None,
    )
    packages = WorkflowDefinitionPackages(
        stage_source(orphan, partial_registration_package()))
    loaded = packages.load(NODE_REQUEST)
    catalog = WorkflowDefinitionCatalog(loaded.registry, loaded.package_manifests)

    with pytest.raises(HostContractError) as caught:
        packages.load({"sample.definition-orphan": "1"})
    assert caught.value.reason_code == "package_missing_dependency"
    assert str(caught.value) == "Required package is unavailable: sample.definition-types@2"

    with pytest.raises(HostContractError) as caught:
        packages.load({"sample.definition-node": "1", "sample.definition-partial": "1"})
    assert caught.value.reason_code == "package_registration_failed"
    assert str(caught.value) == "Package registration failed: sample.definition-partial"
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "definition-stage fixture registration aborted"

    assert loaded.package_lock == STAGE_LOCK
    assert loaded.registry.get("sample.uppercase", "1") is not None
    assert loaded.registry.data_types.get("sample.payload", 1, scope="session") is not None
    assert loaded.registry.data_types.get("sample.partial", 1, scope="session") is None
    assert catalog.get("node", NODE_REFERENCE)["source"] == NODE_SOURCE
    assert catalog.get("data_type", TYPE_REFERENCE)["source"] == TYPES_SOURCE

    assert packages.load(NODE_REQUEST).package_lock == STAGE_LOCK


@pytest.mark.parametrize("requirements", [
    [("sample.definition-node", "1")],
    None,
    {"sample.definition-node": 1},
    {"sample.definition-node": ""},
    {"sample.definition-node": " 1 "},
    {"sample.definition-node": "v" * 129},
])
def test_workflow_packages_reject_non_dict_or_non_bounded_versions(requirements):
    packages = WorkflowDefinitionPackages(stage_source())
    with pytest.raises(HostContractError) as caught:
        packages.load(requirements)
    assert caught.value.reason_code == "definition_invalid_package_request"


def test_workflow_packages_preserve_loader_diagnostic_for_non_exact_version():
    packages = WorkflowDefinitionPackages(stage_source())
    with pytest.raises(HostContractError) as caught:
        packages.load({"sample.definition-node": "1.x"})
    assert caught.value.reason_code == "package_missing_dependency"
    assert str(caught.value) == "Required package is unavailable: sample.definition-node@1.x"


def test_loader_reports_manifest_export_mismatch():
    declared_but_absent = CapabilityPackage(
        PackageManifest(
            "sample.definition-declared", "1", schema_version=1,
            exports={"nodes": [dict(NODE_REFERENCE)]},
        ),
        lambda host: None,
    )
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader((declared_but_absent,)).load(
            {"sample.definition-declared": "1"})
    assert caught.value.reason_code == "package_export_mismatch"
    assert str(caught.value) == "Registered exports do not match the package manifest"
