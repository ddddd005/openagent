"""Controlled workspace tools through the real Agent lifecycle, V1-V9.

The tests use a local package copy, an isolated SQLite store, a deterministic
offline model transport and a real JSON-lines child process.  They never start
the product service, call a real model or touch a default database.
"""

from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import time
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_application import GraphApplication
from phase1_agent.model_host_service import MODEL_SERVICE_REF
from phase1_agent.workflow_host import WorkflowHost

from test_generic_tool_bindings import replace_entry
from test_graph_service import edge, node
from test_local_package_integration import save_configuration
from test_models_service_integration import ModelDatabaseFixture
from test_variable_tool_bindings import VariableToolTransport, _events
from workflow_test_support import graph


PACKAGE = "example.workspace-demo"
LOADER = "example.workspace-demo-tools"
EXAMPLE = Path(__file__).resolve().parents[2] / "examples/workflow-packages/workspace-demo-1.0.0"
DEFAULT_PRESENTATION = {"role": "system", "placement": "before", "depth": None,
                        "order": 0, "enabled": True}
DEFAULT_PARAMETERS = {**DEFAULT_PRESENTATION, "placement": "after"}
MOVED = "city_gate -> market_square"


def export_text(location, history):
    return "current location: " + location + "\nmovement history: " + history


def composed_result(model_text):
    return model_text + "\n\n" + export_text("market_square", MOVED)


def move_rounds(model_text):
    """Three shared-instance calls followed by one valid final answer."""
    return [
        ("look", {}),
        ("move", {"destination": "market_square"}),
        ("status", {}),
        ("final_answer", {"answer": {"text": model_text}}),
    ]


def final_round(model_text):
    return ("final_answer", {"answer": {"text": model_text}})


class FailingTransport:
    """Offline transport that fails every model request."""

    def __init__(self, message="offline model transport failed"):
        self.calls, self.closes, self.message = [], 0, message

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                raise RuntimeError(fixture.message)

            def generate_prepared(self, prepared):
                wire = prepared["transport_arguments"]
                return self.generate(wire["messages"], wire.get("tools", []))

            def close(self):
                fixture.closes += 1

        return Transport()


class GatedTransport:
    """Offline transport that holds one chosen model request open for control."""

    def __init__(self, rounds, block_index):
        self.rounds, self.calls, self.closes = list(rounds), [], 0
        self.block_index = block_index
        self.entered, self.proceed = Event(), Event()

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                index = len(fixture.calls) - 1
                if index == fixture.block_index:
                    fixture.entered.set()
                    assert fixture.proceed.wait(30), "workspace test gate timed out"
                name, arguments = fixture.rounds[index]
                return ModelResponse("tool_calls", tool_calls=(
                    ModelToolCall("call." + str(index), name, json.dumps(arguments)),))

            def generate_prepared(self, prepared):
                wire = prepared["transport_arguments"]
                return self.generate(wire["messages"], wire.get("tools", []))

            def close(self):
                fixture.closes += 1

        return Transport()


