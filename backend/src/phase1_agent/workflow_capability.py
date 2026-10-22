"""Workflow-owned minimal project access declaration and its trusted local bindings.

This module is the workflow source of the project-level access declaration
(147 号 §4.1).  It owns the declaration content: capability identity, access
declaration/contract version, source reference, the per-role acquisition
bindings, the required environment, lifecycle, public business contract
references, frontend access references and the cross-domain dependency list.
The common data types and the common format validation live in the neutral
``project_capability`` module; this module only supplies the workflow's own
content and source policy (both acquisition roles and the five dependency
coverage kinds the workflow declares).

Bindings are explicit trusted local callables (module-level factories), never
string entrypoints or dynamic discovery: this module is the only place that
knows the workflow's original service construction path.  The project
(``project_support``/``workflow_host``) only accepts, validates, indexes and
queries this declaration; it never enumerates internal construction, imports
modules from strings or executes arbitrary module imports.

The declaration deliberately references existing public operations,
``ServiceDefinition`` identities and frontend package/extension ids, and names
the provider/model-preset and global-content domains as project sub-capabilities
now wired through their own trusted source bindings instead of claiming those
supplies are unregistered contract references.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .project_capability import (
    ACCESS_CONTRACT_VERSION,
    ACCESS_ROLES,
    CAPABILITY_SCHEMA_VERSION,
    CONTRACT_KINDS,
    DECLARATION_SCHEMA_VERSION,
    DEPENDENCY_REQUIREMENTS,
    EXECUTION_ROLE,
    MANAGEMENT_ROLE,
    PROJECT_LIFECYCLE_OWNER,
    CapabilityContractReference,
    CapabilityDeclaration,
    CapabilityDependency,
    CapabilitySource,
    RoleBinding,
)

CAPABILITY_ID = "workflow.graph"
CAPABILITY_KIND = "workflow"

SOURCE_ID = "workflow.local-source"
SOURCE_KIND = "local-source"
SOURCE_VERSION = "1.0.0"
SOURCE_MAINTAINER = "workflow"

# The workflow source's own dependency-coverage lower bound: the existing
# cross-domain dependency list must keep covering these five contract kinds.
REQUIRED_DEPENDENCY_CONTRACT_KINDS = ("environment", "operation", "service_definition",
                                      "private_store", "frontend_extension")

__all__ = [
    "ACCESS_CONTRACT_VERSION",
    "ACCESS_ROLES",
    "CAPABILITY_ID",
    "CAPABILITY_KIND",
    "CAPABILITY_SCHEMA_VERSION",
    "CONTRACT_KINDS",
    "CapabilityContractReference",
    "CapabilityDependency",
    "DECLARATION_SCHEMA_VERSION",
    "DEPENDENCY_REQUIREMENTS",
    "EXECUTION_ROLE",
    "MANAGEMENT_ROLE",
    "PROJECT_LIFECYCLE_OWNER",
    "REQUIRED_DEPENDENCY_CONTRACT_KINDS",
    "RoleBinding",
    "SOURCE_ID",
    "SOURCE_KIND",
    "SOURCE_MAINTAINER",
    "SOURCE_VERSION",
    "WorkflowCapabilityDeclaration",
    "WorkflowCapabilitySource",
    "build_execution_service",
    "build_management_service",
    "local_workflow_capability_source",
    "workflow_capability_declaration",
]


@dataclass(frozen=True)
class WorkflowCapabilityDeclaration(CapabilityDeclaration):
    """The workflow source's minimal project access declaration."""


@dataclass(frozen=True)
class WorkflowCapabilitySource(CapabilitySource):
    """The workflow source with the workflow's own source policy.

    Tests and trusted composition keep constructing the source as
    ``WorkflowCapabilitySource(SOURCE_ID, (declaration,))``; the workflow policy
    (both acquisition roles and the five dependency coverage kinds) is applied
    by this subclass rather than by the project or the caller.
    """

    required_roles: tuple = ACCESS_ROLES
    required_dependency_contract_kinds: tuple = REQUIRED_DEPENDENCY_CONTRACT_KINDS


