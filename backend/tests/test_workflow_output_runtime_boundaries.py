"""Output nodes publish their artifacts; runtime owns round completion."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

from phase1_agent.content_contracts import text_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding

from test_graph_service import create, document, edge, node, run, service
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import AgentTransportFixture, base_graph, run as run_agent


OBJECT_KEY = "test/output-runtime"
OBJECT_TYPE = "test.output-state"
OBJECT_VALUE = {"value": "before"}


def _public_payload(service, session, node_id, run_id):
    return service.read_public_output(
        session["workflow_session_id"],
        workflow_definition_id=session["workflow_definition_id"],
        definition_revision=session["definition_revision"],
        node_id=node_id,
        port_id="output",
        run_id=run_id,
    )["output"]["payload"]


def _record_payload(history, node_id, port_id="output"):
    return next(row["payload"] for row in history["outputs"]
                if row["node_binding_id"] == node_id and row["port_id"] == port_id)


def _node_run(history, node_id):
    return next(row for row in history["node_runs"] if row["node_binding_id"] == node_id)


def test_builtin_and_custom_output_nodes_publish_their_received_text(service):
    def transform(config, inputs, context):
        received = context.validate_content(inputs["input"], "TEXT", 2)
        return {"output": text_content(received["text"] + config["suffix"])}

    service.registry.register(NodeDefinition(
        "test.output-transform", "1", "Output transform", "Test", {"suffix": "!"},
        {"type": "object", "properties": {"suffix": {"type": "string"}},
         "required": ["suffix"], "additionalProperties": False},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        is_output=True,
    ), transform)

    for component, expected in (("tools.output", "source"), ("test.output-transform", "source!")):
        source = node(service.registry, "tools.text", 7101, text="source")
        sink = node(service.registry, component, 7102)
        sink["public_outputs"] = ["output"]
        initial = create(service, document([source, sink], [edge(source, sink, 7101)]))

        final = run(service, initial)

        assert final["status"] == "succeeded", final["chains"]
        sink_run = next(row for row in service.get_run(
            initial["workflow_session_id"], final["selected_chain_run_id"])["node_runs"]
                        if row["node_binding_id"] == sink["node_binding_id"])
        assert _public_payload(service, final, sink["node_binding_id"], sink_run["run_id"]) == (
            text_content(expected))


def _register_writer(registry, entered=None, release=None, *, reject=False):
    registry.data_types.register(DataTypeDefinition(
        OBJECT_TYPE, 1,
        {"type": "object", "properties": {"value": {"type": "string"}},
         "required": ["value"], "additionalProperties": False},
        default_value=deepcopy(OBJECT_VALUE),
    ))

    def write(config, inputs, context):
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(10), "writer gate timed out"
        basis = context.object_read(config["object_key"])
        updated = {**basis["value"], "value": "after"}
        context.object_write(config["object_key"], updated, expected_revision=basis["revision"])
        if reject:
            raise ValueError("Reject the unaccepted writer result")
        return {"output": text_content("writer accepted")}

    registry.register(NodeDefinition(
        "test.output-writer", "1", "Output writer", "Test", {"object_key": OBJECT_KEY},
        {"type": "object", "properties": {"object_key": {"type": "string"}},
         "required": ["object_key"], "additionalProperties": False},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        capabilities=("objects:read", "objects:write"),
        object_accesses=({"config_field": "object_key", "multiple": False, "type_id": OBJECT_TYPE,
                          "schema_version": 1, "access": "read_write"},),
    ), write)


def _object_binding(writer):
    return ObjectBinding(
        OBJECT_KEY, OBJECT_TYPE, 1, "shared",
        readers=(writer["node_binding_id"],), writers=(writer["node_binding_id"],),
    ).to_dict()


def test_accepted_output_precedes_gated_writer_and_only_runtime_completion_saves_checkpoint(
        service):
    entered, release, tail_calls = Event(), Event(), []
    _register_writer(service.registry, entered, release)

    def tail(config, inputs, context):
        tail_calls.append(inputs["input"]["text"])
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.output-tail", "1", "Tail", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), tail)
    source = node(service.registry, "tools.text", 7201, text="source")
    sink = node(service.registry, "tools.output", 7202)
    sink["public_outputs"] = ["output"]
    writer = node(service.registry, "test.output-writer", 7203)
    tail_node = node(service.registry, "test.output-tail", 7204)
    value = document([source, sink, writer, tail_node], [
        edge(source, sink, 7201), edge(writer, tail_node, 7202),
    ])
    value["control_edges"] = [{
        "edge_id": str(uuid4()), "source_node_id": sink["node_binding_id"],
        "target_node_id": writer["node_binding_id"],
    }]
    value["object_bindings"] = [_object_binding(writer)]
    initial = create(service, value)
    original_object = deepcopy(initial["objects"][OBJECT_KEY])
    started = service.start(
        initial["workflow_session_id"], expected_revision=initial["revision"],
        idempotency_key="output-runtime-gated-start",
    )
    chain_id = started["active_chain_run_id"]

    try:
        assert entered.wait(5)
        before_pause = service.get_run(initial["workflow_session_id"], chain_id)
        assert before_pause["chain"]["status"] == "running"
        assert _record_payload(before_pause, sink["node_binding_id"]) == text_content("source")
        assert service.list_checkpoints(initial["workflow_session_id"]) == []
        current = service.get_session(initial["workflow_session_id"])
        service.control(
            initial["workflow_session_id"], action="pause", expected_revision=current["revision"],
            idempotency_key="output-runtime-gated-pause",
        )
    finally:
        release.set()
    service.wait(chain_id)

    paused = service.get_run(initial["workflow_session_id"], chain_id)
    assert paused["chain"]["status"] == "paused"
    assert paused["chain"]["completed_nodes"] == [
        source["node_binding_id"], sink["node_binding_id"], writer["node_binding_id"],
    ]
    assert tail_node["node_binding_id"] not in paused["chain"]["completed_nodes"]
    assert _node_run(paused, writer["node_binding_id"])["status"] == "succeeded"
    assert service.get_session(initial["workflow_session_id"])["objects"][OBJECT_KEY]["value"]["value"] == "after"
    assert tail_calls == []
    assert service.list_checkpoints(initial["workflow_session_id"]) == []

    current = service.get_session(initial["workflow_session_id"])
    service.control(
        initial["workflow_session_id"], action="resume", expected_revision=current["revision"],
        idempotency_key="output-runtime-gated-resume",
    )
    service.wait(chain_id)

    final = service.get_session(initial["workflow_session_id"])
    completed = service.get_run(initial["workflow_session_id"], chain_id)
    assert completed["chain"]["status"] == "succeeded"
    assert completed["chain"]["completed_nodes"] == completed["chain"]["ordered_nodes"]
    assert tail_calls == ["writer accepted"]
    assert _public_payload(
        service, final, sink["node_binding_id"], _node_run(completed, sink["node_binding_id"])["run_id"],
    ) == text_content("source")
    assert len(service.list_checkpoints(initial["workflow_session_id"])) == 1


def test_downstream_failure_keeps_accepted_output_but_discards_writer_intent(service):
    _register_writer(service.registry, reject=True)
    source = node(service.registry, "tools.text", 7301, text="accepted before failure")
    sink = node(service.registry, "tools.output", 7302)
    sink["public_outputs"] = ["output"]
    writer = node(service.registry, "test.output-writer", 7303)
    value = document([source, sink, writer], [edge(source, sink, 7301)])
    value["control_edges"] = [{
        "edge_id": str(uuid4()), "source_node_id": sink["node_binding_id"],
        "target_node_id": writer["node_binding_id"],
    }]
    value["object_bindings"] = [_object_binding(writer)]
    initial = create(service, value)
    original_object = deepcopy(initial["objects"][OBJECT_KEY])
    final = run(service, initial)
    history = service.get_run(initial["workflow_session_id"], final["selected_chain_run_id"])

    assert final["status"] == "failed"
    assert history["chain"]["status"] == "failed"
    assert _record_payload(history, sink["node_binding_id"]) == text_content("accepted before failure")
    rejected = _node_run(history, writer["node_binding_id"])
    assert rejected["status"] == "failed" and rejected["output_refs"] == {}
    assert final["objects"][OBJECT_KEY] == original_object
    assert service.list_checkpoints(initial["workflow_session_id"]) == []


def test_read_only_agent_round_without_context_merge_preserves_context_value_and_revision(tmp_path):
    transport = AgentTransportFixture()
    with closing(GraphWorkflowService(
            tmp_path / "output-runtime-read-only-context.sqlite",
            public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        value = base_graph(service)
        merge = next(item for item in value["nodes"] if item["component_id"] == "context.merge")
        read = next(item for item in value["nodes"] if item["component_id"] == "context.output")
        display = next(item for item in value["nodes"] if item["component_id"] == "tools.output")
        display["public_outputs"] = ["output"]
        value["nodes"] = [item for item in value["nodes"]
                          if item["node_binding_id"] != merge["node_binding_id"]]
        value["edges"] = [item for item in value["edges"]
                          if merge["node_binding_id"] not in (
                              item["source_node_id"], item["target_node_id"])]
        value["execution_roots"] = [
            identity for identity in value["execution_roots"]
            if identity != merge["node_binding_id"]]
        binding = value["object_bindings"][0]
        binding["readers"] = [read["node_binding_id"]]
        binding["writers"] = []
        initial = create(service, value)
        original = deepcopy(initial["objects"]["context"])

        final = run_agent(service, initial, "Read the context and output the accepted answer.")

        assert final["status"] == "succeeded", final["chains"]
        assert final["objects"]["context"] == original
        assert final["objects"]["context"]["revision"] == original["revision"]
        display_run = next(item for item in final["nodes"]
                           if item["node_binding_id"] == display["node_binding_id"])
        assert _public_payload(service, final, display["node_binding_id"], display_run["run_id"]) == (
            text_content("accepted answer"))
        assert transport.calls
