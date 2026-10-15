"""UI availability and execution package requirements remain independent."""

from phase1_agent.builtin_packages import builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageDependency, PackageManifest
from phase1_agent.host_sdk import DataTypeDefinition


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
