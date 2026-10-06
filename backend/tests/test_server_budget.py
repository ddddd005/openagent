"""Strict HTTP boundary for explicit, non-dispatching run budget extension."""

import http.client
import json
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import dumps_pretty
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.server import create_server
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import WorkflowService


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="http-budget-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


class BudgetService:
    def __init__(self):
        self.calls = []
        self.receipts = {}
        self.revision = 3
        self.run_revision = 7

    def extend_budget(self, session_id, run_id, **arguments):
        self.calls.append((session_id, run_id, arguments))
        key = arguments["idempotency_key"]
        if key in self.receipts:
            original, receipt = self.receipts[key]
            if original != (session_id, run_id, arguments):
                raise ContractValidationError("private idempotency mismatch")
            return receipt
        if (
            arguments["expected_session_revision"] != self.revision
            or arguments["expected_run_revision"] != self.run_revision
        ):
            raise ContractValidationError("private stale revision")
        self.revision += 1
        self.run_revision += 1
        receipt = {"run_id": run_id, "status": "failed", "operation_id": "op-budget"}
        self.receipts[key] = ((session_id, run_id, arguments), receipt)
        return receipt


@contextmanager
def running_server(service):
    with create_server(service, port=0) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def request(port, method, path, payload=None, *, origin=True, headers=None):
    request_headers = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    body = None
    if method == "POST":
        request_headers["Content-Type"] = "application/json"
        if origin:
            request_headers["Origin"] = f"http://127.0.0.1:{port}"
        body = json.dumps(payload).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def budget_payload(**changes):
    return {
        "idempotency_key": "budget-once",
        "expected_session_revision": 3,
        "expected_run_revision": 7,
        "additional_model_requests": 1,
        "additional_model_attempts": 4,
        **changes,
    }


def test_budget_route_requires_exact_fields_and_strict_integer_increments():
    service = BudgetService()
    payload = budget_payload()
    path = "/api/sessions/session_1/runs/run_1/extend_budget"
    with running_server(service) as port:
        for invalid in (
            *({key: value for key, value in payload.items() if key != field}
              for field in payload),
            {**payload, "unexpected": True},
            {**payload, "idempotency_key": ""},
            {**payload, "idempotency_key": "x" * 129},
            {**payload, "idempotency_key": 1},
            {**payload, "expected_session_revision": True},
            {**payload, "expected_session_revision": 0},
            {**payload, "expected_run_revision": False},
            {**payload, "expected_run_revision": "7"},
            {**payload, "additional_model_requests": -1},
            {**payload, "additional_model_requests": 65},
            {**payload, "additional_model_requests": True},
            {**payload, "additional_model_requests": 1.0},
            {**payload, "additional_model_attempts": -1},
            {**payload, "additional_model_attempts": 257},
            {**payload, "additional_model_attempts": False},
            {**payload, "additional_model_attempts": "4"},
            {**payload, "additional_model_requests": 0, "additional_model_attempts": 0},
        ):
            status, body = request(port, "POST", path, invalid)
            assert status == 400, (invalid, body)
        assert service.calls == []
        status, receipt = request(port, "POST", path, payload)
        assert status == 200
        assert receipt == {"run_id": "run_1", "status": "failed", "operation_id": "op-budget"}
        assert service.calls == [("session_1", "run_1", payload)]


def test_budget_route_preserves_receipt_replay_and_redacts_stale_cas():
    service = BudgetService()
    path = "/api/sessions/session_1/runs/run_1/extend_budget"
    with running_server(service) as port:
        payload = budget_payload(additional_model_requests=0)
        first = request(port, "POST", path, payload)
        assert first[0] == 200
        assert request(port, "POST", path, payload) == first
        assert len(service.receipts) == 1
        for invalid in (
            {**payload, "idempotency_key": "new-stale-key"},
            {**payload, "additional_model_attempts": 8},
        ):
            status, body = request(port, "POST", path, invalid)
            assert status == 409
            assert body["error"]["code"] == "conflict"
            assert "private" not in str(body)
        assert service.revision == 4 and service.run_revision == 8


