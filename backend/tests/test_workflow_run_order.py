"""T6 run order declaration and its real execution consumption.

E1 covers the pure declaration module: five built-in big steps, a deterministic
ascending order independent of the declaration input order, and explicit errors
for duplicate identities/orders and invalid fields; those checks only read the
declaration and never build a service, database, lease or mutable global
registry.

E5 adds the real consumption side (142 号 §8.1): a temporary real round is
started through the public ``start`` entry and the instance-level stage entries
are wrapped once (calling the original implementation) to record the actual
entry sequence and the business boundary facts.  Replay, prepare failure,
post-commit notification failure and pause/resume keep the original replay,
transaction and release semantics.
"""

from contextlib import closing, contextmanager
import json
import os
from threading import Event

import pytest

import phase1_agent.workflow_run_order as run_order_module
from phase1_agent.capability_registry import create_package_registry
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.notification_results import (
    ROUND_NOTIFICATION_FAILURE,
    CommittedNotificationError,
)
from phase1_agent.workflow_run_order import (
    BUILTIN_STEPS,
    STAGE_BACKGROUND,
    STAGE_START,
    RunOrderError,
    RunStep,
    ordered_steps,
    steps_for_stage,
)

from test_graph_service import create, gate_graph, text_graph
from graph_test_plugin import register_content_probe

EXPECTED_STEPS = (
    RunStep("prepare", 100, "本轮准备", "graph.run.prepare", STAGE_START),
    RunStep("execute_graph", 1000, "整图执行", "graph.run.execute", STAGE_BACKGROUND),
    RunStep("settle", 2000, "业务结算", "graph.run.settle", STAGE_BACKGROUND),
    RunStep("notify", 2100, "提交后通知", "graph.run.notify", STAGE_BACKGROUND),
    RunStep("release", 2200, "结束释放", "graph.run.release", STAGE_BACKGROUND),
)


def _step(step_id="extra", order=5000, name="额外步骤", entry_ref="graph.run.extra",
          stage=STAGE_BACKGROUND):
    return RunStep(step_id, order, name, entry_ref, stage)


def test_builtin_steps_match_the_declared_identity():
    assert len(BUILTIN_STEPS) == 5
    assert type(BUILTIN_STEPS) is tuple
    # The declaration input order is the documented list order (not execution order).
    assert BUILTIN_STEPS == EXPECTED_STEPS
    assert [item.step_id for item in BUILTIN_STEPS] == [
        "prepare", "execute_graph", "settle", "notify", "release"]
    assert [item.order for item in BUILTIN_STEPS] == [100, 1000, 2000, 2100, 2200]
    assert [item.name for item in BUILTIN_STEPS] == [
        "本轮准备", "整图执行", "业务结算", "提交后通知", "结束释放"]
    assert [item.entry_ref for item in BUILTIN_STEPS] == [
        "graph.run.prepare", "graph.run.execute", "graph.run.settle",
        "graph.run.notify", "graph.run.release"]
    assert [item.stage for item in BUILTIN_STEPS] == [
        STAGE_START, STAGE_BACKGROUND, STAGE_BACKGROUND, STAGE_BACKGROUND,
        STAGE_BACKGROUND]
    assert all(type(item) is RunStep for item in BUILTIN_STEPS)
    # Symbolic trusted-local references only: never module paths or entrypoint strings.
    assert all(":" not in item.entry_ref and item.entry_ref.startswith("graph.run.")
               for item in BUILTIN_STEPS)


def test_ordered_steps_is_independent_of_declaration_input_order():
    descending = tuple(reversed(BUILTIN_STEPS))
    assert ordered_steps(descending) == BUILTIN_STEPS
    shuffled = (BUILTIN_STEPS[2], BUILTIN_STEPS[0], BUILTIN_STEPS[4],
                BUILTIN_STEPS[1], BUILTIN_STEPS[3])
    assert ordered_steps(shuffled) == BUILTIN_STEPS
    assert ordered_steps(descending) == ordered_steps(shuffled)
    assert [item.order for item in ordered_steps(descending)] == [100, 1000, 2000, 2100, 2200]
    assert ordered_steps([]) == ()


def test_duplicate_order_fails_explicitly_without_sorting_them_apart():
    with pytest.raises(RunOrderError) as error:
        ordered_steps((_step("first", 1500), _step("second", 1500)))
    assert "1500" in str(error.value)
    # A single declaration with that order stays usable: nothing was overwritten.
    assert ordered_steps((_step("first", 1500),)) == (_step("first", 1500),)


