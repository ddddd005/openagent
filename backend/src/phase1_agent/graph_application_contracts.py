"""Named, transport-independent command and query scopes for graph applications."""

from dataclasses import dataclass

from .contract_errors import ContractValidationError
from .contract_json import validate_json_value
from .graph_records import require, uuid_value
from .host_sdk import bounded_name


@dataclass(frozen=True)
class GraphOperation:
    name: str
    method: str
    required: frozenset[str]
    optional: frozenset[str] = frozenset()
    session: bool = False
    consumer: bool = False
    status: int = 200

    def describe(self, kind):
        return {"name": self.name, "kind": kind,
                "scope": "consumer" if self.consumer else "management",
                "required_fields": sorted(self.required | ({"session_id"} if self.session else set())),
                "optional_fields": sorted(self.optional),
                "idempotency": "service_receipt" if "idempotency_key" in self.required else "none",
                "idempotency_key_format": (
                    "uuid4" if self.name.startswith("resource.") and "idempotency_key" in self.required
                    else "opaque" if "idempotency_key" in self.required else None),
                "http_status": self.status}


def operation(name, method, required=(), optional=(), **options):
    return GraphOperation(name, method, frozenset(required), frozenset(optional), **options)


COMMANDS = (
    operation("definition.save", "save_definition",
              ("document", "expected_revision", "idempotency_key"), status=201),
    operation("legacy.migrate", "migrate_legacy",
              ("document", "source_session_id", "expected_source_revision", "mappings", "idempotency_key"), status=201),
    operation("session.create", "create_session",
              ("workflow_definition_id", "definition_revision", "idempotency_key"), status=201),
    operation("run.start", "start", ("expected_revision", "idempotency_key"), ("inputs",), session=True, status=202),
    operation("event.submit", "submit_event",
              ("workflow_definition_id", "definition_revision", "event_id", "event_schema_version",
               "payload", "expected_revision", "idempotency_key"), session=True, status=202),
    operation("run.control", "control", ("action", "expected_revision", "idempotency_key"),
              ("add_model_requests", "add_model_attempts"), session=True),
    operation("session.copy", "copy_session",
              ("document", "expected_session_revision", "expected_data_revision",
               "expected_definition_revision", "expected_head_revision", "idempotency_key"),
              ("mappings",), session=True, status=201),
    operation("session.rebind", "rebind_session",
              ("definition_revision", "expected_revision", "expected_data_revision",
               "expected_head_revision", "idempotency_key"), ("mappings",), session=True),
    operation("session.data.update", "update_data",
              ("expected_revision", "expected_data_revision", "idempotency_key"),
              ("variables", "shared"), session=True),
    operation("candidate.select", "select_graph_candidate",
              ("candidate_id", "expected_revision", "expected_data_revision", "expected_head_revision",
               "idempotency_key"), session=True),
    operation("candidate.fork", "fork_graph_candidate",
              ("candidate_id", "expected_revision", "expected_data_revision", "expected_head_revision",
               "idempotency_key"), session=True, status=201),
    operation("object.write", "update_session_objects",
              ("node_id", "writes", "expected_revision", "idempotency_key"), session=True),
    operation("context.adopt", "adopt_context_view",
              ("node_id", "view_output_id", "expected_revision", "idempotency_key"), session=True),
    operation("resource.save", "save_global_resource", ("record", "expected_sequence", "idempotency_key")),
    operation("resource.delete", "delete_global_resource", ("identity", "expected_sequence", "idempotency_key")),
    operation("resource.import", "import_global_resource", ("legacy_id", "idempotency_key"), ("scope",)),
    operation("packages.configure", "configure_capability_packages", ("enabled_packages",)),
    operation("consumer.session.create", "create_consumer_session",
              ("workflow_definition_id", "definition_revision", "idempotency_key"), consumer=True, status=201),
    operation("consumer.run.start", "start_consumer",
              ("expected_revision", "idempotency_key"), ("inputs",), session=True, consumer=True, status=202),
    operation("consumer.event.submit", "submit_consumer_event",
              ("workflow_definition_id", "definition_revision", "event_id", "event_schema_version",
               "payload", "expected_revision", "idempotency_key"), session=True, consumer=True, status=202),
    operation("consumer.run.control", "control_consumer",
              ("action", "expected_revision", "idempotency_key"), ("add_model_requests", "add_model_attempts"),
              session=True, consumer=True),
)

