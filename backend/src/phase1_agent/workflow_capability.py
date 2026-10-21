"""Workflow-owned minimal project access declaration and its trusted local bindings.

This module is the workflow source of the project-level access declaration for
T2 (140 号 §6.1).  It owns the declaration content: capability identity, access
declaration/contract version, source reference, the per-role acquisition
bindings, the required environment, lifecycle, public business contract
references, frontend access references and the cross-domain dependency list.

Bindings are explicit trusted local callables (module-level factories), never
string entrypoints or dynamic discovery: this module is the only place that
knows the workflow's original service construction path.  The project
(``project_support``/``workflow_host``) only accepts, validates, indexes and
queries this declaration; it never enumerates internal construction, imports
modules from strings or executes arbitrary module imports.

The declaration deliberately references existing public operations,
``ServiceDefinition`` identities and frontend package/extension ids instead of
copying workflow node/type catalogs or claiming provider/content/model supply as
already registered project sub-capabilities.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Callable

from .contract_errors import require

CAPABILITY_SCHEMA_VERSION = 1
DECLARATION_SCHEMA_VERSION = 1
ACCESS_CONTRACT_VERSION = 1

CAPABILITY_ID = "workflow.graph"
CAPABILITY_KIND = "workflow"

MANAGEMENT_ROLE = "management"
EXECUTION_ROLE = "execution"
ACCESS_ROLES = (MANAGEMENT_ROLE, EXECUTION_ROLE)

CONTRACT_KINDS = frozenset({
    "environment", "operation", "service_definition", "private_store",
    "frontend_package", "frontend_extension", "frontend_container",
})
DEPENDENCY_REQUIREMENTS = frozenset({"required", "conditional", "optional"})

SOURCE_ID = "workflow.local-source"
SOURCE_KIND = "local-source"
SOURCE_VERSION = "1.0.0"
SOURCE_MAINTAINER = "workflow"

_MAX_TEXT = 256
_MAX_ITEMS = 64
_MODULE_PATTERN = re.compile(r"phase1_agent\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")


def _text(value: Any, code: str, message: str) -> str:
    require(type(value) is str and 0 < len(value) <= _MAX_TEXT, code, message)
    return value


def _items(value: Any, code: str, message: str, *, maximum: int = _MAX_ITEMS) -> tuple:
    require(type(value) in (tuple, list) and 0 < len(value) <= maximum, code, message)
    return tuple(value)


@dataclass(frozen=True)
class CapabilityContractReference:
    """One existing public contract the workflow depends on or exposes."""

    kind: str
    identity: str
    version: str | None = None

    def to_dict(self) -> dict:
        require(self.kind in CONTRACT_KINDS, "capability_dependency_invalid",
                "Dependency contract kind is unsupported")
        value = {"kind": self.kind,
                 "identity": _text(self.identity, "capability_dependency_invalid",
                                   "Dependency contract identity is invalid")}
        if self.version is not None:
            value["version"] = _text(self.version, "capability_dependency_invalid",
                                     "Dependency contract version is invalid")
        return value


@dataclass(frozen=True)
class CapabilityDependency:
    """One cross-domain dependency entry with its real call location."""

    dependency_id: str
    caller: str
    provider: str
    contracts: tuple[CapabilityContractReference, ...]
    purpose: str
    condition: str
    module: str
    anchor: str
    requirement: str
    carrier: str

    def to_dict(self) -> dict:
        dependency_id = _text(self.dependency_id, "capability_dependency_invalid",
                              "Dependency identity is invalid")
        caller = _text(self.caller, "capability_dependency_invalid", "Dependency caller is invalid")
        provider = _text(self.provider, "capability_dependency_invalid", "Dependency provider is invalid")
        purpose = _text(self.purpose, "capability_dependency_invalid", "Dependency purpose is invalid")
        condition = _text(self.condition, "capability_dependency_invalid",
                          "Dependency condition is invalid")
        carrier = _text(self.carrier, "capability_dependency_invalid", "Dependency carrier is invalid")
        require(self.requirement in DEPENDENCY_REQUIREMENTS, "capability_dependency_invalid",
                "Dependency requirement is unsupported")
        require(type(self.module) is str and _MODULE_PATTERN.fullmatch(self.module) is not None,
                "capability_dependency_invalid", "Dependency call-site module is invalid")
        anchor = _text(self.anchor, "capability_dependency_invalid", "Dependency call-site anchor is invalid")
        contracts = _items(self.contracts, "capability_dependency_invalid",
                           "Dependency contracts must be a bounded array")
        require(all(isinstance(item, CapabilityContractReference) for item in contracts),
                "capability_dependency_invalid", "Dependency contract references are invalid")
        rendered = [item.to_dict() for item in contracts]
        require(len({(item["kind"], item["identity"]) for item in rendered}) == len(rendered),
                "capability_dependency_invalid", "Dependency contract references are duplicated")
        return {"dependency_id": dependency_id, "caller": caller, "provider": provider,
                "contracts": rendered, "purpose": purpose, "condition": condition,
                "call_site": {"module": self.module, "anchor": anchor},
                "requirement": self.requirement, "carrier": carrier}


@dataclass(frozen=True)
class RoleBinding:
    """One trusted local acquisition binding for a project role."""

    role: str
    capability_id: str
    service_contract: str
    factory: Callable

    def to_dict(self) -> dict:
        require(self.role in ACCESS_ROLES, "capability_role_invalid", "Access role is unsupported")
        capability_id = _text(self.capability_id, "capability_binding_invalid",
                              "Access binding capability identity is invalid")
        service_contract = _text(self.service_contract, "capability_binding_invalid",
                                 "Access binding service contract is invalid")
        require(callable(self.factory), "capability_binding_not_callable",
                "Access binding must provide a trusted local callable")
        return {"role": self.role, "capability_id": capability_id,
                "service_contract": service_contract}


def _validate_capability(value: Any) -> dict:
    code = "capability_identity_invalid"
    require(type(value) is dict and set(value) == {
        "schema_version", "capability_id", "capability_kind",
        "access_contract_version", "source"}, code,
        "Capability identity fields differ")
    require(type(value["schema_version"]) is int and value["schema_version"] == CAPABILITY_SCHEMA_VERSION,
            code, "Capability identity schema is unsupported")
    _text(value["capability_id"], code, "Capability identity is invalid")
    require(value["capability_kind"] == CAPABILITY_KIND, code, "Capability kind is unsupported")
    require(type(value["access_contract_version"]) is int and value["access_contract_version"] > 0,
            code, "Access contract version is invalid")
    source = value["source"]
    require(type(source) is dict and set(source) == {
        "source_id", "source_kind", "source_version", "maintainer"}, code,
        "Capability source reference fields differ")
    for field_name in ("source_id", "source_kind", "source_version", "maintainer"):
        _text(source[field_name], code, "Capability source reference is invalid")
    return copy.deepcopy(value)


def _validate_roles(value: Any) -> list[dict]:
    code = "capability_role_invalid"
    roles = _items(value, code, "Access roles must be a bounded array")
    require(len(roles) <= len(ACCESS_ROLES), code, "Access role count exceeds its limit")
    rendered, seen = [], set()
    for binding in roles:
        require(isinstance(binding, RoleBinding), code, "Access role binding is invalid")
        item = binding.to_dict()
        require(item["role"] not in seen, "capability_role_duplicated",
                "Access role is declared more than once")
        seen.add(item["role"])
        rendered.append(item)
    return rendered


def _validate_environment(value: Any) -> dict:
    require(value is not None, "capability_environment_missing",
            "Required environment declaration is missing")
    code = "capability_environment_invalid"
    require(type(value) is dict and set(value) == {
        "schema_version", "contract_id", "contract_version", "requirements"}, code,
        "Environment declaration fields differ")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, code,
            "Environment declaration schema is unsupported")
    _text(value["contract_id"], code, "Environment contract identity is invalid")
    require(type(value["contract_version"]) is int and value["contract_version"] > 0, code,
            "Environment contract version is invalid")
    requirements = _items(value["requirements"], code,
                          "Environment requirements must be a bounded array", maximum=16)
    require(all(type(item) is str and 0 < len(item) <= _MAX_TEXT for item in requirements)
            and len(set(requirements)) == len(requirements), code,
            "Environment requirements are invalid")
    rendered = copy.deepcopy(value)
    rendered["requirements"] = list(requirements)
    return rendered


def _validate_lifecycle(value: Any) -> dict:
    require(value is not None, "capability_lifecycle_missing",
            "Required lifecycle declaration is missing")
    code = "capability_lifecycle_invalid"
    require(type(value) is dict and set(value) == {
        "schema_version", "owner", "close_entry", "roles", "borrowed_by"}, code,
        "Lifecycle declaration fields differ")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, code,
            "Lifecycle declaration schema is unsupported")
    _text(value["owner"], code, "Lifecycle owner is invalid")
    _text(value["close_entry"], code, "Lifecycle close entry is invalid")
    roles = _items(value["roles"], code, "Lifecycle roles must be a bounded array")
    require(all(type(item) is str and item in ACCESS_ROLES for item in roles)
            and len(set(roles)) == len(roles), code, "Lifecycle roles are invalid")
    borrowed = _items(value["borrowed_by"], code, "Lifecycle borrowers must be a bounded array")
    require(all(type(item) is str and 0 < len(item) <= _MAX_TEXT for item in borrowed)
            and len(set(borrowed)) == len(borrowed), code, "Lifecycle borrowers are invalid")
    rendered = copy.deepcopy(value)
    rendered["roles"] = list(roles)
    rendered["borrowed_by"] = list(borrowed)
    return rendered


def _validate_public_contracts(value: Any) -> dict:
    require(value is not None, "capability_public_contract_missing",
            "Required public contract declaration is missing")
    code = "capability_public_contract_invalid"
    require(type(value) is dict and set(value) == {
        "schema_version", "operation_groups", "service_definitions"}, code,
        "Public contract declaration fields differ")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, code,
            "Public contract declaration schema is unsupported")
    groups = _items(value["operation_groups"], code,
                    "Public operation groups must be a bounded array", maximum=8)
    rendered_groups = []
    for group in groups:
        require(type(group) is dict and set(group) == {"scope", "operations"}
                and group["scope"] in ("management", "execution", "lifecycle"), code,
                "Public operation group is invalid")
        operations = _items(group["operations"], code,
                            "Public operations must be a bounded array", maximum=128)
        require(all(type(item) is str and 0 < len(item) <= _MAX_TEXT for item in operations)
                and len(set(operations)) == len(operations), code,
                "Public operation identities are invalid")
        rendered_groups.append({"scope": group["scope"], "operations": list(operations)})
    services = value["service_definitions"]
    require(type(services) in (tuple, list) and len(services) <= 16, code,
            "Public service definitions must be a bounded array")
    rendered_services = []
    for service in services:
        require(type(service) is dict and set(service) == {"service_id", "exact_version"}, code,
                "Public service definition reference is invalid")
        rendered_services.append({
            "service_id": _text(service["service_id"], code,
                                "Public service identity is invalid"),
            "exact_version": _text(service["exact_version"], code,
                                   "Public service version is invalid")})
    return {"schema_version": 1,
            "operation_groups": rendered_groups,
            "service_definitions": rendered_services}


def _validate_frontend(value: Any) -> dict:
    require(value is not None, "capability_frontend_missing",
            "Required frontend access declaration is missing")
    code = "capability_frontend_invalid"
    require(type(value) is dict and set(value) == {
        "schema_version", "packages", "extensions", "containers"}, code,
        "Frontend access declaration fields differ")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, code,
            "Frontend access declaration schema is unsupported")
    packages = value["packages"]
    require(type(packages) in (tuple, list) and len(packages) <= 16, code,
            "Frontend packages must be a bounded array")
    rendered_packages = []
    for package in packages:
        require(type(package) is dict and set(package) == {"package_id", "version"}, code,
                "Frontend package reference is invalid")
        rendered_packages.append({
            "package_id": _text(package["package_id"], code,
                                "Frontend package identity is invalid"),
            "version": _text(package["version"], code, "Frontend package version is invalid")})
    extensions = value["extensions"]
    require(type(extensions) in (tuple, list) and len(extensions) <= 32, code,
            "Frontend extensions must be a bounded array")
    rendered_extensions = []
    for extension in extensions:
        require(type(extension) is dict and set(extension) == {"package_id", "extension_id"}, code,
                "Frontend extension reference is invalid")
        rendered_extensions.append({
            "package_id": _text(extension["package_id"], code,
                                "Frontend extension package identity is invalid"),
            "extension_id": _text(extension["extension_id"], code,
                                  "Frontend extension identity is invalid")})
    containers = _items(value["containers"], code,
                        "Frontend containers must be a bounded array", maximum=8)
    require(all(type(item) is str and 0 < len(item) <= _MAX_TEXT for item in containers)
            and len(set(containers)) == len(containers), code,
            "Frontend containers are invalid")
    return {"schema_version": 1, "packages": rendered_packages,
            "extensions": rendered_extensions, "containers": list(containers)}


def _validate_dependencies(value: Any) -> list[dict]:
    require(value is not None, "capability_dependencies_missing",
            "Required cross-domain dependency declaration is missing")
    dependencies = _items(value, "capability_dependencies_missing",
                          "Cross-domain dependencies must be a bounded array",
                          maximum=32)
    rendered, seen = [], set()
    for dependency in dependencies:
        require(isinstance(dependency, CapabilityDependency), "capability_dependency_invalid",
                "Cross-domain dependency entry is invalid")
        item = dependency.to_dict()
        require(item["dependency_id"] not in seen, "capability_dependency_duplicated",
                "Cross-domain dependency is declared more than once")
        seen.add(item["dependency_id"])
        rendered.append(item)
    return rendered


@dataclass(frozen=True)
class WorkflowCapabilityDeclaration:
    """The workflow source's minimal project access declaration."""

    capability: dict
    roles: tuple[RoleBinding, ...]
    environment: dict
    lifecycle: dict
    public_contracts: dict
    frontend: dict
    dependencies: tuple[CapabilityDependency, ...]

    def to_dict(self) -> dict:
        return {
            "schema_version": DECLARATION_SCHEMA_VERSION,
            "capability": _validate_capability(self.capability),
            "roles": _validate_roles(self.roles),
            "environment": _validate_environment(self.environment),
            "lifecycle": _validate_lifecycle(self.lifecycle),
            "public_contracts": _validate_public_contracts(self.public_contracts),
            "frontend": _validate_frontend(self.frontend),
            "dependencies": _validate_dependencies(self.dependencies),
        }


