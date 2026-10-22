"""Global content's project-level access declaration and its trusted local binding.

This module is the content source of the project-level access declaration added
by 147 R3 (147 号 §4.1/§4.2/§5.2).  It owns the declaration content: the
capability identity, access declaration/contract versions, source reference, its
single existing management role binding, the required project environment, the
lifecycle facts, the public business contract references, the frontend access
references and the real cross-domain dependency list.

The content domain owns global prompt and lorebook resource CRUD plus prompt
display metadata over the project's shared SQLite main library.  It has no
execution role and no graph runtime: declarations only reference the existing
``resource.*``/``content.metadata.*`` public operations, the existing
request-evidence write/read/resolve and receipt-read operations used by the
frontend management-write and reconcile path, the trusted frontend packages that
already carry the prompt and lorebook panels and the existing per-operation
storage environment.  ``resource.*`` operations are shared with the provider
domain and are not claimed as content-owned.

The domain holds no long-lived database connection and has no domain ``close()``:
every management operation opens and closes its own store, so ``close_entry`` is
declared as ``none`` and the Host/Graph borrowers are declared truthfully.

Frontend extension declarations use the real registered identities of the
installed package composition: the prompt panel is exported by
``create_prompt_package`` (``workflow.prompts`` 1.0.0) and the lorebook panel by
``create_current_tavern_package`` (``workflow.tavern`` 1.2.0), whose registered
identity carries that package's ``-v1-2`` derivation
(``workflow.tavern.lorebook-resources-v1-2``).  Package, version and extension
identities therefore match the actual manifests instead of a base literal.

Bindings are explicit trusted local callables (module-level factories), never
string entrypoints or dynamic discovery: this module is the only place that
knows the content domain's original service construction path, and the project
only accepts, validates, indexes and queries the declaration.
"""

from __future__ import annotations

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

CAPABILITY_ID = "content.global"
CAPABILITY_KIND = "content"

SOURCE_ID = "content.local-source"
SOURCE_KIND = "local-source"
SOURCE_VERSION = "1.0.0"
SOURCE_MAINTAINER = "content"

MANAGEMENT_SERVICE_CONTRACT = "content.management-service"


def build_management_service(*, database, environment, store_factory=None,
                             type_registry_factory=None):
    """Trusted local binding: the content domain's original management path.

    The explicit database path is checked against the project storage
    environment before the service is constructed (so a wrong path fails before
    any directory, file or store side effect).  ``store_factory`` defaults to the
    environment's per-operation store opener; a caller with its own store view
    (for example the Graph execution view carrying its store and fault
    injection) keeps it.  ``type_registry_factory`` is passed through unchanged
    and is never called or snapshotted here: the Graph view supplies
    ``lambda: registry.data_types`` so trusted global types are resolved at
    operation time, not at construction time.
    """
    require_matching_database(database, environment)
    from .content_configuration import ContentConfigurationService

    return ContentConfigurationService(
        database,
        store_factory=store_factory or environment.open_store,
        type_registry_factory=type_registry_factory,
    )


def _role_bindings() -> tuple[RoleBinding, ...]:
    # Module-level lookup (not an import string): the content source owns this
    # callable and tests may wrap it to prove acquisition really goes through
    # the declared binding.
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
    # The content domain has no domain close: every operation closes its own
    # per-operation store.  HTTP keeps borrowing the Host management view and
    # the workflow Graph execution view keeps its own content view.
    "close_entry": "none",
    "roles": (MANAGEMENT_ROLE,),
    "borrowed_by": ("http-server", "workflow.graph"),
}

PUBLIC_CONTRACTS = {
    "schema_version": 1,
    "operation_groups": (
        {"scope": "management", "operations": (
            "resource.read", "resource.list", "resource.save", "resource.delete",
            "content.metadata.read", "content.metadata.update",
            "request-evidence.write", "request-evidence.resolve",
            "request-evidence.read", "receipt.read")},
    ),
    "service_definitions": (),
}

FRONTEND = {
    "schema_version": 1,
    "packages": (
        {"package_id": "workflow.prompts", "version": "1.0.0"},
        {"package_id": "workflow.tavern", "version": "1.2.0"},
    ),
    "extensions": (
        {"package_id": "workflow.prompts",
         "extension_id": "workflow.prompts.workbench-panel"},
        {"package_id": "workflow.tavern",
         "extension_id": "workflow.tavern.lorebook-resources-v1-2"},
    ),
    "containers": ("workbench",),
}

