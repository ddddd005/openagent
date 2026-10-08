"""Global lorebook identity, management, frozen runs and local activation."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import object_schema, text_content
from phase1_agent.context_prompt_v6 import assemble_native_context_prompt
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_execution import NodeExecutionContext
from phase1_agent.global_resources import global_resource_reference
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.tavern.lorebook_resources import (
    LOREBOOK_RESOURCE_TYPE, validate_lorebook_resource,
)
from phase1_agent.tavern.package import GLOBAL_TAVERN_FRONTEND_EXTENSIONS

from test_tavern_package import KEY, NODE_ID, entry, object_state, ref, scan_prompt, uid, variable


def record(text="shared content", *, scope="workspace", resource_id=None, entries=None):
    return {
        **ResourceIdentity(scope, LOREBOOK_RESOURCE_TYPE, resource_id or uid(100)).to_dict(),
        "data_schema_version": 1, "update_sequence": 1,
        "value": {"name": "Shared lorebook", "enabled": True,
                  "entries": entries if entries is not None else [
                      entry(101, text=text, primary_keywords=["match"])]},
    }


def identity(value):
    return ResourceIdentity(value["scope"], value["type_id"], value["resource_id"]).to_dict()


def save(app, value, *, key=None):
    return app.command("resource.save", {
        "record": value, "expected_sequence": value["update_sequence"] - 1,
        "idempotency_key": key or str(uuid4()),
    })


def installed():
    return CapabilityPackageLoader(builtin_capability_packages()).load(
        {"workflow.tavern": "1.1.0"}).registry.detached()


def graph(registry, reference, *, number=1000, texts=("match", "absent"), gate=None):
    if registry.get("test.lorebook-scan", "1") is None:
        def source(config, inputs, context):
            if gate is not None:
                gate[0].set()
                assert gate[1].wait(10), "test scan source was not released"
            return {"output": assemble_native_context_prompt(
                [], inputs["view"], inputs["current_input"],
                context_ref={"scope": "artifact", "output_id": context.input_artifact_refs("view")[0]["output_id"]},
                current_input_ref={
                    "scope": "artifact", "output_id": context.input_artifact_refs("current_input")[0]["output_id"]},
            )}
        registry.register(NodeDefinition(
            "test.lorebook-scan", "1", "Scan", "Test", {}, object_schema({}),
            inputs=(NodePort("view", "CONTEXT_VIEW", data_schema_version=4),
                    NodePort("current_input", "TEXT", data_schema_version=2)),
            outputs=(NodePort("output", "PROMPT", data_schema_version=6),),
        ), source)
        registry.register(NodeDefinition(
            "test.lorebook-view", "1", "Empty view", "Test", {}, object_schema({}),
            outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=4),),
        ), lambda config, inputs, context: {"output": deepcopy(scan_prompt()["context"])})
        registry.register(NodeDefinition(
            "test.lorebook-text", "1", "Current input", "Test", {"text": ""},
            object_schema({"text": {"type": "string"}}),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), lambda config, inputs, context: {"output": text_content(config["text"])})
        registry.register(NodeDefinition(
            "test.lorebook-sink", "1", "Materials", "Test", {}, object_schema({}),
            inputs=(NodePort("input", "PROMPT_MATERIALS", multiple=True),),
            outputs=(NodePort("output", "PROMPT_MATERIALS"),), is_output=True,
        ), lambda config, inputs, context: {"output": inputs["input"][0]})

    def node(component, offset, **config):
        definition = registry.get(component, "1").definition
        return {
            "node_binding_id": uid(number + offset), "component_id": component,
            "component_version": "1", "title": component, "position": {"x": 0, "y": 0},
            "config": {**deepcopy(definition.default_config), **config},
        }

    reference_node = node("lorebook.global-reference", 1, reference=reference)
    sources = [node("test.lorebook-scan", 2 + index) for index in range(len(texts))]
    view = node("test.lorebook-view", 8)
    current_inputs = [node("test.lorebook-text", 10 + index, text=text) for index, text in enumerate(texts)]
    activations = [node("lorebook.global-activate", 4 + index) for index in range(len(texts))]
    sink = node("test.lorebook-sink", 6)
    edges = []
    for index, (source, activation) in enumerate(zip(sources, activations)):
        for origin, target, port in (
            (view, source, "view"), (current_inputs[index], source, "current_input"),
            (reference_node, activation, "resource"), (source, activation, "input"),
            (activation, sink, "input"),
        ):
            edges.append({
                "edge_id": uid(number + 100 + len(edges)),
                "source_node_id": origin["node_binding_id"], "source_port_id": "output",
                "target_node_id": target["node_binding_id"], "target_port_id": port,
                "order": index if target is sink else 0,
            })
    document = {
        "schema_version": 2, "workflow_definition_id": uid(number), "revision": 1,
        "name": "Global lorebook", "nodes": [reference_node, view, *current_inputs, *sources, *activations, sink],
        "edges": edges, "object_bindings": [], "package_lock": list(registry.package_lock),
    }
    return document, activations


def create(service, document):
    service.save_definition(document, expected_revision=0, idempotency_key=str(uuid4()))
    return service.create_session(
        document["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def run(service, session):
    started = service.start(
        session["workflow_session_id"], expected_revision=session["revision"],
        idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    result = service.get_session(session["workflow_session_id"])
    assert result["status"] == "succeeded", [row["diagnostic"] for row in result["chains"]]
    return result


def output(session, node):
    return next(row["outputs"]["output"] for row in session["nodes"]
                if row["node_binding_id"] == node["node_binding_id"])


def test_management_receipts_cas_identity_and_reopen(tmp_path):
    database = tmp_path / "resources.sqlite"
    registry = installed()
    workspace = record()
    project = record("project content", scope="project:one")
    with closing(GraphWorkflowService(database, registry=registry)) as service:
        app = GraphApplication(service)
        key = str(uuid4())
        accepted = save(app, workspace, key=key)
        save(app, project)
        assert "shared content" not in canonical_bytes(accepted).decode()
        assert accepted["receipt"]["accepted"] == accepted["result"]
        changed = deepcopy(workspace)
        changed["update_sequence"] = 2
        changed["value"]["entries"][0]["text"] = "changed content"
        save(app, changed)
        assert save(app, workspace, key=key) == accepted
        with pytest.raises(ContractValidationError) as stale:
            save(app, workspace)
        assert stale.value.reason_code == "stale_revision"
        assert app.query("resource.list", {
            "scope": "workspace", "type_id": LOREBOOK_RESOURCE_TYPE}) == [changed]
        assert app.query("resource.read", {"identity": identity(project)}) == project
    with closing(GraphWorkflowService(database, registry=registry)) as reopened:
        assert GraphApplication(reopened).query(
            "resource.read", {"identity": identity(workspace)}) == changed


@pytest.mark.parametrize("field,value", [
    ("object_keys", ["variable/secret"]), ("lifecycle", "context_once"),
    ("scan_state", {}), ("entries", [entry(101), entry(101)]),
])
def test_resource_schema_excludes_permissions_state_and_duplicate_member_ids(field, value):
    invalid = record()["value"]
    invalid[field] = value
    with pytest.raises(ContractValidationError):
        validate_lorebook_resource(invalid)


@pytest.mark.parametrize("problem,reason", [
    ("missing", "global_resource_missing"), ("disabled", "global_content_disabled"),
])
def test_preflight_rejects_before_run_and_preserves_session(tmp_path, problem, reason):
    registry = installed()
    value = record()
    document, _ = graph(registry, identity(value))
    with closing(GraphWorkflowService(tmp_path / "preflight.sqlite", registry=registry)) as service:
        if problem == "disabled":
            value["value"]["enabled"] = False
            save(GraphApplication(service), value)
        initial = create(service, document)
        with pytest.raises(ContractValidationError) as caught:
            run(service, initial)
        assert caught.value.reason_code == reason
        assert service.get_session(initial["workflow_session_id"]) == initial
        assert service._resource_frames == {}


def test_multiple_graphs_share_content_without_sharing_scan_or_node_identity(tmp_path):
    registry = installed()
    value = record()
    first_doc, first_nodes = graph(registry, identity(value))
    second_doc, second_nodes = graph(registry, identity(value), number=2000, texts=("match", "match"))
    with closing(GraphWorkflowService(tmp_path / "graphs.sqlite", registry=registry)) as service:
        save(GraphApplication(service), value)
        first = run(service, create(service, first_doc))
        second = run(service, create(service, second_doc))
        assert output(first, first_nodes[0])["items"][0]["text"] == "shared content"
        assert output(first, first_nodes[1])["items"] == []
        assert output(second, second_nodes[0])["items"][0]["text"] == "shared content"
        assert output(first, first_nodes[0])["items"][0]["item_instance_id"] != (
            output(second, second_nodes[0])["items"][0]["item_instance_id"])
        assert all(output(second, node)["items"][0]["lifecycle"] == "per_request"
                   and output(second, node)["items"][0]["compaction"] == "never" for node in second_nodes)
        assert first["objects"] == second["objects"] == {}
        assert GraphApplication(service).query("resource.read", {"identity": identity(value)}) == value


def test_update_during_run_uses_frozen_body_next_run_uses_new_body_and_history_stays(tmp_path):
    registry = installed()
    value = record()
    entered, release = Event(), Event()
    document, nodes = graph(registry, identity(value), texts=("match", "match"), gate=(entered, release))
    database = tmp_path / "frozen.sqlite"
    with closing(GraphWorkflowService(database, registry=registry)) as service:
        app = GraphApplication(service)
        save(app, value)
        session = create(service, document)
        started = service.start(
            session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key=str(uuid4()))
        try:
            assert entered.wait(10)
            changed = deepcopy(value)
            changed["update_sequence"] = 2
            changed["value"]["entries"][0]["text"] = "new shared content"
            save(app, changed)
        finally:
            release.set()
        service.wait(started["active_chain_run_id"])
        first = service.get_session(session["workflow_session_id"])
        assert first["status"] == "succeeded", first["chains"]
        assert all(output(first, node)["items"][0]["text"] == "shared content" for node in nodes)
        chain_id = first["selected_chain_run_id"]
        frozen_history = service.get_run(session["workflow_session_id"], chain_id)
        second = run(service, first)
        assert all(output(second, node)["items"][0]["text"] == "new shared content" for node in nodes)
        assert service.get_run(session["workflow_session_id"], chain_id) == frozen_history
        assert service._resource_frames == {}
    with closing(GraphWorkflowService(database, registry=registry)) as reopened:
        assert reopened.get_run(session["workflow_session_id"], chain_id) == frozen_history


def test_global_probability_draws_and_recursive_budget_are_owned_by_each_activation(tmp_path, monkeypatch):
    samples = []
    def draw():
        samples.append(0.25)
        return 0.25
    monkeypatch.setattr("phase1_agent.lorebook_engine.random.random", draw)
    members = [entry(300 + index, text=f"STEP_{index}",
                     primary_keywords=["match" if index == 0 else f"STEP_{index - 1}"],
                     recursive=True, probability_enabled=True, probability=50) for index in range(5)]
    value = record(entries=members)
    registry = installed()
    document, nodes = graph(registry, identity(value), texts=("match", "match"))
    with closing(GraphWorkflowService(tmp_path / "local-state.sqlite", registry=registry)) as service:
        save(GraphApplication(service), value)
        completed = run(service, create(service, document))
        assert len(samples) == 10
        assert all({item["text"] for item in output(completed, node)["items"]}
                   == {f"STEP_{index}" for index in range(4)} for node in nodes)


def test_saved_legacy_selection_is_not_upgraded_when_service_reopens(tmp_path):
    database = tmp_path / "legacy.sqlite"
    old = {**DEFAULT_PACKAGES, "workflow.tavern": "1.0.0"}
    with closing(GraphWorkflowService(database, enabled_packages=old)) as service:
        assert service.registry.get("lorebook.global-activate", "1") is None
    with closing(GraphWorkflowService(database)) as reopened:
        assert reopened.registry.get("lorebook.item", "1") is not None
        assert reopened.registry.get("lorebook.global-activate", "1") is None
        assert {"package_id": "workflow.tavern", "version": "1.0.0"} in reopened.registry.package_lock


def test_new_global_nodes_cannot_be_loaded_from_old_exact_lock():
    loader = CapabilityPackageLoader(builtin_capability_packages())
    original = loader.load({"workflow.tavern": "1.0.0"})
    current = loader.load({"workflow.tavern": "1.1.0"})
    assert original.registry.get("lorebook.global-reference", "1") is None
    assert current.registry.get("lorebook.global-reference", "1") is not None
    assert original.registry.get("lorebook.item", "1").definition.to_dict() == (
        current.registry.get("lorebook.item", "1").definition.to_dict())
    assert [row for row in current.frontend_extensions if row["package_id"] == "workflow.tavern"] == sorted([{
        "component_id": None, "component_version": None, **deepcopy(row),
        "schema_version": 2, "host_protocol_version": 1,
        "package_id": "workflow.tavern", "package_version": "1.1.0",
    } for row in GLOBAL_TAVERN_FRONTEND_EXTENSIONS], key=lambda row: row["extension_id"])


def direct_activation(registry, value, *, objects, accepted=None):
    implementation = registry.get("lorebook.global-activate", "1")
    prompt = scan_prompt()
    reference = identity(value)
    def host(context, capability, operation, payload):
        if capability == "resources:read":
            assert operation == "current-global-resource" and payload == reference
            return deepcopy(value)
        assert (capability, operation, payload) == (
            "artifacts:read", "resolve-artifact", {"reference": ref(930)})
        return {"value": deepcopy(prompt if accepted is None else accepted)}
    context = NodeExecutionContext(
        definition=implementation.definition, node_binding_id=NODE_ID,
        workflow_session_id=uid(800), chain_run_id=uid(821), node_run_id=uid(822),
        state={"revision": 0, "values": {}}, private_states={}, external_inputs={},
        type_registry=registry.data_types, object_states=objects,
        input_refs={"input": [{"edge_id": uid(931), "output_id": uid(930), "order": 0}]},
        object_accesses={KEY: "read"}, host=host,
    )
    result = implementation.executor(
        {"object_keys": [KEY]}, {"input": prompt, "resource": global_resource_reference(reference)}, context)
    registry.validate_content(result["output"], "PROMPT_MATERIALS", 1)
    return result["output"], context


def test_shared_resource_reads_only_each_nodes_authorized_variable_and_never_writes():
    registry = installed()
    value = record(entries=[entry(101, primary_keywords=["{{keyword}}"])])
    original = deepcopy(value)
    matching, context = direct_activation(registry, value, objects=object_state(variable("match")))
    missing, other = direct_activation(registry, value, objects=object_state(variable("different")))
    assert len(matching["items"]) == 1 and missing["items"] == []
    assert context.object_writes == other.object_writes == []
    assert context.effects == other.effects == []
    assert value == original
    assert context.reads[0] == {
        "kind": "global_resource_read", "reference": identity(value), "update_sequence": 1}
    assert all("value" not in row for row in context.reads)
    with pytest.raises(ContractValidationError) as forbidden:
        direct_activation(registry, value, objects=object_state(variable(), readers=()))
    assert forbidden.value.reason_code == "session_object_access_denied"


def test_global_activation_preserves_exact_prompt_and_resource_type_checks():
    registry = installed()
    value = record()
    with pytest.raises(ContractValidationError) as mismatch:
        direct_activation(registry, value, objects=object_state(variable()), accepted=scan_prompt("forged"))
    assert mismatch.value.reason_code == "lorebook_artifact_mismatch"
    for field, changed in (("type_id", "workflow.prompt-resource"), ("data_schema_version", 2)):
        invalid = {**deepcopy(value), field: changed}
        implementation = registry.get("lorebook.global-activate", "1")
        with pytest.raises(ContractValidationError) as invalid_type:
            implementation.resource_preflight_validator({"object_keys": []}, [invalid])
        assert invalid_type.value.reason_code == "lorebook_resource_type_mismatch"


def test_global_materials_assemble_once_without_entering_agent_history(tmp_path, monkeypatch):
    from workflow_test_support import run as native_run
    from test_context_native_integration import NativeTransport, node_output, versioned_node
    from test_graph_service import create as native_create, edge
    from test_models_service_integration import ModelDatabaseFixture
    from test_tavern_integration import tavern_graph
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-global-lorebook")
    transport = NativeTransport()
    with closing(GraphWorkflowService(
            tmp_path / "native.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        value = record(entries=[entry(9010, text="GLOBAL_LOREBOOK_ONLY", primary_keywords=["dragon"])])
        save(GraphApplication(service), value)
        document, nodes = tavern_graph(service, [])
        for index, key in enumerate(("lore_a", "lore_b")):
            activation = nodes[key]
            activation["component_id"] = "lorebook.global-activate"
            activation["config"] = {"object_keys": []}
            reference = versioned_node(service, "lorebook.global-reference", "1", 9020 + index)
            reference["config"]["reference"] = identity(value)
            document["nodes"].append(reference)
            document["edges"].append(edge(reference, activation, 9030 + index, target_port="resource"))
        completed = native_run(service, native_create(service, document), "A dragon appears.")
        assert completed["status"] == "succeeded", completed["chains"]
        wire = [message.get("content") for message in transport.calls[0]["messages"]]
        assert wire.count("GLOBAL_LOREBOOK_ONLY") == 1
        assert "GLOBAL_LOREBOOK_ONLY" not in str(node_output(completed, nodes["merge"])["messages"])
        assert all(node_output(completed, nodes[key])["items"][0]["lifecycle"] == "per_request"
                   for key in ("lore_a", "lore_b"))
