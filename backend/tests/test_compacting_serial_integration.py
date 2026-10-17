"""Offline serial A/B acceptance of native compaction and prompt lifetimes."""

from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding

from workflow_test_support import run
from test_context_native_integration import node_output, versioned_node
from test_graph_service import create, document, edge
from test_model_package import source_config
from test_models_service_integration import ModelDatabaseFixture


class CompactingSerialTransport:
    def __init__(self, *, bad_summary_agent=None):
        self.calls, self.summaries = [], []
        self.completed = {"A": 0, "B": 0}
        self.long_generated, self.inspected = set(), set()
        self.bad_summary_agent = bad_summary_agent

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                label = next(message["content"][-1] for message in messages
                             if message["role"] == "system" and message["content"].startswith("ROLE "))
                request = next(message["content"] for message in reversed(messages)
                               if message["role"] == "user" and message.get("content", "").startswith("REQ "))
                captured = {"agent": label, "request": request, "messages": deepcopy(messages),
                            "tools": deepcopy(tools)}
                fixture.calls.append(captured)
                if messages[-1].get("content", "").startswith("SUMMARY_" + label):
                    captured["purpose"] = "compaction"
                    fixture.summaries.append(captured)
                    return ModelResponse("stop", "" if fixture.bad_summary_agent == label else
                                         f"CHECKPOINT_{label}: goal, constraints and pending task retained.")
                captured["purpose"] = "business"
                key = label, request
                if request == "REQ first" and key not in fixture.long_generated:
                    fixture.long_generated.add(key)
                    captured["stage"] = "long-text"
                    return ModelResponse("stop", content=("GENERATED_" + label + "_LONG ") * 1300)
                if key not in fixture.inspected:
                    fixture.inspected.add(key)
                    captured["stage"] = "inspect"
                    return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                        "inspect.provider", "inspect_text",
                        json.dumps({"text": label + " inspected " + request})),))
                fixture.completed[label] += 1
                captured["stage"] = "final"
                captured["answer"] = f"{label}-answer-{fixture.completed[label]}"
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer",
                    json.dumps({"answer": {"text": captured["answer"]}})),))

            def generate_prepared(self, prepared):
                request = prepared["transport_arguments"]
                return self.generate(request["messages"], request["tools"])

            def close(self):
                pass

        return Transport()


def compacting_serial_graph(service):
    nodes, connections, controls, bindings = [], [], [], []

    def make(component, version="1", **config):
        value = versioned_node(service, component, version, 5100 + len(nodes))
        value["config"].update(config)
        nodes.append(value)
        return value

    def connect(source, target, *, source_port="output", target_port="input", order=0):
        connections.append(edge(source, target, 5300 + len(connections),
                                source_port=source_port, target_port=target_port, order=order))

    def precede(source, target):
        controls.append({"edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
                         "target_node_id": target["node_binding_id"]})

    current = make("tools.current-input", input_name="text")
    model = make("models.source", "7", **source_config())
    ModelDatabaseFixture.update_profile(
        service, capacity={"context_window_tokens": 100_000, "input_limit_tokens": 100_000,
                           "max_output_tokens": 128},
        runtime={"context_length_tokens": 100_000, "compaction_threshold_bps": 1000,
                 "output_limit_tokens": 128})
    once = make("prompts.item", "3", text="ONCE_SERIAL_ORIGINAL " * 1600,
                lifecycle="context_once", compaction="allowed")
    once["config"]["presentation"]["role"] = "user"
    agents = {}
    for label in ("A", "B"):
        execute = make("agents.execute", "10")
        key = "context/" + label.lower()
        read = make("context.output", "5", object_key=key)
        assembly = make("context.assembly", "5")
        merge = make("context.merge", "6", object_key=key)
        output = make("tools.output", "1")
        fixed = make("prompts.item", "3", text="ROLE " + label)
        strategy = make("agents.compaction-policy", "2", keep_depth=0,
                        summary_prompt="SUMMARY_" + label)
        for producer, port in ((current, "current_input"), (read, "view"), (fixed, "materials")):
            connect(producer, assembly, target_port=port)
        connect(once, assembly, target_port="materials", order=1)
        connect(model, execute, target_port="model")
        connect(assembly, execute, target_port="prompt")
        connect(strategy, execute, target_port="compaction_policy")
        connect(read, merge, target_port="view")
        connect(execute, merge, source_port="receipts", target_port="receipts")
        connect(execute, output, source_port="result")
        bindings.append(ObjectBinding(
            key, "workflow.effective-context", 4, "shared",
            readers=(read["node_binding_id"], merge["node_binding_id"]),
            writers=(merge["node_binding_id"],),
            registrar_node_id=execute["node_binding_id"],
        ).to_dict())
        agents[label] = {
            "execute": execute, "read": read, "assembly": assembly, "merge": merge,
            "policy": strategy, "fixed": fixed, "output": output,
        }
    handoff = make("prompts.source", "2", lifecycle="per_request", compaction="never")
    handoff["config"]["presentation"].update(role="user", placement="after")
    connect(agents["A"]["execute"], handoff, source_port="result")
    connect(handoff, agents["B"]["assembly"], target_port="materials", order=2)
    precede(agents["B"]["execute"], agents["A"]["merge"])
    precede(agents["A"]["merge"], agents["B"]["merge"])
    doc = document(nodes, connections)
    doc.update(schema_version=2, package_lock=list(service.registry.package_lock),
               object_bindings=bindings, control_edges=controls)
    return doc, agents, once


def new_once_sources(prompt):
    """Once sources injected from materials, excluding messages already in the context."""
    frozen = {message["message_id"] for message in prompt["context"]["messages"]}
    return {message["source"]["item_instance_id"] for message, item in
            zip(prompt["messages"], prompt["layout"])
            if item["kind"] == "once" and message["message_id"] not in frozen}


def business_calls(fixture, label, request):
    return [call for call in fixture.calls if call["agent"] == label
            and call["request"] == request and call["purpose"] == "business"]


def test_serial_bad_second_agent_summary_does_not_commit_first_agent_or_consume_once(tmp_path, monkeypatch):
    fixture = CompactingSerialTransport(bad_summary_agent="B")
    with closing(GraphWorkflowService(tmp_path / "serial-failure.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, agents, _ = compacting_serial_graph(service)
        initial = create(service, doc)
        before = deepcopy(initial["objects"])
        failed = run(service, initial, "REQ first")
        assert failed["status"] == "failed", failed["chains"]
        assert failed["objects"] == before
        first_prompt = node_output(failed, agents["A"]["assembly"])
        assert len(new_once_sources(first_prompt)) == 1
        first_receipts = node_output(failed, agents["A"]["execute"], "receipts")
        assert first_receipts["schema_version"] == 2
        assert [call["agent"] for call in fixture.summaries] == ["A", "A", "B"]
        assert not business_calls(fixture, "B", "REQ first")
        empty_context = {"messages": [], "layout": [], "accepted_delta_ids": [], "generation": 0}
        assert all(record["value"] == empty_context for record in failed["objects"].values())
