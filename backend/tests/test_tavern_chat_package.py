"""Additive chat package selection without changing frozen earlier contracts."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import GraphCompiler
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.tavern.chat_contracts import EMPTY_TAVERN_CHAT_STATE, TAVERN_CHAT_STATE_TYPE
from phase1_agent.tavern.chat_nodes import TAVERN_CHAT_COMPONENTS
from phase1_agent.tavern.package import CHAT_TAVERN_FRONTEND_EXTENSIONS, GLOBAL_TAVERN_FRONTEND_EXTENSIONS


def loader():
    return CapabilityPackageLoader(builtin_capability_packages())


def test_chat_package_registers_only_new_chat_contracts_and_exact_workbench_declarations():
    installed = loader()
    old = installed.load({"workflow.tavern": "1.0.0"})
    global_version = installed.load({"workflow.tavern": "1.1.0"})
    chat = installed.load({"workflow.tavern": "1.2.0"})
    for component in TAVERN_CHAT_COMPONENTS:
        assert old.registry.get(component, "1") is None
        assert global_version.registry.get(component, "1") is None
        assert chat.registry.get(component, "1") is not None
    for component in ("lorebook.item", "lorebook.group"):
        assert old.registry.get(component, "1").definition.to_dict() == (
            chat.registry.get(component, "1").definition.to_dict())
    for component in ("lorebook.global-reference", "lorebook.global-activate"):
        assert global_version.registry.get(component, "1").definition.to_dict() == (
            chat.registry.get(component, "1").definition.to_dict())
    for source in (global_version, chat):
        assert source.registry.get("frontend.state.output", "1") is None
        assert source.registry.data_types.get("workflow.frontend-state", 1, scope="session") is None
    assert chat.registry.data_types.default(TAVERN_CHAT_STATE_TYPE, 1) == EMPTY_TAVERN_CHAT_STATE
    assert len(CHAT_TAVERN_FRONTEND_EXTENSIONS) == len(GLOBAL_TAVERN_FRONTEND_EXTENSIONS) + 1
    declarations = [row for row in chat.frontend_extensions if row["package_id"] == "workflow.tavern"]
    assert declarations == sorted([{
        "schema_version": 2, "host_protocol_version": 1,
        "component_id": None, "component_version": None, **deepcopy(row),
        "package_id": "workflow.tavern", "package_version": "1.2.0",
    } for row in CHAT_TAVERN_FRONTEND_EXTENSIONS], key=lambda row: row["extension_id"])
    assert not any(row["package_id"] == "workflow.frontend-business" for row in chat.package_lock)


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0"])
def test_saved_selection_keeps_old_chat_capability_absence_after_reopen(tmp_path, version):
    database = tmp_path / "selection.sqlite"
    selection = {**DEFAULT_PACKAGES, "workflow.tavern": version}
    with closing(GraphWorkflowService(database, enabled_packages=selection)) as service:
        catalog = service.registry.catalog()
        assert service.registry.get("tavern.chat.append", "1") is None
    with closing(GraphWorkflowService(database)) as reopened:
        assert reopened.registry.catalog() == catalog
        assert {"package_id": "workflow.tavern", "version": version} in reopened.registry.package_lock
        assert reopened.registry.get("tavern.chat.append", "1") is None
        assert not hasattr(reopened, "_native_runtime")


def test_default_chat_package_keeps_generic_frontend_available_and_no_runtime_at_open(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "fresh.sqlite")) as service:
        assert {"package_id": "workflow.tavern", "version": "1.2.0"} in service.registry.package_lock
        assert service.registry.get("tavern.chat.presentation", "1")
        assert service.registry.get("frontend.presentation", "1")
        assert not hasattr(service, "_native_runtime")


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0"])
def test_old_lock_cannot_compile_new_chat_node(version):
    installed = loader()
    modern = installed.load({"workflow.tavern": "1.2.0"})
    old = installed.load({"workflow.tavern": version})
    definition = modern.registry.get("tavern.chat.presentation", "1").definition
    document = {
        "schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Explicit tavern version", "object_bindings": [], "edges": [],
        "package_lock": list(old.registry.package_lock),
        "nodes": [{
            "node_binding_id": str(uuid4()), "component_id": definition.component_id,
            "component_version": "1", "title": "Tavern presentation",
            "position": {"x": 0, "y": 0}, "config": {},
        }],
    }
    with pytest.raises(ContractValidationError):
        GraphCompiler(old.registry).compile(document)
