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
    operation("request-evidence.write", "write_evidence",
              ("operation", "idempotency_key", "owner_kind", "owner_id", "request")),
    operation("request-evidence.resolve", "resolve_evidence",
              ("operation", "idempotency_key", "request_digest", "resolution")),
    operation("content.metadata.update", "update_content_metadata",
              ("reference", "display_name", "expected_metadata_sequence", "idempotency_key")),
    operation("session.metadata.update", "update_session_metadata",
              ("name", "expected_metadata_sequence", "idempotency_key"), session=True),
    operation("provider.credential.save", "save_provider_credential",
              ("identity", "expected_sequence", "credential_ref", "expected_generation",
               "secret", "idempotency_key", "provider_seed")),
    operation("credential.delete", "delete_credential",
              ("credential_ref", "expected_generation", "idempotency_key")),
    operation("definition.update", "update_definition",
              ("workflow_definition_id", "document", "expected_edit_sequence", "idempotency_key"),
              ("confirm_derivation",)),
    operation("definition.archive", "archive_definition",
              ("identity", "expected_revision", "idempotency_key")),
    operation("definition.delete", "delete_definition",
              ("identity", "expected_revision", "idempotency_key")),
    operation("session.create", "create_session",
              ("workflow_definition_id", "definition_revision", "idempotency_key"), status=201),
    operation("session.archive", "archive_session", ("expected_revision", "idempotency_key"), session=True),
    operation("session.delete", "delete_session", ("expected_revision", "idempotency_key"), session=True),
    operation("run.start", "start", ("expected_revision", "idempotency_key"), ("inputs",), session=True, status=202),
    operation("event.submit", "submit_event",
              ("workflow_definition_id", "definition_revision", "event_id", "event_schema_version",
               "payload", "expected_revision", "idempotency_key"), session=True, status=202),
    operation("run.control", "control", ("action", "expected_revision", "idempotency_key"), session=True),
    operation("checkpoint.fork", "fork_checkpoint",
              ("checkpoint_id", "expected_session_revision", "expected_data_revision",
               "expected_head_revision", "idempotency_key"), session=True, status=201),
    operation("session.data.update", "update_data",
              ("expected_revision", "expected_data_revision", "idempotency_key"),
              ("variables", "shared"), session=True),
    operation("object.write", "update_session_objects",
              ("node_id", "writes", "expected_revision", "idempotency_key"), session=True),
    operation("resource.save", "save_global_resource", ("record", "expected_sequence", "idempotency_key")),
    operation("resource.delete", "delete_global_resource", ("identity", "expected_sequence", "idempotency_key")),
    operation("packages.configure", "configure_capability_packages", ("enabled_packages",)),
)

# Kept out of GraphApplication dispatch/discovery. Receipt reads still need the
# exact retired request contract to verify previously persisted results.
RETIRED_RECEIPT_COMMANDS = (
    operation("consumer.draft.save", "save_consumer_draft",
              ("address", "format_version", "value", "expected_sequence", "idempotency_key"), consumer=True),
    operation("consumer.session.create", "create_consumer_session",
              ("workflow_definition_id", "definition_revision", "idempotency_key"), consumer=True, status=201),
    operation("consumer.run.start", "start_consumer",
              ("expected_revision", "idempotency_key"), ("inputs",), session=True, consumer=True, status=202),
    operation("consumer.event.submit", "submit_consumer_event",
              ("workflow_definition_id", "definition_revision", "event_id", "event_schema_version",
               "payload", "expected_revision", "idempotency_key"), session=True, consumer=True, status=202),
    operation("consumer.run.control", "control_consumer",
              ("action", "expected_revision", "idempotency_key"),
              session=True, consumer=True),
    operation("session.copy", "copy_session",
              ("document", "expected_session_revision", "expected_data_revision",
               "expected_definition_revision", "expected_head_revision", "idempotency_key"),
              ("mappings",), session=True, status=201),
    operation("context.adopt", "adopt_context_view",
              ("node_id", "view_output_id", "expected_revision", "idempotency_key"), session=True),
    operation("session.rebind", "rebind_session",
              ("definition_revision", "expected_revision", "expected_data_revision",
               "expected_head_revision", "idempotency_key"), ("mappings",), session=True),
    operation("candidate.select", "select_graph_candidate",
              ("candidate_id", "expected_revision", "expected_data_revision", "expected_head_revision",
               "idempotency_key"), session=True),
    operation("candidate.fork", "fork_graph_candidate",
              ("candidate_id", "expected_revision", "expected_data_revision", "expected_head_revision",
               "idempotency_key"), session=True, status=201),
    operation("consumer.candidate.fork", "fork_consumer_candidate",
              ("candidate_id", "expected_revision", "expected_data_revision", "expected_head_revision",
               "idempotency_key"), session=True, consumer=True, status=201),
    operation("draft.save", "save_draft",
              ("address", "format_version", "value", "expected_sequence", "idempotency_key")),
    operation("definition.save", "save_definition",
              ("document", "expected_revision", "idempotency_key"), status=201),
)

