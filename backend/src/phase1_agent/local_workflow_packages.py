"""Read declared local workflow packages; import only selected package code."""

from __future__ import annotations

from copy import deepcopy
import importlib
from pathlib import Path
import re
import sys
from threading import RLock
import types
from uuid import uuid4

from .capability_packages import CapabilityPackage, PackageManifest
from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, loads_strict
from .host_sdk import HostContractError


DECLARATION_FILE = "openagent-package.json"
_ENTRYPOINT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*",
                         re.ASCII)


def _diagnostic(code, message, source, stage, manifest=None, **details):
    result = {"reason_code": code, "message": message, "source": str(source), "stage": stage}
    if manifest is not None:
        result.update(package_id=manifest.package_id, version=manifest.version)
    result.update(details)
    return result


def _error(diagnostic):
    error = HostContractError(diagnostic["reason_code"], diagnostic["message"])
    error.package_diagnostic = deepcopy(diagnostic)
    error.diagnostics = [deepcopy(diagnostic)]
    return error


class _DeclaredPackage:
    def __init__(self, directory, manifest, entrypoint, diagnostic=None):
        self.directory = directory
        self.manifest = manifest
        self.entrypoint = entrypoint
        self.diagnostic = diagnostic
        self._namespace = "_openagent_local_" + uuid4().hex
        self._implementation = None
        self._lock = RLock()

    def _fail(self, code, message, **details):
        return _error(_diagnostic(
            code, message, self.directory / DECLARATION_FILE, "import", self.manifest, **details))

    def _check_module_path(self, module_name):
        # Check ancestors before any __init__.py executes, including symlink targets.
        parts = module_name.split(".")
        for length in range(1, len(parts) + 1):
            path = self.directory.joinpath(*parts[:length])
            candidates = ((path / "__init__.py", path.with_suffix(".py"))
                          if length == len(parts) else (path / "__init__.py",))
            for candidate in candidates:
                if candidate.exists() and not candidate.resolve().is_relative_to(self.directory):
                    raise self._fail("package_invalid_entrypoint", "Package entry escapes its directory")
            if path.is_dir() and not path.resolve().is_relative_to(self.directory):
                raise self._fail("package_invalid_entrypoint", "Package entry escapes its directory")

    def _load(self):
        if self.diagnostic is not None:
            raise _error(self.diagnostic)
        module_name, attribute = self.entrypoint.split(":")
        self._check_module_path(module_name)
        namespace = types.ModuleType(self._namespace)
        namespace.__path__ = [str(self.directory)]
        namespace.__package__ = self._namespace
        sys.modules[self._namespace] = namespace
        complete = False
        try:
            value = importlib.import_module(self._namespace + "." + module_name)
            for part in attribute.split("."):
                value = getattr(value, part)
            package = value() if callable(value) and not isinstance(value, CapabilityPackage) else value
            if not isinstance(package, CapabilityPackage) or not isinstance(package.manifest, PackageManifest) \
                    or not callable(package.register):
                raise self._fail("package_invalid_implementation",
                                 "Local package entry must return a capability package")
            if canonical_bytes(package.manifest.to_dict()) != canonical_bytes(self.manifest.to_dict()):
                raise self._fail("package_manifest_mismatch",
                                 "Local package implementation differs from its declared manifest")
            complete = True
            return package
        except HostContractError as exc:
            if hasattr(exc, "package_diagnostic"):
                raise
            raise self._fail(exc.reason_code, "Local package entry failed contract validation") from exc
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith(self._namespace + "."):
                raise self._fail("package_discovery_failed", "Package-local Python module is unavailable",
                                 dependency=exc.name[len(self._namespace) + 1:]) from exc
            raise self._fail("package_discovery_failed", "Local package Python dependency is unavailable",
                             dependency=exc.name) from exc
        except Exception as exc:
            raise self._fail("package_discovery_failed", "Local package entry could not be loaded") from exc
        finally:
            if not complete:
                for name in tuple(sys.modules):
                    if name == self._namespace or name.startswith(self._namespace + "."):
                        del sys.modules[name]

    def register(self, registration):
        with self._lock:
            if self._implementation is None:
                self._implementation = self._load()
            package = self._implementation
        try:
            package.register(registration)
        except HostContractError as exc:
            diagnostic = _diagnostic(
                exc.reason_code, "Local package registration failed contract validation",
                self.directory / DECLARATION_FILE, "registration", self.manifest)
            raise _error(diagnostic) from exc
        except Exception as exc:
            diagnostic = _diagnostic("package_registration_failed", "Local package registration failed",
                                     self.directory / DECLARATION_FILE, "registration", self.manifest)
            raise _error(diagnostic) from exc


