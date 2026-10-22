"""Project-level content source declaration, trusted factory and adjacent domain rules.

These checks are deliberately small: they prove the declaration is statically
checkable against the real fixed sources (application contract, installed package
manifests with their real registered frontend extension identities, the
request-evidence cross-domain path and the content service module), that the
trusted local factory checks the project environment before any side effect,
passes the injected store/type-registry factories through unchanged, and that the
existing content/metadata rules still hold through that factory.
"""

import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.content_capability import (
    CAPABILITY_ID,
    CAPABILITY_KIND,
    MANAGEMENT_SERVICE_CONTRACT,
    SOURCE_ID,
    SOURCE_KIND,
    SOURCE_MAINTAINER,
    SOURCE_VERSION,
    build_management_service,
    content_capability_declaration,
    local_content_capability_source,
)
from phase1_agent.content_configuration import ContentConfigurationService
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application_contracts import COMMANDS, QUERIES
from phase1_agent.host_sdk import DataTypeDefinition, TypeRegistry
from phase1_agent.model_contract import CHAT_PROVIDER_TYPE
from phase1_agent.project_capability import (
    EXECUTION_ROLE, MANAGEMENT_ROLE, PROJECT_LIFECYCLE_OWNER,
    CapabilityDeclaration, CapabilitySource,
)
from phase1_agent.project_support import ProjectStorageEnvironment
from phase1_agent.storage import SqliteStore

from test_content_configuration import lorebook_record, prompt_record, provider_record, reference

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "phase1_agent"
_TAVERN_PANEL = "workflow.tavern.lorebook-resources-v1-2"
_PROMPT_PANEL = "workflow.prompts.workbench-panel"


def source_contains(module, anchor):
    path = _SOURCE_ROOT.joinpath(*module.split(".")[1:]).with_suffix(".py")
    return path.is_file() and anchor in path.read_text(encoding="utf-8")


def installed_package_manifests():
    """The real installed package composition: package id -> version -> manifest."""
    manifests = {}
    for package in builtin_capability_packages():
        manifest = package.manifest
        manifests.setdefault(manifest.package_id, {})[manifest.version] = manifest
    return manifests


def test_declaration_identity_roles_environment_lifecycle_and_source_policy():
    declaration = content_capability_declaration()
    assert isinstance(declaration, CapabilityDeclaration)
    rendered = declaration.to_dict()
    capability = rendered["capability"]

    assert capability["schema_version"] == 1
    assert capability["capability_id"] == CAPABILITY_ID == "content.global"
    assert capability["capability_kind"] == CAPABILITY_KIND == "content"
    assert capability["access_contract_version"] == 1
    assert capability["source"] == {
        "source_id": "content.local-source", "source_kind": "local-source",
        "source_version": "1.0.0", "maintainer": "content"}
    assert capability["source"]["source_id"] == SOURCE_ID
    assert capability["source"]["source_kind"] == SOURCE_KIND
    assert capability["source"]["source_version"] == SOURCE_VERSION
    assert capability["source"]["maintainer"] == SOURCE_MAINTAINER

    roles = rendered["roles"]
    assert [row["role"] for row in roles] == [MANAGEMENT_ROLE]
    assert EXECUTION_ROLE not in {row["role"] for row in roles}
    assert roles[0]["capability_id"] == CAPABILITY_ID
    assert roles[0]["service_contract"] == MANAGEMENT_SERVICE_CONTRACT == "content.management-service"
    assert len(declaration.roles) == 1
    assert declaration.roles[0].factory is build_management_service

    assert rendered["environment"] == {
        "schema_version": 1, "contract_id": "project.storage-environment",
        "contract_version": 1,
        "requirements": ["same_database_path", "per_operation_store"]}

    assert rendered["lifecycle"] == {
        "schema_version": 1, "owner": PROJECT_LIFECYCLE_OWNER, "close_entry": "none",
        "roles": [MANAGEMENT_ROLE], "borrowed_by": ["http-server", "workflow.graph"]}
    assert rendered["lifecycle"]["owner"] == "project.workflow-host"

    source = local_content_capability_source()
    assert isinstance(source, CapabilitySource)
    assert source.source_id == SOURCE_ID
    assert len(source.declarations) == 1
    assert source.declarations[0].to_dict() == rendered
    assert source.required_roles == (MANAGEMENT_ROLE,)
    assert source.required_dependency_contract_kinds == ()


