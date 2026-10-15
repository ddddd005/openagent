"""Offline serial A/B acceptance of native compaction and prompt lifetimes."""

from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding

from workflow_test_support import run
from test_context_native_integration import node_output, rebind, versioned_node
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
    model = make("models.source", "5", **source_config())
    ModelDatabaseFixture.update_profile(service, parameters={"max_tokens": 128}, capacity={
        "context_window_tokens": 100_000, "output_reserve_tokens": 128,
        "summary_max_tokens": 128, "max_cold_input_tokens": 100_000,
    })
    once = make("prompts.item", "2", text="ONCE_SERIAL_ORIGINAL " * 1600,
                lifecycle="context_once", compaction="allowed")
    once["config"]["presentation"]["role"] = "user"
    agents = {}
    for label, threshold in (("A", 9000), ("B", 12000)):
        execute = make("agents.execute", "4")
        key = "context/" + label.lower()
        read = make("context.output", "4", object_key=key, agent_node_id=execute["node_binding_id"])
        assembly = make("context.assembly", "4")
        merge = make("context.merge", "4", object_key=key, agent_node_id=execute["node_binding_id"])
        fixed = make("prompts.item", "2", text="ROLE " + label)
        strategy = make("agents.compaction-policy", trigger_tokens=threshold, keep_depth=0,
                        summary_prompt="SUMMARY_" + label)
        for producer, port in ((current, "current_input"), (read, "view"), (fixed, "materials")):
            connect(producer, assembly, target_port=port)
        connect(once, assembly, target_port="materials", order=1)
        connect(model, execute, target_port="model")
        connect(assembly, execute, target_port="prompt")
        connect(strategy, execute, target_port="compaction_policy")
        connect(read, merge, target_port="view")
        connect(execute, merge, source_port="context", target_port="context")
        bindings.append(ObjectBinding(
            key, "workflow.effective-context", 4, "shared",
            readers=(read["node_binding_id"], merge["node_binding_id"]),
            writers=(merge["node_binding_id"],),
        ).to_dict())
        agents[label] = {
            "execute": execute, "read": read, "assembly": assembly, "merge": merge,
            "policy": strategy, "fixed": fixed,
        }
    handoff = make("prompts.source", "2", lifecycle="per_request", compaction="never")
    handoff["config"]["presentation"].update(role="user", placement="after")
    connect(agents["A"]["execute"], handoff, source_port="result")
    connect(handoff, agents["B"]["assembly"], target_port="materials", order=2)
    precede(agents["B"]["execute"], agents["A"]["merge"])
    precede(agents["A"]["merge"], agents["B"]["merge"])
    roots = [agents[label]["merge"]["node_binding_id"] for label in ("A", "B")]
    doc = document(nodes, connections)
    doc.update(schema_version=2, package_lock=list(service.registry.package_lock),
               object_bindings=bindings, control_edges=controls,
               execution_roots=roots)
    return doc, agents, once


def effective(view, agents, label):
    return node_output(view, agents[label]["merge"])


def once_sources(value):
    return {item["source"]["item_instance_id"] for item in value["layout"] if item["kind"] == "once"}


def new_once_sources(prompt):
    """Once sources injected from materials, excluding messages already in the context."""
    frozen = {message["message_id"] for message in prompt["context"]["messages"]}
    return {message["source"]["item_instance_id"] for message, item in
            zip(prompt["messages"], prompt["layout"])
            if item["kind"] == "once" and message["message_id"] not in frozen}


def business_calls(fixture, label, request):
    return [call for call in fixture.calls if call["agent"] == label
            and call["request"] == request and call["purpose"] == "business"]


def inspect_identity(messages, label, request):
    expected = label + " inspected " + request
    return next(call["id"] for message in messages if message.get("kind") == "assistant_calls"
                for call in message["calls"] if call["name"] == "inspect_text"
                and json.loads(call["raw_arguments"])["text"] == expected)


def assert_clean_persistent_state(view, agents):
    assert view["status"] == "succeeded", view["chains"]
    views = [effective(view, agents, label) for label in ("A", "B")]
    assert views[0]["owner"]["agent_node_id"] != views[1]["owner"]["agent_node_id"]
    assert views[0]["applied_delta_ids"] != views[1]["applied_delta_ids"]
    for label, value in zip(("A", "B"), views):
        assert "GENERATED_" + label + "_LONG" not in str(value["messages"])
        assert "ROLE " not in str(value["messages"])
        assert all(item["kind"] in ("history", "summary", "once") for item in value["layout"])
        assert value["messages"][-1]["role"] == "assistant"
        assert value["messages"][-1]["blocks"][0]["text"].startswith(label + "-answer-")
        assert ("CHECKPOINT_B" if label == "A" else "CHECKPOINT_A") not in str(value["messages"])
    assert not any(message["blocks"][0].get("text", "").startswith("A-answer-")
                   for message in views[1]["messages"])


