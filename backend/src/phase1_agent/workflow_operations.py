"""Reusable workflow operations over the existing transactional authorities."""

from contextlib import closing
from copy import deepcopy

from .contract_errors import ContractValidationError, require, uuid4_string
from .contract_json import content_digest, validate_json_value


_MAX_REVISION = 2**53 - 1
_UNKNOWN = "workflow_operation_dispatch_unknown"
_CONTROL_ACTIONS = frozenset(("pause", "resume", "close"))


def _revision(value, *, nullable=False, zero=False):
    minimum = 0 if zero else 1
    require(nullable and value is None
            or type(value) is int and minimum <= value <= _MAX_REVISION,
            "invalid_request", "Expected workflow revision is invalid")


def read_native_receipt(service, operation, key, request):
    """Observe an exact original service receipt without dispatching its command."""
    require(type(key) is str and 1 <= len(key) <= 128 and bool(key.strip()),
            "invalid_request", "An idempotency key is required")
    expected = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
    with service._lock, closing(service._store()) as store:
        service._check_service_open()
        prior = store.read_receipt_with_digest(operation, key)
        if prior is None:
            return None
        require(prior[0] == expected, "idempotency_conflict",
                "The key has a different request", 409)
        require(type(prior[1]) is list and len(prior[1]) == 1
                and type(prior[1][0]) is dict and set(prior[1][0]) == {"graph_result"},
                "storage_contract_violation", "Invalid graph receipt", 500)
        return deepcopy(prior[1][0]["graph_result"])


def _failure(exc):
    if isinstance(exc, ContractValidationError) and hasattr(exc, "reason_code") and hasattr(exc, "diagnostics"):
        return {"reason_code": exc.reason_code, "diagnostics": deepcopy(exc.diagnostics)}
    return {"reason_code": _UNKNOWN}


def _outcome(result, error):
    return "succeeded" if result is not None else (
        "failed" if error is not None and error["reason_code"] != _UNKNOWN else "unknown")


