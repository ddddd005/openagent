"""Provider project access declaration, trusted factory and source policy (147 R2).

Covers the provider side of 147 §4.1/§4.2/§5.1: the provider/model-profile
source maintains its own declaration (identity, the real management role,
required environment, lifecycle, existing public application operations, fixed
frontend references and real cross-domain dependencies); the trusted local
factory checks the explicit database path against the project environment before
any side effect; and the existing provider service still manages provider and
model-profile resources with its original per-operation store and private
credential store.  Synthetic credentials only: no real credential vault, no
network request and no model call.
"""

from inspect import Parameter, signature
from pathlib import Path
from uuid import uuid4

import pytest

import phase1_agent.builtin_packages as builtin_packages
import phase1_agent.model_package as model_package
import phase1_agent.provider_capability as provider_capability
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application_contracts import (
    COMMANDS, QUERIES, application_operation_authority,
)
from phase1_agent.graph_provider_host import ProviderConfigurationService
from phase1_agent.model_contract import CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE
from phase1_agent.model_request_contracts import default_model_runtime
from phase1_agent.project_capability import (
    DEPENDENCY_REQUIREMENTS, MANAGEMENT_ROLE, PROJECT_LIFECYCLE_OWNER,
)
from phase1_agent.project_support import ProjectStorageEnvironment, SUPPORTED_PRIVATE_STORES

from credential_test_support import TestProtector


def _rendered():
    return provider_capability.provider_capability_declaration().to_dict()


def _declared_operations(rendered):
    return [name for group in rendered["public_contracts"]["operation_groups"]
            for name in group["operations"]]


def _source_text(module):
    path = Path(provider_capability.__file__).resolve().parent.joinpath(
        *module.split(".")[1:]).with_suffix(".py")
    return path.read_text(encoding="utf-8")


def test_declaration_declares_the_provider_identity_and_the_real_management_role():
    rendered = _rendered()
    assert rendered["schema_version"] == provider_capability.DECLARATION_SCHEMA_VERSION
    assert rendered["capability"] == {
        "schema_version": provider_capability.CAPABILITY_SCHEMA_VERSION,
        "capability_id": "provider.configuration",
        "capability_kind": "provider",
        "access_contract_version": provider_capability.ACCESS_CONTRACT_VERSION,
        "source": {"source_id": "provider.local-source", "source_kind": "local-source",
                   "source_version": "1.0.0", "maintainer": "provider"},
    }
    assert rendered["roles"] == [{
        "role": "management", "capability_id": "provider.configuration",
        "service_contract": "provider.management-service",
    }]
    declaration = provider_capability.provider_capability_declaration()
    # The binding is the module-level trusted callable itself, not an import string.
    assert declaration.roles[0].factory is provider_capability.build_management_service
    assert {binding["role"] for binding in rendered["roles"]} == {MANAGEMENT_ROLE}


def test_capability_source_policy_is_management_only_and_claims_no_dependency_kinds():
    source = provider_capability.local_provider_capability_source()
    assert source.source_id == "provider.local-source"
    assert source.required_roles == (MANAGEMENT_ROLE,)
    assert source.required_dependency_contract_kinds == ()
    assert len(source.declarations) == 1
    rendered = source.declarations[0].to_dict()
    assert rendered["capability"]["source"]["source_id"] == source.source_id
    # Each call builds a fresh declaration: mutating one cannot leak into the next.
    source.declarations[0].capability["capability_id"] = "mutated"
    assert provider_capability.provider_capability_declaration().to_dict() == rendered


def test_environment_and_lifecycle_declare_the_per_operation_service_without_close():
    rendered = _rendered()
    assert rendered["environment"] == {
        "schema_version": 1, "contract_id": "project.storage-environment",
        "contract_version": 1,
        "requirements": ["same_database_path", "per_operation_store"],
    }
    assert rendered["lifecycle"] == {
        "schema_version": 1, "owner": PROJECT_LIFECYCLE_OWNER, "close_entry": "none",
        "roles": [MANAGEMENT_ROLE], "borrowed_by": ["http-server", "workflow.graph"],
    }
    # Truthful lifecycle: the domain service has no close entry and closes each
    # per-operation store inside the operation instead.
    assert not hasattr(ProviderConfigurationService, "close")
    assert "with self._lock, closing(self._store_factory()) as store:" \
        in _source_text("phase1_agent.graph_provider_host")


def test_public_contracts_reference_existing_management_operations():
    rendered = _rendered()
    assert rendered["public_contracts"]["service_definitions"] == []
    assert [group["scope"] for group in rendered["public_contracts"]["operation_groups"]] \
        == ["management"]
    declared = _declared_operations(rendered)
    existing = {spec.name for spec in COMMANDS + QUERIES}
    assert set(declared) <= existing
    assert {"provider.models.list", "provider.credential.save",
            "credential.status", "credential.delete"} <= set(declared)
    # Provider/model-profile frontend management writes and resource verification
    # use these existing shared operations; the declaration references them but
    # does not own the evidence service or a receipt store.
    assert {"request-evidence.write", "request-evidence.resolve",
            "request-evidence.read", "receipt.read"} <= set(declared)
    assert all(application_operation_authority(name) == "management" for name in declared)
    # resource.* are the existing shared generic operations: they are referenced by
    # their explicit names (no wildcard/prefix ownership claim), and the provider
    # service itself only accepts the provider and model-profile resource types.
    assert {name for name in declared if name.startswith("resource.")} == {
        "resource.read", "resource.list", "resource.save", "resource.delete"}
    assert all("*" not in name for name in declared)
    provider_text = _source_text("phase1_agent.graph_provider_host")
    assert "CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE" in provider_text
    assert "provider_resource_type_denied" in provider_text


