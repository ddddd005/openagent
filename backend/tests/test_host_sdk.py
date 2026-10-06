import copy

import pytest

from phase1_agent.graph_contracts import (
    GraphCompiler, GraphDiagnosticError, NodeDefinition, NodePort, NodeRegistry, validate_graph_document,
)
from phase1_agent.host_sdk import (
    DataTypeDefinition, HostContractError, ObjectBinding, ResourceIdentity, TypeRegistry, WriteIntent,
)

from test_graph_execution import document, edge, identity, node, run_graph


VALUE_SCHEMA = {"type": "object", "properties": {"count": {"type": "integer", "minimum": 0}},
                "required": ["count"], "additionalProperties": False}


def binding(**changes):
    return {"object_key": "plugin/counter", "type_id": "test.counter", "schema_version": 1, "scope": "shared",
            "readers": [identity(1)], "writers": [identity(2)], "owner_node_id": None, **changes}


def counter_registry():
    registry = TypeRegistry()
    registry.register(DataTypeDefinition("test.counter", 1, VALUE_SCHEMA, {"count": 0}))
    return registry


def test_versioned_defaults_and_values_are_detached_from_trusted_declarations():
    registry = TypeRegistry()
    schema, default = copy.deepcopy(VALUE_SCHEMA), {"count": 0}
    registry.register(DataTypeDefinition("test.counter", 1, schema, default))
    schema["properties"]["count"] = {"type": "string"}
    default["count"] = "edited"
    selected = registry.get("test.counter", 1, scope="session")
    selected.schema["properties"].clear()
    selected.default_value["count"] = 99
    returned = registry.default("test.counter", 1)
    returned["count"] = 12
    assert registry.default("test.counter", 1) == {"count": 0}
    assert registry.validate("test.counter", 1, {"count": 2}) == {"count": 2}
    with pytest.raises(HostContractError, match="registered schema"):
        registry.validate("test.counter", 1, {"count": "2"})
    with pytest.raises(HostContractError, match="unavailable"):
        registry.validate("test.counter", 2, {"count": 2})
    with pytest.raises(HostContractError, match="already registered"):
        registry.register(DataTypeDefinition("test.counter", 1, VALUE_SCHEMA, {"count": 1}))


def test_read_only_content_hooks_cannot_rewrite_validated_or_authorized_values():
    registry = TypeRegistry()
    retained = {"scope": "artifact", "output_id": identity(10)}
    foreign = {"scope": "artifact", "output_id": identity(11)}
    original = {"schema_version": 1, "count": 2, "ref": retained}

    def references(value):
        value["count"] = "invalid after validation"
        return [value["ref"]]

    def bindings(value):
        value["count"] = "invalid during binding inspection"
        return []

    def exports(value):
        value["ref"] = foreign
        return [foreign]

    registry.register(DataTypeDefinition(
        "test.hook-content", 1, {"type": "object", "required": ["schema_version", "count", "ref"],
            "properties": {"schema_version": {"const": 1}, "count": {"type": "integer"},
                           "ref": {"type": "object"}}, "additionalProperties": False},
        scope="content", references=references, artifact_bindings=bindings,
        public_references=exports,
    ))
    checked, refs = registry.validate_and_references("test.hook-content", 1, original)
    assert checked == original and refs == [retained]
    assert registry.content_artifact_bindings("test.hook-content", 1, checked) == []
    assert checked == original
    with pytest.raises(HostContractError) as denied:
        registry.public_artifact_references("test.hook-content", 1, original)
    assert denied.value.reason_code == "host_invalid_references"
    assert original == {"schema_version": 1, "count": 2, "ref": retained}


@pytest.mark.parametrize("hook_name", ["references", "artifact_bindings"])
def test_graph_acceptance_preserves_content_after_read_only_hook_inspection(hook_name):
    inspected = []

    def inspect(value):
        inspected.append(copy.deepcopy(value))
        value["count"] = "invalid inspection edit"
        return []

    registry = NodeRegistry()
    registry.data_types.register(DataTypeDefinition(
        "test.inspected-content", 1, {"type": "object", "required": ["schema_version", "count"],
            "properties": {"schema_version": {"const": 1}, "count": {"type": "integer"}},
            "additionalProperties": False}, scope="content", **{hook_name: inspect},
    ))
    registry.register(NodeDefinition(
        "test.inspected-output", "1", "Inspected", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "test.inspected-content"),), is_output=True,
    ), lambda config, inputs, context: {"output": {"schema_version": 1, "count": 2}})
    result = run_graph(document([node(1, "test.inspected-output", registry=registry)]), registry)
    assert result.status == "succeeded"
    assert result.outputs[identity(1)]["output"] == {"schema_version": 1, "count": 2}
    assert inspected == [{"schema_version": 1, "count": 2}]


def test_scope_is_explicit_and_freezing_does_not_freeze_the_source_registry():
    registry = counter_registry()
    registry.register(DataTypeDefinition("test.counter", 1, {"type": "string"}, "global", scope="global"))
    with pytest.raises(HostContractError, match="multiple scopes"):
        registry.get("test.counter", 1)
    assert registry.default("test.counter", 1, scope="global") == "global"
    frozen = registry.detached(frozen=True)
    with pytest.raises(HostContractError, match="frozen"):
        frozen.register(DataTypeDefinition("test.other", 1, {}, None))
    registry.register(DataTypeDefinition("test.counter", 2, VALUE_SCHEMA, {"count": 3}))
    assert frozen.get("test.counter", 2, scope="session") is None
    with pytest.raises(HostContractError, match="lookup"):
        registry.validate("test.counter", True, {"count": 1})


