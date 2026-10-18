"""Same-process operations owned by one exact saved frontend node instance."""

from copy import deepcopy
from dataclasses import dataclass

from .chat_contracts import CHAT_CONTEXT_TYPE
from .contract_errors import require, uuid4_string
from .contract_json import content_digest
from .frontend_node_contracts import FRONTEND_NODE_COMPONENT
from .workflow_operations import WorkflowOperations, read_native_receipt
from .workflow_chat_operations import WorkflowChatOperations


_OPERATIONS = {
    "patch_input": "graph.frontend-node.patch-input",
    "start": "graph.frontend-node.start",
    "pause": "graph.frontend-node.pause",
    "resume": "graph.frontend-node.resume",
    "close": "graph.frontend-node.close",
    "fork_checkpoint": "graph.frontend-node.fork-checkpoint",
    "reroll": "graph.frontend-node.reroll",
    "select_candidate": "graph.frontend-node.select-candidate",
    "fork_floor": "graph.frontend-node.fork-floor",
}
_RECEIPT_ONLY_OPERATIONS = frozenset({
    "patch_input", "start", "pause", "resume", "close", "fork_checkpoint",
})
_CHAT_FIELDS = {"round_id", "expected_session_revision", "expected_data_revision",
                "expected_head_revision", "expected_object_revision"}
_MAX_REVISION = 2**53 - 1


def _revision(value, *, nullable=False):
    require(nullable and value is None or type(value) is int and 1 <= value <= _MAX_REVISION,
            "invalid_request", "Expected session revision is invalid")


class FrontendNodeOperations:
    def __init__(self, service):
        self._service = service

    def receipt_scope(self, *, workflow_definition_id, definition_revision, node_binding_id, session_id=None):
        uuid4_string(workflow_definition_id)
        uuid4_string(node_binding_id)
        _revision(definition_revision)
        if session_id is not None:
            uuid4_string(session_id)
        return FrontendNodeReceiptScope(
            self._service, workflow_definition_id, definition_revision, node_binding_id, session_id)

    def bind(self, *, workflow_definition_id, definition_revision, node_binding_id, session_id=None):
        uuid4_string(workflow_definition_id)
        uuid4_string(node_binding_id)
        _revision(definition_revision)
        if session_id is not None:
            uuid4_string(session_id)
        bound = BoundFrontendNodeOperations(
            self._service, workflow_definition_id, definition_revision, node_binding_id, session_id)
        with self._service._lock, self._service._configuration_data.read() as data:
            self._service._check_open()
            bound._require_binding(data)
        return bound


