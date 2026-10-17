"""End-to-end acceptance of the installed checkpoint state contract."""

from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding
from test_context_native_integration import (
    NativeTransport,
    injected_once_contents,
    native_graph,
    node_output,
    once_content,
    versioned_node,
)
from test_gemini_joint_integration import GeminiGraphFixture, gemini_graph
from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from test_success_checkpoints import checkpoint_history
from workflow_test_support import run as native_run


def _fork(service, view, checkpoint_id):
    return service.fork_checkpoint(
        view["workflow_session_id"],
        checkpoint_id=checkpoint_id,
        expected_session_revision=view["revision"],
        expected_data_revision=view["data_revision"],
        expected_head_revision=view["head_revision"],
        idempotency_key=str(uuid4()),
    )


def _install_noncheckpoint_runtime_state(service, document):
    fixed_id, runtime_id = str(uuid4()), str(uuid4())
    fixed_definition = NodeDefinition(
        "test.checkpoint-acceptance-fixed",
        "1",
        "Checkpoint fixed configuration",
        "Test",
        {"retained": "definition-value", "omitted": "not-declared"},
        {
            "type": "object",
            "required": ["retained", "omitted"],
            "properties": {"retained": {"type": "string"}, "omitted": {"type": "string"}},
            "additionalProperties": False,
        },
        checkpoint_save={"config_paths": [["retained"]]},
    )
    service.registry.register(fixed_definition, lambda config, inputs, context: {})
    document["nodes"].append({
        "node_binding_id": fixed_id,
        "component_id": fixed_definition.component_id,
        "component_version": fixed_definition.component_version,
        "title": fixed_definition.display_name,
        "position": {"x": 0, "y": 0},
        "config": deepcopy(fixed_definition.default_config),
    })

    private_schema = {
        "type": "object",
        "required": ["rounds"],
        "properties": {"rounds": {"type": "integer", "minimum": 0}},
        "additionalProperties": False,
    }

    def update_private_and_program(config, inputs, context):
        private = context.private_read()
        context.private_write({"rounds": private["rounds"] + 1})
        context.register_variable("acceptance_rounds", "integer", initial=0, has_initial=True)
        context.assign_variable("acceptance_rounds", 1, operation="add")
        return {}

    runtime_definition = NodeDefinition(
        "test.checkpoint-acceptance-runtime-state",
        "1",
        "Checkpoint excluded runtime state",
        "Test",
        {},
        {"type": "object", "additionalProperties": False},
        capabilities=("private:read", "private:write", "variables:write"),
        private_state_schema=private_schema,
        private_state_default={"rounds": 0},
    )
    service.registry.register(runtime_definition, update_private_and_program)
    document["nodes"].append({
        "node_binding_id": runtime_id,
        "component_id": runtime_definition.component_id,
        "component_version": runtime_definition.component_version,
        "title": runtime_definition.display_name,
        "position": {"x": 0, "y": 0},
        "config": {},
    })

    service.registry.data_types.register(DataTypeDefinition(
        "test.checkpoint-external-state",
        1,
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        default_value={"value": "initial"},
    ))

    def write_external_state(config, inputs, context):
        current = context.object_read(config["object_key"])
        context.object_write(
            config["object_key"], {"value": "source-only"}, expected_revision=current["revision"],
        )
        return {}

    service.registry.register(NodeDefinition(
        "test.checkpoint-external-writer",
        "1",
        "Checkpoint-excluded external object writer",
        "Test",
        {"object_key": "outside/checkpoint"},
        {
            "type": "object",
            "properties": {"object_key": {"type": "string"}},
            "required": ["object_key"],
            "additionalProperties": False,
        },
        capabilities=("objects:read", "objects:write"),
        object_accesses=({
            "config_field": "object_key",
            "multiple": False,
            "type_id": "test.checkpoint-external-state",
            "schema_version": 1,
            "access": "read_write",
        },),
    ), write_external_state)
    external_writer = versioned_node(service, "test.checkpoint-external-writer", "1", 4900)
    document["nodes"].append(external_writer)
    document.setdefault("object_bindings", []).append(ObjectBinding(
        "outside/checkpoint",
        "test.checkpoint-external-state",
        1,
        "shared",
        readers=(external_writer["node_binding_id"],),
        writers=(external_writer["node_binding_id"],),
    ).to_dict())
    document.setdefault("execution_roots", []).extend((
        fixed_id, runtime_id, external_writer["node_binding_id"],
    ))
    merge = next(node for node in document["nodes"] if node["component_id"] == "context.merge")
    document.setdefault("control_edges", []).append({
        "edge_id": str(uuid4()),
        "source_node_id": merge["node_binding_id"],
        "target_node_id": runtime_id,
    })
    document["control_edges"].append({
        "edge_id": str(uuid4()),
        "source_node_id": merge["node_binding_id"],
        "target_node_id": external_writer["node_binding_id"],
    })
    return {
        "fixed_node_id": fixed_id,
        "runtime_node_id": runtime_id,
        "external_writer_node_id": external_writer["node_binding_id"],
    }


