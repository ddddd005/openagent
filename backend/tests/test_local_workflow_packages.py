import json
from pathlib import Path
import sys

import pytest

from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageManifest
from phase1_agent.host_sdk import HostContractError
from phase1_agent.local_workflow_packages import LocalWorkflowPackageSource


def write_package(root, directory="example-1", *, package_id="example", version="1", entrypoint="plugin.package:create",
                  code=None, manifest_updates=None):
    manifest = {"schema_version": 1, "package_id": package_id, "version": version,
                "host_protocol_version": 1, "dependencies": [], "exports": {}}
    manifest.update(manifest_updates or {})
    path = root / directory
    path.mkdir(parents=True)
    (path / "openagent-package.json").write_text(json.dumps(
        {"schema_version": 1, "entrypoint": entrypoint, "manifest": manifest}), encoding="utf-8")
    module = path / "plugin"
    module.mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    if code is None:
        code = ("from phase1_agent.capability_packages import CapabilityPackage, PackageManifest\n"
                f"def create():\n    return CapabilityPackage(PackageManifest.from_dict({manifest!r}), lambda host: None)\n")
    (module / "package.py").write_text(code, encoding="utf-8")
    return path, manifest


def test_absent_source_is_empty_without_creating_directory(tmp_path):
    root = tmp_path / "absent"
    source = LocalWorkflowPackageSource(root)
    assert source.packages == ()
    assert source.catalog() == []
    assert source.diagnostics == ()
    assert not root.exists()


def test_discovery_and_unselected_load_never_import_package_or_change_sys_path(tmp_path):
    write_package(tmp_path, code="raise RuntimeError('must stay unselected')\n")
    before = list(sys.path)
    source = LocalWorkflowPackageSource(tmp_path)
    assert source.catalog()[0]["available"] is True
    assert CapabilityPackageLoader(source.packages).load({}).package_lock == ()
    assert sys.path == before
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader(source.packages).load({"example": "1"})
    assert caught.value.reason_code == "package_discovery_failed"
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert caught.value.package_diagnostic["package_id"] == "example"


def test_selected_packages_use_relative_imports_and_isolate_equal_module_names(tmp_path):
    sources = []
    before = list(sys.path)
    for version in ("1", "2"):
        code = ("from .value import VALUE\n"
                "from phase1_agent.capability_packages import CapabilityPackage, PackageManifest\n"
                "from phase1_agent.host_sdk import DataTypeDefinition\n"
                "def create():\n"
                f"    manifest = PackageManifest('example', {version!r}, exports={{'data_types': "
                "[{'scope': 'session', 'type_id': 'example.value', 'schema_version': 1}]})\n"
                "    def register(host):\n"
                "        host.register_data_type(DataTypeDefinition('example.value', 1, {'type':'string'}, VALUE))\n"
                "    return CapabilityPackage(manifest, register)\n")
        path, _ = write_package(tmp_path, directory="version-" + version, version=version, code=code,
                                manifest_updates={"exports": {"data_types": [
                                    {"scope": "session", "type_id": "example.value", "schema_version": 1}]}})
        (path / "plugin" / "value.py").write_text(f"VALUE = 'value-{version}'\n", encoding="utf-8")
    source = LocalWorkflowPackageSource(tmp_path)
    for version in ("1", "2"):
        loaded = CapabilityPackageLoader(source.packages).load({"example": version})
        sources.append(loaded.registry.data_types.default("example.value", 1))
    assert sources == ["value-1", "value-2"]
    assert sys.path == before


def test_duplicate_directory_identity_rejects_without_importing(tmp_path):
    write_package(tmp_path, "first", code="raise RuntimeError('must not import')\n")
    write_package(tmp_path, "second", code="raise RuntimeError('must not import')\n")
    with pytest.raises(HostContractError) as caught:
        LocalWorkflowPackageSource(tmp_path)
    assert caught.value.reason_code == "package_duplicate_install"
    assert Path(caught.value.package_diagnostic["conflicting_source"]).parent.name == "first"


def test_existing_identity_conflict_rejects_but_other_exact_version_is_allowed(tmp_path):
    write_package(tmp_path)
    source = LocalWorkflowPackageSource(tmp_path)
    source.require_no_conflicts((CapabilityPackage(PackageManifest("example", "2"), lambda host: None),))
    with pytest.raises(HostContractError) as caught:
        source.require_no_conflicts((CapabilityPackage(PackageManifest("example", "1"), lambda host: None),))
    assert caught.value.reason_code == "package_duplicate_install"
    assert caught.value.package_diagnostic["conflicting_source"] == "existing"