QUERIES = (
    operation("catalog.node-types", "node_types", optional=("protocol_version",)),
    operation("platform", "platform_capabilities"),
    operation("definition.read", "get_definition", ("identity",), ("revision",)),
    operation("definition.sessions", "list_sessions", ("workflow_definition_id",)),
    operation("session.read", "get_session", session=True),
    operation("session.actions", "_management_actions", session=True),
    operation("run.read", "get_run", ("chain_id",), session=True),
    operation("event.bindings", "list_event_bindings",
              ("workflow_definition_id", "definition_revision"), session=True),
    operation("event.read", "read_event", ("chain_id",), session=True),
    operation("archive.read", "get_agent_archive", ("archive_id",), session=True),
    operation("candidate.list", "list_graph_candidates", session=True),
    operation("object.list", "get_session_objects", session=True),
    operation("object.read", "read_session_object", ("object_key", "node_id"), ("revision_id",), session=True),
    operation("object.artifact.read", "read_object_artifact",
              ("object_key", "node_id", "reference"), ("revision_id",), session=True),
    operation("registration.list", "_registration_list",
              optional=("session_id", "chain_id", "node_id", "kind", "package_id", "channel_id", "limit", "cursor"),
              consumer=True),
    operation("information.read", "_information_read", ("reference", "owner", "generation"),
              ("source_scope", "limit", "cursor"), session=True, consumer=True),
    operation("manifest.read", "get_state_manifest", optional=("snapshot_id", "chain_id", "boundary"), session=True),
    operation("resource.read", "get_global_resource", ("identity",)),
    operation("resource.list", "list_global_resources", optional=("scope", "type_id")),
    operation("consumer.definition", "consumer_definition", ("identity",), consumer=True),
    operation("frontend.extensions.read", "consumer_frontend_extensions",
              ("workflow_definition_id", "definition_revision", "node_id", "port_id"),
              session=True, consumer=True),
    operation("consumer.sessions", "list_consumer_sessions", ("identity",), consumer=True),
    operation("consumer.read", "get_consumer", session=True, consumer=True),
    operation("consumer.event.bindings", "list_consumer_event_bindings",
              ("workflow_definition_id", "definition_revision"), session=True, consumer=True),
    operation("consumer.event.read", "read_consumer_event", ("chain_id",), session=True, consumer=True),
    operation("consumer.actions", "_consumer_actions", session=True, consumer=True),
    operation("output.list", "public_outputs", session=True, consumer=True),
    operation("output.read", "read_public_output",
              ("workflow_definition_id", "definition_revision", "node_id", "port_id"), ("run_id",),
              session=True, consumer=True),
    operation("output.artifact.read", "read_public_artifact",
              ("workflow_definition_id", "definition_revision", "node_id", "port_id", "run_id", "reference"),
              session=True, consumer=True),
    operation("output.history", "public_output_history",
              ("workflow_definition_id", "definition_revision", "node_id", "port_id"), session=True, consumer=True),
)


def validate_parameters(spec, parameters):
    required = spec.required | ({"session_id"} if spec.session else set())
    require(type(parameters) is dict and required <= set(parameters) <= required | spec.optional,
            "invalid_request", "Application operation fields differ")
    if spec.session:
        require(uuid_value(parameters["session_id"]), "not_found", "Session not found", 404)
    for field, value in parameters.items():
        if field.endswith("revision"):
            if spec.name == "legacy.migrate" and field == "expected_source_revision" and value is None:
                continue
            if spec.name == "definition.read" and field == "revision" and value is None:
                continue
            minimum = 0 if field == "expected_data_revision" or spec.name == "definition.save" else 1
            require(type(value) is int and minimum <= value <= 2**53 - 1,
                    "invalid_request", "Invalid graph revision")
        if field == "idempotency_key":
            require(type(value) is str and 1 <= len(value) <= 128 and bool(value.strip()),
                    "invalid_request", "An idempotency key is required")
        if field in ("workflow_definition_id", "chain_id", "archive_id"):
            require(uuid_value(value), "invalid_request", "Invalid graph identity")
    if spec.name in ("definition.read", "consumer.definition", "consumer.sessions"):
        require(uuid_value(parameters["identity"]), "not_found", "Definition not found", 404)
    if spec.name == "legacy.migrate":
        require(parameters["source_session_id"] is None or uuid_value(parameters["source_session_id"]),
                "invalid_request", "Invalid source session")
    if "mappings" in parameters:
        require(type(parameters["mappings"]) is list, "invalid_request", "Mappings must be an array")
    if "inputs" in parameters:
        require(type(parameters["inputs"]) is dict, "invalid_request", "Inputs must be an object")
    if "event_id" in parameters:
        require(bounded_name(parameters["event_id"]), "invalid_request", "Event identity must be a bounded name")
    if "event_schema_version" in parameters:
        require(type(parameters["event_schema_version"]) is int
                and 1 <= parameters["event_schema_version"] <= 2**53 - 1,
                "invalid_request", "An exact event schema version is required")
    if "payload" in parameters:
        require(type(parameters["payload"]) is dict and "_workflow_frozen_resources" not in parameters["payload"],
                "invalid_request", "Event payload must be an object with nonreserved inputs")
        try:
            validate_json_value(parameters["payload"])
        except ContractValidationError:
            require(False, "invalid_request", "Event payload must contain only JSON values")
    if "protocol_version" in parameters:
        require(type(parameters["protocol_version"]) is int and parameters["protocol_version"] in (1, 2),
                "invalid_request", "Invalid node protocol version")
    if "action" in parameters:
        require(type(parameters["action"]) is str
                and parameters["action"] in ("pause", "resume", "close", "extend_budget", "retry_archive", "retry_acceptance", "retry_failed_node"),
                "invalid_request", "Unknown graph control")
    return parameters
