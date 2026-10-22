"""Offline creation and historical computation of explicit session preloads."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent import graph_service as graph_service_module
from phase1_agent import session_objects as session_objects_module
from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE
from phase1_agent.checkpoint_collection import collect_checkpoint_state
from phase1_agent.checkpoint_loading import load_checkpoint_state_in_transaction
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding
from phase1_agent.runtime_checkpoints import (
    make_diff, validate_checkpoint_state, validate_preloaded_items, validate_state_storage,
)
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.variable_contract import VARIABLE_TYPE
from phase1_agent.workflow_chat_operations import WorkflowChatOperations
from phase1_agent.workflow_chat_runtime import read_chat_round_inputs

from test_checkpoint_collection import transaction
from test_checkpoint_fork_stage2_acceptance import _fork
from test_graph_application_receipt_custody import raw_database
from test_graph_service import document, edge, node
from test_runtime_checkpoints import child, materialize_records, root, state
from test_success_checkpoints import checkpoint_history
from test_workflow_chat_service import chat, guards, reroll, submit


STRUCTURE = "test.preloaded-structure"


def variable(value=2):
    return {"registered": True, "name": "score", "value_type": "integer",
            "assigned": True, "value": value}


def structure_entry(count):
    return {"type_id": STRUCTURE, "schema_version": 1, "value": {"count": count}}


def preload_package(calls):
    def register(host):
        host.register_data_type(DataTypeDefinition(
            STRUCTURE, 1,
            {"type": "object", "properties": {"count": {"type": "integer"}},
             "required": ["count"], "additionalProperties": False},
            checkpoint_admission=True))

        def compute(config, inputs, context):
            counter = context.object_read("counter")
            structure = context.object_read("structure")
            calls.append((counter["value"]["value"], structure["value"]["count"]))
            amount = len(calls)
            value = {**counter["value"], "value": counter["value"]["value"] + amount}
            context.object_write("counter", value, expected_revision=counter["revision"])
            context.object_write("structure", {"count": structure["value"]["count"] + amount * 10},
                                 expected_revision=structure["revision"])
            return {"output": text_content(inputs["input"]["text"] + ":" + str(value["value"]))}

        host.register_node(NodeDefinition(
            "test.preloaded-compute", "1", "Compute", "Test",
            {"counter": "counter", "structure": "structure"},
            {"type": "object", "properties": {"counter": {"type": "string"}, "structure": {"type": "string"}},
             "required": ["counter", "structure"], "additionalProperties": False},
            inputs=(NodePort("input", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
            capabilities=("objects:read", "objects:write"),
            object_accesses=(
                {"config_field": "counter", "multiple": False, "access": "read_write",
                 "type_id": VARIABLE_TYPE, "schema_version": 1},
                {"config_field": "structure", "multiple": False, "access": "read_write",
                 "type_id": STRUCTURE, "schema_version": 1},
            ),
        ), compute)

    return CapabilityPackage(PackageManifest(
        "test.preloads", "1", (PackageDependency("workflow.content", "1.0.0"),
                               PackageDependency("workflow.tools", "1.0.0"))), register)


@pytest.fixture
def service(tmp_path):
    calls = []

    def no_model(**kwargs):
        pytest.fail("Preload test attempted to construct a model")

    with closing(GraphWorkflowService(
            tmp_path / "preloads.sqlite", capability_packages=(preload_package(calls),),
            enabled_packages={**DEFAULT_PACKAGES, "test.preloads": "1"},
            public_model_factory=no_model)) as instance:
        yield instance, calls


def save_graph(service):
    source = node(service.registry, "tools.editable-input", 55801, text="draft")
    compute = node(service.registry, "test.preloaded-compute", 55802)
    frontend = node(service.registry, "frontend.node", 55803, component_version="2",
                    object_key="chat", role="assistant", input_node_id=source["node_binding_id"])
    graph = document([source, compute, frontend], [
        edge(source, compute, 55801), edge(compute, frontend, 55802, target_port="content")])
    graph.update(
        schema_version=3, package_lock=list(service.registry.execution_package_lock),
        object_bindings=[
            ObjectBinding("counter", VARIABLE_TYPE, 1, "shared",
                          readers=(compute["node_binding_id"],), writers=(compute["node_binding_id"],)).to_dict(),
            ObjectBinding("structure", STRUCTURE, 1, "shared",
                          readers=(compute["node_binding_id"],), writers=(compute["node_binding_id"],)).to_dict(),
            ObjectBinding("chat", CHAT_CONTEXT_TYPE, 1, "shared",
                          readers=(frontend["node_binding_id"],), writers=(frontend["node_binding_id"],),
                          registrar_node_id=frontend["node_binding_id"]).to_dict(),
        ],
        session_preloads=[{"object_key": "counter", "value": variable()},
                          {"object_key": "structure", "value": {"count": 7}}],
    )
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"]).definition.to_dict()["default_configuration_policy"])
    return service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))["document"]


def create(service, graph, *, key=None):
    return service.create_session(
        graph["workflow_definition_id"], graph["revision"], idempotency_key=key or str(uuid4()))


def values(view):
    return (view["objects"]["counter"]["value"]["value"],
            view["objects"]["structure"]["value"]["count"])


def test_old_canonical_storage_and_optional_generic_full_diff():
    old = state()
    before = canonical_bytes(old)
    assert canonical_bytes(validate_checkpoint_state(old)) == before
    first = root(old)
    old_diff = child(first, first, old, old)
    encoded = canonical_bytes(old_diff)
    assert canonical_bytes(validate_state_storage(old_diff["state_storage"])) == canonical_bytes(
        old_diff["state_storage"])
    assert canonical_bytes(materialize_records(old_diff, [first, old_diff])) == before
    assert canonical_bytes(old_diff) == encoded

    new = deepcopy(old)
    new["preloaded_items"] = {"structure": structure_entry(8)}
    second = child(first, old_diff, old, new)
    assert second["state_storage"]["upsert"]["preloaded_items"] == new["preloaded_items"]
    assert materialize_records(second, [first, old_diff, second]) == new
    deleted = deepcopy(new)
    deleted["preloaded_items"] = {}
    third = child(first, second, new, deleted)
    assert third["state_storage"]["delete"]["preloaded_items"] == ["structure"]
    assert materialize_records(third, [first, old_diff, second, third]) == deleted
    with pytest.raises(ContractValidationError):
        validate_state_storage({**second["state_storage"], "delete": {
            key: value for key, value in second["state_storage"]["delete"].items() if key != "preloaded_items"}})
    overlapping = deepcopy(new)
    overlapping["preloaded_items"][next(iter(old["agent_contexts"]))] = structure_entry(1)
    with pytest.raises(ContractValidationError):
        validate_checkpoint_state(overlapping)
    with pytest.raises(ContractValidationError):
        validate_preloaded_items({"x": {"type_id": VARIABLE_TYPE, "schema_version": 1, "value": variable()}})


@pytest.mark.parametrize("items", [{}, {"structure": structure_entry(8)}])
def test_established_generic_partition_cannot_be_omitted_but_can_be_explicitly_cleared(items):
    parent = state()
    parent["preloaded_items"] = deepcopy(items)
    encoded = canonical_bytes(parent)
    with pytest.raises(ContractValidationError) as denied:
        make_diff(parent, state())
    assert denied.value.reason_code == "runtime_checkpoint_invalid"
    cleared = {**state(), "preloaded_items": {}}
    first = root(parent)
    second = child(first, first, parent, cleared)
    assert materialize_records(second, [first, second]) == cleared
    assert canonical_bytes(parent) == encoded


def test_create_once_no_default_isolated_and_replay_keeps_changed_values(service):
    instance, calls = service
    graph = save_graph(instance)
    assert not instance.list_sessions(graph["workflow_definition_id"])
    assert "has_default" in instance.registry.data_types.get(STRUCTURE, 1).to_dict()
    assert instance.registry.data_types.get(STRUCTURE, 1).to_dict()["has_default"] is False
    key = str(uuid4())
    first = create(instance, graph, key=key)
    second = create(instance, graph)
    assert values(first) == values(second) == (2, 7)
    assert all(first["objects"][name]["revision"] == 1 for name in ("counter", "structure"))
    changed = submit(instance, first, "one")
    assert values(changed) == (3, 17) and values(instance.get_session(second["workflow_session_id"])) == (2, 7)
    replay = create(instance, graph, key=key)
    assert replay == first
    assert instance.get_session(first["workflow_session_id"]) == changed
    assert calls == [(2, 7)]
    with pytest.raises(ContractValidationError):
        instance._change("test.preload.reinitialize", str(uuid4()), {}, lambda repo:
                         instance._objects(repo).initialize_preloads(first["workflow_session_id"], graph))


def test_create_failure_rolls_back_all_heads_seed_and_receipt(service, monkeypatch):
    instance, _ = service
    graph = save_graph(instance)
    before = raw_database(instance.database)
    original = SessionObjectStore._validate_initialized_objects

    def fail_after_all_items(self, sid, **kwargs):
        assert set(self.current(sid)) == {"counter", "structure", "chat"}
        original(self, sid, **kwargs)
        raise RuntimeError("injected preload validation failure")

    monkeypatch.setattr(SessionObjectStore, "_validate_initialized_objects", fail_after_all_items)
    with pytest.raises(RuntimeError, match="injected preload"):
        create(instance, graph)
    assert raw_database(instance.database) == before
    assert instance.list_sessions(graph["workflow_definition_id"]) == []


@pytest.mark.parametrize("wrong_reference", [False, True])
def test_base_reference_to_preload_is_checked_after_complete_creation(service, monkeypatch, wrong_reference):
    instance, _ = service
    graph = save_graph(instance)
    sid = str(uuid4())
    revision_ids = [uuid4() for _ in range(4)]
    registry = instance.registry.detached()
    registry.data_types.register(DataTypeDefinition(
        "test.base-reference", 1, {"type": "object"},
        references=lambda value: [value["ref"]]))
    instance.registry = registry
    reader = graph["object_bindings"][1]["readers"][0]
    graph["object_bindings"].append(ObjectBinding(
        "base-reference", "test.base-reference", 1, "shared", readers=(reader,),
        default_value={"ref": {
            "scope": "session", "workflow_session_id": sid, "object_key": "structure",
            "revision_id": str(revision_ids[2] if wrong_reference else revision_ids[3]),
        }}).to_dict())
    graph = instance.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=graph["edit_sequence"], idempotency_key=str(uuid4()))["document"]
    before = raw_database(instance.database)
    original_uid = graph_service_module._uid
    pending_sid = [sid]
    monkeypatch.setattr(graph_service_module, "_uid",
                        lambda: pending_sid.pop() if pending_sid else original_uid())
    planned_revisions = iter(revision_ids)
    monkeypatch.setattr(session_objects_module, "uuid4", lambda: next(planned_revisions))
    if wrong_reference:
        with pytest.raises(ContractValidationError) as denied:
            create(instance, graph)
        assert denied.value.reason_code == "session_object_reference_denied"
        assert raw_database(instance.database) == before
        assert instance.list_sessions(graph["workflow_definition_id"]) == []
    else:
        created = create(instance, graph)
        assert created["workflow_session_id"] == sid
        assert values(created) == (2, 7)
        assert created["objects"]["structure"]["revision_id"] == str(revision_ids[3])
        assert created["objects"]["base-reference"]["value"]["ref"]["revision_id"] == str(revision_ids[3])


def test_real_rounds_two_collectors_checkpoint_fork_and_reopen(service):
    instance, calls = service
    graph = save_graph(instance)
    view = create(instance, graph)
    first = submit(instance, view, "one")
    second = submit(instance, first, "two")
    history = checkpoint_history(instance, second)
    assert [point["state_storage"]["mode"] for point in history["path"]] == ["full", "diff"]
    assert history["state"]["preloaded_items"] == {"structure": structure_entry(37)}
    assert history["state"] == history["collected"]
    result = _fork(instance, second, history["path"][0]["checkpoint_id"])
    target = instance.get_session(result["target_session_id"])
    assert values(target) == (3, 17)
    assert calls == [(2, 7), (3, 17)]
    assert "registrar_node_id" not in target["objects"]["counter"]["binding"]
    with closing(instance._store()) as store:
        with transaction(store._connection):
            collected = collect_checkpoint_state(
                GraphRecordStore(store), graph["workflow_definition_id"], graph["revision"],
                target["workflow_session_id"], types=instance.registry.data_types, registry=instance.registry)
            assert collected["preloaded_items"] == {"structure": structure_entry(17)}
            assert collected["variables"]["counter"]["value"] == variable(3)
    instance.close()
    with closing(GraphWorkflowService(
            instance.database, capability_packages=(preload_package(calls),),
            enabled_packages={**DEFAULT_PACKAGES, "test.preloads": "1"})) as reopened:
        assert values(reopened.get_session(target["workflow_session_id"])) == (3, 17)


def test_chat_initial_reroll_select_floor_fork_restore_structure_history(service):
    instance, calls = service
    graph = save_graph(instance)
    view = submit(instance, create(instance, graph), "first")
    round_id = chat(view)["rounds"][0]["round_id"]
    with closing(instance._store()) as store:
        with transaction(store._connection):
            frozen = read_chat_round_inputs(GraphRecordStore(store), view["workflow_session_id"], "chat", round_id)
            assert frozen["initial_compute_state"]["preloaded_items"] == {"structure": structure_entry(7)}
    view = reroll(instance, view, round_id)
    assert values(view) == (4, 27) and calls == [(2, 7), (2, 7)]
    candidate = chat(view)["rounds"][0]["candidates"][0]
    selected = WorkflowChatOperations(instance).select(
        view["workflow_session_id"], round_id=round_id, candidate_id=candidate["candidate_id"], **guards(view))
    assert values(selected) == (3, 17)
    second = submit(instance, selected, "second")
    assert values(second) == (6, 47) and calls[-1] == (3, 17)
    forked = WorkflowChatOperations(instance).fork_floor(
        second["workflow_session_id"], round_id=round_id, **guards(second))
    target = instance.get_session(forked["target_session_id"])
    assert values(target) == (3, 17) and len(chat(target)["rounds"]) == 1
    target = reroll(instance, target, round_id)
    assert values(target) == (6, 47) and calls[-1] == (2, 7)
    assert instance.get_session(second["workflow_session_id"]) == second


@pytest.mark.parametrize("defect", ["missing", "extra", "type", "cas"])
def test_loading_rejects_wrong_generic_keys_type_or_cas_without_writes(service, defect):
    instance, _ = service
    graph = save_graph(instance)
    completed = submit(instance, create(instance, graph), "one")
    state_value = checkpoint_history(instance, completed)["state"]
    before = raw_database(instance.database)
    with pytest.raises(ContractValidationError):
        with closing(instance._store()) as store:
            with transaction(store._connection):
                repo = GraphRecordStore(store)
                target = instance._initialize_session(
                    repo, graph, preloaded_values={"counter": variable(3), "structure": {"count": 17}})
                current = instance._objects(repo).current(target["workflow_session_id"])
                candidate = deepcopy(state_value)
                if defect == "missing":
                    candidate.pop("preloaded_items")
                elif defect == "extra":
                    candidate["preloaded_items"]["other"] = structure_entry(1)
                elif defect == "type":
                    candidate["preloaded_items"]["structure"]["schema_version"] = 2
                keys = {key for section in ("agent_contexts", "variables", "chat_contexts", "preloaded_items")
                        for key in candidate.get(section, {})}
                cas = {key: {field: current[key][field] for field in ("revision", "revision_id")}
                       for key in keys if key in current}
                if defect == "cas":
                    cas["structure"]["revision_id"] = str(uuid4())
                load_checkpoint_state_in_transaction(
                    repo, target, candidate, cas, types=instance.registry.data_types, registry=instance.registry)
    assert raw_database(instance.database) == before
