"""Complete frontend composition, binding contracts and persisted selection."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageManifest
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_package import FRONTEND_EXTENSIONS, create_frontend_package
from phase1_agent.frontend_business import create_frontend_business_package
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import HostContractError
from phase1_agent.model_package import MODEL_FRONTEND_EXTENSIONS
from phase1_agent.prompt_package import PROMPT_FRONTEND_EXTENSIONS
from phase1_agent.tavern.package import CHAT_TAVERN_FRONTEND_EXTENSIONS

from test_graph_service import create
from test_graph_package_selection import saved_selection
from test_plan13a_frontend_public import frontend_graph
from graph_test_plugin import run_current_graph as run


def loader():
    return CapabilityPackageLoader(builtin_capability_packages())


def expected_workbench_extensions(*, frontend, tavern=True):
    declarations = [*MODEL_FRONTEND_EXTENSIONS, *PROMPT_FRONTEND_EXTENSIONS]
    if tavern:
        declarations.extend(CHAT_TAVERN_FRONTEND_EXTENSIONS)
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


def test_complete_package_declares_exact_dependencies_and_four_bindings():
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
        {**deepcopy(row), "schema_version": 2, "host_protocol_version": 1,
         "package_id": "workflow.frontend", "package_version": "1.0.0"}
        for row in FRONTEND_EXTENSIONS
    ], key=lambda row: row["extension_id"])
    assert list(loaded.frontend_extensions) == expected
    assert loaded.registry.get("frontend.state.append", "1") is not None
    with pytest.raises(Exception, match="frozen"):
        loaded.registry.data_types.register(loaded.registry.data_types.get(
            "workflow.frontend-state", 1, scope="session"))


def test_business_package_remains_independent_without_ui_and_disable_uses_detached_registry():
    installed = loader()
    complete = installed.load({"workflow.frontend": "1.0.0"})
    business = installed.load({"workflow.frontend-business": "1.0.0"})
    empty = installed.load({})
    assert business.frontend_extensions == ()
    assert business.registry.get("frontend.presentation", "1") is not None
    assert empty.registry.get("frontend.presentation", "1") is None
    assert len(complete.frontend_extensions) == 4
    assert complete.registry.get("frontend.state.append", "1") is not None


def test_business_only_service_executes_frontend_graph_without_ui(tmp_path):
    selected = {key: value for key, value in DEFAULT_PACKAGES.items() if key != "workflow.frontend"}
    with closing(GraphWorkflowService(tmp_path / "business-only.sqlite", enabled_packages=selected)) as service:
        assert {row["package_id"] for row in service.platform_capabilities()["frontend_extensions"]} == {
            "workflow.models", "workflow.prompts", "workflow.tavern"}
        assert service_extension_ids(service) == expected_workbench_extensions(frontend=False)
        doc, _, _, _, presentation = frontend_graph(service)
        completed = run(service, create(service, doc), inputs={"text": "business-only"})
        assert completed["status"] == "succeeded"
        assert completed["objects"]["frontend"]["value"]["entries"]
        assert service.read_public_output(completed["workflow_session_id"],
            workflow_definition_id=doc["workflow_definition_id"], definition_revision=doc["revision"],
            node_id=presentation["node_binding_id"], port_id="display")["output"]["availability"] == "produced"


@pytest.mark.parametrize("packages,enabled,reason", [
    ((create_frontend_package(),), {"workflow.frontend": "1.0.0"}, "package_missing_dependency"),
    ((local_extension(target={"scope": "session", "type_id": "missing", "schema_version": 1}),),
     {"local": "1"}, "package_frontend_target_missing"),
    ((*builtin_capability_packages(),
      local_extension("one", target={"scope": "session", "type_id": "workflow.frontend-state", "schema_version": 1}),
      local_extension("two", target={"scope": "session", "type_id": "workflow.frontend-state", "schema_version": 1})),
     {"one": "1", "two": "1", "workflow.frontend-business": "1.0.0"},
     "package_duplicate_frontend_binding"),
    ((local_extension(protocol=2),), {"local": "1"}, "package_extension_protocol_mismatch"),
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


@pytest.mark.parametrize("slot", ["session-object", "node-fields", "public-output"])
def test_every_v2_target_requires_exact_registered_version(slot):
    from phase1_agent.content_contracts import create_content_package

    declaration = deepcopy(next(row for row in FRONTEND_EXTENSIONS if row["binding"]["slot"] == slot))
    target = declaration["binding"]["target"]
    if slot == "node-fields":
        target["component_version"] = declaration["component_version"] = "2"
    else:
        target["schema_version"] = 2
    package = CapabilityPackage(PackageManifest("invalid-ui", "1"), lambda host: host.register_frontend_extension(
        **declaration, host_protocol_version=1))
    installed = CapabilityPackageLoader((create_content_package(), create_frontend_business_package(), package))
    with pytest.raises(HostContractError) as failure:
        installed.load({"workflow.frontend-business": "1.0.0", "invalid-ui": "1"})
    assert failure.value.reason_code == "package_frontend_target_missing"


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
    target = {"scope": "session", "type_id": "workflow.frontend-state", "schema_version": 1}
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
        assert service.registry.get("frontend.state.append", "1") is not None
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


def test_ui_disable_reopen_and_reenable_preserve_state_and_public_history(tmp_path):
    database = tmp_path / "ui-disable.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        doc, _, _, _, presentation = frontend_graph(service)
        final = run(service, create(service, doc), inputs={"text": "accepted"})
        sid = final["workflow_session_id"]
        parameters = {"workflow_definition_id": doc["workflow_definition_id"], "definition_revision": doc["revision"],
                      "node_id": presentation["node_binding_id"], "port_id": "display"}
        active = service.consumer_frontend_extensions(sid, **parameters)
        assert [row["extension_id"] for row in active["frontend_extensions"]] == ["workflow.frontend.public-output"]
        assert all(row["kind"] == "consumer" for row in active["frontend_extensions"])
        assert active["workflow_session_id"] == sid
        previous = service.read_public_output(sid, **parameters)
        state = deepcopy(final["objects"])
        service.configure_capability_packages({key: value for key, value in DEFAULT_PACKAGES.items()
                                               if key != "workflow.frontend"})
        assert service.consumer_frontend_extensions(sid, **parameters)["frontend_extensions"] == []
        assert service.read_public_output(sid, **parameters)["output"] == previous["output"]
        assert service.get_session(sid)["objects"] == state
    with closing(GraphWorkflowService(database)) as service:
        assert service.consumer_frontend_extensions(sid, **parameters)["frontend_extensions"] == []
        assert service.get_session(sid)["objects"] == state
        service.configure_capability_packages(DEFAULT_PACKAGES)
        assert len(service.consumer_frontend_extensions(sid, **parameters)["frontend_extensions"]) == 1


def test_consumer_extensions_require_current_public_root_and_exact_definition(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "private.sqlite")) as service:
        doc, _, read, _, presentation = frontend_graph(service, public=False)
        session = create(service, doc)
        parameters = {"workflow_definition_id": doc["workflow_definition_id"], "definition_revision": doc["revision"],
                      "node_id": presentation["node_binding_id"], "port_id": "display"}
        for node_id, port_id in ((presentation["node_binding_id"], "display"), (read["node_binding_id"], "view")):
            with pytest.raises(ContractValidationError) as failure:
                service.consumer_frontend_extensions(session["workflow_session_id"],
                    **{**parameters, "node_id": node_id, "port_id": port_id})
            assert failure.value.reason_code == "output_not_public"
        with pytest.raises(ContractValidationError) as failure:
            service.consumer_frontend_extensions(session["workflow_session_id"],
                **{**parameters, "definition_revision": doc["revision"] + 1})
        assert failure.value.reason_code == "consumer_definition_mismatch"


def test_missing_ui_package_on_reopen_does_not_activate_fallback_or_delete_saved_data(tmp_path, monkeypatch):
    from phase1_agent import builtin_packages

    database = tmp_path / "missing-ui.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        doc, _, _, _, presentation = frontend_graph(service)
        final = run(service, create(service, doc), inputs={"text": "saved"})
        sid = final["workflow_session_id"]
        parameters = {"workflow_definition_id": doc["workflow_definition_id"], "definition_revision": doc["revision"],
                      "node_id": presentation["node_binding_id"], "port_id": "display"}
        old = service.read_public_output(sid, **parameters)["output"]
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
        assert service.registry.get("frontend.state.append", "1") is None
        assert service.read_public_output(sid, **parameters)["output"] == old
        assert service.get_session(sid)["objects"]["frontend"]["value"]["entries"] == final["objects"]["frontend"]["value"]["entries"]
        assert service.get_run(sid, final["selected_chain_run_id"]) == historical
        with pytest.raises(ContractValidationError) as denied:
            run(service, service.get_session(sid), inputs={"text": "must not execute"})
        assert denied.value.reason_code == "package_missing_dependency"
        assert not hasattr(service, "_native_runtime")
