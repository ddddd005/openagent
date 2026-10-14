from dataclasses import replace

import pytest

from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageManifest
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import GraphCompiler, NodeDefinition, NodePort, NodeRegistry


def identity(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def source_definition(*, checkpoint_save=None, execution_class="working", display_name="Source"):
    return NodeDefinition(
        "sample.source", "1", display_name, "Test",
        {"value": "initial"},
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        outputs=(NodePort("output", "TEXT"),),
        checkpoint_save=checkpoint_save or {"config_paths": []},
        execution_class=execution_class,
    )


def output_definition():
    return NodeDefinition(
        "sample.output", "1", "Output", "Test", {},
        {"type": "object", "properties": {}, "additionalProperties": False},
        inputs=(NodePort("input", "TEXT"),),
        is_output=True,
    )


def graph_document():
    return {
        "schema_version": 2,
        "workflow_definition_id": identity(900),
        "revision": 1,
        "name": "Declaration snapshot",
        "nodes": [
            {
                "node_binding_id": identity(1), "component_id": "sample.source", "component_version": "1",
                "title": "Source", "position": {"x": 0, "y": 0}, "config": {"value": "hello"},
            },
            {
                "node_binding_id": identity(2), "component_id": "sample.output", "component_version": "1",
                "title": "Output", "position": {"x": 0, "y": 0}, "config": {},
            },
        ],
        "edges": [{
            "edge_id": identity(10), "source_node_id": identity(1), "source_port_id": "output",
            "target_node_id": identity(2), "target_port_id": "input", "order": 0,
        }],
        "object_bindings": [],
    }


def test_node_registry_returns_detached_definition_snapshots_and_freezes_updates():
    registry = NodeRegistry()
    original = source_definition()
    registry.register(original, lambda config, inputs, context: {"output": config["value"]},
                     trusted_scope="sample")
    registry.register(output_definition(), lambda config, inputs, context: {})

    assert original.to_dict()["execution_class"] == "working"
    read = registry.get_definition("sample.source", "1")
    read.config_schema["properties"]["value"]["type"] = "integer"
    entry = registry.get("sample.source", "1")
    entry.definition.default_config["value"] = "changed"
    assert registry.get_definition("sample.source", "1").config_schema["properties"]["value"]["type"] == "string"
    assert registry.get_definition("sample.source", "1").default_config["value"] == "initial"

    plan = GraphCompiler(registry).compile(graph_document())
    frozen = registry.detached(frozen=True)
    updated = replace(
        original,
        checkpoint_save={"config_paths": [["value"]]},
        execution_class="non_working",
    )
    registry.update_declarations(updated, trusted_scope="sample")

    assert registry.get_definition("sample.source", "1").execution_class == "non_working"
    assert registry.get_definition("sample.source", "1").checkpoint_save == {"config_paths": [["value"]]}
    assert plan.definitions[identity(1)].execution_class == "working"
    assert plan.definitions[identity(1)].checkpoint_save == {"config_paths": []}
    assert frozen.get_definition("sample.source", "1").execution_class == "working"
    assert frozen.get_definition("sample.source", "1").checkpoint_save == {"config_paths": []}

    with pytest.raises(ContractValidationError) as caught:
        frozen.update_declarations(updated, trusted_scope="sample")
    assert caught.value.reason_code == "host_registry_frozen"


def test_get_detaches_definition_without_cloning_executor_state():
    class StatefulExecutor:
        def __init__(self):
            self.calls = 0

        def __call__(self, config, inputs, context):
            self.calls += 1
            return {"output": "ok"}

    registry = NodeRegistry()
    executor = StatefulExecutor()
    registry.register(source_definition(), executor)

    entry = registry.get("sample.source", "1")
    assert entry.executor is executor
    entry.executor({}, {}, None)
    entry.definition.default_config["value"] = "local mutation"

    assert executor.calls == 1
    assert registry.get_definition("sample.source", "1").default_config["value"] == "initial"


@pytest.mark.parametrize("execution_class", ["", "Working", "utility", True, None])
def test_registration_rejects_unknown_execution_class_without_publishing(execution_class):
    registry = NodeRegistry()
    with pytest.raises(ContractValidationError) as caught:
        registry.register(source_definition(execution_class=execution_class), None)

    assert caught.value.reason_code == "graph_invalid_execution_class"
    assert registry.get_definition("sample.source", "1") is None


def test_explicit_update_is_scoped_and_changes_only_the_two_declarations():
    registry = NodeRegistry()
    definition = source_definition()
    registry.register(definition, None, trusted_scope="owner")

    with pytest.raises(ContractValidationError) as duplicate:
        registry.register(definition, None, trusted_scope="owner")
    assert duplicate.value.reason_code == "graph_duplicate_type"

    updated = replace(
        definition,
        checkpoint_save={"config_paths": [["unknown"]]},
        execution_class="non_working",
    )
    with pytest.raises(ContractValidationError) as invalid:
        registry.update_declarations(updated, trusted_scope="owner")
    assert invalid.value.reason_code == "graph_invalid_checkpoint_save"
    assert registry.get_definition("sample.source", "1") == definition

    with pytest.raises(ContractValidationError) as wrong_scope:
        registry.update_declarations(
            replace(definition, execution_class="non_working"), trusted_scope="other")
    assert wrong_scope.value.reason_code == "graph_declaration_update_scope"
    assert registry.get_definition("sample.source", "1") == definition

    with pytest.raises(ContractValidationError) as changed_contract:
        registry.update_declarations(
            replace(definition, display_name="Replaced", execution_class="non_working"),
            trusted_scope="owner",
        )
    assert changed_contract.value.reason_code == "graph_declaration_update_contract_changed"
    assert registry.get_definition("sample.source", "1") == definition

    registry.update_declarations(
        replace(definition, execution_class="non_working"),
        trusted_scope="owner",
    )
    assert registry.get_definition("sample.source", "1").execution_class == "non_working"


def test_direct_registry_updates_use_the_shared_host_scope_by_default():
    registry = NodeRegistry()
    definition = source_definition()
    registry.register(definition, None)

    registry.update_declarations(
        replace(definition, execution_class="non_working"),
    )

    assert registry.get_definition("sample.source", "1").execution_class == "non_working"


def test_package_declaration_update_requires_manifest_export_and_same_package_scope():
    identity_value = {"component_id": "sample.source", "component_version": "1"}
    base = NodeRegistry()
    base.register(source_definition(), None, trusted_scope="sample")
    update = replace(
        source_definition(),
        checkpoint_save={"config_paths": [["value"]]},
        execution_class="non_working",
    )

    def package_update(host):
        host.update_node_declarations(update)

    package = CapabilityPackage(PackageManifest(
        "sample", "2.0.0", exports={"nodes": [identity_value]},
    ), package_update)
    loaded = CapabilityPackageLoader((package,), base_registry=base).load({"sample": "2.0.0"})

    assert loaded.registry.get_definition("sample.source", "1").execution_class == "non_working"
    assert loaded.registry.get_definition("sample.source", "1").checkpoint_save == {"config_paths": [["value"]]}
    assert base.get_definition("sample.source", "1").execution_class == "working"


def test_package_cannot_update_a_node_without_declaring_its_exact_export():
    base = NodeRegistry()
    base.register(source_definition(), None, trusted_scope="sample")
    package = CapabilityPackage(
        PackageManifest("sample", "2.0.0"),
        lambda host: host.update_node_declarations(
            replace(source_definition(), execution_class="non_working")),
    )

    with pytest.raises(ContractValidationError) as caught:
        CapabilityPackageLoader((package,), base_registry=base).load({"sample": "2.0.0"})

    assert caught.value.reason_code == "package_export_mismatch"
    assert base.get_definition("sample.source", "1").execution_class == "working"


def test_package_scope_cannot_update_another_package_node():
    base = NodeRegistry()
    base.register(source_definition(), None, trusted_scope="owner")
    package = CapabilityPackage(PackageManifest(
        "other", "1.0.0",
        exports={"nodes": [{"component_id": "sample.source", "component_version": "1"}]},
    ), lambda host: host.update_node_declarations(
        replace(source_definition(), execution_class="non_working")))

    with pytest.raises(ContractValidationError):
        CapabilityPackageLoader((package,), base_registry=base).load({"other": "1.0.0"})

    assert base.get_definition("sample.source", "1").execution_class == "working"
