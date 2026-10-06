"""Latest ordinary A -> B graph with isolated contexts and completed-round forks."""

from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding

from test_agent_integration import run
from test_graph_service import copy_current, create, document, edge, node
from test_model_package import source_config
from test_models_service_integration import ModelDatabaseFixture


class SerialTransport:
    def __init__(self):
        self.calls = []
        self.rounds = {"A": 0, "B": 0}

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                label = next(message["content"][-1] for message in messages
                             if message["role"] == "system" and message["content"].startswith("ROLE "))
                fixture.rounds[label] += 1
                answer = f"{label}-answer-{fixture.rounds[label]}"
                fixture.calls.append({"agent": label, "messages": deepcopy(messages), "answer": answer})
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer", json.dumps({"answer": {"text": answer}})),))

            def close(self):
                pass

        return Transport()


def serial_graph(service):
    registry = service.registry
    entries, connections, controls, bindings = [], [], [], []

    def make(component, version="1", **config):
        value = node(registry, component, 3001 + len(entries))
        value["component_version"] = version
        value["config"] = {**deepcopy(registry.get(component, version).definition.default_config), **config}
        entries.append(value)
        return value

    def connect(source, target, source_port="output", target_port="input", order=0):
        connections.append(edge(source, target, 3101 + len(connections),
                                source_port=source_port, target_port=target_port, order=order))

    def precede(source, target):
        controls.append({"edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
                         "target_node_id": target["node_binding_id"]})

    current, model = make("tools.current-input", input_name="text"), make("models.source", **source_config())
    agents = {}
    for label in ("A", "B"):
        execute = make("agents.execute", "3")
        key = f"context/{label.lower()}"
        read = make("context.output", "2", object_key=key, agent_node_id=execute["node_binding_id"])
        assembly = make("context.assembly", "3")
        merge = make("context.merge", "2", object_key=key, agent_node_id=execute["node_binding_id"])
        prompt = make("prompts.item", text=f"ROLE {label}")
        connect(current, assembly, target_port="current_input")
        connect(read, assembly, target_port="view")
        connect(prompt, assembly, target_port="materials")
        connect(assembly, execute, target_port="prompt")
        connect(model, execute, target_port="model")
        connect(read, merge, target_port="view")
        connect(execute, merge, source_port="context", target_port="context")
        bindings.append(ObjectBinding(key, "workflow.effective-context", 3, "shared",
            readers=(read["node_binding_id"], merge["node_binding_id"]),
            writers=(merge["node_binding_id"],)).to_dict())
        agents[label] = {"execute": execute, "read": read, "assembly": assembly, "merge": merge}
    handoff = make("tools.text-to-prompt", presentation={
        "role": "user", "placement": "after", "depth": None, "order": 0, "enabled": True})
    connect(agents["A"]["execute"], handoff, source_port="result")
    connect(handoff, agents["B"]["assembly"], target_port="materials", order=1)
    precede(agents["B"]["execute"], agents["A"]["merge"])
    precede(agents["A"]["merge"], agents["B"]["merge"])
    read_user = make("frontend.state.output", object_key="frontend")
    user = make("frontend.state.append", object_key="frontend", role="user")
    read_assistant = make("frontend.state.output", object_key="frontend")
    assistant = make("frontend.state.append", object_key="frontend", role="assistant")
    display = make("frontend.presentation")
    display["public_outputs"] = ["display"]
    precede(agents["B"]["merge"], read_user)
    connect(read_user, user, source_port="view", target_port="view")
    connect(current, user, target_port="content")
    precede(user, read_assistant)
    connect(read_assistant, assistant, source_port="view", target_port="view")
    connect(agents["B"]["execute"], assistant, source_port="result", target_port="content")
    connect(assistant, display, source_port="view", target_port="view")
    bindings.append(ObjectBinding("frontend", "workflow.frontend-state", 1, "shared",
        readers=tuple(entry["node_binding_id"] for entry in (read_user, user, read_assistant, assistant)),
        writers=(user["node_binding_id"], assistant["node_binding_id"])).to_dict())
    doc = document(entries, connections)
    doc.update(schema_version=2, package_lock=list(registry.package_lock), object_bindings=bindings,
               control_edges=controls, execution_roots=[
                   agents["A"]["merge"]["node_binding_id"], agents["B"]["merge"]["node_binding_id"],
                   display["node_binding_id"]])
    return doc, agents, display


def result(view, entry, port="output"):
    return next(row["outputs"][port] for row in view["nodes"]
                if row["node_binding_id"] == entry["node_binding_id"])


def assert_round(view, agents, roots, *, local_rounds=None):
    local_rounds = len(roots) if local_rounds is None else local_rounds
    assert view["status"] == "succeeded", view["chains"]
    assert set(view["objects"]) == {"context/a", "context/b", "frontend"}
    for label in ("A", "B"):
        key = f"context/{label.lower()}"
        merged = result(view, agents[label]["merge"])
        assert merged["owner"]["agent_node_id"] == agents[label]["execute"]["node_binding_id"]
        assert [unit["unit"]["root"]["content"] for unit in merged["units"]] == roots
        assert len(view["objects"][key]["value"]["accepted_delta_ids"]) == len(roots)
        assert view["objects"][key]["revision"] == local_rounds + 1
        assert result(view, agents[label]["merge"], "commit")["receipt"]["revision_id"] == view["objects"][key]["revision_id"]
    frontend = view["objects"]["frontend"]
    assert len(frontend["value"]["entries"]) == len(roots) * 2
    assert [entry["role"] for entry in frontend["value"]["entries"]] == ["user", "assistant"] * len(roots)
    assert len({entry["entry_id"] for entry in frontend["value"]["entries"]}) == len(roots) * 2
    assert frontend["revision"] == local_rounds * 2 + 1


