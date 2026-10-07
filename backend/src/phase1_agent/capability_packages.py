"""Trusted local package discovery, exact dependency resolution and staging.

Loading builds a detached registry and publishes it only after every package
has registered successfully. It never migrates stored data or changes an
already returned runtime registry. This is a trusted-code entry point, not a
process sandbox or remote marketplace.
"""

from __future__ import annotations

import copy
import importlib
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .contract_json import canonical_bytes, validate_json_value
from .graph_contracts import NodeDefinition, NodeRegistry
from .host_sdk import (
    DataTypeDefinition, HOST_PROTOCOL_VERSION, HostContractError, InformationSourceDefinition,
    ServiceDefinition, bounded_name, ensure,
)
from .runtime_executor_contracts import ExecutorDefinition, PauseSupport


@dataclass(frozen=True)
class PackageDependency:
    package_id: str
    version: str

    def to_dict(self) -> dict:
        ensure(bounded_name(self.package_id) and bounded_name(self.version),
               "package_invalid_manifest", "Dependencies require a package identity and exact version")
        return {"package_id": self.package_id, "version": self.version}


@dataclass(frozen=True)
class PackageManifest:
    package_id: str
    version: str
    dependencies: tuple[PackageDependency, ...] = ()
    host_protocol_version: int = HOST_PROTOCOL_VERSION
    exports: dict = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> dict:
        value = {"schema_version": self.schema_version, "package_id": self.package_id, "version": self.version,
                 "host_protocol_version": self.host_protocol_version,
                 "dependencies": [item.to_dict() for item in self.dependencies],
                 "exports": copy.deepcopy(self.exports)}
        _validate_manifest(value)
        return value

    @classmethod
    def from_dict(cls, value: dict) -> PackageManifest:
        checked = _validate_manifest(value)
        return cls(checked["package_id"], checked["version"],
                   tuple(PackageDependency(**item) for item in checked["dependencies"]),
                   checked["host_protocol_version"], checked["exports"], checked["schema_version"])


def _validate_manifest(value: dict) -> dict:
    validate_json_value(value)
    ensure(type(value) is dict and set(value) == {
        "schema_version", "package_id", "version", "host_protocol_version", "dependencies", "exports",
    } and type(value["schema_version"]) is int and value["schema_version"] in (1, 2, 3, 4)
        and bounded_name(value["package_id"]) and bounded_name(value["version"])
        and type(value["host_protocol_version"]) is int and value["host_protocol_version"] > 0,
        "package_invalid_manifest", "Package manifest identity and protocol fields are invalid")
    dependencies = value["dependencies"]
    ensure(type(dependencies) is list and len(dependencies) <= 256,
           "package_invalid_manifest", "Package dependencies must be a bounded array")
    dependency_ids: set[str] = set()
    for dependency in dependencies:
        ensure(type(dependency) is dict and set(dependency) == {"package_id", "version"}
               and bounded_name(dependency["package_id"]) and bounded_name(dependency["version"])
               and dependency["package_id"] not in dependency_ids,
               "package_invalid_manifest", "Package dependencies must have unique identities and exact versions")
        dependency_ids.add(dependency["package_id"])
    exports = value["exports"]
    allowed_exports = {"data_types", "nodes", "frontend_extensions"}
    if value["schema_version"] >= 2:
        allowed_exports |= {"executors", "pause_support"}
    if value["schema_version"] >= 3:
        allowed_exports.add("services")
    if value["schema_version"] >= 4:
        allowed_exports.add("information_sources")
    ensure(type(exports) is dict and set(exports) <= allowed_exports,
           "package_invalid_manifest", "Package export declarations are invalid")
    for kind, declarations in exports.items():
        ensure(type(declarations) is list and len(declarations) <= 4096,
               "package_invalid_manifest", "Package exports must be bounded arrays")
        seen: set[bytes] = set()
        for declaration in declarations:
            if kind == "data_types":
                valid = (type(declaration) is dict
                         and set(declaration) == {"scope", "type_id", "schema_version"}
                         and declaration["scope"] in ("content", "session", "global")
                         and bounded_name(declaration["type_id"])
                         and type(declaration["schema_version"]) is int and declaration["schema_version"] > 0)
            elif kind == "nodes":
                valid = (type(declaration) is dict and set(declaration) == {"component_id", "component_version"}
                         and bounded_name(declaration["component_id"]) and bounded_name(declaration["component_version"]))
            elif kind in ("executors", "pause_support"):
                valid = (type(declaration) is dict and set(declaration) == {"executor_id", "exact_version"}
                         and bounded_name(declaration["executor_id"]) and bounded_name(declaration["exact_version"]))
            elif kind == "services":
                valid = (type(declaration) is dict and set(declaration) == {"service_id", "exact_version"}
                         and bounded_name(declaration["service_id"]) and bounded_name(declaration["exact_version"]))
            elif kind == "information_sources":
                valid = (type(declaration) is dict and set(declaration) == {"source_id", "exact_version"}
                         and bounded_name(declaration["source_id"]) and bounded_name(declaration["exact_version"]))
            else:
                valid = type(declaration) is dict and set(declaration) == {"extension_id"} \
                    and bounded_name(declaration["extension_id"])
            ensure(valid, "package_invalid_manifest", "Package export identity is invalid")
            identity = canonical_bytes(declaration)
            ensure(identity not in seen, "package_invalid_manifest", "Package export identity is duplicated")
            seen.add(identity)
    ensure(len(canonical_bytes(value)) <= 1_000_000,
           "package_invalid_manifest", "Package manifest exceeds its limit")
    return copy.deepcopy(value)


