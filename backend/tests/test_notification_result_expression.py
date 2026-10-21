"""G12: a committed business result stays separate from its notification stage.

The round business result (status, outputs, commit, checkpoint, end manifest) is
settled before local notifications run.  These tests drive post-commit
notification failures from real entry points and assert that the waiter gets a
structured committed-result error while independent reads still show the
committed business facts and the recorded notification diagnostics.
"""

from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import closing
from copy import deepcopy
import json
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import DiagnosticError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.notification_results import (
    NOTIFICATION_DIAGNOSTIC_FAILURE, NOTIFICATION_FAILURE_REASON,
    ROUND_NOTIFICATION_FAILURE, CommittedNotificationError,
    public_diagnostic_failure, public_stage_failure,
)

from test_graph_service import document, edge, gate_graph, node, text_graph


PRIVATE_MARKER = "PRIVATE-MARKER-7f3c9a-119-p2"


@pytest.fixture
def service(tmp_path):
    registry = create_package_registry().registry.detached()
    instance = GraphWorkflowService(tmp_path / "notification-results.sqlite", registry=registry)
    try:
        yield instance
    finally:
        instance.close()


def _editable(service, graph):
    graph["schema_version"] = 3
    graph.setdefault("package_lock", list(service.registry.execution_package_lock))
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    return graph


def _start(service, graph):
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))
    view = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    started = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                            idempotency_key=str(uuid4()))
    return saved, view, started["active_chain_run_id"]


def _business_snapshot(service):
    """Business facts a notification failure must never rewrite."""
    with closing(service._store()) as store:
        return {
            record_type: tuple(sorted(
                (row["record_id"], row["payload"])
                for row in store._connection.execute(
                    "SELECT record_id,payload FROM records WHERE record_type=?", (record_type,))))
            for record_type in ("workflow_output", "workflow_commit", "state_snapshot",
                                "node_run", "node_session")
        } | {"manifests": tuple(sorted(
            (row[0], row[1], row[2]) for row in store._connection.execute(
                "SELECT owner_kind,owner_id,payload FROM graph_state_manifests")))}


def _run_detail(service, sid, chain_id):
    return service.get_run(sid, chain_id)


def _round_notifications(detail):
    return detail["chain"]["diagnostic"]["round_notifications"]


def test_successful_round_records_notification_success_and_wait_returns(service):
    graph = _editable(service, text_graph(service.registry))
    saved, view, chain_id = _start(service, graph)
    assert service.wait(chain_id) is None
    session = service.get_session(view["workflow_session_id"])
    assert session["status"] == "succeeded", session["chains"]
    detail = _run_detail(service, view["workflow_session_id"], chain_id)
    assert detail["chain"]["status"] == "succeeded"
    recorded = _round_notifications(detail)
    assert [entry["outcome"] for entry in recorded["notifications"]] == [
        "succeeded", "succeeded"]
    assert recorded["business_status"] == "succeeded"
    assert recorded["business_committed"] is True


def test_chat_snapshot_failure_keeps_business_committed_and_reports_structured(service):
    graph = _editable(service, text_graph(service.registry))
    saved, view, chain_id = _start(service, graph)
    entered, release = Event(), Event()
    original = service._refresh_chat_snapshots

    def blocked_then_failing(sid):
        entered.set()
        assert release.wait(10), "notification gate timed out"
        raise RuntimeError("chat snapshot notification probe failed")

    service._refresh_chat_snapshots = blocked_then_failing
    try:
        assert entered.wait(5), "post-commit notification stage did not run"
        before = _business_snapshot(service)
    finally:
        release.set()
    with pytest.raises(CommittedNotificationError) as raised:
        service.wait(chain_id)
    error = raised.value
    assert error.reason_code == ROUND_NOTIFICATION_FAILURE
    assert error.business_status == "succeeded"
    assert error.business_committed is True
    assert error.chain_id == chain_id
    assert error.workflow_session_id == view["workflow_session_id"]
    outcomes = [(entry["stage"], entry["outcome"]) for entry in error.notifications]
    assert outcomes == [("chat_snapshot", "failed"), ("frontend_node_state", "not_executed")]
    failed = error.notifications[0]
    assert failed == {"stage": "chat_snapshot", "outcome": "failed",
                      "reason_code": NOTIFICATION_FAILURE_REASON,
                      "error_type": "RuntimeError"}
    assert error.notification_diagnostic_recorded is True
    service._refresh_chat_snapshots = original

    detail = _run_detail(service, view["workflow_session_id"], chain_id)
    assert detail["chain"]["status"] == "succeeded"
    recorded = _round_notifications(detail)
    assert recorded["notifications"] == error.notifications
    session = service.get_session(view["workflow_session_id"])
    assert session["status"] == "succeeded", session["chains"]
    assert _business_snapshot(service) == before
    assert detail["outputs"], detail
    assert len([row for row in detail["node_runs"] if row["status"] == "succeeded"]) >= 2


