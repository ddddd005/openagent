"""Node-independent chat round rules and calls to workflow authorities."""

from copy import deepcopy

from .chat_contracts import validate_chat_context
from .contract_errors import ContractValidationError, require
from .workflow_operations import read_native_receipt


def _round(value, round_id, *, current=False):
    result = validate_chat_context(value)
    index = next((index for index, item in enumerate(result["rounds"])
                  if item["round_id"] == round_id), None)
    require(index is not None, "chat_round_not_found", "Chat round is unavailable", 404)
    require(not current or index == len(result["rounds"]) - 1,
            "chat_round_locked", "A chat round with later rounds cannot change", 409)
    return result, result["rounds"][index], index


def _candidate(round_value, candidate_id):
    candidate = next((item for item in round_value["candidates"]
                      if item["candidate_id"] == candidate_id), None)
    require(candidate is not None, "chat_candidate_not_found",
            "Candidate does not belong to this chat round", 404)
    require(candidate["result_checkpoint_id"] is not None,
            "chat_candidate_uncommitted", "Candidate has no accepted success checkpoint", 409)
    return candidate


def begin_chat_round(value, *, round_id, base_checkpoint_id, submitted_input):
    """Add one submitted input; the presence of this round locks its predecessor."""
    result = validate_chat_context(value)
    require(all(item["round_id"] != round_id for item in result["rounds"]),
            "chat_round_exists", "A submitted round cannot be registered twice", 409)
    if result["rounds"]:
        previous = result["rounds"][-1]
        selected = _candidate(previous, previous["selected_candidate_id"])
        require(base_checkpoint_id == selected["result_checkpoint_id"],
                "chat_round_basis_mismatch",
                "A new round must use the selected candidate's success checkpoint", 409)
    result["rounds"].append({
        "round_id": round_id, "base_checkpoint_id": base_checkpoint_id,
        "submitted_input": submitted_input, "candidates": [], "selected_candidate_id": None,
    })
    return validate_chat_context(result)


def plan_chat_reroll(value, round_id):
    """Read the same submitted input and fixed pre-round checkpoint for another run."""
    _, current, _ = _round(value, round_id, current=True)
    return {field: deepcopy(current[field]) for field in (
        "round_id", "base_checkpoint_id", "submitted_input",
    )}


def select_chat_candidate(value, *, round_id, candidate_id):
    """Change only the current round's selected successful candidate."""
    result, current, _ = _round(value, round_id, current=True)
    _candidate(current, candidate_id)
    current["selected_candidate_id"] = candidate_id
    return validate_chat_context(result)


def plan_chat_continuation(value):
    """Return the selected computation checkpoint, separately from storage's latest point."""
    result = validate_chat_context(value)
    if not result["rounds"]:
        return None
    current = result["rounds"][-1]
    candidate = _candidate(current, current["selected_candidate_id"])
    return {
        "round_id": current["round_id"], "candidate_id": candidate["candidate_id"],
        "result_checkpoint_id": candidate["result_checkpoint_id"],
    }


def plan_chat_floor_fork(value, round_id):
    """Keep the whole requested floor and all candidate points while dropping later floors."""
    result, current, index = _round(value, round_id)
    selected = _candidate(current, current["selected_candidate_id"])
    successful = [candidate for candidate in current["candidates"]
                  if candidate["result_checkpoint_id"] is not None]
    result["rounds"] = result["rounds"][:index + 1]
    return {
        "round_id": round_id, "chat_context": validate_chat_context(result),
        "copy_through_checkpoint_id": successful[-1]["result_checkpoint_id"],
        "selected_checkpoint_id": selected["result_checkpoint_id"],
    }