@dataclass(frozen=True)
class CapabilityPackage:
    manifest: PackageManifest
    register: Callable[[HostRegistration], None]


class HostRegistration:
    """Registration surface granted to a trusted package during staging."""

    def __init__(self, registry: NodeRegistry, manifest: PackageManifest,
                 frontend_extensions: dict[str, dict]):
        self._registry, self._manifest = registry, manifest
        self._frontend_extensions = frontend_extensions
        self._exports: dict[str, list[dict]] = {"data_types": [], "nodes": [], "frontend_extensions": []}
        if manifest.schema_version >= 2:
            self._exports.update({"executors": [], "pause_support": []})
        if manifest.schema_version >= 3:
            self._exports["services"] = []
        if manifest.schema_version >= 4:
            self._exports["information_sources"] = []

    def register_information_source(self, definition: InformationSourceDefinition,
                                    reader_factory: Callable | None = None,
                                    history_reader: Callable | None = None) -> None:
        ensure(self._manifest.schema_version >= 4, "package_invalid_manifest",
               "Information sources require package manifest schema 4")
        self._registry.information_sources.register(definition, reader_factory, history_reader)
        self._exports["information_sources"].append(definition.reference.to_dict())

    def register_service(self, definition: ServiceDefinition, factory: Callable) -> None:
        ensure(self._manifest.schema_version >= 3, "package_invalid_manifest",
               "Public services require package manifest schema 3")
        self._registry.services.register(definition, factory)
        self._exports["services"].append(definition.reference.to_dict())

    def register_executor(self, definition: ExecutorDefinition, factory: Callable) -> None:
        ensure(self._manifest.schema_version >= 2, "package_invalid_manifest",
               "Public executors require package manifest schema 2")
        self._registry.executors.register_executor(definition, factory)
        self._exports["executors"].append(definition.reference.to_dict())

    def register_pause_support(self, definition: PauseSupport, adapter: Callable) -> None:
        ensure(self._manifest.schema_version >= 2, "package_invalid_manifest",
               "Public pause support requires package manifest schema 2")
        self._registry.executors.register_pause_support(definition, adapter)
        self._exports["pause_support"].append(definition.reference.to_dict())

    def register_data_type(self, definition: DataTypeDefinition) -> None:
        self._registry.data_types.register(definition)
        self._exports["data_types"].append({
            "scope": definition.scope, "type_id": definition.type_id, "schema_version": definition.schema_version,
        })

    def register_shared_data_type(self, definition: DataTypeDefinition) -> None:
        """Share an exact transport contract without silently redefining it."""
        existing = self._registry.data_types.get(definition.type_id, definition.schema_version,
                                                 scope=definition.scope)
        ensure(existing is None or canonical_bytes(existing.to_dict()) == canonical_bytes(definition.to_dict()),
               "package_contract_redefined", "Shared transport type must match the exact registered contract")
        if existing is None:
            self._registry.data_types.register(definition)
        self._exports["data_types"].append({
            "scope": definition.scope, "type_id": definition.type_id, "schema_version": definition.schema_version,
        })

    def register_node(self, definition: NodeDefinition, executor: Callable | None, **implementation_hooks) -> None:
        self._registry.register(definition, executor, **implementation_hooks)
        self._exports["nodes"].append({
            "component_id": definition.component_id, "component_version": definition.component_version,
        })

    def register_frontend_extension(self, extension_id: str, kind: str, entrypoint: str,
                                    *, component_id: str | None = None,
                                    component_version: str | None = None,
                                    binding: dict | None = None,
                                    host_protocol_version: int | None = None) -> None:
        kinds = ("renderer", "field-editor", "resource-editor", "consumer")
        ensure(bounded_name(extension_id) and kind in (*kinds, "workbench-panel")
               and type(entrypoint) is str and 0 < len(entrypoint) <= 512
               and (component_id is None and component_version is None
                    or bounded_name(component_id) and bounded_name(component_version)),
               "package_invalid_extension", "Frontend extension declaration is invalid")
        if binding is None:
            ensure(host_protocol_version is None and kind in kinds,
                   "package_invalid_extension", "Version 2 frontend extensions require an explicit binding")
        else:
            from .frontend_extension_contracts import validate_frontend_binding
            binding = validate_frontend_binding(kind, entrypoint, binding, host_protocol_version,
                                                component_id, component_version)
        ensure(extension_id not in self._frontend_extensions,
               "package_duplicate_extension", "Frontend extension identity is already registered")
        self._frontend_extensions[extension_id] = {
            "extension_id": extension_id, "kind": kind, "entrypoint": entrypoint,
            "component_id": component_id, "component_version": component_version,
            "package_id": self._manifest.package_id, "package_version": self._manifest.version,
        }
        if binding is not None:
            self._frontend_extensions[extension_id].update(
                schema_version=2, host_protocol_version=host_protocol_version, binding=binding)
        self._exports["frontend_extensions"].append({"extension_id": extension_id})

    def finish(self) -> dict:
        for kind, expected in self._manifest.exports.items():
            ensure(sorted(map(canonical_bytes, expected)) == sorted(map(canonical_bytes, self._exports[kind])),
                   "package_export_mismatch", "Registered exports do not match the package manifest")
        return copy.deepcopy(self._exports)