def build_management_service(*, database, environment, provider_configuration_factory=None,
                             content_configuration_factory=None, workflow_package_source=None):
    """Trusted local binding: the workflow's original management construction path.

    The provider and content factories are narrow dependency hand-offs only:
    they carry the already-validated project bindings of the two domain
    capabilities, never the whole host or project repository.
    """
    from .graph_service import GraphManagementService
    return GraphManagementService(
        database, environment=environment,
        provider_configuration_factory=provider_configuration_factory,
        content_configuration_factory=content_configuration_factory,
        workflow_package_source=workflow_package_source)


def build_execution_service(*, database, environment, provider_configuration_factory=None,
                            content_configuration_factory=None, workflow_package_source=None):
    """Trusted local binding: the workflow's original execution construction path."""
    from .graph_service import GraphWorkflowService
    return GraphWorkflowService(
        database, environment=environment,
        provider_configuration_factory=provider_configuration_factory,
        content_configuration_factory=content_configuration_factory,
        workflow_package_source=workflow_package_source)


def _role_bindings() -> tuple[RoleBinding, ...]:
    # Module-level lookup (not an import string): the source owns these callables
    # and tests may wrap them to prove the acquisition really goes through here.
    return (
        RoleBinding(MANAGEMENT_ROLE, CAPABILITY_ID, "workflow.management-service",
                    build_management_service),
        RoleBinding(EXECUTION_ROLE, CAPABILITY_ID, "workflow.execution-service",
                    build_execution_service),
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
    "close_entry": "close",
    "roles": ACCESS_ROLES,
    "borrowed_by": ("http-server",),
}

PUBLIC_CONTRACTS = {
    "schema_version": 1,
    "operation_groups": (
        {"scope": "management", "operations": (
            "platform", "packages.configure",
            "definition.list", "definition.summaries", "definition.read",
            "definition.sessions", "session.read", "session.summaries",
            "definition.update", "definition.copy", "session.metadata.update",
            "resource.read", "resource.list", "content.metadata.read",
            "provider.models.list", "provider.credential.save",
            "credential.status", "credential.delete",
            "request-evidence.write", "request-evidence.resolve", "request-evidence.read",
            "receipt.read")},
        {"scope": "execution", "operations": (
            "run.start", "event.submit", "run.control", "checkpoint.fork", "object.write")},
        {"scope": "lifecycle", "operations": (
            "definition.archive", "definition.delete", "session.archive", "session.delete")},
    ),
    "service_definitions": (
        {"service_id": "workflow.models", "exact_version": "2.0.0"},
    ),
}

FRONTEND = {
    "schema_version": 1,
    "packages": (
        {"package_id": "workflow.frontend", "version": "1.0.0"},
        {"package_id": "workflow.frontend-business", "version": "1.0.0"},
    ),
    "extensions": (
        {"package_id": "workflow.frontend",
         "extension_id": "workflow.frontend.node-entry-fields-v2"},
        {"package_id": "workflow.frontend",
         "extension_id": "workflow.frontend.chat-session-object"},
    ),
    "containers": ("workbench",),
}

