"""Context-package validation behind the generic, registered host service."""

from .contract_json import canonical_bytes
from .graph_records import require
from .host_sdk import ServiceDefinition, ServiceOperation, ServiceReference
from .content_contracts import object_schema


CONTEXT_SERVICE_REF = ServiceReference("workflow.context", "1.0.0")
_VALIDATE_SCHEMA = object_schema({
    "operation": {"type": "string"}, "view": {"type": "object"}, "view_ref": {"type": "object"},
    "delta": {"type": "object"}, "delta_ref": {"type": "object"},
    "context": {"type": "object"}, "context_ref": {"type": "object"},
}, required=["operation", "view", "view_ref"])
_VALIDATE_SCHEMA["dependentRequired"] = {"delta": ["delta_ref"], "delta_ref": ["delta"],
                                       "context": ["context_ref"], "context_ref": ["context"]}
CONTEXT_SERVICE_DEFINITION = ServiceDefinition(CONTEXT_SERVICE_REF, (
    ServiceOperation("context:read", "read-context", object_schema({"object_key": {"type": "string"}}),
                     object_schema({"object": {"type": "object"}, "view": {"type": ["object", "null"]}})),
    ServiceOperation("context:validate", "validate-context", _VALIDATE_SCHEMA,
                     object_schema({"validated": {"const": True}})),
))


def service_requirement(capability, *operations):
    return {**CONTEXT_SERVICE_REF.to_dict(), "capability": capability, "operations": list(operations)}


class ContextHostService:
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
                "host_service_operation_denied", "Context service operation is not declared")
        view = payload["view"]
        origin = self.environment.resolve_input(context, "view")
        require(payload["view_ref"]["output_id"] == origin["output_id"]
                and canonical_bytes(origin["value"]) == canonical_bytes(view),
                "context_input_mismatch", "Context view differs from the exact accepted input", 409)
        require(view["owner"]["workflow_session_id"] == context.workflow_session_id,
                "context_owner_mismatch", "Context view belongs to another session", 403)

        def resolve(reference):
            return self.environment.resolve_artifact(context, reference)["value"]

        if view.get("schema_version") == 2:
            from .context_v2 import validate_bound_context_artifacts
            if "context" in payload:
                round_origin = self.environment.resolve_input(context, "context")
                packet = payload["context"]
                require(payload["context_ref"]["output_id"] == round_origin["output_id"]
                        and canonical_bytes(round_origin["value"]) == canonical_bytes(packet)
                        and packet["owner"] == round_origin["producer"],
                        "context_delta_owner_mismatch", "Agent context differs from its exact accepted producer", 409)
            validate_bound_context_artifacts(payload, resolve)
        else:
            from .context_contract import validate_context_artifacts, validate_context_view
            validate_context_view(view)
            if payload.get("delta") is not None:
                delta_origin = self.environment.resolve_input(context, "delta")
                delta = payload["delta"]
                require(payload["delta_ref"]["output_id"] == delta_origin["output_id"]
                        and canonical_bytes(delta_origin["value"]) == canonical_bytes(delta)
                        and delta["owner"] == delta_origin["producer"],
                        "context_delta_owner_mismatch", "Delta differs from its exact accepted producer", 409)
            validate_context_artifacts(payload, resolve)
        return {"validated": True}


def create_context_host_service(environment):
    return ContextHostService(environment)


SUMMARY_CONTEXT_SERVICE_REF = ServiceReference("workflow.context", "2.0.0")
SUMMARY_CONTEXT_SERVICE_DEFINITION = ServiceDefinition(SUMMARY_CONTEXT_SERVICE_REF, (
    ServiceOperation("context:read", "read-context", object_schema({"object_key": {"type": "string"}}),
                     object_schema({"object": {"type": "object"}, "view": {"type": ["object", "null"]}})),
    ServiceOperation("context:validate", "validate-context", _VALIDATE_SCHEMA,
                     object_schema({"validated": {"const": True}})),
))


def summary_service_requirement(capability, *operations):
    return {**SUMMARY_CONTEXT_SERVICE_REF.to_dict(), "capability": capability, "operations": list(operations)}


class SummaryContextHostService(ContextHostService):
    def __call__(self, context, capability, operation, payload):
        if capability == "context:read":
            return super().__call__(context, capability, operation, payload)
        require(capability == "context:validate" and operation == "validate-context",
                "host_service_operation_denied", "Summary context service operation is not declared")
        from .context_v3 import validate_effective_view_artifacts
        view = payload["view"]
        origin = self.environment.resolve_input(context, "view")
        require(payload["view_ref"]["output_id"] == origin["output_id"] and origin["value"] == view,
                "context_input_mismatch", "Effective context must match the exact accepted input", 409)
        require(view["owner"]["workflow_session_id"] == context.workflow_session_id,
                "context_owner_mismatch", "Effective context belongs to another session", 403)
        if "context" in payload:
            round_origin = self.environment.resolve_input(context, "context")
            require(payload["context_ref"]["output_id"] == round_origin["output_id"]
                    and round_origin["value"] == payload["context"]
                    and round_origin["producer"] == payload["context"]["owner"],
                    "context_delta_owner_mismatch", "Agent round differs from its exact accepted producer", 409)
        validate_effective_view_artifacts(
            payload, lambda reference: self.environment.resolve_artifact(context, reference)["value"])
        return {"validated": True}


def create_summary_context_host_service(environment):
    return SummaryContextHostService(environment)