def test_duplicate_identity_fails_explicitly():
    with pytest.raises(RunOrderError) as error:
        ordered_steps((_step("same", 1500), _step("same", 1600)))
    assert "same" in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("step_id", ""), ("step_id", None),
    ("name", ""), ("name", None),
    ("entry_ref", ""), ("entry_ref", None),
])
def test_empty_or_missing_text_fields_fail_explicitly(field, value):
    step = _step(**{field: value})
    with pytest.raises(RunOrderError):
        ordered_steps((step,))


@pytest.mark.parametrize("order", [True, False, 100.0, "100", -1, 9001])
def test_invalid_order_values_fail_explicitly(order):
    with pytest.raises(RunOrderError):
        ordered_steps((_step(order=order),))


def test_order_bounds_are_inclusive():
    assert ordered_steps((_step("low", 0),))[0].order == 0
    assert ordered_steps((_step("high", 9000),))[0].order == 9000


@pytest.mark.parametrize("stage", ["", "sync", "BACKGROUND", None, 1])
def test_unknown_stage_fails_explicitly(stage):
    with pytest.raises(RunOrderError):
        ordered_steps((_step(stage=stage),))
    with pytest.raises(RunOrderError):
        steps_for_stage(stage)


def test_non_step_declarations_fail_explicitly():
    with pytest.raises(RunOrderError):
        ordered_steps(("prepare",))
    with pytest.raises(RunOrderError):
        ordered_steps((BUILTIN_STEPS[0], object()))
    with pytest.raises(RunOrderError):
        ordered_steps("prepare")


def test_steps_for_stage_returns_the_ascending_stage_subset():
    assert steps_for_stage(STAGE_START) == (BUILTIN_STEPS[0],)
    assert steps_for_stage(STAGE_BACKGROUND) == tuple(BUILTIN_STEPS[1:])
    assert [item.step_id for item in steps_for_stage(STAGE_BACKGROUND)] == [
        "execute_graph", "settle", "notify", "release"]
    # Selection follows the declaration, not the physical slice order.
    assert steps_for_stage(STAGE_BACKGROUND, tuple(reversed(BUILTIN_STEPS))) == (
        tuple(BUILTIN_STEPS[1:]))
    assert steps_for_stage(STAGE_START, ()) == ()
    two_stage = (_step("late", 2000), _step("early", 100, stage=STAGE_START))
    assert steps_for_stage(STAGE_START, two_stage) == (
        RunStep("early", 100, "额外步骤", "graph.run.extra", STAGE_START),)
    assert steps_for_stage(STAGE_BACKGROUND, two_stage) == (
        _step("late", 2000),)


def test_declaration_reads_are_pure_and_repeatable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = ordered_steps()
    first_start = steps_for_stage(STAGE_START)
    second = ordered_steps()
    second_start = steps_for_stage(STAGE_START)
    assert first == second == BUILTIN_STEPS
    assert first_start == second_start == (BUILTIN_STEPS[0],)
    assert sorted(tmp_path.iterdir()) == []
    assert os.listdir(tmp_path) == []
    # No mutable global registry is exposed by the declaration module.
    assert type(run_order_module.BUILTIN_STEPS) is tuple
    assert not hasattr(run_order_module, "register")


# --- E5: a real round consumes the declared order ---------------------------

_STAGE_ENTRIES = ("_run_prepare_stage", "_run_execute_stage", "_run_settle_stage",
                  "_run_notify_stage", "_run_release_stage")
_MISSING = object()


@pytest.fixture
def service(tmp_path):
    registry = create_package_registry().registry.detached()
    register_content_probe(registry)
    instance = GraphWorkflowService(tmp_path / "run-order.sqlite", registry=registry)
    try:
        yield instance
    finally:
        instance.close()


