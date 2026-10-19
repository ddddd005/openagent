"""Cross-layer proof: public entries, execution ports, persisted consumers."""

from contextlib import closing
from copy import deepcopy
import inspect
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.content_contracts import text_content
from phase1_agent.execution_data import (
    ConfigurationReadSession,
    ExecutionRecords,
    ExecutionSettlement,
    ExecutionState,
    ExecutionTypeEvidence,
)
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.workflow_operations import WorkflowOperations

from test_graph_service import create, document, edge, node, text_graph


def _track(monkeypatch, calls, owner, *names):
    for name in names:
        original = getattr(owner, name)
        label = f"{owner.__name__}.{name}"

        def wrapper(*args, __method=original, __label=label, **kwargs):
            calls[__label] = calls.get(__label, 0) + 1
            return __method(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)


def _output_payload(session, entry):
    return next(item["outputs"]["output"] for item in session["nodes"]
                if item["node_binding_id"] == entry["node_binding_id"])


def _observation(view, node_id, port_id="output"):
    return next(row for row in view["outputs"]
                if row["node_binding_id"] == node_id and row["port_id"] == port_id)


@pytest.fixture
def service(tmp_path):
    registry = create_package_registry().registry.detached()
    instance = GraphWorkflowService(
        tmp_path / "execution-cross-layer-control.sqlite", registry=registry)
    try:
        yield instance
    finally:
        instance.close()


