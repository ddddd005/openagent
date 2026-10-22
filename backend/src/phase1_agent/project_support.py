"""Project-level support: storage environment, definition management and thin acquisition.

This module is the project base runtime surface.  It owns no database connection,
no HTTP contract, no durable state, no second dispatch table and no scheduler:
the workflow keeps its own domain data management, commands, transactions and
execution authorities.

147 R1 extends the project side of the capability declarations to the three
fixed trusted local sources: the workflow graph, the provider/model-profile
configuration and the global content configuration.  The project accepts,
validates, indexes and queries each source's minimal declaration and its trusted
local callable bindings, indexes them by exact ``capability_id`` (and role) and
reports structured diagnostics carrying the capability identity.  The common
declaration format and validation live in ``project_capability``; per-source
policy (required acquisition roles and dependency-coverage lower bound) is
reported by the source module itself and consumed generically here.

Static checks inspect the declaration, public contract references, frontend
source anchors and binding callables only; they never open the database, take the
execution lease, build services, import arbitrary modules or send network
requests.  First real acquisition keeps each existing domain service's own lazy
initialization.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

from .contract_errors import ContractValidationError, DiagnosticError, require
from .project_capability import (
    ACCESS_ROLES,
    CONTRACT_KINDS,
    ENVIRONMENT_MISMATCH,
    EXECUTION_ROLE,
    MANAGEMENT_ROLE,
    MAX_TEXT,
    PROJECT_LIFECYCLE_OWNER,
    CapabilityDeclaration,
    CapabilitySource,
    require_matching_database,
)
from .workflow_capability import (
    CAPABILITY_ID as WORKFLOW_CAPABILITY_ID,
    local_workflow_capability_source,
)

SUPPORTED_ACCESS_CONTRACT_VERSIONS = frozenset({1})
SUPPORTED_ENVIRONMENT_CONTRACTS = frozenset({("project.storage-environment", 1)})
SUPPORTED_ENVIRONMENT_REQUIREMENTS = frozenset({"same_database_path", "per_operation_store"})
SUPPORTED_CONTRACT_KINDS = CONTRACT_KINDS
SUPPORTED_PRIVATE_STORES = frozenset({"project.private-credential-store"})

# Frontend access references are verified against the fixed trusted package
# composition: the installed builtin capability packages' manifests export every
# real package/version pair and its frontend extension identities.  The
# composition module is imported lazily during declaration acceptance and only
# its manifests are read (no service construction, database, directory, lease,
# credential store, network or arbitrary import).  The built registry is cached
# per ProjectDefinitionManagement instance.
_PACKAGE_ROOT = Path(__file__).resolve().parent
_MODULE_PATTERN = re.compile(r"phase1_agent\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_MAX_TEXT = MAX_TEXT
_MAX_DECLARATIONS = 8

__all__ = [
    "ENVIRONMENT_MISMATCH",
    "EXECUTION_ROLE",
    "MANAGEMENT_ROLE",
    "ProjectDefinitionManagement",
    "ProjectExecutionManagement",
    "ProjectStorageEnvironment",
    "local_project_capability_sources",
    "require_matching_database",
]


class ProjectStorageEnvironment:
    """The project's shared SQLite main-library environment.

    Construction only resolves the path; it opens no connection and creates no
    directory or database.  Every ``open_store()`` acquires the same kind of
    per-operation store the workflow domain already uses, so independent reads
    remain independent connections over the existing main database.
    """

    def __init__(self, database_path):
        self.database = Path(database_path).resolve()

    def open_store(self, *, fault_injector=None):
        from .storage import SqliteStore

        return SqliteStore(self.database, fault_injector=fault_injector)

    def __repr__(self):
        return f"ProjectStorageEnvironment({str(self.database)!r})"


def _diagnostic(code, message, *, severity="blocking", capability_id=None, **details):
    entry = {"reason_code": code, "severity": severity, "message": message,
             **copy.deepcopy(details)}
    if capability_id is not None:
        entry["capability_id"] = capability_id
    return entry


def _source_contains(module, anchor, cache):
    """Statically confirm that one call-site anchor exists in a package source file.

    This is a read-only text check: it never imports the module, executes it or
    loads an entrypoint, so validating dependency call sites has no business
    side effect.
    """
    if type(module) is not str or _MODULE_PATTERN.fullmatch(module) is None:
        return False
    if type(anchor) is not str or not 0 < len(anchor) <= _MAX_TEXT:
        return False
    path = Path(_PACKAGE_ROOT).joinpath(*module.split(".")[1:]).with_suffix(".py")
    if path not in cache:
        try:
            if not path.is_file():
                return False
            cache[path] = path.read_text(encoding="utf-8")
        except OSError:
            return False
    return anchor in cache[path]


def _domain_capability_sources():
    """The two fixed domain sources, imported lazily to keep the neutral modules
    free of an import cycle with ``project_support``."""
    from .content_capability import local_content_capability_source
    from .provider_capability import local_provider_capability_source

    return (local_provider_capability_source(), local_content_capability_source())


def local_project_capability_sources():
    """The three fixed trusted capability sources in their fixed composition order.

    The composition is deliberately explicit: workflow graph, provider/model
    profile, then global content.  It never depends on a directory listing, a
    sorted search or a user configuration.  The domain modules are imported
    lazily here so importing the project support surface stays free of the domain
    service modules.
    """
    return (local_workflow_capability_source(), *_domain_capability_sources())


class ProjectDefinitionManagement:
    """Project-level definition management over the fixed trusted capability sources.

    The project accepts, validates, indexes and queries each source's minimal
    access declaration.  It does not construct services, open a database, take
    the execution lease, activate lazy services or expose an import entrypoint.
    Static diagnostics describe dependency, condition and result with safe reason
    codes and carry the capability identity; they never contain credentials or
    business content.

    ``source`` is the workflow-source replacement/test seam: one injected
    ``CapabilitySource`` replaces the workflow source and the fixed provider and
    content sources are still composed.  ``sources`` injects the exact source
    tuple for focused tests.  Neither seam is a string entrypoint or a dynamic
    import.
    """

    def __init__(self, source=None, *, sources=None):
        if source is not None and sources is not None:
            raise DiagnosticError(
                "capability_source_invalid",
                "Project definition management accepts one injected source or exact sources, not both",
                status=409)
        self._records = {}
        self._sources = []
        self._diagnostics = []
        self._dependency_report = []
        self._frontend_registry = None
        if sources is not None:
            accepted = (sources,) if type(sources) not in (tuple, list) else tuple(sources)
        elif source is not None:
            accepted = (source, *_domain_capability_sources())
        else:
            accepted = local_project_capability_sources()
        for item in accepted:
            self._accept(item)

    # -- queries -----------------------------------------------------------

    def registered_sub_capabilities(self):
        """Identities of the project-registered capability set, sorted by identity."""
        return tuple(sorted(self._records))

    def is_populated(self):
        return bool(self._records)

    def connected(self):
        return bool(self._records) and not any(
            item["severity"] == "blocking" for item in self._diagnostics)

    def describe(self):
        bindings = []
        for identity in sorted(self._records):
            record = self._records[identity]
            for role, binding in sorted(record["roles"].items()):
                bindings.append({"capability_id": identity, "role": role,
                                 "service_contract": binding.service_contract,
                                 "source_id": record["source_id"]})
        return {"sub_capabilities": list(self.registered_sub_capabilities()),
                "connected": self.connected(),
                "sources": copy.deepcopy(self._sources),
                "bindings": bindings,
                "diagnostics": self.diagnostics()}

    def capabilities(self):
        return tuple(copy.deepcopy(self._records[identity]["declaration"])
                     for identity in sorted(self._records))

    def capability(self, identity):
        require(type(identity) is str and identity in self._records,
                "capability_not_found", "Project capability is not registered", 404)
        return copy.deepcopy(self._records[identity]["declaration"])

    def diagnostics(self, *, capability_id=None):
        if capability_id is None:
            return copy.deepcopy(self._diagnostics)
        return copy.deepcopy([item for item in self._diagnostics
                              if item.get("capability_id") == capability_id])

    def dependency_report(self):
        return copy.deepcopy(self._dependency_report)

    # -- role acquisition --------------------------------------------------

    def require_role_binding(self, role, *, database, environment, capability_id=None):
        """Return the declared trusted binding for one project capability role.

        ``capability_id`` explicitly defaults to the workflow capability so the
        historical workflow-only entry keeps working without relying on any
        directory or declaration order.  The requested capability's own blocking
        diagnostics decide the result: a broken provider/content declaration does
        not report success or failure for another declared capability, and a
        missing domain source fails explicitly instead of falling back.

        A missing/invalid declaration, a wrong binding or an environment that
        does not satisfy the declared environment contract fails explicitly.  No
        fallback binding and no service construction happen here.
        """
        if capability_id is None:
            capability_id = WORKFLOW_CAPABILITY_ID
        require(type(capability_id) is str and 0 < len(capability_id) <= _MAX_TEXT,
                "capability_identity_invalid", "Project capability identity is invalid")
        require(role in ACCESS_ROLES, "capability_role_unsupported",
                "Project acquisition role is unsupported")
        global_blocking = [item for item in self._diagnostics
                           if item["severity"] == "blocking" and "capability_id" not in item]
        scoped_blocking = [item for item in self._diagnostics
                           if item["severity"] == "blocking"
                           and item.get("capability_id") == capability_id]
        if capability_id not in self._records:
            first = scoped_blocking[0] if scoped_blocking else (
                global_blocking[0] if global_blocking else None)
            if first is not None:
                raise DiagnosticError(first["reason_code"], first["message"], status=409)
            raise DiagnosticError("capability_not_registered",
                                  "Requested project capability is not registered", status=409)
        if scoped_blocking:
            first = scoped_blocking[0]
            raise DiagnosticError(first["reason_code"], first["message"], status=409)
        if global_blocking:
            first = global_blocking[0]
            raise DiagnosticError(first["reason_code"], first["message"], status=409)
        binding = self._records[capability_id]["roles"].get(role)
        if binding is None:
            raise DiagnosticError(
                "capability_role_missing",
                f"Declared access bindings of {capability_id} do not include the {role} role",
                status=409, capability_id=capability_id)
        if not callable(binding.factory):
            raise DiagnosticError(
                "capability_binding_not_callable",
                "Project capability access binding is not a trusted local callable",
                status=409, capability_id=capability_id)
        self._require_environment(
            self._records[capability_id]["declaration"]["environment"],
            database=database, environment=environment, capability_id=capability_id)
        return binding

    @staticmethod
    def _require_environment(declaration, *, database, environment, capability_id=None):
        requirements = set(declaration["requirements"])
        satisfied = True
        if "same_database_path" in requirements:
            try:
                satisfied = Path(environment.database).resolve() == Path(database).resolve()
            except (AttributeError, TypeError, ValueError, OSError):
                satisfied = False
        if satisfied and "per_operation_store" in requirements:
            satisfied = callable(getattr(environment, "open_store", None))
        if not satisfied:
            raise DiagnosticError(
                "capability_environment_mismatch",
                "Declared capability environment is not satisfied by the project storage environment",
                status=409, capability_id=capability_id)

    # -- acceptance and static validation ----------------------------------

    def _accept(self, source):
        if not isinstance(source, CapabilitySource):
            self._diagnostics.append(_diagnostic(
                "capability_source_invalid",
                "Project capability source is not a trusted local capability source"))
            return
        source_id = source.source_id
        if type(source_id) is not str or not 0 < len(source_id) <= _MAX_TEXT:
            self._diagnostics.append(_diagnostic(
                "capability_source_invalid", "Project capability source identity is invalid"))
            return
        required_roles = self._validate_source_policy(source.required_roles, ACCESS_ROLES)
        if required_roles is None:
            self._diagnostics.append(_diagnostic(
                "capability_source_invalid",
                "Project capability source required roles are invalid", source_id=source_id))
            return
        required_kinds = self._validate_source_policy(
            source.required_dependency_contract_kinds, CONTRACT_KINDS)
        if required_kinds is None:
            self._diagnostics.append(_diagnostic(
                "capability_source_invalid",
                "Project capability source dependency coverage policy is invalid",
                source_id=source_id))
            return
        declarations = source.declarations
        if type(declarations) not in (tuple, list) or not declarations:
            self._diagnostics.append(_diagnostic(
                "capability_declaration_missing",
                "Project capability source provided no access declaration",
                source_id=source_id))
            return
        if len(declarations) > _MAX_DECLARATIONS:
            self._diagnostics.append(_diagnostic(
                "capability_declaration_unsupported",
                "Project capability source declaration count exceeds its limit",
                source_id=source_id))
            return
        source_record = {"source_id": source_id,
                         "required_roles": list(required_roles),
                         "required_dependency_contract_kinds": list(required_kinds),
                         "capability_ids": []}
        self._sources.append(source_record)
        for declaration in declarations:
            self._accept_declaration(source_record, required_roles, required_kinds, declaration)

    @staticmethod
    def _validate_source_policy(value, allowed):
        if type(value) not in (tuple, list):
            return None
        values = tuple(value)
        if not all(type(item) is str and item in allowed for item in values) \
                or len(set(values)) != len(values):
            return None
        return values

    def _accept_declaration(self, source_record, required_roles, required_kinds, declaration):
        source_id = source_record["source_id"]
        candidate = None
        if isinstance(declaration, CapabilityDeclaration) and type(declaration.capability) is dict:
            candidate = declaration.capability.get("capability_id")
        if type(candidate) is not str or not 0 < len(candidate) <= _MAX_TEXT:
            candidate = None
        if candidate is not None and candidate in self._records:
            self._diagnostics.append(_diagnostic(
                "capability_identity_duplicated",
                "Project capability identity is declared more than once; the first declaration is kept",
                capability_id=candidate, source_id=source_id))
            return
        if not isinstance(declaration, CapabilityDeclaration):
            self._diagnostics.append(_diagnostic(
                "capability_declaration_invalid",
                "Source declaration is not a valid capability declaration",
                capability_id=candidate, source_id=source_id))
            return
        try:
            rendered = declaration.to_dict()
        except ContractValidationError as error:
            self._diagnostics.append(_diagnostic(
                getattr(error, "reason_code", "capability_declaration_invalid"), str(error),
                capability_id=candidate, source_id=source_id))
            return
        identity = rendered["capability"]["capability_id"]
        if identity in self._records:
            self._diagnostics.append(_diagnostic(
                "capability_identity_duplicated",
                "Project capability identity is declared more than once; the first declaration is kept",
                capability_id=identity, source_id=source_id))
            return

        bindings = {}
        problems = self._diagnostics
        if rendered["capability"]["source"]["source_id"] != source_id:
            problems.append(_diagnostic(
                "capability_source_mismatch",
                "Declared capability source reference differs from the accepted source",
                capability_id=identity, source_id=source_id))
        if rendered["capability"]["access_contract_version"] not in SUPPORTED_ACCESS_CONTRACT_VERSIONS:
            problems.append(_diagnostic(
                "capability_access_contract_unsupported",
                "Declared capability access contract version is not supported",
                capability_id=identity))
        self._check_roles(declaration, rendered, identity, bindings, problems)
        self._check_required_roles(rendered, identity, required_roles, problems)
        self._check_environment(rendered["environment"], identity, problems)
        self._check_lifecycle(rendered["lifecycle"], rendered["roles"], identity, problems)

        cache = {}
        operation_names = self._check_public_contracts(
            rendered["public_contracts"], identity, problems, cache)
        frontend_packages, frontend_extensions = self._check_frontend(
            rendered["frontend"], identity, problems)
        dependency_report = self._check_dependencies(
            rendered, identity, problems, cache, operation_names,
            frontend_packages=frontend_packages,
            frontend_extensions=frontend_extensions)
        self._dependency_report.extend(dependency_report)
        self._check_dependency_coverage(
            rendered["dependencies"], identity, required_kinds, problems)

        self._records[identity] = {"declaration": rendered, "roles": bindings,
                                   "source_id": source_id}
        source_record["capability_ids"].append(identity)

    @staticmethod
    def _check_roles(declaration, rendered, identity, bindings, problems):
        for binding in declaration.roles:
            if binding.capability_id != identity:
                problems.append(_diagnostic(
                    "capability_binding_mismatch",
                    "Declared access binding identity differs from the capability identity",
                    capability_id=identity, role=binding.role))
            bindings[binding.role] = binding

    @staticmethod
    def _check_required_roles(rendered, identity, required_roles, problems):
        declared = {binding["role"] for binding in rendered["roles"]}
        for role in required_roles:
            if role not in declared:
                problems.append(_diagnostic(
                    "capability_role_missing",
                    "Declared access bindings miss a source-required acquisition role",
                    capability_id=identity, role=role))

    @staticmethod
    def _check_environment(environment, identity, problems):
        contract = (environment["contract_id"], environment["contract_version"])
        if contract not in SUPPORTED_ENVIRONMENT_CONTRACTS:
            problems.append(_diagnostic(
                "capability_environment_contract_unsupported",
                "Declared environment contract is not supported by the project",
                capability_id=identity))
        unsupported = set(environment["requirements"]) - SUPPORTED_ENVIRONMENT_REQUIREMENTS
        if unsupported:
            problems.append(_diagnostic(
                "capability_environment_requirement_unsupported",
                "Declared environment requirement is not supported by the project",
                capability_id=identity, requirements=sorted(unsupported)))

    @staticmethod
    def _check_lifecycle(lifecycle, rendered_roles, identity, problems):
        if lifecycle["owner"] != PROJECT_LIFECYCLE_OWNER:
            problems.append(_diagnostic(
                "capability_lifecycle_mismatch",
                "Declared lifecycle owner is not the project Host",
                capability_id=identity))
        declared = {binding["role"] for binding in rendered_roles}
        if set(lifecycle["roles"]) != declared:
            problems.append(_diagnostic(
                "capability_lifecycle_mismatch",
                "Declared lifecycle roles differ from the declared acquisition roles",
                capability_id=identity))

    def _check_public_contracts(self, public_contracts, identity, problems, cache):
        operations = []
        for group in public_contracts["operation_groups"]:
            for name in group["operations"]:
                operations.append(name)
                if not _source_contains("phase1_agent.graph_application_contracts",
                                        f'"{name}"', cache):
                    problems.append(_diagnostic(
                        "capability_public_contract_unknown",
                        "Declared public operation is not present in the application contract",
                        capability_id=identity, operation=name))
        for service in public_contracts["service_definitions"]:
            anchor = f'"{service["service_id"]}", "{service["exact_version"]}"'
            if not _source_contains("phase1_agent.model_request_contracts", anchor, cache):
                problems.append(_diagnostic(
                    "capability_public_contract_unknown",
                    "Declared public service definition is not present in the service contract",
                    capability_id=identity, service_id=service["service_id"],
                    exact_version=service["exact_version"]))
        return operations

    def _frontend_package_registry(self):
        """The fixed trusted package composition's frontend export registry.

        Built lazily from the installed builtin capability packages' manifests
        only: ``(package_id, version) -> exported extension identities``.  The
        composition module is imported on first use and the registry is cached
        per definition-management instance; no service is constructed, no
        database/directory/lease/credential store is touched and no network or
        arbitrary import happens.  A manifest that exports no frontend
        extensions still registers its package/version pair, so declared package
        references are checked even when they own no frontend extension.
        """
        if self._frontend_registry is None:
            from .builtin_packages import builtin_capability_packages

            registry = {}
            for package in builtin_capability_packages():
                manifest = package.manifest
                key = (manifest.package_id, manifest.version)
                extensions = {row["extension_id"]
                              for row in manifest.exports.get("frontend_extensions", ())}
                registry.setdefault(key, set()).update(extensions)
            self._frontend_registry = {key: frozenset(value)
                                       for key, value in registry.items()}
        return self._frontend_registry

    def _check_frontend(self, frontend, identity, problems):
        """Validate the declaration's frontend references against the real composition.

        Every declared ``(package_id, version)`` pair must exist in the fixed
        trusted package composition, and every declared frontend extension must
        be exported by the very package/version pair the declaration claims for
        it -- a literal match in some module or an extension that exists only
        under another package/version is rejected.  Returns the validated
        package pairs and extension identities so dependency coverage is checked
        against the same pairing.
        """
        registry = self._frontend_package_registry()
        declared_versions = {}
        valid_packages = set()
        for package in frontend["packages"]:
            package_id, version = package["package_id"], package["version"]
            declared_versions.setdefault(package_id, set()).add(version)
            if (package_id, version) in registry:
                valid_packages.add((package_id, version))
                continue
            known_versions = sorted(row_version for row_package, row_version in registry
                                    if row_package == package_id)
            problems.append(_diagnostic(
                "capability_frontend_unknown",
                "Declared frontend package/version pair is not present in the fixed "
                "trusted package composition",
                capability_id=identity, package_id=package_id, version=version,
                known_versions=known_versions))
        valid_extensions = set()
        for extension in frontend["extensions"]:
            package_id = extension["package_id"]
            extension_id = extension["extension_id"]
            versions = declared_versions.get(package_id)
            if not versions:
                problems.append(_diagnostic(
                    "capability_frontend_unknown",
                    "Declared frontend extension belongs to a package the capability does "
                    "not declare",
                    capability_id=identity, package_id=package_id,
                    extension_id=extension_id,
                    declared_packages=sorted(declared_versions)))
                continue
            matching = sorted(version for version in versions
                              if extension_id in registry.get((package_id, version), ()))
            if matching:
                valid_extensions.add(extension_id)
                continue
            exported_by = [{"package_id": row_package, "version": row_version}
                           for row_package, row_version in sorted(registry)
                           if extension_id in registry[(row_package, row_version)]]
            problems.append(_diagnostic(
                "capability_frontend_unknown",
                "Declared frontend extension is not exported by its declared "
                "package/version pair",
                capability_id=identity, package_id=package_id,
                extension_id=extension_id, declared_versions=sorted(versions),
                exported_by=exported_by))
        return valid_packages, valid_extensions

    def _check_dependencies(self, rendered, identity, problems, cache, operation_names, *,
                            frontend_packages, frontend_extensions):
        environment = rendered["environment"]
        environment_contract = (environment["contract_id"], str(environment["contract_version"]))
        operations = set(operation_names)
        service_refs = {(row["service_id"], row["exact_version"])
                        for row in rendered["public_contracts"]["service_definitions"]}
        frontend_containers = set(rendered["frontend"]["containers"])
        report = []
        for dependency in rendered["dependencies"]:
            requirement = dependency["requirement"]
            severity = "report" if requirement == "optional" else "blocking"
            dependency_problems = []

            def deny(code, message, **details):
                entry = _diagnostic(code, message, severity=severity, capability_id=identity,
                                    result="missing", **details)
                dependency_problems.append(entry)
                problems.append(entry)

            call_site = dependency["call_site"]
            if not _source_contains(call_site["module"], call_site["anchor"], cache):
                deny("capability_dependency_call_site_missing",
                     "Declared dependency call site is not present in the current source",
                     dependency_id=dependency["dependency_id"],
                     condition=dependency["condition"], call_site=call_site)
            for contract in dependency["contracts"]:
                kind, contract_identity = contract["kind"], contract["identity"]
                version = contract.get("version")
                covered = True
                if kind not in SUPPORTED_CONTRACT_KINDS:
                    covered = False
                elif kind == "environment":
                    covered = (contract_identity, version) == environment_contract
                elif kind == "operation":
                    covered = contract_identity in operations
                elif kind == "service_definition":
                    covered = (contract_identity, version) in service_refs
                elif kind == "private_store":
                    covered = contract_identity in SUPPORTED_PRIVATE_STORES
                elif kind == "frontend_package":
                    covered = (contract_identity, version) in frontend_packages
                elif kind == "frontend_extension":
                    covered = contract_identity in frontend_extensions
                elif kind == "frontend_container":
                    covered = contract_identity in frontend_containers
                if not covered:
                    deny("capability_dependency_contract_uncovered",
                         "Declared dependency contract is not covered by the declaration",
                         dependency_id=dependency["dependency_id"],
                         condition=dependency["condition"],
                         contract={"kind": kind, "identity": contract_identity,
                                   "version": version})
            report.append({
                "capability_id": identity,
                "dependency_id": dependency["dependency_id"],
                "caller": dependency["caller"],
                "provider": dependency["provider"],
                "purpose": dependency["purpose"],
                "condition": dependency["condition"],
                "requirement": requirement,
                "carrier": dependency["carrier"],
                "contracts": copy.deepcopy(dependency["contracts"]),
                "call_site": copy.deepcopy(call_site),
                "static_result": "missing" if dependency_problems else "present",
                "problems": [entry["reason_code"] for entry in dependency_problems],
            })
        return report

    @staticmethod
    def _check_dependency_coverage(dependencies, identity, required_kinds, problems):
        kinds = {contract["kind"] for dependency in dependencies
                 for contract in dependency["contracts"]}
        missing = [kind for kind in required_kinds if kind not in kinds]
        if missing:
            problems.append(_diagnostic(
                "capability_dependency_coverage_missing",
                "Cross-domain dependency declaration misses a source-required dependency domain",
                capability_id=identity, contract_kinds=missing))


class ProjectExecutionManagement:
    """Thin project-level acquisition organization over existing workflow entries.

    It owns no dispatch table, run state, receipt or scheduler: each method
    delegates to the acquisition callable supplied by the lifecycle owner, so
    the existing workflow authorities keep deciding who answers a request.
    """

    def __init__(self, *, management_entry, execution_entry, lifecycle_entry):
        self._management_entry = management_entry
        self._execution_entry = execution_entry
        self._lifecycle_entry = lifecycle_entry

    def management_entry(self):
        return self._management_entry()

    def execution_entry(self):
        return self._execution_entry()

    def lifecycle_entry(self):
        return self._lifecycle_entry()
