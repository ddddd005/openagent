import sys
import types

import pytest

from phase1_agent.capability_packages import (
    CapabilityPackage, CapabilityPackageLoader, PackageDependency, PackageManifest,
    create_compatibility_package, discover_trusted_packages,
)
from phase1_agent.graph_contracts import GraphCompiler, NodeDefinition, NodePort, NodeRegistry, text_value
from phase1_agent.graph_nodes import create_default_registry, create_package_registry
from phase1_agent.host_sdk import DataTypeDefinition, HostContractError

from test_graph_execution import document, edge, node, run_graph


def package(package_id, register=lambda host: None, *, version="1.0.0", dependencies=(), protocol=1, exports=None):
    return CapabilityPackage(PackageManifest(package_id, version, tuple(dependencies), protocol, exports or {}), register)


def test_independent_package_declares_types_node_and_optional_frontend_without_core_branches():
    def register(host):
        host.register_data_type(DataTypeDefinition("sample.counter", 1, {"type": "integer"}, 0))
        host.register_node(NodeDefinition(
            "sample.upper", "1", "Upper", "Sample", {}, {"type": "object", "additionalProperties": False},
            inputs=(NodePort("input", "TEXT"),), outputs=(NodePort("output", "TEXT"),),
        ), lambda config, inputs, context: {"output": text_value(inputs["input"]["text"].upper())})
        host.register_frontend_extension("sample.upper.editor", "field-editor", "sample/frontend:Upper",
                                         component_id="sample.upper", component_version="1")

    loaded = create_package_registry(packages=(package("sample", register),),
                                    enabled={"sample": "1.0.0", "workflow.compat": "1.0.0"})
    graph = document([node(1, config={"text": "ready"}), node(2, "sample.upper", registry=loaded.registry),
                      node(3, "workflow.output")], [edge(10, 1, 2), edge(11, 2, 3)])
    assert run_graph(graph, loaded.registry).outputs[node(3)["node_binding_id"]]["output"]["text"] == "READY"
    assert loaded.registry.data_types.default("sample.counter", 1) == 0
    assert loaded.frontend_extensions[0]["package_id"] == "sample"
    assert loaded.package_lock == ({"package_id": "sample", "version": "1.0.0"},
                                   {"package_id": "workflow.compat", "version": "1.0.0"})
    with pytest.raises(Exception, match="frozen"):
        loaded.registry.register(NodeDefinition("other", "1", "Other", "Other", {}, {}), None)


def test_dependencies_register_in_topological_order_and_lock_every_resolved_version():
    order = []
    first = package("a", lambda host: order.append("a"),
                    dependencies=(PackageDependency("z", "2.0.0"),))
    second = package("z", lambda host: order.append("z"), version="2.0.0")
    loaded = CapabilityPackageLoader((first, second)).load({"a": "1.0.0"})
    assert order == ["z", "a"]
    assert loaded.package_lock == ({"package_id": "a", "version": "1.0.0"},
                                   {"package_id": "z", "version": "2.0.0"})
    assert [manifest["package_id"] for manifest in loaded.package_manifests] == ["z", "a"]


def test_compatibility_shared_transport_contract_is_independent_of_dependency_loading_order():
    from phase1_agent.content_contracts import create_content_package
    from phase1_agent.prompt_package import create_prompt_package

    synthetic = package(
        "context-test.synthetic",
        dependencies=(PackageDependency("workflow.content", "1.0.0"),
                      PackageDependency("workflow.prompts", "1.0.0")))
    loaded = CapabilityPackageLoader((
        create_compatibility_package(create_default_registry()),
        create_content_package(), create_prompt_package(), synthetic,
    )).load({"workflow.compat": "1.0.0", "context-test.synthetic": "1.0.0"})
    assert loaded.registry.data_types.get("GLOBAL_RESOURCE_REF", 1, scope="content") is not None
    assert [item["package_id"] for item in loaded.package_manifests].index("workflow.content") < [
        item["package_id"] for item in loaded.package_manifests].index("workflow.compat")


@pytest.mark.parametrize("packages,enabled,reason", [
    ((package("a", dependencies=(PackageDependency("missing", "1.0.0"),)),),
     {"a": "1.0.0"}, "package_missing_dependency"),
    ((package("a", dependencies=(PackageDependency("b", "1.0.0"),)),
      package("b", dependencies=(PackageDependency("a", "1.0.0"),))),
     {"a": "1.0.0"}, "package_dependency_cycle"),
    ((package("a", dependencies=(PackageDependency("b", "1.0.0"),)),
      package("b"), package("b", version="2.0.0")),
     {"a": "1.0.0", "b": "2.0.0"}, "package_dependency_conflict"),
    ((package("a", protocol=99),), {"a": "1.0.0"}, "package_protocol_mismatch"),
])
def test_resolution_rejects_missing_cyclic_conflicting_or_incompatible_packages_before_registration(packages, enabled, reason):
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader(packages).load(enabled)
    assert caught.value.reason_code == reason


