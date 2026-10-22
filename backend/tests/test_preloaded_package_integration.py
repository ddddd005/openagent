"""Disk package and native Agent consumers of preloaded session values, offline."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.local_workflow_packages import LocalWorkflowPackageSource

from test_checkpoint_fork_stage2_acceptance import _fork
from test_graph_receipt_roundtrip import fixed_configuration
from test_graph_service import edge, node, run
from test_local_workflow_packages import write_package
from test_models_service_integration import ModelDatabaseFixture
from test_success_checkpoints import checkpoint_history
from test_variable_tool_bindings import (
    KEY, VariableToolTransport, _events, _run, build_graph, node_output,
)


PACKAGE = "test.disk-preloads"
STRUCTURE = "test.disk-preloaded-value"
DATA_KEY = "disk/structure"
EXPORTS = {
    "data_types": [{"scope": "session", "type_id": STRUCTURE, "schema_version": 1}],
    "nodes": [
        {"component_id": "test.disk-consumer", "component_version": "1"},
        {"component_id": "test.disk-output", "component_version": "1"},
    ],
}
PACKAGE_CODE = '''
from phase1_agent.capability_packages import CapabilityPackage, PackageManifest
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.host_sdk import DataTypeDefinition

def create():
    def register(host):
        host.register_data_type(DataTypeDefinition(
            "test.disk-preloaded-value", 1,
            {"type": "object", "properties": {
                "count": {"type": "integer"}, "label": {"type": "string"}},
             "required": ["count", "label"], "additionalProperties": False},
            checkpoint_admission=True))

        def consume(config, inputs, context):
            basis = context.object_read(config["object_key"])
            value = {**basis["value"], "count": basis["value"]["count"] + config["step"]}
            context.object_write(config["object_key"], value, expected_revision=basis["revision"])
            return {"output": {
                "schema_version": 1, "kind": "workflow.text", "text": str(value["count"])}}

        host.register_node(NodeDefinition(
            "test.disk-consumer", "1", "Disk consumer", "Test",
            {"object_key": "disk/structure", "step": 3},
            {"type": "object", "properties": {
                "object_key": {"type": "string"}, "step": {"type": "integer"}},
             "required": ["object_key", "step"], "additionalProperties": False},
            outputs=(NodePort("output", "TEXT"),),
            capabilities=("objects:read", "objects:write"),
            object_accesses=({
                "config_field": "object_key", "multiple": False, "access": "read_write",
                "type_id": "test.disk-preloaded-value", "schema_version": 1},),
        ), consume)
        host.register_node(NodeDefinition(
            "test.disk-output", "1", "Disk output", "Test", {}, {"type": "object"},
            inputs=(NodePort("input", "TEXT"),), outputs=(NodePort("output", "TEXT"),),
            is_output=True,
        ), lambda config, inputs, context: {"output": inputs["input"]})

    return CapabilityPackage(PackageManifest.from_dict(PACKAGE_MANIFEST), register)
'''


def disk_source(root):
    manifest = {
        "schema_version": 1, "package_id": PACKAGE, "version": "1",
        "host_protocol_version": 1, "dependencies": [], "exports": EXPORTS,
    }
    write_package(root, package_id=PACKAGE, code=PACKAGE_CODE.replace("PACKAGE_MANIFEST", repr(manifest)),
                  manifest_updates={"exports": EXPORTS})
    return LocalWorkflowPackageSource(root)


def disk_document(service):
    consumer = node(service.registry, "test.disk-consumer", 57201, component_version="1")
    output = node(service.registry, "test.disk-output", 57202, component_version="1")
    output["public_outputs"] = ["output"]
    return fixed_configuration({
        "schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Disk structure preload", "nodes": [consumer, output],
        "edges": [edge(consumer, output, 57201)],
        "package_lock": list(service.registry.execution_package_lock),
        "object_bindings": [ObjectBinding(
            DATA_KEY, STRUCTURE, 1, "shared", readers=(consumer["node_binding_id"],),
            writers=(consumer["node_binding_id"],)).to_dict()],
        "session_preloads": [{"object_key": DATA_KEY, "value": {"count": 2, "label": "seed"}}],
    })


def save(service, document, *, sequence=0, confirm=None):
    return service.update_definition(
        document, workflow_definition_id=document["workflow_definition_id"],
        expected_edit_sequence=sequence, confirm_derivation=confirm, idempotency_key=str(uuid4()))


def create(service, document, *, key=None):
    return service.create_session(
        document["workflow_definition_id"], document["revision"], idempotency_key=key or str(uuid4()))


def disk_value(view):
    return view["objects"][DATA_KEY]["value"]


def test_disk_package_type_catalog_save_consumer_checkpoint_fork_derivation_and_reopen(tmp_path):
    package_root = tmp_path / "packages"
    source = disk_source(package_root)
    database = tmp_path / "disk.sqlite"

    def no_model(**kwargs):
        pytest.fail("Disk preload flow must not construct a model transport")

    with closing(GraphWorkflowService(
            database, workflow_package_source=source, enabled_packages={PACKAGE: "1"},
            public_model_factory=no_model)) as service:
        capabilities = service.platform_capabilities()
        assert capabilities["package_diagnostics"] == []
        assert capabilities["local_package_sources"]["root"] == str(package_root.resolve())
        assert capabilities["local_package_sources"]["packages"][0]["available"] is True
        declaration = service.definition_query.get("data_type", {
            "scope": "session", "type_id": STRUCTURE, "schema_version": 1})
        assert declaration["source"] == [{"package_id": PACKAGE, "package_version": "1"}]
        assert declaration["declaration"]["has_default"] is False
        assert declaration["declaration"]["checkpoint_admission"] is True
        assert any(item["component_id"] == "test.disk-consumer"
                   for item in service.node_types(protocol_version=2)["node_types"])

        saved = save(service, disk_document(service))
        graph = saved["document"]
        assert service.get_definition(graph["workflow_definition_id"]) == graph
        key = str(uuid4())
        first = create(service, graph, key=key)
        second = create(service, graph)
        assert disk_value(first) == disk_value(second) == {"count": 2, "label": "seed"}
        assert first["objects"][DATA_KEY]["binding"] == graph["object_bindings"][0]
        assert first["objects"][DATA_KEY]["revision"] == 1
        done = run(service, first)
        assert done["status"] == "succeeded", done["chains"]
        assert disk_value(done) == {"count": 5, "label": "seed"}
        assert disk_value(service.get_session(second["workflow_session_id"])) == {"count": 2, "label": "seed"}
        assert create(service, graph, key=key) == first
        assert disk_value(service.get_session(first["workflow_session_id"])) == {"count": 5, "label": "seed"}

        history = checkpoint_history(service, done)
        root = history["latest"]
        root_state = deepcopy(history["state"])
        assert root["state_storage"]["mode"] == "full"
        assert root_state["preloaded_items"] == {
            DATA_KEY: {"type_id": STRUCTURE, "schema_version": 1, "value": disk_value(done)}}
        assert history["collected"] == root_state
        twice = run(service, done)
        assert twice["status"] == "succeeded", twice["chains"]
        assert disk_value(twice) == {"count": 8, "label": "seed"}
        assert checkpoint_history(service, twice)["latest"]["state_storage"]["mode"] == "diff"

        proposed = deepcopy(graph)
        proposed["session_preloads"][0]["value"] = {"count": 100, "label": "new basis"}
        pending = save(service, proposed, sequence=1)
        assert pending["outcome"] == "confirmation_required"
        derived = save(service, proposed, sequence=1, confirm=True)
        assert derived["outcome"] == "derived"
        assert derived["workflow_definition_id"] != graph["workflow_definition_id"]
        assert disk_value(create(service, derived["document"])) == {"count": 100, "label": "new basis"}
        assert disk_value(service.get_session(first["workflow_session_id"])) == {"count": 8, "label": "seed"}
        assert disk_value(service.get_session(second["workflow_session_id"])) == {"count": 2, "label": "seed"}

        forked = _fork(service, twice, root["checkpoint_id"])
        target = service.get_session(forked["target_session_id"])
        assert disk_value(target) == {"count": 5, "label": "seed"}
        assert checkpoint_history(service, target)["state"] == root_state
        target_id = target["workflow_session_id"]
        source_id = first["workflow_session_id"]
        derived_id = derived["workflow_definition_id"]

    with closing(GraphWorkflowService(
            database, workflow_package_source=LocalWorkflowPackageSource(package_root),
            public_model_factory=no_model)) as reopened:
        assert disk_value(reopened.get_session(source_id)) == {"count": 8, "label": "seed"}
        target = reopened.get_session(target_id)
        assert disk_value(target) == {"count": 5, "label": "seed"}
        assert checkpoint_history(reopened, target)["state"] == root_state
        assert reopened.get_definition(derived_id)["session_preloads"][0]["value"] == {
            "count": 100, "label": "new basis"}
        continued = run(reopened, target)
        assert continued["status"] == "succeeded", continued["chains"]
        assert disk_value(continued) == {"count": 8, "label": "seed"}


def agent_preloaded_graph(service, *, allowed=True):
    graph, parts = build_graph(service, with_normal=False)
    registrar_id = parts["register"]["node_binding_id"]
    graph["nodes"] = [item for item in graph["nodes"] if item["node_binding_id"] != registrar_id]
    graph.pop("event_bindings")
    for binding in graph["object_bindings"]:
        if binding["object_key"] != KEY:
            continue
        binding.pop("registrar_node_id")
        binding["readers"] = [identity for identity in binding["readers"] if identity != registrar_id]
        binding["writers"] = [identity for identity in binding["writers"] if identity != registrar_id]
        if not allowed:
            binding["writers"].remove(parts["execute"]["node_binding_id"])
    graph["session_preloads"] = [{
        "object_key": KEY, "value": {
            "registered": True, "name": "score", "value_type": "integer",
            "assigned": True, "value": 4},
    }]
    return fixed_configuration(graph), parts


def test_native_agent_uses_preloaded_variable_without_register_or_creation_permissions(tmp_path):
    transport = VariableToolTransport([
        ("score_read", {}), ("score_set", {"value": 10}), ("score_add", {"value": 2}),
        ("final_answer", {"answer": {"text": "done"}}),
    ])
    with closing(GraphWorkflowService(
            tmp_path / "agent.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        graph, parts = agent_preloaded_graph(service)
        assert not any(item["component_id"] == "tools.variable-register" for item in graph["nodes"])
        saved = save(service, graph)
        initial = create(service, saved["document"])
        assert initial["objects"][KEY]["value"]["value"] == 4
        assert initial["objects"][KEY]["value"]["registered"] is True
        chain_id, done = _run(service, initial)
        assert done["status"] == "succeeded", done["chains"]
        assert done["objects"][KEY]["value"]["value"] == 12
        assert node_output(done, parts["after"])["value"] == 12
        assert len(transport.calls) == 4
        events = _events(service, done, chain_id)
        agent = next(item["payload"] for item in events if item["kind"] == "agent_result")
        names = {item["name"] for item in agent["snapshot"]["tool_definitions"]}
        assert {"score_read", "score_set", "score_add", "score_subtract", "final_answer"} <= names
        assert not any("register" in name or "create" in name or "declare" in name for name in names)
        history = checkpoint_history(service, done)
        assert history["state"]["variables"][KEY]["value"]["value"] == 12
        target = service.get_session(_fork(service, done, history["latest"]["checkpoint_id"])["target_session_id"])
        assert target["objects"][KEY]["value"]["value"] == 12


def test_agent_binding_cannot_override_preload_writer_acl(tmp_path):
    transport = VariableToolTransport([("score_set", {"value": 99})])
    with closing(GraphWorkflowService(
            tmp_path / "denied.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        graph, _ = agent_preloaded_graph(service, allowed=False)
        saved = save(service, graph)
        initial = create(service, saved["document"])
        with pytest.raises(ContractValidationError) as error:
            _run(service, initial)
        assert error.value.reason_code == "tool_binding_access_denied"
        assert service.get_session(initial["workflow_session_id"])["objects"][KEY]["value"]["value"] == 4
        assert transport.calls == []