@dataclass(frozen=True)
class BoundFrontendNodeOperations:
    _service: object
    workflow_definition_id: str
    definition_revision: int
    node_binding_id: str
    session_id: str | None

    def _identity(self):
        return {
            "workflow_definition_id": self.workflow_definition_id,
            "definition_revision": self.definition_revision,
            "node_binding_id": self.node_binding_id, "session_id": self.session_id,
        }

    def _binding_data(self, access):
        # Inside an existing command transaction, wrap the caller's repo
        # instead of opening a second store (which would block on its
        # BEGIN IMMEDIATE).  A limited data session is used as-is.
        return (access if hasattr(access, "fixed_document")
                else self._service._configuration_data.transaction(access))

    def _require_binding(self, access, *, current=False):
        data = self._binding_data(access)
        service = self._service
        service._require_definition_active_in(data, self.workflow_definition_id)
        document = data.fixed_document(self.workflow_definition_id, self.definition_revision)
        node = next((entry for entry in document["nodes"]
                     if entry["node_binding_id"] == self.node_binding_id), None)
        require(node is not None, "frontend_node_binding_mismatch",
                "The saved frontend node binding is unavailable", 409)
        identity = node["component_id"], node["component_version"]
        require(identity == FRONTEND_NODE_COMPONENT,
                "frontend_node_binding_mismatch", "The binding must be exactly frontend.node@2", 409)
        entry = service.registry.get(*identity)
        require(entry is not None, "frontend_node_binding_mismatch",
                "The exact frontend node contract is unavailable", 409)
        service.registry.validate_config(entry, node["config"])
        binding = next((item for item in document.get("object_bindings", [])
                        if item["object_key"] == node["config"]["object_key"]), None)
        require(binding is not None and binding["type_id"] == CHAT_CONTEXT_TYPE
                and binding["schema_version"] == 1
                and binding.get("registrar_node_id") == self.node_binding_id
                and self.node_binding_id in binding["readers"]
                and self.node_binding_id in binding["writers"],
                "frontend_node_registration_mismatch",
                "Frontend operations require the node's own registered readable and writable item", 409)
        if current:
            configuration = data.current_configuration(self.workflow_definition_id)
            require(configuration is not None
                    and configuration["fixed_definition_revision"] == self.definition_revision,
                    "stale_revision", "Frontend node fixed definition changed", 409)
        if self.session_id is not None:
            session = data.session(self.session_id)
            require(session["workflow_definition_id"] == self.workflow_definition_id
                    and session["definition_revision"] == self.definition_revision,
                    "frontend_node_session_mismatch", "Session belongs to another saved frontend node", 409)
            service._require_session_active_in(data, session)
        return node

    def _request(self, operation, arguments):
        request = {**self._identity(), **deepcopy(arguments)}
        allowed = {
            "patch_input": {"text", "expected_edit_sequence", "confirm_derivation"},
            "start": {"expected_session_revision"},
            "pause": {"expected_session_revision"},
            "resume": {"expected_session_revision"},
            "close": {"expected_session_revision"},
            "fork_checkpoint": {"checkpoint_id", "expected_session_revision",
                                "expected_data_revision", "expected_head_revision"},
            "reroll": _CHAT_FIELDS,
            "select_candidate": _CHAT_FIELDS | {"candidate_id"},
            "fork_floor": _CHAT_FIELDS,
        }[operation]
        require(set(arguments) <= allowed, "invalid_request", "Frontend operation arguments differ")
        return request

    def patch_input(self, text, *, expected_edit_sequence, idempotency_key, confirm_derivation=None):
        arguments = {"text": text, "expected_edit_sequence": expected_edit_sequence}
        if confirm_derivation is not None:
            arguments["confirm_derivation"] = confirm_derivation
        request = self._request("patch_input", arguments)

        def change(data):
            node = self._require_binding(data, current=True)
            target_id = node["config"]["input_node_id"]
            require(bool(target_id), "frontend_node_input_unbound",
                    "Frontend node has no editable input binding", 409)
            document = data.fixed_document(self.workflow_definition_id, self.definition_revision)
            target = next((item for item in document["nodes"] if item["node_binding_id"] == target_id), None)
            require(target is not None
                    and (target["component_id"], target["component_version"]) == ("tools.editable-input", "1"),
                    "frontend_node_input_mismatch", "Frontend node input must be exactly tools.editable-input@1", 409)
            return self._service.configuration_service.patch_exact_configuration(
                data, workflow_definition_id=self.workflow_definition_id, node_binding_id=target_id,
                component_id="tools.editable-input", component_version="1", changes={"text": request["text"]},
                expected_edit_sequence=expected_edit_sequence, confirm_derivation=confirm_derivation)

        return self._service._configuration_change(_OPERATIONS["patch_input"], idempotency_key, request, change)

    def read_snapshot(self):
        return self._service.read_frontend_node_snapshot(
            self.workflow_definition_id, self.definition_revision, self.node_binding_id, self.session_id)

    def _child_key(self, operation, key):
        return "frontend-node:" + content_digest({
            **self._identity(), "operation": operation, "idempotency_key": key,
        }).rsplit(":", 1)[1]

    def start(self, *, expected_session_revision=None, idempotency_key):
        request = self._request("start", {"expected_session_revision": expected_session_revision})
        return WorkflowOperations(self._service).start(
            workflow_definition_id=self.workflow_definition_id, definition_revision=self.definition_revision,
            session_id=self.session_id, expected_session_revision=expected_session_revision,
            idempotency_key=idempotency_key, receipt_operation=_OPERATIONS["start"], receipt_request=request,
            verify=lambda repo: self._require_binding(repo, current=self.session_id is None),
            create_key=self._child_key("create", idempotency_key),
            dispatch_key=self._child_key("start", idempotency_key))

    def _control(self, operation, *, expected_session_revision, idempotency_key):
        request = self._request(operation, {"expected_session_revision": expected_session_revision})
        return WorkflowOperations(self._service).control(
            session_id=self.session_id, action=operation, expected_session_revision=expected_session_revision,
            idempotency_key=idempotency_key, receipt_operation=_OPERATIONS[operation], receipt_request=request,
            verify=self._require_binding, create_key=self._child_key("create", idempotency_key),
            dispatch_key=self._child_key(operation, idempotency_key))

    def pause(self, **parameters):
        return self._control("pause", **parameters)

    def resume(self, **parameters):
        return self._control("resume", **parameters)

    def close(self, **parameters):
        return self._control("close", **parameters)

    def fork_checkpoint(self, *, checkpoint_id, expected_session_revision,
                        expected_data_revision, expected_head_revision, idempotency_key):
        arguments = {
            "checkpoint_id": checkpoint_id, "expected_session_revision": expected_session_revision,
            "expected_data_revision": expected_data_revision, "expected_head_revision": expected_head_revision,
        }
        request = self._request("fork_checkpoint", arguments)
        return WorkflowOperations(self._service).fork_checkpoint(
            session_id=self.session_id, **arguments, idempotency_key=idempotency_key,
            receipt_operation=_OPERATIONS["fork_checkpoint"], receipt_request=request,
            verify=self._require_binding, dispatch_key=self._child_key("fork_checkpoint", idempotency_key))

    def list_checkpoints(self):
        with self._service._lock, self._service._configuration_data.read() as data:
            self._service._check_open()
            self._require_binding(data)
            require(self.session_id is not None, "invalid_request", "Checkpoint reads require a session")
            operations = WorkflowOperations(self._service)
            return {"items": operations.list_checkpoints(self.session_id),
                    "session": operations.read_session_state(self.session_id)}

    def read_checkpoint(self, checkpoint_id):
        with self._service._lock, self._service._configuration_data.read() as data:
            self._service._check_open()
            self._require_binding(data)
            require(self.session_id is not None, "invalid_request", "Checkpoint reads require a session")
            return WorkflowOperations(self._service).read_checkpoint(self.session_id, checkpoint_id)

    def _chat_operation(self, operation, *, idempotency_key, **arguments):
        request = self._request(operation, arguments)
        required = _CHAT_FIELDS | ({"candidate_id"} if operation == "select_candidate" else set())
        require(set(arguments) == required, "invalid_request", "Frontend chat operation fields are missing")
        uuid4_string(arguments["round_id"])
        if operation == "select_candidate":
            uuid4_string(arguments["candidate_id"])
        for field in ("expected_session_revision", "expected_head_revision", "expected_object_revision"):
            _revision(arguments[field])
        require(type(arguments["expected_data_revision"]) is int
                and 0 <= arguments["expected_data_revision"] <= _MAX_REVISION,
                "invalid_request", "Expected data revision is invalid")

        def verify(repo):
            node = self._require_binding(repo)
            require(node["component_version"] == "2" and self.session_id is not None,
                    "frontend_node_chat_unavailable",
                    "Chat operations require frontend.node@2 and an exact session", 409)
            return node

        parameters = dict(arguments)
        with self._service._lock, self._service._configuration_data.read() as data:
            self._service._check_open()
            # A replay observes its saved plan even if the binding has changed.
            previous = read_native_receipt(self._service, _OPERATIONS[operation], idempotency_key, request)
            if previous is not None:
                return WorkflowChatOperations(self._service).read_operation_result(operation, previous)
            node = verify(data)
            parameters["object_key"] = node["config"]["object_key"]
        return WorkflowChatOperations(self._service).execute_operation(
            operation, self.session_id, parameters=parameters, idempotency_key=idempotency_key,
            receipt_operation=_OPERATIONS[operation], receipt_request=request, verify=verify,
            dispatch_key=self._child_key(operation, idempotency_key))

    def reroll(self, **parameters):
        return self._chat_operation("reroll", **parameters)

    def select_candidate(self, **parameters):
        return self._chat_operation("select_candidate", **parameters)

    def fork_floor(self, **parameters):
        return self._chat_operation("fork_floor", **parameters)

    def read_receipt(self, operation, original_request):
        require(type(operation) is str and operation in _OPERATIONS
                and type(original_request) is dict and "idempotency_key" in original_request,
                "invalid_request", "An exact frontend operation request is required")
        arguments = deepcopy(original_request)
        key = arguments.pop("idempotency_key")
        if operation == "start":
            arguments.setdefault("expected_session_revision", None)
        if operation == "patch_input" and arguments.get("confirm_derivation") is None:
            arguments.pop("confirm_derivation", None)
        required = {
            "patch_input": {"text", "expected_edit_sequence"},
            "start": {"expected_session_revision"}, "pause": {"expected_session_revision"},
            "resume": {"expected_session_revision"}, "close": {"expected_session_revision"},
            "fork_checkpoint": {"checkpoint_id", "expected_session_revision",
                                "expected_data_revision", "expected_head_revision"},
            "reroll": _CHAT_FIELDS,
            "select_candidate": _CHAT_FIELDS | {"candidate_id"},
            "fork_floor": _CHAT_FIELDS,
        }[operation]
        require(required <= set(arguments), "invalid_request", "Original frontend operation fields are missing")
        request = self._request(operation, arguments)
        receipt = read_native_receipt(self._service, _OPERATIONS[operation], key, request)
        if receipt is None or operation == "patch_input":
            return receipt
        if operation in ("reroll", "select_candidate", "fork_floor"):
            return WorkflowChatOperations(self._service).read_operation_result(operation, receipt)
        operations = WorkflowOperations(self._service)
        if operation == "start":
            return operations.read_start_result(receipt)
        if operation == "fork_checkpoint":
            return operations.read_fork_result(receipt)
        return operations.read_control_result(receipt, operation=operation)


@dataclass(frozen=True)
class FrontendNodeReceiptScope:
    """Read-only projection for a frozen frontend owner; it has no dispatch methods."""

    _service: object
    workflow_definition_id: str
    definition_revision: int
    node_binding_id: str
    session_id: str | None

    def read_receipt(self, operation, original_request):
        require(type(operation) is str and operation in _RECEIPT_ONLY_OPERATIONS, "invalid_request",
                "This receipt operation is not available through retired-node verification")
        bound = BoundFrontendNodeOperations(
            self._service, self.workflow_definition_id, self.definition_revision,
            self.node_binding_id, self.session_id)
        return bound.read_receipt(operation, original_request)
