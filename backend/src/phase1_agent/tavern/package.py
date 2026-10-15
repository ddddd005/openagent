"""Lorebook packages retaining their existing exact selection identities."""

from copy import deepcopy

from ..capability_packages import CapabilityPackage, PackageManifest
from ..tavern_package import TAVERN_FRONTEND_EXTENSIONS, create_tavern_package, register_inline_lorebook
from .lorebook_resources import (
    GLOBAL_LOREBOOK_COMPONENTS, LOREBOOK_RESOURCE_TYPE, register_global_lorebook,
)
TAVERN_PACKAGE_VERSION = "1.1.0"
CURRENT_TAVERN_PACKAGE_VERSION = "1.2.0"
GLOBAL_TAVERN_FRONTEND_EXTENSIONS = (
    *({
        **deepcopy(row), "extension_id": row["extension_id"] + "-v1-1",
        "entrypoint": row["entrypoint"] + "-v1-1",
    } for row in TAVERN_FRONTEND_EXTENSIONS),
    {
        "extension_id": "workflow.tavern.lorebook-resources", "kind": "workbench-panel",
        "entrypoint": "workflow.tavern.workbench.lorebook-resources",
        "binding": {"surface": "workbench", "slot": "panel", "target": {}},
    },
    *({
        "extension_id": f"workflow.tavern.{name}-fields", "kind": "field-editor",
        "entrypoint": f"workflow.tavern.workbench.{name}-fields",
        "component_id": f"lorebook.{name}", "component_version": "1",
        "binding": {
            "surface": "workbench", "slot": "node-fields",
            "target": {"component_id": f"lorebook.{name}", "component_version": "1"},
        },
    } for name in ("global-reference", "global-activate")),
)
CURRENT_TAVERN_FRONTEND_EXTENSIONS = (
    *({
        **deepcopy(row), "extension_id": row["extension_id"] + "-v1-2",
        "entrypoint": row["entrypoint"] + "-v1-2",
    } for row in GLOBAL_TAVERN_FRONTEND_EXTENSIONS),
)


def create_global_tavern_package():
    original = create_tavern_package()
    exports = deepcopy(original.manifest.exports)
    exports["data_types"] = [
        {"scope": "global", "type_id": LOREBOOK_RESOURCE_TYPE, "schema_version": 1}]
    exports["nodes"] += [
        {"component_id": identity, "component_version": "1"} for identity in GLOBAL_LOREBOOK_COMPONENTS]
    exports["frontend_extensions"] = [
        {"extension_id": row["extension_id"]} for row in GLOBAL_TAVERN_FRONTEND_EXTENSIONS]

    def register(host):
        register_inline_lorebook(host)
        register_global_lorebook(host)
        for extension in GLOBAL_TAVERN_FRONTEND_EXTENSIONS:
            protocol = 2 if extension["binding"]["slot"] == "node-fields" else 1
            host.register_frontend_extension(**extension, host_protocol_version=protocol)

    return CapabilityPackage(PackageManifest(
        original.manifest.package_id, TAVERN_PACKAGE_VERSION,
        dependencies=original.manifest.dependencies, exports=exports,
    ), register)


def create_current_tavern_package():
    original = create_global_tavern_package()
    exports = deepcopy(original.manifest.exports)
    exports["frontend_extensions"] = [
        {"extension_id": row["extension_id"]} for row in CURRENT_TAVERN_FRONTEND_EXTENSIONS]

    def register(host):
        register_inline_lorebook(host)
        register_global_lorebook(host)
        for extension in CURRENT_TAVERN_FRONTEND_EXTENSIONS:
            protocol = 2 if extension["binding"]["slot"] == "node-fields" else 1
            host.register_frontend_extension(**extension, host_protocol_version=protocol)

    return CapabilityPackage(PackageManifest(
        original.manifest.package_id, CURRENT_TAVERN_PACKAGE_VERSION,
        dependencies=original.manifest.dependencies, exports=exports,
    ), register)
