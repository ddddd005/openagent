"""Complete frontend package: business dependency and local UI declarations."""

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .frontend_contracts import FRONTEND_DISPLAY_TYPE, FRONTEND_STATE_TYPE


FRONTEND_EXTENSIONS = (
    {"extension_id": "workflow.frontend.workbench-panel", "kind": "workbench-panel",
     "entrypoint": "workflow.frontend.workbench.panel", "component_id": None, "component_version": None,
     "binding": {"surface": "workbench", "slot": "panel", "target": {}}},
    {"extension_id": "workflow.frontend.session-object", "kind": "renderer",
     "entrypoint": "workflow.frontend.workbench.session-object", "component_id": None, "component_version": None,
     "binding": {"surface": "workbench", "slot": "session-object",
                 "target": {"scope": "session", "type_id": FRONTEND_STATE_TYPE, "schema_version": 1}}},
    {"extension_id": "workflow.frontend.node-fields", "kind": "field-editor",
     "entrypoint": "workflow.frontend.workbench.node-fields",
     "component_id": "frontend.state.append", "component_version": "1",
     "binding": {"surface": "workbench", "slot": "node-fields",
                 "target": {"component_id": "frontend.state.append", "component_version": "1"}}},
    {"extension_id": "workflow.frontend.public-output", "kind": "consumer",
     "entrypoint": "workflow.frontend.consumer.public-output", "component_id": None, "component_version": None,
     "binding": {"surface": "consumer", "slot": "public-output",
                 "target": {"scope": "content", "type_id": FRONTEND_DISPLAY_TYPE, "schema_version": 1}}},
)


def register_frontend(host):
    for extension in FRONTEND_EXTENSIONS:
        host.register_frontend_extension(**extension, host_protocol_version=1)


def create_frontend_package():
    return CapabilityPackage(PackageManifest(
        "workflow.frontend", "1.0.0",
        dependencies=(PackageDependency("workflow.frontend-business", "1.0.0"),),
        exports={"frontend_extensions": [{"extension_id": row["extension_id"]} for row in FRONTEND_EXTENSIONS]},
    ), register_frontend)
