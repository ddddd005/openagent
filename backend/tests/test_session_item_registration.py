from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import (
    GraphCompiler, NodeDefinition, NodePort, NodeRegistry, validate_graph_document,
    validate_session_item_registrations,
)
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding


SESSION_TYPE = "test.session-item"


def registry():
    value = NodeRegistry()
    value.data_types.register(DataTypeDefinition(
        SESSION_TYPE, 1, {"type": "object"}, default_value={"value": None}, scope="session"))
    value.data_types.register(DataTypeDefinition(
        "test.other-session-item", 1, {"type": "object"}, default_value={}, scope="session"))
    value.register(NodeDefinition(
        "test.provider", "2", "Provider", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT"),), is_output=True,
        provided_session_item_types=({"type_id": SESSION_TYPE, "schema_version": 1},)),
        lambda config, inputs, context: {"output": {"schema_version": 1, "kind": "workflow.text", "text": "x"}})
    value.register(NodeDefinition(
        "test.sink", "1", "Sink", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", multiple=True),), is_output=True),
        lambda config, inputs, context: {})
    return value


def binding(key, registrar, *, type_id=SESSION_TYPE):
    return ObjectBinding(key, type_id, 1, "shared", registrar_node_id=registrar).to_dict()


def document(*, providers=1, registrations=True, provider_type=SESSION_TYPE):
    nodes = [{
        "node_binding_id": str(uuid4()), "component_id": "test.provider", "component_version": "2",
        "title": f"Provider {index}", "position": {"x": 0, "y": index * 100}, "config": {},
    } for index in range(providers)]
    sink_id = str(uuid4())
    nodes.append({"node_binding_id": sink_id, "component_id": "test.sink", "component_version": "1",
                  "title": "Sink", "position": {"x": 300, "y": 0}, "config": {}})
    edges = [{
        "edge_id": str(uuid4()), "source_node_id": node["node_binding_id"], "source_port_id": "output",
        "target_node_id": sink_id, "target_port_id": "input", "order": index,
    } for index, node in enumerate(nodes[:-1])]
    bindings = ([binding(f"item/{index}", node["node_binding_id"], type_id=provider_type)
                 for index, node in enumerate(nodes[:-1])] if registrations else [])
    return {"schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": "Registration", "nodes": nodes, "edges": edges,
            "object_bindings": bindings, "package_lock": [], "execution_roots": [], "control_edges": []}


def config_document(registrar_id, *, registered=True):
    node = {
        "node_binding_id": registrar_id, "component_id": "test.provider", "component_version": "2",
        "title": "Provider", "position": {"x": 0, "y": 0}, "config": {},
        "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
    }
    return {"schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": "Registration config", "nodes": [node], "edges": [],
            "object_bindings": [binding("item/formal", registrar_id)] if registered else [],
            "package_lock": [], "execution_roots": [], "control_edges": []}


def configuration_registry():
    value = registry()
    value.register(NodeDefinition(
        "test.provider-config", "1", "Provider config", "Test", {}, {"type": "object"},
        provided_session_item_types=({"type_id": SESSION_TYPE, "schema_version": 1},)),
        lambda config, inputs, context: {})
    return value


def test_registration_declaration_requires_an_existing_exact_session_type():
    value = NodeRegistry()
    with pytest.raises(ContractValidationError) as error:
        value.register(NodeDefinition(
            "test.bad-provider", "1", "Bad", "Test", {}, {"type": "object"},
            provided_session_item_types=({"type_id": SESSION_TYPE, "schema_version": 1},)), None)
    assert error.value.reason_code == "graph_missing_session_item_type"

    content_only = NodeRegistry()
    content_only.data_types.register(DataTypeDefinition(SESSION_TYPE, 1, {"type": "object"}, scope="content"))
    with pytest.raises(ContractValidationError) as error:
        content_only.register(NodeDefinition(
            "test.bad-provider", "1", "Bad", "Test", {}, {"type": "object"},
            provided_session_item_types=({"type_id": SESSION_TYPE, "schema_version": 1},)), None)
    assert error.value.reason_code == "graph_missing_session_item_type"


def test_object_binding_serializes_registrar_separately_from_owner_and_acl():
    registrar = str(uuid4())
    saved = binding("item/one", registrar)
    assert saved["registrar_node_id"] == registrar
    assert saved["owner_node_id"] is None and saved["readers"] == saved["writers"] == []
    assert ObjectBinding.from_dict(saved).to_dict() == saved
    legacy = {key: value for key, value in saved.items() if key != "registrar_node_id"}
    assert ObjectBinding.from_dict(legacy).to_dict() == legacy


def test_compiler_requires_every_provider_registration_and_exact_item_keys():
    active_registry = registry()
    valid = document(providers=2)
    plan = GraphCompiler(active_registry).compile(valid)
    providers = [node["node_binding_id"] for node in valid["nodes"] if node["component_id"] == "test.provider"]
    registered = {row["registrar_node_id"]: row["object_key"] for row in valid["object_bindings"]}
    assert registered == dict(zip(providers, ("item/0", "item/1")))
    assert len(plan.ordered_node_ids) == 3

    missing = document(registrations=False)
    with pytest.raises(ContractValidationError) as error:
        GraphCompiler(active_registry).compile(missing)
    assert error.value.reason_code == "session_object_registration_missing"

    mismatch = document(provider_type="test.other-session-item")
    with pytest.raises(ContractValidationError) as error:
        GraphCompiler(active_registry).compile(mismatch)
    assert error.value.reason_code == "session_object_registration_mismatch"

    duplicate = document()
    registrar = duplicate["nodes"][0]["node_binding_id"]
    duplicate["object_bindings"].append(binding("item/duplicate", registrar))
    with pytest.raises(ContractValidationError) as error:
        GraphCompiler(active_registry).compile(duplicate)
    assert error.value.reason_code == "session_object_registration_duplicate"

    missing_registrar = document()
    missing_registrar["object_bindings"][0]["registrar_node_id"] = str(uuid4())
    with pytest.raises(ContractValidationError):
        validate_graph_document(missing_registrar)


def test_registration_declaration_cannot_be_added_by_same_version_update():
    active_registry = registry()
    current = active_registry.get_definition("test.sink", "1")
    widened = NodeDefinition(
        current.component_id, current.component_version, current.display_name, current.category,
        deepcopy(current.default_config), deepcopy(current.config_schema),
        inputs=current.inputs, outputs=current.outputs, is_output=current.is_output,
        provided_session_item_types=({"type_id": SESSION_TYPE, "schema_version": 1},))
    with pytest.raises(ContractValidationError) as error:
        active_registry.update_declarations(widened)
    assert error.value.reason_code == "graph_declaration_update_contract_changed"


def test_formal_configuration_saves_and_reads_back_provider_registration(tmp_path):
    active_registry = configuration_registry()
    service = GraphWorkflowService(tmp_path / "registration.sqlite", registry=active_registry)
    try:
        registrar = str(uuid4())
        source = config_document(registrar)
        saved = service.update_definition(
            source, workflow_definition_id=source["workflow_definition_id"],
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        expected = source["object_bindings"][0]
        assert saved["document"]["object_bindings"] == [expected]
        assert service.get_definition(saved["workflow_definition_id"])["object_bindings"] == [expected]

        invalid = config_document(str(uuid4()), registered=False)
        with pytest.raises(ContractValidationError) as error:
            service.update_definition(
                invalid, workflow_definition_id=invalid["workflow_definition_id"],
                expected_edit_sequence=0, idempotency_key=str(uuid4()))
        assert error.value.reason_code == "session_object_registration_missing"
    finally:
        service.close()
