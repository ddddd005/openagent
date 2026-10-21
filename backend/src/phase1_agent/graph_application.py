"""Same-process graph application entry point over the existing service authorities."""

from copy import deepcopy

from .graph_application_contracts import (
    COMMANDS, QUERIES, application_operation_authority, validate_parameters,
)
from .graph_application_identity import (
    application_accepted_result, application_command_receipt, bind_application_command,
)
from .graph_records import require


class GraphApplication:
    """Bound trusted-local scope; owns neither runtime state nor durable records.

    Consumer scope retains only read-only original receipts and stored drafts.
    It is not remote caller authentication, and node_id remains a trusted management
    input whose graph permissions are checked by the underlying object service.
    """

    def __init__(self, service, *, scope="management"):
        require(scope in ("management", "consumer"), "invalid_request", "Unknown application scope")
        self._service = service
        self._scope = scope
        self._commands = {item.name: item for item in COMMANDS}
        self._queries = {item.name: item for item in QUERIES}

    @property
    def scope(self):
        return self._scope

    def for_consumer(self):
        return self if self.scope == "consumer" else GraphApplication(self._service, scope="consumer")

    def _operation(self, registry, name):
        require(type(name) is str and name in registry, "not_found", "Application operation not found", 404)
        spec = registry[name]
        require(self.scope == "management" or spec.consumer,
                "application_scope_denied", "Operation requires trusted management scope", 403)
        return spec

    def describe(self):
        return {"schema_version": 1, "kind": "workflow.application", "scope": self.scope,
                "boundary": {"caller": "trusted_local", "remote_plugin_authentication": False,
                             "consumer_policy": "original_receipts_and_stored_drafts_readonly",
                             "object_node_identity": "trusted_management_input",
                             "execution_authority": "graph_workflow_service",
                             "receipt_authority": "existing_service_transactions"},
                "commands": [spec.describe("command") for spec in COMMANDS
                             if self.scope == "management" or spec.consumer],
                "queries": [spec.describe("query") for spec in QUERIES
                            if self.scope == "management" or spec.consumer]}

    def command_status(self, name):
        return self._operation(self._commands, name).status

    def _invoke(self, spec, parameters):
        arguments = deepcopy(validate_parameters(spec, parameters))
        if spec.name.startswith("request-evidence."):
            from .request_evidence import RequestEvidenceService
            target = RequestEvidenceService(self._service.database)
        elif spec.name.startswith("content.metadata."):
            target = self._service.content_configuration
        elif spec.name.startswith("resource."):
            target = self._service
        else:
            target = self._service.provider_configuration if spec.name.startswith((
            "credential.", "provider.",
            )) else self if spec.method in (
                "_management_actions", "_registration_list", "_information_read",
                "_receipt_read", "_editor_inputs",
            ) else self._invocation_service(spec)
        callback = getattr(target, spec.method)
        positional = []
        if spec.session:
            positional.append(arguments.pop("session_id"))
        # The existing read APIs use different positional identity names.
        for field in ("identity",):
            if field in arguments and spec.name not in ("resource.read", "resource.delete"):
                positional.append(arguments.pop(field))
        return callback(*positional, **arguments)

    def _invocation_service(self, spec):
        authority = application_operation_authority(spec.name)
        # Queries keep their existing management-service read path first.
        if spec.name in self._queries or authority == "management":
            return self._management_service()
        if authority == "lifecycle":
            return self._service
        return getattr(self._service, "graph_service", self._service)

    def _management_service(self):
        return getattr(self._service, "graph_management_service", self._service)

    @staticmethod
    def _receipt_result(result):
        return application_accepted_result(result)

    def command(self, name, parameters):
        spec = self._operation(self._commands, name)
        # Keep the original request fixed even if a service callback mutates its copy.
        request = deepcopy(validate_parameters(spec, parameters))
        operation_scope = "consumer" if spec.consumer else "management"
        if name in ("credential.delete", "provider.credential.save"):
            result = self._invoke(spec, request)
            public_request = {key: value for key, value in request.items() if key != "secret"}
            receipt = application_command_receipt(name, operation_scope, public_request, result,
                                                  authority="service_receipt")
            receipt["accepted"] = deepcopy(result)
            return {"schema_version": 1, "kind": "workflow.application-command",
                    "receipt": receipt, "result": result}
        with bind_application_command(name, operation_scope, request):
            result = self._invoke(spec, request)
        receipt = application_command_receipt(name, operation_scope, request, result,
            authority="service_receipt" if "idempotency_key" in spec.required else "service_result")
        return {"schema_version": 1, "kind": "workflow.application-command",
                "receipt": receipt, "result": result}

    def query(self, name, parameters=None):
        spec = self._operation(self._queries, name)
        return self._invoke(spec, {} if parameters is None else parameters)

    def _receipt_read(self, operation, parameters):
        from .graph_receipts import read_graph_application_receipt
        return read_graph_application_receipt(self._service.database, operation, parameters, scope=self.scope)

    def _editor_inputs(self, document):
        from .graph_input_declarations import editor_input_declarations
        service = self._management_service()
        return editor_input_declarations(
            service.registry, document, definitions=getattr(service, "definition_query", None))

    def _management_actions(self, session_id):
        service = self._management_service()
        view = service.get_session(session_id)
        return {"schema_version": 1, "kind": "workflow.application-actions",
                "scope": "management",
                "workflow_definition_id": view["workflow_definition_id"],
                "definition_revision": view["definition_revision"], "workflow_session_id": session_id,
                "session_revision": view["revision"],
                "status": view["status"], "can_submit": view["can_submit"],
                "available_actions": deepcopy(view["available_actions"]),
                "start_operation": "run.start", "control_operation": "run.control"}

    def _registration_list(self, **parameters):
        return self._management_service().list_registrations(audience=self.scope, **parameters)

    def _information_read(self, session_id, **parameters):
        return self._management_service().read_information(session_id, audience=self.scope, **parameters)
