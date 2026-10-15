"""Logical chat values survive schema-4 collection, copy and target initialization."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from phase1_agent.chat_contracts import (
    CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION, append_chat_candidate,
    bind_chat_candidate_checkpoint, chat_context_data_type, empty_chat_context,
)
from phase1_agent.checkpoint_collection import (
    checkpoint_chat_bindings, collect_checkpoint_configuration, collect_checkpoint_state,
)
from phase1_agent.checkpoint_loading import load_checkpoint_state_in_transaction
from phase1_agent.checkpoint_repository import CheckpointRepository
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.host_sdk import TypeRegistry, WriteIntent
from phase1_agent.runtime_checkpoints import (
    CHECKPOINT_SCHEMA_VERSION, make_diff, materialize, remap_checkpoint_state_chat,
    remap_checkpoint_storage_chat, validate_checkpoint_state, validate_state_storage,
)
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.workflow_chat_runtime import save_chat_round_inputs
from test_checkpoint_collection import transaction
from test_checkpoint_fork_stage2_acceptance import _fork
from test_graph_application_receipt_custody import raw_database
from test_graph_service import create, save_definition_fixture, text_graph
from test_success_checkpoints import checkpoint_history


def identity():
    return str(uuid4())


def entry(value):
    return {"type_id": CHAT_CONTEXT_TYPE, "schema_version": CHAT_CONTEXT_SCHEMA_VERSION,
            "value": deepcopy(value)}


def chat_value(*, count=1, base=None, checkpoint_ids=None):
    value, round_id = empty_chat_context(), identity()
    for index in range(count):
        candidate_id = identity()
        value = append_chat_candidate(
            value, round_id=round_id, base_checkpoint_id=base,
            submitted_input="same input", candidate_id=candidate_id,
            messages=[{"message_id": identity(), "role": "assistant", "text": f"answer {index}"}],
        )
        if checkpoint_ids is not None:
            value = bind_chat_candidate_checkpoint(
                value, round_id=round_id, candidate_id=candidate_id,
                checkpoint_id=checkpoint_ids[index])
    return value


@pytest.fixture
def chat_service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "chat-checkpoints.sqlite")) as service:
        if service.registry.data_types.get(
                CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION, scope="session") is None:
            service.registry.data_types.register(chat_context_data_type())

        def reject_provider_execution(config, inputs, context):
            raise AssertionError("Checkpoint handling cannot execute its data registrar")

        service.registry.register(NodeDefinition(
            "test.checkpoint-chat", "1", "Independent chat provider", "Test",
            {}, {"type": "object", "additionalProperties": False},
            execution_class="non_working",
            provided_session_item_types=({
                "type_id": CHAT_CONTEXT_TYPE, "schema_version": CHAT_CONTEXT_SCHEMA_VERSION,
            },),
        ), reject_provider_execution)
        yield service


def chat_graph(service, *, count=2, writer=False):
    graph = text_graph(service.registry)
    graph["schema_version"] = 3
    for node in graph["nodes"]:
        node["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    for index in range(count):
        node_id = identity()
        graph["nodes"].append({
            "node_binding_id": node_id,
            "component_id": "test.checkpoint-chat", "component_version": "1",
            "title": f"Chat {index}", "position": {"x": index * 100, "y": 100},
            "config": {}, "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
        })
        graph["object_bindings"].append({
            "object_key": f"chat-{index}", "type_id": CHAT_CONTEXT_TYPE,
            "schema_version": CHAT_CONTEXT_SCHEMA_VERSION, "scope": "shared",
            "readers": [], "writers": [node_id] if writer else [],
            "owner_node_id": None, "registrar_node_id": node_id,
        })
    return graph


def state_for(service, repo, document, values):
    return {
        "configuration": collect_checkpoint_configuration(
            repo, document["workflow_definition_id"], document["revision"],
            registry=service.registry),
        "agent_contexts": {}, "variables": {},
        "chat_contexts": {key: entry(value) for key, value in values.items()},
    }


def pristine_target(service, repo, document):
    target = service._initialize_session(repo, document)
    current = SessionObjectStore(repo.store, service.registry.data_types).current(
        target["workflow_session_id"])
    cas = {
        key: {field: item[field] for field in ("revision", "revision_id")}
        for key, item in current.items() if item["type_id"] == CHAT_CONTEXT_TYPE
    }
    return target, current, cas


def seed_source_history(service, *, count=5):
    """Fixture accepted values and points without invoking a frontend or model node."""
    document = chat_graph(service)
    save_definition_fixture(service, document)
    point_ids, round_id = [identity() for _ in range(count)], identity()

    def seed(repo):
        session, _, cas = pristine_target(service, repo, document)
        save_chat_round_inputs(
            repo, session["workflow_session_id"], "chat-0", round_id,
            inputs={}, input_overrides={}, initial_compute_state={"agent_contexts": {}, "variables": {}})
        repository = CheckpointRepository(
            repo.store._connection, workflow_definition_id=document["workflow_definition_id"],
            workflow_session_id=session["workflow_session_id"], fixed_definition_revision=1)
        previous_state, previous_point, value = None, None, empty_chat_context()
        for index, point_id in enumerate(point_ids):
            candidate_id = identity()
            value = append_chat_candidate(
                value, round_id=round_id, base_checkpoint_id=None,
                submitted_input="same input", candidate_id=candidate_id,
                messages=[{"message_id": identity(), "role": "assistant", "text": f"answer {index}"}])
            value = bind_chat_candidate_checkpoint(
                value, round_id=round_id, candidate_id=candidate_id, checkpoint_id=point_id)
            if index == count - 1 and count > 1:
                value["rounds"][0]["selected_candidate_id"] = value["rounds"][0][
                    "candidates"][1]["candidate_id"]
            state = state_for(
                service, repo, document, {"chat-0": value, "chat-1": empty_chat_context()})
            storage = ({"mode": "full", "state": state} if previous_state is None else {
                "mode": "diff", "base_checkpoint_id": point_ids[0],
                **make_diff(previous_state, state),
            })
            point = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION, "checkpoint_id": point_id,
                **repository.owner,
                "parent_checkpoint_id": None if previous_point is None else previous_point,
                "state_storage": storage,
            }
            repository.insert(point)
            previous_state, previous_point = state, point_id
        load_checkpoint_state_in_transaction(
            repo, session, previous_state, cas,
            types=service.registry.data_types, registry=service.registry)
        service._commit(repo, session, source={"kind": "session_seed", "source": None}, history=[])
        return service._view(repo, session["workflow_session_id"])

    source = service._change("test.chat.seed", identity(), {}, seed)
    return source, point_ids


def test_collection_uses_exact_admission_and_includes_every_empty_chat_item(chat_service):
    service = chat_service
    source = create(service, chat_graph(service))
    with closing(service._store()) as store:
        with transaction(store._connection):
            repo = GraphRecordStore(store)
            document = service._document(repo, source["workflow_definition_id"], 1)
            state = collect_checkpoint_state(
                repo, source["workflow_definition_id"], 1, source["workflow_session_id"],
                types=service.registry.data_types, registry=service.registry)
            assert state["chat_contexts"] == {
                "chat-0": entry(empty_chat_context()), "chat-1": entry(empty_chat_context())}
            types = TypeRegistry()
            types.register(replace(
                service.registry.data_types.get(
                    CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION, scope="session"),
                checkpoint_admission=False))
            assert checkpoint_chat_bindings(document, types=types, registry=service.registry) == {}


def test_all_candidates_are_current_data_layer_values_without_a_frontend_consumer(chat_service):
    service = chat_service
    source, point_ids = seed_source_history(service)
    other = service.create_session(
        source["workflow_definition_id"], source["definition_revision"], idempotency_key=identity())
    history = checkpoint_history(service, source)
    current = source["objects"]["chat-0"]["value"]
    assert history["state"]["chat_contexts"]["chat-0"] == entry(current)
    assert history["collected"] == history["state"]
    round_value = current["rounds"][0]
    assert [item["result_checkpoint_id"] for item in round_value["candidates"]] == point_ids
    assert [item["messages"][0]["text"] for item in round_value["candidates"]] == [
        f"answer {index}" for index in range(5)]
    assert round_value["selected_candidate_id"] == round_value["candidates"][1]["candidate_id"]
    assert source["objects"]["chat-1"]["value"] == empty_chat_context()
    assert all(item["value"] == empty_chat_context() for item in other["objects"].values())
    with closing(service._store()) as store:
        assert GraphRecordStore(store).rows(
            "workflow_output", workflow_session_id=source["workflow_session_id"]) == []
        binding = source["objects"]["chat-0"]["binding"]
        assert binding["readers"] == binding["writers"] == []
        with pytest.raises(ContractValidationError) as caught:
            SessionObjectStore(store, service.registry.data_types).read(
                source["workflow_session_id"], "chat-0", node_id=binding["registrar_node_id"])
        assert caught.value.reason_code == "session_object_access_denied"


def test_default_data_boundary_denies_unproven_write_even_for_an_authorized_node(chat_service):
    service = chat_service
    source = create(service, chat_graph(service, writer=True))
    default_types = TypeRegistry()
    default_types.register(chat_context_data_type())
    with closing(service._store()) as store:
        with transaction(store._connection):
            objects = SessionObjectStore(store, default_types)
            before = objects.current(source["workflow_session_id"])
            current = before["chat-0"]
            with pytest.raises(ContractValidationError) as caught:
                objects.apply(
                    source["workflow_session_id"],
                    node_id=current["binding"]["registrar_node_id"],
                    writes=[WriteIntent(
                        "chat-0", current["revision"], identity(), chat_value()).to_dict()])
            assert caught.value.reason_code == "chat_write_evidence_required"
            assert objects.current(source["workflow_session_id"]) == before
            assert store._connection.execute(
                "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
                (source["workflow_session_id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("invalid", ["registrar", "missing_head", "deleted_head"])
def test_collection_rejects_wrong_provider_or_missing_admitted_chat(chat_service, invalid):
    service = chat_service
    source = create(service, chat_graph(service))
    with closing(service._store()) as store:
        with transaction(store._connection):
            repo = GraphRecordStore(store)
            document = service._document(repo, source["workflow_definition_id"], 1)
            if invalid == "registrar":
                changed = deepcopy(document)
                changed["object_bindings"][0]["registrar_node_id"] = changed["nodes"][0]["node_binding_id"]
                with pytest.raises(ContractValidationError) as caught:
                    checkpoint_chat_bindings(
                        changed, types=service.registry.data_types, registry=service.registry)
                assert caught.value.reason_code == "checkpoint_context_registration_invalid"
                return
            if invalid == "missing_head":
                store._connection.execute(
                    "DELETE FROM session_object_bindings WHERE session_id=? AND object_key='chat-0'",
                    (source["workflow_session_id"],))
            else:
                current = SessionObjectStore(store, service.registry.data_types).current(
                    source["workflow_session_id"])["chat-0"]
                store._connection.execute(
                    "UPDATE session_object_values SET deleted=1,value='null' WHERE revision_id=?",
                    (current["revision_id"],))
            with pytest.raises(ContractValidationError) as caught:
                collect_checkpoint_state(
                    repo, source["workflow_definition_id"], 1, source["workflow_session_id"],
                    types=service.registry.data_types, registry=service.registry)
            assert caught.value.reason_code == "checkpoint_chat_state_unavailable"


@pytest.mark.parametrize("failure", ["missing", "extra", "wrong_type", "cas"])
def test_loading_requires_complete_chat_set_exact_types_and_pristine_heads(chat_service, failure):
    service = chat_service
    source = create(service, chat_graph(service))
    with closing(service._store()) as store:
        with transaction(store._connection):
            repo = GraphRecordStore(store)
            document = service._document(repo, source["workflow_definition_id"], 1)
            target, before, cas = pristine_target(service, repo, document)
            state = state_for(service, repo, document, {
                "chat-0": chat_value(), "chat-1": empty_chat_context()})
            if failure == "missing":
                del state["chat_contexts"]["chat-1"]
            elif failure == "extra":
                state["chat_contexts"]["extra"] = entry(empty_chat_context())
            elif failure == "wrong_type":
                state["chat_contexts"]["chat-0"]["schema_version"] = 2
            else:
                cas["chat-0"]["revision_id"] = identity()
            with pytest.raises(ContractValidationError) as caught:
                load_checkpoint_state_in_transaction(
                    repo, target, state, cas,
                    types=service.registry.data_types, registry=service.registry)
            assert caught.value.reason_code == {
                "missing": "checkpoint_target_chat_binding_mismatch",
                "extra": "checkpoint_target_chat_binding_mismatch",
                "wrong_type": "runtime_checkpoint_invalid", "cas": "checkpoint_target_cas_stale",
            }[failure]
            assert SessionObjectStore(store, service.registry.data_types).current(
                target["workflow_session_id"]) == before
            assert store._connection.execute(
                "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
                (target["workflow_session_id"],)).fetchone()[0] == 0


def test_fork_copies_all_candidates_remaps_points_and_needs_no_artifact_history(chat_service):
    service = chat_service
    source, point_ids = seed_source_history(service)
    source_history = checkpoint_history(service, source)
    assert source_history["state"] == source_history["collected"]
    forks = [_fork(service, source, point_ids[-1]) for _ in range(2)]
    targets = [service.get_session(result["target_session_id"]) for result in forks]
    for target, result in zip(targets, forks):
        history = checkpoint_history(service, target)
        target_points = [point["checkpoint_id"] for point in history["path"]]
        assert len(target_points) == 5 and set(target_points).isdisjoint(point_ids)
        assert target_points[-1] == result["target_checkpoint_id"]
        assert history["state"] == history["collected"]
        expected = remap_checkpoint_state_chat(source_history["state"], dict(zip(point_ids, target_points)))
        assert history["state"] == expected
        value = target["objects"]["chat-0"]["value"]["rounds"][0]
        assert len(value["candidates"]) == 5
        assert value["selected_candidate_id"] == value["candidates"][1]["candidate_id"]
        assert [candidate["result_checkpoint_id"] for candidate in value["candidates"]] == target_points
        assert target["objects"]["chat-1"]["value"] == empty_chat_context()
        assert target["objects"]["chat-0"]["revision_id"] != source["objects"]["chat-0"]["revision_id"]
        with closing(service._store()) as store:
            assert store._connection.execute(
                "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
                (target["workflow_session_id"],)).fetchone()[0] == 0
            assert GraphRecordStore(store).rows(
                "workflow_output", workflow_session_id=target["workflow_session_id"]) == []
    assert targets[0]["objects"]["chat-0"]["value"] != targets[1]["objects"]["chat-0"]["value"]
    assert service.get_session(source["workflow_session_id"]) == source
    assert checkpoint_history(service, source) == source_history

    deleted = service.delete_session(
        source["workflow_session_id"], expected_revision=source["revision"], idempotency_key=identity())
    assert deleted["deleted"]
    for target in targets:
        assert checkpoint_history(service, target)["state"] == checkpoint_history(service, target)["collected"]


@pytest.mark.parametrize("failure", ["checkpoint_prefix", "second_chat", "after_load"])
def test_chat_fork_failure_rolls_back_the_whole_target(chat_service, monkeypatch, failure):
    service = chat_service
    source, point_ids = seed_source_history(service)
    baseline = raw_database(service.database)
    source_id = source["workflow_session_id"]
    if failure == "checkpoint_prefix":
        original = CheckpointRepository.insert

        def fail(repository, point):
            result = original(repository, point)
            if repository.owner["workflow_session_id"] != source_id:
                raise OSError("injected copied checkpoint failure")
            return result

        monkeypatch.setattr(CheckpointRepository, "insert", fail)
    elif failure == "second_chat":
        original, written = SessionObjectStore._put_value, []

        def fail(objects, binding, value, **kwargs):
            result = original(objects, binding, value, **kwargs)
            if binding.type_id == CHAT_CONTEXT_TYPE:
                written.append(binding.object_key)
                # Two defaults are initialized first, followed by the copied pair.
                if len(written) == 4:
                    raise OSError("injected second chat load failure")
            return result

        monkeypatch.setattr(SessionObjectStore, "_put_value", fail)
    else:
        original = SessionObjectStore._initialize_checkpoint_values

        def fail(objects, sid, *args, **kwargs):
            original(objects, sid, *args, **kwargs)
            raise OSError("injected after chat load failure")

        monkeypatch.setattr(SessionObjectStore, "_initialize_checkpoint_values", fail)
    with pytest.raises(OSError, match="injected"):
        _fork(service, source, point_ids[-1])
    assert raw_database(service.database) == baseline
    assert service.get_session(source_id) == source


def test_chat_diff_add_delete_and_reference_only_remapping_are_detached():
    workflow_id, session_id = identity(), identity()
    root_id, target_id = identity(), identity()
    before = {
        "configuration": {
            "workflow_definition_id": workflow_id, "fixed_definition_revision": 1, "nodes": {}},
        "agent_contexts": {}, "variables": {},
        "chat_contexts": {"removed": entry(empty_chat_context())},
    }
    after = deepcopy(before)
    after["chat_contexts"] = {"added": entry(chat_value(base=root_id, checkpoint_ids=[root_id]))}
    root = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION, "checkpoint_id": root_id,
        "workflow_definition_id": workflow_id, "workflow_session_id": session_id,
        "fixed_definition_revision": 1, "parent_checkpoint_id": None,
        "state_storage": {"mode": "full", "state": before},
    }
    child = {
        **root, "checkpoint_id": identity(), "parent_checkpoint_id": root_id,
        "state_storage": {"mode": "diff", "base_checkpoint_id": root_id, **make_diff(before, after)},
    }
    assert child["state_storage"]["delete"]["chat_contexts"] == ["removed"]
    records = {point["checkpoint_id"]: point for point in (root, child)}
    assert materialize(child["checkpoint_id"], records.get) == after
    remapped = remap_checkpoint_storage_chat(child["state_storage"], {root_id: target_id})
    assert remapped["base_checkpoint_id"] == root_id
    candidate = remapped["upsert"]["chat_contexts"]["added"]["value"]["rounds"][0]
    assert candidate["base_checkpoint_id"] == target_id
    assert candidate["candidates"][0]["result_checkpoint_id"] == target_id
    assert child["state_storage"]["upsert"]["chat_contexts"] == after["chat_contexts"]
    full = remap_checkpoint_storage_chat(
        {"mode": "full", "state": after}, {root_id: target_id})
    assert full["state"] == remap_checkpoint_state_chat(after, {root_id: target_id})
    invalid = deepcopy(after)
    invalid["variables"]["added"] = {
        "type_id": "workflow.variable", "schema_version": 1,
        "value": {"registered": False, "name": None, "value_type": None,
                  "assigned": False, "value": None},
    }
    with pytest.raises(ContractValidationError):
        validate_checkpoint_state(invalid)
    with pytest.raises(ContractValidationError):
        validate_state_storage({
            **child["state_storage"],
            "delete": {**child["state_storage"]["delete"], "chat_contexts": ["added"]},
        })