def test_frontend_state_failure_reports_succeeded_then_failed(service):
    graph = _editable(service, text_graph(service.registry))
    saved, view, chain_id = _start(service, graph)
    entered, release = Event(), Event()
    original = service._frontend_node_state_changed

    def blocked_then_failing(sid):
        entered.set()
        assert release.wait(10), "frontend state gate timed out"
        raise RuntimeError("frontend state notification probe failed")

    service._frontend_node_state_changed = blocked_then_failing
    try:
        assert entered.wait(5), "post-commit frontend state stage did not run"
        before = _business_snapshot(service)
    finally:
        release.set()
    with pytest.raises(CommittedNotificationError) as raised:
        service.wait(chain_id)
    error = raised.value
    assert [(entry["stage"], entry["outcome"]) for entry in error.notifications] == [
        ("chat_snapshot", "succeeded"), ("frontend_node_state", "failed")]
    service._frontend_node_state_changed = original
    detail = _run_detail(service, view["workflow_session_id"], chain_id)
    assert detail["chain"]["status"] == "succeeded"
    assert _round_notifications(detail)["notifications"] == error.notifications
    assert _business_snapshot(service) == before


def test_failed_round_notification_failure_keeps_failed_business_status(service):
    def fail(config, inputs, context):
        raise ValueError("business failure probe")

    service.registry.register(NodeDefinition(
        "test.notification-business-failure", "1", "Failure", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), fail)
    source = node(service.registry, "tools.text", 1)
    middle = node(service.registry, "test.notification-business-failure", 2)
    output = node(service.registry, "tools.output", 3)
    graph = document([source, middle, output], [edge(source, middle, 1), edge(middle, output, 2)])
    graph = _editable(service, graph)
    saved, view, chain_id = _start(service, graph)
    original = service._frontend_node_state_changed
    service._frontend_node_state_changed = (
        lambda sid: (_ for _ in ()).throw(RuntimeError("failed round notification probe")))
    try:
        with pytest.raises(CommittedNotificationError) as raised:
            service.wait(chain_id)
    finally:
        service._frontend_node_state_changed = original
    error = raised.value
    assert error.business_status == "failed"
    assert error.business_committed is True
    detail = _run_detail(service, view["workflow_session_id"], chain_id)
    assert detail["chain"]["status"] == "failed"
    session = service.get_session(view["workflow_session_id"])
    assert session["status"] == "failed", session["chains"]


def test_notification_diagnostic_record_failure_is_reported(service):
    graph = _editable(service, text_graph(service.registry))
    saved, view, chain_id = _start(service, graph)
    original = service._record_round_notification

    def broken_record(*args, **kwargs):
        raise RuntimeError("notification diagnostic write probe failed " + PRIVATE_MARKER)

    service._record_round_notification = broken_record
    try:
        with pytest.raises(CommittedNotificationError) as raised:
            service.wait(chain_id)
    finally:
        service._record_round_notification = original
    error = raised.value
    assert error.notification_diagnostic_recorded is False
    assert error.notification_diagnostic_error == (
        NOTIFICATION_DIAGNOSTIC_FAILURE + ": RuntimeError")
    exposed = "\n".join((str(error), repr(error),
                         json.dumps(error.to_dict(), ensure_ascii=False)))
    assert PRIVATE_MARKER not in exposed
    detail = _run_detail(service, view["workflow_session_id"], chain_id)
    assert PRIVATE_MARKER not in json.dumps(detail, ensure_ascii=False)
    assert detail["chain"]["status"] == "succeeded"
    assert "round_notifications" not in (detail["chain"]["diagnostic"] or {})
    # Reopen/independent read keeps business success; absent diagnostics are unknown.
    session = service.get_session(view["workflow_session_id"])
    assert session["status"] == "succeeded", session["chains"]


