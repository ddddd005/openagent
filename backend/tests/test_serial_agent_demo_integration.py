"""Offline A/B handoff across isolated current context objects."""

from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding

from workflow_test_support import run
from test_graph_service import create, document, edge, node
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

            def generate_prepared(self, prepared):
                request = prepared["transport_arguments"]
                return self.generate(request["messages"], request["tools"])

            def close(self):
                pass

        return Transport()


def serial_graph(service):
    registry = service.registry
    entries, connections, controls, bindings = [], [], [], []

    def make(component, version=None, **config):
        value = node(registry, component, 3001 + len(entries))
        version = version or value["component_version"]
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
        execute = make("agents.execute", "10")
        key = f"context/{label.lower()}"
        read = make("context.output", "5", object_key=key)
        assembly = make("context.assembly", "5")
        merge = make("context.merge", "6", object_key=key)
        output = make("tools.output", "1")
        prompt = make("prompts.item", text=f"ROLE {label}")
        connect(current, assembly, target_port="current_input")
        connect(read, assembly, target_port="view")
        connect(prompt, assembly, target_port="materials")
        connect(assembly, execute, target_port="prompt")
        connect(model, execute, target_port="model")
        connect(read, merge, target_port="view")
        connect(execute, merge, source_port="receipts", target_port="receipts")
        connect(execute, output, source_port="result")
        bindings.append(ObjectBinding(key, "workflow.effective-context", 4, "shared",
            readers=(read["node_binding_id"], merge["node_binding_id"]),
            writers=(merge["node_binding_id"],), registrar_node_id=execute["node_binding_id"]).to_dict())
        agents[label] = {
            "execute": execute, "read": read, "assembly": assembly, "merge": merge, "output": output,
        }
    handoff = make("prompts.source", presentation={
        "role": "user", "placement": "after", "depth": None, "order": 0, "enabled": True})
    connect(agents["A"]["execute"], handoff, source_port="result")
    connect(handoff, agents["B"]["assembly"], target_port="materials", order=1)
    precede(agents["B"]["execute"], agents["A"]["merge"])
    precede(agents["A"]["merge"], agents["B"]["merge"])
    doc = document(entries, connections)
    doc.update(schema_version=2, package_lock=list(registry.execution_package_lock), object_bindings=bindings,
               control_edges=controls)
    return doc, agents


def result(view, entry, port="output"):
    return next(row["outputs"][port] for row in view["nodes"]
                if row["node_binding_id"] == entry["node_binding_id"])


def assert_round(view, agents, roots, *, local_rounds=None):
    local_rounds = len(roots) if local_rounds is None else local_rounds
    assert view["status"] == "succeeded", view["chains"]
    assert set(view["objects"]) == {"context/a", "context/b"}
    for label in ("A", "B"):
        key = f"context/{label.lower()}"
        merged = result(view, agents[label]["merge"])
        assert merged["owner"] == {
            "workflow_session_id": view["workflow_session_id"], "object_key": key}
        assert [message["blocks"][0]["text"] for message in merged["messages"]
                if message["role"] == "user"] == roots
        assert len(view["objects"][key]["value"]["accepted_delta_ids"]) == len(roots)
        assert view["objects"][key]["revision"] == local_rounds + 1
        assert result(view, agents[label]["merge"], "commit")["receipt"]["revision_id"] == view["objects"][key]["revision_id"]
def assert_serial_packages(service):
    packages = {item["package_id"] for item in service.registry.package_lock}
    assert set(DEFAULT_PACKAGES) <= packages and "workflow.compat" not in packages
    assert service.registry.get("workflow.agent", "2") is None
    assert not hasattr(service, "_native_runtime")


def test_serial_current_contexts_keep_exact_agent_handoff(tmp_path):
    transport = SerialTransport()
    with closing(GraphWorkflowService(tmp_path / "serial.sqlite",
                                     public_model_factory=transport.factory)) as service:
        assert_serial_packages(service)
        ModelDatabaseFixture.write(service, 1)
        doc, agents = serial_graph(service)
        initial = create(service, doc)
        assert transport.calls == []
        first = run(service, initial, "first original question")
        assert_round(first, agents, ["first original question"])
        assert [call["agent"] for call in transport.calls] == ["A", "B"]
        a_call, b_call = transport.calls
        assert b_call["messages"][-1] == {
            "role": "user", "content": a_call["answer"]}
        assert b_call["messages"][-2] == {
            "role": "user", "content": "first original question"}
        assert_serial_packages(service)
