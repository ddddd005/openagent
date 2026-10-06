"""Exact project selections and lazy compatibility without live models or databases."""

from contextlib import closing
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from phase1_agent import builtin_packages, graph_nodes
from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.capability_packages import (
    CapabilityPackage, CapabilityPackageLoader, PackageManifest,
    create_builtin_compatibility_package, create_compatibility_package,
)
from phase1_agent.content_contracts import create_content_package
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_nodes import create_package_registry
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore
from phase1_agent.tool_package import create_tool_package
from test_graph_service import gate_graph


def uid(number):
    return str(UUID(int=number, version=4))


def no_compat_registry():
    pytest.fail("An independent or unavailable project constructed the legacy registry")


def saved_selection(path):
    with closing(SqliteStore(path)) as store:
        row = store._connection.execute(
            "SELECT payload FROM graph_project_packages "
            "WHERE configuration_id IN ('current-execution','project') "
            "ORDER BY CASE configuration_id WHEN 'current-execution' THEN 0 ELSE 1 END LIMIT 1",
        ).fetchone()
        return row["payload"] if row else None


def selected_lock(selection):
    if selection is None:
        return {**DEFAULT_PACKAGES, "workflow.content": "1.0.0"}
    return {**selection, **({"workflow.content": "1.0.0"} if selection else {})}


def simple_document(service):
    nodes = []
    for number, component in ((1, "tools.text"), (2, "tools.output")):
        definition = service.registry.get(component, "1").definition
        config = deepcopy(definition.default_config)
        if number == 1:
            config["text"] = "preserved independent output"
        nodes.append({"node_binding_id": uid(number), "component_id": component, "component_version": "1",
                      "title": component, "position": {"x": number * 150, "y": 0}, "config": config})
    nodes[-1]["public_outputs"] = ["output"]
    return {
        "schema_version": 2, "workflow_definition_id": uid(3), "revision": 1,
        "name": "Independent selection", "nodes": nodes, "object_bindings": [],
        "package_lock": deepcopy(list(service.registry.execution_package_lock)),
        "edges": [{"edge_id": uid(4), "source_node_id": uid(1), "source_port_id": "output",
                   "target_node_id": uid(2), "target_port_id": "input", "order": 0}],
    }


@pytest.mark.parametrize("selection", [None, {}, {"workflow.tools": "1.0.0"}, {"workflow.prompts": "1.0.0"}],
                         ids=["default", "empty", "tools-only", "prompts-only"])
def test_fresh_and_reopened_service_respect_exact_selection_without_legacy_registry(
    tmp_path, monkeypatch, selection,
):
    monkeypatch.setattr(graph_nodes, "create_default_registry", no_compat_registry)
    path = tmp_path / "selection.sqlite"
    with closing(GraphWorkflowService(path, enabled_packages=selection)) as service:
        lock = {row["package_id"]: row["version"] for row in service.registry.package_lock}
        assert lock == selected_lock(selection)
        assert "workflow.compat" not in lock and service.registry.get("workflow.agent", "2") is None
        assert service.platform_capabilities()["package_diagnostics"] == []
        assert service._native_runtime is None
        catalog = service.registry.catalog()
        if selection == {}:
            assert catalog == [] and saved_selection(path) == "{}"
    persisted = saved_selection(path)
    with closing(GraphWorkflowService(path)) as reopened:
        assert {row["package_id"]: row["version"] for row in reopened.registry.package_lock} == lock
        assert reopened.registry.catalog() == catalog
        assert reopened.platform_capabilities()["package_diagnostics"] == []
        assert reopened._native_runtime is None
    assert saved_selection(path) == persisted


@pytest.mark.parametrize("selection", [None, {}, {"workflow.tools": "1.0.0"}, {"workflow.prompts": "1.0.0"}],
                         ids=["default", "empty", "tools-only", "prompts-only"])
def test_public_registry_helper_respects_exact_selection_without_constructing_compat(monkeypatch, selection):
    monkeypatch.setattr(graph_nodes, "create_default_registry", no_compat_registry)
    loaded = create_package_registry(enabled=selection)
    assert {row["package_id"]: row["version"] for row in loaded.package_lock} == selected_lock(selection)
    assert loaded.registry.get("workflow.text", "1") is None
    assert loaded.registry.get("workflow.agent", "2") is None


