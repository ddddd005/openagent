"""Project-level support: storage environment, definition management and thin acquisition.

This module is the project base runtime surface.  It owns no database connection,
no HTTP contract, no durable state, no second dispatch table and no scheduler:
the workflow keeps its own domain data management, commands, transactions and
execution authorities.

T2 adds the project side of the workflow access declaration (140 号 §6.1): the
project accepts, validates, indexes and queries the workflow source's minimal
declaration and its trusted local callable bindings, and reports structured
diagnostics.  Static checks inspect the declaration, public contract references
and binding callables only; they never open the database, take the execution
lease, build lazy services or import arbitrary modules.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

from .contract_errors import ContractValidationError, DiagnosticError, require
from .workflow_capability import (
    WorkflowCapabilityDeclaration,
    WorkflowCapabilitySource,
    local_workflow_capability_source,
)

ENVIRONMENT_MISMATCH = "project_environment_mismatch"

MANAGEMENT_ROLE = "management"
EXECUTION_ROLE = "execution"
REQUIRED_ROLES = (MANAGEMENT_ROLE, EXECUTION_ROLE)

SUPPORTED_ACCESS_CONTRACT_VERSIONS = frozenset({1})
SUPPORTED_ENVIRONMENT_CONTRACTS = frozenset({("project.storage-environment", 1)})
SUPPORTED_ENVIRONMENT_REQUIREMENTS = frozenset({"same_database_path", "per_operation_store"})
SUPPORTED_CONTRACT_KINDS = frozenset({
    "environment", "operation", "service_definition", "private_store",
    "frontend_package", "frontend_extension", "frontend_container",
})
SUPPORTED_PRIVATE_STORES = frozenset({"project.private-credential-store"})
REQUIRED_DEPENDENCY_CONTRACT_KINDS = ("environment", "operation", "service_definition",
                                      "private_store", "frontend_extension")
PROJECT_LIFECYCLE_OWNER = "project.workflow-host"

_PACKAGE_ROOT = Path(__file__).resolve().parent
_MODULE_PATTERN = re.compile(r"phase1_agent\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_MAX_TEXT = 256


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


def _diagnostic(code, message, *, severity="blocking", **details):
    return {"reason_code": code, "severity": severity, "message": message, **copy.deepcopy(details)}


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
    path = _PACKAGE_ROOT.joinpath(*module.split(".")[1:]).with_suffix(".py")
    if path not in cache:
        try:
            if not path.is_file():
                return False
            cache[path] = path.read_text(encoding="utf-8")
        except OSError:
            return False
    return anchor in cache[path]


class ProjectDefinitionManagement:
    """Project-level definition management over a trusted local workflow source.

    The project accepts, validates, indexes and queries the workflow source's
    minimal access declaration.  It does not construct workflow services, open a
    database, take the execution lease, activate lazy services or expose an
    import entrypoint.  Static diagnostics describe dependency, condition and
    result with safe reason codes; they never contain credentials or business
    content.
    """

    def __init__(self, source=None):
        self._records = {}
        self._diagnostics = []
        self._dependency_report = []
        if source is None:
            source = local_workflow_capability_source()
        self._source = source
        self._source_id = source.source_id if isinstance(source, WorkflowCapabilitySource) else None
        self._accept(source)

    # -- queries -----------------------------------------------------------

    def registered_sub_capabilities(self):
        """Identities of the project-registered workflow sub-capability set.

        Only the workflow capability is registered by this source.  Referenced
        provider/content/model services stay contract references and are not
        reported as already registered project sub-capabilities.
        """
        return tuple(sorted(self._records))

    def is_populated(self):
        return bool(self._records)

    def connected(self):
        return bool(self._records) and not any(
            item["severity"] == "blocking" for item in self._diagnostics)

    def describe(self):
        bindings = []
        for identity in sorted(self._records):
            for role, binding in sorted(self._records[identity]["roles"].items()):
                bindings.append({"capability_id": identity, "role": role,
                                 "service_contract": binding.service_contract})
        return {"sub_capabilities": list(self.registered_sub_capabilities()),
                "connected": self.connected(),
                "source": {"source_id": self._source_id},
                "bindings": bindings,
                "diagnostics": self.diagnostics()}

    def capabilities(self):
        return tuple(copy.deepcopy(self._records[identity]["declaration"])
                     for identity in sorted(self._records))

    def capability(self, identity):
        require(type(identity) is str and identity in self._records,
                "capability_not_found", "Project capability is not registered", 404)
        return copy.deepcopy(self._records[identity]["declaration"])

    def diagnostics(self):
        return copy.deepcopy(self._diagnostics)

    def dependency_report(self):
        return copy.deepcopy(self._dependency_report)

    # -- first role acquisition -------------------------------------------

    def require_role_binding(self, role, *, database, environment):
        """Return the declared trusted binding for one project role.

        The workflow source declaration is the only source of role bindings; a
        missing/invalid declaration, a wrong binding or an environment that does
        not satisfy the declared environment contract fails explicitly.  No
        fallback binding and no service construction happen here.
        """
        blocking = [item for item in self._diagnostics if item["severity"] == "blocking"]
        if not self._records:
            first = blocking[0] if blocking else None
            raise DiagnosticError(
                first["reason_code"] if first else "capability_declaration_missing",
                first["message"] if first else "No workflow access declaration is registered",
                status=409)
        if blocking:
            raise DiagnosticError(blocking[0]["reason_code"], blocking[0]["message"], status=409)
        require(role in REQUIRED_ROLES, "capability_role_unsupported",
                "Project acquisition role is unsupported")
        identity = sorted(self._records)[0]
        binding = self._records[identity]["roles"].get(role)
        if binding is None:
            raise DiagnosticError("capability_role_missing",
                                  f"Declared workflow access bindings do not include the {role} role",
                                  status=409)
        if not callable(binding.factory):
            raise DiagnosticError("capability_binding_not_callable",
                                  "Workflow access binding is not a trusted local callable",
                                  status=409)
        self._require_environment(self._records[identity]["declaration"]["environment"],
                                  database=database, environment=environment)
        return binding

    @staticmethod
    def _require_environment(declaration, *, database, environment):
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
                "Declared workflow environment is not satisfied by the project storage environment",
                status=409)

    # -- acceptance and static validation ----------------------------------

    def _accept(self, source):
        if not isinstance(source, WorkflowCapabilitySource):
            self._diagnostics.append(_diagnostic(
                "capability_source_invalid",
                "Project capability source is not a trusted local workflow source"))
            return
        if type(source.source_id) is not str or not 0 < len(source.source_id) <= _MAX_TEXT:
            self._diagnostics.append(_diagnostic(
                "capability_source_invalid", "Project capability source identity is invalid"))
            return
        declarations = source.declarations
        if type(declarations) not in (tuple, list) or not declarations:
            self._diagnostics.append(_diagnostic(
                "capability_declaration_missing",
                "Workflow source provided no access declaration"))
            return
        if len(declarations) != 1:
            self._diagnostics.append(_diagnostic(
                "capability_declaration_unsupported",
                "Project runtime accepts one workflow capability declaration"))
            return
        declaration = declarations[0]
        if not isinstance(declaration, WorkflowCapabilityDeclaration):
            self._diagnostics.append(_diagnostic(
                "capability_declaration_invalid",
                "Workflow source declaration is not a valid capability declaration"))
            return
        try:
            rendered = declaration.to_dict()
        except ContractValidationError as error:
            self._diagnostics.append(_diagnostic(
                getattr(error, "reason_code", "capability_declaration_invalid"), str(error)))
            return

        identity = rendered["capability"]["capability_id"]
        bindings = {}
        problems = self._diagnostics

        if rendered["capability"]["source"]["source_id"] != source.source_id:
            problems.append(_diagnostic(
                "capability_source_mismatch",
                "Declared capability source reference differs from the accepted source"))
        if rendered["capability"]["access_contract_version"] not in SUPPORTED_ACCESS_CONTRACT_VERSIONS:
            problems.append(_diagnostic(
                "capability_access_contract_unsupported",
                "Declared workflow access contract version is not supported"))
        self._check_roles(declaration, rendered, identity, bindings, problems)
        self._check_environment(rendered["environment"], problems)
        self._check_lifecycle(rendered["lifecycle"], problems)

        cache = {}
        operation_names = self._check_public_contracts(rendered["public_contracts"], problems, cache)
        self._check_frontend(rendered["frontend"], problems, cache)
        self._dependency_report = self._check_dependencies(
            rendered, problems, cache, operation_names)
        self._check_dependency_coverage(rendered["dependencies"], problems)

        self._records[identity] = {"declaration": rendered, "roles": bindings}

    @staticmethod
    def _check_roles(declaration, rendered, identity, bindings, problems):
        for binding in declaration.roles:
            if binding.role not in REQUIRED_ROLES:
                problems.append(_diagnostic(
                    "capability_role_unsupported",
                    "Declared acquisition role is not supported by the project",
                    role=binding.role))
                continue
            if binding.capability_id != identity:
                problems.append(_diagnostic(
                    "capability_binding_mismatch",
                    "Declared access binding identity differs from the capability identity",
                    role=binding.role))
            if not callable(binding.factory):
                problems.append(_diagnostic(
                    "capability_binding_not_callable",
                    "Workflow access binding is not a trusted local callable",
                    role=binding.role))
            bindings[binding.role] = binding
        declared = {binding["role"] for binding in rendered["roles"]}
        for role in REQUIRED_ROLES:
            if role not in declared:
                problems.append(_diagnostic(
                    "capability_role_missing",
                    "Declared workflow access bindings miss a required acquisition role",
                    role=role))

    @staticmethod
    def _check_environment(environment, problems):
        contract = (environment["contract_id"], environment["contract_version"])
        if contract not in SUPPORTED_ENVIRONMENT_CONTRACTS:
            problems.append(_diagnostic(
                "capability_environment_contract_unsupported",
                "Declared workflow environment contract is not supported by the project"))
        unsupported = set(environment["requirements"]) - SUPPORTED_ENVIRONMENT_REQUIREMENTS
        if unsupported:
            problems.append(_diagnostic(
                "capability_environment_requirement_unsupported",
                "Declared workflow environment requirement is not supported by the project",
                requirements=sorted(unsupported)))

    @staticmethod
    def _check_lifecycle(lifecycle, problems):
        if lifecycle["owner"] != PROJECT_LIFECYCLE_OWNER:
            problems.append(_diagnostic(
                "capability_lifecycle_mismatch",
                "Declared lifecycle owner is not the project Host"))
        if set(lifecycle["roles"]) != set(REQUIRED_ROLES):
            problems.append(_diagnostic(
                "capability_lifecycle_mismatch",
                "Declared lifecycle roles differ from the project acquisition roles"))

    def _check_public_contracts(self, public_contracts, problems, cache):
        operations = []
        for group in public_contracts["operation_groups"]:
            for name in group["operations"]:
                operations.append(name)
                if not _source_contains("phase1_agent.graph_application_contracts",
                                        f'"{name}"', cache):
                    problems.append(_diagnostic(
                        "capability_public_contract_unknown",
                        "Declared public operation is not present in the application contract",
                        operation=name))
        for service in public_contracts["service_definitions"]:
            anchor = f'"{service["service_id"]}", "{service["exact_version"]}"'
            if not _source_contains("phase1_agent.model_request_contracts", anchor, cache):
                problems.append(_diagnostic(
                    "capability_public_contract_unknown",
                    "Declared public service definition is not present in the service contract",
                    service_id=service["service_id"],
                    exact_version=service["exact_version"]))
        return operations

    @staticmethod
    def _check_frontend(frontend, problems, cache):
        for extension in frontend["extensions"]:
            anchor = f'"extension_id": "{extension["extension_id"]}"'
            if not _source_contains("phase1_agent.frontend_package", anchor, cache):
                problems.append(_diagnostic(
                    "capability_frontend_unknown",
                    "Declared frontend extension is not present in the frontend package declaration",
                    extension_id=extension["extension_id"]))
        for package in frontend["packages"]:
            anchor = f'"{package["package_id"]}": "{package["version"]}"'
            if not _source_contains("phase1_agent.builtin_packages", anchor, cache):
                problems.append(_diagnostic(
                    "capability_frontend_unknown",
                    "Declared frontend package is not present in the installed package composition",
                    package_id=package["package_id"], version=package["version"]))

    def _check_dependencies(self, rendered, problems, cache, operation_names):
        environment = rendered["environment"]
        environment_contract = (environment["contract_id"], str(environment["contract_version"]))
        operations = set(operation_names)
        service_refs = {(row["service_id"], row["exact_version"])
                        for row in rendered["public_contracts"]["service_definitions"]}
        frontend_packages = {(row["package_id"], row["version"])
                             for row in rendered["frontend"]["packages"]}
        frontend_extensions = {row["extension_id"] for row in rendered["frontend"]["extensions"]}
        frontend_containers = set(rendered["frontend"]["containers"])
        report = []
        for dependency in rendered["dependencies"]:
            requirement = dependency["requirement"]
            severity = "report" if requirement == "optional" else "blocking"
            dependency_problems = []

            def deny(code, message, **details):
                entry = _diagnostic(code, message, severity=severity,
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
                kind, identity = contract["kind"], contract["identity"]
                version = contract.get("version")
                covered = True
                if kind not in SUPPORTED_CONTRACT_KINDS:
                    covered = False
                elif kind == "environment":
                    covered = (identity, version) == environment_contract
                elif kind == "operation":
                    covered = identity in operations
                elif kind == "service_definition":
                    covered = (identity, version) in service_refs
                elif kind == "private_store":
                    covered = identity in SUPPORTED_PRIVATE_STORES
                elif kind == "frontend_package":
                    covered = (identity, version) in frontend_packages
                elif kind == "frontend_extension":
                    covered = identity in frontend_extensions
                elif kind == "frontend_container":
                    covered = identity in frontend_containers
                if not covered:
                    deny("capability_dependency_contract_uncovered",
                         "Declared dependency contract is not covered by the declaration",
                         dependency_id=dependency["dependency_id"],
                         condition=dependency["condition"],
                         contract={"kind": kind, "identity": identity, "version": version})
            report.append({
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
    def _check_dependency_coverage(dependencies, problems):
        kinds = {contract["kind"] for dependency in dependencies
                 for contract in dependency["contracts"]}
        missing = [kind for kind in REQUIRED_DEPENDENCY_CONTRACT_KINDS if kind not in kinds]
        if missing:
            problems.append(_diagnostic(
                "capability_dependency_coverage_missing",
                "Cross-domain dependency declaration misses a required dependency domain",
                contract_kinds=missing))


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


def require_matching_database(database_path, environment):
    """Reject an explicit workflow path that differs from the project environment."""
    from .graph_records import require

    database = Path(database_path).resolve()
    require(environment.database == database, ENVIRONMENT_MISMATCH,
            "Explicit database path differs from the project environment")
