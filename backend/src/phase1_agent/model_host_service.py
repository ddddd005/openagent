"""Public host boundary for reference-only bindings and per-request models."""

from uuid import uuid4

from .contract_json import content_digest
from .contract_errors import require
from .host_sdk import ServiceDefinition, ServiceOperation, ServiceReference
from .model_contract import MODEL_PROFILE_TYPE, validate_model_reference
from .model_request_contracts import (
    MODEL_REQUEST_SERVICE_REF, model_binding_schema, model_request_operations, object_schema,
    validate_model_binding,
)
from .model_request_service import ModelRequestService
from .model_request_transport import resolve_model_request_transport


MODEL_SERVICE_REF = MODEL_REQUEST_SERVICE_REF
_UUID_SCHEMA = {"type": "string", "pattern":
                "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}
_MODEL_REFERENCE_SCHEMA = object_schema({
    "envelope_version": {"const": 1}, "scope": {"const": "workspace"},
    "type_id": {"const": MODEL_PROFILE_TYPE}, "resource_id": _UUID_SCHEMA,
})
_OWNER_SCHEMA = object_schema({key: _UUID_SCHEMA for key in (
    "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")})
_FACT_REQUEST_SCHEMA = object_schema({
    "port": {"const": "receipts"},
    "producer": _OWNER_SCHEMA,
    "binding_ref": object_schema({"scope": {"const": "artifact"}, "output_id": _UUID_SCHEMA}),
    "requests": {"type": "array", "minItems": 1, "maxItems": 4096, "items": object_schema({
        "request_id": _UUID_SCHEMA, "settings_id": _UUID_SCHEMA,
        "fact_ids": {"type": "array", "minItems": 3, "maxItems": 3, "items": _UUID_SCHEMA},
    })},
})
_FACT_RESPONSE_SCHEMA = object_schema({
    "requests": {"type": "array", "items": object_schema({
        "request_id": _UUID_SCHEMA, "settings_id": _UUID_SCHEMA,
        "facts": {"type": "array", "minItems": 3, "maxItems": 3,
                  "items": {"type": "object"}},
    })},
})
MODEL_SERVICE_DEFINITION = ServiceDefinition(MODEL_SERVICE_REF, (
    ServiceOperation("models:resolve", "bind-native-model", object_schema({
        "reference": _MODEL_REFERENCE_SCHEMA,
    }), model_binding_schema()),
    *model_request_operations(),
    ServiceOperation("model-facts:read", "read-model-request-facts",
                     _FACT_REQUEST_SCHEMA, _FACT_RESPONSE_SCHEMA),
))


def model_service_requirement(capability, *operations):
    return {**MODEL_SERVICE_REF.to_dict(), "capability": capability, "operations": list(operations)}


class ModelHostService:
    """Own one chain-scoped request service; never freezes a model at run start."""

    def __init__(self, environment):
        self.environment = environment
        options = dict(environment.options)
        self.resource_reader = options.pop("model_resource_reader", None)
        self.fact_reader = options.pop("model_fact_reader", None)
        broker_factory = options.pop("credential_broker_factory", None)
        transport_factory = options.pop("transport_factory", None)
        require(self.resource_reader is not None
                and all(callable(getattr(self.resource_reader, name, None)) for name in (
                    "authorize_binding", "read_resource_bundle", "check_resource_bundle"))
                and callable(getattr(self.fact_reader, "__call__", None))
                and callable(broker_factory),
                "model_host_callbacks_unavailable",
                "Model service requires exact readers and a private credential broker", 500)
        credential_broker = broker_factory()
        self.implementation = ModelRequestService(
            authorize_binding=self.resource_reader.authorize_binding,
            read_resource_bundle=self.resource_reader.read_resource_bundle,
            check_resource_bundle=self.resource_reader.check_resource_bundle,
            accept_fact=self._accept_fact,
            credential_broker=credential_broker,
            resolve_transport=lambda protocol, model: resolve_model_request_transport(
                protocol, model, transport_factory=transport_factory),
        )
        self._bindings = {}

    def _accept_fact(self, context, fact):
        owner = {"session_id": context.workflow_session_id, "chain_run_id": context.chain_run_id,
                 "node_binding_id": context.node_binding_id, "node_run_id": context.node_run_id}
        require(all(fact.get(key) == value for key, value in owner.items()),
                "runtime_fact_owner_mismatch", "Model fact belongs to another invocation", 409)
        return self.environment.accept_fact(context, fact, "model:" + fact["request_id"])

    @staticmethod
    def _owner(context):
        return tuple(getattr(context, key, None) for key in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"))

    def _bind_reference(self, context, payload):
        reference = validate_model_reference(payload["reference"])
        owner = self._owner(context)
        key = owner
        digest = content_digest(reference)
        prior = self._bindings.get(key)
        if prior is not None:
            require(prior[0] == digest, "model_binding_conflict",
                    "Source invocation cannot change its saved model reference", 409)
            return dict(prior[1])
        binding = validate_model_binding({
            "schema_version": 5, "kind": "workflow.model-binding", "binding_id": str(uuid4()),
            "reference": reference, "authorization": {"purposes": ["normal", "compaction"]},
        })
        self._bindings[key] = (digest, binding)
        return dict(binding)

    def __call__(self, context, capability, operation, payload):
        if capability == "models:resolve" and operation == "bind-native-model":
            return self._bind_reference(context, payload)
        if capability == "model-facts:read" and operation == "read-model-request-facts":
            return self.fact_reader(context, capability, operation, payload)
        return self.implementation(context, capability, operation, payload)

    def has_pending_acceptance(self, context):
        return self.implementation.has_pending_acceptance(context)

    def recover_acceptance(self, context):
        return {"output": self.implementation.retry_acceptance(context)}

    def release_run(self, workflow_session_id, chain_run_id):
        self.implementation.release_run(workflow_session_id, chain_run_id)
        self._bindings = {
            owner: binding for owner, binding in self._bindings.items()
            if owner[:2] != (workflow_session_id, chain_run_id)
        }

    @property
    def active_frame_count(self):
        return self.implementation.active_frame_count


def create_model_host_service(environment):
    return ModelHostService(environment)