@dataclass(frozen=True)
class ResolvedPackageSelection:
    """Dependency metadata only; declared exports have not been registered."""

    package_lock: tuple[dict, ...]
    package_manifests: tuple[dict, ...]
    registration_order: tuple[PackageDependency, ...]

    def to_dict(self) -> dict:
        return {"package_lock": copy.deepcopy(list(self.package_lock)),
                "package_manifests": copy.deepcopy(list(self.package_manifests)),
                "registration_order": [item.to_dict() for item in self.registration_order]}


@dataclass(frozen=True)
class LoadedCapabilities:
    registry: NodeRegistry
    package_lock: tuple[dict, ...]
    frontend_extensions: tuple[dict, ...]
    package_manifests: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {"host_protocol_version": HOST_PROTOCOL_VERSION,
                "package_lock": copy.deepcopy(list(self.package_lock)),
                "execution_package_lock": copy.deepcopy(list(self.registry.execution_package_lock)),
                "frontend_extensions": copy.deepcopy(list(self.frontend_extensions)),
                "package_manifests": copy.deepcopy(list(self.package_manifests))}


def _execution_package_lock(manifests: list[dict]) -> tuple[dict, ...]:
    """Exclude UI-only exports, retaining exact dependencies of execution packages.

    Empty packages remain conservative execution requirements: their dependency
    declarations can compose business capabilities without registering exports.
    """
    by_id = {manifest["package_id"]: manifest for manifest in manifests}
    required = set()

    def retain(package_id):
        if package_id in required:
            return
        required.add(package_id)
        for dependency in by_id[package_id]["dependencies"]:
            retain(dependency["package_id"])

    for manifest in manifests:
        exports = manifest["exports"]
        pure_ui = bool(exports.get("frontend_extensions")) and not any(
            declarations for kind, declarations in exports.items() if kind != "frontend_extensions")
        if not pure_ui:
            retain(manifest["package_id"])
    return tuple({"package_id": package_id, "version": by_id[package_id]["version"]}
                 for package_id in sorted(required))


