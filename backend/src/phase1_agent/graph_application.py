"""Same-process graph application entry point over the existing service authorities."""

from copy import deepcopy

from .graph_application_contracts import COMMANDS, QUERIES, validate_parameters
from .graph_application_identity import (
    application_accepted_result, application_command_receipt, bind_application_command,
)
from .graph_records import require


class GraphApplication:
    """Bound trusted-local scope; owns neither runtime state nor durable records.

    Consumer scope restricts dispatch to existing consumer projections and actions.
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
                             "consumer_policy": "declared_public_outputs_and_session_history",
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
        target = self if spec.method in (
            "_management_actions", "_consumer_actions", "_registration_list", "_information_read",
            "_receipt_read",
        ) else self._service
        callback = getattr(target, spec.method)
        positional = []
        if spec.session:
            positional.append(arguments.pop("session_id"))
        # The existing read APIs use different positional identity names.
        for field in ("identity", "archive_id"):
            if field in arguments and spec.name not in ("resource.read", "resource.delete"):
                positional.append(arguments.pop(field))
        return callback(*positional, **arguments)

    @staticmethod
    def _receipt_result(result):
        return application_accepted_result(result)

    def command(self, name, parameters):
        spec = self._operation(self._commands, name)
        # Keep the original request fixed even if a service callback mutates its copy.
        request = deepcopy(validate_parameters(spec, parameters))
        operation_scope = "consumer" if spec.consumer else "management"
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

    def _actions(self, session_id, *, consumer):
        view = self._service.get_consumer(session_id) if consumer else self._service.get_session(session_id)
        return {"schema_version": 1, "kind": "workflow.application-actions",
                "scope": "consumer" if consumer else "management",
                "workflow_definition_id": view["workflow_definition_id"],
                "definition_revision": view["definition_revision"], "workflow_session_id": session_id,
                "session_revision": view["session_revision"] if consumer else view["revision"],
                "status": view["status"], "can_submit": view["can_submit"],
                "available_actions": deepcopy(view["available_actions"]),
                "start_operation": "consumer.run.start" if consumer else "run.start",
                "control_operation": "consumer.run.control" if consumer else "run.control"}

    def _management_actions(self, session_id):
        return self._actions(session_id, consumer=False)

    def _consumer_actions(self, session_id):
        return self._actions(session_id, consumer=True)

    def _registration_list(self, **parameters):
        return self._service.list_registrations(audience=self.scope, **parameters)

    def _information_read(self, session_id, **parameters):
        return self._service.read_information(session_id, audience=self.scope, **parameters)