def test_declaration_is_rebuilt_fresh_and_renders_independent_copy():
    first = content_capability_declaration()
    second = content_capability_declaration()
    assert first is not second
    assert first.roles is not second.roles
    rendered = first.to_dict()
    rendered["capability"]["capability_id"] = "tampered"
    assert second.to_dict()["capability"]["capability_id"] == CAPABILITY_ID


def test_public_operations_are_existing_application_contracts_and_management_only():
    rendered = content_capability_declaration().to_dict()
    existing = {spec.name for spec in (*COMMANDS, *QUERIES)}
    groups = rendered["public_contracts"]["operation_groups"]
    assert [group["scope"] for group in groups] == ["management"]
    declared = groups[0]["operations"]
    assert declared == ["resource.read", "resource.list", "resource.save", "resource.delete",
                        "content.metadata.read", "content.metadata.update",
                        "request-evidence.write", "request-evidence.resolve",
                        "request-evidence.read", "receipt.read"]
    assert set(declared) <= existing
    assert rendered["public_contracts"]["service_definitions"] == []
    # request-evidence.* and receipt.read are the existing frontend management
    # write/reconcile operations; resource.* are shared existing operations, not
    # content-owned ones.
    assert {"request-evidence.write", "request-evidence.resolve",
            "request-evidence.read", "receipt.read"} <= existing
    assert {"resource.read", "resource.list", "resource.save", "resource.delete"} <= existing


def test_frontend_references_match_real_registered_package_manifests():
    rendered = content_capability_declaration().to_dict()
    frontend = rendered["frontend"]
    manifests = installed_package_manifests()

    declared_packages = {(package["package_id"], package["version"])
                        for package in frontend["packages"]}
    assert declared_packages == {("workflow.prompts", "1.0.0"), ("workflow.tavern", "1.2.0")}
    declared_versions = {package["package_id"]: package["version"]
                        for package in frontend["packages"]}
    for package_id, version in declared_packages:
        # The declared version is the installed default version and the manifest
        # actually present in the fixed package composition.
        assert DEFAULT_PACKAGES[package_id] == version
        assert version in manifests[package_id]

    declared_extensions = {(row["package_id"], row["extension_id"])
                           for row in frontend["extensions"]}
    assert declared_extensions == {
        ("workflow.prompts", _PROMPT_PANEL), ("workflow.tavern", _TAVERN_PANEL)}
    for package_id, extension_id in declared_extensions:
        manifest = manifests[package_id][declared_versions[package_id]]
        exported = {row["extension_id"]
                    for row in manifest.exports.get("frontend_extensions", [])}
        assert extension_id in exported
    # The lorebook panel is registered by the current tavern package under its
    # real derived identity, not under the older base literal.
    current_tavern = manifests["workflow.tavern"]["1.2.0"]
    current_exports = {row["extension_id"]
                       for row in current_tavern.exports.get("frontend_extensions", [])}
    assert _TAVERN_PANEL == "workflow.tavern.lorebook-resources-v1-2"
    assert _TAVERN_PANEL in current_exports
    assert "workflow.tavern.lorebook-resources" not in current_exports
    assert frontend["containers"] == ["workbench"]


