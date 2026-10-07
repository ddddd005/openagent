"""Model-package adaptation of the generic host service lifecycle."""

from .host_sdk import ServiceDefinition, ServiceOperation, ServiceReference
from .model_service import PublicModelService
from .content_contracts import object_schema


MODEL_SERVICE_REF = ServiceReference("workflow.models", "1.0.0")
_BINDING_SCHEMA = {"type": "object", "required": ["schema_version", "kind", "binding_id"],
                   "properties": {"schema_version": {"const": 1},
                                  "kind": {"const": "workflow.model-binding"},
                                  "binding_id": {"type": "string"}}}
_RESULT_SCHEMA = {"type": "object", "required": ["schema_version", "kind", "binding_id", "request_id"],
                  "properties": {"schema_version": {"const": 1},
                                 "kind": {"const": "workflow.model-result"},
                                 "binding_id": {"type": "string"}, "request_id": {"type": "string"}}}
MODEL_SERVICE_DEFINITION = ServiceDefinition(MODEL_SERVICE_REF, (
    ServiceOperation("models:resolve", "bind-model", object_schema({
        "reference": {"type": "object"}, "parameters": {"type": "object"}}), _BINDING_SCHEMA),
    ServiceOperation("models:resolve", "bind-native-model", object_schema({
        "reference": {"type": "object"}, "parameters": {"type": "object"},
        "capacity": {"type": "object"}}),
        {**_BINDING_SCHEMA, "properties": {
            **_BINDING_SCHEMA["properties"], "schema_version": {"const": 2}}}),
    ServiceOperation("models:call", "chat", object_schema({"binding_id": {"type": "string"}}), _RESULT_SCHEMA),
    ServiceOperation("models:call", "kernel-model", object_schema({
        "binding_id": {"type": "string"}, "request_key": {"type": "string"},
        "messages": {"type": "array"}, "tools": {"type": "array"}}), _RESULT_SCHEMA),
    ServiceOperation("models:call", "kernel-compaction", object_schema({
        "binding_id": {"type": "string"}, "request_key": {"type": "string"},
        "messages": {"type": "array"}, "tools": {"type": "array"}}), _RESULT_SCHEMA),
))


def model_service_requirement(capability, *operations):
    return {**MODEL_SERVICE_REF.to_dict(), "capability": capability, "operations": list(operations)}


class ModelHostService:
    def __init__(self, environment):
        self.environment = environment
        options = environment.options
        self.implementation = PublicModelService(
            provider_reader=environment.read_resource,
            accept_fact=self._accept_fact,
            resolve_input=lambda context, port: environment.resolve_input(context, port)["value"],
            **options,
        )

    def _accept_fact(self, context, fact):
        from .graph_records import require
        owner = {"session_id": context.workflow_session_id, "chain_run_id": context.chain_run_id,
                 "node_binding_id": context.node_binding_id, "node_run_id": context.node_run_id}
        require(all(fact.get(key) == value for key, value in owner.items()),
                "runtime_fact_owner_mismatch", "Model fact belongs to another invocation", 409)
        return self.environment.accept_fact(context, fact, "model:" + fact["request_id"])

    def prepare_run(self, *, workflow_session_id, chain_run_id, nodes, resources):
        source_ids = {node["node_binding_id"] for node in nodes if any(
            grant["capability"] == "models:resolve"
            and bool({"bind-model", "bind-native-model"} & set(grant["operations"]))
            for grant in node["service_requirements"])}
        self.implementation.prepare_run(
            workflow_session_id=workflow_session_id, chain_run_id=chain_run_id,
            node_configs={node["node_binding_id"]: node["config"] for node in nodes
                          if node["node_binding_id"] in source_ids},
            records=[record for node_id in source_ids for record in resources.get(node_id, [])],
        )

    def __call__(self, context, capability, operation, payload):
        return self.implementation(context, capability, operation, payload)

    def accept_outputs(self, event):
        for port, value in event["outputs"].items():
            if value.get("kind") == "workflow.model-binding":
                self.implementation.accept_binding_output(
                    workflow_session_id=event["workflow_session_id"], chain_run_id=event["chain_run_id"],
                    node_run_id=event["node_run_id"], binding_id=value["binding_id"],
                    output_id=event["output_refs"][port])

    def has_pending_acceptance(self, context):
        return self.implementation.has_pending_acceptance(context)

    def recover_acceptance(self, context):
        return {"output": self.implementation.retry_acceptance(context)}

    def validate_failed_retry(self, owner, evidence, *, authorize=False):
        return self.implementation.validate_failed_retry(owner, evidence, authorize=authorize)

    def release_run(self, workflow_session_id, chain_run_id):
        self.implementation.release_run(workflow_session_id, chain_run_id)

    @property
    def active_frame_count(self):
        return self.implementation.active_frame_count


def create_model_host_service(environment):
    return ModelHostService(environment)