class WorkflowChatOperations:
    """Trusted local functionality; consumer adapters own their exact-node authorization."""

    def __init__(self, service):
        self._service = service

    def execute_operation(
        self, operation, session_id, *, parameters, idempotency_key,
        receipt_operation, receipt_request, verify, dispatch_key,
    ):
        """Freeze a trusted adapter's dispatch before invoking a workflow command."""
        require(operation in ("reroll", "select_candidate", "fork_floor"),
                "invalid_request", "Unknown chat operation")
        accepted = []

        def change(repo):
            verify(repo)
            accepted.append(True)
            return {"dispatch_key": dispatch_key,
                    "dispatch_request": {"session": session_id, **deepcopy(parameters)}}

        plan = self._service._change(
            receipt_operation, idempotency_key, deepcopy(receipt_request), change)
        if accepted:
            try:
                arguments = deepcopy(plan["dispatch_request"])
                sid = arguments.pop("session")
                callback = self.select if operation == "select_candidate" else getattr(self, operation)
                callback(sid, **arguments, idempotency_key=plan["dispatch_key"])
            except Exception as exc:
                error = (
                    {"reason_code": exc.reason_code, "diagnostics": deepcopy(exc.diagnostics)}
                    if isinstance(exc, ContractValidationError) and hasattr(exc, "reason_code")
                    and hasattr(exc, "diagnostics") else {"reason_code": "workflow_operation_dispatch_unknown"})
                return self.read_operation_result(operation, plan, error=error)
        return self.read_operation_result(operation, plan)

    def read_operation_result(self, operation, plan, *, error=None):
        native_operation = {
            "reroll": "graph.chat.reroll", "select_candidate": "graph.chat.select",
            "fork_floor": "graph.chat.fork-floor",
        }[operation]
        result = read_native_receipt(
            self._service, native_operation, plan["dispatch_key"], plan["dispatch_request"])
        outcome = "succeeded" if result is not None else (
            "failed" if error is not None and error["reason_code"] != "workflow_operation_dispatch_unknown"
            else "unknown")
        return {
            "operation": operation, "outcome": outcome,
            "fork_result" if operation == "fork_floor" else "control_result": result,
            "failure_phase": None if result is not None else operation,
            "error": error if result is None else None,
        }

    @staticmethod
    def _guards(
        object_key, expected_session_revision, expected_data_revision,
        expected_head_revision, expected_object_revision, idempotency_key,
    ):
        return {
            "object_key": object_key, "expected_session_revision": expected_session_revision,
            "expected_data_revision": expected_data_revision, "expected_head_revision": expected_head_revision,
            "expected_object_revision": expected_object_revision, "idempotency_key": idempotency_key,
        }

    def submit(
        self, session_id, *, object_key, submitted_input, expected_session_revision,
        expected_data_revision, expected_head_revision, expected_object_revision,
        idempotency_key, inputs=None, input_overrides=None,
    ):
        return self._service.chat_submit(
            session_id, submitted_input=submitted_input,
            inputs={} if inputs is None else deepcopy(inputs),
            input_overrides={} if input_overrides is None else deepcopy(input_overrides),
            **self._guards(
                object_key, expected_session_revision, expected_data_revision,
                expected_head_revision, expected_object_revision, idempotency_key))

    def reroll(
        self, session_id, *, object_key, round_id, expected_session_revision,
        expected_data_revision, expected_head_revision, expected_object_revision, idempotency_key,
    ):
        return self._service.chat_reroll(
            session_id, round_id=round_id, **self._guards(
                object_key, expected_session_revision, expected_data_revision,
                expected_head_revision, expected_object_revision, idempotency_key))

    def select(
        self, session_id, *, object_key, round_id, candidate_id, expected_session_revision,
        expected_data_revision, expected_head_revision, expected_object_revision, idempotency_key,
    ):
        return self._service.chat_select(
            session_id, round_id=round_id, candidate_id=candidate_id, **self._guards(
                object_key, expected_session_revision, expected_data_revision,
                expected_head_revision, expected_object_revision, idempotency_key))

    def fork_floor(
        self, session_id, *, object_key, round_id, expected_session_revision,
        expected_data_revision, expected_head_revision, expected_object_revision, idempotency_key,
    ):
        return self._service.chat_fork_floor(
            session_id, round_id=round_id, **self._guards(
                object_key, expected_session_revision, expected_data_revision,
                expected_head_revision, expected_object_revision, idempotency_key))