def test_serial_native_compaction_preserves_order_tool_ids_once_state_edit_fork_and_reopen(tmp_path, monkeypatch):
    fixture = CompactingSerialTransport()
    database = tmp_path / "serial-native.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, agents, once = compacting_serial_graph(service)
        initial = create(service, doc)
        assert fixture.calls == []
        first = run(service, initial, "REQ first")
        assert_clean_persistent_state(first, agents)
        candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
        assert len(fixture.summaries) == 4
        first_tool_ids = {}
        for label in ("A", "B"):
            value = effective(first, agents, label)
            update = node_output(first, agents[label]["execute"], "context")
            assert value["generation"] == 2
            compactions = [index for index, operation in enumerate(update["operations"])
                           if operation["kind"] == "compact"]
            assert len(compactions) == 2 and compactions[0] == 0
            assert any(operation["kind"] == "append" for operation in update["operations"][compactions[1] + 1:])
            assert len(new_once_sources(node_output(first, agents[label]["assembly"]))) == 1
            calls = business_calls(fixture, label, "REQ first")
            last = calls[-1]
            first_tool_ids[label] = inspect_identity(last["messages"], label, "REQ first")
            summaries = [call for call in fixture.summaries if call["agent"] == label]
            assert summaries[1]["messages"][:len(calls[0]["messages"])] == calls[0]["messages"]
            assert summaries[1]["tools"] == calls[0]["tools"]
            assert first["objects"]["context/" + label.lower()]["revision"] == 2
        a_answer = business_calls(fixture, "A", "REQ first")[-1]["answer"]
        assert business_calls(fixture, "B", "REQ first")[0]["messages"][-1] == {
            "kind": "text", "role": "user", "content": a_answer}
        second = run(service, first, "REQ second")
        assert_clean_persistent_state(second, agents)
        assert len(fixture.summaries) == 4
        for label in ("A", "B"):
            second_prompt = node_output(second, agents[label]["assembly"])
            assert len(new_once_sources(second_prompt)) == 1
            second_calls = business_calls(fixture, label, "REQ second")
            assert inspect_identity(second_calls[0]["messages"], label, "REQ first") == first_tool_ids[label]
            assert second["objects"]["context/" + label.lower()]["revision"] == 3
        edited = deepcopy(doc)
        next(entry for entry in edited["nodes"] if entry["node_binding_id"] == once["node_binding_id"])[
            "config"]["text"] = "ONCE_SERIAL_EDITED " * 1600
        rebound, edited = rebind(service, second, edited)
        parent = run(service, rebound, "REQ edited")
        assert_clean_persistent_state(parent, agents)
        assert len(fixture.summaries) == 4
        for label in ("A", "B"):
            frozen = node_output(parent, agents[label]["assembly"])
            assert frozen["items"] != node_output(first, agents[label]["assembly"])["items"]
            # The previous round compacted the material away, so its source is absent again.
            assert len(new_once_sources(frozen)) == 1
            assert "ONCE_SERIAL_EDITED" in str(frozen["messages"])
        parent_objects = deepcopy(parent["objects"])
        forked = service.fork_graph_candidate(
            parent["workflow_session_id"], candidate_id=candidate["candidate_id"],
            expected_revision=parent["revision"], expected_data_revision=parent["data_revision"],
            expected_head_revision=parent["head_revision"], idempotency_key=str(uuid4()))
        for key in first["objects"]:
            assert forked["objects"][key]["value"] == first["objects"][key]["value"]
        branch = run(service, forked, "REQ branch")
        assert_clean_persistent_state(branch, agents)
        assert len(fixture.summaries) == 4
        for label in ("A", "B"):
            frozen = node_output(branch, agents[label]["assembly"])
            assert len(new_once_sources(frozen)) == 1
            assert "REQ second" not in str(frozen["messages"])
            assert "REQ edited" not in str(frozen["messages"])
            assert inspect_identity(business_calls(fixture, label, "REQ branch")[0]["messages"],
                                    label, "REQ first") == first_tool_ids[label]
        assert service.get_session(parent["workflow_session_id"])["objects"] == parent_objects
        branch_id, branch_objects = branch["workflow_session_id"], deepcopy(branch["objects"])
        calls_before_close = len(fixture.calls)
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as service:
        reopened = service.get_session(branch_id)
        assert reopened["objects"] == branch_objects
        assert len(fixture.calls) == calls_before_close
        continued = run(service, reopened, "REQ reopened")
        assert_clean_persistent_state(continued, agents)
        assert len(fixture.summaries) == 4
        for label in ("A", "B"):
            assert len(new_once_sources(node_output(continued, agents[label]["assembly"]))) == 1
            assert inspect_identity(business_calls(fixture, label, "REQ reopened")[0]["messages"],
                                    label, "REQ first") == first_tool_ids[label]


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
        candidate_view = node_output(failed, agents["A"]["execute"], "context")["next_view"]
        assert len(once_sources(candidate_view)) == 1
        assert [call["agent"] for call in fixture.summaries] == ["A", "A", "B"]
        assert not business_calls(fixture, "B", "REQ first")
        assert all(record["value"] == {"view_ref": None, "accepted_delta_ids": []}
                   for record in failed["objects"].values())
