"""Provider and model-profile project access declaration and trusted local binding.

This module is the provider domain's trusted local source of the project-level
access declaration added by 147 R2 (147 号 §4.1/§4.2/§5.1).  It owns the
declaration content: identity, the single real acquisition role (management),
the required project storage environment, the lifecycle facts of a service that
holds no long-lived connection, the existing public application operations, the
fixed frontend package/extension references and the real cross-domain
dependencies.

The provider domain reuses the existing ``ProviderConfigurationService`` for the
existing provider and model-profile global resources, its private credential
store and its discovery path.  No second model-profile service is created, the
credential store is neither copied nor merged into another database, and no
execution role or graph runtime is invented here.  Provider/model-profile
records stay in the project's shared SQLite main library while credentials stay
in the existing private store next to it.

The domain has no long-lived database connection and no domain ``close()``:
every management operation opens and closes its own store, so ``close_entry`` is
declared as ``none`` and the Host/Graph borrowers are declared truthfully.

Bindings are explicit trusted local callables (module-level factories), never
string entrypoints or dynamic discovery: this module is the only place that
knows the provider domain's original service construction path, and the project
only accepts, validates, indexes and queries the declaration.  Building the
declaration stays free of side effects: it constructs no service, opens no
database, creates no directory, protects no credential and sends no request.
"""

from __future__ import annotations

from copy import deepcopy

from .project_capability import (
    ACCESS_CONTRACT_VERSION,
    CAPABILITY_SCHEMA_VERSION,
    DECLARATION_SCHEMA_VERSION,
    MANAGEMENT_ROLE,
    PROJECT_LIFECYCLE_OWNER,
    CapabilityContractReference,
    CapabilityDependency,
    CapabilityDeclaration,
    CapabilitySource,
    RoleBinding,
    require_matching_database,
)

CAPABILITY_ID = "provider.configuration"
CAPABILITY_KIND = "provider"

SOURCE_ID = "provider.local-source"
SOURCE_KIND = "local-source"
SOURCE_VERSION = "1.0.0"
SOURCE_MAINTAINER = "provider"

MANAGEMENT_SERVICE_CONTRACT = "provider.management-service"


def build_management_service(*, database, environment, store_factory=None,
                             credential_protector=None, discovery_client_factory=None):
    """Trusted local binding: the provider domain's original management path.

    The explicit database path is checked against the project storage
    environment before the service is constructed, so a wrong path fails before
    any directory, database or private credential store side effect.
    ``store_factory`` defaults to the environment's per-operation store opener;
    a caller with its own store view (for example the Graph execution view
    carrying its store and fault injection) keeps it.  ``credential_protector``
    and ``discovery_client_factory`` are passed through unchanged; the service
    keeps its own original construction protocol, including the private
    credential store at ``<main database>.credentials.sqlite``.
    """
    require_matching_database(database, environment)
    from .graph_provider_host import ProviderConfigurationService

    return ProviderConfigurationService(
        database,
        credential_protector=credential_protector,
        discovery_client_factory=discovery_client_factory,
        store_factory=store_factory or environment.open_store,
    )


def _role_bindings() -> tuple[RoleBinding, ...]:
    # Module-level lookup (not an import string): the provider source owns this
    # callable and the project may only acquire the service through it.
    return (
        RoleBinding(MANAGEMENT_ROLE, CAPABILITY_ID, MANAGEMENT_SERVICE_CONTRACT,
                    build_management_service),
    )


CAPABILITY = {
    "schema_version": CAPABILITY_SCHEMA_VERSION,
    "capability_id": CAPABILITY_ID,
    "capability_kind": CAPABILITY_KIND,
    "access_contract_version": ACCESS_CONTRACT_VERSION,
    "source": {"source_id": SOURCE_ID, "source_kind": SOURCE_KIND,
               "source_version": SOURCE_VERSION, "maintainer": SOURCE_MAINTAINER},
}

ENVIRONMENT = {
    "schema_version": 1,
    "contract_id": "project.storage-environment",
    "contract_version": 1,
    "requirements": ("same_database_path", "per_operation_store"),
}

LIFECYCLE = {
    "schema_version": 1,
    "owner": PROJECT_LIFECYCLE_OWNER,
    # The provider domain has no domain close: every management operation opens
    # and closes its own store, and the private credential store is used per
    # operation.  HTTP keeps borrowing the Host management view and the workflow
    # Graph keeps its own provider view for execution consumers.
    "close_entry": "none",
    "roles": (MANAGEMENT_ROLE,),
    "borrowed_by": ("http-server", "workflow.graph"),
}

PUBLIC_CONTRACTS = {
    "schema_version": 1,
    "operation_groups": (
        {"scope": "management", "operations": (
            # Provider and private-credential management operations the existing
            # service answers.
            "provider.models.list", "provider.credential.save",
            "provider.credential.receipt", "provider.credential.bind-receipt",
            "credential.protection", "credential.status", "credential.receipt",
            "credential.delete",
            # Provider/model-profile frontend management writes use the existing
            # request-evidence operations (original request persistence, unknown
            # read-only reconciliation and evidence resolution) and read existing
            # application receipts.  They are shared existing operations: this
            # declaration references them and does not own the evidence service.
            "request-evidence.write", "request-evidence.resolve",
            "request-evidence.read", "receipt.read",
            # The generic global-resource operations are shared with the content
            # domain: this declaration references the existing operations, it
            # does not claim the whole resource.* family as provider-owned.
            "resource.read", "resource.list", "resource.save", "resource.delete")},
    ),
    # The provider management service is not a public ServiceDefinition; the
    # workflow.models service definition belongs to the model consumer package.
    "service_definitions": (),
}