def _boundary_snapshot(service, sid):
    """Observable business boundary facts before/after one declared stage."""
    view = service.get_session(sid)
    chain_id = view["active_chain_run_id"] or view["selected_chain_run_id"]
    snapshot = {"active_chain_run_id": chain_id, "session_status": view["status"],
                "chain_status": None, "start_manifest_status": None,
                "end_manifest_status": None, "notification_recorded": False,
                "checkpoints": 0, "completed_commits": 0, "succeeded_nodes": 0}
    with closing(service._store()) as store:
        for row in store._connection.execute(
                "SELECT payload FROM records WHERE record_type='workflow_commit'"):
            payload = json.loads(row["payload"])
            if (payload.get("workflow_session_id") == sid
                    and (payload.get("source") or {}).get("kind") == "completed_execution"):
                snapshot["completed_commits"] += 1
    if chain_id is None:
        return snapshot
    detail = service.get_run(sid, chain_id)
    snapshot.update({
        "chain_status": detail["chain"]["status"],
        "start_manifest_status": (detail["state_manifests"]["start"] or {}).get("status"),
        "end_manifest_status": (detail["state_manifests"]["end"] or {}).get("status"),
        "notification_recorded": "round_notifications" in (
            detail["chain"].get("diagnostic") or {}),
        "checkpoints": len(service.list_checkpoints(sid)),
        "succeeded_nodes": sum(1 for row in detail["node_runs"]
                               if row["status"] == "succeeded"),
    })
    return snapshot


@contextmanager
def _recorded_stages(service, *, sid=None):
    """Wrap the real instance-level stage entries once, then call the originals."""
    entries, observations = [], []
    originals = {}
    for name in _STAGE_ENTRIES:
        originals[name] = service.__dict__.get(name, _MISSING)
        bound = getattr(service, name)

        def wrapper(*arguments, _name=name, _bound=bound, **keywords):
            before = _boundary_snapshot(service, sid) if sid is not None else None
            entries.append(_name)
            output = _bound(*arguments, **keywords)
            if sid is not None:
                observations.append({"entry": _name, "before": before,
                                     "after": _boundary_snapshot(service, sid)})
            return output

        setattr(service, name, wrapper)
    try:
        yield entries, observations
    finally:
        for name, original in originals.items():
            if original is _MISSING:
                service.__dict__.pop(name, None)
            else:
                setattr(service, name, original)


class _Gate:
    """Counting call record and blocking gate events for one gated round."""

    def __init__(self):
        self.calls = []
        self.entered = Event()
        self.release = Event()


