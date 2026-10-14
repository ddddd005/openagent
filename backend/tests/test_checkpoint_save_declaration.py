import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry, normalize_checkpoint_save


CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string"},
        "options": {
            "type": "object",
            "properties": {"strategy": {"type": ["string", "null"]}},
            "required": [],
            "additionalProperties": False,
        },
        "members": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    "required": [],
    "additionalProperties": False,
}


def definition(*, checkpoint_save=None):
    fields = {} if checkpoint_save is None else {"checkpoint_save": checkpoint_save}
    return NodeDefinition(
        "example.checkpoint", "1", "Checkpoint", "Test", {}, CONFIG_SCHEMA, **fields,
    )


def test_default_checkpoint_save_is_empty_and_present_in_all_descriptor_exports():
    node = definition()
    registry = NodeRegistry()
    registry.register(node, None)

    assert node.to_dict()["checkpoint_save"] == {"config_paths": []}
    registered = registry.get("example.checkpoint", "1").definition
    assert registered.checkpoint_save == {"config_paths": []}
    assert registered.to_dict()["checkpoint_save"] == {"config_paths": []}
    assert registry.catalog()[0]["checkpoint_save"] == {"config_paths": []}


def test_registration_canonicalizes_paths_and_allows_object_or_array_terminals():
    registry = NodeRegistry()
    registry.register(definition(checkpoint_save={
        "config_paths": [["options", "strategy"], ["members"], ["mode"]],
    }), None)

    normalized = {
        "config_paths": [["members"], ["mode"], ["options", "strategy"]],
    }
    registered = registry.get("example.checkpoint", "1").definition
    assert registered.checkpoint_save == normalized
    assert registered.to_dict()["checkpoint_save"] == normalized
    assert registry.catalog()[0]["checkpoint_save"] == normalized


@pytest.mark.parametrize("declaration", [
    None,
    {},
    {"config_paths": None},
    {"config_paths": [[]]},
    {"config_paths": [[""]]},
    {"config_paths": [["unknown"]]},
    {"config_paths": [["options", "unknown"]]},
    {"config_paths": [["members", "id"]]},
    {"config_paths": [["mode", 1]]},
    {"config_paths": [["mode"], ["mode"]]},
    {"config_paths": [["options"], ["options", "strategy"]]},
    {"config_paths": [], "private_state_paths": []},
])
def test_invalid_checkpoint_save_declarations_are_rejected(declaration):
    with pytest.raises(ContractValidationError) as caught:
        normalize_checkpoint_save(CONFIG_SCHEMA, declaration)
    assert caught.value.reason_code == "graph_invalid_checkpoint_save"


def test_registration_rejects_invalid_paths_before_adding_the_node():
    registry = NodeRegistry()
    with pytest.raises(ContractValidationError) as caught:
        registry.register(definition(checkpoint_save={"config_paths": [["members", "0"]]}), None)

    assert caught.value.reason_code == "graph_invalid_checkpoint_save"
    assert registry.get("example.checkpoint", "1") is None