def test_dependency_call_sites_exist_and_contracts_are_covered_by_the_declaration():
    rendered = content_capability_declaration().to_dict()
    dependencies = rendered["dependencies"]
    assert [(row["dependency_id"], row["requirement"]) for row in dependencies] == [
        ("dep.storage-environment", "required"),
        ("dep.request-evidence", "conditional"),
        ("dep.frontend-access", "conditional")]

    request_evidence = next(row for row in dependencies
                            if row["dependency_id"] == "dep.request-evidence")
    assert request_evidence["caller"] == "content.frontend-management-write"
    assert request_evidence["provider"] == "workflow.request-evidence"
    assert request_evidence["carrier"].startswith("existing-operation")

    call_sites = {(row["dependency_id"], row["call_site"]["module"], row["call_site"]["anchor"])
                  for row in dependencies}
    assert call_sites == {
        ("dep.storage-environment", "phase1_agent.content_configuration",
         "def __init__(self, database_path"),
        ("dep.request-evidence", "phase1_agent.graph_application", "request-evidence."),
        ("dep.frontend-access", "phase1_agent.prompt_package", "PROMPT_FRONTEND_EXTENSIONS")}
    for _dependency_id, module, anchor in call_sites:
        assert source_contains(module, anchor)

    contracts = [[(contract["kind"], contract["identity"], contract.get("version"))
                  for contract in row["contracts"]] for row in dependencies]
    assert contracts == [
        [("environment", "project.storage-environment", "1")],
        [("operation", "request-evidence.write", None),
         ("operation", "request-evidence.resolve", None),
         ("operation", "request-evidence.read", None),
         ("operation", "receipt.read", None)],
        [("frontend_package", "workflow.prompts", "1.0.0"),
         ("frontend_package", "workflow.tavern", "1.2.0"),
         ("frontend_extension", "workflow.prompts.workbench-panel", None),
         ("frontend_extension", "workflow.tavern.lorebook-resources-v1-2", None),
         ("frontend_container", "workbench", None)]]
    # No fabricated credential/private-store or graph-execution dependency.
    assert {kind for row in contracts for kind, _identity, _version in row} == {
        "environment", "operation", "frontend_package", "frontend_extension", "frontend_container"}

    operations = {name for group in rendered["public_contracts"]["operation_groups"]
                  for name in group["operations"]}
    environment_contract = (rendered["environment"]["contract_id"],
                            str(rendered["environment"]["contract_version"]))
    frontend_packages = {(row["package_id"], row["version"])
                        for row in rendered["frontend"]["packages"]}
    frontend_extensions = {row["extension_id"] for row in rendered["frontend"]["extensions"]}
    frontend_containers = set(rendered["frontend"]["containers"])
    for dependency in dependencies:
        for contract in dependency["contracts"]:
            kind, identity = contract["kind"], contract["identity"]
            version = contract.get("version")
            if kind == "environment":
                assert (identity, version) == environment_contract
            elif kind == "operation":
                assert identity in operations
            elif kind == "frontend_package":
                assert (identity, version) in frontend_packages
            elif kind == "frontend_extension":
                assert identity in frontend_extensions
            elif kind == "frontend_container":
                assert identity in frontend_containers
            else:  # pragma: no cover - the exact set assertion above pins the kinds
                pytest.fail(f"unexpected dependency contract kind {kind}")


def test_factory_rejects_other_database_before_side_effects(tmp_path):
    environment = ProjectStorageEnvironment(tmp_path / "project.sqlite")
    database = tmp_path / "missing-directory" / "content.sqlite"
    with pytest.raises(ContractValidationError) as failure:
        build_management_service(database=database, environment=environment)
    assert failure.value.reason_code == "project_environment_mismatch"
    assert not database.exists()
    assert not database.parent.exists()
    assert not (tmp_path / "project.sqlite").exists()


class CountingEnvironment(ProjectStorageEnvironment):
    def __init__(self, database_path):
        super().__init__(database_path)
        self.opened = 0

    def open_store(self, *, fault_injector=None):
        self.opened += 1
        return super().open_store(fault_injector=fault_injector)


def test_factory_uses_environment_store_by_default_and_honours_an_injected_factory(tmp_path):
    environment = CountingEnvironment(tmp_path / "default.sqlite")
    service = build_management_service(database=environment.database, environment=environment)
    assert isinstance(service, ContentConfigurationService)
    assert environment.opened == 0
    assert not environment.database.exists()
    record = prompt_record()
    service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
    assert environment.opened > 0
    assert service.get_global_resource(reference(record)) == record

    other_environment = ProjectStorageEnvironment(tmp_path / "injected.sqlite")
    calls = []

    def store_factory():
        calls.append(1)
        return other_environment.open_store()

    injected = build_management_service(database=other_environment.database,
                                        environment=other_environment,
                                        store_factory=store_factory)
    assert injected._store_factory is store_factory
    assert calls == []
    injected.save_global_resource(prompt_record(), expected_sequence=0,
                                  idempotency_key=str(uuid4()))
    assert calls


