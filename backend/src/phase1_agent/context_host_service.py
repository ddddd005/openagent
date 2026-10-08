"""Native-context validation through the registered public host service."""

from .content_contracts import object_schema
from .graph_records import require
from .host_sdk import ServiceDefinition, ServiceOperation, ServiceReference

NATIVE_CONTEXT_SERVICE_REF = ServiceReference("workflow.context", "3.0.0")
_VALIDATE_SCHEMA = object_schema({
    "operation": {"type": "string"}, "view": {"type": "object"}, "view_ref": {"type": "object"},
    "context": {"type": "object"}, "context_ref": {"type": "object"},
}, required=["operation", "view", "view_ref"])
_VALIDATE_SCHEMA["dependentRequired"] = {"context": ["context_ref"], "context_ref": ["context"]}
NATIVE_CONTEXT_SERVICE_DEFINITION = ServiceDefinition(NATIVE_CONTEXT_SERVICE_REF, (
    ServiceOperation("context:read", "read-context", object_schema({"object_key": {"type": "string"}}),
                     object_schema({"object": {"type": "object"}, "view": {"type": ["object", "null"]}})),
    ServiceOperation("context:validate", "validate-context", _VALIDATE_SCHEMA,
                     object_schema({"validated": {"const": True}})),
))


def native_service_requirement(capability, *operations):
    return {**NATIVE_CONTEXT_SERVICE_REF.to_dict(), "capability": capability, "operations": list(operations)}


class NativeContextHostService:
    def __init__(self, environment):
        self.environment = environment

    def __call__(self, context, capability, operation, payload):
        if capability == "context:read" and operation == "read-context":
            require(type(payload) is dict and set(payload) == {"object_key"},
                    "context_invalid_read", "Context read requires its exact object key")
            item = context.object_read(payload["object_key"])
            reference = item["value"]["view_ref"]
            view = None if reference is None else self.environment.resolve_artifact(context, reference)["value"]
            return {"object": item, "view": view}
        require(capability == "context:validate" and operation == "validate-context",
                "host_service_operation_denied", "Native context operation is not declared")
        from .context_v4 import validate_native_context_artifacts
        view = payload["view"]
        origin = self.environment.resolve_input(context, "view")
        require(payload["view_ref"]["output_id"] == origin["output_id"] and origin["value"] == view,
                "context_input_mismatch", "Native context must equal its exact accepted input", 409)
        require(view["owner"]["workflow_session_id"] == context.workflow_session_id,
                "context_owner_mismatch", "Native context belongs to another session", 403)
        if "context" in payload:
            update_origin = self.environment.resolve_input(context, "context")
            require(payload["context_ref"]["output_id"] == update_origin["output_id"]
                    and payload["context"] == update_origin["value"]
                    and payload["context"]["owner"] == update_origin["producer"],
                    "context_delta_owner_mismatch", "Update must equal its exact accepted Agent producer", 409)
        validate_native_context_artifacts(
            payload, lambda reference: self.environment.resolve_artifact(context, reference)["value"])
        return {"validated": True}


def create_native_context_host_service(environment):
    return NativeContextHostService(environment)