def test_builtin_compatibility_is_lazy_cached_and_preserves_original_manifest_and_catalog(monkeypatch):
    original = graph_nodes.create_default_registry()
    eager = create_compatibility_package(original)
    calls = []
    monkeypatch.setattr(graph_nodes, "create_default_registry", lambda: calls.append(True) or original)
    lazy = create_builtin_compatibility_package()
    assert lazy.manifest.to_dict() == eager.manifest.to_dict()
    loader = CapabilityPackageLoader((create_content_package(), create_tool_package(), lazy))
    loader.load({"workflow.tools": "1.0.0"})
    assert calls == []
    first = loader.load({"workflow.compat": "1.0.0"})
    assert calls == [True]
    assert first.registry.catalog() == original.catalog()
    assert first.registry.data_types.catalog() == original.data_types.catalog()
    loader.load({})
    again = loader.load({"workflow.compat": "1.0.0"})
    assert calls == [True] and again.registry.catalog() == first.registry.catalog()
    assert again.package_manifests == first.package_manifests


@pytest.mark.parametrize("selection", [{}, {"workflow.compat": "1.0.0"}], ids=["saved-empty", "saved-compat"])
def test_saved_selection_is_not_upgraded_when_application_defaults_change(tmp_path, monkeypatch, selection):
    path = tmp_path / "unchanged.sqlite"
    with closing(GraphWorkflowService(path, enabled_packages=selection)) as service:
        lock, catalog = service.registry.package_lock, service.registry.catalog()
    before = saved_selection(path)
    monkeypatch.setattr(builtin_packages, "DEFAULT_PACKAGES", {"not.installed": "1"})
    with closing(GraphWorkflowService(path)) as reopened:
        assert reopened.registry.package_lock == lock
        assert reopened.registry.catalog() == catalog
        assert reopened.platform_capabilities()["package_diagnostics"] == []
    assert saved_selection(path) == before


def test_explicit_missing_selection_is_rejected_without_replacing_saved_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_nodes, "create_default_registry", no_compat_registry)
    path = tmp_path / "explicit-missing.sqlite"
    with closing(GraphWorkflowService(path, enabled_packages={"workflow.tools": "1.0.0"})):
        pass
    before = saved_selection(path)
    with pytest.raises(ContractValidationError) as missing:
        GraphWorkflowService(path, enabled_packages={"not.installed": "1"})
    assert missing.value.reason_code == "package_missing_dependency"
    assert saved_selection(path) == before
    with closing(GraphWorkflowService(path)) as reopened:
        assert reopened.registry.get("tools.text", "1") is not None


def test_missing_new_default_is_not_silently_accepted_as_an_empty_project(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_nodes, "create_default_registry", no_compat_registry)
    original = builtin_capability_packages
    monkeypatch.setattr(builtin_packages, "builtin_capability_packages", lambda **kwargs: tuple(
        package for package in original(**kwargs) if package.manifest.package_id != "workflow.frontend"))
    path = tmp_path / "fresh-missing.sqlite"
    with pytest.raises(ContractValidationError) as missing:
        GraphWorkflowService(path)
    assert missing.value.reason_code == "package_missing_dependency"
    assert saved_selection(path) is None


def test_missing_saved_package_keeps_history_and_original_receipts_read_only_until_explicit_repair(
    tmp_path, monkeypatch,
):
    optional = CapabilityPackage(PackageManifest("example.optional-ui", "1"), lambda host:
        host.register_frontend_extension(
            "example.optional-ui.panel", "workbench-panel", "example.optional-ui.panel",
            binding={"surface": "workbench", "slot": "panel", "target": {}}, host_protocol_version=1))
    path = tmp_path / "missing-saved.sqlite"
    selection = {**DEFAULT_PACKAGES, "example.optional-ui": "1"}
    with closing(GraphWorkflowService(path, capability_packages=(optional,), enabled_packages=selection)) as service:
        document = simple_document(service)
        service.save_definition(document, expected_revision=0, idempotency_key="original-definition")
        initial = service.create_session(document["workflow_definition_id"], 1, idempotency_key="original-session")
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key="original-start")
        service.wait(started["active_chain_run_id"])
        sid, chain_id = initial["workflow_session_id"], started["active_chain_run_id"]
        historical = service.get_run(sid, chain_id)
        current = service.get_session(sid)
        public_parameters = {
            "workflow_definition_id": document["workflow_definition_id"], "definition_revision": 1,
            "node_id": uid(2), "port_id": "output",
        }
        output = service.read_public_output(sid, **public_parameters)["output"]
    persisted = saved_selection(path)
    monkeypatch.setattr(graph_nodes, "create_default_registry", no_compat_registry)
    with closing(GraphWorkflowService(path)) as missing:
        catalog = missing.platform_capabilities()
        assert catalog["package_diagnostics"][0]["enabled_packages"] == selection
        assert catalog["package_lock"] == [] and missing.registry.catalog() == []
        assert not missing.get_session(sid)["can_submit"]
        assert not missing.get_consumer(sid)["can_submit"]
        assert missing.get_run(sid, chain_id) == historical
        assert missing.read_public_output(sid, **public_parameters)["output"] == output
        assert missing.create_session(document["workflow_definition_id"], 1,
                                      idempotency_key="original-session") == initial
        assert missing.start(sid, expected_revision=initial["revision"],
                             idempotency_key="original-start") == started
        assert missing._futures == {} and missing._native_runtime is None
        for action in ("resume", "retry_archive", "retry_acceptance", "retry_failed_node", "extend_budget"):
            with pytest.raises(ContractValidationError) as denied:
                missing.control(sid, action=action, expected_revision=current["revision"],
                                idempotency_key="missing-" + action)
            assert denied.value.reason_code == "package_missing_dependency"
        with pytest.raises(ContractValidationError) as denied:
            missing.start(sid, expected_revision=current["revision"], idempotency_key="new-start")
        assert denied.value.reason_code == "package_missing_dependency"
        with pytest.raises(ContractValidationError) as denied:
            missing.create_session(document["workflow_definition_id"], 1, idempotency_key="new-session")
        assert denied.value.reason_code == "package_missing_dependency"
        assert saved_selection(path) == persisted
        assert missing.get_run(sid, chain_id) == historical
        assert missing._futures == {} and missing._native_runtime is None

        repaired = missing.configure_capability_packages(DEFAULT_PACKAGES)
        assert repaired["package_diagnostics"] == []
        assert missing.get_consumer(sid)["can_submit"]
        again = missing.start(sid, expected_revision=current["revision"], idempotency_key=str(uuid4()))
        missing.wait(again["active_chain_run_id"])
        assert missing.get_session(sid)["status"] == "succeeded"
        assert missing.get_run(sid, chain_id) == historical
        assert missing._native_runtime is None