DEPENDENCIES = (
    CapabilityDependency(
        dependency_id="dep.storage-environment",
        caller="content.management",
        provider="project.storage-environment",
        contracts=(CapabilityContractReference("environment", "project.storage-environment", "1"),),
        purpose="Global prompt/lorebook resources and their display metadata are read and "
                "written through the project's shared SQLite main library environment, with "
                "an independent store opened and closed per content operation.",
        condition="Required on the normal project Host acquisition path and on the workflow "
                  "Graph content view.",
        module="phase1_agent.content_configuration",
        anchor="def __init__(self, database_path",
        requirement="required",
        carrier="project-environment",
    ),
    CapabilityDependency(
        dependency_id="dep.request-evidence",
        caller="content.frontend-management-write",
        provider="workflow.request-evidence",
        contracts=(
            CapabilityContractReference("operation", "request-evidence.write"),
            CapabilityContractReference("operation", "request-evidence.resolve"),
            CapabilityContractReference("operation", "request-evidence.read"),
            CapabilityContractReference("operation", "receipt.read"),
        ),
        purpose="Protected content management writes persist the original request, verify "
                "unknown results read-only, resolve evidence and read resource receipts "
                "through the existing request-evidence and receipt operations.",
        condition="Only on the frontend content management write outbox and resource "
                  "receipt verification path; backend commands are not forced through "
                  "request evidence.",
        module="phase1_agent.graph_application",
        anchor="request-evidence.",
        requirement="conditional",
        carrier="existing-operation (request evidence service is not a project capability)",
    ),
    CapabilityDependency(
        dependency_id="dep.frontend-access",
        caller="content.frontend",
        provider="content.frontend-packages",
        contracts=(
            CapabilityContractReference("frontend_package", "workflow.prompts", "1.0.0"),
            CapabilityContractReference("frontend_package", "workflow.tavern", "1.2.0"),
            CapabilityContractReference("frontend_extension", "workflow.prompts.workbench-panel"),
            CapabilityContractReference("frontend_extension",
                                        "workflow.tavern.lorebook-resources-v1-2"),
            CapabilityContractReference("frontend_container", "workbench"),
        ),
        purpose="Page availability: the backend declaration reaches the trusted local prompt "
                "and lorebook workbench panels and their workbench container.",
        condition="Only when the referenced packages are enabled and present in the package "
                  "lock with matching local frontend declarations.",
        module="phase1_agent.prompt_package",
        anchor="PROMPT_FRONTEND_EXTENSIONS",
        requirement="conditional",
        carrier="existing-frontend-package (frontend relation only, not a project sub-capability)",
    ),
)


def content_capability_declaration() -> CapabilityDeclaration:
    """Build a fresh declaration and validate its rendered form."""
    declaration = CapabilityDeclaration(
        capability=dict(CAPABILITY, source=dict(CAPABILITY["source"])),
        roles=_role_bindings(),
        environment=dict(ENVIRONMENT),
        lifecycle=dict(LIFECYCLE),
        public_contracts={"schema_version": PUBLIC_CONTRACTS["schema_version"],
                          "operation_groups": tuple(
                              {"scope": group["scope"], "operations": tuple(group["operations"])}
                              for group in PUBLIC_CONTRACTS["operation_groups"]),
                          "service_definitions": tuple(PUBLIC_CONTRACTS["service_definitions"])},
        frontend={"schema_version": FRONTEND["schema_version"],
                  "packages": tuple(dict(package) for package in FRONTEND["packages"]),
                  "extensions": tuple(dict(extension) for extension in FRONTEND["extensions"]),
                  "containers": tuple(FRONTEND["containers"])},
        dependencies=tuple(DEPENDENCIES),
    )
    declaration.to_dict()
    return declaration


def local_content_capability_source() -> CapabilitySource:
    """The content domain's trusted local source of its project access declaration."""
    return CapabilitySource(SOURCE_ID, (content_capability_declaration(),),
                            required_roles=(MANAGEMENT_ROLE,),
                            required_dependency_contract_kinds=())