def test_post_commit_diagnostic_annotation_cannot_change_business_facts(service):
    """The G12 diagnostic-only allowance must not open a lifecycle transition."""
    from phase1_agent.contract_errors import ContractValidationError
    from phase1_agent.graph_records import validate_graph_transition

    graph = _editable(service, text_graph(service.registry))
    saved, view, chain_id = _start(service, graph)
    service.wait(chain_id)
    chain = _run_detail(service, view["workflow_session_id"], chain_id)["chain"]
    previous = deepcopy(chain)
    annotated = deepcopy(chain)
    annotated["revision"] += 1
    annotated["diagnostic"] = {"round_notifications": {"notifications": []}}
    validate_graph_transition("chain_run", previous, annotated)  # same status: allowed

    changed_progress = deepcopy(annotated)
    changed_progress["next_node_index"] += 1
    with pytest.raises(ContractValidationError):
        validate_graph_transition("chain_run", previous, changed_progress)

    changed_status = deepcopy(annotated)
    changed_status["status"] = "running"
    with pytest.raises(ContractValidationError):
        validate_graph_transition("chain_run", previous, changed_status)


def test_wait_timeout_still_means_unknown_not_business_failure(service):
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "wait timeout gate timed out"
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.notification-wait-gate", "1", "Gate", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), gate)
    first = node(service.registry, "tools.text", 1)
    middle = node(service.registry, "test.notification-wait-gate", 2)
    output = node(service.registry, "tools.output", 3)
    graph = document([first, middle, output], [edge(first, middle, 1), edge(middle, output, 2)])
    graph["schema_version"] = 3
    for entry in graph["nodes"]:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    saved, view, chain_id = _start(service, graph)
    try:
        assert entered.wait(5)
        with pytest.raises(FutureTimeout):
            service.wait(chain_id, timeout=0.1)
        detail = _run_detail(service, view["workflow_session_id"], chain_id)
        assert detail["chain"]["status"] == "running"
    finally:
        release.set()
        service.wait(chain_id)
    session = service.get_session(view["workflow_session_id"])
    assert session["status"] == "succeeded", session["chains"]


def _run_chain_status(service, sid, chain_id):
    return service.get_run(sid, chain_id)["chain"]["status"]


def _pause_with_notification_failure(service, sid, chain_id, release):
    """Pause a gated round and fail its post-pause notification stage."""
    original = service._frontend_node_state_changed

    def fail_when_paused(execution_sid):
        # control() also refreshes frontend state before the round settles; only
        # the post-finish notification stage of the paused round fails here.
        if _run_chain_status(service, execution_sid, chain_id) == "paused":
            raise RuntimeError("paused notification probe failed")
        return original(execution_sid)

    service._frontend_node_state_changed = fail_when_paused
    try:
        running = service.get_session(sid)
        service.control(sid, action="pause", expected_revision=running["revision"],
                        idempotency_key=str(uuid4()))
    finally:
        release.set()
    with pytest.raises(CommittedNotificationError) as raised:
        service.wait(chain_id)
    service._frontend_node_state_changed = original
    assert raised.value.business_status == "paused"
    return raised.value