class WorkflowOperations:
    """Workflow functionality independent of nodes, pages and transports.

    A trusted adapter may provide its existing request namespace and verification
    callback. The callback runs while the plan is first recorded, never on replay.
    Native commands remain the authority for state changes and their receipts.
    """

    def __init__(self, service):
        self._service = service

    @staticmethod
    def _key(operation, key, request):
        return "workflow-operation:" + content_digest({
            "operation": operation, "idempotency_key": key, "request": request,
        }).rsplit(":", 1)[1]

    def plan_start(
        self, *, workflow_definition_id, definition_revision, session_id=None,
        expected_session_revision=None, inputs=None, idempotency_key,
        receipt_operation="graph.workflow-operation.start", receipt_request=None,
        verify=None, create_key=None, dispatch_key=None,
    ):
        inputs = {} if inputs is None else deepcopy(inputs)
        require(type(inputs) is dict, "invalid_request", "Workflow inputs must be an object")
        validate_json_value(inputs)
        request = deepcopy(receipt_request) if receipt_request is not None else {
            "workflow_definition_id": workflow_definition_id,
            "definition_revision": definition_revision, "session_id": session_id,
            "expected_session_revision": expected_session_revision,
            **({"inputs": deepcopy(inputs)} if inputs else {}),
        }
        accepted = []

        def change(repo):
            if verify is not None:
                verify(repo)
            uuid4_string(workflow_definition_id)
            _revision(definition_revision)
            _revision(expected_session_revision, nullable=session_id is None)
            self._service._require_definition_active(repo, workflow_definition_id)
            if session_id is None:
                require(expected_session_revision is None, "invalid_request",
                        "Starting a new session requires an empty session revision")
                current = repo.current_configuration(workflow_definition_id)
                require(current is None or current["fixed_definition_revision"] == definition_revision,
                        "stale_revision", "The workflow fixed definition changed", 409)
                self._service._document(repo, workflow_definition_id, definition_revision)
            else:
                uuid4_string(session_id)
                session = repo.get("workflow_session", workflow_session_id=session_id)
                self._service._require_session_active(repo, session)
                require(session["workflow_definition_id"] == workflow_definition_id
                        and session["definition_revision"] == definition_revision,
                        "workflow_operation_session_mismatch",
                        "Session belongs to another workflow definition", 409)
                require(session["revision"] == expected_session_revision,
                        "stale_revision", "Session changed", 409)
            accepted.append(True)
            plan = {
                "create_request": ({
                    "workflow_definition_id": workflow_definition_id,
                    "definition_revision": definition_revision,
                } if session_id is None else None),
                "create_key": create_key or self._key("create", idempotency_key, request),
                "dispatch_key": dispatch_key or self._key("start", idempotency_key, request),
                "dispatch_request": ({
                    "session": session_id, "expected_revision": expected_session_revision,
                    "inputs": deepcopy(inputs),
                } if session_id is not None else None),
            }
            if session_id is None and inputs:
                plan["start_inputs"] = deepcopy(inputs)
            return plan

        plan = self._service._change(receipt_operation, idempotency_key, request, change)
        return plan, bool(accepted)

    def _created(self, plan):
        request = plan["create_request"]
        if request is None:
            return None
        result = read_native_receipt(self._service, "graph.session.create", plan["create_key"], request)
        if result is not None:
            require(result["workflow_definition_id"] == request["workflow_definition_id"]
                    and result["definition_revision"] == request["definition_revision"],
                    "storage_contract_violation",
                    "Created session receipt differs from the original workflow request", 500)
            uuid4_string(result["workflow_session_id"])
            _revision(result["revision"])
        return result

    def read_start_result(self, plan, *, error=None):
        created = self._created(plan)
        dispatch = plan["dispatch_request"]
        if plan["create_request"] is not None and created is not None:
            dispatch = {"session": created["workflow_session_id"],
                        "expected_revision": created["revision"],
                        "inputs": deepcopy(plan.get("start_inputs", {}))}
        started = (read_native_receipt(
            self._service, "graph.run.start", plan["dispatch_key"], dispatch)
            if dispatch is not None else None)
        phase = None if started is not None else "create" if dispatch is None else "start"
        return {
            "operation": "start", "outcome": _outcome(started, error),
            "create_result": created, "start_result": started, "failure_phase": phase,
            "error": error if phase is not None else None,
        }

    def execute_start(self, plan, accepted):
        if not accepted:
            return self.read_start_result(plan)
        try:
            if plan["create_request"] is not None:
                self._service.create_session(
                    **plan["create_request"], idempotency_key=plan["create_key"])
                created = self._created(plan)
                require(created is not None, "storage_contract_violation",
                        "Session creation returned without its original receipt", 500)
                dispatch = {"session": created["workflow_session_id"],
                            "expected_revision": created["revision"],
                            "inputs": deepcopy(plan.get("start_inputs", {}))}
            else:
                dispatch = plan["dispatch_request"]
            self._service.start(
                dispatch["session"], expected_revision=dispatch["expected_revision"],
                inputs=dispatch["inputs"], idempotency_key=plan["dispatch_key"])
        except Exception as exc:
            return self.read_start_result(plan, error=_failure(exc))
        return self.read_start_result(plan)

    def start(self, **parameters):
        return self.execute_start(*self.plan_start(**parameters))

    def plan_control(
        self, *, session_id, action, expected_session_revision, idempotency_key,
        receipt_operation=None, receipt_request=None, verify=None,
        create_key=None, dispatch_key=None,
    ):
        require(type(action) is str and action in _CONTROL_ACTIONS,
                "invalid_request", "Unknown workflow operation")
        request = deepcopy(receipt_request) if receipt_request is not None else {
            "session_id": session_id, "action": action,
            "expected_session_revision": expected_session_revision,
        }
        accepted = []

        def change(repo):
            if verify is not None:
                verify(repo)
            uuid4_string(session_id)
            _revision(expected_session_revision)
            session = repo.get("workflow_session", workflow_session_id=session_id)
            self._service._require_session_active(repo, session)
            require(session["revision"] == expected_session_revision,
                    "stale_revision", "Session changed", 409)
            accepted.append(True)
            return {
                "create_request": None,
                "create_key": create_key or self._key("create", idempotency_key, request),
                "dispatch_key": dispatch_key or self._key(action, idempotency_key, request),
                "dispatch_request": {
                    "session": session_id, "expected_revision": expected_session_revision, "action": action,
                },
            }

        plan = self._service._change(
            receipt_operation or "graph.workflow-operation." + action, idempotency_key, request, change)
        return plan, bool(accepted)

    def read_control_result(self, plan, *, operation=None, error=None):
        operation = operation or plan["dispatch_request"]["action"]
        controlled = read_native_receipt(
            self._service, "graph.run.control", plan["dispatch_key"], plan["dispatch_request"])
        return {
            "operation": operation, "outcome": _outcome(controlled, error),
            "control_result": controlled,
            "failure_phase": None if controlled is not None else operation,
            "error": error if controlled is None else None,
        }

    def execute_control(self, plan, accepted, *, operation=None):
        if accepted:
            try:
                dispatch = plan["dispatch_request"]
                self._service.control(
                    dispatch["session"], action=dispatch["action"],
                    expected_revision=dispatch["expected_revision"],
                    idempotency_key=plan["dispatch_key"])
            except Exception as exc:
                return self.read_control_result(plan, operation=operation, error=_failure(exc))
        return self.read_control_result(plan, operation=operation)

    def control(self, **parameters):
        return self.execute_control(*self.plan_control(**parameters))

    def plan_fork(
        self, *, session_id, checkpoint_id, expected_session_revision, expected_data_revision,
        expected_head_revision, idempotency_key,
        receipt_operation="graph.workflow-operation.fork-checkpoint", receipt_request=None,
        verify=None, dispatch_key=None,
    ):
        dispatch = {
            "session": session_id, "checkpoint_id": checkpoint_id,
            "expected_session_revision": expected_session_revision,
            "expected_data_revision": expected_data_revision,
            "expected_head_revision": expected_head_revision,
        }
        request = deepcopy(receipt_request) if receipt_request is not None else {
            "session_id": session_id, **{name: value for name, value in dispatch.items() if name != "session"},
        }
        accepted = []

        def change(repo):
            if verify is not None:
                verify(repo)
            uuid4_string(session_id)
            uuid4_string(checkpoint_id)
            _revision(expected_session_revision)
            _revision(expected_data_revision, zero=True)
            _revision(expected_head_revision)
            self._service._stable(
                repo, session_id, expected_session_revision, expected_data_revision, expected_head_revision)
            accepted.append(True)
            return {
                "dispatch_key": dispatch_key or self._key("fork-checkpoint", idempotency_key, request),
                "dispatch_request": deepcopy(dispatch),
            }

        plan = self._service._change(receipt_operation, idempotency_key, request, change)
        return plan, bool(accepted)

    def read_fork_result(self, plan, *, error=None):
        result = read_native_receipt(
            self._service, "graph.checkpoint.fork", plan["dispatch_key"], plan["dispatch_request"])
        return {
            "operation": "fork_checkpoint", "outcome": _outcome(result, error),
            "fork_result": result, "failure_phase": None if result is not None else "fork_checkpoint",
            "error": error if result is None else None,
        }

    def execute_fork(self, plan, accepted):
        if accepted:
            try:
                dispatch = deepcopy(plan["dispatch_request"])
                session_id = dispatch.pop("session")
                self._service.fork_checkpoint(
                    session_id, **dispatch, idempotency_key=plan["dispatch_key"])
            except Exception as exc:
                return self.read_fork_result(plan, error=_failure(exc))
        return self.read_fork_result(plan)

    def fork_checkpoint(self, **parameters):
        return self.execute_fork(*self.plan_fork(**parameters))

    def list_checkpoints(self, session_id):
        return self._service.list_checkpoints(session_id)

    def read_checkpoint(self, session_id, checkpoint_id):
        return self._service.read_checkpoint(session_id, checkpoint_id=checkpoint_id)

    def read_session_state(self, session_id):
        state = self._service.get_session(session_id)
        return {name: deepcopy(state[name]) for name in (
            "workflow_definition_id", "definition_revision", "workflow_session_id", "revision",
            "data_revision", "head_revision", "status", "can_submit", "available_actions",
        )}