def configure_serial_packages(service, compat_enabled):
    if compat_enabled:
        service.configure_capability_packages({**DEFAULT_PACKAGES, "workflow.compat": "1.0.0"})
    assert_serial_packages(service, compat_enabled)


def assert_serial_packages(service, compat_enabled):
    packages = {item["package_id"] for item in service.registry.package_lock}
    assert ("workflow.compat" in packages) == compat_enabled
    if not compat_enabled:
        assert service.registry.get("workflow.agent", "2") is None
    assert service._native_runtime is None


@pytest.mark.parametrize("compat_enabled", [False, True], ids=["default-without-compat", "explicit-compat"])
def test_serial_two_rounds_distinct_contexts_exact_handoff_and_completed_checkpoint(
    tmp_path, monkeypatch, compat_enabled,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-serial-test")
    transport = SerialTransport()
    database = tmp_path / "serial.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        configure_serial_packages(service, compat_enabled)
        ModelDatabaseFixture.write(service, 1)
        doc, agents, display = serial_graph(service)
        initial = create(service, doc)
        assert transport.calls == []
        first = run(service, initial, "first original question")
        assert_round(first, agents, ["first original question"])
        candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
        second = run(service, first, "second original question")
        assert_round(second, agents, ["first original question", "second original question"])
        assert [call["agent"] for call in transport.calls] == ["A", "B", "A", "B"]
        for index in (0, 2):
            a_call, b_call = transport.calls[index:index + 2]
            assert b_call["messages"][-1] == {"kind": "text", "role": "user", "content": a_call["answer"]}
            assert b_call["messages"][-2] == {
                "kind": "text", "role": "user",
                "content": "first original question" if index == 0 else "second original question"}
        assert not any(message.get("content") == "A-answer-1" for message in transport.calls[3]["messages"])
        assert result(second, display, "display")["entries"] == second["objects"]["frontend"]["value"]["entries"]
        forked = service.fork_graph_candidate(
            second["workflow_session_id"], candidate_id=candidate["candidate_id"],
            expected_revision=second["revision"], expected_data_revision=second["data_revision"],
            expected_head_revision=second["head_revision"], idempotency_key=str(uuid4()))
        # Restoring a completed checkpoint must include every registered object's exact value.
        for key in first["objects"]:
            assert forked["objects"][key]["value"] == first["objects"][key]["value"]
        branch = run(service, forked, "branch question")
        assert_round(branch, agents, ["first original question", "branch question"], local_rounds=1)
        assert service.get_session(second["workflow_session_id"])["objects"] == second["objects"]
        assert_serial_packages(service, compat_enabled)
        sid, expected = second["workflow_session_id"], deepcopy(second["objects"])
    calls = len(transport.calls)
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        reopened = service.get_session(sid)
        assert reopened["objects"] == expected
        assert len(transport.calls) == calls
        continued = run(service, reopened, "reopened question")
        assert_round(continued, agents, ["first original question", "second original question", "reopened question"])
        assert len(transport.calls) == calls + 2
        assert_serial_packages(service, compat_enabled)


@pytest.mark.parametrize("compat_enabled", [False, True], ids=["default-without-compat", "explicit-compat"])
def test_serial_copy_remaps_agents_keeps_parent_future_outside_child(tmp_path, monkeypatch, compat_enabled):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-serial-copy")
    transport = SerialTransport()
    with closing(GraphWorkflowService(tmp_path / "serial-copy.sqlite", public_model_factory=transport.factory)) as service:
        configure_serial_packages(service, compat_enabled)
        ModelDatabaseFixture.write(service, 1)
        doc, agents, _ = serial_graph(service)
        first = run(service, create(service, doc), "shared first")
        copied = deepcopy(doc)
        copied["workflow_definition_id"] = str(uuid4())
        mapping = {entry["node_binding_id"]: str(uuid4()) for entry in copied["nodes"]}
        for entry in copied["nodes"]:
            entry["node_binding_id"] = mapping[entry["node_binding_id"]]
            if "agent_node_id" in entry["config"]:
                entry["config"]["agent_node_id"] = mapping[entry["config"]["agent_node_id"]]
        for connection in copied["edges"] + copied["control_edges"]:
            connection["edge_id"] = str(uuid4())
            for key in ("source_node_id", "target_node_id"):
                connection[key] = mapping[connection[key]]
        copied["execution_roots"] = [mapping[identity] for identity in copied["execution_roots"]]
        for binding in copied["object_bindings"]:
            for key in ("readers", "writers"):
                binding[key] = [mapping[identity] for identity in binding[key]]
        child = copy_current(service, first, copied, mappings=[
            {"source_node_id": source, "target_node_id": target, "action": "copy"}
            for source, target in mapping.items()])
        assert len(transport.calls) == 2
        child_agents = deepcopy(agents)
        for family in child_agents.values():
            for entry in family.values():
                entry["node_binding_id"] = mapping[entry["node_binding_id"]]
        parent = run(service, first, "parent future")
        child = run(service, child, "child future")
        assert_round(parent, agents, ["shared first", "parent future"])
        assert_round(child, child_agents, ["shared first", "child future"], local_rounds=1)
        for label in ("A", "B"):
            owner = result(child, child_agents[label]["merge"])["owner"]
            assert owner["workflow_session_id"] == child["workflow_session_id"]
            assert owner["agent_node_id"] == child_agents[label]["execute"]["node_binding_id"]
        assert_serial_packages(service, compat_enabled)