@contextmanager
def workspace_case(tmp_path, transport):
    """Real Host, local package copy, graph with one workspace tool loader."""
    root = tmp_path / "packages/workflow"
    shutil.copytree(EXAMPLE, root / EXAMPLE.name)
    database = tmp_path / "workspace-demo.sqlite"
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        application = GraphApplication(host)
        assert service.registry.get(LOADER, "1") is None
        assert any(row.get("package_id") == PACKAGE
                   or row.get("manifest", {}).get("package_id") == PACKAGE
                   for row in service.platform_capabilities()["local_package_sources"]["packages"])
        application.command("packages.configure", {
            "enabled_packages": {**DEFAULT_PACKAGES, PACKAGE: "1.0.0"}})
        service._service_options[MODEL_SERVICE_REF]["transport_factory"] = transport.factory
        ModelDatabaseFixture.write(service, 1)
        document = graph(service)
        agent = next(row for row in document["nodes"] if row["component_id"] == "agents.execute")
        assembly = next(row for row in document["nodes"] if row["component_id"] == "context.assembly")
        state_path = tmp_path / "workspace-state.json"
        loader = node(service.registry, LOADER, 501, component_version="1",
                      agent_node_id=agent["node_binding_id"], state_path=str(state_path),
                      list_presentation=deepcopy(DEFAULT_PRESENTATION),
                      parameters_presentation=deepcopy(DEFAULT_PARAMETERS))
        document["nodes"].append(loader)
        document["edges"].append(edge(
            loader, assembly, 1501, source_port="materials", target_port="materials"))
        document.update(schema_version=3, edit_sequence=0, control_edges=[],
                        package_lock=deepcopy(list(service.registry.execution_package_lock)))
        for row in document["nodes"]:
            row["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        _, saved = save_configuration(application, document)
        revision = saved["result"]["definition_revision"]
        session = application.command("session.create", {
            "workflow_definition_id": document["workflow_definition_id"],
            "definition_revision": revision, "idempotency_key": str(uuid4())})["result"]
        yield SimpleNamespace(
            host=host, service=service, application=application, transport=transport,
            document=document, session=session, agent=agent, loader=loader, revision=revision,
            database=database, state_path=state_path)


def instrument(case, *, finish_error=None, close_error=None, open_error=None):
    """Count and optionally fail the exact adapter lifecycle stages."""
    entry = case.service.registry.get(LOADER, "1")
    adapter = entry.tool_binding
    counts = {"open": 0, "invoke": 0, "finish": 0, "close": 0, "abort": 0}
    handles = []

    def counted_open(binding, invocation):
        counts["open"] += 1
        if open_error is not None:
            raise open_error
        handle = adapter.open(binding, invocation)
        handles.append(handle)
        return handle

    def counted_invoke(handle, tool, arguments, invocation):
        counts["invoke"] += 1
        return adapter.invoke(handle, tool, arguments, invocation)

    def counted_finish(handle, invocation):
        counts["finish"] += 1
        if finish_error is not None:
            raise finish_error
        return adapter.finish(handle, invocation)

    def counted_close(handle):
        counts["close"] += 1
        if close_error is not None:
            raise close_error
        return adapter.close(handle)

    def counted_abort(handle):
        counts["abort"] += 1
        return adapter.abort(handle)

    replace_entry(case, LOADER, tool_binding=replace(
        adapter, open=counted_open, invoke=counted_invoke,
        finish=counted_finish, close=counted_close, abort=counted_abort))
    return counts, handles


def start(case):
    current = case.service.get_session(case.session["workflow_session_id"])
    return case.application.command("run.start", {
        "session_id": current["workflow_session_id"],
        "expected_revision": current["revision"],
        "idempotency_key": str(uuid4()), "inputs": {"text": "workspace question"},
    })["result"]["active_chain_run_id"]


def finish(case):
    chain_id = start(case)
    case.service.wait(chain_id)
    return chain_id, case.service.get_session(case.session["workflow_session_id"])


def retry_acceptance(case, view):
    result = case.service.control(view["workflow_session_id"], action="retry_acceptance",
                                  expected_revision=view["revision"], idempotency_key=str(uuid4()))
    case.service.wait(result["active_chain_run_id"])
    return case.service.get_session(view["workflow_session_id"])


def chain_of(session, chain_id):
    return next(row for row in session["chains"] if row["chain_run_id"] == chain_id)


def node_of(session, node_id):
    return next(row for row in session["nodes"] if row["node_binding_id"] == node_id)


def settled_contents(case, session, chain_id):
    return [event["payload"]["message"]["blocks"][0]["content"]
            for event in _events(case.service, session, chain_id)
            if event["kind"] == "tool_settled"]


def tool_definitions(transport):
    return {row["function"]["name"]: row["function"] for row in transport.calls[0]["tools"]}


def test_shared_instance_sees_earlier_workspace_commands(tmp_path):
    """V1: one open; look -> move -> status share the same running state."""
    transport = VariableToolTransport(move_rounds("v1 complete"))
    with workspace_case(tmp_path, transport) as case:
        counts, handles = instrument(case)
        chain_id, session = finish(case)
        assert session["status"] == "succeeded", session["chains"]
        assert counts == {"open": 1, "invoke": 3, "finish": 1, "close": 1, "abort": 0}
        contents = settled_contents(case, session, chain_id)
        assert contents[0]["location"] == "city_gate"
        assert contents[1]["location"] == "market_square" and contents[1]["from"] == "city_gate"
        assert contents[2]["location"] == "market_square"
        assert contents[2]["moves"] == [{"from": "city_gate", "to": "market_square"}]
        definitions = tool_definitions(transport)
        assert {"look", "status", "move"} <= set(definitions)
        assert "final_answer" in definitions
        for name in ("look", "status", "move"):
            assert definitions[name]["parameters"]["additionalProperties"] is False
        assert definitions["move"]["parameters"]["required"] == ["destination"]
        assert definitions["look"]["parameters"]["properties"] == {}
        loader_output = node_of(session, case.loader["node_binding_id"])["outputs"]["materials"]
        assert {item["purpose"] for item in loader_output["items"]} == {
            "tool-description", "tool-schema"}
        assert {item["source"]["agent_node_id"] for item in loader_output["items"]} == {
            case.agent["node_binding_id"]}
        settled = [event["payload"] for event in _events(case.service, session, chain_id)
                   if event["kind"] == "tool_settled"]
        # Three workspace calls plus the kernel control call, each with its own
        # exact dispatch identity and no repeated execution.
        assert len(settled) == 4
        assert len({event["tool_execution_id"] for event in settled}) == 4
        assert len({event["tool_call_id"] for event in settled}) == 4


def test_second_invocation_isolated_but_reloads_saved_state(tmp_path):
    """V2: two calls never share a process; the second reads saved data."""
    rounds = move_rounds("first complete") + [("status", {}), final_round("second complete")]
    transport = VariableToolTransport(rounds)
    with workspace_case(tmp_path, transport) as case:
        counts, handles = instrument(case)
        first_chain, first = finish(case)
        assert first["status"] == "succeeded", first["chains"]
        assert counts["open"] == 1
        assert handles[0]["process"].poll() is not None
        saved = json.loads(case.state_path.read_text(encoding="utf-8"))
        assert saved == {"location": "market_square",
                         "moves": [{"from": "city_gate", "to": "market_square"}]}
        assert node_of(first, case.agent["node_binding_id"])["outputs"]["result"]["text"] == (
            composed_result("first complete"))
        second_chain, second = finish(case)
        assert second["status"] == "succeeded", second["chains"]
        assert counts["open"] == 2 and counts["close"] == 2 and counts["finish"] == 2
        assert len(handles) == 2 and handles[0] is not handles[1]
        assert handles[0]["process"].pid != handles[1]["process"].pid
        assert handles[1]["process"].poll() is not None
        contents = settled_contents(case, second, second_chain)
        assert contents[0]["location"] == "market_square"
        assert contents[0]["moves"] == [{"from": "city_gate", "to": "market_square"}]
        assert node_of(second, case.agent["node_binding_id"])["outputs"]["result"]["text"] == (
            composed_result("second complete"))


def test_valid_final_stops_model_requests_and_finalizes_once(tmp_path):
    """V3: no model request after the final; finish happens exactly once."""
    transport = VariableToolTransport(move_rounds("v3 final"))
    with workspace_case(tmp_path, transport) as case:
        counts, handles = instrument(case)
        chain_id, session = finish(case)
        assert session["status"] == "succeeded", session["chains"]
        assert chain_of(session, chain_id)["status"] == "succeeded"
        assert counts == {"open": 1, "invoke": 3, "finish": 1, "close": 1, "abort": 0}
        assert len(transport.calls) == 4
        time.sleep(0.1)
        assert len(transport.calls) == 4
        assert transport.closes == 4


def test_composed_result_is_text_at_2_without_new_output_ports(tmp_path):
    """V4: result is TEXT@2 model text + blank line + export, no new port."""
    final_text = "v4 model summary"
    transport = VariableToolTransport(move_rounds(final_text))
    with workspace_case(tmp_path, transport) as case:
        instrument(case)
        chain_id, session = finish(case)
        assert session["status"] == "succeeded", session["chains"]
        expected = composed_result(final_text)
        agent_view = node_of(session, case.agent["node_binding_id"])
        assert set(agent_view["outputs"]) == {"result", "receipts"}
        assert agent_view["outputs"]["result"] == {
            "schema_version": 2, "kind": "workflow.text", "text": expected}
        sid = case.session["workflow_session_id"]
        reread = case.application.query("session.read", {"session_id": sid})
        assert node_of(reread, case.agent["node_binding_id"])["outputs"]["result"] == {
            "schema_version": 2, "kind": "workflow.text", "text": expected}
        run = case.application.query("run.read", {"session_id": sid, "chain_id": chain_id})
        outputs = {row["port_id"]: row["payload"] for row in run["outputs"]
                   if row["node_binding_id"] == case.agent["node_binding_id"]}
        assert set(outputs) == {"result", "receipts"}
        assert outputs["result"] == {
            "schema_version": 2, "kind": "workflow.text", "text": expected}
        messages = [event["payload"]["message"] for event in _events(case.service, session, chain_id)
                    if event["kind"] == "message_accepted"]
        assert "movement history" not in json.dumps(messages)


def test_original_final_and_agent_facts_are_not_replaced(tmp_path):
    """V5: the composed text only changes the node result; facts stay original."""
    final_text = "v5 original final answer"
    transport = VariableToolTransport(move_rounds(final_text))
    with workspace_case(tmp_path, transport) as case:
        instrument(case)
        chain_id, session = finish(case)
        assert session["status"] == "succeeded", session["chains"]
        sid = case.session["workflow_session_id"]
        run = case.service.get_run(sid, chain_id)
        events = _events(case.service, session, chain_id)
        agent_facts = [event for event in events if event["kind"] == "agent_result"]
        assert len(agent_facts) == 1
        agent_result = agent_facts[0]["payload"]
        assert agent_result["final"]["value"] == {"text": final_text}
        messages = [event["payload"]["message"] for event in events
                    if event["kind"] == "message_accepted"]
        serialized = json.dumps(messages)
        assert final_text in serialized
        assert "movement history" not in serialized
        receipts = node_of(session, case.agent["node_binding_id"])["outputs"]["receipts"]
        assert receipts["snapshot_id"] == agent_result["snapshot_id"]
        assert run == case.service.get_run(sid, chain_id)


def test_model_error_aborts_without_saving(tmp_path):
    """V6a: a model error never runs finish; the process is released unsaved."""
    transport = FailingTransport()
    with workspace_case(tmp_path, transport) as case:
        counts, handles = instrument(case)
        chain_id, session = finish(case)
        assert session["status"] == "failed", session["chains"]
        assert len(transport.calls) == 1
        assert counts == {"open": 1, "invoke": 0, "finish": 0, "close": 0, "abort": 1}
        assert handles[0]["process"].poll() is not None
        assert not case.state_path.exists()


def test_user_termination_aborts_paused_workspace_without_saving(tmp_path):
    """V6b: pause keeps the workspace open; the user close aborts without saving."""
    transport = GatedTransport([("move", {"destination": "harbor"}),
                                final_round("never delivered")], block_index=1)
    with workspace_case(tmp_path, transport) as case:
        counts, handles = instrument(case)
        chain_id = start(case)
        assert transport.entered.wait(30), "model gate was never reached"
        sid = case.session["workflow_session_id"]
        current = case.service.get_session(sid)
        case.service.control(sid, action="pause", expected_revision=current["revision"],
                             idempotency_key=str(uuid4()))
        transport.proceed.set()
        case.service.wait(chain_id)
        paused = case.service.get_session(sid)
        assert paused["status"] == "paused", paused["chains"]
        assert counts == {"open": 1, "invoke": 1, "finish": 0, "close": 0, "abort": 0}
        assert handles[0]["process"].poll() is None
        assert not case.state_path.exists()
        closed = case.service.control(paused["workflow_session_id"], action="close",
                                      expected_revision=paused["revision"],
                                      idempotency_key=str(uuid4()))
        assert closed["status"] == "closed", closed["chains"]
        assert counts == {"open": 1, "invoke": 1, "finish": 0, "close": 0, "abort": 1}
        assert handles[0]["process"].poll() is not None
        assert not case.state_path.exists()


def test_open_failure_reports_stage_before_any_model_call(tmp_path):
    """V7a: an open failure is explicit and never enters the model loop."""
    transport = VariableToolTransport([])
    with workspace_case(tmp_path, transport) as case:
        counts, handles = instrument(case, open_error=RuntimeError("worker start failed"))
        chain_id, session = finish(case)
        assert session["status"] == "failed", session["chains"]
        assert chain_of(session, chain_id)["diagnostic"]["code"] == "workspace_open_failed"
        assert transport.calls == []
        assert counts == {"open": 1, "invoke": 0, "finish": 0, "close": 0, "abort": 0}
        assert not case.state_path.exists()


def test_finish_failure_reports_stage_without_retry(tmp_path):
    """V7b: finish failure names workspace_finalize_failed and is not replayed."""
    transport = VariableToolTransport([
        ("move", {"destination": "market_square"}), final_round("v7 final")])
    with workspace_case(tmp_path, transport) as case:
        counts, handles = instrument(case, finish_error=RuntimeError("finish transport failed"))
        chain_id, session = finish(case)
        assert session["status"] == "failed", session["chains"]
        diagnostic = chain_of(session, chain_id)["diagnostic"]
        assert diagnostic["code"] == "workspace_finalize_failed"
        assert diagnostic["message"]
        assert counts == {"open": 1, "invoke": 1, "finish": 1, "close": 0, "abort": 1}
        assert len(transport.calls) == 2
        assert handles[0]["process"].poll() is not None
        assert not case.state_path.exists()


def test_acceptance_reentry_reuses_composed_output_without_repeating(tmp_path):
    """V8: result acceptance re-entry reuses finish/close output and model calls."""
    transport = VariableToolTransport(move_rounds("v8 reused final"))
    with workspace_case(tmp_path, transport) as case:
        counts, handles = instrument(case)
        original = case.service._accept_runtime_fact
        failures = []

        def fail(owner, fact, stream_id):
            if (fact.get("payload", {}).get("kind") == "agent_result"
                    and not failures):
                failures.append(True)
                raise OSError("terminal fact acknowledgment unavailable")
            return original(owner, fact, stream_id)

        case.service._accept_runtime_fact = fail
        chain_id, pending = finish(case)
        assert failures == [True]
        assert pending["status"] == "archive_failed", pending["chains"]
        assert pending["available_actions"] == ["retry_acceptance", "close"]
        assert counts == {"open": 1, "invoke": 3, "finish": 1, "close": 1, "abort": 0}
        calls_before = len(transport.calls)
        final = retry_acceptance(case, pending)
        assert final["status"] == "succeeded", final["chains"]
        assert node_of(final, case.agent["node_binding_id"])["outputs"]["result"]["text"] == (
            composed_result("v8 reused final"))
        assert counts == {"open": 1, "invoke": 3, "finish": 1, "close": 1, "abort": 0}
        assert len(transport.calls) == calls_before
        assert handles[0]["process"].poll() is not None
        assert not case.service._acceptance_candidates


def test_close_failure_keeps_saved_state_and_reports_stage(tmp_path):
    """V9: a close failure keeps the finish-saved facts and claims no rollback."""
    transport = VariableToolTransport([
        ("move", {"destination": "market_square"}), final_round("v9 final")])
    with workspace_case(tmp_path, transport) as case:
        counts, handles = instrument(case, close_error=RuntimeError("close handshake failed"))
        chain_id, session = finish(case)
        assert session["status"] == "failed", session["chains"]
        diagnostic = chain_of(session, chain_id)["diagnostic"]
        assert diagnostic["code"] == "workspace_close_failed"
        assert "rollback" not in json.dumps(diagnostic).lower()
        assert counts == {"open": 1, "invoke": 1, "finish": 1, "close": 1, "abort": 1}
        assert handles[0]["process"].poll() is not None
        saved = json.loads(case.state_path.read_text(encoding="utf-8"))
        assert saved == {"location": "market_square",
                         "moves": [{"from": "city_gate", "to": "market_square"}]}
        assert node_of(session, case.agent["node_binding_id"])["status"] == "failed"
        assert node_of(session, case.agent["node_binding_id"])["outputs"] == {}