DEPENDENCIES = (
    CapabilityDependency(
        dependency_id="dep.storage-environment",
        caller="workflow.management+execution",
        provider="project.storage-environment",
        contracts=(CapabilityContractReference("environment", "project.storage-environment", "1"),),
        purpose="Workflow domain reads and writes use the project's shared SQLite main "
                "library environment and open independent per-operation stores.",
        condition="Required on the normal project Host acquisition path.",
        module="phase1_agent.graph_service",
        anchor="def _store(self)",
        requirement="required",
        carrier="project-environment",
    ),
    CapabilityDependency(
        dependency_id="dep.local-package-source",
        caller="workflow.management+execution",
        provider="project.workflow-package-source",
        contracts=(CapabilityContractReference("operation", "platform"),
                   CapabilityContractReference("operation", "packages.configure")),
        purpose="The project supplies a declaration-only local directory source to the "
                "workflow factory. Workflow owns exact selection, loading and consumption; "
                "platform exposes discovery and diagnostics and packages.configure changes "
                "the existing persisted selection.",
        condition="Required on normal project Host acquisition; a missing directory is an "
                  "empty source, and direct trusted service construction may omit it.",
        module="phase1_agent.workflow_host",
        anchor="workflow_package_source=self._package_source()",
        requirement="required",
        carrier="project-supplied LocalWorkflowPackageSource; operation references describe "
                "its existing public discovery and selection contract",
    ),
    CapabilityDependency(
        dependency_id="dep.resource-content",
        caller="workflow.runtime",
        provider="content.global",
        contracts=(CapabilityContractReference("operation", "resource.read"),
                   CapabilityContractReference("operation", "resource.list"),
                   CapabilityContractReference("operation", "content.metadata.read")),
        purpose="Declared node resource dependencies are frozen before a run: the preflight "
                "reads records.read_global_resources in the original records transaction to "
                "verify resource identities, references and the current type basis, without "
                "cross-service reads.",
        condition="Only when a node declares resources:read or references global resources; "
                  "the resource type must be registered by an enabled package.",
        module="phase1_agent.graph_resource_host",
        anchor="def _preflight_capabilities",
        requirement="conditional",
        carrier="domain direct read in the original records transaction "
                "(records.read_global_resources; not a cross-service content-service call)",
    ),
    CapabilityDependency(
        dependency_id="dep.content-use-time-read",
        caller="workflow.runtime",
        provider="content.global",
        contracts=(CapabilityContractReference("operation", "resource.read"),),
        purpose="After the declared identity is verified, node use-time reads fetch current "
                "content through the original _store/_global_store direct domain read with "
                "the retained RunBasis registry, not through the wired content service.",
        condition="Only when a node with resources:read or a routed service reads current "
                  "global content during execution.",
        module="phase1_agent.graph_resource_host",
        anchor="def _read_current_resource",
        requirement="conditional",
        carrier="in-domain direct read (original _store/_global_store with the retained "
                "RunBasis registry; not the wired content service)",
    ),
    CapabilityDependency(
        dependency_id="dep.content-host-service-read",
        caller="workflow.runtime",
        provider="content.global",
        contracts=(CapabilityContractReference("operation", "resource.read"),),
        purpose="The Host service environment reads content-type resources through the wired "
                "content configuration service and passes this invocation's registry.data_types "
                "explicitly; other relations keep the original Host read.",
        condition="Only when a routed service reads a content-type global resource in a frozen run.",
        module="phase1_agent.graph_runtime_host",
        anchor="self.content_configuration.get_global_resource(",
        requirement="conditional",
        carrier="project capability content.global (wired ContentConfigurationService with "
                "this run's registry.data_types)",
    ),
    CapabilityDependency(
        dependency_id="dep.provider-model",
        caller="workflow.management+runtime",
        provider="provider.configuration",
        contracts=(CapabilityContractReference("operation", "provider.models.list"),),
        purpose="Model nodes resolve and bind provider and model configuration through the "
                "project's wired provider capability; ordinary management and discovery do "
                "not cover the restricted request bundle read.",
        condition="Only when provider/model nodes participate in a run or a user manages "
                  "provider configuration. The restricted request bundle read is declared "
                  "separately.",
        module="phase1_agent.graph_provider_host",
        anchor="class ProviderConfigurationService",
        requirement="conditional",
        carrier="project capability provider.configuration (provider source management "
                "factory → ProviderConfigurationService over the project environment)",
    ),
    CapabilityDependency(
        dependency_id="dep.model-request-resource-reader",
        caller="workflow.runtime",
        provider="workflow.models",
        contracts=(CapabilityContractReference("service_definition", "workflow.models", "2.0.0"),),
        purpose="Model request preparation reads authorized workspace profile and provider "
                "via ModelRequestResourceReader: one read-only transaction authorizes the "
                "accepted source binding and reads both records; check_resource_bundle "
                "checks head without reloading values.",
        condition="Only when a model request prepares an authorized source bundle; "
                  "graph_runtime_host injects the reader as its model_resource_reader option. "
                  "check_resource_bundle re-verifies the authorized update_sequence/deleted head.",
        module="phase1_agent.model_request_resources",
        anchor="def read_resource_bundle",
        requirement="conditional",
        carrier="restricted ModelRequestResourceReader port (not ordinary management CRUD, "
                "not discovery, not two ordinary reads)",
    ),
    CapabilityDependency(
        dependency_id="dep.model-request-service",
        caller="workflow.runtime",
        provider="workflow.models",
        contracts=(CapabilityContractReference("service_definition", "workflow.models", "2.0.0"),),
        purpose="Model invocation, projections and fact reads use the existing public "
                "model request service definition.",
        condition="Only for model/agent nodes that declare and participate in a run.",
        module="phase1_agent.graph_runtime_host",
        anchor="MODEL_REQUEST_SERVICE_REF",
        requirement="conditional",
        carrier="existing-service-definition (model supply is not a project capability)",
    ),
    CapabilityDependency(
        dependency_id="dep.credentials",
        caller="workflow.runtime",
        provider="workflow.credentials",
        contracts=(CapabilityContractReference("operation", "credential.status"),
                   CapabilityContractReference("operation", "credential.delete"),
                   CapabilityContractReference("operation", "provider.credential.save"),
                   CapabilityContractReference("private_store", "project.private-credential-store")),
        purpose="Model request preparation reads local credentials and provider saving "
                "writes them through the existing private credential store.",
        condition="Only for model request preparation or provider credential management; "
                  "ordinary management does not need it.",
        module="phase1_agent.credential_store",
        anchor="class PrivateCredentialStore",
        requirement="conditional",
        carrier="existing-operation and private credential store",
    ),
    CapabilityDependency(
        dependency_id="dep.request-evidence",
        caller="workflow.frontend-management-write",
        provider="workflow.request-evidence",
        contracts=(CapabilityContractReference("operation", "request-evidence.write"),
                   CapabilityContractReference("operation", "request-evidence.resolve"),
                   CapabilityContractReference("operation", "request-evidence.read"),
                   CapabilityContractReference("operation", "receipt.read")),
        purpose="Protected management writes persist the original request, verify unknown "
                "results read-only and resolve evidence through the existing operations.",
        condition="Only on the frontend management write outbox path; backend commands "
                  "are not forced through request evidence.",
        module="phase1_agent.graph_application",
        anchor="request-evidence.",
        requirement="conditional",
        carrier="existing-operation (request evidence service is not a project capability)",
    ),
    CapabilityDependency(
        dependency_id="dep.frontend-access",
        caller="workflow.frontend",
        provider="workflow.frontend-packages",
        contracts=(CapabilityContractReference("frontend_package", "workflow.frontend", "1.0.0"),
                   CapabilityContractReference("frontend_extension",
                                               "workflow.frontend.node-entry-fields-v2"),
                   CapabilityContractReference("frontend_extension",
                                               "workflow.frontend.chat-session-object"),
                   CapabilityContractReference("frontend_container", "workbench")),
        purpose="Page availability: the backend declarations reach the trusted local "
                "frontend implementations and their workbench containers.",
        condition="Only when the referenced packages are enabled and present in the "
                  "package lock with matching local frontend declarations.",
        module="phase1_agent.frontend_package",
        anchor="FRONTEND_EXTENSIONS",
        requirement="conditional",
        carrier="existing-frontend-package (frontend relation only, not a project sub-capability)",
    ),
)


def workflow_capability_declaration() -> WorkflowCapabilityDeclaration:
    """Build a fresh declaration; trust bindings are resolved at call time."""
    declaration = WorkflowCapabilityDeclaration(
        capability=copy.deepcopy(CAPABILITY),
        roles=_role_bindings(),
        environment=copy.deepcopy(ENVIRONMENT),
        lifecycle=copy.deepcopy(LIFECYCLE),
        public_contracts=copy.deepcopy(PUBLIC_CONTRACTS),
        frontend=copy.deepcopy(FRONTEND),
        dependencies=tuple(DEPENDENCIES),
    )
    declaration.to_dict()
    return declaration


def local_workflow_capability_source() -> WorkflowCapabilitySource:
    """The workflow's trusted local source of its project access declaration."""
    return WorkflowCapabilitySource(SOURCE_ID, (workflow_capability_declaration(),))
