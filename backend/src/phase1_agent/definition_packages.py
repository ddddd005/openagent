"""Workflow-side package loading request over the existing trusted loader.

This is an adapter, not a second loader.  A caller supplies an exact package
selection; the source delegates dependency resolution, staging and registration
to ``CapabilityPackageLoader`` and returns its loaded capabilities or its
original diagnostic.  The local source is replaceable when project-level
package management arrives; definition registration and consumers do not
depend on how the trusted package set is supplied.
"""

from __future__ import annotations

from .capability_packages import CapabilityPackageLoader, LoadedCapabilities
from .host_sdk import bounded_name, ensure


class TrustedLocalPackageSource:
    """Explicit local package collection; no directory scan or implicit default set."""

    def __init__(self, packages, *, base_registry=None):
        self._loader = CapabilityPackageLoader(packages, base_registry=base_registry)

    def resolve(self, requirements):
        return self._loader.resolve(requirements)

    def load(self, requirements):
        return self._loader.load(requirements)


class WorkflowDefinitionPackages:
    """Accept an exact workflow package requirement and return this load's content."""

    def __init__(self, source):
        ensure(hasattr(source, "load") and callable(source.load),
               "definition_invalid_package_source",
               "Definition package source must provide a load operation")
        self._source = source

    def resolve(self, requirements):
        return self._source.resolve(requirements)

    def load(self, requirements) -> LoadedCapabilities:
        ensure(type(requirements) is dict
               and len(requirements) <= 256
               and all(bounded_name(package_id) and bounded_name(version)
                       for package_id, version in requirements.items()),
               "definition_invalid_package_request",
               "Definition loading requires a bounded map of exact package versions")
        return self._source.load(dict(requirements))
