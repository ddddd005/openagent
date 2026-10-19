"""Private frontend-node communication with exact owners and narrow results."""

from contextlib import closing
from copy import deepcopy

from .configuration_mutations import validate_editable_input_configuration
from .contract_errors import ContractValidationError, require, uuid4_string
from .contract_json import validate_json_value
from .frontend_node_evidence import FrontendNodeEvidenceAdapter
from .frontend_node_operations import FrontendNodeOperations
from .graph_store import GraphRecordStore


_OWNER_FIELDS = {"workflow_definition_id", "definition_revision", "node_binding_id", "session_id"}
_COMMANDS = {"patch_input", "start", "pause", "resume", "close", "fork_checkpoint",
             "reroll", "select_candidate", "fork_floor"}
_CHAT_RECEIPT_OPERATIONS = {"reroll", "select_candidate", "fork_floor"}
_CHAT_FIELDS = {"round_id", "expected_session_revision", "expected_data_revision",
                "expected_head_revision", "expected_object_revision", "idempotency_key"}
_MAX_REVISION = 2**53 - 1


def _owner(value):
    require(type(value) is dict and set(value) == _OWNER_FIELDS,
            "frontend_node_invalid_binding", "Frontend communication requires an exact owner")
    uuid4_string(value["workflow_definition_id"])
    uuid4_string(value["node_binding_id"])
    require(type(value["definition_revision"]) is int
            and 1 <= value["definition_revision"] <= _MAX_REVISION,
            "frontend_node_invalid_binding", "Frontend definition revision is invalid")
    if value["session_id"] is not None:
        uuid4_string(value["session_id"])
    return deepcopy(value)


def _key(value):
    require(type(value) is str and 1 <= len(value) <= 128 and bool(value.strip()),
            "invalid_request", "Frontend commands require an idempotency key")


