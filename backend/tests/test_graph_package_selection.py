"""Exact current project selections without live model calls."""

import json
from contextlib import closing
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from phase1_agent import builtin_packages
from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackage, PackageManifest
from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore


def uid(number):
    return str(UUID(int=number, version=4))


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


def configuration_document(service):
    value = simple_document(service)
    value.update(schema_version=3, edit_sequence=0, control_edges=[])
    for item in value["nodes"]:
        item["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    return value


@pytest.mark.parametrize("selection", [None, {}, {"workflow.tools": "1.0.0"}, {"workflow.prompts": "1.0.0"}],
                         ids=["default", "empty", "tools-only", "prompts-only"])
def test_fresh_and_reopened_service_respect_exact_current_selection(tmp_path, selection):
    path = tmp_path / "selection.sqlite"
    with closing(GraphWorkflowService(path, enabled_packages=selection)) as service:
        lock = {row["package_id"]: row["version"] for row in service.registry.package_lock}
        assert lock == selected_lock(selection)
        assert "workflow.compat" not in lock and service.registry.get("workflow.agent", "2") is None
        assert service.platform_capabilities()["package_diagnostics"] == []
        assert not hasattr(service, "_native_runtime")
        catalog = service.registry.catalog()
        if selection == {}:
            assert catalog == [] and saved_selection(path) == "{}"
    persisted = saved_selection(path)
    with closing(GraphWorkflowService(path)) as reopened:
        assert {row["package_id"]: row["version"] for row in reopened.registry.package_lock} == lock
        assert reopened.registry.catalog() == catalog
        assert reopened.platform_capabilities()["package_diagnostics"] == []
        assert not hasattr(reopened, "_native_runtime")
    assert saved_selection(path) == persisted


def test_tool_prompt_selection_stays_opt_in_until_explicit_configuration(tmp_path):
    path = tmp_path / "tool-prompts.sqlite"
    selection = {key: value for key, value in DEFAULT_PACKAGES.items()
                 if key != "workflow.tool-prompts"}
    enabled = {**selection, "workflow.tool-prompts": "1.0.0"}
    with closing(GraphWorkflowService(path, enabled_packages=selection)) as service:
        lock = {row["package_id"]: row["version"] for row in service.registry.package_lock}
        assert lock == selected_lock(selection)
        assert service.registry.get("tool-bindings.variable-tools", "2") is None
        assert service.registry.get("context.assembly", "6") is None
    persisted = saved_selection(path)
    assert json.loads(persisted) == selection

    with closing(GraphWorkflowService(path)) as reopened:
        lock = {row["package_id"]: row["version"] for row in reopened.registry.package_lock}
        assert lock == selected_lock(selection)
        assert reopened.registry.get("tool-bindings.variable-tools", "2") is None
        assert reopened.registry.get("context.assembly", "6") is None
        assert saved_selection(path) == persisted

        configured = reopened.configure_capability_packages(enabled)
        assert configured["package_diagnostics"] == []
        assert reopened.registry.get("tool-bindings.variable-tools", "2") is not None
        assert reopened.registry.get("context.assembly", "6") is not None
        assert json.loads(saved_selection(path)) == enabled


@pytest.mark.parametrize("selection", [None, {}, {"workflow.tools": "1.0.0"}, {"workflow.prompts": "1.0.0"}],
                         ids=["default", "empty", "tools-only", "prompts-only"])
def test_public_registry_helper_respects_exact_current_selection(selection):
    loaded = create_package_registry(enabled=selection)
    assert {row["package_id"]: row["version"] for row in loaded.package_lock} == selected_lock(selection)
    assert loaded.registry.get("workflow.text", "1") is None
    assert loaded.registry.get("workflow.agent", "2") is None


@pytest.mark.parametrize("selection", [{}, {"workflow.tools": "1.0.0"}], ids=["saved-empty", "saved-tools"])
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


def test_explicit_missing_selection_is_rejected_without_replacing_saved_configuration(tmp_path):
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
    original = builtin_capability_packages
    monkeypatch.setattr(builtin_packages, "builtin_capability_packages", lambda **kwargs: tuple(
        package for package in original(**kwargs) if package.manifest.package_id != "workflow.frontend"))
    path = tmp_path / "fresh-missing.sqlite"
    with pytest.raises(ContractValidationError) as missing:
        GraphWorkflowService(path)
    assert missing.value.reason_code == "package_missing_dependency"
    assert saved_selection(path) is None


def test_missing_saved_package_keeps_history_and_original_receipts_read_only_until_explicit_repair(tmp_path):
    optional = CapabilityPackage(PackageManifest("example.optional-ui", "1"), lambda host:
        host.register_frontend_extension(
            "example.optional-ui.panel", "workbench-panel", "example.optional-ui.panel",
            binding={"surface": "workbench", "slot": "panel", "target": {}}, host_protocol_version=1))
    path = tmp_path / "missing-saved.sqlite"
    selection = {**DEFAULT_PACKAGES, "example.optional-ui": "1"}
    with closing(GraphWorkflowService(path, capability_packages=(optional,), enabled_packages=selection)) as service:
        document = configuration_document(service)
        definition_request = {
            "workflow_definition_id": document["workflow_definition_id"], "document": document,
            "expected_edit_sequence": 0, "idempotency_key": "original-definition-update",
        }
        application = GraphApplication(service)
        definition_origin = application.command("definition.update", definition_request)
        saved = definition_origin["result"]
        assert saved["outcome"] == "saved"
        verified = read_graph_application_receipt(path, "definition.update", definition_request)
        assert verified["outcome"] == "matched"
        assert verified["receipt"] == definition_origin["receipt"]
        assert verified["result"] == saved
        initial = service.create_session(
            document["workflow_definition_id"], saved["definition_revision"], idempotency_key="original-session")
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
    with closing(GraphWorkflowService(path)) as missing:
        catalog = missing.platform_capabilities()
        assert catalog["package_diagnostics"][0]["enabled_packages"] == selection
        assert catalog["package_lock"] == [] and missing.registry.catalog() == []
        missing_receipt = GraphApplication(missing).query("receipt.read", {
            "operation": "definition.update", "parameters": definition_request,
        })
        assert missing_receipt["outcome"] == "matched"
        assert missing_receipt["receipt"] == definition_origin["receipt"]
        assert missing_receipt["result"] == definition_origin["result"]
        assert not missing.get_session(sid)["can_submit"]
        assert not missing.get_consumer(sid)["can_submit"]
        assert missing.get_run(sid, chain_id) == historical
        with pytest.raises(ContractValidationError) as unavailable:
            missing.read_public_output(sid, **public_parameters)
        assert unavailable.value.reason_code == "graph_missing_node_type"
        assert missing.create_session(document["workflow_definition_id"], 1,
                                      idempotency_key="original-session") == initial
        assert missing.start(sid, expected_revision=initial["revision"],
                             idempotency_key="original-start") == started
        assert missing._futures == {} and not hasattr(missing, "_native_runtime")
        for action in ("resume", "retry_archive", "retry_acceptance", "retry_failed_node"):
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
        assert missing._futures == {} and not hasattr(missing, "_native_runtime")

        repaired = missing.configure_capability_packages(DEFAULT_PACKAGES)
        assert repaired["package_diagnostics"] == []
        assert missing.get_consumer(sid)["can_submit"]
        again = missing.start(sid, expected_revision=current["revision"], idempotency_key=str(uuid4()))
        missing.wait(again["active_chain_run_id"])
        assert missing.get_session(sid)["status"] == "succeeded"
        assert missing.get_run(sid, chain_id) == historical
        assert not hasattr(missing, "_native_runtime")
