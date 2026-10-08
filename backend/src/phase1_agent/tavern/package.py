"""Additive tavern version; the original exact inline package stays available."""

from copy import deepcopy

from ..capability_packages import CapabilityPackage, PackageManifest
from ..tavern_package import TAVERN_FRONTEND_EXTENSIONS, create_tavern_package, register_inline_lorebook
from .lorebook_resources import (
    GLOBAL_LOREBOOK_COMPONENTS, LOREBOOK_RESOURCE_TYPE, register_global_lorebook,
)
from .chat_contracts import (
    TAVERN_CHAT_COMMIT_TYPE, TAVERN_CHAT_DISPLAY_TYPE, TAVERN_CHAT_STATE_TYPE, TAVERN_CHAT_VIEW_TYPE,
)
from .chat_nodes import TAVERN_CHAT_COMPONENTS, register_tavern_chat


TAVERN_PACKAGE_VERSION = "1.1.0"
CHAT_TAVERN_PACKAGE_VERSION = "1.2.0"
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
CHAT_TAVERN_FRONTEND_EXTENSIONS = (
    *({
        **deepcopy(row), "extension_id": row["extension_id"] + "-v1-2",
        "entrypoint": row["entrypoint"] + "-v1-2",
    } for row in GLOBAL_TAVERN_FRONTEND_EXTENSIONS),
    {
        "extension_id": "workflow.tavern.chat-launch", "kind": "workbench-panel",
        "entrypoint": "workflow.tavern.workbench.chat-launch",
        "binding": {"surface": "workbench", "slot": "panel", "target": {}},
    },
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
            host.register_frontend_extension(**extension, host_protocol_version=1)

    return CapabilityPackage(PackageManifest(
        original.manifest.package_id, TAVERN_PACKAGE_VERSION,
        dependencies=original.manifest.dependencies, exports=exports,
    ), register)


def create_chat_tavern_package():
    original = create_global_tavern_package()
    exports = deepcopy(original.manifest.exports)
    exports["data_types"] += [
        {"scope": scope, "type_id": type_id, "schema_version": 1} for scope, type_id in (
            ("session", TAVERN_CHAT_STATE_TYPE), ("content", TAVERN_CHAT_VIEW_TYPE),
            ("content", TAVERN_CHAT_COMMIT_TYPE), ("content", TAVERN_CHAT_DISPLAY_TYPE),
        )
    ]
    exports["nodes"] += [
        {"component_id": identity, "component_version": "1"} for identity in TAVERN_CHAT_COMPONENTS]
    exports["frontend_extensions"] = [
        {"extension_id": row["extension_id"]} for row in CHAT_TAVERN_FRONTEND_EXTENSIONS]

    def register(host):
        register_inline_lorebook(host)
        register_global_lorebook(host)
        register_tavern_chat(host)
        for extension in CHAT_TAVERN_FRONTEND_EXTENSIONS:
            host.register_frontend_extension(**extension, host_protocol_version=1)

    return CapabilityPackage(PackageManifest(
        original.manifest.package_id, CHAT_TAVERN_PACKAGE_VERSION,
        dependencies=original.manifest.dependencies, exports=exports,
    ), register)
