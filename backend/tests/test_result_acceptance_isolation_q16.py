"""Q16 (J8) acceptance-fault isolation probes - current-behavior evidence only.

These tests drive the real package Agent graph (``workflow_test_support.graph``)
through ``GraphWorkflowService``: the production host is built without a result
sink, and the real ``agents.snapshot-kernel`` executor publishes its terminal
``agent_result`` fact through the real host fact sink into the isolated SQLite
database.  Faults are injected test-side at the real acceptance/settlement
boundaries; no production code, switch, retry rule or error policy is changed.

Observed dispositions (failed / archive_failed / recovery_unavailable and the
retry availability) are asserted so the observation is reproducible.  They are
NOT product target behavior and must not be read as fixing or accepting the Q16
classification question (G16 remains open).  Each test writes one structured
observation JSON next to ``tmp_path`` and the summary lives in
``.local/q16-recon-2026-10-20/Q16-REPORT.md``.

Coverage (per 118 sec. 4.8):
  1. coded failure with a definite retained result (acceptance + post-host
     settlement, paired with the no-code equivalents);
  2. coded deterministic rejections (result/identity violation vs
     owner/state-not-allowed);
  3. no-code unknown commit state at pre-write / commit-boundary / deterministic
     program error injection points, with actual commit state read independently;
  4. committed result whose acknowledgment was lost after the actual COMMIT,
     reusing the exact retained fact without a new model/tool invocation.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import pytest

from phase1_agent import agent_executor
from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import HostContractError
from phase1_agent.runtime import SnapshotKernel
from phase1_agent.runtime_hosting import InvocationOwner

from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import AgentTransportFixture, graph


# ---------------------------------------------------------------------------
# environment / observation helpers
# ---------------------------------------------------------------------------

def _setup(tmp_path, name):
    fixture = AgentTransportFixture()
    service = GraphWorkflowService(
        tmp_path / (name + ".sqlite"), public_model_factory=fixture.factory)
    ModelDatabaseFixture.write(service, 1)
    document = graph(service)
    agent_node_id = next(row["node_binding_id"] for row in document["nodes"]
                         if row["component_id"] == "agents.execute")
    initial = create(service, document)
    return service, fixture, initial, agent_node_id


def _start(service, initial):
    started = service.start(
        initial["workflow_session_id"], expected_revision=initial["revision"],
        idempotency_key="q16-start-" + initial["workflow_session_id"],
        inputs={"text": "question"})
    return started["workflow_session_id"], started["active_chain_run_id"]


def _owner(service, sid, chain_id, agent_node_id):
    chain = service.get_run(sid, chain_id)["chain"]
    index = chain["ordered_nodes"].index(agent_node_id)
    return InvocationOwner(sid, chain_id, agent_node_id,
                           chain["node_run_ids"][index])


def _host_state(service, owner):
    host = service._runtime_hosts.get(owner.chain_run_id)
    if host is None:
        return {"host_present": False}
    if not host.contains(owner):
        return {"host_present": True, "contains": False,
                "result_sink_configured": host._result_sink is not None}
    snapshot = host.snapshot(owner)
    invocation = host._invocations.get(owner)
    handle = getattr(invocation, "handle", None) if invocation is not None else None
    return {
        "host_present": True,
        "contains": True,
        "status": snapshot["status"],
        "disposed": snapshot["disposed"],
        "fact_sequence": snapshot["fact_sequence"],
        "has_pending_acceptance": host.has_pending_acceptance(owner),
        "retained_outputs_present": invocation.outputs is not None if invocation else None,
        "handle_pending_acceptance": (
            handle.has_pending_acceptance() if callable(
                getattr(handle, "has_pending_acceptance", None)) and handle is not None
            else None),
        # The production wiring must be the evidence: no synthetic result sink.
        "result_sink_configured": host._result_sink is not None,
        "executor_ref": snapshot["executor_ref"],
    }


def _sqlite_read(service):
    connection = sqlite3.connect(service.database.as_uri() + "?mode=ro", uri=True)
    try:
        terminal = connection.execute(
            "SELECT COUNT(*) FROM workflow_runtime_facts "
            "WHERE json_extract(payload, '$.payload.kind') = 'agent_result'").fetchone()[0]
        facts = connection.execute(
            "SELECT COUNT(*) FROM workflow_runtime_facts").fetchone()[0]
        fact_ids = [row[0] for row in connection.execute(
            "SELECT fact_id FROM workflow_runtime_facts ORDER BY rowid")]
        return {
            "runtime_fact_rows": facts,
            "terminal_agent_result_rows": terminal,
            "runtime_fact_ids": fact_ids,
            "idempotency_rows": connection.execute(
                "SELECT COUNT(*) FROM idempotency").fetchone()[0],
            "output_rows": connection.execute(
                "SELECT COUNT(*) FROM records WHERE record_type='workflow_output'")
                .fetchone()[0],
            "node_run_rows": [list(row) for row in connection.execute(
                "SELECT json_extract(payload, '$.status') FROM records "
                "WHERE record_type='node_run' ORDER BY rowid")],
        }
    finally:
        connection.close()


def _view(service, sid):
    value = service.get_session(sid)
    return {"status": value["status"],
            "available_actions": value["available_actions"],
            "revision": value["revision"]}


def _run(service, sid, chain_id):
    value = service.get_run(sid, chain_id)
    chain = value["chain"]
    return {
        "chain_status": chain["status"],
        "next_node_index": chain["next_node_index"],
        "completed_nodes": len(chain["completed_nodes"]),
        "ordered_nodes": len(chain["ordered_nodes"]),
        "chain_diagnostic": chain.get("diagnostic"),
        "node_runs": [{"run": row["run_id"][:8], "node": row["node_binding_id"][:8],
                       "status": row["status"],
                       "output_ports": sorted(row["output_refs"].keys())}
                      for row in value["node_runs"]],
        "runtime_facts": [
            {"stream": fact.get("stream_id"),
             "kind": ((fact.get("payload") or {}) .get("kind")
                      if isinstance(fact.get("payload"), dict) else fact.get("kind")),
             "generation": fact.get("generation")}
            for fact in value["runtime_facts"]],
    }


def _control(service, sid, action, expected_revision=None):
    view = service.get_session(sid)
    revision = view["revision"] if expected_revision is None else expected_revision
    try:
        response = service.control(sid, action=action, expected_revision=revision,
                                   idempotency_key=f"q16-{action}-{revision}")
        return {"accepted": True, "status": response["status"]}
    except ContractValidationError as error:
        return {"accepted": False, "reason_code": getattr(error, "reason_code", None),
                "message": str(error)}


class _Counters:
    def __init__(self, monkeypatch):
        self.executor_advance = 0
        self.kernel_run = 0
        original_advance = agent_executor.PublicAgentExecutor.advance
        original_kernel = SnapshotKernel.run

        def counted_advance(instance, *args, **kwargs):
            self.executor_advance += 1
            return original_advance(instance, *args, **kwargs)

        def counted_kernel(instance, *args, **kwargs):
            self.kernel_run += 1
            return original_kernel(instance, *args, **kwargs)

        monkeypatch.setattr(agent_executor.PublicAgentExecutor, "advance", counted_advance)
        monkeypatch.setattr(SnapshotKernel, "run", counted_kernel)

    def of(self, fixture):
        return {"executor_advance": self.executor_advance,
                "kernel_run": self.kernel_run,
                "transport_calls": len(fixture.calls),
                "transport_closes": fixture.closes}


class _AcceptanceFault:
    """Arm one fault at the real terminal-agent-result fact sink boundary."""

    def __init__(self, service, kind, *, code=None):
        self.service = service
        self.kind = kind
        self.code = code
        self.original = service._accept_executor_fact
        self.remaining = 1
        self.agent_result_envelopes = []
        self.host_at_injection = None
        self.late_callbacks = None
        self.injected_error = None

    def install(self):
        self.original = self.service._accept_executor_fact
        self.service._accept_executor_fact = self

    def __call__(self, envelope):
        payload = envelope.get("payload", {})
        if payload.get("kind") == "agent_result":
            self.agent_result_envelopes.append(deepcopy(envelope))
        if payload.get("kind") != "agent_result" or self.remaining <= 0:
            return self.original(envelope)
        self.remaining -= 1
        owner = InvocationOwner.from_dict(envelope["owner"])
        self.host_at_injection = _host_state(self.service, owner)
        host = self.service._runtime_hosts.get(owner.chain_run_id)
        if host is not None and host.contains(owner):
            invocation = host._invocations.get(owner)
            self.late_callbacks = getattr(
                getattr(invocation, "handle", None), "_callbacks", None)
        if self.kind == "pre_code":
            self.injected_error = {"type": "DiagnosticError", "reason_code": self.code}
            raise DiagnosticError(self.code, "probe injected coded acceptance failure",
                                  status=500)
        if self.kind == "pre_no_code":
            self.injected_error = {"type": "RuntimeError", "stage": "before fact persistence"}
            raise RuntimeError("probe injected pre-write program error")
        if self.kind == "commit_boundary":
            self.injected_error = {"type": "ZeroDivisionError",
                                   "stage": "before_runtime_fact_commit"}

            def fail_commit(point):
                if point == "before_runtime_fact_commit":
                    raise ZeroDivisionError("probe injected commit-boundary program error")

            self.service._fault_injector = fail_commit
            try:
                return self.original(envelope)
            finally:
                self.service._fault_injector = None
        if self.kind == "post_commit":
            self.injected_error = {"type": "RuntimeError",
                                   "stage": "after actual fact COMMIT, before acknowledgment"}
            self.original(envelope)
            raise RuntimeError("probe injected lost acknowledgment after actual commit")
        raise AssertionError(self.kind)


def _write_observation(tmp_path, name, observation):
    (tmp_path / (name + ".json")).write_text(
        json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# scenario 3 (and the no-code baseline for scenario 1): before write
# ---------------------------------------------------------------------------

def test_q16_no_code_pre_write_acceptance_retries_without_redrive(tmp_path, monkeypatch):
    service, fixture, initial, agent_node_id = _setup(tmp_path, "pre-write")
    counters = _Counters(monkeypatch)
    try:
        fault = _AcceptanceFault(service, "pre_no_code")
        fault.install()
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        owner = _owner(service, sid, chain_id, agent_node_id)
        assert fault.host_at_injection["result_sink_configured"] is False
        assert fault.host_at_injection["handle_pending_acceptance"] is True
        assert fault.host_at_injection["executor_ref"] == {
            "executor_id": "agents.snapshot-kernel", "exact_version": "2.0.0"}
        # Observation only: a no-code failure after the Agent retained its
        # completed result is treated as acceptance-pending.
        failed_view = _view(service, sid)
        failed_run = _run(service, sid, chain_id)
        failed_candidates = _candidate_summary(service, chain_id)
        failed_sqlite = _sqlite_read(service)
        failed_counts = counters.of(fixture)
        assert failed_view["status"] == "archive_failed"
        assert failed_view["available_actions"] == ["retry_acceptance", "close"]
        assert failed_run["chain_status"] == "archive_failed"
        assert failed_run["next_node_index"] == 4
        assert failed_sqlite["terminal_agent_result_rows"] == 0
        assert failed_counts == {"executor_advance": 1, "kernel_run": 1,
                                 "transport_calls": 1, "transport_closes": 1}
        host_failed = _host_state(service, owner)
        assert host_failed["status"] == "acceptance_pending"
        assert host_failed["has_pending_acceptance"] is True
        assert host_failed["disposed"] is False
        # Owner/state rejection from an obsolete callback generation.
        with pytest.raises(HostContractError) as stale:
            fault.late_callbacks.publish_fact("q16-late", {"probe": True})
        assert stale.value.reason_code == "runtime_stale_callback"
        retry = _control(service, sid, "retry_acceptance")
        assert retry["accepted"] is True
        service.wait(chain_id)
        final = _run(service, sid, chain_id)
        final_sqlite = _sqlite_read(service)
        assert _view(service, sid)["status"] == "succeeded"
        assert final["chain_status"] == "succeeded"
        assert final["next_node_index"] == 7
        assert final_sqlite["terminal_agent_result_rows"] == 1
        assert final_sqlite["runtime_fact_rows"] == failed_sqlite["runtime_fact_rows"] + 1
        assert counters.of(fixture) == failed_counts
        _write_observation(tmp_path, "pre-write", {
            "scenario": "no_code_pre_write",
            "ids": {"session": sid, "chain": chain_id, "owner": owner.to_dict()},
            "candidates_after_failure": failed_candidates,
            "fault": fault.injected_error,
            "host_at_injection": fault.host_at_injection,
            "host_after_failure": host_failed,
            "view_after_failure": failed_view,
            "run_after_failure": failed_run,
            "sqlite_after_failure": failed_sqlite,
            "counts_after_failure": failed_counts,
            "late_callback_reason_code": stale.value.reason_code,
            "retry": retry,
            "view_after_retry": _view(service, sid),
            "run_after_retry": final,
            "sqlite_after_retry": final_sqlite,
            "counts_after_retry": counters.of(fixture),
            "node_settlement_after_host_success": final["node_runs"][4],
        })
    finally:
        service.close()


# ---------------------------------------------------------------------------
# scenario 3: commit boundary
# ---------------------------------------------------------------------------

def test_q16_no_code_commit_boundary_rolls_back_then_retries(tmp_path, monkeypatch):
    service, fixture, initial, agent_node_id = _setup(tmp_path, "commit-boundary")
    counters = _Counters(monkeypatch)
    try:
        fault = _AcceptanceFault(service, "commit_boundary")
        fault.install()
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        owner = _owner(service, sid, chain_id, agent_node_id)
        failed_view = _view(service, sid)
        failed_run = _run(service, sid, chain_id)
        failed_candidates = _candidate_summary(service, chain_id)
        failed_sqlite = _sqlite_read(service)
        failed_counts = counters.of(fixture)
        # The injected exception is raised before the real COMMIT; the rollback
        # is verified independently, not inferred from the exception type.
        assert failed_sqlite["terminal_agent_result_rows"] == 0
        assert failed_view["status"] == "archive_failed"
        assert failed_view["available_actions"] == ["retry_acceptance", "close"]
        assert fault.host_at_injection["result_sink_configured"] is False
        host_failed = _host_state(service, owner)
        assert host_failed["status"] == "acceptance_pending"
        assert host_failed["has_pending_acceptance"] is True
        retry = _control(service, sid, "retry_acceptance")
        assert retry["accepted"] is True
        service.wait(chain_id)
        final = _run(service, sid, chain_id)
        final_sqlite = _sqlite_read(service)
        assert _view(service, sid)["status"] == "succeeded"
        assert final["chain_status"] == "succeeded" and final["next_node_index"] == 7
        assert final_sqlite["terminal_agent_result_rows"] == 1
        assert counters.of(fixture) == failed_counts
        _write_observation(tmp_path, "commit-boundary", {
            "scenario": "no_code_commit_boundary",
            "ids": {"session": sid, "chain": chain_id, "owner": owner.to_dict()},
            "candidates_after_failure": failed_candidates,
            "fault": fault.injected_error,
            "host_at_injection": fault.host_at_injection,
            "host_after_failure": host_failed,
            "view_after_failure": failed_view,
            "run_after_failure": failed_run,
            "sqlite_after_failure": failed_sqlite,
            "counts_after_failure": failed_counts,
            "retry": retry,
            "view_after_retry": _view(service, sid),
            "run_after_retry": final,
            "sqlite_after_retry": final_sqlite,
            "counts_after_retry": counters.of(fixture),
        })
    finally:
        service.close()


# ---------------------------------------------------------------------------
# scenario 3: deterministic program error after durable facts were committed
# ---------------------------------------------------------------------------

def test_q16_no_code_program_error_after_commit_retries_without_rewrite(
        tmp_path, monkeypatch):
    service, fixture, initial, agent_node_id = _setup(tmp_path, "projection-error")
    counters = _Counters(monkeypatch)
    original_validate = agent_executor.validate_agent_receipts
    armed = {"value": True}

    def failing_validate(value):
        if armed["value"]:
            armed["value"] = False
            raise ZeroDivisionError("probe injected projection program error")
        return original_validate(value)

    monkeypatch.setattr(agent_executor, "validate_agent_receipts", failing_validate)
    try:
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        owner = _owner(service, sid, chain_id, agent_node_id)
        failed_view = _view(service, sid)
        failed_run = _run(service, sid, chain_id)
        failed_candidates = _candidate_summary(service, chain_id)
        failed_sqlite = _sqlite_read(service)
        failed_counts = counters.of(fixture)
        # The terminal fact was committed before the program error: the actual
        # commit state is read independently (not inferred from the exception).
        assert failed_sqlite["terminal_agent_result_rows"] == 1
        assert failed_view["status"] == "archive_failed"
        assert failed_run["next_node_index"] == 4
        host_failed = _host_state(service, owner)
        assert host_failed["status"] == "acceptance_pending"
        retry = _control(service, sid, "retry_acceptance")
        assert retry["accepted"] is True
        service.wait(chain_id)
        final = _run(service, sid, chain_id)
        final_sqlite = _sqlite_read(service)
        assert _view(service, sid)["status"] == "succeeded"
        assert final["next_node_index"] == 7
        assert final_sqlite["runtime_fact_rows"] == failed_sqlite["runtime_fact_rows"]
        assert final_sqlite["runtime_fact_ids"] == failed_sqlite["runtime_fact_ids"]
        assert counters.of(fixture) == failed_counts
        _write_observation(tmp_path, "projection-error", {
            "scenario": "no_code_program_error_after_durable_commit",
            "ids": {"session": sid, "chain": chain_id, "owner": owner.to_dict()},
            "candidates_after_failure": failed_candidates,
            "fault": {"type": "ZeroDivisionError",
                      "stage": "after terminal fact committed, during result projection"},
            "host_after_failure": host_failed,
            "view_after_failure": failed_view,
            "run_after_failure": failed_run,
            "sqlite_after_failure": failed_sqlite,
            "counts_after_failure": failed_counts,
            "retry": retry,
            "view_after_retry": _view(service, sid),
            "run_after_retry": final,
            "sqlite_after_retry": final_sqlite,
            "counts_after_retry": counters.of(fixture),
        })
    finally:
        service.close()


# ---------------------------------------------------------------------------
# scenario 4: committed result, acknowledgment lost after the real COMMIT
# ---------------------------------------------------------------------------

def test_q16_committed_result_lost_ack_reuses_exact_fact(tmp_path, monkeypatch):
    service, fixture, initial, agent_node_id = _setup(tmp_path, "lost-ack")
    counters = _Counters(monkeypatch)
    try:
        fault = _AcceptanceFault(service, "post_commit")
        fault.install()
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        owner = _owner(service, sid, chain_id, agent_node_id)
        failed_view = _view(service, sid)
        failed_run = _run(service, sid, chain_id)
        failed_candidates = _candidate_summary(service, chain_id)
        failed_sqlite = _sqlite_read(service)
        failed_counts = counters.of(fixture)
        # Harness never fakes the commit: the real sink ran, then the injected
        # error was raised, so the fact row is present exactly once.
        assert failed_sqlite["terminal_agent_result_rows"] == 1
        assert failed_view["status"] == "archive_failed"
        assert failed_view["available_actions"] == ["retry_acceptance", "close"]
        assert len(fault.agent_result_envelopes) == 1
        host_failed = _host_state(service, owner)
        assert host_failed["status"] == "acceptance_pending"
        retry = _control(service, sid, "retry_acceptance")
        assert retry["accepted"] is True
        service.wait(chain_id)
        final = _run(service, sid, chain_id)
        final_sqlite = _sqlite_read(service)
        assert _view(service, sid)["status"] == "succeeded"
        assert final["next_node_index"] == 7
        # The retry replays the exact retained fact; it is not rewritten.
        assert len(fault.agent_result_envelopes) == 2
        assert fault.agent_result_envelopes[1] == fault.agent_result_envelopes[0]
        assert final_sqlite["runtime_fact_rows"] == failed_sqlite["runtime_fact_rows"]
        assert final_sqlite["runtime_fact_ids"] == failed_sqlite["runtime_fact_ids"]
        assert counters.of(fixture) == failed_counts
        _write_observation(tmp_path, "lost-ack", {
            "scenario": "committed_result_lost_ack",
            "ids": {"session": sid, "chain": chain_id, "owner": owner.to_dict()},
            "candidates_after_failure": failed_candidates,
            "fault": fault.injected_error,
            "terminal_envelope": fault.agent_result_envelopes[0],
            "replayed_envelope_equals_original":
                fault.agent_result_envelopes[1] == fault.agent_result_envelopes[0],
            "host_at_injection": fault.host_at_injection,
            "host_after_failure": host_failed,
            "view_after_failure": failed_view,
            "run_after_failure": failed_run,
            "sqlite_after_failure": failed_sqlite,
            "counts_after_failure": failed_counts,
            "retry": retry,
            "view_after_retry": _view(service, sid),
            "run_after_retry": final,
            "sqlite_after_retry": final_sqlite,
            "counts_after_retry": counters.of(fixture),
            "node_settlement_after_host_success": final["node_runs"][4],
        })
    finally:
        service.close()


# ---------------------------------------------------------------------------
# scenario 1/2 (observed dispositions only, not target policy)
# ---------------------------------------------------------------------------

def test_q16_coded_storage_failure_with_definite_result_is_not_recovered(
        tmp_path, monkeypatch):
    """Observation: same retained result as the no-code case, coded -> failed."""
    service, fixture, initial, agent_node_id = _setup(tmp_path, "coded-storage")
    counters = _Counters(monkeypatch)
    try:
        fault = _AcceptanceFault(service, "pre_code", code="storage_contract_violation")
        fault.install()
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        owner = _owner(service, sid, chain_id, agent_node_id)
        failed_view = _view(service, sid)
        failed_run = _run(service, sid, chain_id)
        failed_candidates = _candidate_summary(service, chain_id)
        failed_sqlite = _sqlite_read(service)
        failed_counts = counters.of(fixture)
        # At the injected moment a definite completed result was retained, and
        # the host carries no synthetic result sink.
        assert fault.host_at_injection["handle_pending_acceptance"] is True
        assert fault.host_at_injection["result_sink_configured"] is False
        # Observation only (2026-10-20): reason_code presence selects the
        # non-recoverable disposition; this is NOT accepted product policy.
        assert failed_view["status"] == "failed"
        assert failed_view["available_actions"] == ["close"]
        assert failed_run["chain_status"] == "failed"
        assert (failed_run["chain_diagnostic"] or {}).get("code") == \
            "storage_contract_violation"
        assert failed_sqlite["terminal_agent_result_rows"] == 0
        assert _host_state(service, owner) == {"host_present": False}
        assert _acceptance_candidates(service, chain_id) == {}
        retry = _control(service, sid, "retry_acceptance")
        assert retry["accepted"] is False
        assert retry["reason_code"] == "recovery_unavailable"
        assert counters.of(fixture) == failed_counts
        _write_observation(tmp_path, "coded-storage", {
            "scenario": "coded_storage_failure_with_definite_result",
            "observation_only": "coded failure selects failed/no-retry; not target policy",
            "ids": {"session": sid, "chain": chain_id, "owner": owner.to_dict()},
            "candidates_after_failure": failed_candidates,
            "fault": fault.injected_error,
            "host_at_injection": fault.host_at_injection,
            "view_after_failure": failed_view,
            "run_after_failure": failed_run,
            "sqlite_after_failure": failed_sqlite,
            "counts_after_failure": failed_counts,
            "retry": retry,
            "view_after_retry": _view(service, sid),
            "run_after_retry": _run(service, sid, chain_id),
            "sqlite_after_retry": _sqlite_read(service),
            "counts_after_retry": counters.of(fixture),
        })
    finally:
        service.close()


def test_q16_coded_settlement_after_host_success_is_not_recovered(tmp_path, monkeypatch):
    """Observation: host succeeded, settlement coded failure -> recovery_unavailable."""
    service, fixture, initial, agent_node_id = _setup(tmp_path, "coded-settlement")
    counters = _Counters(monkeypatch)
    original_event = service._node_event
    trace = {"host_at_injection": None}

    def failing_event(sid_value, chain_id_value, event):
        if (event.get("event") == "succeeded"
                and event.get("node_binding_id") == agent_node_id
                and "injected" not in trace):
            trace["injected"] = True
            chain = service.get_run(sid_value, chain_id_value)["chain"]
            index = chain["ordered_nodes"].index(agent_node_id)
            owner = InvocationOwner(sid_value, chain_id_value, agent_node_id,
                                    chain["node_run_ids"][index])
            trace["owner"] = owner
            host = service._runtime_hosts.get(chain_id_value)
            trace["host_at_injection"] = _host_state(service, owner) if host else {"host_present": False}
            raise DiagnosticError("storage_contract_violation",
                                  "probe injected coded settlement failure", status=500)
        return original_event(sid_value, chain_id_value, event)

    monkeypatch.setattr(service, "_node_event", failing_event)
    try:
        sid, chain_id = _start(service, initial)
        with pytest.raises(DiagnosticError) as caught:
            service.wait(chain_id)
        assert caught.value.reason_code == "storage_contract_violation"
        owner = trace["owner"]
        failed_view = _view(service, sid)
        failed_run = _run(service, sid, chain_id)
        failed_candidates = _candidate_summary(service, chain_id)
        failed_sqlite = _sqlite_read(service)
        failed_counts = counters.of(fixture)
        assert trace["host_at_injection"]["status"] == "succeeded"
        assert trace["host_at_injection"]["disposed"] is True
        assert trace["host_at_injection"]["retained_outputs_present"] is True
        assert trace["host_at_injection"]["result_sink_configured"] is False
        # Observation only: a coded settlement failure after a definite host
        # success becomes recovery_unavailable; not a product target policy.
        assert failed_view["status"] == "recovery_unavailable"
        assert failed_view["available_actions"] == ["close"]
        assert failed_sqlite["terminal_agent_result_rows"] == 1
        assert (failed_run["chain_diagnostic"] or {}).get("reason_code") == \
            "execution_result_unknown"
        assert _acceptance_candidates(service, chain_id) == {}
        retry = _control(service, sid, "retry_acceptance")
        assert retry["accepted"] is False
        assert retry["reason_code"] == "recovery_unavailable"
        assert counters.of(fixture) == failed_counts
        _write_observation(tmp_path, "coded-settlement", {
            "scenario": "coded_settlement_after_host_success",
            "observation_only": "coded settlement failure -> recovery_unavailable; "
                                "wait() raises; not target policy; Q12 adjacent",
            "ids": {"session": sid, "chain": chain_id, "owner": owner.to_dict()},
            "candidates_after_failure": failed_candidates,
            "fault": {"type": "DiagnosticError", "reason_code": "storage_contract_violation",
                      "stage": "node settlement after host success"},
            "host_at_injection": trace["host_at_injection"],
            "view_after_failure": failed_view,
            "run_after_failure": failed_run,
            "sqlite_after_failure": failed_sqlite,
            "counts_after_failure": failed_counts,
            "wait_error": {"type": type(caught.value).__name__,
                           "reason_code": caught.value.reason_code},
            "retry": retry,
            "view_after_retry": _view(service, sid),
            "run_after_retry": _run(service, sid, chain_id),
            "sqlite_after_retry": _sqlite_read(service),
            "counts_after_retry": counters.of(fixture),
        })
    finally:
        service.close()


def test_q16_no_code_settlement_after_host_success_accepts_retained_result(
        tmp_path, monkeypatch):
    """Paired no-code case for the coded settlement observation above."""
    service, fixture, initial, agent_node_id = _setup(tmp_path, "plain-settlement")
    counters = _Counters(monkeypatch)
    original_event = service._node_event
    trace = {"host_at_injection": None}

    def failing_event(sid_value, chain_id_value, event):
        if (event.get("event") == "succeeded"
                and event.get("node_binding_id") == agent_node_id
                and "injected" not in trace):
            trace["injected"] = True
            chain = service.get_run(sid_value, chain_id_value)["chain"]
            index = chain["ordered_nodes"].index(agent_node_id)
            owner = InvocationOwner(sid_value, chain_id_value, agent_node_id,
                                    chain["node_run_ids"][index])
            trace["owner"] = owner
            host = service._runtime_hosts.get(chain_id_value)
            trace["host_at_injection"] = _host_state(service, owner) if host else {"host_present": False}
            raise RuntimeError("probe injected settlement program error")
        return original_event(sid_value, chain_id_value, event)

    monkeypatch.setattr(service, "_node_event", failing_event)
    try:
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        owner = trace["owner"]
        failed_view = _view(service, sid)
        failed_run = _run(service, sid, chain_id)
        failed_candidates = _candidate_summary(service, chain_id)
        failed_sqlite = _sqlite_read(service)
        failed_counts = counters.of(fixture)
        assert trace["host_at_injection"]["status"] == "succeeded"
        assert trace["host_at_injection"]["retained_outputs_present"] is True
        assert failed_view["status"] == "archive_failed"
        assert failed_view["available_actions"] == ["retry_acceptance", "close"]
        assert failed_sqlite["terminal_agent_result_rows"] == 1
        candidate_keys = sorted(_acceptance_candidates(service, chain_id)[
            next(iter(_acceptance_candidates(service, chain_id)))])
        retry = _control(service, sid, "retry_acceptance")
        assert retry["accepted"] is True
        service.wait(chain_id)
        final = _run(service, sid, chain_id)
        final_sqlite = _sqlite_read(service)
        assert _view(service, sid)["status"] == "succeeded"
        assert final["next_node_index"] == 7
        assert final_sqlite["runtime_fact_ids"] == failed_sqlite["runtime_fact_ids"]
        assert counters.of(fixture) == failed_counts
        _write_observation(tmp_path, "plain-settlement", {
            "scenario": "no_code_settlement_after_host_success",
            "ids": {"session": sid, "chain": chain_id, "owner": owner.to_dict()},
            "candidates_after_failure": failed_candidates,
            "fault": {"type": "RuntimeError",
                      "stage": "node settlement after host success"},
            "host_at_injection": trace["host_at_injection"],
            "candidate_keys": candidate_keys,
            "view_after_failure": failed_view,
            "run_after_failure": failed_run,
            "sqlite_after_failure": failed_sqlite,
            "counts_after_failure": failed_counts,
            "retry": retry,
            "view_after_retry": _view(service, sid),
            "run_after_retry": final,
            "sqlite_after_retry": final_sqlite,
            "counts_after_retry": counters.of(fixture),
            "node_settlement_after_host_success": final["node_runs"][4],
        })
    finally:
        service.close()


def test_q16_coded_identity_violation_is_not_offered_acceptance(tmp_path, monkeypatch):
    service, fixture, initial, agent_node_id = _setup(tmp_path, "coded-identity")
    counters = _Counters(monkeypatch)
    try:
        fault = _AcceptanceFault(service, "pre_code", code="runtime_fact_conflict")
        fault.install()
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        owner = _owner(service, sid, chain_id, agent_node_id)
        failed_view = _view(service, sid)
        failed_run = _run(service, sid, chain_id)
        failed_candidates = _candidate_summary(service, chain_id)
        failed_sqlite = _sqlite_read(service)
        assert failed_view["status"] == "failed"
        assert failed_view["available_actions"] == ["close"]
        assert (failed_run["chain_diagnostic"] or {}).get("code") == "runtime_fact_conflict"
        assert _host_state(service, owner) == {"host_present": False}
        retry = _control(service, sid, "retry_acceptance")
        assert retry["accepted"] is False
        assert retry["reason_code"] == "recovery_unavailable"
        _write_observation(tmp_path, "coded-identity", {
            "scenario": "coded_identity_violation",
            "observation_only": "identity violation code -> failed/no-retry; not target policy",
            "ids": {"session": sid, "chain": chain_id, "owner": owner.to_dict()},
            "candidates_after_failure": failed_candidates,
            "fault": fault.injected_error,
            "host_at_injection": fault.host_at_injection,
            "view_after_failure": failed_view,
            "run_after_failure": failed_run,
            "sqlite_after_failure": failed_sqlite,
            "counts_after_failure": counters.of(fixture),
            "retry": retry,
        })
    finally:
        service.close()


def test_q16_owner_and_state_rejections_keep_original_codes(tmp_path):
    """Owner/state-not-allowed evidence belongs to the control layer, not the
    result/identity classification."""
    fixture = AgentTransportFixture()
    database = tmp_path / "owner-state.sqlite"
    service = GraphWorkflowService(database, public_model_factory=fixture.factory)
    try:
        ModelDatabaseFixture.write(service, 1)
        document = graph(service)
        agent_node_id = next(row["node_binding_id"] for row in document["nodes"]
                             if row["component_id"] == "agents.execute")
        initial = create(service, document)
        fault = _AcceptanceFault(service, "pre_no_code")
        fault.install()
        sid, chain_id = _start(service, initial)
        service.wait(chain_id)
        owner = _owner(service, sid, chain_id, agent_node_id)
        failed_view = _view(service, sid)
        stale = _control(service, sid, "retry_acceptance",
                         expected_revision=failed_view["revision"] - 1)
        assert stale["accepted"] is False and stale["reason_code"] == "stale_revision"
        with pytest.raises(HostContractError) as late:
            fault.late_callbacks.publish_fact("q16-late-owner", {"probe": True})
        assert late.value.reason_code == "runtime_stale_callback"
        before_close = _view(service, sid)
        before_close_run = _run(service, sid, chain_id)
        before_close_sqlite = _sqlite_read(service)
        before_close_candidates = _candidate_summary(service, chain_id)
    finally:
        service.close()
    reopened = GraphWorkflowService(database, public_model_factory=fixture.factory)
    try:
        after_reopen = _view(reopened, sid)
        after_reopen_run = _run(reopened, sid, chain_id)
        retry_after_reopen = _control(reopened, sid, "retry_acceptance")
        archive_after_reopen = _control(reopened, sid, "retry_archive")
        assert after_reopen["status"] == "archive_failed"
        assert after_reopen["available_actions"] == ["retry_archive", "close"]
        assert retry_after_reopen["accepted"] is False
        assert retry_after_reopen["reason_code"] == "recovery_unavailable"
        assert archive_after_reopen["accepted"] is False
        assert archive_after_reopen["reason_code"] == "recovery_unavailable"
        assert _sqlite_read(reopened)["runtime_fact_ids"] == \
            before_close_sqlite["runtime_fact_ids"]
        failed_candidates = before_close_candidates
        _write_observation(tmp_path, "owner-state", {
            "scenario": "owner_state_rejections",
            "ids": {"session": sid, "chain": chain_id, "owner": owner.to_dict()},
            "candidates_after_failure": failed_candidates,
            "fault": fault.injected_error,
            "host_at_injection": fault.host_at_injection,
            "view_before_close": before_close,
            "run_before_close": before_close_run,
            "sqlite_before_close": before_close_sqlite,
            "stale_revision": stale,
            "late_callback_reason_code": late.value.reason_code,
            "view_after_reopen": after_reopen,
            "run_after_reopen": after_reopen_run,
            "retry_acceptance_after_reopen": retry_after_reopen,
            "retry_archive_after_reopen": archive_after_reopen,
            "sqlite_after_reopen": _sqlite_read(reopened),
        })
    finally:
        reopened.close()


def _acceptance_candidates(service, chain_id):
    return service._acceptance_candidates.get(chain_id, {})


def _candidate_summary(service, chain_id):
    return {run_id: sorted(value) for run_id, value in
            _acceptance_candidates(service, chain_id).items()}