class LocalWorkflowPackageSource:
    """One discovery snapshot, with exact declarations and lazy trusted imports."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._diagnostics = []
        self._catalog = []
        self._package_sources = {}
        packages = []
        identities = {}
        if not self.root.exists():
            self.packages = ()
            return
        if not self.root.is_dir():
            diagnostic = _diagnostic("package_invalid_source", "Workflow package root is not a directory",
                                     self.root, "discovery")
            self._diagnostics.append(diagnostic)
            self.packages = ()
            return
        try:
            directories = sorted((path for path in self.root.iterdir() if path.is_dir()),
                                 key=lambda path: path.name)
        except OSError as exc:
            raise _error(_diagnostic("package_discovery_failed", "Workflow package root cannot be read",
                                    self.root, "discovery")) from exc
        for directory in directories:
            source = directory / DECLARATION_FILE
            manifest = None
            entrypoint = None
            try:
                declaration = loads_strict(source.read_text(encoding="utf-8"))
                if type(declaration) is not dict or set(declaration) != {
                    "schema_version", "manifest", "entrypoint",
                } or type(declaration["schema_version"]) is not int or declaration["schema_version"] != 1:
                    raise HostContractError("package_invalid_declaration", "Local package declaration is invalid")
                manifest = PackageManifest.from_dict(declaration["manifest"])
                identity = manifest.package_id, manifest.version
                if identity in identities:
                    raise _error(_diagnostic("package_duplicate_install", "Local package identity is duplicated",
                                            source, "discovery", manifest,
                                            conflicting_source=str(identities[identity])))
                identities[identity] = source
                entrypoint = declaration["entrypoint"]
                diagnostic = None
                if type(entrypoint) is not str or len(entrypoint) > 512 \
                        or _ENTRYPOINT.fullmatch(entrypoint) is None:
                    diagnostic = _diagnostic("package_invalid_entrypoint",
                                             "Local package entry must use a package-local module:attribute",
                                             source, "declaration", manifest)
                declared = _DeclaredPackage(directory.resolve(), manifest, entrypoint, diagnostic)
                packages.append(CapabilityPackage(manifest, declared.register))
                self._package_sources[identity] = source
                if diagnostic is not None:
                    self._diagnostics.append(diagnostic)
                self._catalog.append({"source": str(source), "entrypoint": entrypoint,
                                      "manifest": manifest.to_dict(), "available": diagnostic is None})
            except (OSError, UnicodeError, ContractValidationError) as exc:
                if getattr(exc, "reason_code", None) == "package_duplicate_install":
                    raise
                diagnostic = _diagnostic(getattr(exc, "reason_code", "package_invalid_declaration"),
                                         "Local package declaration cannot be read or validated",
                                         source, "declaration", manifest)
                self._diagnostics.append(diagnostic)
                self._catalog.append({"source": str(source), "entrypoint": entrypoint,
                                      "manifest": manifest.to_dict() if manifest else None,
                                      "available": False, "diagnostic": diagnostic})
        self.packages = tuple(packages)

    @property
    def diagnostics(self):
        return tuple(deepcopy(self._diagnostics))

    def catalog(self):
        return deepcopy(self._catalog)

    def require_no_conflicts(self, existing_packages):
        identities = {(package.manifest.package_id, package.manifest.version) for package in existing_packages}
        for package in self.packages:
            identity = package.manifest.package_id, package.manifest.version
            if identity in identities:
                raise _error(_diagnostic("package_duplicate_install", "Local package identity conflicts with an existing source",
                                        self._package_sources[identity], "discovery", package.manifest,
                                        conflicting_source="existing"))
