"""Original request custody for one bound frontend node, never generic dispatch."""

from copy import deepcopy
from hashlib import sha256

from .contract_errors import ContractValidationError, require, uuid4_string
from .contract_json import canonical_bytes


FRONTEND_NODE_EVIDENCE_OPERATIONS = {
    "patch_input": "frontend-node.patch-input",
    "start": "frontend-node.start",
    "pause": "frontend-node.pause",
    "resume": "frontend-node.resume",
    "close": "frontend-node.close",
    "fork_checkpoint": "frontend-node.fork-checkpoint",
    "reroll": "frontend-node.reroll",
    "select_candidate": "frontend-node.select-candidate",
    "fork_floor": "frontend-node.fork-floor",
}
_PRIVATE_OPERATIONS = {private: operation for operation, private in FRONTEND_NODE_EVIDENCE_OPERATIONS.items()}
_MAX_SEQUENCE = 2**53 - 1
_OWNER_FIELDS = {"workflow_definition_id", "definition_revision", "node_binding_id", "session_id"}


def _validate_owner(owner):
    require(type(owner) is dict and set(owner) == _OWNER_FIELDS,
            "invalid_request", "Frontend evidence requires its exact bound owner")
    uuid4_string(owner["workflow_definition_id"])
    uuid4_string(owner["node_binding_id"])
    require(type(owner["definition_revision"]) is int and 1 <= owner["definition_revision"] <= _MAX_SEQUENCE,
            "invalid_request", "Frontend evidence fixed definition revision is invalid")
    if owner["session_id"] is not None:
        uuid4_string(owner["session_id"])


def validate_frontend_node_evidence_request(operation, idempotency_key, request):
    require(type(operation) is str and operation in _PRIVATE_OPERATIONS,
            "invalid_request", "Frontend evidence operation is not permitted")
    require(type(request) is dict and set(request) == {"owner", "parameters"},
            "invalid_request", "Frontend evidence request fields differ")
    _validate_owner(request["owner"])
    parameters = request["parameters"]
    name = _PRIVATE_OPERATIONS[operation]
    required = ({"text", "expected_edit_sequence", "idempotency_key"} if name == "patch_input"
                else {"expected_session_revision", "idempotency_key"})
    if name == "fork_checkpoint":
        required |= {"checkpoint_id", "expected_data_revision", "expected_head_revision"}
    if name in ("reroll", "select_candidate", "fork_floor"):
        required |= {"round_id", "expected_data_revision", "expected_head_revision", "expected_object_revision"}
        if name == "select_candidate":
            required.add("candidate_id")
    optional = {"confirm_derivation"} if name == "patch_input" else set()
    require(type(parameters) is dict and required <= set(parameters) <= required | optional,
            "invalid_request", "Frontend evidence operation parameters differ")
    key = parameters["idempotency_key"]
    require(type(key) is str and 1 <= len(key) <= 128 and bool(key.strip()),
            "invalid_request", "Frontend evidence requires an original idempotency key")
    require(key == idempotency_key, "request_evidence_conflict",
            "Frontend evidence key differs from the original request", 409)
    if name == "patch_input":
        require(type(parameters["text"]) is str and len(parameters["text"]) <= 1_000_000,
                "invalid_request", "Frontend evidence input must be bounded text")
        sequence = parameters["expected_edit_sequence"]
        require(type(sequence) is int and 0 <= sequence <= _MAX_SEQUENCE,
                "invalid_request", "Frontend evidence edit sequence is invalid")
        require("confirm_derivation" not in parameters or type(parameters["confirm_derivation"]) is bool,
                "invalid_request", "Frontend evidence derivation confirmation must be boolean")
    else:
        revision = parameters["expected_session_revision"]
        if name == "start" and request["owner"]["session_id"] is None:
            require(revision is None, "invalid_request",
                    "A frontend start without a session requires a null revision")
        else:
            require(request["owner"]["session_id"] is not None and type(revision) is int
                    and 1 <= revision <= _MAX_SEQUENCE,
                    "invalid_request", "Frontend evidence requires an exact session revision")
        if name in ("fork_checkpoint", "reroll", "select_candidate", "fork_floor"):
            uuid4_string(parameters["checkpoint_id"] if name == "fork_checkpoint" else parameters["round_id"])
            if name == "select_candidate":
                uuid4_string(parameters["candidate_id"])
            revisions = [("expected_data_revision", 0), ("expected_head_revision", 1)]
            if name != "fork_checkpoint":
                revisions.append(("expected_object_revision", 1))
            for field, minimum in revisions:
                require(type(parameters[field]) is int and minimum <= parameters[field] <= _MAX_SEQUENCE,
                        "invalid_request", "Frontend fork revision is invalid")
    return deepcopy(request)


def frontend_node_evidence_owner(operation, request):
    require(type(operation) is str and operation in _PRIVATE_OPERATIONS,
            "invalid_request", "Frontend evidence operation is not permitted")
    owner = request["owner"]
    _validate_owner(owner)
    identity = [owner["workflow_definition_id"], owner["definition_revision"],
                owner["node_binding_id"], owner["session_id"], _PRIVATE_OPERATIONS[operation]]
    return "frontend-node", sha256(canonical_bytes(identity)).hexdigest()