def test_public_round_consumes_execution_ports_and_consumers_read_after_reopen(
        tmp_path, monkeypatch):
    executions = []
    registry = create_package_registry().registry.detached()

    def probe(config, inputs, context):
        executions.append(context.node_run_id)
        return {"output": inputs["input"]}

    registry.register(NodeDefinition(
        "test.cross-layer-probe", "1", "Cross-layer probe", "Test", {},
        {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), probe)
    path = tmp_path / "execution-cross-layer.sqlite"
    calls = {}
    with closing(GraphWorkflowService(path, registry=registry)) as service:
        source = node(service.registry, "tools.text", 1, text="cross layer")
        middle = node(service.registry, "test.cross-layer-probe", 2)
        output = node(service.registry, "tools.output", 3)
        output["public_outputs"] = ["output"]
        graph = document([source, middle, output], [
            edge(source, middle, 1), edge(middle, output, 2)])
        graph["package_lock"] = list(service.registry.execution_package_lock)
        view = create(service, graph)
        sid = view["workflow_session_id"]

        _track(monkeypatch, calls, ConfigurationReadSession, "fixed_document")
        _track(monkeypatch, calls, ExecutionTypeEvidence, "check_registry")
        _track(monkeypatch, calls, ExecutionRecords, "put_node_run", "put_chain")
        _track(monkeypatch, calls, ExecutionSettlement, "commit", "save_manifest")
        _track(monkeypatch, calls, ExecutionState, "variables")

        started = service.start(
            sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert _output_payload(final, middle) == text_content("cross layer")
        assert _output_payload(final, output) == text_content("cross layer")
        assert len(executions) == 1
        for name in (
            "ConfigurationReadSession.fixed_document",
            "ExecutionTypeEvidence.check_registry",
            "ExecutionRecords.put_node_run",
            "ExecutionRecords.put_chain",
            "ExecutionSettlement.commit",
            "ExecutionSettlement.save_manifest",
            "ExecutionState.variables",
        ):
            assert calls.get(name, 0) > 0, name

        stored = service.get_run(sid, chain_id)
        assert stored["chain"]["status"] == "succeeded"
        assert [run["status"] for run in stored["node_runs"]] == [
            "succeeded", "succeeded", "succeeded"]
        assert stored["runtime_facts"] == []
        checkpoints = service.list_checkpoints(sid)
        assert len(checkpoints) == 1
        selected = service.read_checkpoint(
            sid, checkpoint_id=checkpoints[0]["checkpoint_id"])
        assert selected["checkpoint_id"] == checkpoints[0]["checkpoint_id"]
        application = GraphApplication(service)
        current = application.query("output.list", {"session_id": sid})
        observed = _observation(current, output["node_binding_id"])
        assert observed["availability"] == "produced"
        assert observed["payload"] == text_content("cross layer")
        history = application.query("output.history", {
            "session_id": sid,
            "workflow_definition_id": graph["workflow_definition_id"],
            "definition_revision": 1,
            "node_id": output["node_binding_id"],
            "port_id": "output",
        })
        assert [row["payload"] for row in history["outputs"]] == [
            text_content("cross layer")]
        state = WorkflowOperations(service).read_session_state(sid)
        assert state["status"] == "succeeded"
        assert "nodes" not in state and "outputs" not in state
        assert len(executions) == 1

    with closing(GraphWorkflowService(path, registry=registry)) as reopened:
        assert reopened.get_session(sid)["status"] == "succeeded"
        assert reopened.get_run(sid, chain_id)["chain"]["status"] == "succeeded"
        reread = GraphApplication(reopened).query("output.history", {
            "session_id": sid,
            "workflow_definition_id": graph["workflow_definition_id"],
            "definition_revision": 1,
            "node_id": output["node_binding_id"],
            "port_id": "output",
        })
        assert [row["payload"] for row in reread["outputs"]] == [
            text_content("cross layer")]
        assert len(executions) == 1


def test_public_pause_and_resume_consume_execution_ports(service, monkeypatch):
    entered, release = Event(), Event()
    executions = []

    def gate(config, inputs, context):
        executions.append(context.node_run_id)
        entered.set()
        assert release.wait(10), "pause gate timed out"
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.cross-layer-gate", "1", "Pause gate", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), gate)
    source = node(service.registry, "tools.text", 1, text="paused")
    middle = node(service.registry, "test.cross-layer-gate", 2)
    output = node(service.registry, "tools.output", 3)
    graph = document([source, middle, output], [
        edge(source, middle, 1), edge(middle, output, 2)])
    graph["schema_version"] = 3
    graph["package_lock"] = list(service.registry.execution_package_lock)
    for entry in graph["nodes"]:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))
    view = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    sid = view["workflow_session_id"]
    started = service.start(
        sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
    chain_id = started["active_chain_run_id"]
    assert entered.wait(5)

    calls = {}
    direct = []
    original_put = GraphRecordStore.put

    def tracked_put(self, kind, value):
        caller = inspect.currentframe().f_back.f_code.co_name
        if caller == "_control_change_data":
            direct.append((kind, caller))
        return original_put(self, kind, value)

    monkeypatch.setattr(GraphRecordStore, "put", tracked_put)
    _track(monkeypatch, calls, ExecutionRecords, "put_session", "put_chain")
    running = service.get_session(sid)
    paused_cmd = service.control(
        sid, action="pause", expected_revision=running["revision"],
        idempotency_key=str(uuid4()))
    assert paused_cmd["active_chain_run_id"] == chain_id
    assert calls.get("ExecutionRecords.put_session", 0) > 0
    release.set()
    service.wait(chain_id)
    paused = service.get_session(sid)
    assert paused["status"] == "paused", paused["chains"]

    calls.clear()
    resumed = service.control(
        sid, action="resume", expected_revision=paused["revision"],
        idempotency_key=str(uuid4()))
    assert resumed["active_chain_run_id"] == chain_id
    service.wait(chain_id)
    final = service.get_session(sid)
    assert final["status"] == "succeeded", final["chains"]
    assert _output_payload(final, middle) == text_content("paused")
    assert len(executions) == 1
    assert calls.get("ExecutionRecords.put_chain", 0) > 0
    assert calls.get("ExecutionRecords.put_session", 0) > 0
    assert direct == []


def test_public_failed_round_settlement_uses_execution_ports(service, monkeypatch):
    def fail(config, inputs, context):
        raise ValueError("cross-layer failure probe")

    service.registry.register(NodeDefinition(
        "test.cross-layer-failure", "1", "Failure probe", "Test", {},
        {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), fail)
    source = node(service.registry, "tools.text", 1, text="failed")
    middle = node(service.registry, "test.cross-layer-failure", 2)
    output = node(service.registry, "tools.output", 3)
    graph = document([source, middle, output], [
        edge(source, middle, 1), edge(middle, output, 2)])
    graph["package_lock"] = list(service.registry.execution_package_lock)
    view = create(service, graph)
    sid = view["workflow_session_id"]

    calls = {}
    direct = []
    original_put = GraphRecordStore.put

    def tracked_put(self, kind, value):
        caller = inspect.currentframe().f_back.f_code.co_name
        if caller in ("fail", "finish", "_execute"):
            direct.append((kind, caller))
        return original_put(self, kind, value)

    monkeypatch.setattr(GraphRecordStore, "put", tracked_put)
    _track(monkeypatch, calls, ExecutionRecords, "put_chain", "put_node_run", "put_session")
    _track(monkeypatch, calls, ExecutionSettlement, "save_manifest")

    started = service.start(
        sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
    chain_id = started["active_chain_run_id"]
    service.wait(chain_id)
    final = service.get_session(sid)
    assert final["status"] == "failed", final["chains"]
    for name in ("ExecutionRecords.put_chain", "ExecutionRecords.put_node_run",
                 "ExecutionRecords.put_session",
                 "ExecutionSettlement.save_manifest"):
        assert calls.get(name, 0) > 0, name

    stored = service.get_run(sid, chain_id)
    assert stored["chain"]["status"] == "failed"
    assert stored["state_manifests"]["end"]["status"] == "failed"
    assert direct == []


def test_pause_then_saved_configuration_does_not_change_the_resumed_round(service):
    entered, release = Event(), Event()
    executions = []

    def gate(config, inputs, context):
        executions.append(context.node_run_id)
        entered.set()
        assert release.wait(10), "pause configuration gate timed out"
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.cross-layer-pause-gate", "1", "Pause gate", "Test", {},
        {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), gate)
    wait = node(service.registry, "tools.text", 1)
    middle = node(service.registry, "test.cross-layer-pause-gate", 2)
    source = node(service.registry, "tools.editable-input", 3, text="A")
    output = node(service.registry, "tools.output", 4)
    output["public_outputs"] = ["output"]
    graph = document([wait, middle, source, output], [
        edge(wait, middle, 1), edge(source, output, 2)])
    graph["schema_version"] = 3
    graph["package_lock"] = list(service.registry.execution_package_lock)
    graph["control_edges"] = [{
        "edge_id": str(uuid4()), "source_node_id": middle["node_binding_id"],
        "target_node_id": source["node_binding_id"],
    }]
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))
    view = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    sid = view["workflow_session_id"]
    started = service.start(
        sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
    chain_id = started["active_chain_run_id"]
    assert entered.wait(5)
    running = service.get_session(sid)
    service.control(
        sid, action="pause", expected_revision=running["revision"],
        idempotency_key=str(uuid4()))
    release.set()
    service.wait(chain_id)
    paused = service.get_session(sid)
    assert paused["status"] == "paused", paused["chains"]

    changed = deepcopy(saved["document"])
    changed_source = next(entry for entry in changed["nodes"]
                          if entry["node_binding_id"] == source["node_binding_id"])
    changed_source["config"]["text"] = "B"
    updated = service.update_definition(
        changed, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=saved["edit_sequence"], idempotency_key=str(uuid4()))
    assert updated["edit_sequence"] == 2
    assert updated["definition_revision"] == 1
    assert updated["changed_categories"] == ["adjustable"]

    resumed = service.control(
        sid, action="resume", expected_revision=paused["revision"],
        idempotency_key=str(uuid4()))
    assert resumed["active_chain_run_id"] == chain_id
    service.wait(chain_id)
    first = service.get_session(sid)
    assert first["status"] == "succeeded", first["chains"]
    assert _output_payload(first, source) == text_content("A")
    assert _output_payload(first, output) == text_content("A")
    assert len(executions) == 1

    second_start = service.start(
        sid, expected_revision=first["revision"], idempotency_key=str(uuid4()))
    service.wait(second_start["active_chain_run_id"])
    second = service.get_session(sid)
    assert second["status"] == "succeeded", second["chains"]
    assert _output_payload(second, source) == text_content("B")
    assert _output_payload(second, output) == text_content("B")


def test_independent_workflow_operations_start_uses_execution_ports(service, monkeypatch):
    graph = text_graph(service.registry)
    view = create(service, graph)
    calls = {}
    _track(monkeypatch, calls, ExecutionRecords, "put_chain", "put_node_run")
    _track(monkeypatch, calls, ExecutionSettlement, "save_manifest")

    operations = WorkflowOperations(service)
    started = operations.start(
        workflow_definition_id=graph["workflow_definition_id"],
        definition_revision=1,
        session_id=view["workflow_session_id"],
        expected_session_revision=view["revision"],
        idempotency_key="operations-start",
    )
    assert started["outcome"] == "succeeded", started
    service.wait(started["start_result"]["active_chain_run_id"])
    final = service.get_session(view["workflow_session_id"])
    assert final["status"] == "succeeded", final["chains"]
    for name in ("ExecutionRecords.put_chain", "ExecutionRecords.put_node_run",
                 "ExecutionSettlement.save_manifest"):
        assert calls.get(name, 0) > 0, name


def test_public_start_prepares_without_running_node_business(service, monkeypatch):
    executions = []

    def probe(config, inputs, context):
        executions.append(context.node_run_id)
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.cross-layer-prepared", "1", "Prepared probe", "Test", {},
        {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), probe)
    source = node(service.registry, "tools.text", 1, text="prepared")
    middle = node(service.registry, "test.cross-layer-prepared", 2)
    output = node(service.registry, "tools.output", 3)
    graph = document([source, middle, output], [
        edge(source, middle, 1), edge(middle, output, 2)])
    graph["package_lock"] = list(service.registry.execution_package_lock)
    view = create(service, graph)
    submitted = []
    monkeypatch.setattr(
        service._executor, "submit",
        lambda *args, **kwargs: submitted.append(args) or None)

    started = service.start(
        view["workflow_session_id"], expected_revision=view["revision"],
        idempotency_key=str(uuid4()))
    assert submitted
    assert executions == []
    prepared = service.get_session(view["workflow_session_id"])
    assert prepared["status"] == "prepared", prepared["chains"]
    assert prepared["active_chain_run_id"] == started["active_chain_run_id"]