def test_frontend_references_match_the_fixed_package_manifest_pairing():
    frontend = _rendered()["frontend"]
    assert frontend["containers"] == ["workbench"]
    packages = {(row["package_id"], row["version"]) for row in frontend["packages"]}
    assert packages == {("workflow.models", "1.0.0")}
    for package_id, version in packages:
        assert builtin_packages.DEFAULT_PACKAGES[package_id] == version
    declared_extensions = {(row["package_id"], row["extension_id"])
                           for row in frontend["extensions"]}
    expected_extensions = {(model_package.MODEL_PACKAGE_ID, row["extension_id"])
                           for row in model_package.MODEL_FRONTEND_EXTENSIONS}
    assert declared_extensions == expected_extensions
    package_text = _source_text("phase1_agent.model_package")
    for _, extension_id in declared_extensions:
        assert f'"extension_id": "{extension_id}"' in package_text
    for row in model_package.MODEL_FRONTEND_EXTENSIONS:
        assert row["binding"]["surface"] == "workbench"
    # The declaration must pair with the real installed package manifest: the
    # exact workflow.models version and exactly the extension ids it exports.
    manifest = model_package.create_model_package().manifest
    assert (manifest.package_id, manifest.version) in packages
    exported_extensions = {row["extension_id"]
                           for row in manifest.exports["frontend_extensions"]}
    assert {extension_id for _, extension_id in declared_extensions} == exported_extensions
    assert {package_id for package_id, _ in declared_extensions} == {manifest.package_id}


def test_cross_domain_dependencies_point_at_real_call_sites_and_stay_covered():
    rendered = _rendered()
    dependencies = rendered["dependencies"]
    assert [row["dependency_id"] for row in dependencies] == [
        "dep.storage-environment", "dep.private-credential-store",
        "dep.request-evidence", "dep.frontend-access"]
    declared_operations = set(_declared_operations(rendered))
    packages = {(row["package_id"], row["version"])
                for row in rendered["frontend"]["packages"]}
    extensions = {row["extension_id"] for row in rendered["frontend"]["extensions"]}
    containers = set(rendered["frontend"]["containers"])
    environment = rendered["environment"]
    contract_kinds = set()
    for row in dependencies:
        assert row["requirement"] in DEPENDENCY_REQUIREMENTS
        call_site = row["call_site"]
        assert call_site["anchor"] in _source_text(call_site["module"])
        for contract in row["contracts"]:
            kind, identity = contract["kind"], contract["identity"]
            version = contract.get("version")
            contract_kinds.add(kind)
            if kind == "environment":
                assert (identity, version) == (
                    environment["contract_id"], str(environment["contract_version"]))
            elif kind == "private_store":
                assert identity in SUPPORTED_PRIVATE_STORES
            elif kind == "operation":
                assert identity in declared_operations
            elif kind == "frontend_package":
                assert (identity, version) in packages
            elif kind == "frontend_extension":
                assert identity in extensions
            elif kind == "frontend_container":
                assert identity in containers
            else:
                pytest.fail(f"unsupported dependency contract kind {kind}")
    assert {"environment", "operation", "private_store", "frontend_extension"} \
        <= contract_kinds
    private_store = next(row for row in dependencies
                         if row["dependency_id"] == "dep.private-credential-store")
    assert ".credentials.sqlite" in private_store["carrier"]
    assert "neither copied nor merged" in private_store["carrier"]
    # The existing service really keeps credentials in the private store next to
    # the main database; this declaration does not move or merge them.
    assert 'str(self.database) + ".credentials.sqlite"' \
        in _source_text("phase1_agent.graph_provider_host")
    request_evidence = next(row for row in dependencies
                            if row["dependency_id"] == "dep.request-evidence")
    assert request_evidence["caller"] == "provider.frontend-management-write"
    assert request_evidence["provider"] == "workflow.request-evidence"
    assert request_evidence["requirement"] == "conditional"
    assert request_evidence["call_site"] == {
        "module": "phase1_agent.graph_application", "anchor": "request-evidence."}
    assert "request-evidence." in _source_text("phase1_agent.graph_application")
    assert all(contract["kind"] == "operation"
               for contract in request_evidence["contracts"])
    assert {contract["identity"] for contract in request_evidence["contracts"]} == {
        "request-evidence.write", "request-evidence.resolve",
        "request-evidence.read", "receipt.read"}
    # The evidence service stays the existing operation owner: original requests
    # are never copied into this declaration or the provider main database.
    assert "existing-operation" in request_evidence["carrier"]
    assert "never copied" in request_evidence["carrier"]