def test_missing_saved_package_with_active_chain_requires_original_install_before_close(tmp_path, monkeypatch):
    optional = CapabilityPackage(PackageManifest("example.optional-ui", "1"), lambda host:
        host.register_frontend_extension(
            "example.optional-ui.panel", "workbench-panel", "example.optional-ui.panel",
            binding={"surface": "workbench", "slot": "panel", "target": {}}, host_protocol_version=1))
    path = tmp_path / "missing-active.sqlite"
    selection = {"workflow.compat": "1.0.0", "example.optional-ui": "1"}
    with closing(GraphWorkflowService(path, capability_packages=(optional,), enabled_packages=selection)) as service:
        sid, chain_id, release = gate_graph(service)
        try:
            current = service.get_session(sid)
            service.control(sid, action="pause", expected_revision=current["revision"], idempotency_key="pause")
        finally:
            release.set()
        service.wait(chain_id)
        assert service.get_session(sid)["status"] == "paused"
    persisted = saved_selection(path)

    with monkeypatch.context() as unavailable:
        unavailable.setattr(graph_nodes, "create_default_registry", no_compat_registry)
        with closing(GraphWorkflowService(path)) as missing:
            current = missing.get_session(sid)
            assert current["status"] == "recovery_unavailable"
            assert current["active_chain_run_id"] == chain_id
            assert not current["can_submit"] and current["available_actions"] == []
            assert missing.registry.catalog() == []
            assert missing.platform_capabilities()["package_diagnostics"][0]["enabled_packages"] == selection
            with pytest.raises(ContractValidationError) as denied:
                missing.control(sid, action="close", expected_revision=current["revision"],
                                idempotency_key="unavailable-close")
            assert denied.value.reason_code == "package_missing_dependency"
            with pytest.raises(ContractValidationError) as denied:
                missing.configure_capability_packages({"workflow.compat": "1.0.0"})
            assert denied.value.reason_code == "package_change_during_execution"
            assert missing.get_session(sid) == current
            assert saved_selection(path) == persisted
            assert missing._futures == {} and missing._native_runtime is None

    with closing(GraphWorkflowService(path, capability_packages=(optional,))) as restored:
        current = restored.get_session(sid)
        assert restored.platform_capabilities()["package_diagnostics"] == []
        assert saved_selection(path) == persisted
        assert current["status"] == "recovery_unavailable" and current["active_chain_run_id"] == chain_id
        with pytest.raises(ContractValidationError) as denied:
            restored.control(sid, action="resume", expected_revision=current["revision"],
                             idempotency_key="restored-resume")
        assert denied.value.reason_code == "recovery_unavailable"
        assert restored._futures == {} and restored._native_runtime is None
        closed = restored.control(sid, action="close", expected_revision=current["revision"],
                                  idempotency_key="restored-close")
        assert closed["status"] == "closed" and closed["active_chain_run_id"] is None
        assert restored.configure_capability_packages(DEFAULT_PACKAGES)["package_diagnostics"] == []
