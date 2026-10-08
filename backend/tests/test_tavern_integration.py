"""Offline acceptance of lorebook scanning, assembly and native history."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.lorebook_engine import default_lorebook_entry
from phase1_agent.session_objects import SessionObjectStore

from workflow_test_support import run
from test_context_native_integration import (
    NativeTransport, native_graph, node_output, versioned_node,
)
from test_graph_service import create, edge, uid
from test_models_service_integration import ModelDatabaseFixture


def entry(number, text, keywords=(), **options):
    value = default_lorebook_entry(uid(number))
    value.update(text=text, primary_keywords=list(keywords), **options)
    return value


def tavern_graph(service, entries_a, entries_b=(), *, single=False, variables=False):
    doc, native = native_graph(service, policy=False, once=False)
    lane = native["a"]
    current = next(item for item in doc["nodes"] if item["component_id"] == "tools.current-input")
    scan_a = versioned_node(service, "context.assembly", "4", 6100)
    scan_b = versioned_node(service, "context.assembly", "4", 6101)
    lore_a = versioned_node(service, "lorebook.item" if single else "lorebook.group", "1", 6102)
    lore_b = versioned_node(service, "lorebook.group", "1", 6103)
    summary = versioned_node(service, "prompts.summary", "2", 6104)
    lore_a["config"].update({"entry": entries_a[0]} if single else {"entries": entries_a})
    lore_b["config"]["entries"] = list(entries_b)
    doc["nodes"].extend([scan_a, scan_b, lore_a, lore_b, summary])
    doc["edges"] = [item for item in doc["edges"] if not (
        item["source_node_id"] == lane["fixed"]["node_binding_id"]
        and item["target_node_id"] == lane["assembly"]["node_binding_id"])]
    doc["edges"].extend([
        edge(lane["read"], scan_a, 7100, target_port="view"),
        edge(current, scan_a, 7101, target_port="current_input"),
        edge(lane["fixed"], scan_a, 7102, target_port="materials"),
        edge(scan_a, lore_a, 7103),
        edge(lane["read"], scan_b, 7104, target_port="view"),
        edge(current, scan_b, 7105, target_port="current_input"),
        edge(lane["fixed"], scan_b, 7106, target_port="materials"),
        edge(lore_a, scan_b, 7107, target_port="materials", order=1),
        edge(scan_b, lore_b, 7108),
        edge(lane["fixed"], summary, 7109),
        edge(lore_a, summary, 7110, order=1),
        edge(lore_b, summary, 7111, order=2),
        edge(summary, lane["assembly"], 7112, target_port="materials"),
    ])
    if variables:
        register = versioned_node(service, "tools.variable-register", "1", 6099)
        register["config"].update(object_key="variable/keyword", name="keyword",
                                  has_initial=True, initial_value="DRAGON")
        lore_a["config"]["object_keys"] = ["variable/keyword"]
        doc["nodes"].append(register)
        doc["control_edges"] = [{
            "edge_id": uid(19000), "source_node_id": register["node_binding_id"],
            "target_node_id": lore_a["node_binding_id"],
        }]
        doc["object_bindings"].append(ObjectBinding(
            "variable/keyword", "workflow.variable", 1, "shared",
            readers=(register["node_binding_id"], lore_a["node_binding_id"]),
            writers=(register["node_binding_id"],),
        ).to_dict())
    doc["package_lock"] = list(service.registry.package_lock)
    doc["name"] = "Offline Lorebook Acceptance"
    for index, item in enumerate(doc["nodes"]):
        item["position"] = {"x": (index % 5) * 300, "y": (index // 5) * 220}
    return doc, {**lane, "scan_a": scan_a, "scan_b": scan_b, "lore_a": lore_a,
                 "lore_b": lore_b, "summary": summary}


def recursive_entries(prefix, number, initial):
    return [
        entry(number + index, f"{prefix}_STEP_{index}",
              [initial if index == 0 else f"{prefix}_STEP_{index - 1}"], recursive=True)
        for index in range(5)
    ]


def material_texts(view, node):
    return {item["text"] for item in node_output(view, node)["items"]}


def frozen_output(history, node, port="output"):
    return next(item["payload"] for item in history["outputs"]
                if item["node_binding_id"] == node["node_binding_id"]
                and item["port_id"] == port)


def test_two_lorebooks_own_three_rounds_and_materials_do_not_persist_or_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-lorebook")
    transport = NativeTransport()
    database = tmp_path / "recursive.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        a = recursive_entries("A", 8000, "dragon")
        b = recursive_entries("B", 8100, "A_STEP_3")
        a[2]["presentation"].update(role="user", placement="middle", depth=0)
        b[1]["presentation"].update(role="assistant", placement="middle", depth=20)
        doc, nodes = tavern_graph(service, a, b)
        first = run(service, create(service, doc), "A dragon arrives.")
        assert first["status"] == "succeeded", first["chains"]
        assert material_texts(first, nodes["lore_a"]) == {f"A_STEP_{i}" for i in range(4)}
        assert material_texts(first, nodes["lore_b"]) == {f"B_STEP_{i}" for i in range(4)}
        assembled = node_output(first, nodes["assembly"])
        texts = [message["blocks"][0]["text"] for message in assembled["messages"]]
        assert texts.count("FIXED_RULE_a") == texts.count("A dragon arrives.") == 1
        assert texts.index("A_STEP_2") > texts.index("A dragon arrives.")
        assert texts.index("B_STEP_1") < texts.index("A dragon arrives.")
        wire = [message.get("content") for message in transport.calls[-1]["messages"]]
        assert wire.count("FIXED_RULE_a") == wire.count("A dragon arrives.") == 1
        assert all(wire.count(f"{prefix}_STEP_{index}") == 1
                   for prefix in ("A", "B") for index in range(4))
        assert "A_STEP_4" not in wire and "B_STEP_4" not in wire
        for item in node_output(first, nodes["summary"])["items"]:
            assert item["lifecycle"] == "per_request" and item["compaction"] == "never"
        assert [message["blocks"][0]["text"] for message in
                node_output(first, nodes["merge"])["messages"]] == [
                    "A dragon arrives.", "Native accepted answer"]
        sid, chain = first["workflow_session_id"], first["selected_chain_run_id"]
        saved_material = deepcopy(node_output(first, nodes["lore_a"]))
        # The first request remains real history, but only the latest user floor is scanned.
        for node in (nodes["lore_a"], nodes["lore_b"]):
            for member in node["config"]["entries"]:
                member["scan_depth"] = 1
        doc["revision"] = 2
        service.save_definition(doc, expected_revision=1, idempotency_key=str(uuid4()))
        rebound = service.rebind_session(
            sid, definition_revision=2, expected_revision=first["revision"],
            expected_data_revision=first["data_revision"], expected_head_revision=first["head_revision"],
            idempotency_key=str(uuid4()))
        second = run(service, rebound, "A quiet continuation.")
        assert second["status"] == "succeeded", second["chains"]
        assert not material_texts(second, nodes["lore_a"])
        assert not material_texts(second, nodes["lore_b"])
        assert "A_STEP_" not in str(node_output(second, nodes["merge"])["messages"])
        assert "B_STEP_" not in str(node_output(second, nodes["merge"])["messages"])
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as reopened:
        history = reopened.get_run(sid, chain)
        assert frozen_output(history, nodes["lore_a"]) == saved_material
        assert frozen_output(history, nodes["assembly"]) == assembled
        assert reopened.get_session(sid)["status"] == "succeeded"
        assert len(transport.calls) == 2


def test_variable_keyword_is_read_only_and_role_does_not_turn_material_into_history(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-lorebook")
    transport = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "variables.sqlite",
                                      public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        members = [
            entry(8200, "VARIABLE_MATCH", ["{{keyword}}"]),
            entry(8201, "MISSING_VARIABLE", ["{{absent}}"]),
            entry(8202, "FAKE_CONVERSATION_MATCH", ["MATERIAL_ONLY"]),
        ]
        doc, nodes = tavern_graph(service, members, variables=True)
        nodes["fixed"]["config"]["text"] = "MATERIAL_ONLY"
        nodes["fixed"]["config"]["presentation"]["role"] = "user"
        first = run(service, create(service, doc), "A dragon is visible.")
        assert first["status"] == "succeeded", first["chains"]
        assert material_texts(first, nodes["lore_a"]) == {"VARIABLE_MATCH"}
        variable = deepcopy(first["objects"]["variable/keyword"])
        assert variable["value"]["value"] == "DRAGON" and variable["revision"] == 2
        second = run(service, first, "Still discussing a dragon.")
        assert second["status"] == "succeeded", second["chains"]
        assert material_texts(second, nodes["lore_a"]) == {"VARIABLE_MATCH"}
        assert second["objects"]["variable/keyword"] == variable


def test_single_entry_and_probability_are_frozen_across_acceptance_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-lorebook")
    from phase1_agent import lorebook_engine
    samples = []

    def draw():
        samples.append(0.25)
        return samples[-1]

    monkeypatch.setattr(lorebook_engine.random, "random", draw)
    transport = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "retry.sqlite",
                                      public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        member = entry(8300, "PROBABILITY_FROZEN", mode="constant",
                       probability_enabled=True, probability=50)
        doc, nodes = tavern_graph(service, [member], single=True)
        initial = create(service, doc)
        apply, fail = SessionObjectStore.apply, [True]

        def fail_once(store, sid, *, node_id, writes):
            receipts = apply(store, sid, node_id=node_id, writes=writes)
            if writes and fail[0]:
                fail[0] = False
                raise OSError("injected lorebook acceptance failure")
            return receipts

        monkeypatch.setattr(SessionObjectStore, "apply", fail_once)
        pending = run(service, initial, "An ordinary request.")
        assert pending["status"] == "archive_failed", pending["chains"]
        accepted = deepcopy(node_output(pending, nodes["lore_a"]))
        assert samples == [0.25] and material_texts(pending, nodes["lore_a"]) == {"PROBABILITY_FROZEN"}
        service.control(pending["workflow_session_id"], action="retry_acceptance",
                        expected_revision=pending["revision"], idempotency_key=str(uuid4()))
        service.wait(pending["active_chain_run_id"])
        final = service.get_session(pending["workflow_session_id"])
        assert final["status"] == "succeeded", final["chains"]
        assert node_output(final, nodes["lore_a"]) == accepted
        assert samples == [0.25] and len(transport.calls) == 1
        assert "PROBABILITY_FROZEN" not in str(node_output(final, nodes["merge"])["messages"])


@pytest.mark.parametrize("mode", ["constant", "keyword"])
def test_disabled_or_probability_zero_has_empty_material_output(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-lorebook")
    transport = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "empty.sqlite",
                                      public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        members = [
            entry(8400, "ZERO_PROBABILITY", ["dragon"], mode=mode,
                  probability_enabled=True, probability=0),
            entry(8401, "DISABLED", ["dragon"], mode=mode),
        ]
        members[1]["presentation"]["enabled"] = False
        doc, nodes = tavern_graph(service, members)
        result = run(service, create(service, doc), "dragon")
        assert result["status"] == "succeeded", result["chains"]
        assert node_output(result, nodes["lore_a"]) == {
            "schema_version": 1, "kind": "workflow.prompt-materials", "items": []}
        assert "ZERO_PROBABILITY" not in str(transport.calls)
        assert "DISABLED" not in str(transport.calls)