@dataclass(frozen=True)
class WorkflowCapabilitySource:
    """One trusted local source together with the declarations it maintains."""

    source_id: str
    declarations: tuple[WorkflowCapabilityDeclaration, ...]


def build_management_service(*, database, environment):
    """Trusted local binding: the workflow's original management construction path."""
    from .graph_service import GraphManagementService
    return GraphManagementService(database, environment=environment)


def build_execution_service(*, database, environment):
    """Trusted local binding: the workflow's original execution construction path."""
    from .graph_service import GraphWorkflowService
    return GraphWorkflowService(database, environment=environment)


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
    "owner": "project.workflow-host",
    "close_entry": "close",
    "roles": ACCESS_ROLES,
    "borrowed_by": ("http-server",),
}

PUBLIC_CONTRACTS = {
    "schema_version": 1,
    "operation_groups": (
        {"scope": "management", "operations": (
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
        dependency_id="dep.resource-content",
        caller="workflow.runtime",
        provider="workflow.resources",
        contracts=(CapabilityContractReference("operation", "resource.read"),
                   CapabilityContractReference("operation", "resource.list"),
                   CapabilityContractReference("operation", "content.metadata.read")),
        purpose="Declared node resource dependencies are frozen before a run and current "
                "global content records are read at use time.",
        condition="Only when a node declares resources:read or references global resources; "
                  "the resource type must be registered by an enabled package.",
        module="phase1_agent.graph_resource_host",
        anchor="def _content_dependencies",
        requirement="conditional",
        carrier="existing-operation (resources/content are not yet project capabilities)",
    ),
    CapabilityDependency(
        dependency_id="dep.provider-model",
        caller="workflow.management+runtime",
        provider="workflow.providers",
        contracts=(CapabilityContractReference("operation", "provider.models.list"),),
        purpose="Model nodes resolve and bind provider and model configuration through the "
                "existing provider operations.",
        condition="Only when provider/model nodes participate in a run or a user manages "
                  "provider configuration.",
        module="phase1_agent.graph_provider_host",
        anchor="class ProviderConfigurationService",
        requirement="conditional",
        carrier="existing-operation (providers are not yet project capabilities)",
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
        carrier="existing-service-definition (model supply is not yet a project capability)",
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
