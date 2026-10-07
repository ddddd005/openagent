"""Current trusted package composition through the public registry API."""

from copy import deepcopy

from .capability_packages import CapabilityPackageLoader
from .builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages


def create_package_registry(*, packages=(), enabled: dict[str, str] | None = None):
    selected = dict(DEFAULT_PACKAGES) if enabled is None else deepcopy(enabled)
    supplied = tuple(packages)
    identities = {(package.manifest.package_id, package.manifest.version) for package in supplied}
    installed = tuple(package for package in builtin_capability_packages()
                      if (package.manifest.package_id, package.manifest.version) not in identities)
    return CapabilityPackageLoader((*installed, *supplied)).load(selected)
