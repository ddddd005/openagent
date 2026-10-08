"""Real graph and SQLite acceptance using synthetic Gemini HTTP fixtures only."""

from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest

from phase1_agent.gemini_adapter import GeminiAdapter
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService

from test_agent_integration import graph as legacy_graph, output, run
from test_bound_context_integration import graph as bound_graph
from test_context_native_integration import native_graph
from test_context_summary_integration import base_graph as summary_graph
from test_graph_service import create
from test_model_package import source_config
from test_tavern_chat_integration import add_chat, public_read


class GeminiGraphFixture:
    def __init__(self):
        self.wires, self.original_contents = [], []

    def factory(self, *, provider, parameters, api_key):
        step = [0]

        def respond(request):
            self.wires.append(json.loads(request.content))
            current = step[0]
            step[0] += 1
            calls = ([("inspect_text", {"text": "one"}), ("inspect_text", {"text": "two"})]
                     if current == 0 else [("inspect_text", {"text": "three"})]
                     if current == 1 else [("final_answer", {"answer": {"text": "Gemini accepted answer"}})])
            parts = [{"text": f"Visible summary {current}", "thought": True}]
            for index, (name, args) in enumerate(calls):
                part = {"functionCall": {"name": name, "args": args, "id": f"native-{current}-{index}"}}
                if index == 0:
                    part["thoughtSignature"] = f"synthetic-fixture-only-{current}"
                parts.append(part)
            content = {"role": "model", "parts": parts}
            self.original_contents.append(deepcopy(content))
            return httpx.Response(200, json={
                "candidates": [{"content": content, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 32, "candidatesTokenCount": 10,
                                  "thoughtsTokenCount": 8, "totalTokenCount": 50},
                "modelVersion": parameters["model"], "responseId": str(uuid4()),
            })

        return GeminiAdapter(api_key, parameters["model"], base_url=provider["base_url"],
                             thinking=parameters["thinking"], max_tokens=parameters.get("max_tokens"),
                             temperature=parameters.get("temperature"),
                             http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    @staticmethod
    def write_provider(service):
        record = {**source_config()["reference"], "data_schema_version": 2, "update_sequence": 1,
                  "value": {"name": "Offline Gemini", "protocol": "gemini",
                            "base_url": "https://generativelanguage.googleapis.com/v1beta",
                            "credential_ref": "env:GEMINI_API_KEY", "enabled": True}}
        service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))


def gemini_graph(service, route):
    if route == "native":
        doc, _ = native_graph(service, policy=False, once=False)
    else:
        doc = {"legacy": legacy_graph, "bound": bound_graph, "summary": summary_graph}[route](service)
    source = next(node for node in doc["nodes"] if node["component_id"] == "models.source")
    source["component_version"] = "4" if route == "native" else "3"
    source["config"]["parameters"].update(model="gemini-3-flash-preview",
        thinking={"mode": "level", "level": "low", "include_summary": True})
    agent = next(node for node in doc["nodes"] if node["component_id"] == "agents.execute")
    agent["component_version"] = {"legacy": "5", "bound": "6", "summary": "7", "native": "8"}[route]
    for node in doc["nodes"]:
        if node["component_id"] == "agents.delta":
            node["component_version"] = "2"
    current = next(node for node in doc["nodes"] if node["component_id"] == "tools.current-input")
    chat = add_chat(service, doc, current, agent)
    return doc, chat


def read_public_thinking(service, view):
    application = GraphApplication(service).for_consumer()
    directory = application.query("registration.list", {"session_id": view["workflow_session_id"], "limit": 1000})
    bindings = [row for row in directory["items"] if row["kind"] == "information_binding"
                and row["declaration"]["channel_id"] == "thinking-summaries"]
    assert bindings
    items = []
    for binding in bindings:
        cursor = None
        while True:
            page = application.query("information.read", {
                "session_id": view["workflow_session_id"], "reference": binding["registration_ref"],
                "owner": binding["owner"], "generation": binding["generation"],
                "source_scope": "history", "limit": binding["declaration"]["max_page_size"],
                **({"cursor": cursor} if cursor else {})})
            assert page["status"] == "ok"
            items.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
    assert all(set(item) == {"message_id", "request_id", "thinking_summary"} for item in items)
    assert "synthetic-fixture" not in json.dumps(items)
    return items


@pytest.mark.parametrize("route", ["legacy", "bound", "summary", "native"])
def test_gemini_agent_tools_context_cold_reopen_and_public_tavern(route, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-fixture-not-a-real-key")
    fixture = GeminiGraphFixture()
    database = tmp_path / (route + ".sqlite")
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as service:
        fixture.write_provider(service)
        doc, chat = gemini_graph(service, route)
        first = run(service, create(service, doc), "First Gemini request")
        assert first["status"] == "succeeded", json.dumps([
            {"label": item["label"], "status": item["status"], "diagnostic": item.get("diagnostic")}
            for item in first["nodes"]])
        assert len(fixture.wires) == 3
        assert fixture.original_contents[0] in fixture.wires[1]["contents"]
        assert fixture.original_contents[1] in fixture.wires[2]["contents"]
        responses = [part["functionResponse"] for content in fixture.wires[1]["contents"]
                     for part in content["parts"] if "functionResponse" in part]
        assert [response["id"] for response in responses] == ["native-0-0", "native-0-1"]
        assert output(first, "agents.execute", "result")["text"] == "Gemini accepted answer"
        _, _, texts = public_read(service, first, doc, chat)
        assert texts == ["First Gemini request", "Gemini accepted answer"]
        assert "Visible summary" not in "".join(texts)
        summaries = read_public_thinking(service, first)
        assert [item["thinking_summary"] for item in summaries] == [
            "Visible summary 0", "Visible summary 1", "Visible summary 2"]
        sid, chain = first["workflow_session_id"], first["selected_chain_run_id"]
        history = service.get_run(sid, chain)
        model_requests = [fact for fact in history["runtime_facts"]
                          if fact.get("kind") == "workflow.model-fact" and fact["stage"] == "request"]
        assert [fact["details"]["wire_request"] for fact in model_requests] == fixture.wires
        assert "offline-fixture-not-a-real-key" not in json.dumps(history)
        saved_context = deepcopy(first["objects"])
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as reopened:
        cold = reopened.get_session(sid)
        assert cold["objects"] == saved_context
        assert reopened.get_run(sid, chain) == history
        assert read_public_thinking(reopened, cold) == summaries
        before = len(fixture.wires)
        assert before == 3
        second = run(reopened, cold, "After cold reopen")
        assert second["status"] == "succeeded", json.dumps([
            {"label": item["label"], "status": item["status"], "diagnostic": item.get("diagnostic")}
            for item in second["nodes"]])
        assert len(fixture.wires) == 6
        for original in fixture.original_contents[:3]:
            assert original in fixture.wires[3]["contents"]
        _, _, texts = public_read(reopened, second, doc, chat)
        assert texts == ["First Gemini request", "Gemini accepted answer",
                         "After cold reopen", "Gemini accepted answer"]