def test_resume_success_clears_the_paused_notification_error(service):
    sid, chain_id, release = gate_graph(service)
    paused_error = _pause_with_notification_failure(service, sid, chain_id, release)
    assert paused_error.business_status == "paused"
    paused = service.get_session(sid)
    assert paused["status"] == "paused"

    resumed = service.control(sid, action="resume", expected_revision=paused["revision"],
                              idempotency_key=str(uuid4()))
    assert resumed["active_chain_run_id"] == chain_id
    assert service.wait(chain_id) is None
    assert chain_id not in service._execution_errors

    settled = service.get_session(sid)
    assert settled["status"] == "succeeded", settled["chains"]
    detail = _run_detail(service, sid, chain_id)
    assert detail["chain"]["status"] == "succeeded"
    recorded = _round_notifications(detail)
    assert recorded["business_status"] == "succeeded"
    assert [entry["outcome"] for entry in recorded["notifications"]] == [
        "succeeded", "succeeded"]


def test_resume_notification_failure_replaces_the_paused_error(service):
    sid, chain_id, release = gate_graph(service)
    paused_error = _pause_with_notification_failure(service, sid, chain_id, release)
    assert paused_error.business_status == "paused"

    original = service._refresh_chat_snapshots

    def fail_snapshot(execution_sid):
        raise RuntimeError("resume notification probe failed")

    service._refresh_chat_snapshots = fail_snapshot
    try:
        paused = service.get_session(sid)
        service.control(sid, action="resume", expected_revision=paused["revision"],
                        idempotency_key=str(uuid4()))
        with pytest.raises(CommittedNotificationError) as raised:
            service.wait(chain_id)
    finally:
        service._refresh_chat_snapshots = original
    error = raised.value
    assert error is not paused_error
    assert error.business_status == "succeeded"
    assert [(entry["stage"], entry["outcome"]) for entry in error.notifications] == [
        ("chat_snapshot", "failed"), ("frontend_node_state", "not_executed")]
    assert service._execution_errors[chain_id] is error


def test_notification_failure_reason_is_redacted_in_wait_and_diagnostic(service):
    graph = _editable(service, text_graph(service.registry))
    saved, view, chain_id = _start(service, graph)
    original = service._refresh_chat_snapshots

    def fail_with_private_payload(execution_sid):
        raise RuntimeError("notification payload " + PRIVATE_MARKER)

    service._refresh_chat_snapshots = fail_with_private_payload
    try:
        with pytest.raises(CommittedNotificationError) as raised:
            service.wait(chain_id)
    finally:
        service._refresh_chat_snapshots = original
    error = raised.value
    exposed = "\n".join((str(error), repr(error),
                         json.dumps(error.to_dict(), ensure_ascii=False)))
    assert PRIVATE_MARKER not in exposed
    failed = error.notifications[0]
    assert failed == {"stage": "chat_snapshot", "outcome": "failed",
                      "reason_code": NOTIFICATION_FAILURE_REASON,
                      "error_type": "RuntimeError"}
    detail = _run_detail(service, view["workflow_session_id"], chain_id)
    assert PRIVATE_MARKER not in json.dumps(detail, ensure_ascii=False)
    assert _round_notifications(detail)["notifications"] == error.notifications
    assert detail["chain"]["status"] == "succeeded"


def test_public_failure_expression_never_carries_exception_text():
    runtime = public_stage_failure(RuntimeError("private " + PRIVATE_MARKER))
    assert runtime == {"reason_code": NOTIFICATION_FAILURE_REASON,
                       "error_type": "RuntimeError"}
    assert PRIVATE_MARKER not in json.dumps(runtime)

    class ProbedError(RuntimeError):
        reason_code = "private_marker_lowercase"

    assert public_stage_failure(ProbedError(PRIVATE_MARKER)) == {
        "reason_code": NOTIFICATION_FAILURE_REASON, "error_type": "ProbedError"}
    assert public_diagnostic_failure(RuntimeError("private " + PRIVATE_MARKER)) == (
        NOTIFICATION_DIAGNOSTIC_FAILURE + ": RuntimeError")
    assert PRIVATE_MARKER not in public_diagnostic_failure(
        RuntimeError("private " + PRIVATE_MARKER))

    product = DiagnosticError("chat_snapshot_unavailable", "private " + PRIVATE_MARKER)
    expression = public_stage_failure(product)
    assert expression == {"reason_code": "chat_snapshot_unavailable",
                          "error_type": "DiagnosticError"}
    assert PRIVATE_MARKER not in json.dumps(expression)