def test_partial_registration_failure_does_not_modify_base_or_previous_loaded_runtime():
    base = NodeRegistry()

    def good(host):
        host.register_data_type(DataTypeDefinition("sample.state", 1, {"type": "integer"}, 0))

    def bad(host):
        host.register_data_type(DataTypeDefinition("sample.partial", 1, {"type": "string"}, ""))
        raise RuntimeError("broken entry")

    loader = CapabilityPackageLoader((package("good", good), package("bad", bad)), base_registry=base)
    active = loader.load({"good": "1.0.0"})
    with pytest.raises(HostContractError) as caught:
        loader.load({"good": "1.0.0", "bad": "1.0.0"})
    assert caught.value.reason_code == "package_registration_failed"
    assert base.data_types.get("sample.state", 1, scope="session") is None
    assert active.registry.data_types.get("sample.partial", 1, scope="session") is None
    assert loader.load({"good": "1.0.0"}).registry.data_types.default("sample.state", 1) == 0


def test_upgrade_and_disable_return_new_registries_and_preserve_old_frozen_executor():
    def register(text):
        return lambda host: host.register_node(NodeDefinition(
            "sample.source", "1", "Source", "Sample", {}, {"type": "object"},
            outputs=(NodePort("output", "TEXT"),), is_output=True,
        ), lambda config, inputs, context: {"output": text_value(text)})

    loader = CapabilityPackageLoader((package("sample", register("old")),
                                     package("sample", register("new"), version="2.0.0")))
    old = loader.load({"sample": "1.0.0"})
    new = loader.load({"sample": "2.0.0"})
    disabled = loader.load({})
    graph = document([node(1, "sample.source", registry=old.registry)])
    assert run_graph(graph, old.registry).outputs[node(1)["node_binding_id"]]["output"]["text"] == "old"
    assert run_graph(graph, new.registry).outputs[node(1)["node_binding_id"]]["output"]["text"] == "new"
    assert disabled.registry.get("sample.source", "1") is None
    assert old.registry.get("sample.source", "1") is not None
    with pytest.raises(HostContractError, match="explicit"):
        loader.load()


def test_declared_exports_are_checked_and_duplicate_registration_is_atomic():
    missing = package("sample", exports={"nodes": [{"component_id": "sample.source", "component_version": "1"}]})
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader((missing,)).load({"sample": "1.0.0"})
    assert caught.value.reason_code == "package_export_mismatch"
    duplicate = package("sample", lambda host: (
        host.register_data_type(DataTypeDefinition("sample.value", 1, {}, None)),
        host.register_data_type(DataTypeDefinition("sample.value", 1, {}, None)),
    ))
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader((duplicate,)).load({"sample": "1.0.0"})
    assert caught.value.reason_code == "host_duplicate_type"


def test_package_upgrade_cannot_reinterpret_a_stored_schema_version():
    def register(schema, default):
        return lambda host: host.register_data_type(DataTypeDefinition("sample.value", 1, schema, default))

    loader = CapabilityPackageLoader((
        package("sample", register({"type": "string"}, "")),
        package("sample", register({"type": "integer"}, 0), version="2.0.0"),
    ))
    first = loader.load({"sample": "1.0.0"})
    with pytest.raises(HostContractError) as caught:
        loader.load({"sample": "2.0.0"})
    assert caught.value.reason_code == "package_contract_redefined"
    assert first.registry.data_types.default("sample.value", 1) == ""
    assert loader.load({}).registry.data_types.get("sample.value", 1, scope="session") is None
    assert loader.load({"sample": "1.0.0"}).registry.data_types.default("sample.value", 1) == ""


def test_compatibility_package_keeps_existing_node_declarations_and_versioned_global_types():
    registry = create_default_registry()
    loaded = CapabilityPackageLoader((create_compatibility_package(registry),)).load({"workflow.compat": "1.0.0"})
    assert loaded.registry.catalog() == registry.catalog()
    assert loaded.registry.data_types.catalog() == registry.data_types.catalog()
    graph = {**document([node(1, config={"text": "compat"}), node(2, "workflow.output")], [edge(10, 1, 2)]),
             "schema_version": 2, "object_bindings": [],
             "package_lock": [{"package_id": "workflow.compat", "version": "1.0.0"}]}
    assert GraphCompiler(loaded.registry).compile(graph).document == graph


def test_registered_resource_preflight_hook_survives_compatibility_loading_and_detach():
    calls = []
    registry = NodeRegistry()
    validator = lambda config, records: calls.append((config, records))
    registry.register(NodeDefinition("sample.resource", "1", "Resource", "Sample", {}, {}),
                      None, resource_preflight_validator=validator)
    loaded = CapabilityPackageLoader((create_compatibility_package(registry),)).load({"workflow.compat": "1.0.0"})
    frozen = loaded.registry.detached(frozen=True)
    entry = frozen.get("sample.resource", "1")
    entry.resource_preflight_validator({"resource_id": "chosen"}, [{"resource_id": "chosen", "enabled": True}])
    assert calls == [({"resource_id": "chosen"}, [{"resource_id": "chosen", "enabled": True}])]


def test_local_discovery_uses_only_explicit_trusted_module_entries(monkeypatch):
    module = types.ModuleType("test_trusted_capability")
    module.entry = lambda: package("local")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    discovered = discover_trusted_packages(("test_trusted_capability:entry",))
    assert discovered[0].manifest.package_id == "local"
    assert CapabilityPackageLoader(discovered).load({"local": "1.0.0"}).package_lock[0]["package_id"] == "local"
    with pytest.raises(HostContractError, match="module:attribute"):
        discover_trusted_packages(("https://example.com/plugin.py",))
    with pytest.raises(HostContractError, match="could not be loaded"):
        discover_trusted_packages(("test_trusted_capability:missing",))