QUERIES = (
    operation("request-evidence.read", "read_evidence", ("owner_kind", "owner_id")),
    operation("content.metadata.read", "read_content_metadata", ("reference",)),
    operation("session.metadata.read", "get_session_metadata", session=True),
    operation("provider.credential.receipt", "provider_credential_receipt", ("request",)),
    operation("provider.credential.bind-receipt", "provider_binding_receipt", ("request",)),
    operation("credential.protection", "credential_protection"),
    operation("credential.status", "credential_status", ("credential_ref",)),
    operation("credential.receipt", "credential_receipt", ("operation", "request")),
    operation("provider.models.list", "list_provider_models", ("identity", "expected_sequence")),
    operation("draft.read", "read_draft", ("address",)),
    operation("draft.list", "list_drafts", optional=("kind", "workflow_definition_id")),
    operation("consumer.draft.read", "read_consumer_draft", ("address",), consumer=True),
    operation("consumer.draft.list", "list_consumer_drafts", ("workflow_definition_id",),
              ("kind",), consumer=True),
    operation("receipt.read", "_receipt_read", ("operation", "parameters"), consumer=True),
    operation("catalog.node-types", "node_types", optional=("protocol_version",)),
    operation("platform", "platform_capabilities"),
    operation("definition.list", "list_definitions", optional=("include_archived",)),
    operation("definition.read", "get_definition", ("identity",), ("revision",)),
    operation("definition.inputs", "_editor_inputs", ("document",)),
    operation("definition.sessions", "list_sessions", ("workflow_definition_id",)),
    operation("session.read", "get_session", session=True),
    operation("session.actions", "_management_actions", session=True),
    operation("run.read", "get_run", ("chain_id",), session=True),
    operation("checkpoint.list", "list_checkpoints", session=True),
    operation("checkpoint.read", "read_checkpoint", ("checkpoint_id",), session=True),
    operation("event.bindings", "list_event_bindings",
              ("workflow_definition_id", "definition_revision"), session=True),
    operation("event.read", "read_event", ("chain_id",), session=True),
    operation("candidate.list", "list_graph_candidates", session=True),
    operation("object.list", "get_session_objects", session=True),
    operation("object.read", "read_session_object", ("object_key", "node_id"), ("revision_id",), session=True),
    operation("object.artifact.read", "read_object_artifact",
              ("object_key", "node_id", "reference"), ("revision_id",), session=True),
    operation("registration.list", "_registration_list",
              optional=("session_id", "chain_id", "node_id", "kind", "package_id", "channel_id", "limit", "cursor")),
    operation("information.read", "_information_read", ("reference", "owner", "generation"),
              ("source_scope", "limit", "cursor"), session=True),
    operation("manifest.read", "get_state_manifest", optional=("snapshot_id", "chain_id", "boundary"), session=True),
    operation("resource.read", "get_global_resource", ("identity",)),
    operation("resource.list", "list_global_resources", optional=("scope", "type_id")),
    operation("frontend.extensions.read", "consumer_frontend_extensions",
              ("workflow_definition_id", "definition_revision", "node_id", "port_id"),
              session=True),
    operation("output.list", "public_outputs", session=True),
    operation("output.read", "read_public_output",
              ("workflow_definition_id", "definition_revision", "node_id", "port_id"), ("run_id",),
              session=True),
    operation("output.artifact.read", "read_public_artifact",
              ("workflow_definition_id", "definition_revision", "node_id", "port_id", "run_id", "reference"),
              session=True),
    operation("output.history", "public_output_history",
              ("workflow_definition_id", "definition_revision", "node_id", "port_id"), session=True),
)


