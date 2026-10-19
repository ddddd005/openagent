"""Real variable capability reached by both a normal node and Agent tools.

The fixture uses the installed built-in packages, a real isolated SQLite store,
the real variable executors and a controlled local model transport.  It does not
start a product service, call a real model or touch a default database.
"""

from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent import tool_package
from phase1_agent.variable_contract import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE

from test_graph_service import create, edge, node, uid
from test_models_service_integration import ModelDatabaseFixture
from test_model_package import source_config
from workflow_test_support import graph


KEY = "variable/score"
EVENT_ID = "tools.variable-register/event"


def control(source, target):
    return {"edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
            "target_node_id": target["node_binding_id"]}


def node_output(view, owner, port="output"):
    return next(item["outputs"][port] for item in view["nodes"]
                if item["node_binding_id"] == owner["node_binding_id"])


def build_graph(service, *, readers=None, writers=None, object_key=KEY, with_normal=True):
    document = graph(service)
    execute = next(item for item in document["nodes"] if item["component_id"] == "agents.execute")
    register = node(service.registry, "tools.variable-register", 401, component_version="2",
                    object_key=object_key, name="score", value_type="integer",
                    has_initial=True, initial_value=0)
    after = node(service.registry, "tools.variable-to-content", 403,
                 object_key=object_key, mode="json")
    after_output = node(service.registry, "tools.output", 404, mode="json")
    anchor = node(service.registry, "tool-bindings.variable-tools", 405, component_version="1",
                  agent_node_id=execute["node_binding_id"],
                  variables=[{"alias": "score", "object_key": object_key,
                              "operations": ["read", "set", "add", "subtract"]}])
    prompt = node(service.registry, "tool-bindings.tool-prompt", 406, component_version="1",
                 agent_node_id=execute["node_binding_id"], tool_name="score_read")
    summary = node(service.registry, "prompts.tool-summary", 407, component_version="2")
    assembly = node(service.registry, "prompts.assembly", 408, component_version="2")
    prompt_output = node(service.registry, "tools.output", 409, mode="prompt")
    extra_nodes = [register, after, after_output, anchor, prompt, summary, assembly, prompt_output]
    extra_edges = [
        edge(prompt, summary, 1402, source_port="tool-descriptions", target_port="tool-descriptions"),
        edge(prompt, summary, 1403, source_port="tool-schemas", target_port="tool-schemas"),
        edge(summary, assembly, 1404, source_port="tool-descriptions", target_port="input", order=0),
        edge(summary, assembly, 1405, source_port="tool-schemas", target_port="input", order=1),
        edge(assembly, prompt_output, 1406),
    ]
    controls = [control(execute, after)]
    reader_ids = [register["node_binding_id"], execute["node_binding_id"],
                  after["node_binding_id"]]
    writer_ids = [register["node_binding_id"], execute["node_binding_id"]]
    if with_normal:
        assign = node(service.registry, "tools.variable-assign", 402,
                      object_key=object_key, operation="set", value=5)
        extra_nodes.insert(1, assign)
        extra_edges.insert(0, edge(after, after_output, 1401))
        controls.insert(0, control(assign, execute))
        reader_ids.append(assign["node_binding_id"])
        writer_ids.append(assign["node_binding_id"])
    document["nodes"].extend(extra_nodes)
    document["edges"].extend(extra_edges)
    document["control_edges"] = controls
    document["object_bindings"].append(ObjectBinding(
        object_key, VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION, "shared",
        readers=tuple(readers or reader_ids), writers=tuple(writers or writer_ids),
        registrar_node_id=register["node_binding_id"]).to_dict())
    document["event_bindings"] = [{
        "event_id": EVENT_ID, "schema_version": 1, "display_name": "Register score",
        "audience": "management", "target_node_ids": [register["node_binding_id"]],
        "payload_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }]
    return document, {"register": register, "after": after,
                      "after_output": after_output, "anchor": anchor, "prompt": prompt,
                      "prompt_output": prompt_output, "execute": execute}


class VariableToolTransport:
    """Deterministic local model: call the requested variable tools in order."""

    def __init__(self, rounds):
        self.rounds, self.calls, self.closes = list(rounds), [], 0

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                index = len(fixture.calls) - 1
                name, arguments = fixture.rounds[index]
                return ModelResponse("tool_calls", tool_calls=(
                    ModelToolCall("call." + str(index), name, json.dumps(arguments)),))

            def generate_prepared(self, prepared):
                wire = prepared["transport_arguments"]
                return self.generate(wire["messages"], wire.get("tools", []))

            def close(self):
                fixture.closes += 1

        return Transport()


def _service(path, transport):
    return GraphWorkflowService(path, public_model_factory=transport.factory)


def _registered(service, document):
    session = create(service, document)
    started = service.submit_event(
        session["workflow_session_id"],
        workflow_definition_id=document["workflow_definition_id"],
        definition_revision=document["revision"], event_id=EVENT_ID, event_schema_version=1,
        payload={}, expected_revision=session["revision"], idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    return service.get_session(session["workflow_session_id"])


def _run(service, session, *, text="current question"):
    started = service.start(
        session["workflow_session_id"], expected_revision=session["revision"],
        idempotency_key=str(uuid4()), inputs={"text": text})
    service.wait(started["active_chain_run_id"])
    return started["active_chain_run_id"], service.get_session(session["workflow_session_id"])


def _events(service, session, chain_id):
    return [fact["payload"] for fact in service.get_run(
        session["workflow_session_id"], chain_id)["runtime_facts"]
        if fact.get("executor_ref") and isinstance(fact.get("payload"), dict)]


def test_variable_tool_bindings_run_read_write_and_reopen(tmp_path, monkeypatch):
    counts = {"assign": 0, "read": 0}
    original_assign, original_read = tool_package._assign, tool_package._variable_to_content

    def counted_assign(config, inputs, context):
        counts["assign"] += 1
        return original_assign(config, inputs, context)

    def counted_read(config, inputs, context):
        counts["read"] += 1
        return original_read(config, inputs, context)

    monkeypatch.setattr(tool_package, "_assign", counted_assign)
    monkeypatch.setattr(tool_package, "_variable_to_content", counted_read)
    transport = VariableToolTransport([
        ("score_read", {}), ("score_set", {"value": 10}), ("score_add", {"value": 2}),
        ("score_read", {}), ("final_answer", {"answer": {"text": "done"}}),
    ])
    database = tmp_path / "variable-tools.sqlite"
    with closing(_service(database, transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, parts = build_graph(service)
        session = _registered(service, document)
        assert session["objects"][KEY]["value"]["value"] == 0
        chain_id, view = _run(service, session)
        assert view["status"] == "succeeded", view["chains"]
        assert view["objects"][KEY]["value"]["value"] == 12
        assert view["objects"][KEY]["revision"] == 5
        assert node_output(view, parts["after"])["value"] == 12
        events = _events(service, view, chain_id)
        settled = [event["payload"] for event in events if event["kind"] == "tool_settled"]
        business = [fact for fact in settled
                    if isinstance(fact["message"]["blocks"][0]["content"], dict)
                    and "value" in fact["message"]["blocks"][0]["content"]]
        results = [fact["message"]["blocks"][0]["content"] for fact in business]
        assert [result["value"] for result in results] == [5, 10, 12, 12]
        assert len({fact["tool_execution_id"] for fact in business}) == len(business)
        agent = next(event["payload"] for event in events if event["kind"] == "agent_result")
        declarations = {tool["name"]: tool
                        for tool in agent["snapshot"]["tool_definitions"]}
        assert {"score_read", "score_set", "score_add", "score_subtract"} <= set(declarations)
        prompt = node_output(view, parts["prompt_output"])
        texts = [message["content"] for message in prompt["assembly"]["messages"]]
        assert declarations["score_read"]["description"] in texts
        assert canonical_bytes(declarations["score_read"]["parameters_schema"]).decode() in texts
        assert declarations["score_read"]["description"] in [
            item["text"] for item in prompt["items"]]
        assert counts == {"assign": 3, "read": 3}
        with closing(service._store()) as store:
            receipts = store._connection.execute(
                "SELECT operation_key FROM session_object_receipts WHERE session_id=?",
                (view["workflow_session_id"],)).fetchall()
            accepted_rows = store._connection.execute(
                "SELECT result_payload FROM idempotency "
                "WHERE operation='graph.tool.object-write'").fetchall()
        tool_keys = [row["operation_key"] for row in receipts
                     if row["operation_key"].startswith("tool:")]
        assert len(tool_keys) == 2
        accepted = [json.loads(row["result_payload"])[0]["graph_result"]
                    for row in accepted_rows]
        assert len(accepted) == 2
        executions = {fact["tool_execution_id"] for fact in business}
        assert all(any(identity in key for identity in executions) for key in tool_keys)
        assert len({row["binding_id"] for row in accepted}) == 1
        assert {row["tool_execution_id"] for row in accepted} <= executions
        assert {row["object_key"] for row in accepted} == {KEY}
    with closing(_service(database, transport)) as reopened:
        reopened_session = reopened.get_session(session["workflow_session_id"])
        assert reopened_session["objects"][KEY]["value"]["value"] == 12
        assert reopened_session["objects"][KEY]["revision"] == 5


def test_second_tool_failure_keeps_first_accepted_write(tmp_path):
    transport = VariableToolTransport([
        ("score_set", {"value": 10}), ("score_set", {"value": 2 ** 53}),
    ])
    with closing(_service(tmp_path / "variable-tools-failure.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = build_graph(service)
        session = _registered(service, document)
        _run(service, session)
        current = service.get_session(session["workflow_session_id"])
        assert current["status"] == "failed"
        assert current["objects"][KEY]["value"]["value"] == 10
        assert current["objects"][KEY]["revision"] == 4


@pytest.mark.parametrize("change", ["acl-reader", "acl-writer", "missing-object"])
def test_missing_binding_or_permission_blocks_start_before_any_execution(tmp_path, change):
    transport = VariableToolTransport([("final_answer", {"answer": {"text": "never"}})])
    database = tmp_path / ("blocked-" + change + ".sqlite")
    with closing(_service(database, transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = build_graph(service)
        execute = next(item for item in document["nodes"]
                       if item["component_id"] == "agents.execute")
        binding = next(item for item in document["object_bindings"]
                       if item["object_key"] == KEY)
        if change == "acl-reader":
            binding["readers"] = [item for item in binding["readers"]
                                  if item != execute["node_binding_id"]]
        elif change == "acl-writer":
            binding["writers"] = [item for item in binding["writers"]
                                  if item != execute["node_binding_id"]]
        else:
            anchor = next(item for item in document["nodes"]
                          if item["component_id"] == "tool-bindings.variable-tools")
            anchor["config"]["variables"][0]["object_key"] = "variable/missing"
        session = _registered(service, document) if change != "missing-object" else create(service, document)
        before = deepcopy(service.get_session(session["workflow_session_id"]))
        futures_before = set(service._futures)
        with pytest.raises(ContractValidationError) as caught:
            service.start(session["workflow_session_id"], expected_revision=session["revision"],
                          idempotency_key=str(uuid4()), inputs={"text": "blocked"})
        expected = "tool_binding_access_denied" if change.startswith("acl") else "tool_binding_variable_missing"
        assert caught.value.reason_code == expected
        after = service.get_session(session["workflow_session_id"])
        assert after == before
        assert set(service._futures) == futures_before
        assert transport.calls == []


def test_type_error_is_not_written(tmp_path):
    transport = VariableToolTransport([("score_set", {"value": "not-an-integer"})])
    with closing(_service(tmp_path / "variable-tools-type.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = build_graph(service)
        session = _registered(service, document)
        _run(service, session)
        current = service.get_session(session["workflow_session_id"])
        assert current["status"] == "failed"
        assert current["objects"][KEY]["value"]["value"] == 5
        assert current["objects"][KEY]["revision"] == 3


def test_missing_target_implementation_blocks_start(tmp_path):
    transport = VariableToolTransport([("final_answer", {"answer": {"text": "never"}})])
    with closing(_service(tmp_path / "missing-target.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, _ = build_graph(service, with_normal=False)
        session = _registered(service, document)
        service.registry._nodes.pop(("tools.variable-assign", "1"))
        with pytest.raises(ContractValidationError) as caught:
            service.start(session["workflow_session_id"], expected_revision=session["revision"],
                          idempotency_key=str(uuid4()), inputs={"text": "blocked"})
        assert caught.value.reason_code == "tool_binding_target_unavailable"
        assert transport.calls == []


def test_start_prepares_tool_targets_without_business_execution(tmp_path, monkeypatch):
    counts = {"assign": 0, "read": 0}
    original_assign, original_read = tool_package._assign, tool_package._variable_to_content
    monkeypatch.setattr(tool_package, "_assign",
                        lambda c, i, x: counts.__setitem__("assign", counts["assign"] + 1)
                        or original_assign(c, i, x))
    monkeypatch.setattr(tool_package, "_variable_to_content",
                        lambda c, i, x: counts.__setitem__("read", counts["read"] + 1)
                        or original_read(c, i, x))
    transport = VariableToolTransport([("final_answer", {"answer": {"text": "never"}})])
    with closing(_service(tmp_path / "prepared-only.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, parts = build_graph(service)
        session = _registered(service, document)
        submitted = []
        monkeypatch.setattr(service._executor, "submit",
                            lambda *args, **kwargs: submitted.append(args) or None)
        started = service.start(
            session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key=str(uuid4()), inputs={"text": "prepared only"})
        assert submitted
        assert counts == {"assign": 0, "read": 0}
        prepared = service.get_session(session["workflow_session_id"])
        assert prepared["status"] == "prepared"
        chain_id = started["active_chain_run_id"]
        frame = service._tool_frames[chain_id]
        assert frame.target_count == 4
        bindings = frame.bindings_for(parts["execute"]["node_binding_id"])
        assert [binding.alias for binding in bindings] == ["score"]
        assert {tool.target_component_id for tool in bindings[0].tools} == {
            "tools.variable-assign", "tools.variable-to-content"}
        assert all([dict(item) for item in tool.source]
                   == [{"package_id": "workflow.tools", "package_version": "1.0.0"}]
                   for tool in bindings[0].tools)
        assert [dict(item) for item in bindings[0].anchor_source] == [
            {"package_id": "workflow.tool-bindings", "package_version": "1.0.0"}]
        chain = service.get_run(session["workflow_session_id"], chain_id)["chain"]
        assert parts["anchor"]["node_binding_id"] not in chain["ordered_nodes"]
        assert all(run["node_binding_id"] not in {
            parts["anchor"]["node_binding_id"], } for run in service.get_run(
                session["workflow_session_id"], chain_id)["node_runs"])
        assert transport.calls == []


def test_anchor_configuration_saves_and_rereads_through_configuration_interface(tmp_path):
    transport = VariableToolTransport([("final_answer", {"answer": {"text": "unused"}})])
    with closing(_service(tmp_path / "registered-anchor.sqlite", transport)) as service:
        text = node(service.registry, "tools.text", 501, text="hello")
        output = node(service.registry, "tools.output", 502)
        execute = node(service.registry, "agents.execute", 503)
        register = node(service.registry, "tools.variable-register", 504, component_version="2",
                        object_key=KEY, name="score", value_type="integer",
                        has_initial=True, initial_value=0)
        anchor = node(service.registry, "tool-bindings.variable-tools", 505, component_version="1",
                      agent_node_id=execute["node_binding_id"],
                      variables=[{"alias": "score", "object_key": KEY,
                                  "operations": ["read", "set", "add", "subtract"]}])
        prompt = node(service.registry, "tool-bindings.tool-prompt", 506, component_version="1",
                      agent_node_id=execute["node_binding_id"], tool_name="score_read")
        nodes = [text, output, execute, register, anchor, prompt]
        for item in nodes:
            item["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        identity = str(uuid4())
        document = {
            "schema_version": 3, "workflow_definition_id": identity, "revision": 1,
            "name": "Anchor configuration", "nodes": nodes,
            "edges": [edge(text, output, 1501)], "object_bindings": [
                ObjectBinding(
                    KEY, VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION, "shared",
                    readers=(register["node_binding_id"],),
                    writers=(register["node_binding_id"],),
                    registrar_node_id=register["node_binding_id"]).to_dict(),
                ObjectBinding(
                    "context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
                    readers=(execute["node_binding_id"],),
                    writers=(execute["node_binding_id"],),
                    registrar_node_id=execute["node_binding_id"]).to_dict(),
            ],
        }
        saved = service.update_definition(
            document, workflow_definition_id=identity, expected_edit_sequence=0,
            idempotency_key=str(uuid4()))
        assert saved["outcome"] == "saved", saved
        fresh = service.get_definition(identity)
        fresh_anchor = next(item for item in fresh["nodes"]
                            if item["node_binding_id"] == anchor["node_binding_id"])
        assert fresh_anchor["config"] == anchor["config"]
        assert fresh_anchor["component_id"] == "tool-bindings.variable-tools"
        assert fresh_anchor["component_version"] == "1"
        fresh_prompt = next(item for item in fresh["nodes"]
                            if item["node_binding_id"] == prompt["node_binding_id"])
        assert fresh_prompt["config"] == prompt["config"]