def test_full_diff_fork_loads_only_declared_and_agent_state_then_runs_normally(tmp_path):
    transport = NativeTransport(generate_long_text=True)
    database = tmp_path / "checkpoint-stage2-acceptance.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, entries = native_graph(service)
        extra = _install_noncheckpoint_runtime_state(service, document)
        initial = create(service, document)
        assert checkpoint_history(service, initial)["latest"] is None

        first = native_run(service, initial, "First accepted input.")
        assert first["status"] == "succeeded", first["chains"]
        first_history = checkpoint_history(service, first)
        assert len(first_history["path"]) == 1
        assert first_history["path"][0]["state_storage"]["mode"] == "full"
        assert first_history["state"] == first_history["collected"]
        assert first_history["state"]["configuration"]["nodes"][extra["fixed_node_id"]]["config"] == {
            "retained": "definition-value",
        }
        first_agent = first_history["state"]["agent_contexts"]["context-a"]["value"]
        assert set(first_agent) == {"messages", "layout", "accepted_delta_ids", "generation"}
        assert first_agent["generation"] > 0
        assert "GENERATED_LONG_TEXT" not in json.dumps(first_agent)
        assert "ONCE_ORIGINAL_TEXT" not in json.dumps(first_agent)
        first_root = deepcopy(first_history["path"][0])

        second = native_run(service, first, "Second accepted input.")
        assert second["status"] == "succeeded", second["chains"]
        second_history = checkpoint_history(service, second)
        assert len(second_history["path"]) == 2
        assert second_history["path"][0] == first_root
        assert second_history["path"][1]["state_storage"]["mode"] == "diff"
        assert second_history["state"] == second_history["collected"]
        assert second_history["state"]["configuration"]["nodes"][extra["fixed_node_id"]]["config"] == {
            "retained": "definition-value",
        }
        assert second["private_states"][extra["runtime_node_id"]] == {"rounds": 2}
        assert second["data"]["values"]["acceptance_rounds"]["value"] == 2
        assert second["objects"]["outside/checkpoint"]["value"]["value"] == "source-only"
        assert set(second_history["state"]) == {
            "configuration", "agent_contexts", "variables", "chat_contexts",
        }
        assert "outside/checkpoint" not in json.dumps(second_history["state"])
        assert "acceptance_rounds" not in json.dumps(second_history["state"])

        selected = second_history["path"][-1]
        source_object = deepcopy(second["objects"]["context-a"])
        source_data = deepcopy(second["data"])
        source_private = deepcopy(second["private_states"])
        fork = _fork(service, second, selected["checkpoint_id"])
        target = service.get_session(fork["target_session_id"])
        target_history = checkpoint_history(service, target)
        assert target_history["state"] == second_history["state"]
        assert target_history["collected"] == second_history["state"]
        assert target["objects"]["context-a"]["value"] == (
            second_history["state"]["agent_contexts"]["context-a"]["value"])
        assert target["objects"]["context-a"]["revision_id"] != source_object["revision_id"]
        assert target["private_states"][extra["runtime_node_id"]] == {"rounds": 0}
        assert target["data"]["values"] == {}
        assert target["objects"]["outside/checkpoint"]["value"] == {"value": "initial"}

        continued = native_run(service, target, "Continue from the checkpoint.")
        assert continued["status"] == "succeeded", continued["chains"]
        continued_history = checkpoint_history(service, continued)
        assert len(continued_history["path"]) == 3
        assert continued_history["path"][-1]["state_storage"]["mode"] == "diff"
        assert continued_history["state"] == continued_history["collected"]
        assembled = node_output(continued, entries["a"]["assembly"])
        assert injected_once_contents(assembled) == {once_content(entries["once"])}
        assert continued["private_states"][extra["runtime_node_id"]] == {"rounds": 1}
        assert continued["data"]["values"]["acceptance_rounds"]["value"] == 1
        assert continued["objects"]["outside/checkpoint"]["value"]["value"] == "source-only"
        current_source = service.get_session(second["workflow_session_id"])
        assert current_source["objects"]["context-a"] == source_object
        assert current_source["data"] == source_data
        assert current_source["private_states"] == source_private


def test_signed_provider_content_survives_fork_source_deletion_and_refork(tmp_path):
    fixture = GeminiGraphFixture()
    with closing(GraphWorkflowService(
            tmp_path / "checkpoint-signed-context.sqlite",
            public_model_factory=fixture.factory)) as service:
        fixture.write_provider(service)
        document = gemini_graph(service, "native")
        source = native_run(service, create(service, document), "Signed accepted input.")
        assert source["status"] == "succeeded", source["chains"]
        source_history = checkpoint_history(service, source)
        assert source_history["state"] == source_history["collected"]
        signed_value = source_history["state"]["agent_contexts"]["context-a"]["value"]
        assert "synthetic-fixture-only" in json.dumps(signed_value)
        selected_id = source_history["latest"]["checkpoint_id"]

        fork = _fork(service, source, selected_id)
        target = service.get_session(fork["target_session_id"])
        assert checkpoint_history(service, target)["state"] == source_history["state"]
        assert target["objects"]["context-a"]["value"] == signed_value

        continued = native_run(service, target, "Continue with signed history.")
        assert continued["status"] == "succeeded", continued["chains"]
        continued_history = checkpoint_history(service, continued)
        assert continued_history["state"] == continued_history["collected"]
        continued_value = continued_history["state"]["agent_contexts"]["context-a"]["value"]
        assert "synthetic-fixture-only" in json.dumps(continued_value)

        deleted = service.delete_session(
            source["workflow_session_id"],
            expected_revision=source["revision"],
            idempotency_key=str(uuid4()),
        )
        assert deleted["deleted"]
        surviving = service.get_session(continued["workflow_session_id"])
        surviving_history = checkpoint_history(service, surviving)
        assert surviving_history["state"] == surviving_history["collected"]
        assert "synthetic-fixture-only" in json.dumps(surviving_history["state"])

        refork = _fork(
            service, surviving, surviving_history["latest"]["checkpoint_id"])
        reforked = service.get_session(refork["target_session_id"])
        reforked_history = checkpoint_history(service, reforked)
        assert reforked_history["state"] == surviving_history["state"]
        assert reforked_history["collected"] == surviving_history["state"]