def test_factory_signature_is_the_fixed_trusted_local_binding():
    parameters = signature(provider_capability.build_management_service).parameters
    assert list(parameters) == ["database", "environment", "store_factory",
                                "credential_protector", "discovery_client_factory"]
    assert all(parameters[name].kind is Parameter.KEYWORD_ONLY for name in parameters)
    assert parameters["database"].default is Parameter.empty
    assert parameters["environment"].default is Parameter.empty
    for optional in ("store_factory", "credential_protector", "discovery_client_factory"):
        assert parameters[optional].default is None


def test_factory_rejects_a_mismatched_database_before_any_side_effect(tmp_path):
    environment = ProjectStorageEnvironment(tmp_path / "project" / "main.sqlite")
    wrong = tmp_path / "other" / "main.sqlite"
    opened = []

    def forbidden_store():
        opened.append(True)
        raise AssertionError("store opened before the environment check")

    with pytest.raises(ContractValidationError) as rejected:
        provider_capability.build_management_service(
            database=wrong, environment=environment, store_factory=forbidden_store)
    assert rejected.value.reason_code == "project_environment_mismatch"
    assert opened == []
    assert not wrong.exists() and not wrong.parent.exists()
    assert not Path(str(wrong) + ".credentials.sqlite").exists()
    assert not (tmp_path / "project").exists()


def test_factory_uses_the_project_environment_and_manages_both_resource_kinds(tmp_path):
    database = tmp_path / "provider-management.sqlite"
    environment = ProjectStorageEnvironment(database)
    calls = []

    def store_factory():
        calls.append(True)
        return environment.open_store()

    manager = provider_capability.build_management_service(
        database=database, environment=environment,
        store_factory=store_factory, credential_protector=TestProtector())
    assert isinstance(manager, ProviderConfigurationService)
    assert manager.database == database.resolve()
    assert calls  # the service construction already opened its per-operation store
    before = len(calls)
    manager.list_global_resources(scope="workspace", type_id=MODEL_PROFILE_TYPE)
    assert len(calls) == before + 1

    provider_identity = {"envelope_version": 1, "scope": "workspace",
                         "type_id": CHAT_PROVIDER_TYPE, "resource_id": str(uuid4())}
    reference = "local:" + str(uuid4())
    secret = "dummy-provider-source-only-key"
    accepted = manager.save_provider_credential(
        identity=provider_identity, expected_sequence=0, credential_ref=reference,
        expected_generation=0, secret=secret, idempotency_key=str(uuid4()),
        provider_seed={"name": "Provider source", "protocol": "chat",
                       "base_url": "https://offline.invalid/v1", "enabled": True})
    assert accepted["outcome"] == "saved"
    assert manager.get_global_resource(provider_identity)["value"]["credential_ref"] == reference

    profile_identity = {"envelope_version": 1, "scope": "workspace",
                        "type_id": MODEL_PROFILE_TYPE, "resource_id": str(uuid4())}
    profile_record = {
        **profile_identity, "data_schema_version": 2, "update_sequence": 1,
        "value": {"name": "Provider source profile", "provider": provider_identity,
                  "model": {"id": "deepseek-chat", "capacity": {
                      "context_window_tokens": 400_000, "input_limit_tokens": 400_000,
                      "max_output_tokens": 64_000}},
                  "runtime": default_model_runtime(), "enabled": True},
    }
    manager.save_global_resource(profile_record, expected_sequence=0,
                                 idempotency_key=str(uuid4()))
    assert manager.get_global_resource(profile_identity) == profile_record
    assert manager.list_global_resources(scope="workspace", type_id=MODEL_PROFILE_TYPE) \
        == [profile_record]

    credentials_path = Path(str(database.resolve()) + ".credentials.sqlite")
    assert manager.credentials.path == credentials_path
    assert credentials_path.exists()
    # Existing protection is unchanged: no secret reaches either database in plaintext.
    assert secret.encode() not in database.read_bytes()
    assert secret.encode() not in credentials_path.read_bytes()


def test_module_import_and_declaration_build_have_no_side_effects(tmp_path, monkeypatch):
    module_text = Path(provider_capability.__file__).read_text(encoding="utf-8")
    # The domain service is imported lazily inside the factory, never at module import.
    assert "\n    from .graph_provider_host import ProviderConfigurationService" \
        in module_text

    database = tmp_path / "absent" / "main.sqlite"
    environment = ProjectStorageEnvironment(database)
    assert not database.parent.exists()

    def forbidden(*args, **kwargs):
        raise AssertionError("declaration construction built the provider service")

    monkeypatch.setattr("phase1_agent.graph_provider_host.ProviderConfigurationService",
                        forbidden)
    source = provider_capability.local_provider_capability_source()
    source.declarations[0].to_dict()
    assert list(tmp_path.iterdir()) == []
    assert not database.exists() and not database.parent.exists()
    assert not Path(str(database) + ".credentials.sqlite").exists()
