"""Shared current Agent graph fixtures, independent of retired route tests."""

from copy import deepcopy
import json
from threading import Event
from uuid import uuid4

from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.host_sdk import ObjectBinding

from test_graph_service import document, edge, node
from test_model_package import source_config


class AgentTransportFixture:
    def __init__(self, *, gate=False, batch=False):
        self.calls, self.closes = [], 0
        self.entered, self.proceed = Event(), Event()
        self.gate, self.batch = gate, batch

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append(deepcopy(messages))
                fixture.entered.set()
                if fixture.gate:
                    assert fixture.proceed.wait(10), "Agent model gate timed out"
                if fixture.batch and len(fixture.calls) % 2 == 1:
                    return ModelResponse("tool_calls", tool_calls=(
                        ModelToolCall("inspect.one", "inspect_text", json.dumps({"text": "one"})),
                        ModelToolCall("inspect.two", "inspect_text", json.dumps({"text": "two"}))))
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer", json.dumps({"answer": {"text": "accepted answer"}})),))

            def close(self):
                fixture.closes += 1

        return Transport()


class SummaryTransport:
    def __init__(self, *, summary_text=None):
        self.calls, self.summary_calls, self.summary_text = [], 0, summary_text

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                if not tools:
                    fixture.summary_calls += 1
                    return ModelResponse("stop", content=fixture.summary_text or (
                        f"S{fixture.summary_calls}: ORION approved; preserve decisions and pending tasks."))
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer", json.dumps({"answer": {"text": "accepted ORION answer"}})),))

            def close(self):
                pass

        return Transport()


def _graph(service, numbers):
    read, current, model, assembly, execute, merge, display = [
        node(service.registry, component, number) for component, number in zip((
            "context.output", "tools.current-input", "models.source", "context.assembly",
            "agents.execute", "context.merge", "tools.output"), numbers)]
    for entry in (read, merge):
        entry["config"]["agent_node_id"] = execute["node_binding_id"]
    model["config"] = source_config()
    doc = document([read, current, model, assembly, execute, merge, display], [
        edge(read, assembly, 1301, target_port="view"),
        edge(current, assembly, 1302, target_port="current_input"),
        edge(model, execute, 1303, target_port="model"),
        edge(assembly, execute, 1304, target_port="prompt"),
        edge(read, merge, 1305, target_port="view"),
        edge(execute, merge, 1306, source_port="context", target_port="context"),
        edge(execute, display, 1307, source_port="result"),
    ])
    doc.update(execution_roots=[merge["node_binding_id"], display["node_binding_id"]],
               package_lock=list(service.registry.package_lock), object_bindings=[
                   ObjectBinding("context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
                                 readers=(read["node_binding_id"], merge["node_binding_id"]),
                                 writers=(merge["node_binding_id"],)).to_dict()])
    return doc


def base_graph(service):
    return _graph(service, range(1301, 1308))


def graph(service):
    return _graph(service, (301, 303, 304, 305, 306, 309, 310))


def run(service, view, text):
    started = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                            idempotency_key=str(uuid4()), inputs={"text": text})
    service.wait(started["active_chain_run_id"])
    return service.get_session(view["workflow_session_id"])


def output(view, component, port="output"):
    return next(node["outputs"][port] for node in view["nodes"] if node["label"] == component)
