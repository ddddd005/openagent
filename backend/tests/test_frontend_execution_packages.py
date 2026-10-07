"""UI availability and execution package requirements remain independent."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageDependency, PackageManifest
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition

from test_graph_service import create
from test_plan13a_frontend_public import frontend_graph
from graph_test_plugin import run_current_graph as run


def package(identity, *, dependencies=(), ui=False, business=False):
    def register(host):
        if ui:
            host.register_frontend_extension(identity + ".panel", "workbench-panel", identity + ".panel",
                binding={"surface": "workbench", "slot": "panel", "target": {}}, host_protocol_version=1)
        if business:
            host.register_data_type(DataTypeDefinition(identity + ".state", 1, {"type": "integer"}, 0))

    return CapabilityPackage(PackageManifest(identity, "1", dependencies=tuple(
        PackageDependency(dependency, "1") for dependency in dependencies)), register)


def ids(lock):
    return {row["package_id"] for row in lock}


def test_complete_frontend_keeps_project_lock_but_excludes_ui_from_execution_lock_and_detach():
    loaded = CapabilityPackageLoader(builtin_capability_packages()).load({"workflow.frontend": "1.0.0"})
    assert ids(loaded.package_lock) == {"workflow.frontend", "workflow.frontend-business", "workflow.content"}
    assert ids(loaded.registry.execution_package_lock) == {"workflow.frontend-business", "workflow.content"}
    assert loaded.to_dict()["execution_package_lock"] == list(loaded.registry.execution_package_lock)
    detached = loaded.registry.detached(frozen=True)
    assert detached.execution_package_lock == loaded.registry.execution_package_lock
    detached.execution_package_lock[0]["version"] = "mutated-copy"
    assert loaded.registry.execution_package_lock[0]["version"] == "1.0.0"


def test_execution_locks_follow_business_dependency_chain_and_retain_empty_composition_packages():
    installed = CapabilityPackageLoader((
        package("ui", ui=True, dependencies=("wrapper",)),
        package("wrapper", dependencies=("middle",)),
        package("middle", dependencies=("business",)),
        package("business", business=True),
        package("empty"),
    ))
    loaded = installed.load({"ui": "1", "empty": "1"})
    assert ids(loaded.registry.execution_package_lock) == {"wrapper", "middle", "business", "empty"}
    assert ids(loaded.package_lock) == {"ui", "wrapper", "middle", "business", "empty"}


def test_business_declared_dependency_on_ui_is_still_an_exact_execution_requirement():
    loaded = CapabilityPackageLoader((
        package("business", business=True, dependencies=("ui",)), package("ui", ui=True),
    )).load({"business": "1"})
    assert ids(loaded.registry.execution_package_lock) == {"business", "ui"}


def test_mixed_exports_remain_execution_requirements():
    loaded = CapabilityPackageLoader((package("mixed", ui=True, business=True),)).load({"mixed": "1"})
    assert loaded.registry.execution_package_lock == ({"package_id": "mixed", "version": "1"},)


def test_disabling_pure_ui_keeps_business_can_submit_and_runs_again_with_original_execution_lock(tmp_path):
    database = tmp_path / "ui-execution.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        doc, _, _, _, _ = frontend_graph(service)
        doc["package_lock"] = deepcopy(list(service.registry.execution_package_lock))
        completed = run(service, create(service, doc), inputs={"text": "before disable"})
        sid = completed["workflow_session_id"]
        before = completed["objects"]["frontend"]["revision"]
        disabled = {identity: version for identity, version in DEFAULT_PACKAGES.items()
                    if identity != "workflow.frontend"}
        catalog = service.configure_capability_packages(disabled)
        assert "workflow.frontend" not in ids(catalog["execution_package_lock"])
        assert service.get_consumer(sid)["can_submit"]
        completed = run(service, service.get_session(sid), inputs={"text": "after disable"})
        assert completed["status"] == "succeeded"
        assert completed["objects"]["frontend"]["revision"] == before + 1
        assert len(completed["objects"]["frontend"]["value"]["entries"]) == 2
        manifests = service.get_run(sid, completed["selected_chain_run_id"])["state_manifests"]
        for name in ("start", "end"):
            assert "workflow.frontend" not in ids(manifests[name]["package_lock"])
            assert manifests[name]["package_lock"] == doc["package_lock"]
    with closing(GraphWorkflowService(database)) as service:
        assert service.get_consumer(sid)["can_submit"]
        assert service.get_session(sid)["objects"]["frontend"]["revision"] == before + 1


def test_explicit_old_ui_lock_is_not_silently_rewritten_when_ui_is_disabled(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "old-lock.sqlite")) as service:
        doc, _, _, _, _ = frontend_graph(service)
        doc["package_lock"] = deepcopy(list(service.registry.package_lock))
        completed = run(service, create(service, doc), inputs={"text": "old explicit lock"})
        service.configure_capability_packages({key: value for key, value in DEFAULT_PACKAGES.items()
                                               if key != "workflow.frontend"})
        assert not service.get_consumer(completed["workflow_session_id"])["can_submit"]
        with pytest.raises(ContractValidationError) as failure:
            run(service, service.get_session(completed["workflow_session_id"]), inputs={"text": "blocked"})
        assert failure.value.reason_code == "graph_missing_package"


def test_disabling_mixed_package_refuses_execution_without_deleting_business_state(tmp_path):
    mixed = package("mixed", ui=True, business=True)
    with closing(GraphWorkflowService(tmp_path / "mixed-lock.sqlite", capability_packages=(mixed,),
                                     enabled_packages={**DEFAULT_PACKAGES, "mixed": "1"})) as service:
        doc, _, _, _, _ = frontend_graph(service)
        doc["package_lock"] = deepcopy(list(service.registry.execution_package_lock))
        completed = run(service, create(service, doc), inputs={"text": "mixed required"})
        state = deepcopy(completed["objects"])
        service.configure_capability_packages(DEFAULT_PACKAGES)
        assert not service.get_consumer(completed["workflow_session_id"])["can_submit"]
        assert service.get_session(completed["workflow_session_id"])["objects"] == state
        with pytest.raises(ContractValidationError) as failure:
            run(service, service.get_session(completed["workflow_session_id"]), inputs={"text": "blocked"})
        assert failure.value.reason_code == "graph_missing_package"