def test_budget_route_validates_peer_method_and_path_before_service():
    service = BudgetService()
    path = "/api/sessions/session_1/runs/run_1/extend_budget"
    with running_server(service) as port:
        assert request(port, "POST", path, budget_payload(), origin=False)[0] == 403
        assert request(port, "POST", path, budget_payload(),
                       headers={"Host": "evil.example"})[0] == 403
        assert request(port, "GET", path)[0] == 404
        for invalid in (
            path + "/extra", path + "?revision=1",
            path.replace("/session_1/", "/bad.id/"),
            path.replace("/run_1/", "/bad.id/"),
        ):
            assert request(port, "POST", invalid, budget_payload())[0] == 404
        assert service.calls == []


def final_response():
    return ModelResponse("tool_calls", tool_calls=(
        ModelToolCall(str(uuid4()), "final_answer",
                      dumps_pretty({"answer": {"text": "Finished from saved progress"}})),
    ))


def run_payload(view, key, **increments):
    return {
        "idempotency_key": key,
        "expected_session_revision": view["revision"],
        "expected_run_revision": view["nodes"][0]["revision"],
        **increments,
    }


def test_http_exhaustion_extension_is_persisted_without_dispatch_then_same_run_resumes(database):
    model_calls = []
    recover = False

    class BudgetAdapter:
        def __init__(self, stage):
            self.stage = stage

        def generate(self, messages, tools):
            model_calls.append((self.stage, messages))
            if recover:
                return final_response()
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "inspect_text",
                              dumps_pretty({"text": "Preserve this completed tool result"})),
            ))

    with closing(WorkflowService(database, model_factory=BudgetAdapter)) as service:
        with running_server(service) as port:
            sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
            submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "Continue the same run", "idempotency_key": "submit",
            })[1]
            service.wait_for_idle(sid)
            stopped = request(port, "GET", f"/api/sessions/{sid}")[1]
            run_id = stopped["nodes"][0]["run_id"]
            assert stopped["nodes"][0]["status"] == "failed"
            assert stopped["nodes"][0]["budget"] == {
                "max_model_requests": 8, "max_model_attempts": 32,
                "model_requests": 8, "attempts": 8,
            }
            assert "extend_budget" in stopped["available_actions"]
            assert "resume" not in stopped["available_actions"]
            assert len(model_calls) == 8
            with closing(SqliteStore(database)) as store:
                old_run = store.get_record("run_record", {"run_id": run_id})
                old_snapshot = store.get_record("input_snapshot", {"snapshot_id": old_run["snapshot_id"]})
                old_facts = store.read_execution_facts(run_id)
            path = f"/api/sessions/{sid}/runs/{run_id}/extend_budget"
            payload = run_payload(
                stopped, "extend-once", additional_model_requests=1, additional_model_attempts=0,
            )
            stale = {**payload, "expected_run_revision": payload["expected_run_revision"] + 1}
            assert request(port, "POST", path, stale)[0] == 409
            assert len(model_calls) == 8
            status, receipt = request(port, "POST", path, payload)
            assert status == 200
            assert receipt["status"] == "failed"
            assert receipt["budget"] == {"max_model_requests": 9, "max_model_attempts": 32}
            assert request(port, "POST", path, payload) == (200, receipt)
            assert len(model_calls) == 8
            extended = request(port, "GET", f"/api/sessions/{sid}")[1]
            assert "resume" in extended["available_actions"]
            assert "extend_budget" not in extended["available_actions"]
            assert extended["nodes"][0]["budget"]["model_requests"] == 8
            assert extended["nodes"][0]["budget"]["attempts"] == 8
            assert request(port, "POST", path, {**payload, "idempotency_key": "stale"})[0] == 409
            with closing(SqliteStore(database)) as store:
                assert store.read_execution_facts(run_id) == old_facts
                assert store.get_record("run_record", {"run_id": run_id})["initial_budget"] == \
                    old_run["initial_budget"]
            recover = True
            status, resumed = request(
                port, "POST", f"/api/sessions/{sid}/runs/{run_id}/resume",
                run_payload(extended, "resume-once"),
            )
            assert status == 202 and resumed["chain_run_id"] == submitted["chain_run_id"]
            service.wait_for_idle(sid)
            finished = request(port, "GET", f"/api/sessions/{sid}")[1]
            assert finished["error"] is None
            assert finished["nodes"][0]["run_id"] == run_id
            assert finished["chains"] == [{
                "chain_run_id": submitted["chain_run_id"], "status": "succeeded",
            }]
            assert [row["role"] for row in finished["messages"]] == ["user", "assistant"]
            assert finished["nodes"][0]["budget"] == {
                "max_model_requests": 9, "max_model_attempts": 32,
                "model_requests": 9, "attempts": 9,
            }
            with closing(SqliteStore(database)) as store:
                facts = store.read_execution_facts(run_id)
                assert facts[:len(old_facts)] == old_facts
                assert len([row for row in facts if row["kind"] == "tool_dispatch"]) == 9
                assert store.get_record("input_snapshot", {"snapshot_id": old_run["snapshot_id"]}) == \
                    old_snapshot
                operations = [row for row in store.list_records("workflow_operation")
                              if row["kind"] == "extend_budget"]
                assert len(operations) == 1
            assert len(model_calls) == 10


