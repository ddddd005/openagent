"""Real workflow execution, candidate computation and data-layer persistence."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.variable_contract import VARIABLE_TYPE
from phase1_agent.workflow_chat_operations import WorkflowChatOperations

from test_graph_service import document, edge, node
from test_success_checkpoints import checkpoint_history


def graph_fixture(service):
    calls = []

    def compute(config, inputs, context):
        record = context.object_read(config["object_key"])
        calls.append(record["value"]["value"])
        value = {**record["value"], "value": record["value"]["value"] + len(calls)}
        context.object_write(config["object_key"], value, expected_revision=record["revision"])
        return {"output": text_content(inputs["input"]["text"] + ":" + str(value["value"]))}

    service.registry.register(NodeDefinition(
        "test.chat-compute", "1", "Compute", "Test", {"object_key": "counter"},
        {"type": "object", "properties": {"object_key": {"type": "string"}},
         "required": ["object_key"], "additionalProperties": False},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        capabilities=("objects:read", "objects:write"),
        object_accesses=({"config_field": "object_key", "multiple": False, "access": "read_write",
                          "type_id": VARIABLE_TYPE, "schema_version": 1},),
        provided_session_item_types=({"type_id": VARIABLE_TYPE, "schema_version": 1},),
    ), compute)
    source = node(service.registry, "tools.editable-input", 55101, text="draft")
    calc = node(service.registry, "test.chat-compute", 55102)
    frontend = node(service.registry, "frontend.node", 55103, object_key="chat", role="assistant",
                    input_node_id=source["node_binding_id"])
    frontend["component_version"] = "2"
    graph = document([source, calc, frontend], [
        edge(source, calc, 55101), edge(calc, frontend, 55102, target_port="content"),
    ])
    graph.update(
        schema_version=3, package_lock=list(service.registry.execution_package_lock),
        object_bindings=[
            ObjectBinding(
                "counter", VARIABLE_TYPE, 1, "shared", readers=(calc["node_binding_id"],),
                writers=(calc["node_binding_id"],), registrar_node_id=calc["node_binding_id"],
                default_value={"registered": True, "name": "counter", "value_type": "integer",
                               "assigned": True, "value": 0},
            ).to_dict(),
            ObjectBinding(
                "chat", CHAT_CONTEXT_TYPE, 1, "shared", readers=(frontend["node_binding_id"],),
                writers=(frontend["node_binding_id"],), registrar_node_id=frontend["node_binding_id"],
            ).to_dict(),
        ],
    )
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"]).definition.to_dict()["default_configuration_policy"])
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))["document"]
    session = service.create_session(saved["workflow_definition_id"], saved["revision"],
                                     idempotency_key=str(uuid4()))
    return saved, session, calls, source, frontend


def guards(view):
    return {
        "object_key": "chat", "expected_session_revision": view["revision"],
        "expected_data_revision": view["data_revision"], "expected_head_revision": view["head_revision"],
        "expected_object_revision": view["objects"]["chat"]["revision"],
        "idempotency_key": str(uuid4()),
    }


def settle(service, started):
    service.wait(started["active_chain_run_id"])
    view = service.get_session(started["workflow_session_id"])
    assert view["status"] == "succeeded", view["chains"]
    return view


def submit(service, view, text):
    return settle(service, WorkflowChatOperations(service).submit(
        view["workflow_session_id"], submitted_input=text, **guards(view)))


def reroll(service, view, round_id):
    return settle(service, WorkflowChatOperations(service).reroll(
        view["workflow_session_id"], round_id=round_id, **guards(view)))


def chat(view):
    return view["objects"]["chat"]["value"]


def counter(view):
    return view["objects"]["counter"]["value"]["value"]


def test_five_rolls_keep_fixed_initial_basis_and_store_real_success_points(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "five.sqlite")) as service:
        graph, view, calls, source, frontend = graph_fixture(service)
        view = submit(service, view, "original")
        round_id = chat(view)["rounds"][0]["round_id"]
        for _ in range(4):
            view = reroll(service, view, round_id)
        floor = chat(view)["rounds"][0]
        assert calls == [0] * 5
        assert floor["submitted_input"] == "original" and floor["base_checkpoint_id"] is None
        assert [item["messages"][0]["text"] for item in floor["candidates"]] == [
            "original:" + str(index) for index in range(1, 6)]
        history = checkpoint_history(service, view)
        points = {point["checkpoint_id"] for point in history["path"]}
        assert len(points) == 5
        assert {item["result_checkpoint_id"] for item in floor["candidates"]} == points
        assert history["state"] == history["collected"]
        snapshot = service.get_cached_frontend_node_snapshot(
            graph["workflow_definition_id"], graph["revision"], frontend["node_binding_id"],
            view["workflow_session_id"])
        assert snapshot["chat"] == chat(view)
        run = service.get_run(view["workflow_session_id"], view["selected_chain_run_id"])
        producer = next(item for item in run["node_runs"]
                        if item["node_binding_id"] == frontend["node_binding_id"])
        with closing(service._store()) as store:
            from phase1_agent.session_objects import SessionObjectStore
            detail = SessionObjectStore(store, service.registry.data_types)._resolve_write_artifact(
                view["workflow_session_id"],
                {"scope": "artifact", "output_id": producer["output_refs"]["proof"]})
            assert detail["value"]["schema_version"] == 2 and "chat_append_context" not in detail
        configuration = service.get_definition(graph["workflow_definition_id"])
        assert next(item for item in configuration["nodes"]
                    if item["node_binding_id"] == source["node_binding_id"])["config"]["text"] == "draft"


def test_select_old_candidate_controls_next_round_and_backend_locks_old_floor(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "select.sqlite")) as service:
        _, view, calls, _, _ = graph_fixture(service)
        view = submit(service, view, "first")
        round_id = chat(view)["rounds"][0]["round_id"]
        for _ in range(4):
            view = reroll(service, view, round_id)
        candidate = chat(view)["rounds"][0]["candidates"][1]
        view = WorkflowChatOperations(service).select(
            view["workflow_session_id"], round_id=round_id, candidate_id=candidate["candidate_id"], **guards(view))
        assert counter(view) == 2 and len(checkpoint_history(service, view)["path"]) == 5
        view = submit(service, view, "second")
        assert calls[-1] == 2 and counter(view) == 8
        assert chat(view)["rounds"][1]["base_checkpoint_id"] == candidate["result_checkpoint_id"]
        for action in ("select", "reroll"):
            parameters = {"round_id": round_id, **guards(view)}
            if action == "select":
                parameters["candidate_id"] = candidate["candidate_id"]
            with pytest.raises(ContractValidationError) as caught:
                getattr(WorkflowChatOperations(service), action)(view["workflow_session_id"], **parameters)
            assert caught.value.reason_code == "chat_round_locked"
        assert service.get_session(view["workflow_session_id"]) == view


def test_floor_fork_retains_all_five_candidates_and_selected_computation(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "floor-fork.sqlite")) as service:
        _, view, _, _, _ = graph_fixture(service)
        view = submit(service, view, "first")
        round_id = chat(view)["rounds"][0]["round_id"]
        for _ in range(4):
            view = reroll(service, view, round_id)
        candidate = chat(view)["rounds"][0]["candidates"][1]
        view = WorkflowChatOperations(service).select(
            view["workflow_session_id"], round_id=round_id, candidate_id=candidate["candidate_id"], **guards(view))
        source = submit(service, view, "second")
        result = WorkflowChatOperations(service).fork_floor(
            source["workflow_session_id"], round_id=round_id, **guards(source))
        target = service.get_session(result["target_session_id"])
        assert len(chat(target)["rounds"]) == 1 and counter(target) == 2
        floor = chat(target)["rounds"][0]
        assert len(floor["candidates"]) == 5
        target_points = {point["checkpoint_id"] for point in checkpoint_history(service, target)["path"]}
        source_points = {point["checkpoint_id"] for point in checkpoint_history(service, source)["path"]}
        assert len(target_points) == 5 and not target_points & source_points
        assert {item["result_checkpoint_id"] for item in floor["candidates"]} == target_points
        target = WorkflowChatOperations(service).select(
            target["workflow_session_id"], round_id=round_id,
            candidate_id=floor["candidates"][3]["candidate_id"], **guards(target))
        assert counter(target) == 4
        target = reroll(service, target, round_id)
        assert counter(target) == 7
        assert service.get_session(source["workflow_session_id"]) == source


@pytest.mark.parametrize("guard", [
    "expected_session_revision", "expected_data_revision", "expected_head_revision", "expected_object_revision",
])
def test_stale_operation_guards_cannot_change_any_current_data(tmp_path, guard):
    with closing(GraphWorkflowService(tmp_path / "stale.sqlite")) as service:
        _, view, calls, _, _ = graph_fixture(service)
        view = submit(service, view, "input")
        parameters = guards(view)
        parameters[guard] += 1
        with pytest.raises(ContractValidationError):
            WorkflowChatOperations(service).reroll(
                view["workflow_session_id"], round_id=chat(view)["rounds"][0]["round_id"], **parameters)
        assert calls == [0] and service.get_session(view["workflow_session_id"]) == view


def test_reroll_original_request_replay_never_dispatches_again(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "replay.sqlite")) as service:
        _, view, calls, _, _ = graph_fixture(service)
        view = submit(service, view, "original")
        arguments = {"round_id": chat(view)["rounds"][0]["round_id"], **guards(view)}
        operations = WorkflowChatOperations(service)
        first = operations.reroll(view["workflow_session_id"], **arguments)
        view = settle(service, first)
        assert operations.reroll(view["workflow_session_id"], **arguments) == first
        assert calls == [0, 0] and len(chat(view)["rounds"][0]["candidates"]) == 2