def validate_parameters(spec, parameters, *, stored_evidence=False):
    required = spec.required | ({"session_id"} if spec.session else set())
    require(type(parameters) is dict and required <= set(parameters) <= required | spec.optional,
            "invalid_request", "Application operation fields differ")
    if spec.name.startswith("request-evidence."):
        from .request_evidence import validate_evidence_owner, validate_evidence_request
        if spec.name in ("request-evidence.read", "request-evidence.write"):
            validate_evidence_owner(parameters["owner_kind"], parameters["owner_id"])
        if spec.name == "request-evidence.write":
            validate_evidence_request(
                parameters["operation"], parameters["idempotency_key"], parameters["request"],
                stored=stored_evidence)
        if spec.name == "request-evidence.resolve":
            from re import fullmatch
            require(type(parameters["operation"]) is str
                    and type(parameters["request_digest"]) is str
                    and fullmatch("[0-9a-f]{64}", parameters["request_digest"]) is not None
                    and parameters["resolution"] in ("verified", "archived"),
                    "invalid_request", "Request evidence resolution is invalid")
    if "expected_metadata_sequence" in parameters:
        require(type(parameters["expected_metadata_sequence"]) is int
                and 0 <= parameters["expected_metadata_sequence"] < 2**53 - 1,
                "invalid_request", "Metadata expected sequence is invalid")
    if "expected_edit_sequence" in parameters:
        require(type(parameters["expected_edit_sequence"]) is int
                and 0 <= parameters["expected_edit_sequence"] < 2**53 - 1,
                "invalid_request", "Configuration expected sequence is invalid")
    if "confirm_derivation" in parameters:
        require(type(parameters["confirm_derivation"]) is bool, "invalid_request",
                "Derivation confirmation must be boolean")
    if spec.name == "credential.delete":
        from .credential_store import credential_id
        credential_id(parameters["credential_ref"])
        require(type(parameters["expected_generation"]) is int
                and 0 <= parameters["expected_generation"] < 2**53 - 1
                and uuid_value(parameters["idempotency_key"]),
                "invalid_request", "Credential mutation identity is invalid")
    if spec.name == "provider.credential.save":
        from .graph_provider_host import validate_provider_key_request
        validate_provider_key_request({key: value for key, value in parameters.items() if key != "secret"})
    if spec.name in ("draft.read", "draft.save", "consumer.draft.read", "consumer.draft.save"):
        from .graph_drafts import validate_draft_address, validate_draft_value
        address = validate_draft_address(parameters["address"], "consumer" if spec.consumer else "management")
        if spec.name.endswith(".save"):
            validate_draft_value(address, parameters["format_version"], parameters["value"])
            require(type(parameters["expected_sequence"]) is int
                    and 0 <= parameters["expected_sequence"] <= 2**53 - 1,
                    "invalid_request", "Draft expected sequence is invalid")
    if spec.session:
        require(uuid_value(parameters["session_id"]), "not_found", "Session not found", 404)
    for field, value in parameters.items():
        if field.endswith("revision"):
            if spec.name == "definition.read" and field == "revision" and value is None:
                continue
            minimum = 0 if field == "expected_data_revision" or spec.name == "definition.save" else 1
            require(type(value) is int and minimum <= value <= 2**53 - 1,
                    "invalid_request", "Invalid graph revision")
        if field == "idempotency_key":
            require(type(value) is str and 1 <= len(value) <= 128 and bool(value.strip()),
                    "invalid_request", "An idempotency key is required")
        if field in ("workflow_definition_id", "chain_id", "candidate_id", "checkpoint_id"):
            require(uuid_value(value), "invalid_request", "Invalid graph identity")
    if spec.name in ("definition.read", "definition.archive", "definition.delete",
                     "consumer.definition", "consumer.sessions"):
        require(uuid_value(parameters["identity"]), "not_found", "Definition not found", 404)
    if spec.name == "definition.list" and "include_archived" in parameters:
        require(type(parameters["include_archived"]) is bool, "invalid_request", "Archive filter is invalid")
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
                and parameters["action"] in ("pause", "resume", "close", "retry_archive", "retry_acceptance", "retry_failed_node"),
                "invalid_request", "Unknown graph control")
    return parameters
