"""Native context read, assembly and verified CAS settlement."""

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .context_contract import artifact_ref
from .graph_contracts import require

CONTEXT_PACKAGE_ID = "workflow.context"


def _exact_input(context, port):
    refs = context.input_artifact_refs(port)
    require(len(refs) == 1, "context_exact_artifact_required",
            "Context nodes require exactly one accepted artifact per input")
    return artifact_ref({"scope": "artifact", "output_id": refs[0]["output_id"]})


def create_context_package():
    def register(host):
        from .context_host_service import NATIVE_CONTEXT_SERVICE_DEFINITION, create_native_context_host_service
        from .context_receipt import validate_context_receipt
        from .context_contract import preserve_artifact_references
        from .context_v4_nodes import register_native_context
        from .host_sdk import DataTypeDefinition
        host.register_service(NATIVE_CONTEXT_SERVICE_DEFINITION, create_native_context_host_service)
        host.register_data_type(DataTypeDefinition(
            "CONTEXT_COMMIT", 2, {"type": "object"}, scope="content",
            validator=validate_context_receipt, references=lambda value: [value["view_ref"]],
            reference_mapper=preserve_artifact_references, max_bytes=4_000_000))
        register_native_context(host)

    from .context_host_service import NATIVE_CONTEXT_SERVICE_REF
    return CapabilityPackage(PackageManifest(
        CONTEXT_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.content", "1.0.0"),
                                    PackageDependency("workflow.prompts", "1.0.0")),
        exports={"services": [NATIVE_CONTEXT_SERVICE_REF.to_dict()]},
        schema_version=3), register)