def test_http_transient_model_failure_resumes_without_repeating_completed_tool(database):
    model_calls = []
    recovered = False

    class RetryAdapter:
        def __init__(self, stage):
            self.stage = stage

        def generate(self, messages, tools):
            model_calls.append((self.stage, messages))
            if recovered:
                return final_response()
            if len(model_calls) > 1:
                raise TimeoutError("private transient provider failure")
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall(str(uuid4()), "inspect_text", dumps_pretty({"text": "once"})),
            ))

    with closing(WorkflowService(database, model_factory=RetryAdapter)) as service:
        with running_server(service) as port:
            sid = request(port, "POST", "/api/sessions", {})[1]["workflow_session_id"]
            submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "Retry only the model", "idempotency_key": "submit",
            })[1]
            service.wait_for_idle(sid)
            failed = request(port, "GET", f"/api/sessions/{sid}")[1]
            assert failed["error"]["code"] == "model_retry_exhausted"
            assert "private" not in str(failed)
            assert "resume" in failed["available_actions"]
            assert "extend_budget" not in failed["available_actions"]
            run_id = failed["nodes"][0]["run_id"]
            assert failed["nodes"][0]["budget"] == {
                "max_model_requests": 8, "max_model_attempts": 32,
                "model_requests": 2, "attempts": 5,
            }
            with closing(SqliteStore(database)) as store:
                old_facts = store.read_execution_facts(run_id)
            recovered = True
            path = f"/api/sessions/{sid}/runs/{run_id}/resume"
            payload = run_payload(failed, "continue-same-run")
            status, receipt = request(port, "POST", path, payload)
            assert status == 202
            assert receipt["chain_run_id"] == submitted["chain_run_id"]
            assert request(port, "POST", path, payload) == (202, receipt)
            service.wait_for_idle(sid)
            finished = request(port, "GET", f"/api/sessions/{sid}")[1]
            assert finished["error"] is None
            assert finished["nodes"][0]["run_id"] == run_id
            assert finished["nodes"][0]["budget"]["model_requests"] == 3
            assert finished["nodes"][0]["budget"]["attempts"] == 6
            assert len(model_calls) == 7
            with closing(SqliteStore(database)) as store:
                facts = store.read_execution_facts(run_id)
                assert facts[:len(old_facts)] == old_facts
                assert len([row for row in facts if row["kind"] == "tool_dispatch"]) == 2
                requests = [row for row in facts if row["kind"] == "model_request"]
                assert len({row["payload"]["request_id"] for row in requests}) == 3