def test_type_registry_factory_is_passed_through_and_resolved_at_operation_time(tmp_path):
    holder = [TypeRegistry()]
    factory_calls = []

    def type_registry_factory():
        factory_calls.append(1)
        return holder[0]

    environment = ProjectStorageEnvironment(tmp_path / "types.sqlite")
    service = build_management_service(database=environment.database, environment=environment,
                                       type_registry_factory=type_registry_factory)
    # Construction neither calls nor snapshots the registry factory.
    assert factory_calls == []
    record = {"envelope_version": 1, "scope": "workspace", "type_id": "extension.content",
              "resource_id": str(uuid4()), "data_schema_version": 1, "update_sequence": 1,
              "value": "Trusted extension"}
    with pytest.raises(ContractValidationError) as missing:
        service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
    assert missing.value.reason_code == "content_resource_type_denied"

    replacement = TypeRegistry()
    replacement.register(DataTypeDefinition("extension.content", 1, {"type": "string"},
                                            scope="global"))
    replacement.register(DataTypeDefinition(CHAT_PROVIDER_TYPE, 3, {"type": "object"},
                                            scope="global"))
    holder[0] = replacement
    service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
    assert factory_calls
    assert service.get_global_resource(reference(record)) == record
    assert service.list_global_resources() == [record]
    with pytest.raises(ContractValidationError) as denied:
        service.list_global_resources(type_id=CHAT_PROVIDER_TYPE)
    assert denied.value.reason_code == "content_resource_type_denied"
    with pytest.raises(ContractValidationError):
        service.save_global_resource(provider_record(), expected_sequence=0,
                                     idempotency_key=str(uuid4()))


def test_prompt_and_lorebook_management_through_the_factory_keeps_metadata_independent(tmp_path):
    database = tmp_path / "content.sqlite"
    environment = ProjectStorageEnvironment(database)
    service = build_management_service(database=database, environment=environment)
    prompt, lorebook = prompt_record(), lorebook_record()
    service.save_global_resource(prompt, expected_sequence=0, idempotency_key=str(uuid4()))
    service.save_global_resource(lorebook, expected_sequence=0, idempotency_key=str(uuid4()))
    assert service.list_global_resources() == sorted(
        [prompt, lorebook], key=lambda row: (row["scope"], row["type_id"], row["resource_id"]))
    assert service.get_global_resource(reference(lorebook)) == lorebook
    with pytest.raises(ContractValidationError) as denied:
        service.list_global_resources(type_id=CHAT_PROVIDER_TYPE)
    assert denied.value.reason_code == "content_resource_type_denied"
    assert not (tmp_path / "content.sqlite.credentials.sqlite").exists()

    accepted = service.update_content_metadata(reference(prompt), "Named prompt", 0, str(uuid4()))
    assert accepted == {"reference": reference(prompt), "display_name": "Named prompt",
                        "metadata_sequence": 1}
    # Display metadata is an independent transaction and never rewrites the
    # resource envelope.
    assert service.get_global_resource(reference(prompt)) == prompt


def test_display_metadata_transaction_rolls_back_independently_of_the_resource(tmp_path):
    database = tmp_path / "metadata.sqlite"
    environment = ProjectStorageEnvironment(database)
    plain = build_management_service(database=database, environment=environment)
    prompt = prompt_record()
    plain.save_global_resource(prompt, expected_sequence=0, idempotency_key=str(uuid4()))

    active = [False]

    def fault(point):
        if active[0] and point == "after_content_metadata_write":
            raise RuntimeError("Controlled metadata transaction fault")

    faulty = build_management_service(
        database=database, environment=environment,
        store_factory=lambda: SqliteStore(database, fault_injector=fault))
    active[0] = True
    with pytest.raises(RuntimeError):
        faulty.update_content_metadata(reference(prompt), "Lost name", 0, str(uuid4()))
    active[0] = False
    assert plain.read_content_metadata(reference(prompt)) == {
        "reference": reference(prompt), "display_name": "", "metadata_sequence": 0}
    assert plain.get_global_resource(reference(prompt)) == prompt
    assert plain.update_content_metadata(reference(prompt), "Kept name", 0,
                                         str(uuid4()))["metadata_sequence"] == 1


def test_importing_the_content_source_module_has_no_filesystem_side_effects(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-B", "-c", "import phase1_agent.content_capability"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert list(tmp_path.iterdir()) == []
