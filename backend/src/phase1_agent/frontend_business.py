"""Business registration for the current logical-chat frontend node."""

from copy import deepcopy

from .frontend_contracts import (
    FRONTEND_COMMIT_TYPE, preserve_frontend_references, validate_frontend_commit,
)
from .host_sdk import DataTypeDefinition


def register_frontend_business(host):
    host.register_data_type(DataTypeDefinition(
        FRONTEND_COMMIT_TYPE, 1, {"type": "object"}, scope="content",
        validator=validate_frontend_commit,
        references=lambda value: [deepcopy(value["view_ref"])],
        reference_mapper=preserve_frontend_references))
    from .frontend_node_v2 import register_frontend_chat_node
    register_frontend_chat_node(host)


def create_frontend_business_package():
    from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
    return CapabilityPackage(PackageManifest(
        "workflow.frontend-business", "1.0.0",
        dependencies=(PackageDependency("workflow.content", "1.0.0"),)), register_frontend_business)