def test_bad_unselected_declaration_is_visible_and_selected_dependency_cannot_disappear(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "openagent-package.json").write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    write_package(tmp_path, manifest_updates={"dependencies": [{"package_id": "bad", "version": "1"}]})
    source = LocalWorkflowPackageSource(tmp_path)
    assert len(source.diagnostics) == 1
    assert source.catalog()[0]["available"] is False
    loader = CapabilityPackageLoader(source.packages)
    assert loader.load({}).package_lock == ()
    with pytest.raises(HostContractError) as caught:
        loader.load({"example": "1"})
    assert caught.value.reason_code == "package_missing_dependency"


@pytest.mark.parametrize("entrypoint", ["phase1_agent.capability_packages:CapabilityPackage", "../other:create", None])
def test_invalid_or_external_entry_cannot_use_global_module_search(tmp_path, entrypoint):
    write_package(tmp_path, entrypoint=entrypoint)
    source = LocalWorkflowPackageSource(tmp_path)
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader(source.packages).load({"example": "1"})
    assert caught.value.reason_code in ("package_invalid_entrypoint", "package_discovery_failed")
    assert caught.value.package_diagnostic["source"].endswith("openagent-package.json")


def test_implementation_manifest_must_match_declaration_before_registration(tmp_path):
    write_package(tmp_path, code=(
        "from phase1_agent.capability_packages import CapabilityPackage, PackageManifest\n"
        "def create():\n"
        "    return CapabilityPackage(PackageManifest('other', '1'), lambda host: None)\n"))
    source = LocalWorkflowPackageSource(tmp_path)
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader(source.packages).load({"example": "1"})
    assert caught.value.reason_code == "package_manifest_mismatch"


def test_missing_python_dependency_is_named_without_installing(tmp_path):
    write_package(tmp_path, code="import openagent_test_dependency_that_does_not_exist\n")
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader(LocalWorkflowPackageSource(tmp_path).packages).load({"example": "1"})
    assert caught.value.reason_code == "package_discovery_failed"
    assert caught.value.package_diagnostic["dependency"] == "openagent_test_dependency_that_does_not_exist"


def test_diagnostic_and_catalog_returns_do_not_mutate_source(tmp_path):
    write_package(tmp_path, entrypoint=None)
    source = LocalWorkflowPackageSource(tmp_path)
    source.catalog()[0]["manifest"]["package_id"] = "tampered"
    source.diagnostics[0]["message"] = "tampered"
    assert source.catalog()[0]["manifest"]["package_id"] == "example"
    assert source.diagnostics[0]["message"] != "tampered"


@pytest.mark.parametrize("failure,reason", [
    ("RuntimeError('private-business-payload')", "package_registration_failed"),
    ("HostContractError('package_export_mismatch', 'private-business-payload')", "package_export_mismatch"),
])
def test_registration_failure_exposes_safe_source_and_stage_with_original_cause(tmp_path, failure, reason):
    write_package(tmp_path, code=(
        "from phase1_agent.capability_packages import CapabilityPackage, PackageManifest\n"
        "from phase1_agent.host_sdk import HostContractError\n"
        "def create():\n"
        "    def register(host):\n"
        f"        raise {failure}\n"
        "    return CapabilityPackage(PackageManifest('example', '1'), register)\n"))
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader(LocalWorkflowPackageSource(tmp_path).packages).load({"example": "1"})
    assert caught.value.reason_code == reason
    assert caught.value.package_diagnostic["stage"] == "registration"
    assert "private-business-payload" not in str(caught.value)
    assert "private-business-payload" not in json.dumps(caught.value.package_diagnostic)
    assert "private-business-payload" in str(caught.value.__cause__)


def test_source_errors_expose_same_safe_diagnostic_to_http_without_shared_mutable_projection(tmp_path):
    write_package(tmp_path, code="raise RuntimeError('private-entry-payload')\n")
    with pytest.raises(HostContractError) as caught:
        CapabilityPackageLoader(LocalWorkflowPackageSource(tmp_path).packages).load({"example": "1"})
    error = caught.value
    public = getattr(error, "diagnostics", [])
    assert public == [error.package_diagnostic]
    assert public[0]["stage"] == "import"
    assert public[0]["package_id"] == "example"
    assert public[0]["version"] == "1"
    assert Path(public[0]["source"]).name == "openagent-package.json"
    assert "private-entry-payload" not in json.dumps(public)
    public[0]["message"] = "changed by consumer"
    assert error.package_diagnostic["message"] != "changed by consumer"