FRONTEND = {
    "schema_version": 1,
    "packages": (
        {"package_id": "workflow.models", "version": "1.0.0"},
    ),
    "extensions": (
        {"package_id": "workflow.models",
         "extension_id": "workflow.models.workbench-panel"},
        {"package_id": "workflow.models",
         "extension_id": "workflow.models.node-fields-v7"},
    ),
    "containers": ("workbench",),
}

DEPENDENCIES = (
    CapabilityDependency(
        dependency_id="dep.storage-environment",
        caller="provider.management",
        provider="project.storage-environment",
        contracts=(CapabilityContractReference("environment", "project.storage-environment", "1"),),
        purpose="Provider and model-profile management reads and writes use the project's "
                "shared SQLite main library environment, with an independent store opened "
                "and closed per provider operation.",
        condition="Required on the normal project Host acquisition path and on the workflow "
                  "Graph provider view.",
        module="phase1_agent.graph_provider_host",
        anchor="self._store_factory = store_factory",
        requirement="required",
        carrier="project-environment",
    ),
    CapabilityDependency(
        dependency_id="dep.private-credential-store",
        caller="provider.management",
        provider="workflow.credentials",
        contracts=(CapabilityContractReference("private_store",
                                               "project.private-credential-store"),),
        purpose="Provider Key saves, reads, rotations and deletions use the existing private "
                "credential store next to the main database; provider and model-profile "
                "records stay in the main database.",
        condition="Required when provider credentials are managed or model discovery reads a "
                  "saved local Key; ordinary provider/model-profile management without a Key "
                  "still uses the same store path.",
        module="phase1_agent.graph_provider_host",
        anchor="PrivateCredentialStore",
        requirement="required",
        carrier="existing-private-store (<main database>.credentials.sqlite); credentials are "
                 "neither copied nor merged",
    ),
    CapabilityDependency(
        dependency_id="dep.request-evidence",
        caller="provider.frontend-management-write",
        provider="workflow.request-evidence",
        contracts=(CapabilityContractReference("operation", "request-evidence.write"),
                   CapabilityContractReference("operation", "request-evidence.resolve"),
                   CapabilityContractReference("operation", "request-evidence.read"),
                   CapabilityContractReference("operation", "receipt.read")),
        purpose="Provider/model-profile frontend management writes persist their original "
                "request, reconcile unknown results read-only and resolve evidence through "
                "the existing request-evidence operations; resource verification reads the "
                "existing application receipt.",
        condition="Only on the frontend management write outbox path; backend commands are "
                  "not forced through request evidence.",
        module="phase1_agent.graph_application",
        anchor="request-evidence.",
        requirement="conditional",
        carrier="existing-operation (request evidence service is not a project capability); "
                "original requests stay with the existing evidence owner and are never copied "
                "into this declaration, a project store or the provider main database",
    ),
    CapabilityDependency(
        dependency_id="dep.frontend-access",
        caller="provider.frontend",
        provider="workflow.frontend-packages",
        contracts=(
            CapabilityContractReference("frontend_package", "workflow.models", "1.0.0"),
            CapabilityContractReference("frontend_extension",
                                        "workflow.models.workbench-panel"),
            CapabilityContractReference("frontend_extension",
                                        "workflow.models.node-fields-v7"),
            CapabilityContractReference("frontend_container", "workbench"),
        ),
        purpose="Page availability: the provider/model-profile declaration reaches the "
                "trusted local provider workbench panel and model field editor.",
        condition="Only when the workflow.models package is enabled in the package lock with "
                  "matching local frontend declarations.",
        module="phase1_agent.model_package",
        anchor="MODEL_FRONTEND_EXTENSIONS",
        requirement="conditional",
        carrier="existing-frontend-package (frontend relation only, not a project sub-capability)",
    ),
)


def provider_capability_declaration() -> CapabilityDeclaration:
    """Build a fresh declaration and validate its rendered form."""
    declaration = CapabilityDeclaration(
        capability=deepcopy(CAPABILITY),
        roles=_role_bindings(),
        environment=deepcopy(ENVIRONMENT),
        lifecycle=deepcopy(LIFECYCLE),
        public_contracts=deepcopy(PUBLIC_CONTRACTS),
        frontend=deepcopy(FRONTEND),
        dependencies=tuple(DEPENDENCIES),
    )
    declaration.to_dict()
    return declaration


def local_provider_capability_source() -> CapabilitySource:
    """The provider domain's trusted local source of its project access declaration."""
    return CapabilitySource(SOURCE_ID, (provider_capability_declaration(),),
                            required_roles=(MANAGEMENT_ROLE,),
                            required_dependency_contract_kinds=())