class FrontendNodeChannel:
    def __init__(self, service, owner, *, evidence_adapter=None):
        self._service = service
        self._owner = _owner(owner)
        self._bound = None
        self._evidence = (evidence_adapter if evidence_adapter is not None
                          else FrontendNodeEvidenceAdapter(service.database))

    def _bind(self):
        if self._bound is None:
            self._bound = FrontendNodeOperations(self._service).bind(**self._owner)
        return self._bound

    def _require_chat_receipt_binding(self, bound):
        with self._service._lock, self._service._configuration_data.read() as data:
            node = bound._require_binding(data)
        require(node["component_version"] == "2",
                "frontend_node_chat_unavailable",
                "Chat receipts require frontend.node@2 and an exact session", 409)

    def _parameters(self, operation, value):
        require(type(value) is dict, "invalid_request", "Frontend parameters must be an object")
        fields = {
            "patch_input": {"text", "expected_edit_sequence", "idempotency_key"},
            "start": {"expected_session_revision", "idempotency_key"},
            "pause": {"expected_session_revision", "idempotency_key"},
            "resume": {"expected_session_revision", "idempotency_key"},
            "close": {"expected_session_revision", "idempotency_key"},
            "fork_checkpoint": {"checkpoint_id", "expected_session_revision", "expected_data_revision",
                                "expected_head_revision", "idempotency_key"},
            "reroll": _CHAT_FIELDS,
            "select_candidate": _CHAT_FIELDS | {"candidate_id"},
            "fork_floor": _CHAT_FIELDS,
        }[operation]
        optional = {"confirm_derivation"} if operation == "patch_input" else set()
        require(fields <= set(value) <= fields | optional,
                "invalid_request", "Frontend command parameters differ")
        _key(value["idempotency_key"])
        if operation == "patch_input":
            validate_editable_input_configuration(
                {"text": value["text"]}, node_id=self._owner["node_binding_id"])
            require(type(value["expected_edit_sequence"]) is int
                    and 0 <= value["expected_edit_sequence"] <= _MAX_REVISION,
                    "invalid_request", "Frontend edit sequence is invalid")
            require("confirm_derivation" not in value or type(value["confirm_derivation"]) is bool,
                    "invalid_request", "Frontend derivation confirmation must be boolean")
        else:
            revision = value["expected_session_revision"]
            require((operation == "start" and self._owner["session_id"] is None and revision is None)
                    or self._owner["session_id"] is not None and type(revision) is int
                    and 1 <= revision <= _MAX_REVISION,
                    "invalid_request", "Frontend session revision does not match its binding")
            if operation in ("fork_checkpoint", "reroll", "select_candidate", "fork_floor"):
                uuid4_string(value["checkpoint_id"] if operation == "fork_checkpoint" else value["round_id"])
                if operation == "select_candidate":
                    uuid4_string(value["candidate_id"])
                revisions = [("expected_data_revision", 0), ("expected_head_revision", 1)]
                if operation != "fork_checkpoint":
                    revisions.append(("expected_object_revision", 1))
                for field, minimum in revisions:
                    require(type(value[field]) is int and minimum <= value[field] <= _MAX_REVISION,
                            "invalid_request", "Frontend checkpoint revision is invalid")
        return deepcopy(value)

    def handle(self, request):
        request = deepcopy(request)
        validate_json_value(request)
        require(type(request) is dict and set(request) == {"owner", "operation", "parameters"},
                "invalid_request", "Frontend communication fields differ")
        owner = _owner(request["owner"])
        require(owner == self._owner, "frontend_node_owner_mismatch",
                "Communication belongs to another frontend node binding", 409)
        operation, parameters = request["operation"], request["parameters"]
        require(type(operation) is str
                and operation in {"state", "checkpoints", "checkpoint", "receipt", "accept_receipt"} | _COMMANDS,
                "invalid_request", "Frontend operation is not allowed")
        if operation == "state":
            require(type(parameters) is dict and not parameters,
                    "invalid_request", "Frontend state accepts no selectors")
            return self._state(self._bind())
        if operation in ("checkpoints", "checkpoint"):
            fields = set() if operation == "checkpoints" else {"checkpoint_id"}
            require(type(parameters) is dict and set(parameters) == fields,
                    "invalid_request", "Frontend checkpoint read parameters differ")
            bound = self._bind()
            if operation == "checkpoints":
                data = bound.list_checkpoints()
                session = data["session"]
                result = {
                    "items": data["items"],
                    "basis": {name: session[name] for name in ("revision", "data_revision", "head_revision")},
                }
            else:
                uuid4_string(parameters["checkpoint_id"])
                result = bound.read_checkpoint(parameters["checkpoint_id"])
            return {"schema_version": 1, "kind": "workflow.frontend-node-" + operation,
                    "owner": deepcopy(self._owner), "result": result}
        if operation in ("receipt", "accept_receipt"):
            require(type(parameters) is dict and set(parameters) == {"operation", "original_request"}
                    and type(parameters["operation"]) is str and parameters["operation"] in _COMMANDS,
                    "invalid_request", "Frontend receipt requires an exact allowed original operation")
            receipt_operation = operation
            operation = parameters["operation"]
            original_request = parameters["original_request"]
            require(type(original_request) is dict and set(original_request) == {"owner", "parameters"},
                    "invalid_request", "Frontend receipt requires its complete original request")
            require(_owner(original_request["owner"]) == owner,
                    "frontend_node_owner_mismatch", "Original request belongs to another frontend binding", 409)
            original = self._parameters(operation, original_request["parameters"])
            if operation in _CHAT_RECEIPT_OPERATIONS:
                bound = self._bind()
                self._require_chat_receipt_binding(bound)
            elif self._bound is not None:
                bound = self._bound
            else:
                bound = FrontendNodeOperations(self._service).receipt_scope(**owner)
            if receipt_operation == "accept_receipt":
                raw = self._evidence.accept_receipt(
                    bound, operation, original_request)
            else:
                try:
                    raw = self._evidence.read_receipt(
                        bound, operation, original_request)
                except ContractValidationError as exc:
                    if getattr(exc, "reason_code", None) != "request_evidence_missing":
                        raise
                    raw = None
            return self._result(operation, original, raw)
        bound = self._bind()
        original = self._parameters(operation, parameters)
        raw = self._evidence.execute(bound, operation, original)
        return self._result(operation, original, raw)

    def _state(self, bound):
        with self._service._lock, closing(self._service._store()) as store:
            self._service._check_open()
            repo = GraphRecordStore(store)
            data = self._service._configuration_data.transaction(repo)
            bound._require_binding(data, current=True)
            document = self._service._configuration_view(data, self._owner["workflow_definition_id"])
            require(document["revision"] == self._owner["definition_revision"],
                    "stale_revision", "Frontend current definition revision changed", 409)
            node = next(item for item in document["nodes"]
                        if item["node_binding_id"] == self._owner["node_binding_id"])
            input_value = None
            target_id = node["config"]["input_node_id"]
            if target_id:
                target = next((item for item in document["nodes"]
                               if item["node_binding_id"] == target_id), None)
                require(target is not None
                        and (target["component_id"], target["component_version"]) == ("tools.editable-input", "1"),
                        "frontend_node_input_mismatch", "Frontend input binding is not editable-input@1", 409)
                validate_editable_input_configuration(target["config"], node_id=target_id)
                input_value = {
                    "text": target["config"]["text"], "edit_sequence": document["edit_sequence"],
                    "policy": target["configuration_policy"]["prompt_mode"],
                }
            sid = self._owner["session_id"]
            run, snapshot = None, None
            if sid is not None:
                run_data = self._service._data_management.execution_read(
                    repo, registry=self._service.registry)
                view = self._service._view(run_data, sid)
                pausing = (view["status"] in ("prepared", "running")
                           and view["active_chain_run_id"] in self._service._pauses)
                run = {
                    "revision": view["revision"], "status": "pausing" if pausing else view["status"],
                    "can_start": view["can_submit"],
                    "can_pause": not pausing and "pause" in view["available_actions"],
                    "can_resume": "resume" in view["available_actions"],
                    "can_close": "close" in view["available_actions"],
                    "can_fork": view["can_submit"],
                }
                if node["component_version"] == "2":
                    run.update(
                        data_revision=view["data_revision"], head_revision=view["head_revision"],
                        object_revision=view["objects"][node["config"]["object_key"]]["revision"])
                snapshot = self._service._read_frontend_snapshot(
                    run_data, self._owner["workflow_definition_id"], self._owner["definition_revision"],
                    self._owner["node_binding_id"], sid)
        return {
            "schema_version": 1, "kind": "workflow.frontend-node-state",
            "owner": deepcopy(self._owner), "input": input_value, "run": run, "snapshot": snapshot,
            "pending": self._evidence.pending(bound),
        }

    def _phase(self, view):
        if view is None:
            return None
        require(type(view) is dict
                and view["workflow_definition_id"] == self._owner["workflow_definition_id"]
                and view["definition_revision"] == self._owner["definition_revision"],
                "frontend_node_result_mismatch", "Native result belongs to another frontend definition", 409)
        uuid4_string(view["workflow_session_id"])
        require(self._owner["session_id"] is None
                or view["workflow_session_id"] == self._owner["session_id"],
                "frontend_node_result_mismatch", "Native result belongs to another session", 409)
        return {key: deepcopy(view[key]) for key in (
            "workflow_definition_id", "definition_revision", "workflow_session_id",
            "revision", "status", "active_chain_run_id",
        )}

    def _patch_projection(self, raw):
        if raw is None:
            return {"outcome": "unknown", "edit_sequence": None,
                    "definition_revision": self._owner["definition_revision"], "text": None, "next_owner": None}
        require(raw["source_workflow_definition_id"] == self._owner["workflow_definition_id"],
                "frontend_node_result_mismatch", "Configuration result belongs to another workflow", 409)
        document = raw["document"]
        mapped_id = raw["node_id_map"].get(self._owner["node_binding_id"])
        frontend = next((item for item in document["nodes"] if item["node_binding_id"] == mapped_id), None)
        require(frontend is not None
                and (frontend["component_id"], frontend["component_version"])
                in (("frontend.node", "1"), ("frontend.node", "2")),
                "frontend_node_result_mismatch", "Configuration result lost its frontend node", 409)
        target = next((item for item in document["nodes"]
                       if item["node_binding_id"] == frontend["config"]["input_node_id"]), None)
        require(target is not None
                and (target["component_id"], target["component_version"]) == ("tools.editable-input", "1"),
                "frontend_node_result_mismatch", "Configuration result lost its editable input", 409)
        validate_editable_input_configuration(target["config"], node_id=target["node_binding_id"])
        next_owner = {
            "workflow_definition_id": raw["workflow_definition_id"],
            "definition_revision": raw["definition_revision"], "node_binding_id": mapped_id,
            "session_id": None if raw["outcome"] == "derived" else self._owner["session_id"],
        }
        return {
            "outcome": raw["outcome"], "edit_sequence": raw["edit_sequence"],
            "definition_revision": raw["definition_revision"], "text": target["config"]["text"],
            "next_owner": None if next_owner == self._owner else _owner(next_owner),
        }

    def _run_projection(self, operation, raw):
        if raw is None:
            raw = {
                "outcome": "unknown", "failure_phase": "create" if operation == "start"
                and self._owner["session_id"] is None else operation, "error": None,
            }
        names = ("create_result", "start_result") if operation == "start" else ("control_result",)
        phases = {name: self._phase(raw.get(name)) for name in names}
        sessions = {value["workflow_session_id"] for value in phases.values() if value is not None}
        require(len(sessions) <= 1, "frontend_node_result_mismatch",
                "Frontend command phases belong to different sessions", 409)
        next_owner = None
        if sessions and self._owner["session_id"] is None:
            next_owner = {**self._owner, "session_id": next(iter(sessions))}
        return {
            "outcome": raw["outcome"], **phases, "failure_phase": raw["failure_phase"],
            "error": deepcopy(raw["error"]), "next_owner": next_owner,
        }

    def _result(self, operation, original, raw):
        if operation == "patch_input":
            result = self._patch_projection(raw)
        elif operation in ("fork_checkpoint", "fork_floor"):
            result = self._fork_projection(raw, operation=operation)
        else:
            result = self._run_projection(operation, raw)
        return {
            "schema_version": 1, "kind": "workflow.frontend-node-result", "owner": deepcopy(self._owner),
            "operation": operation, "idempotency_key": original["idempotency_key"],
            "outcome": result["outcome"], "result": result,
        }

    def _fork_projection(self, raw, *, operation="fork_checkpoint"):
        if raw is None:
            raw = {"outcome": "unknown", "fork_result": None, "failure_phase": operation, "error": None}
        target = raw["fork_result"]
        next_owner = None
        if target is not None:
            require(type(target) is dict and set(target) == {
                "target_session_id", "target_checkpoint_id", "target_commit_id", "target_workflow_ref_id"},
                "frontend_node_result_mismatch", "Fork receipt must contain the exact target identities", 409)
            for identity in target.values():
                uuid4_string(identity)
            require(target["target_session_id"] != self._owner["session_id"],
                    "frontend_node_result_mismatch", "Fork must initialize an independent session", 409)
            next_owner = {**self._owner, "session_id": target["target_session_id"]}
        return {
            "outcome": raw["outcome"], "fork_result": deepcopy(target), "failure_phase": raw["failure_phase"],
            "error": deepcopy(raw["error"]), "next_owner": next_owner,
        }