def _gated_round(service):
    from phase1_agent.content_contracts import text_content
    from phase1_agent.graph_contracts import NodeDefinition, NodePort
    from test_graph_service import document, edge, node

    gate = _Gate()

    def first(config, inputs, context):
        gate.calls.append("first")
        return {"output": text_content("first")}

    def blocked(config, inputs, context):
        gate.calls.append("gated")
        gate.entered.set()
        assert gate.release.wait(10), "run order gate timed out"
        return {"output": text_content("gated")}

    service.registry.register(NodeDefinition(
        "test.run-order-first", "1", "First", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),)), first)
    service.registry.register(NodeDefinition(
        "test.run-order-gated", "1", "Gated", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),)), blocked)
    first_node = node(service.registry, "test.run-order-first", 1)
    gated_node = node(service.registry, "test.run-order-gated", 2)
    output_node = node(service.registry, "tools.output", 3)
    value = document([first_node, gated_node, output_node],
                     [edge(first_node, gated_node, 1), edge(gated_node, output_node, 2)])
    value["schema_version"] = 3
    for item in value["nodes"]:
        item["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    saved = service.update_definition(
        value, workflow_definition_id=value["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key="run-order-gated-save")
    initial = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key="run-order-gated-session")
    started = service.start(initial["workflow_session_id"],
                            expected_revision=initial["revision"],
                            idempotency_key="run-order-gated-start")
    return gate, initial["workflow_session_id"], started["active_chain_run_id"]


def test_real_round_consumes_declared_stages_in_order(service):
    session = create(service, text_graph(service.registry))
    sid = session["workflow_session_id"]
    with _recorded_stages(service, sid=sid) as (entries, observations):
        started = service.start(sid, expected_revision=session["revision"],
                                idempotency_key="run-order-consumption")
        chain_id = started["active_chain_run_id"]
        assert service.wait(chain_id) is None

    assert entries == list(_STAGE_ENTRIES)
    assert set(item["entry"] for item in observations) == set(_STAGE_ENTRIES)

    prepare = {item["entry"]: item for item in observations}["_run_prepare_stage"]
    assert prepare["before"]["active_chain_run_id"] is None
    assert prepare["before"]["chain_status"] is None
    assert prepare["after"]["chain_status"] == "prepared"
    assert prepare["after"]["start_manifest_status"] == "prepared"
    assert prepare["after"]["end_manifest_status"] is None
    assert prepare["after"]["completed_commits"] == 0
    assert prepare["after"]["succeeded_nodes"] == 0

    execute = {item["entry"]: item for item in observations}["_run_execute_stage"]
    assert execute["before"]["chain_status"] == "prepared"
    assert execute["before"]["end_manifest_status"] is None
    assert execute["after"]["chain_status"] == "running"
    assert execute["after"]["end_manifest_status"] is None
    assert execute["after"]["completed_commits"] == 0

    settle = {item["entry"]: item for item in observations}["_run_settle_stage"]
    assert settle["before"]["chain_status"] == "running"
    assert settle["before"]["completed_commits"] == 0
    assert settle["before"]["checkpoints"] == 0
    assert settle["before"]["end_manifest_status"] is None
    assert settle["before"]["notification_recorded"] is False
    assert settle["after"]["chain_status"] == "succeeded"
    assert settle["after"]["end_manifest_status"] == "succeeded"
    assert settle["after"]["completed_commits"] == 1
    assert settle["after"]["checkpoints"] == 1
    assert settle["after"]["notification_recorded"] is False

    notify = {item["entry"]: item for item in observations}["_run_notify_stage"]
    assert notify["before"]["chain_status"] == "succeeded"
    assert notify["before"]["notification_recorded"] is False
    assert notify["after"]["notification_recorded"] is True

    completed = service.get_session(sid)
    assert completed["status"] == "succeeded"
    assert len(completed["chains"]) == 1
    detail = service.get_run(sid, chain_id)
    assert detail["chain"]["status"] == "succeeded"
    assert all(row["status"] == "succeeded" for row in detail["node_runs"])
    recorded = detail["chain"]["diagnostic"]["round_notifications"]
    assert recorded["business_status"] == "succeeded"
    assert [row["outcome"] for row in recorded["notifications"]] == [
        "succeeded", "succeeded"]
    assert "pear pear" in json.dumps(
        [row["payload"] for row in detail["outputs"]], ensure_ascii=False)
    for mapping in (service._resource_frames, service._execution_registries,
                    service._acceptance_candidates, service._failed_retry_candidates):
        assert chain_id not in mapping


def test_replayed_start_enters_only_prepare_shell_without_background(service):
    session = create(service, text_graph(service.registry))
    sid = session["workflow_session_id"]
    with _recorded_stages(service) as (entries, _observations):
        started = service.start(sid, expected_revision=session["revision"],
                                idempotency_key="run-order-replay")
        chain_id = started["active_chain_run_id"]
        assert service.wait(chain_id) is None
        assert entries == list(_STAGE_ENTRIES)
        accepted_count = len(entries)
        replay = service.start(sid, expected_revision=session["revision"],
                               idempotency_key="run-order-replay")
        assert replay["active_chain_run_id"] == chain_id
        # The receipt-replay shell still enters 0100 once to read its receipt,
        # but the prepare transaction and all four background stages stay absent.
        assert entries[accepted_count:] == ["_run_prepare_stage"]

    view = service.get_session(sid)
    assert view["status"] == "succeeded"
    assert [row["chain_run_id"] for row in view["chains"]] == [chain_id]
    detail = service.get_run(sid, chain_id)
    assert detail["chain"]["status"] == "succeeded"
    assert len(detail["node_runs"]) == 3


def test_prepare_failure_leaves_no_chain_and_retry_succeeds(service):
    session = create(service, text_graph(service.registry))
    sid = session["workflow_session_id"]
    armed = [True]

    def fail_before_commit(point):
        if armed[0] and point == "before_commit":
            raise RuntimeError("run order prepare probe failed")

    service._fault_injector = fail_before_commit
    try:
        with pytest.raises(RuntimeError):
            service.start(sid, expected_revision=session["revision"],
                          idempotency_key="run-order-prepare-fail")
    finally:
        armed[0] = False
        service._fault_injector = None

    failed = service.get_session(sid)
    assert failed["active_chain_run_id"] is None
    assert failed["chains"] == []
    assert failed["revision"] == session["revision"]
    # Only this invocation's newly created frames were cleaned up, and no
    # background invocation was scheduled for the rolled-back prepare.
    assert service._resource_frames == {}
    assert service._execution_registries == {}
    assert service._futures == {}

    with _recorded_stages(service) as (entries, _observations):
        retried = service.start(sid, expected_revision=failed["revision"],
                                idempotency_key="run-order-prepare-retry")
        chain_id = retried["active_chain_run_id"]
        assert service.wait(chain_id) is None
    assert entries == list(_STAGE_ENTRIES)
    completed = service.get_session(sid)
    assert completed["status"] == "succeeded"
    assert [row["chain_run_id"] for row in completed["chains"]] == [chain_id]


def test_post_commit_notification_failure_is_reported_and_release_runs(service):
    session = create(service, text_graph(service.registry))
    sid = session["workflow_session_id"]
    original = service._refresh_chat_snapshots

    def failing_chat_snapshot(execution_sid):
        raise RuntimeError("run order notification probe failed")

    service._refresh_chat_snapshots = failing_chat_snapshot
    try:
        with _recorded_stages(service, sid=sid) as (entries, observations):
            started = service.start(sid, expected_revision=session["revision"],
                                    idempotency_key="run-order-notification-fail")
            chain_id = started["active_chain_run_id"]
            with pytest.raises(CommittedNotificationError) as raised:
                service.wait(chain_id)
    finally:
        service._refresh_chat_snapshots = original

    error = raised.value
    assert error.reason_code == ROUND_NOTIFICATION_FAILURE
    assert error.business_status == "succeeded"
    assert error.business_committed is True
    assert [(row["stage"], row["outcome"]) for row in error.notifications] == [
        ("chat_snapshot", "failed"), ("frontend_node_state", "not_executed")]
    assert "run order notification probe failed" not in json.dumps(
        error.to_dict(), ensure_ascii=False)

    assert entries == list(_STAGE_ENTRIES)
    assert entries.count("_run_notify_stage") == 1
    assert entries.count("_run_release_stage") == 1
    by_entry = {item["entry"]: item for item in observations}
    assert by_entry["_run_notify_stage"]["after"]["notification_recorded"] is True
    assert by_entry["_run_notify_stage"]["after"]["chain_status"] == "succeeded"
    assert by_entry["_run_release_stage"]["after"]["chain_status"] == "succeeded"

    completed = service.get_session(sid)
    assert completed["status"] == "succeeded"
    detail = service.get_run(sid, chain_id)
    assert detail["chain"]["status"] == "succeeded"
    assert detail["state_manifests"]["end"]["status"] == "succeeded"
    # The committed business result and output are untouched by the local
    # notification failure, and the release stage still cleaned this chain.
    assert all(row["status"] == "succeeded" for row in detail["node_runs"])
    for mapping in (service._resource_frames, service._execution_registries,
                    service._acceptance_candidates, service._failed_retry_candidates):
        assert chain_id not in mapping


def test_pause_then_resume_continues_background_without_prepare_or_rerun(service):
    gate, sid, chain_id = _gated_round(service)
    try:
        assert gate.entered.wait(5), "gated node did not start"
        running = service.get_session(sid)
        assert running["status"] == "running"
        service.control(sid, action="pause", expected_revision=running["revision"],
                        idempotency_key="run-order-pause")
    finally:
        gate.release.set()
    service.wait(chain_id)

    paused = service.get_session(sid)
    assert paused["status"] == "paused"
    paused_detail = service.get_run(sid, chain_id)
    accepted = list(paused_detail["chain"]["completed_nodes"])
    assert accepted, paused_detail["chain"]
    accepted_runs = {row["node_binding_id"]: row["run_id"]
                     for row in paused_detail["node_runs"]}
    calls_after_pause = list(gate.calls)
    assert calls_after_pause == ["first", "gated"]
    retained_basis = service._execution_registries[chain_id]

    original_require_basis = service._require_run_basis
    original_capture_basis = service._capture_run_basis
    seen_basis, captures = [], []

    def record_basis(identity):
        basis = original_require_basis(identity)
        seen_basis.append(basis)
        return basis

    def record_capture():
        captures.append(True)
        return original_capture_basis()

    service._require_run_basis = record_basis
    service._capture_run_basis = record_capture
    try:
        with _recorded_stages(service) as (entries, _observations):
            paused = service.get_session(sid)
            resumed = service.control(sid, action="resume",
                                      expected_revision=paused["revision"],
                                      idempotency_key="run-order-resume")
            assert resumed["active_chain_run_id"] == chain_id
            assert service.wait(chain_id) is None
    finally:
        service.__dict__.pop("_require_run_basis", None)
        service.__dict__.pop("_capture_run_basis", None)

    # Resume continues the background stages only: 0100 is never re-entered and
    # the retained RunBasis is not re-captured or replaced.
    assert entries == ["_run_execute_stage", "_run_settle_stage",
                       "_run_notify_stage", "_run_release_stage"]
    assert entries.count("_run_prepare_stage") == 0
    assert captures == []
    assert seen_basis and all(item is retained_basis for item in seen_basis)
    # Accepted nodes keep their original node-run identities and are not re-run.
    assert gate.calls == calls_after_pause
    completed = service.get_session(sid)
    assert completed["status"] == "succeeded"
    detail = service.get_run(sid, chain_id)
    assert detail["chain"]["status"] == "succeeded"
    assert detail["chain"]["completed_nodes"] == detail["chain"]["ordered_nodes"]
    assert set(accepted) <= set(detail["chain"]["completed_nodes"])
    completed_runs = {row["node_binding_id"]: row["run_id"]
                      for row in detail["node_runs"]}
    assert {node_id: completed_runs[node_id] for node_id in accepted} == {
        node_id: accepted_runs[node_id] for node_id in accepted}
    assert detail["chain"]["ordered_nodes"] == paused_detail["chain"]["ordered_nodes"]
    assert service._execution_registries.get(chain_id) is None


def _reject_background(monkeypatch):
    import phase1_agent.graph_service as service_module
    original = service_module.steps_for_stage

    def steps(stage):
        result = original(stage)
        return result[:-1] if stage == STAGE_BACKGROUND else result

    monkeypatch.setattr(service_module, "steps_for_stage", steps)


def test_invalid_background_declaration_rejects_before_prepare(service, monkeypatch):
    from phase1_agent.contract_errors import ContractValidationError
    session = create(service, text_graph(service.registry))
    _reject_background(monkeypatch)
    with pytest.raises(ContractValidationError, match="declaration and bindings differ"):
        service.start(session["workflow_session_id"], expected_revision=session["revision"],
                      idempotency_key="invalid-order")
    assert service.get_session(session["workflow_session_id"]) == session
    assert service._resource_frames == {}
    assert service._execution_registries == {}
    assert service._futures == {}


def test_replay_precedes_invalid_background_declaration(service, monkeypatch):
    session = create(service, text_graph(service.registry))
    arguments = dict(expected_revision=session["revision"], idempotency_key="valid-then-replay")
    started = service.start(session["workflow_session_id"], **arguments)
    service.wait(started["active_chain_run_id"])
    _reject_background(monkeypatch)
    assert service.start(session["workflow_session_id"], **arguments) == started


def test_resume_declaration_rejection_preserves_paused_records_and_frames(service, monkeypatch):
    from phase1_agent.contract_errors import ContractValidationError
    gate, sid, chain_id = _gated_round(service)
    try:
        assert gate.entered.wait(5)
        running = service.get_session(sid)
        service.control(sid, action="pause", expected_revision=running["revision"],
                        idempotency_key="invalid-order-pause")
    finally:
        gate.release.set()
    service.wait(chain_id)
    paused = service.get_session(sid)
    detail = service.get_run(sid, chain_id)
    frame = service._resource_frames[chain_id]
    basis = service._execution_registries[chain_id]
    calls = list(gate.calls)
    _reject_background(monkeypatch)
    with pytest.raises(ContractValidationError, match="declaration and bindings differ"):
        service.control(sid, action="resume", expected_revision=paused["revision"],
                        idempotency_key="invalid-order-resume")
    assert service.get_session(sid) == paused
    assert service.get_run(sid, chain_id) == detail
    assert service._resource_frames[chain_id] is frame
    assert service._execution_registries[chain_id] is basis
    assert chain_id not in service._resuming
    assert gate.calls == calls


def test_background_consumes_captured_declaration(service, monkeypatch):
    import phase1_agent.graph_service as service_module
    original = service._executor.submit
    captured = []

    def submit(callback, sid, chain_id, steps):
        captured.append(steps)
        def changed(_stage):
            raise AssertionError("declaration was read after prepare committed")
        monkeypatch.setattr(service_module, "steps_for_stage", changed)
        return original(callback, sid, chain_id, steps)

    monkeypatch.setattr(service._executor, "submit", submit)
    session = create(service, text_graph(service.registry))
    started = service.start(session["workflow_session_id"], expected_revision=session["revision"],
                            idempotency_key="captured-order")
    service.wait(started["active_chain_run_id"])
    assert captured == [BUILTIN_STEPS[1:]]
    assert service.get_session(session["workflow_session_id"])["status"] == "succeeded"
