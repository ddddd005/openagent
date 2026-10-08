"""Global prompt groups reuse existing contracts, with no network model calls."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.content_contracts import default_presentation, object_schema, stable_item_id
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.global_resources import global_resource_reference
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.prompt_package import PROMPT_RESOURCE_TYPE

from test_agent_integration import run as native_run
from test_context_native_integration import NativeTransport, native_graph, node_output, rebind, versioned_node
from test_graph_service import create, document, edge, node, run, uid
from test_models_service_integration import ModelDatabaseFixture
from test_prompt_current_resources import registry


def member(number, text, *, order=0, lifecycle="per_request", role="system", compaction="never"):
    return {
        "id": uid(number), "text": text,
        "presentation": {**default_presentation(), "role": role, "order": order},
        "metadata": {"custom": {"retained": [True, None, "fixture"]}},
        "lifecycle": lifecycle, "compaction": compaction,
    }


def record(*members):
    return {
        **ResourceIdentity("workspace", PROMPT_RESOURCE_TYPE, uid(9000)).to_dict(),
        "data_schema_version": 2, "update_sequence": 1,
        "value": {"enabled": True, "members": list(members)},
    }


def identity(value):
    return ResourceIdentity(value["scope"], value["type_id"], value["resource_id"]).to_dict()


def request(value, *, key=None):
    return {"record": value, "expected_sequence": value["update_sequence"] - 1,
            "idempotency_key": key or str(uuid4())}


def save(app, value):
    return app.command("resource.save", request(value))


def preset_graph(installed, reference, *, gate=None):
    installed = installed.detached()
    installed.register(NodeDefinition(
        "test.preset-sink", "1", "Preset sink", "Test", {}, object_schema({}),
        inputs=(NodePort("input", "PROMPT_MATERIALS"),),
        outputs=(NodePort("output", "PROMPT_MATERIALS"),), is_output=True,
    ), lambda config, inputs, context: {"output": inputs["input"]})

    reference_node = node(installed, "prompts.global-reference", 9010)
    reference_node.update(component_version="2", config={"reference": reference})
    resolver = node(installed, "prompts.global-resolve", 9011)
    resolver.update(component_version="2", config={})
    sink = node(installed, "test.preset-sink", 9012)
    doc = document([reference_node, resolver, sink], [
        edge(reference_node, resolver, 9010), edge(resolver, sink, 9011),
    ])
    doc["package_lock"] = list(installed.package_lock)
    if gate is not None:
        def wait(config, inputs, context):
            gate[0].set()
            assert gate[1].wait(10), "preset test gate was not released"
            return {}

        installed.register(NodeDefinition(
            "test.preset-gate", "1", "Preset gate", "Test", {}, object_schema({}),
        ), wait)
        blocker = node(installed, "test.preset-gate", 9013)
        doc["nodes"].append(blocker)
        doc["control_edges"] = [{
            "edge_id": uid(9020), "source_node_id": blocker["node_binding_id"],
            "target_node_id": resolver["node_binding_id"],
        }]
    return installed, doc, resolver, sink


def test_schema_two_save_read_cas_receipt_replay_and_reopen(tmp_path):
    database = tmp_path / "presets.sqlite"
    installed = registry()
    original = record(member(9001, "Original background", lifecycle="context_once",
                             role="user", compaction="allowed"))
    original_request = request(original)
    with closing(GraphWorkflowService(database, registry=installed)) as service:
        app = GraphApplication(service)
        accepted = app.command("resource.save", original_request)
        assert accepted["result"] == {
            "reference": identity(original), "update_sequence": 1, "deleted": False}
        assert "Original background" not in canonical_bytes(accepted).decode()
        changed = deepcopy(original)
        changed["update_sequence"] = 2
        changed["value"]["members"][0]["text"] = "Edited background"
        save(app, changed)
        assert app.command("resource.save", original_request) == accepted
        receipt = app.query("receipt.read", {"operation": "resource.save", "parameters": original_request})
        assert receipt["outcome"] == "matched"
        assert receipt["result"] == accepted["result"] and receipt["receipt"] == accepted["receipt"]
        with pytest.raises(ContractValidationError) as stale:
            save(app, original)
        assert stale.value.reason_code == "stale_revision"
        assert app.query("resource.read", {"identity": identity(original)}) == changed
        assert app.query("resource.list", {"scope": "workspace", "type_id": PROMPT_RESOURCE_TYPE}) == [changed]
        assert not hasattr(service, "_native_runtime")
    with closing(GraphWorkflowService(database, registry=installed)) as reopened:
        app = GraphApplication(reopened)
        assert app.query("resource.read", {"identity": identity(original)}) == changed
        assert app.query("receipt.read", {
            "operation": "resource.save", "parameters": original_request}) == receipt


@pytest.mark.parametrize("problem,reason", [
    ("missing", "global_resource_missing"), ("disabled", "global_content_disabled"),
    ("schema-one", "graph_prompt_resource_type_mismatch"),
])
def test_version_two_preflight_rejects_before_start_without_changing_session(tmp_path, problem, reason):
    value = record(member(9001, "Rule"))
    installed, doc, _, _ = preset_graph(registry(), identity(value))
    with closing(GraphWorkflowService(tmp_path / "preflight.sqlite", registry=installed)) as service:
        if problem != "missing":
            if problem == "disabled":
                value["value"]["enabled"] = False
            else:
                value["data_schema_version"] = 1
                for entry in value["value"]["members"]:
                    del entry["lifecycle"], entry["compaction"]
            save(GraphApplication(service), value)
        initial = create(service, doc)
        with pytest.raises(ContractValidationError) as caught:
            run(service, initial)
        assert caught.value.reason_code == reason
        assert service.get_session(initial["workflow_session_id"]) == initial
        assert service._resource_frames == {} and not hasattr(service, "_native_runtime")


@pytest.mark.parametrize("change", ["name", "implicit-lifecycle", "system-compaction"])
def test_preset_management_keeps_existing_strict_schema_and_current_body(tmp_path, change):
    value = record(member(9001, "Rule"))
    with closing(GraphWorkflowService(tmp_path / "strict.sqlite", registry=registry())) as service:
        app = GraphApplication(service)
        save(app, value)
        invalid = deepcopy(value)
        invalid["update_sequence"] = 2
        if change == "name":
            invalid["value"]["name"] = "Unsupported group name"
        elif change == "implicit-lifecycle":
            del invalid["value"]["members"][0]["lifecycle"]
        else:
            invalid["value"]["members"][0].update(lifecycle="context_once", compaction="allowed")
        with pytest.raises(ContractValidationError):
            save(app, invalid)
        assert app.query("resource.read", {"identity": identity(value)}) == value


def test_global_group_freezes_at_start_and_preserves_order_and_old_history(tmp_path):
    value = record(member(9001, "Later rule", order=8), member(9002, "Earlier rule", order=-7))
    entered, release = Event(), Event()
    installed, doc, resolver, sink = preset_graph(registry(), identity(value), gate=(entered, release))
    database = tmp_path / "frozen.sqlite"
    with closing(GraphWorkflowService(database, registry=installed)) as service:
        app = GraphApplication(service)
        save(app, value)
        initial = create(service, doc)
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        try:
            assert entered.wait(10), "preset resolver gate did not start"
            changed = deepcopy(value)
            changed["update_sequence"] = 2
            changed["value"]["members"][0]["text"] = "Updated later rule"
            changed["value"]["members"][0]["presentation"]["order"] = -9
            save(app, changed)
        finally:
            release.set()
        service.wait(started["active_chain_run_id"])
        first = service.get_session(initial["workflow_session_id"])
        assert first["status"] == "succeeded", first["chains"]
        assert node_output(first, doc["nodes"][0]) == global_resource_reference(identity(value))
        assert [entry["text"] for entry in node_output(first, sink)["items"]] == ["Earlier rule", "Later rule"]
        first_ids = {entry["source"]["member_id"]: entry["origin_item_ids"]
                     for entry in node_output(first, resolver)["items"]}
        archived = service.get_run(first["workflow_session_id"], first["selected_chain_run_id"])
        second = run(service, first)
        assert second["status"] == "succeeded", second["chains"]
        assert [entry["text"] for entry in node_output(second, sink)["items"]] == [
            "Updated later rule", "Earlier rule"]
        assert {entry["source"]["member_id"]: entry["origin_item_ids"]
                for entry in node_output(second, resolver)["items"]} == first_ids
        assert service.get_run(first["workflow_session_id"], first["selected_chain_run_id"]) == archived
        assert second["objects"] == {} and service._resource_frames == {}
    with closing(GraphWorkflowService(database, registry=installed)) as reopened:
        assert reopened.get_run(first["workflow_session_id"], first["selected_chain_run_id"]) == archived


def test_real_workflow_edits_keep_once_identity_and_explicit_copy_creates_new_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-tavern-prompt-presets")
    transport = NativeTransport()
    database = tmp_path / "native.sqlite"
    value = record(
        member(9001, "PRESET_FIXED_RULE", order=-3),
        member(9002, "PRESET_ORIGINAL_BACKGROUND", order=5, lifecycle="context_once",
               role="user", compaction="allowed"),
        member(9003, "PRESET_DISABLED_BACKGROUND", order=8, lifecycle="context_once", role="user"),
    )
    value["value"]["members"][2]["presentation"]["enabled"] = False
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        app = GraphApplication(service)
        save(app, value)
        doc, entries = native_graph(service, policy=False, once=False)
        resolver = entries["a"]["fixed"]
        resolver.update(component_id="prompts.global-resolve", component_version="2", config={})
        reference = versioned_node(service, "prompts.global-reference", "2", 9030)
        reference["config"] = {"reference": identity(value)}
        doc["nodes"].append(reference)
        doc["edges"].append(edge(reference, resolver, 9030))

        first = native_run(service, create(service, doc), "First input.")
        assert first["status"] == "succeeded", first["chains"]
        first_prompt = node_output(first, entries["a"]["assembly"])
        original_once_id = stable_item_id(resolver["node_binding_id"], value["value"]["members"][1]["id"])
        assert first_prompt["once_pending_item_ids"] == [original_once_id]
        assert [message["blocks"][0]["text"] for message in first_prompt["messages"]] == [
            "PRESET_FIXED_RULE", "PRESET_ORIGINAL_BACKGROUND", "First input."]
        effective = node_output(first, entries["a"]["merge"])
        assert effective["once_injected_item_ids"] == [original_once_id]
        assert "PRESET_FIXED_RULE" not in str(effective["messages"])
        assert "PRESET_DISABLED_BACKGROUND" not in str(effective["messages"])
        archived = service.get_run(first["workflow_session_id"], first["selected_chain_run_id"])

        changed = deepcopy(value)
        changed["update_sequence"] = 2
        changed["value"]["members"][0]["text"] = "PRESET_EDITED_RULE"
        changed["value"]["members"][1]["text"] = "PRESET_EDITED_BACKGROUND"
        changed["value"]["members"].reverse()
        for index, entry in enumerate(changed["value"]["members"]):
            entry["presentation"]["order"] = index
        save(app, changed)
        second = native_run(service, first, "Second input.")
        assert second["status"] == "succeeded", second["chains"]
        assert node_output(second, entries["a"]["assembly"])["once_pending_item_ids"] == []
        wire = [message.get("content") for message in transport.calls[-1]["messages"]]
        assert wire.count("PRESET_ORIGINAL_BACKGROUND") == 1
        assert "PRESET_EDITED_BACKGROUND" not in wire
        assert wire.count("PRESET_EDITED_RULE") == 1
        assert node_output(second, entries["a"]["merge"])["once_injected_item_ids"] == [original_once_id]

        copied = deepcopy(changed)
        copied.update(resource_id=str(uuid4()), update_sequence=1)
        for entry in copied["value"]["members"]:
            entry["id"] = str(uuid4())
        save(app, copied)
        assert reference["config"] == {"reference": identity(value)}
        assert app.query("resource.read", {"identity": identity(value)}) == changed
        reference["config"] = {"reference": identity(copied)}
        rebound, doc = rebind(service, second, doc)
        third = native_run(service, rebound, "Third input.")
        assert third["status"] == "succeeded", third["chains"]
        new_once_id = stable_item_id(resolver["node_binding_id"], copied["value"]["members"][1]["id"])
        assert new_once_id != original_once_id
        assert node_output(third, entries["a"]["assembly"])["once_pending_item_ids"] == [new_once_id]
        third_effective = node_output(third, entries["a"]["merge"])
        assert set(third_effective["once_injected_item_ids"]) == {original_once_id, new_once_id}
        wire = [message.get("content") for message in transport.calls[-1]["messages"]]
        assert wire.count("PRESET_ORIGINAL_BACKGROUND") == wire.count("PRESET_EDITED_BACKGROUND") == 1
        assert "PRESET_EDITED_RULE" not in str(third_effective["messages"])
        assert service.get_run(first["workflow_session_id"], first["selected_chain_run_id"]) == archived
        sid = third["workflow_session_id"]

    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as reopened:
        fourth = native_run(reopened, reopened.get_session(sid), "Fourth input after reopen.")
        assert fourth["status"] == "succeeded", fourth["chains"]
        assert node_output(fourth, entries["a"]["assembly"])["once_pending_item_ids"] == []
        wire = [message.get("content") for message in transport.calls[-1]["messages"]]
        assert wire.count("PRESET_EDITED_BACKGROUND") == wire.count("PRESET_ORIGINAL_BACKGROUND") == 1
        assert len(transport.calls) == 4 and not transport.summaries
        assert reopened.get_run(first["workflow_session_id"], first["selected_chain_run_id"]) == archived
