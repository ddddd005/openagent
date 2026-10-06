"""Native Agent evidence and controls within the ordinary serial graph host."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.model_configuration import default_provider
from phase1_agent.model_configuration_store import ModelConfigurationStore
from phase1_agent.storage import SqliteStore
from phase1_agent.contract_graph import validate_bundle
from test_graph_service import node, edge, document, create, run, copy_current
from test_graph_agent_runtime import final, response


class ScriptModel:
    def __init__(self, script, requests, hook=None):
        self.script, self.requests, self.hook = script, requests, hook

    def generate(self, messages, tools):
        self.requests.append(deepcopy(messages))
        if self.hook:
            self.hook()
        return self.script.pop(0)

    def close(self):
        pass


def graph(service, *, budget=8, context=False):
    registry = service.registry
    text = node(registry, "text", 1, text="first input")
    item = node(registry, "prompt-item", 2, text="Use final_answer")
    assembly = node(registry, "prompt-assembly", 3)
    agent = node(registry, "agent", 4)
    agent.update(component_version="2", config={"max_model_requests": budget, "max_model_attempts": budget * 4})
    model = node(registry, "model-provider", 5)
    model.update(component_version="2", config={"provider_id": default_provider()["provider_id"],
                                               "parameters": {"model": "deepseek-flash", "max_tokens": 2048}})
    output = node(registry, "output", 6)
    nodes = [text, item, assembly, agent, model, output]
    edges = [edge(text, assembly, 1, target_port="current_input"), edge(item, assembly, 2),
             edge(assembly, agent, 3, target_port="prompt"), edge(model, agent, 4, source_port="model", target_port="model"),
             edge(agent, output, 5)]
    if context:
        history = node(registry, "context", 7, source_node_id=agent["node_binding_id"])
        nodes.append(history)
        edges.append(edge(history, assembly, 6, order=1))
    return document(nodes, edges)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-credential")
    scripts, requests, hooks = [], [], []
    service = GraphWorkflowService(tmp_path / "graph-agent.sqlite", model_factory=lambda binding: ScriptModel(
        scripts.pop(0), requests, hooks.pop(0) if hooks else None))
    with closing(service._store()) as store:
        ModelConfigurationStore(store).write("provider", default_provider(), expected_revision=0, idempotency_key="provider")
    yield service, scripts, requests, hooks
    service.close()


def test_native_graph_repeated_runs_closed_context_copy_and_restore(harness):
    service, scripts, requests, _ = harness
    doc = graph(service, context=True)
    scripts.extend([[response("inspect_text", {"text": "once"}, "call1"), final("first")], [final("second")], [final("copy")]])
    session = create(service, doc)
    first = run(service, session)
    assert first["status"] == "succeeded"
    second = run(service, first)
    assert second["status"] == "succeeded"
    assert len(requests) == 3
    assert any(message["role"] == "tool" for message in requests[-1])
    assert first["private_states"] != second["private_states"]
    copied_doc = deepcopy(doc)
    copied_doc["workflow_definition_id"] = str(uuid4())
    copied = copy_current(service, second, copied_doc)
    assert copied["private_states"] == second["private_states"]
    copied = run(service, copied)
    assert copied["status"] == "succeeded"
    assert service.get_session(second["workflow_session_id"])["private_states"] == second["private_states"]
    with closing(service._store()) as store:
        validate_bundle(store.read_bundle(include_graph=True))
    history = service.get_run(copied["workflow_session_id"], copied["chains"][-1]["chain_run_id"])
    evidence = next(value["agent"] for value in history["node_runs"] if value.get("agent"))
    assert evidence["accepted"]["turn"]["final"]["value"] == {"text": "copy"}
    assert evidence["snapshot"]["config"]["payload"]["graph_preparation"]["context_basis"]


def test_native_pause_and_resume_keeps_same_run_and_snapshot(harness):
    service, scripts, requests, hooks = harness
    entered, release = Event(), Event()
    def block():
        entered.set()
        assert release.wait(10)
    scripts.extend([[response("inspect_text", {"text": "once"}, "call1")], [final()]])
    hooks.append(block)
    session = create(service, graph(service))
    started = service.start(session["workflow_session_id"], expected_revision=session["revision"], idempotency_key="start")
    assert entered.wait(10)
    view = service.get_session(session["workflow_session_id"])
    service.control(view["workflow_session_id"], action="pause", expected_revision=view["revision"], idempotency_key="pause")
    release.set()
    service.wait(started["active_chain_run_id"])
    paused = service.get_session(view["workflow_session_id"])
    assert paused["status"] == "paused"
    before = service.get_run(view["workflow_session_id"], started["active_chain_run_id"])
    service.control(view["workflow_session_id"], action="resume", expected_revision=paused["revision"], idempotency_key="resume")
    service.wait(started["active_chain_run_id"])
    after = service.get_run(view["workflow_session_id"], started["active_chain_run_id"])
    old_agent = next(run for run in before["node_runs"] if run.get("agent"))
    new_agent = next(run for run in after["node_runs"] if run.get("agent"))
    assert old_agent["run_id"] == new_agent["run_id"]
    assert old_agent["agent"]["snapshot"] == new_agent["agent"]["snapshot"]
    assert new_agent["status"] == "succeeded"
    assert len(requests) == 2


def test_native_budget_extension_is_idempotent_and_resume_preserves_frozen_config(harness):
    service, scripts, requests, _ = harness
    scripts.extend([[response("inspect_text", {"text": "once"}, "call1")], [final()]])
    session = run(service, create(service, graph(service, budget=1)))
    assert session["status"] == "budget_exhausted"
    request = dict(action="extend_budget", expected_revision=session["revision"], idempotency_key="budget",
                   add_model_requests=1, add_model_attempts=1)
    extended = service.control(session["workflow_session_id"], **request)
    assert service.control(session["workflow_session_id"], **request) == extended
    service.control(session["workflow_session_id"], action="resume", expected_revision=extended["revision"], idempotency_key="resume")
    service.wait(session["active_chain_run_id"])
    completed = service.get_session(session["workflow_session_id"])
    assert completed["status"] == "succeeded"
    assert len(requests) == 2
    assert service.control(session["workflow_session_id"], **request) == extended


def test_archive_retry_does_not_dispatch_model_again(harness, monkeypatch):
    service, scripts, requests, _ = harness
    scripts.append([final()])
    original = service._save_agent_evidence
    def fail(sid, chain, run_id, kind, payload):
        if kind == "archive":
            raise OSError("archive fixture")
        return original(sid, chain, run_id, kind, payload)
    monkeypatch.setattr(service, "_save_agent_evidence", fail)
    paused = run(service, create(service, graph(service)))
    assert paused["status"] == "archive_failed"
    monkeypatch.setattr(service, "_save_agent_evidence", original)
    service.control(paused["workflow_session_id"], action="retry_archive", expected_revision=paused["revision"], idempotency_key="retry")
    service.wait(paused["active_chain_run_id"])
    assert service.get_session(paused["workflow_session_id"])["status"] == "succeeded"
    assert len(requests) == 1


def test_zero_agent_and_unreachable_native_nodes_do_not_construct_runtime(harness):
    service, scripts, requests, _ = harness
    doc = graph(service)
    doc["edges"] = [edge(doc["nodes"][0], doc["nodes"][-1], 10)]
    doc["nodes"][4]["config"]["provider_id"] = None
    completed = run(service, create(service, doc))
    assert completed["status"] == "succeeded"
    assert service._native_runtime is None
    assert requests == []


def test_model_preflight_rejects_before_upstream_variable_effect(harness):
    service, _, requests, _ = harness
    doc = graph(service)
    writer = node(service.registry, "variable-register", 8, name="count", valueType="integer", hasInitialValue=True, initialValue=8)
    doc["nodes"].append(writer)
    doc["edges"][0]["target_node_id"] = writer["node_binding_id"]
    doc["edges"][0]["target_port_id"] = "text"
    doc["edges"].append(edge(writer, doc["nodes"][2], 9, source_port="text", target_port="current_input"))
    doc["nodes"][4]["config"]["provider_id"] = None
    session = create(service, doc)
    with pytest.raises(ContractValidationError) as missing:
        service.start(session["workflow_session_id"], expected_revision=session["revision"], idempotency_key="missing-model")
    assert missing.value.reason_code == "provider_missing"
    unchanged = service.get_session(session["workflow_session_id"])
    assert unchanged["revision"] == session["revision"]
    assert unchanged["data"]["values"] == {} and unchanged["chains"] == []
    assert requests == [] and service._native_runtime is None


def test_output_commit_retry_reuses_accepted_agent_without_model_replay(harness, monkeypatch):
    service, scripts, requests, _ = harness
    scripts.append([final()])
    original = service._node_event
    def fail(sid, chain, event):
        if event["event"] == "succeeded" and service._native_runtime and service._native_runtime.has_accepted(event["node_run_id"]):
            raise OSError("output commit fixture")
        return original(sid, chain, event)
    monkeypatch.setattr(service, "_node_event", fail)
    session = run(service, create(service, graph(service)))
    assert session["status"] == "archive_failed"
    assert session["available_actions"] == ["retry_acceptance", "close"]
    before = service.get_run(session["workflow_session_id"], session["active_chain_run_id"])
    monkeypatch.setattr(service, "_node_event", original)
    service.control(session["workflow_session_id"], action="retry_acceptance", expected_revision=session["revision"], idempotency_key="retry")
    service.wait(session["active_chain_run_id"])
    assert service.get_session(session["workflow_session_id"])["status"] == "succeeded"
    after = service.get_run(session["workflow_session_id"], session["active_chain_run_id"])
    assert [node["run_id"] for node in before["node_runs"]] == [node["run_id"] for node in after["node_runs"]]
    assert [node["agent"] for node in before["node_runs"] if node.get("agent")] == [
        node["agent"] for node in after["node_runs"] if node.get("agent")]
    assert len(requests) == 1


def test_old_graph_run_v2_remains_readable():
    from phase1_agent.graph_records import graph_record, validate_graph_record
    value = graph_record("node_run", profile="node", run_id=str(uuid4()), workflow_session_id=str(uuid4()),
        node_binding_id=str(uuid4()), chain_run_id=str(uuid4()), status="prepared", revision=1,
        input_refs={}, input_values={}, output_refs={}, reads=[], effects=[], config={}, diagnostic=None)
    value.pop("agent")
    value.pop("input_storage")
    value.pop("control_refs")
    value["schema_version"] = 2
    assert validate_graph_record("node_run", value) == value
