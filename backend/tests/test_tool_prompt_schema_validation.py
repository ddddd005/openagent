"""Tool prompt parameters must obey the real tool registration rules.

The public content type is the entry point: a valid JSON schema that is not a
strict object tool schema must be rejected with ``tool_prompt_invalid``.
"""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.tool_prompt_contract import tool_prompt_schema

# Recorded before R1 from the unchanged public declaration; the fix may only
# tighten the validator, not the persisted type contract.
DECLARATION_SHA256 = "31134a52a8ec5d49b658ceccdde88acf2fdf317458918f11d51700120bda1b4c"

READ_PARAMETERS = {"type": "object", "properties": {}, "required": [],
                   "additionalProperties": False}
WRITE_PARAMETERS = {
    "type": "object",
    "properties": {"value": {"type": "integer", "description": "The variable value."}},
    "required": ["value"], "additionalProperties": False,
}


def prompt_value(parameters):
    return {
        "schema_version": 1, "kind": "workflow.tool-prompt",
        "agent_node_id": str(uuid4()),
        "source": {"anchor_node_id": str(uuid4()),
                   "component_id": "tool-bindings.variable-tools", "component_version": "2"},
        "list_presentation": {"role": "system", "placement": "before", "depth": None,
                              "order": 0, "enabled": True},
        "parameters_presentation": {"role": "system", "placement": "after", "depth": None,
                                    "order": 0, "enabled": True},
        "entries": [{"binding_id": str(uuid4()), "name": "score_write", "operation": "set",
                     "description": "Set the variable.", "parameters": deepcopy(parameters)}],
    }


def validate_public(parameters):
    registry = create_package_registry().registry
    return registry.data_types.validate(
        "TOOL_PROMPT", 1, prompt_value(parameters), scope="content")


def test_public_declaration_is_unchanged_by_the_validator_fix():
    assert sha256(canonical_bytes(tool_prompt_schema())).hexdigest() == DECLARATION_SHA256


def test_real_read_and_write_parameter_shapes_pass_public_validation():
    read = prompt_value(READ_PARAMETERS)
    write = prompt_value(WRITE_PARAMETERS)
    registry = create_package_registry().registry
    assert registry.data_types.validate("TOOL_PROMPT", 1, read, scope="content") == read
    assert registry.data_types.validate("TOOL_PROMPT", 1, write, scope="content") == write


@pytest.mark.parametrize("parameters", [
    {"type": "string"},
    {"type": "object", "properties": {}},
    {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
    {"type": "object", "properties": {"value": {"type": "integer", "description": "Value"}},
     "required": [], "additionalProperties": False},
    {"type": "object",
     "properties": {"value": {"type": "object", "properties": {}}},
     "required": ["value"], "additionalProperties": False},
    {"type": "object",
     "properties": {"value": {"type": "nope"}},
     "required": ["value"], "additionalProperties": False},
], ids=["string-schema", "missing-structure", "open-object", "required-mismatch",
        "nested-object", "invalid-schema"])
def test_non_strict_or_invalid_parameter_schemas_are_tool_prompt_invalid(parameters):
    with pytest.raises(ContractValidationError) as caught:
        validate_public(parameters)
    assert caught.value.reason_code == "tool_prompt_invalid"


def test_reopened_isolated_database_accepts_legal_tool_prompt_content(tmp_path):
    from contextlib import closing

    from phase1_agent.graph_service import GraphWorkflowService

    path = tmp_path / "reopened-type.sqlite"
    value = prompt_value(WRITE_PARAMETERS)
    with closing(GraphWorkflowService(path)):
        pass
    with closing(GraphWorkflowService(path)) as reopened:
        assert reopened.registry.data_types.validate(
            "TOOL_PROMPT", 1, value, scope="content") == value
