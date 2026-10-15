"""Variable logical states traverse success checkpoints and independent forks."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_collection import collect_checkpoint_state, checkpoint_variable_bindings
from phase1_agent.checkpoint_loading import load_checkpoint_state_in_transaction
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.host_sdk import TypeRegistry, WriteIntent
from phase1_agent.runtime_checkpoints import (
    make_diff, materialize, validate_checkpoint_state, validate_state_storage,
)
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.variable_contract import (
    VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION, empty_variable_value,
)
from test_checkpoint_collection import transaction
from test_checkpoint_fork_stage2_acceptance import _fork
from test_graph_application_receipt_custody import raw_database
from test_graph_service import create, run as graph_run, text_graph
from test_success_checkpoints import checkpoint_history


def variable_value(name="score", value_type="integer", *, assigned=True, value=0):
    return {
        "registered": True, "name": name, "value_type": value_type,
        "assigned": assigned, "value": value if assigned else None,
    }


def entry(value):
    return {"type_id": VARIABLE_TYPE, "schema_version": VARIABLE_SCHEMA_VERSION,
            "value": deepcopy(value)}


@pytest.fixture
def variable_service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "variable-checkpoints.sqlite")) as service:
        def reject_provider_execution(config, inputs, context):
            raise AssertionError("Checkpoint collection and loading cannot execute a provider")

        service.registry.register(NodeDefinition(
            "test.checkpoint-variable", "1", "Variable provider", "Test",
            {}, {"type": "object", "additionalProperties": False},
            execution_class="non_working",
            provided_session_item_types=({
                "type_id": VARIABLE_TYPE, "schema_version": VARIABLE_SCHEMA_VERSION,
            },),
        ), reject_provider_execution)
        yield service


def variable_graph(service, *, count=2, acl=True):
    graph = text_graph(service.registry)
    graph["schema_version"] = 3
    for node in graph["nodes"]:
        node["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    for index in range(count):
        identity = str(uuid4())
        graph["nodes"].append({
            "node_binding_id": identity, "component_id": "test.checkpoint-variable",
            "component_version": "1", "title": f"Variable {index}",
            "position": {"x": index * 100, "y": 100}, "config": {},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
        })
        graph["object_bindings"].append({
            "object_key": f"variable-{index}", "type_id": VARIABLE_TYPE,
            "schema_version": VARIABLE_SCHEMA_VERSION, "scope": "shared",
            "readers": [identity] if acl else [], "writers": [identity] if acl else [],
            "owner_node_id": None, "registrar_node_id": identity,
        })
    return graph


def put_values(service, session, values):
    """Submit explicit accepted object writes through the existing data boundary."""
    with closing(service._store()) as store:
        with transaction(store._connection):
            objects = SessionObjectStore(store, service.registry.data_types)
            current = objects.current(session["workflow_session_id"])
            for key, value in values.items():
                objects.apply(
                    session["workflow_session_id"],
                    node_id=current[key]["binding"]["registrar_node_id"],
                    writes=[WriteIntent(key, current[key]["revision"], str(uuid4()), value).to_dict()],
                )
    return service.get_session(session["workflow_session_id"])


def variable_values(session):
    return {key: item["value"] for key, item in session["objects"].items()
            if item["type_id"] == VARIABLE_TYPE}


def assert_no_target_write_receipts(service, session):
    with closing(service._store()) as store:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
            (session["workflow_session_id"],),
        ).fetchone()[0] == 0


def test_success_full_diff_and_repeated_fork_preserve_independent_sessions_and_items(variable_service):
    service = variable_service
    source = create(service, variable_graph(service))
    other = service.create_session(
        source["workflow_definition_id"], source["definition_revision"],
        idempotency_key=str(uuid4()),
    )
    source = graph_run(service, source)
    first = checkpoint_history(service, source)
    frozen_root = deepcopy(first["path"][0])
    assert frozen_root["schema_version"] == 4
    assert frozen_root["state_storage"]["mode"] == "full"
    assert first["state"]["variables"] == {
        "variable-0": entry(empty_variable_value()), "variable-1": entry(empty_variable_value()),
    }

    unassigned = variable_value(assigned=False)
    source = put_values(service, source, {"variable-0": unassigned})
    source = graph_run(service, source)
    second = checkpoint_history(service, source)
    assert second["path"][0] == frozen_root
    assert second["path"][-1]["state_storage"]["mode"] == "diff"
    assert second["path"][-1]["state_storage"]["upsert"]["variables"] == {
        "variable-0": entry(unassigned),
    }
    selected_point = second["latest"]["checkpoint_id"]
    selected_state = deepcopy(second["state"])

    zero = variable_value(value=0)
    false = variable_value("switch", "boolean", value=False)
    source = put_values(service, source, {"variable-0": zero, "variable-1": false})
    source = graph_run(service, source)
    third = checkpoint_history(service, source)
    frozen_source = deepcopy(source)
    frozen_history = deepcopy(third)
    assert third["state"]["variables"] == {"variable-0": entry(zero), "variable-1": entry(false)}
    assert variable_values(service.get_session(other["workflow_session_id"])) == {
        "variable-0": empty_variable_value(), "variable-1": empty_variable_value(),
    }

    forks = [_fork(service, source, selected_point) for _ in range(2)]
    assert forks[0]["target_session_id"] != forks[1]["target_session_id"]
    targets = [service.get_session(result["target_session_id"]) for result in forks]
    for target in targets:
        history = checkpoint_history(service, target)
        assert len(history["path"]) == 2
        assert history["state"] == selected_state == history["collected"]
        assert variable_values(target) == {"variable-0": unassigned, "variable-1": empty_variable_value()}
        assert_no_target_write_receipts(service, target)
        assert target["objects"]["variable-0"]["revision_id"] != (
            source["objects"]["variable-0"]["revision_id"])

    empty_string = variable_value("text", "string", value="")
    changed_target = put_values(service, targets[0], {"variable-1": empty_string})
    changed_target = graph_run(service, changed_target)
    changed_history = checkpoint_history(service, changed_target)
    assert changed_history["state"]["variables"]["variable-1"] == entry(empty_string)
    again = _fork(service, changed_target, changed_history["latest"]["checkpoint_id"])
    descendant = service.get_session(again["target_session_id"])
    assert variable_values(descendant) == variable_values(changed_target)
    assert_no_target_write_receipts(service, descendant)
    assert service.get_session(targets[1]["workflow_session_id"]) == targets[1]
    assert service.get_session(source["workflow_session_id"]) == frozen_source
    assert checkpoint_history(service, frozen_source) == frozen_history


@pytest.mark.parametrize(("value_type", "value"), [
    ("integer", 0), ("number", 0.5), ("boolean", False), ("string", ""),
])
def test_each_scalar_falsey_or_fractional_value_survives_selected_full_point(
        variable_service, value_type, value):
    service = variable_service
    source = create(service, variable_graph(service))
    saved = variable_value("target", value_type, value=value)
    source = put_values(service, source, {"variable-0": saved})
    source = graph_run(service, source)
    history = checkpoint_history(service, source)
    assert history["state"]["variables"]["variable-0"] == entry(saved)
    fork = _fork(service, source, history["latest"]["checkpoint_id"])
    target = service.get_session(fork["target_session_id"])
    assert target["objects"]["variable-0"]["value"] == saved
    assert target["objects"]["variable-1"]["value"] == empty_variable_value()
    assert_no_target_write_receipts(service, target)


def test_success_checkpoint_fault_rolls_back_point_and_retries_saved_variable_state(
        variable_service, monkeypatch):
    service = variable_service
    source = create(service, variable_graph(service))
    source = put_values(service, source, {
        "variable-0": variable_value(value=0),
        "variable-1": variable_value("switch", "boolean", value=False),
    })
    values = variable_values(source)
    fired = []

    def fail_once(point):
        if point == "after_checkpoint_write" and not fired:
            fired.append(point)
            raise OSError("injected variable success checkpoint failure")

    monkeypatch.setattr(service, "_fault_injector", fail_once)
    pending = graph_run(service, source)
    assert pending["status"] == "archive_failed"
    assert fired == ["after_checkpoint_write"]
    assert pending["head_commit_id"] == source["head_commit_id"]
    assert pending["head_revision"] == source["head_revision"]
    assert variable_values(pending) == values
    assert checkpoint_history(service, pending)["latest"] is None
    chain_id = pending["chains"][-1]["chain_run_id"]
    before = service.get_run(pending["workflow_session_id"], chain_id)
    retry = service.control(
        pending["workflow_session_id"], action="retry_archive",
        expected_revision=pending["revision"], idempotency_key=str(uuid4()),
    )
    service.wait(chain_id)
    final = service.get_session(retry["workflow_session_id"])
    assert final["status"] == "succeeded"
    history = checkpoint_history(service, final)
    assert len(history["path"]) == 1
    assert history["state"]["variables"] == {key: entry(value) for key, value in values.items()}
    after = service.get_run(final["workflow_session_id"], chain_id)
    assert after["node_runs"] == before["node_runs"]
    assert after["outputs"] == before["outputs"]
    assert variable_values(final) == values


def test_checkpoint_privilege_needs_no_consumer_acl_and_admission_stays_default_deny(variable_service):
    service = variable_service
    source = create(service, variable_graph(service, acl=False))
    source = graph_run(service, source)
    history = checkpoint_history(service, source)
    assert set(history["state"]["variables"]) == {"variable-0", "variable-1"}
    result = _fork(service, source, history["latest"]["checkpoint_id"])
    target = service.get_session(result["target_session_id"])
    assert variable_values(target) == variable_values(source)
    types = TypeRegistry()
    # An exact registered type is insufficient without its explicit admission.
    types.register(replace(
        service.registry.data_types.get(VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION, scope="session"),
        checkpoint_admission=False,
    ))
    document = service.get_definition(source["workflow_definition_id"])
    assert checkpoint_variable_bindings(document, types=types, registry=service.registry) == {}


@pytest.mark.parametrize("invalid", ["registrar", "binding", "missing_head", "deleted_head"])
def test_collection_rejects_wrong_origin_or_incomplete_current_state(variable_service, invalid):
    service = variable_service
    source = create(service, variable_graph(service))
    with closing(service._store()) as store:
        with transaction(store._connection):
            repo = GraphRecordStore(store)
            document = service._document(repo, source["workflow_definition_id"], source["definition_revision"])
            if invalid == "registrar":
                changed = deepcopy(document)
                changed["object_bindings"][0]["registrar_node_id"] = changed["nodes"][0]["node_binding_id"]
                with pytest.raises(ContractValidationError) as error:
                    checkpoint_variable_bindings(
                        changed, types=service.registry.data_types, registry=service.registry)
                assert error.value.reason_code == "checkpoint_context_registration_invalid"
                return
            if invalid == "binding":
                store._connection.execute(
                    "UPDATE session_object_bindings SET binding=json_set(binding,'$.readers',json('[]')) "
                    "WHERE session_id=? AND object_key='variable-0'", (source["workflow_session_id"],),
                )
            elif invalid == "missing_head":
                store._connection.execute(
                    "DELETE FROM session_object_bindings WHERE session_id=? AND object_key='variable-0'",
                    (source["workflow_session_id"],),
                )
            else:
                head = SessionObjectStore(store, service.registry.data_types).current(
                    source["workflow_session_id"])["variable-0"]["revision_id"]
                store._connection.execute(
                    "UPDATE session_object_values SET deleted=1,value='null' WHERE revision_id=?", (head,),
                )
            with pytest.raises(ContractValidationError) as error:
                collect_checkpoint_state(
                    repo, source["workflow_definition_id"], source["definition_revision"],
                    source["workflow_session_id"],
                    types=service.registry.data_types, registry=service.registry,
                )
            assert error.value.reason_code == (
                "session_object_binding_mismatch" if invalid == "binding"
                else "checkpoint_variable_state_unavailable")


@pytest.mark.parametrize("failure", ["checkpoint_prefix", "second_variable", "after_load"])
def test_variable_fork_failure_rolls_back_all_target_records_and_receipts(
        variable_service, monkeypatch, failure):
    service = variable_service
    source = create(service, variable_graph(service))
    source = put_values(service, source, {
        "variable-0": variable_value(value=0),
        "variable-1": variable_value("switch", "boolean", value=False),
    })
    source = graph_run(service, source)
    history = checkpoint_history(service, source)
    source_id = source["workflow_session_id"]
    baseline = raw_database(service.database)
    if failure == "checkpoint_prefix":
        from phase1_agent.checkpoint_repository import CheckpointRepository
        original = CheckpointRepository.insert

        def fail(repository, checkpoint):
            result = original(repository, checkpoint)
            if repository.owner["workflow_session_id"] != source_id:
                raise OSError("injected checkpoint prefix failure")
            return result

        monkeypatch.setattr(CheckpointRepository, "insert", fail)
    elif failure == "second_variable":
        original = SessionObjectStore._put_value
        loaded = []

        def fail(objects, binding, value, **kwargs):
            result = original(objects, binding, value, **kwargs)
            if binding.type_id == VARIABLE_TYPE and value["registered"]:
                loaded.append(binding.object_key)
                if len(loaded) == 2:
                    raise OSError("injected second variable failure")
            return result

        monkeypatch.setattr(SessionObjectStore, "_put_value", fail)
    else:
        original = SessionObjectStore._initialize_checkpoint_values

        def fail(objects, sid, *args, **kwargs):
            original(objects, sid, *args, **kwargs)
            raise OSError("injected after load failure")

        monkeypatch.setattr(SessionObjectStore, "_initialize_checkpoint_values", fail)
    with pytest.raises(OSError, match="injected"):
        _fork(service, source, history["latest"]["checkpoint_id"])
    assert raw_database(service.database) == baseline
    assert service.get_session(source_id) == source
    assert checkpoint_history(service, source) == history


@pytest.mark.parametrize("failure", ["missing", "extra", "wrong_type", "cas"])
def test_loading_requires_complete_exact_variable_set_and_pristine_cas(variable_service, failure):
    service = variable_service
    source = create(service, variable_graph(service))
    source = graph_run(service, source)
    state = checkpoint_history(service, source)["state"]
    with closing(service._store()) as store:
        with transaction(store._connection):
            repo = GraphRecordStore(store)
            document = service._document(repo, source["workflow_definition_id"], source["definition_revision"])
            target = service._initialize_session(repo, document)
            objects = SessionObjectStore(store, service.registry.data_types)
            before = objects.current(target["workflow_session_id"])
            cas = {
                key: {field: item[field] for field in ("revision", "revision_id")}
                for key, item in before.items() if item["type_id"] == VARIABLE_TYPE
            }
            changed = deepcopy(state)
            if failure == "missing":
                del changed["variables"]["variable-0"]
            elif failure == "extra":
                changed["variables"]["other"] = entry(empty_variable_value())
            elif failure == "wrong_type":
                changed["variables"]["variable-0"]["schema_version"] = 2
            else:
                cas["variable-0"]["revision_id"] = str(uuid4())
            with pytest.raises(ContractValidationError) as error:
                load_checkpoint_state_in_transaction(
                    repo, target, changed, cas,
                    types=service.registry.data_types, registry=service.registry,
                )
            assert error.value.reason_code == {
                "missing": "checkpoint_target_variable_binding_mismatch",
                "extra": "checkpoint_target_variable_binding_mismatch",
                "wrong_type": "runtime_checkpoint_invalid",
                "cas": "checkpoint_target_cas_stale",
            }[failure]
            assert objects.current(target["workflow_session_id"]) == before
            assert store._connection.execute(
                "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
                (target["workflow_session_id"],),
            ).fetchone()[0] == 0


def test_variable_diff_add_delete_and_shape_validation():
    workflow_id, session_id = str(uuid4()), str(uuid4())
    before = {
        "configuration": {
            "workflow_definition_id": workflow_id, "fixed_definition_revision": 1, "nodes": {},
        },
        "agent_contexts": {},
        "variables": {"old": entry(variable_value(assigned=False))},
        "chat_contexts": {},
    }
    after = deepcopy(before)
    after["variables"] = {"new": entry(variable_value("toggle", "boolean", value=False))}
    root = {
        "schema_version": 4, "checkpoint_id": str(uuid4()),
        "workflow_definition_id": workflow_id, "workflow_session_id": session_id,
        "fixed_definition_revision": 1, "parent_checkpoint_id": None,
        "state_storage": {"mode": "full", "state": before},
    }
    child = {
        **root, "checkpoint_id": str(uuid4()), "parent_checkpoint_id": root["checkpoint_id"],
        "state_storage": {
            "mode": "diff", "base_checkpoint_id": root["checkpoint_id"], **make_diff(before, after),
        },
    }
    assert child["state_storage"]["delete"]["variables"] == ["old"]
    assert child["state_storage"]["upsert"]["variables"] == after["variables"]
    records = {point["checkpoint_id"]: point for point in (root, child)}
    assert materialize(child["checkpoint_id"], records.get) == after
    del before["variables"]
    with pytest.raises(ContractValidationError):
        validate_checkpoint_state(before)


@pytest.mark.parametrize("invalid", [
    "extra_envelope", "type", "version_bool", "shell_assignment",
    "unassigned_value", "boolean_as_integer", "partition_overlap",
])
def test_variable_logical_state_rejects_inexact_or_ambiguous_values(invalid):
    value = empty_variable_value()
    state = {
        "configuration": {
            "workflow_definition_id": str(uuid4()), "fixed_definition_revision": 1, "nodes": {},
        },
        "agent_contexts": {}, "variables": {"variable": entry(value)}, "chat_contexts": {},
    }
    stored = state["variables"]["variable"]
    if invalid == "extra_envelope":
        stored["revision_id"] = str(uuid4())
    elif invalid == "type":
        stored["type_id"] = "another.variable"
    elif invalid == "version_bool":
        stored["schema_version"] = True
    elif invalid == "shell_assignment":
        stored["value"]["assigned"] = True
    elif invalid == "unassigned_value":
        stored["value"] = variable_value(assigned=False)
        stored["value"]["value"] = 0
    elif invalid == "boolean_as_integer":
        stored["value"] = variable_value("switch", "boolean", value=0)
    else:
        state["agent_contexts"]["variable"] = {
            "value": {"messages": [], "layout": [], "accepted_delta_ids": [], "generation": 0},
        }
        with pytest.raises(ContractValidationError):
            validate_state_storage({
                "mode": "diff", "base_checkpoint_id": str(uuid4()),
                "upsert": {"agent_contexts": state["agent_contexts"],
                           "variables": state["variables"], "chat_contexts": {}},
                "delete": {"agent_contexts": [], "variables": [], "chat_contexts": []},
            })
    with pytest.raises(ContractValidationError):
        validate_checkpoint_state(state)
