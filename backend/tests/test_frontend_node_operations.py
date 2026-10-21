"""Bound frontend operations use existing configuration/run transactions."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event, Thread
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.frontend_node_operations import FrontendNodeOperations
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_records import graph_error

from frontend_node_test_support import create as _create, graph as _graph, save as _save
from test_graph_service import node


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "frontend-operations.sqlite")) as instance:
        yield instance


def prepared(service, *, bound=True, session=False):
    graph, messages, frontends = _graph(service, bound=bound)
    graph = _save(service, graph)
    view = _create(service, graph) if session else None
    binding = {
        "workflow_definition_id": graph["workflow_definition_id"], "definition_revision": graph["revision"],
        "node_binding_id": frontends[0]["node_binding_id"],
        "session_id": view["workflow_session_id"] if view else None,
    }
    return FrontendNodeOperations(service).bind(**binding), graph, messages, view


def storage(service):
    with closing(service._store()) as store:
        return {
            table: [tuple(row) for row in store._connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in ("records", "workflow_current_configuration", "idempotency")
        }


def assert_another_thread_can_take_service_lock(service):
    acquired = Event()

    def take_lock():
        with service._lock:
            acquired.set()

    worker = Thread(target=take_lock)
    worker.start()
    try:
        assert acquired.wait(1), "Frontend facade held the service lock across native dispatch"
    finally:
        worker.join(timeout=1)


def test_bind_is_exact_read_only_and_has_no_caller_target(service):
    bound, graph, messages, view = prepared(service)
    before = storage(service)
    second = FrontendNodeOperations(service).bind(**bound._identity())
    assert second == bound and storage(service) == before
    with pytest.raises(TypeError):
        bound.patch_input(
            "spoof", node_binding_id=messages[0]["node_binding_id"],
            expected_edit_sequence=1, idempotency_key="spoof")
    with pytest.raises(DiagnosticError, match="exactly frontend.node"):
        FrontendNodeOperations(service).bind(
            **{**bound._identity(), "node_binding_id": messages[0]["node_binding_id"]})
    with pytest.raises(DiagnosticError):
        FrontendNodeOperations(service).bind(**{**bound._identity(), "definition_revision": True})
    assert storage(service) == before


def test_binding_rejects_another_workflow_session(service):
    first, _, _, _ = prepared(service)
    _, _, _, other = prepared(service, session=True)
    with pytest.raises(DiagnosticError) as failure:
        FrontendNodeOperations(service).bind(**{**first._identity(), "session_id": other["workflow_session_id"]})
    assert failure.value.reason_code == "frontend_node_session_mismatch"


def test_binding_rejects_another_registered_frontend_contract_version(service):
    definition = service.registry.get("frontend.node", "2").definition
    service.registry.register(replace(definition, component_version="3"), lambda *args: {})
    graph, _, frontends = _graph(service)
    graph["nodes"][1]["component_version"] = "3"
    graph = _save(service, graph)
    before = storage(service)
    with pytest.raises(DiagnosticError) as failure:
        FrontendNodeOperations(service).bind(
            workflow_definition_id=graph["workflow_definition_id"], definition_revision=1,
            node_binding_id=frontends[0]["node_binding_id"])
    assert failure.value.reason_code == "frontend_node_binding_mismatch"
    assert storage(service) == before


def test_patch_uses_own_target_and_own_original_receipt_before_current_graph(service, monkeypatch):
    bound, graph, messages, _ = prepared(service)
    original = {"text": "edited", "expected_edit_sequence": 1, "idempotency_key": "patch-original"}
    changed = bound.patch_input(**original)
    assert changed["outcome"] == "saved" and changed["changed_categories"] == ["adjustable"]
    assert changed["definition_revision"] == 1 and changed["edit_sequence"] == 2
    assert changed["document"]["nodes"][0]["config"]["text"] == "edited"
    assert changed["document"]["nodes"][1]["config"] == graph["nodes"][1]["config"]
    with closing(service._store()) as store:
        assert store.read_receipt("graph.frontend-node.patch-input", "patch-original") is not None
        assert store.read_receipt("graph.configuration.patch-node", "patch-original") is None
    monkeypatch.setattr(service, "_document", lambda *args: pytest.fail("replay reread current graph"))
    monkeypatch.setattr(
        "phase1_agent.configuration_data.ConfigurationDataSession.fixed_document",
        lambda *args: pytest.fail("replay reread current graph"))
    assert bound.patch_input(**original) == changed
    assert bound.read_receipt("patch_input", original) == changed
    with pytest.raises(ContractValidationError) as failure:
        bound.patch_input(**{**original, "text": "different"})
    assert failure.value.reason_code == "idempotency_conflict"


def test_unbound_input_is_not_patchable_but_graph_can_start(service):
    bound, _, _, _ = prepared(service, bound=False)
    before = storage(service)
    with pytest.raises(DiagnosticError) as failure:
        bound.patch_input("x", expected_edit_sequence=1, idempotency_key="unbound")
    assert failure.value.reason_code == "frontend_node_input_unbound"
    assert storage(service) == before
    result = bound.start(idempotency_key="unbound-start")
    assert result["outcome"] == "succeeded", result
    service.wait(result["start_result"]["active_chain_run_id"])
    assert service.get_session(result["create_result"]["workflow_session_id"])["status"] == "succeeded"


def test_patch_fixed_input_preserves_confirmation_and_derivation(service):
    graph, _, frontends = _graph(service, bound=True)
    graph["nodes"][0]["configuration_policy"]["prompt_mode"] = "fixed"
    graph = _save(service, graph)
    session = _create(service, graph)
    bound = FrontendNodeOperations(service).bind(
        workflow_definition_id=graph["workflow_definition_id"], definition_revision=1,
        node_binding_id=frontends[0]["node_binding_id"], session_id=session["workflow_session_id"])
    before = service.get_definition(graph["workflow_definition_id"])
    asked = bound.patch_input("fixed edit", expected_edit_sequence=1, idempotency_key="ask")
    assert asked["outcome"] == "confirmation_required"
    assert service.get_definition(graph["workflow_definition_id"]) == before
    derived = bound.patch_input(
        "fixed edit", expected_edit_sequence=1, confirm_derivation=True, idempotency_key="confirm")
    assert derived["outcome"] == "derived"
    assert derived["workflow_definition_id"] != graph["workflow_definition_id"]
    assert derived["node_id_map"][bound.node_binding_id] == bound.node_binding_id
    assert service.get_definition(graph["workflow_definition_id"]) == before
    assert bound.patch_input("fixed edit", expected_edit_sequence=1, idempotency_key="ask") == asked


@pytest.mark.parametrize("arguments", [
    {"text": 1, "expected_edit_sequence": 1},
    {"text": "x", "expected_edit_sequence": True},
    {"text": "x", "expected_edit_sequence": 0},
    {"text": "x", "expected_edit_sequence": 1, "confirm_derivation": "yes"},
])
def test_patch_invalid_or_stale_arguments_write_nothing(service, arguments):
    bound, _, _, _ = prepared(service)
    before = storage(service)
    with pytest.raises(DiagnosticError):
        bound.patch_input(**arguments, idempotency_key="invalid")
    assert storage(service) == before


def test_patch_configuration_and_receipt_roll_back_together(service):
    bound, _, _, _ = prepared(service)
    before = storage(service)

    def fail(stage):
        if stage == "after_configuration_write":
            raise RuntimeError("test configuration failure")

    service._fault_injector = fail
    try:
        with pytest.raises(RuntimeError, match="test configuration failure"):
            bound.patch_input("rolled back", expected_edit_sequence=1, idempotency_key="rollback")
    finally:
        service._fault_injector = None
    assert storage(service) == before


def test_read_snapshot_delegates_only_exact_binding(service, monkeypatch):
    bound, _, _, view = prepared(service, session=True)
    calls = []
    snapshot = {"chat": {"rounds": []}}
    monkeypatch.setattr(
        service, "read_frontend_node_snapshot",
        lambda *args: calls.append(args) or deepcopy(snapshot), raising=False)
    assert bound.read_snapshot() == snapshot
    assert calls == [(bound.workflow_definition_id, bound.definition_revision,
                      bound.node_binding_id, view["workflow_session_id"])]


def test_bound_snapshot_reads_actual_accepted_messages_and_none_session_is_rejected(service):
    unstarted, _, _, _ = prepared(service)
    with pytest.raises(ContractValidationError):
        unstarted.read_snapshot()
    bound, _, messages, view = prepared(service, session=True)
    empty = bound.read_snapshot()
    assert empty["chat"] == {"rounds": []}
    started = bound.start(expected_session_revision=view["revision"], idempotency_key="snapshot-start")
    service.wait(started["start_result"]["active_chain_run_id"])
    current = bound.read_snapshot()
    assert current["owner"]["node_binding_id"] == bound.node_binding_id
    assert current["owner"]["workflow_session_id"] == bound.session_id
    assert current["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] == (
        messages[0]["config"]["text"])
    current["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] = "local"
    assert bound.read_snapshot()["chat"]["rounds"][0]["candidates"][0]["messages"][0]["text"] == (
        messages[0]["config"]["text"])


def test_start_create_and_start_have_separate_native_receipts_and_no_replay_dispatch(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    first = bound.start(idempotency_key="new-start")
    assert first["outcome"] == "succeeded" and first["failure_phase"] is None
    created, started = first["create_result"], first["start_result"]
    assert created["revision"] == 1 and created["active_chain_run_id"] is None
    assert started["workflow_session_id"] == created["workflow_session_id"]
    assert started["active_chain_run_id"] is not None
    service.wait(started["active_chain_run_id"])
    bound.patch_input("later", expected_edit_sequence=1, idempotency_key="later-input")
    monkeypatch.setattr(service, "create_session", lambda *args, **kwargs: pytest.fail("recreated session"))
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: pytest.fail("restarted session"))
    monkeypatch.setattr(service, "_document", lambda *args: pytest.fail("replay reread binding"))
    assert bound.start(idempotency_key="new-start") == first
    assert bound.read_receipt(
        "start", {"expected_session_revision": None, "idempotency_key": "new-start"}) == first


def test_start_new_session_basis_is_original_create_receipt(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    native_create = service.create_session
    native_start = service.start

    def misleading_create(*args, **kwargs):
        assert_another_thread_can_take_service_lock(service)
        actual = native_create(*args, **kwargs)
        return {**actual, "revision": 9999, "workflow_session_id": str(uuid4())}

    def checked_start(*args, **kwargs):
        assert_another_thread_can_take_service_lock(service)
        return native_start(*args, **kwargs)

    monkeypatch.setattr(service, "create_session", misleading_create)
    monkeypatch.setattr(service, "start", checked_start)
    result = bound.start(idempotency_key="receipt-basis")
    assert result["outcome"] == "succeeded", result
    assert result["create_result"]["revision"] == 1
    assert result["start_result"]["workflow_session_id"] == result["create_result"]["workflow_session_id"]
    service.wait(result["start_result"]["active_chain_run_id"])


def test_concurrent_same_key_start_observes_plan_without_dispatching_again(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    entered, release = Event(), Event()
    create_calls, start_calls, results, errors = [], [], [], []
    native_create, native_start = service.create_session, service.start

    def blocked_create(*args, **kwargs):
        create_calls.append(kwargs)
        entered.set()
        assert release.wait(10), "Concurrent frontend creation gate timed out"
        return native_create(*args, **kwargs)

    def counted_start(*args, **kwargs):
        start_calls.append(kwargs)
        return native_start(*args, **kwargs)

    def first_start():
        try:
            results.append(bound.start(idempotency_key="concurrent-start"))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(service, "create_session", blocked_create)
    monkeypatch.setattr(service, "start", counted_start)
    first = Thread(target=first_start)
    first.start()
    try:
        assert entered.wait(5), "First start did not reach creation after committing its plan"
        observed = bound.start(idempotency_key="concurrent-start")
        assert observed == {
            "operation": "start", "outcome": "unknown", "create_result": None,
            "start_result": None, "failure_phase": "create", "error": None,
        }
        assert len(create_calls) == 1 and start_calls == []
    finally:
        release.set()
        first.join(timeout=10)
    assert not first.is_alive(), "First frontend start did not finish"
    assert errors == [] and len(results) == 1
    accepted = results[0]
    assert accepted["outcome"] == "succeeded" and accepted["failure_phase"] is None
    assert accepted["create_result"] is not None and accepted["start_result"] is not None
    assert len(create_calls) == 1 and len(start_calls) == 1
    service.wait(accepted["start_result"]["active_chain_run_id"])
    assert bound.start(idempotency_key="concurrent-start") == accepted
    assert bound.read_receipt("start", {"idempotency_key": "concurrent-start"}) == accepted
    assert len(create_calls) == 1 and len(start_calls) == 1


def test_create_success_start_failure_returns_both_phase_and_actual_create_and_does_not_retry(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    calls = []

    def rejected(*args, **kwargs):
        calls.append(kwargs)
        raise DiagnosticError("test_start_rejected", "Test start rejection", status=409)

    monkeypatch.setattr(service, "start", rejected)
    failed = bound.start(idempotency_key="partial-start")
    assert failed["outcome"] == "failed" and failed["failure_phase"] == "start"
    assert failed["create_result"]["revision"] == 1 and failed["start_result"] is None
    assert failed["error"]["reason_code"] == "test_start_rejected"
    replay = bound.start(idempotency_key="partial-start")
    assert replay["outcome"] == "unknown" and replay["failure_phase"] == "start"
    assert replay["create_result"] == failed["create_result"] and replay["start_result"] is None
    assert len(calls) == 1


def test_missing_create_receipt_never_retries_unknown_dispatch(service, monkeypatch):
    bound, _, _, _ = prepared(service)
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("not public")

    monkeypatch.setattr(service, "create_session", interrupted)
    result = bound.start(idempotency_key="unknown-create")
    assert result["outcome"] == "unknown" and result["failure_phase"] == "create"
    assert result["create_result"] is None and result["start_result"] is None
    assert result["error"] == {"reason_code": "workflow_operation_dispatch_unknown"}
    assert bound.start(idempotency_key="unknown-create")["outcome"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("phase", ["create", "start"])
def test_native_transaction_failure_reports_actual_phase_and_leaves_no_partial_native_write(
        service, monkeypatch, phase):
    bound, _, _, _ = prepared(service)

    def reject(*args, **kwargs):
        raise graph_error("test_native_rejected", "Native transaction rejected", 409)

    monkeypatch.setattr(service, "_new_session" if phase == "create" else "_prepare_run", reject)
    result = bound.start(idempotency_key="native-failure")
    assert result["outcome"] == "failed" and result["failure_phase"] == phase
    assert result["error"]["reason_code"] == "test_native_rejected"
    assert (result["create_result"] is None) == (phase == "create")
    assert result["start_result"] is None
    with closing(service._store()) as store:
        assert store._connection.execute(
            "SELECT count(*) FROM records WHERE record_type='chain_run'").fetchone()[0] == 0
        assert store._connection.execute(
            "SELECT count(*) FROM records WHERE record_type='workflow_session'").fetchone()[0] == (
                0 if phase == "create" else 1)
    replay = bound.start(idempotency_key="native-failure")
    assert replay["outcome"] == "unknown" and replay["failure_phase"] == phase


def test_start_existing_session_requires_original_revision_and_creates_none(service):
    bound, _, _, view = prepared(service, session=True)
    with pytest.raises(DiagnosticError):
        bound.start(idempotency_key="missing-revision")
    result = bound.start(expected_session_revision=view["revision"], idempotency_key="existing-start")
    assert result["outcome"] == "succeeded" and result["create_result"] is None
    service.wait(result["start_result"]["active_chain_run_id"])
    assert bound.start(
        expected_session_revision=view["revision"], idempotency_key="existing-start") == result
    with pytest.raises(ContractValidationError) as failure:
        bound.start(expected_session_revision=view["revision"] + 1, idempotency_key="existing-start")
    assert failure.value.reason_code == "idempotency_conflict"


def test_start_new_request_does_not_rebase_changed_fixed_definition(service):
    bound, graph, _, _ = prepared(service)
    changed = deepcopy(graph)
    changed["nodes"][1]["config"]["role"] = "user"
    saved = service.update_definition(
        changed, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=1, idempotency_key="change-fixed")
    assert saved["definition_revision"] == 2
    before = storage(service)
    with pytest.raises(DiagnosticError) as failure:
        bound.start(idempotency_key="stale-start")
    assert failure.value.reason_code == "stale_revision" and storage(service) == before


def test_patch_replay_keeps_original_result_after_saved_frontend_binding_changes(service):
    bound, graph, _, _ = prepared(service)
    request = {"text": "first", "expected_edit_sequence": 1, "idempotency_key": "before-rebinding"}
    original = bound.patch_input(**request)
    changed = service.get_definition(graph["workflow_definition_id"])
    changed["nodes"][1]["config"]["input_node_id"] = ""
    updated = service.update_definition(
        changed, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=2, idempotency_key="unbind-input")
    assert updated["definition_revision"] == 2
    assert bound.patch_input(**request) == original
    with pytest.raises(DiagnosticError) as failure:
        bound.patch_input("second", expected_edit_sequence=3, idempotency_key="after-rebinding")
    assert failure.value.reason_code == "stale_revision"


def test_pause_while_running_uses_formal_control_and_replay_does_not_reapply_pause(service, monkeypatch):
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "frontend operation gate timed out"
        return {}

    service.registry.register(NodeDefinition(
        "test.frontend-operation-gate", "1", "Gate", "Test", {}, {"type": "object"}, is_output=True), gate)
    graph, _, frontends = _graph(service, bound=True)
    downstream = node(service.registry, "test.frontend-operation-gate", 19030)
    downstream["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    graph["nodes"].append(downstream)
    graph["control_edges"] = [{
        "edge_id": str(uuid4()), "source_node_id": frontends[0]["node_binding_id"],
        "target_node_id": downstream["node_binding_id"],
    }]
    graph = _save(service, graph)
    initial = _create(service, graph)
    bound = FrontendNodeOperations(service).bind(
        workflow_definition_id=graph["workflow_definition_id"], definition_revision=1,
        node_binding_id=frontends[0]["node_binding_id"], session_id=initial["workflow_session_id"])
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                            idempotency_key="gate-start")
    try:
        assert entered.wait(5)
        running = service.get_session(initial["workflow_session_id"])
        assert running["status"] == "running"
        native_control = service.control

        def checked_control(*args, **kwargs):
            assert_another_thread_can_take_service_lock(service)
            return native_control(*args, **kwargs)

        monkeypatch.setattr(service, "control", checked_control)
        result = bound.pause(expected_session_revision=running["revision"], idempotency_key="pause-running")
        assert result["outcome"] == "succeeded"
        assert result["control_result"]["status"] == "running"
        chain_id = started["active_chain_run_id"]
        assert chain_id in service._pauses
        service._pauses.discard(chain_id)
        monkeypatch.setattr(service, "control", lambda *args, **kwargs: pytest.fail("control replayed"))
        assert bound.pause(
            expected_session_revision=running["revision"], idempotency_key="pause-running") == result
        assert chain_id not in service._pauses
    finally:
        release.set()
        service.wait(started["active_chain_run_id"])


def test_receipt_reader_cannot_choose_generic_operation_or_other_binding(service):
    bound, _, _, _ = prepared(service)
    with pytest.raises(DiagnosticError):
        bound.read_receipt("graph.run.start", {"idempotency_key": "x"})
    with pytest.raises(DiagnosticError):
        bound.read_receipt("start", {
            "idempotency_key": "x", "expected_session_revision": None, "node_binding_id": str(uuid4())})
    assert bound.read_receipt(
        "start", {"idempotency_key": "absent", "expected_session_revision": None}) is None


def test_receipt_reader_rejects_same_key_from_another_bound_node(service):
    graph, _, frontends = _graph(service, bound=True, count=2)
    graph = _save(service, graph)
    nodes = [FrontendNodeOperations(service).bind(
        workflow_definition_id=graph["workflow_definition_id"], definition_revision=1,
        node_binding_id=frontend["node_binding_id"]) for frontend in frontends]
    request = {"text": "own input", "expected_edit_sequence": 1, "idempotency_key": "own-receipt"}
    nodes[0].patch_input(**request)
    with pytest.raises(DiagnosticError) as failure:
        nodes[1].read_receipt("patch_input", request)
    assert failure.value.reason_code == "idempotency_conflict"
