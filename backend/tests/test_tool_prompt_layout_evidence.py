"""Second-round evidence for tool prompt layout and real model transport projection.

The graph uses the installed built-in packages, one real isolated SQLite store,
the real variable executors and a controlled local model transport.  It does not
start a product service, call a real model or touch a default database.
"""

from contextlib import closing
from uuid import uuid4

from phase1_agent.contract_json import canonical_bytes
from phase1_agent.prompt_depth import logical_floor_materials

from test_models_service_integration import ModelDatabaseFixture
from test_variable_tool_bindings import (
    DEFAULT_PARAMETERS, DEFAULT_PRESENTATION, KEY, VariableToolTransport,
    _registered, _run, _service, build_graph, canonical_texts, node_output,
)


def _events(service, view, chain_id):
    return [fact["payload"] for fact in service.get_run(
        view["workflow_session_id"], chain_id)["runtime_facts"]
        if fact.get("executor_ref") and isinstance(fact.get("payload"), dict)]


def _lifecycle_item(text, order):
    identity = str(uuid4())
    return {
        "item_instance_id": identity, "text": text, "role": "system",
        "placement": "before", "depth": None, "order": order, "enabled": True,
        "purpose": "tool-description", "source": {"kind": "layout-evidence"},
        "protected": False, "metadata": {}, "lifecycle": "per_request",
        "compaction": "never", "origin_item_ids": [identity],
    }


def test_middle_parameters_reach_real_transport_with_matching_roles_and_text(tmp_path):
    transport = VariableToolTransport([
        ("final_answer", {"answer": {"text": "first"}}),
        ("score_read", {}),
        ("final_answer", {"answer": {"text": "second"}}),
    ])
    with closing(_service(tmp_path / "tool-prompt-layout.sqlite", transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, parts = build_graph(
            service,
            parameters_presentation={**DEFAULT_PARAMETERS, "placement": "middle", "depth": 1},
            list_presentation={**DEFAULT_PRESENTATION, "placement": "after"},
        )
        session = _registered(service, document)
        assert session["objects"][KEY]["value"]["value"] == 0

        _first_chain, first_view = _run(service, session, text="first question")
        assert first_view["status"] == "succeeded", first_view["chains"]
        second_chain, view = _run(service, first_view, text="second question")
        assert view["status"] == "succeeded", view["chains"]
        assert view["selected_chain_run_id"] == second_chain
        assert len(transport.calls) == 3

        prompt = node_output(view, parts["assembly"])
        assert prompt["schema_version"] == 6 and prompt["kind"] == "workflow.prompt"
        canonical = [(message["role"], "\n".join(block["text"] for block in message["blocks"]))
                     for message in prompt["messages"]]
        assert [text for _, text in canonical] == canonical_texts(prompt["messages"])
        request = transport.calls[1]["messages"]
        projected = [(message["role"], message.get("content", "")) for message in request]
        assert projected == canonical

        loader_output = node_output(view, parts["loader"], port="tool_prompt")
        entries = loader_output["entries"]
        names = [entry["name"] for entry in entries]
        assert names == ["score_read", "score_set", "score_add", "score_subtract"]

        agent = next(event["payload"] for event in _events(service, view, second_chain)
                     if event["kind"] == "agent_result")
        declarations = {tool["name"]: tool for tool in agent["snapshot"]["tool_definitions"]}
        assert set(names) <= set(declarations)
        for entry in entries:
            declaration = declarations[entry["name"]]
            assert entry["description"] == declaration["description"]
            assert entry["parameters"] == declaration["parameters_schema"]
        wire_names = [tool["function"]["name"] for tool in transport.calls[1]["tools"]]
        assert [name for name in wire_names if name in set(names)] == names

        texts = [text for _, text in projected]
        current_index = texts.index("second question")
        parameter_texts = [canonical_bytes(entry["parameters"]).decode("utf-8")
                           for entry in entries]
        description_texts = [entry["description"] for entry in entries]
        history_indexes = [texts.index("first question"), texts.index("first")]
        middle_start = max(history_indexes) + 1
        assert middle_start > texts.index("first question")
        assert middle_start < current_index
        assert sorted(texts[middle_start:current_index]) == sorted(parameter_texts)
        assert max(texts.index(text) for text in parameter_texts) < current_index
        assert sorted(texts[current_index + 1:]) == sorted(description_texts)
        assert min(texts.index(text) for text in description_texts) > current_index


def test_material_order_controls_same_placement_sequence():
    high_order = _lifecycle_item("parameters after", 5)
    low_order = _lifecycle_item("parameters first", 1)
    buckets = logical_floor_materials([high_order, low_order], [0], 1)
    assert list(buckets) == [0]
    assert buckets[0] == [low_order, high_order]
    assert [item["order"] for item in buckets[0]] == [1, 5]
