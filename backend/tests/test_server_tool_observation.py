"""A complete uncertain batch crosses the real HTTP and SQLite boundary."""

from contextlib import closing

import pytest

from phase1_agent.contract_graph import validate_bundle
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, OfflineAdapter

from test_server_integration import database, request, running_service
from test_workflow_unknown_tool import (
    ObservationAdapter, assert_closed_observations, observation_tools,
)


def test_http_unknown_batch_continues_same_chain_and_reopens_without_redispatch(
    database, monkeypatch,
):
    calls, observed = [], []
    observation_tools(monkeypatch, calls)

    def factory(stage):
        return ObservationAdapter(stage, observed, verify=True) if stage == "A" else OfflineAdapter(stage)

    with running_service(database, factory) as (service, port):
        status, created = request(port, "POST", "/api/sessions", {})
        assert status == 201
        sid = created["workflow_session_id"]
        payload = {"text": "Keep all tool observations", "idempotency_key": "once"}
        path = f"/api/sessions/{sid}/inputs"
        status, submitted = request(port, "POST", path, payload)
        assert status == 202
        service.wait_for_idle(sid)
        status, view = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and view["error"] is None
        assert view["unresolved_tool_executions"] == []
        assert view["chains"] == [{
            "chain_run_id": submitted["chain_run_id"], "status": "succeeded",
        }]
        assert [row["role"] for row in view["messages"]] == ["user", "assistant"]
        assert calls[:3] == ["known", "uncertain", "verify"]
        assert len(calls) == 4
        before = list(calls)
        assert request(port, "POST", path, payload) == (202, submitted)
        assert calls == before
        assert_closed_observations(observed[0])
        with closing(SqliteStore(database)) as store:
            bundle = validate_bundle(store.read_bundle())
            assert len(bundle["chain_run"]) == 1
            assert len(bundle["turn"]) == 2
            assert len(bundle["workflow_candidate"]) == 1
            assert len(bundle["output_delivery"]) == 1
            a_run = next(row for row in bundle["run_record"] if row["node_binding_id"] == A_BINDING)
            original_run_id = a_run["run_id"]
            assert a_run["chain_run_id"] == submitted["chain_run_id"]
            assert a_run["source_run_id"] is None
            a_turn = next(row for row in bundle["turn"] if row["run_id"] == original_run_id)
            assert_closed_observations(a_turn["messages"])
    with running_service(
        database, lambda stage: pytest.fail(f"reopen redispatched {stage}"),
    ) as (_, port):
        status, reopened = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and reopened["error"] is None
        assert reopened["messages"] == view["messages"]
        assert reopened["nodes"][0]["run_id"] == original_run_id
        assert request(port, "POST", path, payload) == (202, submitted)
        assert calls == before
