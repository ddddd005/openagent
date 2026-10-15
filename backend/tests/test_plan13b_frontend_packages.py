"""Complete frontend composition, binding contracts and persisted selection."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageManifest
from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_package import FRONTEND_EXTENSIONS, create_frontend_package
from phase1_agent.frontend_business import create_frontend_business_package
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import HostContractError
from phase1_agent.model_package import MODEL_FRONTEND_EXTENSIONS
from phase1_agent.prompt_package import PROMPT_FRONTEND_EXTENSIONS
from phase1_agent.tavern.package import CURRENT_TAVERN_FRONTEND_EXTENSIONS

from frontend_node_test_support import create, graph as frontend_graph, save as save_frontend_graph
from test_graph_service import run
from test_graph_package_selection import saved_selection


def loader():
    return CapabilityPackageLoader(builtin_capability_packages())


def expected_workbench_extensions(*, frontend, tavern=True):
    declarations = [*MODEL_FRONTEND_EXTENSIONS, *PROMPT_FRONTEND_EXTENSIONS]
    if tavern:
        declarations.extend(CURRENT_TAVERN_FRONTEND_EXTENSIONS)
    if frontend:
        declarations.extend(FRONTEND_EXTENSIONS)
    return {row["extension_id"] for row in declarations}


def service_extension_ids(service):
    return {row["extension_id"] for row in service.platform_capabilities()["frontend_extensions"]}


def local_extension(identity="local", *, target=None, entrypoint="local.panel", protocol=1):
    declaration = {
        "extension_id": identity, "kind": "workbench-panel", "entrypoint": entrypoint,
        "binding": {"surface": "workbench", "slot": "panel", "target": {}},
        "host_protocol_version": protocol,
    }
    if target is not None:
        declaration.update(kind="renderer",
                           binding={"surface": "workbench", "slot": "session-object", "target": target})
    return CapabilityPackage(PackageManifest(identity, "1"), lambda host: host.register_frontend_extension(**declaration))


def test_complete_package_declares_exact_dependencies_and_current_bindings():
    package = create_frontend_package()
    assert package.manifest.to_dict()["dependencies"] == [
        {"package_id": "workflow.frontend-business", "version": "1.0.0"}]
    loaded = loader().load({"workflow.frontend": "1.0.0"})
    assert loaded.package_lock == (
        {"package_id": "workflow.content", "version": "1.0.0"},
        {"package_id": "workflow.frontend", "version": "1.0.0"},
        {"package_id": "workflow.frontend-business", "version": "1.0.0"},
    )
    expected = sorted([
        {**deepcopy(row),
         "schema_version": 3 if row["binding"]["slot"] == "node-fields" else 2,
         "host_protocol_version": 2 if row["binding"]["slot"] == "node-fields" else 1,
         "package_id": "workflow.frontend", "package_version": "1.0.0"}
        for row in FRONTEND_EXTENSIONS
    ], key=lambda row: row["extension_id"])
    assert list(loaded.frontend_extensions) == expected
    assert loaded.registry.get("frontend.node", "2") is not None
    assert loaded.registry.get("frontend.node", "1") is None
    assert loaded.registry.data_types.get(CHAT_CONTEXT_TYPE, 1, scope="session") is not None


def test_business_package_remains_independent_without_ui_and_disable_uses_detached_registry():
    installed = loader()
    complete = installed.load({"workflow.frontend": "1.0.0"})
    business = installed.load({"workflow.frontend-business": "1.0.0"})
    empty = installed.load({})
    assert business.frontend_extensions == ()
    assert business.registry.get("frontend.node", "2") is not None
    assert empty.registry.get("frontend.node", "2") is None
    assert len(complete.frontend_extensions) == 2
    node_entry = next(item for item in complete.frontend_extensions
                      if item["extension_id"] == "workflow.frontend.node-entry-fields-v2")
    assert node_entry["schema_version"] == 3 and node_entry["host_protocol_version"] == 2
    assert node_entry["entrypoint"] == "workflow.frontend.workbench.node-entry-fields"
    assert node_entry["binding"] == {
        "surface": "workbench", "slot": "node-fields",
        "target": {"component_id": "frontend.node", "component_version": "2"},
    }
    assert complete.registry.get("frontend.node", "2") is not None


def test_business_only_service_executes_frontend_graph_without_ui(tmp_path):
    selected = {key: value for key, value in DEFAULT_PACKAGES.items() if key != "workflow.frontend"}
    with closing(GraphWorkflowService(tmp_path / "business-only.sqlite", enabled_packages=selected)) as service:
        assert {row["package_id"] for row in service.platform_capabilities()["frontend_extensions"]} == {
            "workflow.models", "workflow.prompts", "workflow.tavern"}
        assert service_extension_ids(service) == expected_workbench_extensions(frontend=False)
        doc, _, frontends = frontend_graph(service, bound=True)
        doc = save_frontend_graph(service, doc)
        completed = run(service, create(service, doc))
        assert completed["status"] == "succeeded"
        assert completed["objects"][frontends[0]["config"]["object_key"]]["value"]["rounds"]


@pytest.mark.parametrize("packages,enabled,reason", [
    ((create_frontend_package(),), {"workflow.frontend": "1.0.0"}, "package_missing_dependency"),
    ((local_extension(target={"scope": "session", "type_id": "missing", "schema_version": 1}),),
     {"local": "1"}, "package_frontend_target_missing"),
    ((*builtin_capability_packages(),
      local_extension("one", target={"scope": "session", "type_id": CHAT_CONTEXT_TYPE, "schema_version": 1}),
      local_extension("two", target={"scope": "session", "type_id": CHAT_CONTEXT_TYPE, "schema_version": 1})),
     {"one": "1", "two": "1", "workflow.frontend-business": "1.0.0"},
     "package_duplicate_frontend_binding"),
    ((local_extension(protocol=3),), {"local": "1"}, "package_extension_protocol_mismatch"),
    ((local_extension(entrypoint="https://example.com/plugin.js"),), {"local": "1"}, "package_invalid_extension"),
])
def test_invalid_bindings_dependencies_and_remote_entrypoints_fail(packages, enabled, reason):
    with pytest.raises(HostContractError) as failure:
        CapabilityPackageLoader(packages).load(enabled)
    assert failure.value.reason_code == reason


def test_independent_workbench_panels_can_coexist_but_extension_ids_are_unique():
    installed = CapabilityPackageLoader((local_extension("one"), local_extension("two")))
    assert len(installed.load({"one": "1", "two": "1"}).frontend_extensions) == 2
    second = CapabilityPackage(PackageManifest("two", "1"), local_extension("one").register)
    with pytest.raises(HostContractError) as failure:
        CapabilityPackageLoader((local_extension("one"), second)).load({"one": "1", "two": "1"})
    assert failure.value.reason_code == "package_duplicate_extension"


def test_target_validation_runs_after_all_registrations_and_does_not_reinterpret_legacy_extensions():
    from phase1_agent.host_sdk import DataTypeDefinition

    later = CapabilityPackage(PackageManifest("z", "1"), lambda host: host.register_data_type(
        DataTypeDefinition("later.state", 1, {"type": "integer"}, 0)))
    early = local_extension("a", target={"scope": "session", "type_id": "later.state", "schema_version": 1})
    assert len(CapabilityPackageLoader((early, later)).load({"a": "1", "z": "1"}).frontend_extensions) == 1
    legacy = CapabilityPackage(PackageManifest("legacy", "1"), lambda host: host.register_frontend_extension(
        "legacy.remote-looking", "renderer", "trusted/old:Renderer", component_id="not-installed", component_version="1"))
    loaded = CapabilityPackageLoader((legacy,)).load({"legacy": "1"})
    assert "schema_version" not in loaded.frontend_extensions[0]
    assert "binding" not in loaded.frontend_extensions[0]


def test_complete_package_rejects_conflicting_exact_business_version():
    from phase1_agent.content_contracts import create_content_package

    business = create_frontend_business_package()
    newer = CapabilityPackage(PackageManifest("workflow.frontend-business", "2.0.0"), business.register)
    installed = CapabilityPackageLoader((create_content_package(), business, newer, create_frontend_package()))
    with pytest.raises(HostContractError) as failure:
        installed.load({"workflow.frontend": "1.0.0", "workflow.frontend-business": "2.0.0"})
    assert failure.value.reason_code == "package_dependency_conflict"


@pytest.mark.parametrize("slot", ["session-object", "node-fields"])
def test_every_v2_target_requires_exact_registered_version(slot):
    from phase1_agent.content_contracts import create_content_package

    declaration = deepcopy(next(row for row in FRONTEND_EXTENSIONS if row["binding"]["slot"] == slot))
    target = declaration["binding"]["target"]
    if slot == "node-fields":
        target["component_version"] = declaration["component_version"] = "3"
    else:
        target["schema_version"] = 2
    package = CapabilityPackage(PackageManifest("invalid-ui", "1"), lambda host: host.register_frontend_extension(
        **declaration, host_protocol_version=1))
    installed = CapabilityPackageLoader((create_content_package(), create_frontend_business_package(), package))
    with pytest.raises(HostContractError) as failure:
        installed.load({"workflow.frontend-business": "1.0.0", "invalid-ui": "1"})
    assert failure.value.reason_code == "package_frontend_target_missing"


def test_frontend_protocol_two_does_not_raise_package_or_runtime_protocol():
    from phase1_agent.content_contracts import create_content_package

    extension = deepcopy(next(row for row in FRONTEND_EXTENSIONS if row["binding"]["slot"] == "node-fields"))
    package = CapabilityPackage(PackageManifest("frontend-v2", "1"), lambda host:
        host.register_frontend_extension(**extension, host_protocol_version=2))
    loaded = CapabilityPackageLoader((create_content_package(), create_frontend_business_package(), package)).load(
        {"workflow.frontend-business": "1.0.0", "frontend-v2": "1"})
    registered = next(row for row in loaded.frontend_extensions if row["extension_id"] == extension["extension_id"])
    manifest = next(row for row in loaded.package_manifests if row["package_id"] == "frontend-v2")
    assert registered["schema_version"] == 3 and registered["host_protocol_version"] == 2
    assert manifest["host_protocol_version"] == 1
    assert loaded.to_dict()["host_protocol_version"] == 1


def test_same_package_exact_version_cannot_change_a_previously_loaded_binding():
    changed = {"entrypoint": "local.first"}

    def register(host):
        host.register_frontend_extension("local.panel", "workbench-panel", changed["entrypoint"],
                                         binding={"surface": "workbench", "slot": "panel", "target": {}},
                                         host_protocol_version=1)

    installed = CapabilityPackageLoader((CapabilityPackage(PackageManifest("local", "1"), register),))
    first = installed.load({"local": "1"})
    changed["entrypoint"] = "local.changed"
    with pytest.raises(HostContractError) as failure:
        installed.load({"local": "1"})
    assert failure.value.reason_code == "package_contract_redefined"
    assert first.frontend_extensions[0]["entrypoint"] == "local.first"
    changed["entrypoint"] = "local.first"
    assert installed.load({"local": "1"}).frontend_extensions == first.frontend_extensions


def test_failed_final_binding_validation_does_not_publish_partial_contracts():
    target = {"scope": "session", "type_id": CHAT_CONTEXT_TYPE, "schema_version": 1}
    installed = CapabilityPackageLoader((*builtin_capability_packages(),
                                        local_extension("one", target=target), local_extension("two", target=target)))
    first = installed.load({"one": "1", "workflow.frontend-business": "1.0.0"})
    with pytest.raises(HostContractError):
        installed.load({"one": "1", "two": "1", "workflow.frontend-business": "1.0.0"})
    assert installed.load({"two": "1", "workflow.frontend-business": "1.0.0"}).frontend_extensions[0]["extension_id"] == "two"
    assert first.frontend_extensions[0]["extension_id"] == "one"


def test_new_project_defaults_complete_package_and_old_saved_selection_is_not_upgraded(tmp_path):
    assert DEFAULT_PACKAGES["workflow.frontend"] == "1.0.0"
    database = tmp_path / "selection.sqlite"
    legacy = {key: value for key, value in DEFAULT_PACKAGES.items() if key != "workflow.frontend"}
    with closing(GraphWorkflowService(database, enabled_packages=legacy)) as service:
        assert service_extension_ids(service) == expected_workbench_extensions(frontend=False)
        assert service.registry.get("frontend.node", "2") is not None
    with closing(GraphWorkflowService(database)) as service:
        assert service_extension_ids(service) == expected_workbench_extensions(frontend=False)
        service.configure_capability_packages(DEFAULT_PACKAGES)
        assert service_extension_ids(service) == expected_workbench_extensions(frontend=True)
    with closing(GraphWorkflowService(database)) as service:
        assert service_extension_ids(service) == expected_workbench_extensions(frontend=True)
    with closing(GraphWorkflowService(tmp_path / "new.sqlite")) as service:
        assert service_extension_ids(service) == expected_workbench_extensions(frontend=True)


def test_saved_selection_without_tavern_does_not_gain_its_nodes_or_extensions_on_reopen(tmp_path):
    database = tmp_path / "pre-tavern-selection.sqlite"
    selected = {key: value for key, value in DEFAULT_PACKAGES.items() if key != "workflow.tavern"}
    expected_extensions = expected_workbench_extensions(frontend=True, tavern=False)
    with closing(GraphWorkflowService(database, enabled_packages=selected)) as service:
        assert service_extension_ids(service) == expected_extensions
        assert service.registry.get("lorebook.item", "1") is None
        assert service.registry.get("lorebook.group", "1") is None
        lock, catalog = service.registry.package_lock, service.registry.catalog()
        assert all(row["package_id"] != "workflow.tavern" for row in lock)
    persisted = saved_selection(database)
    with closing(GraphWorkflowService(database)) as reopened:
        assert reopened.registry.package_lock == lock
        assert reopened.registry.catalog() == catalog
        assert service_extension_ids(reopened) == expected_extensions
        assert reopened.platform_capabilities()["package_diagnostics"] == []
        assert reopened.registry.get("lorebook.item", "1") is None
        assert reopened.registry.get("lorebook.group", "1") is None
    assert saved_selection(database) == persisted


def test_missing_ui_package_on_reopen_does_not_activate_fallback_or_delete_saved_data(tmp_path, monkeypatch):
    from phase1_agent import builtin_packages

    database = tmp_path / "missing-ui.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        doc, _, frontends = frontend_graph(service, bound=True)
        doc = save_frontend_graph(service, doc)
        final = run(service, create(service, doc))
        sid = final["workflow_session_id"]
        state = deepcopy(final["objects"])
        historical = service.get_run(sid, final["selected_chain_run_id"])
    original = builtin_packages.builtin_capability_packages
    monkeypatch.setattr(builtin_packages, "builtin_capability_packages", lambda **kwargs: tuple(
        item for item in original(**kwargs) if item.manifest.package_id != "workflow.frontend"))
    with closing(GraphWorkflowService(database)) as service:
        catalog = service.platform_capabilities()
        assert catalog["package_diagnostics"][0]["reason_code"] == "package_missing_dependency"
        assert catalog["package_diagnostics"][0]["enabled_packages"] == DEFAULT_PACKAGES
        assert catalog["package_lock"] == [] and service.registry.catalog() == []
        assert catalog["frontend_extensions"] == []
        assert service.registry.get("frontend.node", "2") is None
        assert service.get_session(sid)["objects"] == state
        assert service.get_run(sid, final["selected_chain_run_id"]) == historical
        with pytest.raises(ContractValidationError) as denied:
            run(service, service.get_session(sid))
        assert denied.value.reason_code in ("package_missing_dependency", "graph_missing_package")
        assert not hasattr(service, "_native_runtime")