class FrontendNodeEvidenceAdapter:
    def __init__(self, database_path, *, store_factory=None):
        from .request_evidence import RequestEvidenceService
        self._evidence = RequestEvidenceService(database_path, store_factory=store_factory)

    @staticmethod
    def _owner(bound):
        return {
            "workflow_definition_id": bound.workflow_definition_id,
            "definition_revision": bound.definition_revision,
            "node_binding_id": bound.node_binding_id, "session_id": bound.session_id,
        }

    def _original(self, bound, operation, parameters):
        require(type(operation) is str and operation in FRONTEND_NODE_EVIDENCE_OPERATIONS,
                "invalid_request", "Frontend operation is not permitted")
        parameters = deepcopy(parameters)
        require(type(parameters) is dict and "idempotency_key" in parameters,
                "invalid_request", "An original frontend operation request is required")
        request = {"owner": self._owner(bound), "parameters": parameters}
        return validate_frontend_node_evidence_request(
            FRONTEND_NODE_EVIDENCE_OPERATIONS[operation], parameters["idempotency_key"], request)

    def _records(self, bound, operation, original):
        private = FRONTEND_NODE_EVIDENCE_OPERATIONS[operation]
        owner_kind, owner_id = frontend_node_evidence_owner(private, original)
        records = self._evidence.read_evidence(owner_kind, owner_id)
        require(all(record["operation"] == private and record["request"]["owner"] == self._owner(bound)
                    for record in records),
                "request_evidence_conflict", "Stored frontend evidence belongs to another binding", 409)
        return records

    def _existing(self, bound, operation, original):
        key = original["parameters"]["idempotency_key"]
        record = next((record for record in self._records(bound, operation, original)
                       if record["idempotency_key"] == key), None)
        if record is not None:
            require(canonical_bytes(record["request"]) == canonical_bytes(original),
                    "request_evidence_conflict", "The frontend evidence key has another original request", 409)
        return record

    @staticmethod
    def _accepted(operation, result):
        if type(result) is not dict:
            return False
        if operation == "patch_input":
            return result.get("kind") == "workflow.configuration-result" and result.get("outcome") in (
                "saved", "unchanged", "confirmation_required", "derived")
        return result.get("operation") == operation and result.get("outcome") == "succeeded"

    def _resolve(self, record, resolution):
        return self._evidence.resolve_evidence(
            operation=record["operation"], idempotency_key=record["idempotency_key"],
            request_digest=record["request_digest"], resolution=resolution)

    @staticmethod
    def _known_rejection(error):
        code = getattr(error, "reason_code", None)
        status = getattr(error, "status_code", None)
        diagnostics = getattr(error, "diagnostics", None)
        return (type(code) is str and bool(code)
                and code not in ("idempotency_conflict", "frontend_node_dispatch_unknown",
                                 "workflow_operation_dispatch_unknown")
                and type(status) is int and 400 <= status < 500
                and type(diagnostics) is list
                and any(type(item) is dict and (
                    item.get("code") == code or item.get("reason_code") == code) for item in diagnostics))

    def execute(self, bound, operation, parameters):
        original = self._original(bound, operation, parameters)
        if self._existing(bound, operation, original) is not None:
            return self.read_receipt(bound, operation, original)
        private = FRONTEND_NODE_EVIDENCE_OPERATIONS[operation]
        owner_kind, owner_id = frontend_node_evidence_owner(private, original)
        record = self._evidence.write_evidence(
            operation=private, idempotency_key=original["parameters"]["idempotency_key"],
            owner_kind=owner_kind, owner_id=owner_id, request=deepcopy(original))
        require(record["resolution"] == "pending" and canonical_bytes(record["request"]) == canonical_bytes(original),
                "request_evidence_conflict", "Saved frontend evidence differs from the original request", 409)
        try:
            result = getattr(bound, operation)(**deepcopy(original["parameters"]))
        except ContractValidationError as exc:
            if self._known_rejection(exc):
                self._resolve(record, "archived")
            raise
        if self._accepted(operation, result):
            self._resolve(record, "verified")
        elif type(result) is dict and result.get("operation") == operation and result.get("outcome") == "failed":
            self._resolve(record, "archived")
        return deepcopy(result)

    def read_receipt(self, bound, operation, original_request):
        original_request = deepcopy(original_request)
        require(type(original_request) is dict and set(original_request) == {"owner", "parameters"}
                and original_request["owner"] == self._owner(bound),
                "request_evidence_conflict", "Original frontend request belongs to another bound owner", 409)
        original = self._original(bound, operation, original_request["parameters"])
        require(self._existing(bound, operation, original) is not None,
                "request_evidence_missing", "Original frontend request evidence was not found", 404)
        return deepcopy(bound.read_receipt(operation, deepcopy(original["parameters"])))

    def pending(self, bound):
        owner = self._owner(bound)
        _validate_owner(owner)
        records = []
        for operation, private in FRONTEND_NODE_EVIDENCE_OPERATIONS.items():
            identity = {"owner": owner, "parameters": {}}
            records.extend(record for record in self._records(bound, operation, identity)
                           if record["resolution"] == "pending")
        return deepcopy(records)

    def accept_receipt(self, bound, operation, original_request):
        result = self.read_receipt(bound, operation, original_request)
        require(self._accepted(operation, result), "frontend_node_receipt_unresolved",
                "Frontend evidence requires its actual accepted native receipt", 409)
        original = self._original(bound, operation, original_request["parameters"])
        self._resolve(self._existing(bound, operation, original), "verified")
        return deepcopy(result)