def test_migration_and_reference_mapping_need_declared_hooks_and_exact_references():
    ref = {"scope": "session", "workflow_session_id": identity(10),
           "object_key": "counter", "revision_id": identity(11)}
    registry = TypeRegistry()
    registry.register(DataTypeDefinition("test.pointer", 1, {"type": "object"}, ref,
                                         references=lambda value: [value]))
    assert registry.references("test.pointer", 1, ref) == [ref]
    with pytest.raises(HostContractError, match="mapping"):
        registry.remap("test.pointer", 1, ref, {identity(10): identity(20)})
    current_ref = {**ref, "revision_id": "latest"}
    with pytest.raises(HostContractError, match="exact revision"):
        registry.references("test.pointer", 1, current_ref)
    registry.register(DataTypeDefinition(
        "test.pointer", 2, {"type": "object"}, references=lambda value: [value],
        migrator=lambda value, source: value,
        reference_mapper=lambda value, mapping: {**value, "workflow_session_id":
                                                mapping.get(value["workflow_session_id"], value["workflow_session_id"])},
    ))
    migrated = registry.migrate("test.pointer", 1, 2, ref)
    assert registry.remap("test.pointer", 2, migrated, {identity(10): identity(20)})["workflow_session_id"] == identity(20)


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"readers": [identity(1), identity(1)]}, {"writers": ["not-a-node"]},
    {"scope": "private", "owner_node_id": identity(1), "writers": [identity(2)]},
    {"scope": "shared", "owner_node_id": identity(1)}, {"extra": True},
])
def test_bindings_reject_ambiguous_permissions_and_unknown_fields(changes):
    with pytest.raises(HostContractError):
        ObjectBinding.from_dict(binding(**changes))


def test_binding_optional_default_does_not_turn_into_an_initialized_null():
    omitted = ObjectBinding.from_dict(binding())
    assert omitted.has_default is False and "default_value" not in omitted.to_dict()
    explicit = ObjectBinding.from_dict(binding(default_value=None))
    assert explicit.has_default is True and explicit.to_dict()["default_value"] is None


def test_write_intent_and_global_identity_have_no_implicit_latest_or_content_copy():
    intent = WriteIntent("plugin/counter", 3, "run:node:write", {"count": 4})
    assert WriteIntent.from_dict(intent.to_dict()) == intent
    with pytest.raises(HostContractError):
        WriteIntent("counter", True, "op").to_dict()
    with pytest.raises(HostContractError):
        WriteIntent("counter", 1, "op", "payload", "delete").to_dict()
    reference = ResourceIdentity("workspace", "test.resource", identity(50)).to_dict()
    assert set(reference) == {"envelope_version", "scope", "type_id", "resource_id"}
    assert ResourceIdentity.from_dict(reference).resource_id == identity(50)
    with pytest.raises(HostContractError):
        ResourceIdentity.from_dict({**reference, "value": "old global content"})


def test_graph_v1_remains_strict_and_v2_can_be_saved_when_package_is_missing():
    source, output = node(1), node(2, "workflow.output")
    old = document([source, output], [edge(10, 1, 2)])
    with pytest.raises(GraphDiagnosticError):
        validate_graph_document({**old, "object_bindings": []})
    updated = {**old, "schema_version": 2, "object_bindings": [binding(default_value={"count": 1})],
               "package_lock": [{"package_id": "test.plugin", "version": "1.0.0"}]}
    assert validate_graph_document(updated) == updated
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(NodeRegistry()).compile(updated)
    assert caught.value.reason_code == "graph_missing_package"
    broken = copy.deepcopy(updated)
    broken["object_bindings"][0]["writers"] = [identity(999)]
    with pytest.raises(GraphDiagnosticError, match="missing node"):
        validate_graph_document(broken)


def test_custom_content_schema_versions_are_registered_and_edges_do_not_silently_convert():
    registry = NodeRegistry()
    for version in (1, 2):
        registry.data_types.register(DataTypeDefinition(
            "test.card", version, {"type": "object", "properties": {
                "schema_version": {"const": version}, "kind": {"const": "test.card"}, "text": {"type": "string"},
            }, "required": ["schema_version", "kind", "text"], "additionalProperties": False},
            scope="content",
        ))
    for component, version, output in (("test.source", 1, False), ("test.sink", 2, True)):
        registry.register(NodeDefinition(
            component, "1", component, "test", {}, {"type": "object"},
            inputs=() if not output else (NodePort("input", "test.card", data_schema_version=version),),
            outputs=(NodePort("output", "test.card", data_schema_version=version),), is_output=output,
        ), lambda config, inputs, context: {})
    value = document([node(1, "test.source", registry=registry), node(2, "test.sink", registry=registry)],
                     [edge(10, 1, 2)])
    with pytest.raises(GraphDiagnosticError) as caught:
        GraphCompiler(registry).compile(value)
    assert caught.value.reason_code == "graph_type_mismatch"
    assert registry.validate_content({"schema_version": 2, "kind": "test.card", "text": "ok"}, "test.card", 2)["text"] == "ok"
    with pytest.raises(GraphDiagnosticError):
        registry.validate_content({"schema_version": 1, "kind": "test.card", "text": "old"}, "test.card", 2)
    assert registry.get("test.sink", "1").definition.outputs[0].to_dict()["data_schema_version"] == 2
