"""Real capability packages used by definition-stage loading tests.

The two normal samples are trusted local packages, not production defaults:
``sample.definition-types`` owns one session data type and
``sample.definition-node`` owns one TEXT@2 node.  The node explicitly depends
on the exact ``workflow.content`` package that registers the TEXT@2 envelope,
so loading it only for the node requirement must resolve and register the
content package first.  Loading intentionally leaves all dependency
resolution, staging, registration and export checking to
``CapabilityPackageLoader``; a test must not register these definitions
directly on a final registry.

``partial_registration_package`` is a separate failure sample.  It registers
one type and then raises, so callers can prove that a failed batch is not
published.  It is never part of ``definition_stage_packages``.

``definition_stage_packages`` intentionally returns only the two sample
packages; a caller such as the definition-package tests must install
``content_contracts.create_content_package()`` in the loader package set so
the declared ``workflow.content`` dependency can be resolved.
"""

from __future__ import annotations

from phase1_agent.capability_packages import (
    CapabilityPackage, PackageDependency, PackageManifest,
)
from phase1_agent.content_contracts import text_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.host_sdk import DataTypeDefinition


TYPE_REFERENCE = {"scope": "session", "type_id": "sample.payload", "schema_version": 1}
NODE_REFERENCE = {"component_id": "sample.uppercase", "component_version": "1"}


def _upper_executor(config, inputs, context):
    return {"output": text_content(inputs["input"]["text"].upper())}


def _register_definition_types(host):
    host.register_data_type(DataTypeDefinition("sample.payload", 1, {"type": "integer"}, 0))


def _register_definition_node(host):
    host.register_node(NodeDefinition(
        "sample.uppercase", "1", "Upper sample", "Sample", {},
        {"type": "object", "properties": {}, "additionalProperties": False},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        is_output=True,
    ), _upper_executor)


def definition_types_package():
    """Package that registers exactly one session data type."""
    return CapabilityPackage(
        PackageManifest(
            "sample.definition-types", "1", schema_version=1,
            exports={"data_types": [dict(TYPE_REFERENCE)]},
        ),
        _register_definition_types,
    )


def definition_node_package():
    """Package whose only node depends on the exact type and content packages."""
    return CapabilityPackage(
        PackageManifest(
            "sample.definition-node", "1", schema_version=1,
            dependencies=(
                PackageDependency("sample.definition-types", "1"),
                PackageDependency("workflow.content", "1.0.0"),
            ),
            exports={"nodes": [dict(NODE_REFERENCE)]},
        ),
        _register_definition_node,
    )


def definition_stage_packages():
    """The two real samples; callers install ``workflow.content`` alongside them."""
    return (definition_types_package(), definition_node_package())


def partial_registration_package():
    """Registers one type and then aborts for staging-isolation checks."""
    def register(host):
        host.register_data_type(DataTypeDefinition("sample.partial", 1, {"type": "string"}, ""))
        raise RuntimeError("definition-stage fixture registration aborted")

    return CapabilityPackage(
        PackageManifest(
            "sample.definition-partial", "1", schema_version=1,
            exports={"data_types": [
                {"scope": "session", "type_id": "sample.partial", "schema_version": 1},
            ]},
        ),
        register,
    )