class CapabilityPackageLoader:
    def __init__(self, packages: Iterable[CapabilityPackage], *, base_registry: NodeRegistry | None = None):
        self._packages: dict[tuple[str, str], CapabilityPackage] = {}
        for package in packages:
            ensure(isinstance(package, CapabilityPackage) and isinstance(package.manifest, PackageManifest)
                   and callable(package.register),
                   "package_invalid_implementation", "Trusted package must provide a manifest and registration callable")
            manifest = PackageManifest.from_dict(package.manifest.to_dict())
            key = manifest.package_id, manifest.version
            ensure(key not in self._packages, "package_duplicate_install", "Package version is installed more than once")
            self._packages[key] = CapabilityPackage(manifest, package.register)
        ensure(len(self._packages) <= 256, "package_limit", "Installed package count exceeds its limit")
        self._base_registry = (base_registry or NodeRegistry()).detached()
        self._type_contracts = {
            (item["scope"], item["type_id"], item["schema_version"]): canonical_bytes(item)
            for item in self._base_registry.data_types.catalog()
        }
        self._node_contracts = {
            (item["component_id"], item["component_version"]): canonical_bytes(item)
            for item in self._base_registry.catalog()
        }
        self._executor_contracts = self._hosting_contracts(self._base_registry)
        self._service_contracts = self._services_contracts(self._base_registry)
        self._information_contracts = self._information_sources_contracts(self._base_registry)
        self._frontend_contracts: dict[tuple[str, str, str], bytes] = {}

    @staticmethod
    def _information_sources_contracts(registry: NodeRegistry) -> dict:
        return {(item["source_ref"]["source_id"], item["source_ref"]["exact_version"]): canonical_bytes(item)
                for item in registry.information_sources.catalog()}

    @staticmethod
    def _services_contracts(registry: NodeRegistry) -> dict:
        return {(item["service_ref"]["service_id"], item["service_ref"]["exact_version"]): canonical_bytes(item)
                for item in registry.services.catalog()}

    @staticmethod
    def _hosting_contracts(registry: NodeRegistry) -> dict:
        result = {}
        for kind, catalog in (("executor", registry.executors.catalog()),
                              ("pause", registry.executors.pause_catalog())):
            for item in catalog:
                reference = item["executor_ref"]
                result[(kind, reference["executor_id"], reference["exact_version"])] = canonical_bytes(item)
        return result

    def resolve(self, enabled: dict[str, str] | None = None) -> ResolvedPackageSelection:
        """Resolve exact declarations without staging or invoking a package."""
        if enabled is None:
            enabled = {}
            for package_id, version in sorted(self._packages):
                ensure(package_id not in enabled, "package_version_required",
                       "Multiple installed versions require an explicit project version")
                enabled[package_id] = version
        validate_json_value(enabled)
        ensure(type(enabled) is dict and len(enabled) <= 256
               and all(bounded_name(identity) and bounded_name(version) for identity, version in enabled.items()),
               "package_invalid_selection", "Enabled packages require a bounded map of exact versions")
        selected: dict[str, str] = {}
        visiting: set[str] = set()
        order: list[CapabilityPackage] = []

        def visit(package_id: str, version: str) -> None:
            ensure(package_id not in visiting, "package_dependency_cycle", "Package dependencies form a cycle")
            ensure(package_id not in selected or selected[package_id] == version,
                   "package_dependency_conflict", "Dependencies require incompatible exact versions")
            if package_id in selected:
                return
            package = self._packages.get((package_id, version))
            ensure(package is not None, "package_missing_dependency", f"Required package is unavailable: {package_id}@{version}")
            ensure(package.manifest.host_protocol_version == HOST_PROTOCOL_VERSION,
                   "package_protocol_mismatch", "Package host protocol is not supported")
            visiting.add(package_id)
            for dependency in sorted(package.manifest.dependencies, key=lambda item: item.package_id):
                ensure(dependency.package_id not in enabled or enabled[dependency.package_id] == dependency.version,
                       "package_dependency_conflict", "Enabled dependency version conflicts with its requirement")
                visit(dependency.package_id, dependency.version)
            visiting.remove(package_id)
            selected[package_id] = version
            order.append(package)

        for package_id, version in sorted(enabled.items()):
            visit(package_id, version)
        lock = tuple({"package_id": identity, "version": version} for identity, version in sorted(selected.items()))
        return ResolvedPackageSelection(
            copy.deepcopy(lock),
            tuple(package.manifest.to_dict() for package in order),
            tuple(PackageDependency(package.manifest.package_id, package.manifest.version)
                  for package in order),
        )

    def load(self, enabled: dict[str, str] | None = None) -> LoadedCapabilities:
        resolved = self.resolve(enabled)
        staged = self._base_registry.detached()
        frontend_extensions: dict[str, dict] = {}
        manifests: list[dict] = []
        for identity in resolved.registration_order:
            package = self._packages[(identity.package_id, identity.version)]
            registration = HostRegistration(staged, package.manifest, frontend_extensions)
            try:
                package.register(registration)
                exports = registration.finish()
            except Exception as exc:
                if isinstance(exc, HostContractError):
                    raise
                raise HostContractError("package_registration_failed",
                                        f"Package registration failed: {package.manifest.package_id}") from exc
            manifest = package.manifest.to_dict()
            manifest["exports"] = exports
            manifests.append(manifest)
        lock = resolved.package_lock
        type_contracts = {
            (item["scope"], item["type_id"], item["schema_version"]): canonical_bytes(item)
            for item in staged.data_types.catalog()
        }
        node_contracts = {
            (item["component_id"], item["component_version"]): canonical_bytes(item)
            for item in staged.catalog()
        }
        executor_contracts = self._hosting_contracts(staged)
        service_contracts = self._services_contracts(staged)
        information_contracts = self._information_sources_contracts(staged)
        from .frontend_extension_contracts import validate_frontend_targets
        validate_frontend_targets(staged, frontend_extensions.values())
        frontend_contracts = {
            (item["package_id"], item["package_version"], item["extension_id"]): canonical_bytes(item)
            for item in frontend_extensions.values() if item.get("schema_version") == 2
        }
        ensure(all(staged.get(item["component_id"], item["component_version"]) is not None
                   for item in staged.information_sources.catalog()),
               "information_missing_node_type", "Information source targets an unavailable exact node type")
        ensure(all(key not in self._type_contracts or self._type_contracts[key] == value
                   for key, value in type_contracts.items())
               and all(key not in self._node_contracts or self._node_contracts[key] == value
                       for key, value in node_contracts.items())
               and all(key not in self._executor_contracts or self._executor_contracts[key] == value
                       for key, value in executor_contracts.items())
               and all(key not in self._service_contracts or self._service_contracts[key] == value
                       for key, value in service_contracts.items())
               and all(key not in self._information_contracts or self._information_contracts[key] == value
                       for key, value in information_contracts.items())
               and all(key not in self._frontend_contracts or self._frontend_contracts[key] == value
                       for key, value in frontend_contracts.items()),
               "package_contract_redefined", "Upgrades must use new type or component versions when contracts change")
        self._type_contracts.update(type_contracts)
        self._node_contracts.update(node_contracts)
        self._executor_contracts.update(executor_contracts)
        self._service_contracts.update(service_contracts)
        self._information_contracts.update(information_contracts)
        self._frontend_contracts.update(frontend_contracts)
        staged.package_lock = copy.deepcopy(lock)
        staged.execution_package_lock = _execution_package_lock(manifests)
        return LoadedCapabilities(staged.detached(frozen=True), copy.deepcopy(lock),
                                  tuple(copy.deepcopy(frontend_extensions[key]) for key in sorted(frontend_extensions)),
                                  tuple(copy.deepcopy(manifests)))


def discover_trusted_packages(entrypoints: Iterable[str]) -> tuple[CapabilityPackage, ...]:
    """Import only administrator-selected local ``module:attribute`` entries."""
    packages: list[CapabilityPackage] = []
    for entrypoint in entrypoints:
        ensure(type(entrypoint) is str and len(entrypoint) <= 512
               and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", entrypoint),
               "package_invalid_entrypoint", "Trusted package entry must use module:attribute")
        module_name, attribute = entrypoint.split(":")
        try:
            value = importlib.import_module(module_name)
            for part in attribute.split("."):
                value = getattr(value, part)
            package = value() if callable(value) and not isinstance(value, CapabilityPackage) else value
        except Exception as exc:
            raise HostContractError("package_discovery_failed", "Trusted local package entry could not be loaded") from exc
        ensure(isinstance(package, CapabilityPackage), "package_invalid_implementation",
               "Trusted local entry did not return a capability package")
        packages.append(package)
        ensure(len(packages) <= 256, "package_limit", "Trusted package entry count exceeds its limit")
    return tuple(packages)
